# Инфраструктура GitHub этого репозитория

Справочник по тому, с чем агент работает на GitHub-стороне: workflow, токены, секреты,
лимиты, границы. Не пересказ документации GitHub — состояние ИМЕННО этого репозитория,
проверенное прогоном (`gh api`), не по памяти. Общие факты о GitHub Actions (лимиты, Terms,
поведение событий) не дублируются — см. [`docs/research/21-github-actions.md`](../research/21-github-actions.md).
Параллельный документ — [`docs/agents/INFRA-CF.md`](INFRA-CF.md) (Cloudflare-сторона: DO, воркер, квоты) —
готовится параллельной задачей #322 (PR #328), порядок слияния этого документа и того не
гарантирован в обе стороны; если ссылка ещё красная — документ в процессе, не ошибка.

## Первым делом — грабли, на которых теряют время каждый день

1. **`gh issue view`/`pr view`/`pr create` идут через GraphQL** и падают чаще, чем
   REST-эндпоинты (`gh api ...`) — при отказе сначала попробуй эквивалент через `gh api`
   (`gh api repos/{repo}/issues/{n}`), прежде чем диагностировать что-то другое.
2. **heredoc в bash-командах блокируется классификатором окружения агента.** Многострочный
   текст (тело issue/PR/коммита) — через `Write` в файл, затем `--body-file`/`-F body=@file`.

Ещё две, специфичные для Windows-агентов (найдены живым прогоном при подготовке этого
документа, 2026-09-05):

3. **`python3 ... | gh ...` и любой subprocess, читающий вывод `gh`, падает на кириллице**
   (`UnicodeDecodeError: 'charmap' codec ... cp1251`), если процесс не переведён явно на UTF-8.
   `PYTHONIOENCODING=utf-8` здесь НЕ спасает: она задаёт кодировку только собственных
   stdin/stdout/stderr процесса, а `subprocess.run(..., text=True)` без явного `encoding`
   декодирует ЧУЖОЙ пайп кодовой страницей консоли (cp1251 на Windows) — эта переменная на
   решение подпроцесса не влияет. Единственное лекарство —
   `subprocess.run(..., encoding="utf-8")` явно в каждом вызове (найдено и исправлено в
   `scripts/lib/claim_task.py::gh()`, вердикт ai-review PR #326). Класс не закрыт целиком —
   именной список мест устареет быстрее этого файла (находка ревью #326: список из трёх
   имён уже отстал от кода на момент проверки), поэтому вместо перечня — команда,
   актуальная всегда:
   `grep -rn 'text=True' scripts | grep -v encoding=` (не стреляет в CI из-за UTF-8-локали
   раннера, стреляет на Windows-агенте). Полное закрытие класса — отдельная задача
   (единый helper вместо `subprocess.run` россыпью).
4. **Смешение `\n` и `\r\n`** в файлах, которые правит и bash, и Windows-инструмент — не
   специфично для GitHub, но регулярно всплывает в диффах workflow-файлов; проверяй
   `git diff` перед коммитом, если правил не через `Edit`/`Write`.

## Инвентарь workflow (`.github/workflows/*.yml`, прочитаны все, 2026-09-06)

Найдено расхождение доки с кодом ревью PR #326/#333: таблица не поспевала за
`.github/workflows/` — `worker-ci.yml` был удалён cleanup-коммитом и
восстановлен задачей #72 (#331) уже ПОСЛЕ прошлой сверки этой таблицы, строка
не добавилась. Защита от такого же протухания молча теперь механическая, не
дисциплина памяти: `scripts/lib/test_infra_gh_inventory.py` в `repo-ci.yml`
требует, чтобы каждый `.yml` из `.github/workflows/` встречался здесь по
имени (и наоборот — удалённый workflow не оставляет мёртвой строки).

| Файл | Триггер | Что делает | Секреты | Vars |
|---|---|---|---|---|
| `worker.yml` | `workflow_dispatch` (task опционален) | Автономный воркер: берёт задачу из пула (или конкретную), DSH headless по `WORKER-PLAYBOOK.md`, открывает PR. Один прогон на репозиторий (`concurrency: worker`) | `GH_PIPELINE_PAT`, `DEEPSEEK_API_KEY`, `HANDS_TOKEN`, `DSH_EDGE_ACCESS_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `HARNESS_URL`, `DSH_EDGE_URL`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` |
| `hands.yml` | `repository_dispatch` (`harness-task`), `workflow_dispatch` (ручной прогон под `manual-<run_id>`) | «Руки» слайса dsh-in-job: исполняет одну задачу из морды, стримит журнал в DO. Аренда задачи — `github.token` (`contents:write`, снимается до старта DSH) | `HANDS_TOKEN`, `DEEPSEEK_API_KEY`, `DSH_EDGE_ACCESS_KEY` | `HARNESS_URL`, `DSH_EDGE_URL`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` |
| `orchestra.yml` | `pull_request` (opened/synchronize/reopened/labeled), `schedule */15`, `workflow_dispatch` | Два джоба: `contract` (обязательная проверка PR↔задача на каждый PR) и `orchestra` (просроченные назначения, метки конфликтов, очередь слияний — ровно один PR/прогон). `concurrency` — на уровне джоба, не файла (класс #189) | `GH_PIPELINE_PAT` (как `ORCHESTRA_PAT`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DSH_EDGE_ACCESS_KEY` (архив сессий) | `DSH_EDGE_URL` |
| `pr-review.yml` | `pull_request` (opened/synchronize/reopened) | Гейт 1: детерминированное ревью диффа (`check_pr.py`), метки `review:ok`/`review:changes-requested`/`review:large`. Доверенный код — чекаут `ref: main`, дерево PR — только данные | `github.token` | — |
| `ai-review.yml` | `workflow_run` (завершение `pr-review`), `workflow_dispatch` (input `pr`) | Гейт 2: AI-ревью диффа. Trust-зона между job'ами: недоверенный DSH-шаг без токенов, доверенный `verdict` — свежая VM, свой чекаут main. Метки `ai:ok`/`ai:changes-requested`/`ai:failed` | `github.token`, `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` |
| `deploy-worker.yml` | `push` на `main` (`cf-worker/**`, `.github/workflows/deploy-worker.yml`), `workflow_dispatch` | Деплой морды (edge-harness) в Cloudflare: проверка секретов → гвардия актуальности типов → гвардия узости `GH_DISPATCH_TOKEN` → `wrangler deploy` → синк секретов воркера → канарейка UI на проде | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `HANDS_TOKEN`, `GH_DISPATCH_TOKEN`, `SESSION_SECRET` | `HARNESS_URL`, `GH_DISPATCH_TOKEN_KIND` |
| `worker-ci.yml` | `push` на `main` (`cf-worker/**`, `.github/workflows/worker-ci.yml`), `pull_request` (без фильтра путей — на каждый PR) | Ворота CI морды до прода (ADR 0004): `worker-test` — контракт фронтенда, typecheck, vitest на настоящем workerd; `canary` — канарейка UI реальным браузером (playwright) против локального `wrangler dev`. Восстановлен задачей #72 (#331) после случайного удаления cleanup-коммитом | `github.token` (read) | — |
| `deploy-dsh-edge.yml` | `schedule` (`37 4 * * *`), `workflow_dispatch` (input `version`) | Деплой морды dsh-edge: source-build с плагинами по манифесту (основной путь) либо npm-префаб (fallback при пустом манифесте). Синк `GH_RUNNER_TOKEN` (=`GH_PIPELINE_PAT`) и опциональных секретов интеграций (#115) в воркер dsh-edge | `CLOUDFLARE_*`, `GH_PIPELINE_PAT`, `DSH_EDGE_ACCESS_KEY`, `DEEPSEEK_*`, `JIRA_*`, `CONFLUENCE_*`, `BITBUCKET_*`, `SLACK_BOT_TOKEN`, `TELEGRAM_*` (интеграционные — опциональны, отсутствие не красит деплой) | `DSH_EDGE_URL`, `DSH_EDGE_PROVIDER_NAME`, `DSH_EDGE_MODEL_CATALOG` |
| `dispatch-latency-probe.yml` | `schedule */15`, `repository_dispatch` (`dispatch-latency-probe`), `workflow_dispatch` | Кампания замера хвоста задержки dispatch→старт job'а (#4/ADR 0005). `tick` шлёт dispatch и пишет CSV/PR под PAT; `probe` — сам измеряемый job | `GH_PIPELINE_PAT` | — |
| `repo-ci.yml` | `pull_request`, `push` на `main` | Обязательная проверка `test` защиты ветки: компиляция Python, гвардии классов (`body=`, провайдер/модель, разделение токенов, реестр меток, concurrency), юнит-тесты скриптов и плагинов, валидность YAML всех workflow | `github.token` (read) | — |
| `codeql.yml` | `push` на `main`, `pull_request`, `schedule` (пн 04:17) | Статический анализ JS/TS → Security → Code scanning | `github.token` (`security-events:write`) | — |

## Токены — роли и права

| Токен | Где живёт | Права | Кто использует | Риск утечки |
|---|---|---|---|---|
| `GITHUB_TOKEN` (`github.token`) | встроенный, на каждый job | Задаётся `permissions:` конкретного job'а (contents/issues/pull-requests/actions read или write) | `hands.yml`, `orchestra.yml` (contract), `pr-review.yml`, `ai-review.yml`, `repo-ci.yml` | Живёт только внутри job'а, гасится с ним. Не зажигает новые workflow-события (кроме `workflow_dispatch`/`repository_dispatch`) — антирекурсия GitHub |
| `GH_PIPELINE_PAT` (секрет репо) | `worker.yml`, `orchestra.yml`, `deploy-dsh-edge.yml` (как `GH_RUNNER_TOKEN`), `dispatch-latency-probe.yml` | Широкий classic PAT владельца (`repo` и т.п.) | Всё, чему нужны operations шире dispatch: push веток, PR, метки, issues, update-branch, синк `GH_RUNNER_TOKEN` runner-bridge'а | Компрометация job'а/скрипта = широкий доступ ко всему аккаунту владельца. Разделён от `GH_DISPATCH_TOKEN` (ADR 0008) намеренно — не сливать использование обратно, гвардируется `scripts/lib/test_dispatch_token_usage.py` |
| `GH_DISPATCH_TOKEN` (секрет репо + секрет воркера edge-harness) | `deploy-worker.yml` (синк в секрет воркера) | Узкий fine-grained PAT **только на этот репозиторий**: Contents RW + Actions RW | Морда (edge-harness): `repository_dispatch` под задачу, `workflow_dispatch` оркестратора/деплоя dsh-edge из пульса DO | Худший сценарий утечки — запуск workflow-run в этом репозитории, не весь аккаунт (ADR 0008). Единственный потребитель по коду — `deploy-worker.yml`; новое использование где-то ещё — красный CI |
| `HANDS_TOKEN` | секрет репо + секрет воркера | Свой прикладной токен (не GitHub PAT) — авторизация job'а перед журналом DO (`/api/heartbeat`, ingest) | `worker.yml`, `hands.yml`, канарейка UI, `deploy-worker.yml` (проверка секретов) | Утечка = можно писать в журнал/heartbeat DO от чужого имени, не GitHub-доступ |
| `GH_RUNNER_TOKEN` (секрет воркера dsh-edge, значение = `GH_PIPELINE_PAT`) | синкается `deploy-dsh-edge.yml` | Тот же широкий PAT, под именем, говорящим о роли (runner-bridge морды dsh-edge вызывает issues + workflow dispatch) | Плагин runner-bridge морды dsh-edge | Сужение — отдельная незакрытая задача (issue #146 в файле ревью, ADR 0008 «Последствия») |
| `DSH_EDGE_ACCESS_KEY` | секрет репо | Прикладной ключ доступа к морде dsh-edge (сессии раннера, #119) | `worker.yml`, `hands.yml`, `orchestra.yml` (архив сессий) | Утечка = доступ к транскриптам сессий морды dsh-edge |

Секреты `JIRA_*`/`CONFLUENCE_*`/`BITBUCKET_*`/`SLACK_BOT_TOKEN` из реестра интеграций
(#115) — прикладные токены внешних систем, не GitHub. Отсутствие в репозитории (см. ниже)
не красит деплой: интеграция без секрета остаётся видимо `not_configured`, это не поломка.

## Секреты и переменные репозитория (прогон `gh api`, 2026-09-05)

Секреты (`gh api repos/mytab0r/edge-harness/actions/secrets`, только имена):
`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `DEEPSEEK_API_KEY`, `DSH_EDGE_ACCESS_KEY`,
`GH_DISPATCH_TOKEN`, `GH_PIPELINE_PAT`, `HANDS_TOKEN`, `NVIDIA_API_KEY`, `SESSION_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Интеграционные секреты (`JIRA_*`, `CONFLUENCE_*`, `BITBUCKET_*`, `SLACK_BOT_TOKEN`),
на которые ссылается `deploy-dsh-edge.yml`, **сейчас не заведены** — интеграции живут в
состоянии `not_configured`, это ожидаемо, не белое пятно.

Переменные (`gh api repos/mytab0r/edge-harness/actions/variables`, имена, без значений):
`HARNESS_URL`, `DSH_EDGE_URL`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DSH_EDGE_PROVIDER_NAME`,
`DSH_EDGE_MODEL_CATALOG`. Текущий провайдер/модель — намеренно не значение здесь, а
команда, которой его смотреть: `gh api repos/mytab0r/edge-harness/actions/variables/DEEPSEEK_MODEL`.
Причина — не лень, а гвардия класса #153 (`scripts/lib/test/provider-default.guard.sh`):
`docs/agents/**` конкатенируется прямо в промпт агента (`WORKER-PLAYBOOK.md` читается
целиком), и литерал текущей модели здесь стал бы вторым местом правды, которое устареет
при следующей смене провайдера и никто не обязан был бы это заметить.

`GH_DISPATCH_TOKEN_KIND` не выставлена — гвардия узости токена (deploy-worker.yml) сейчас
только предупреждает (`::warning::`), не красит деплой (миграция ADR 0008 не завершена шагом
владельца). Не путать с отсутствием секрета — секрет `GH_DISPATCH_TOKEN` заведён, просто его
тип не подтверждён явно.

## Защита ветки `main` (прогон `gh api .../branches/main/protection`, 2026-09-05)

- Обязательные проверки: **`test`** (repo-ci.yml) и **`contract`** (orchestra.yml, джоб
  `contract`). `strict: true` — **ветка обязана быть свежей** (up to date с `main`) перед
  слиянием, не только зелёной.
- `enforce_admins: false` — у владельца прямой пуш технически не заблокирован защитой (см.
  «Ограничения честности» в `PROTOCOL.md`); агенту он недоступен не по правам, а потому что
  required checks не проходят без PR.
- `review:ok`/`ai:ok` — **не** required status checks GitHub, это лейблы. Гейт по ним —
  логика оркестратора (`scripts/lib/review_labels.py::merge_label_gate`), а не защита ветки.
  Обходить их через настройки защиты ветки нельзя (см. «Границы»), но технически они не
  видны в `required_status_checks.contexts`.
- `allow_force_pushes: false`, `allow_deletions: false`.

## Лимиты (прогон `gh api rate_limit`, 2026-09-05, всё «пусто» — не показатель обычного дня)

| Ресурс | Лимит | Комментарий |
|---|---|---|
| `core` (REST) | 5000/час | Личный PAT. `GITHUB_TOKEN` из job'а — отдельный счётчик, 1000/час на репозиторий |
| `graphql` | 5000/час | `gh issue view`/`pr view`/`pr create` идут через него |
| `search` | 30/мин | Отдельный, маленький — не гонять поиск в цикле |
| вторичный, content-generating | **500/час, 80/мин** | Не отображается в `rate_limit` — настоящий потолок для `repository_dispatch`/`workflow_dispatch` извне. Подробности и вывод — [`docs/research/21-github-actions.md`](../research/21-github-actions.md) |
| `schedule` | доставка ~7% измерена кампанией | Не полагаться на cron как на единственный живой канал — см. #297, задача про уход от `*/15`. Публичный репозиторий отключает schedule после 60 дней без активности |

## Инструменты `scripts/gh/`

Прямой доступ к `api.github.com` — как в CI и в облаке, никакого прокси-слоя в
репозитории нет и не должно быть: это не свойство репозитория, а настройка чьей-то
конкретной машины. Если сеть конкретного агента требует прокси — он задаёт стандартную
переменную окружения `HTTPS_PROXY` сам, `gh`/`git`/python подхватывают её без участия
скриптов репозитория. Имя репозитория скрипты не хранят вовсе: `gh api` сам резолвит
плейсхолдер `repos/{owner}/{repo}/...` в текущий репозиторий.

- **`scripts/gh/net.py`** — единая обёртка над `gh api` для python-скриптов ниже (раньше
  `pr_blockers.py`/`queue.py` держали по своей почти идентичной копии).
  Дайджест граблей репозитория печатается из `scripts/gh/infra_digest.sh` — при входе в
  agent-ветку и из `scripts/git/task-branch`, и из доводки уже открытого PR в
  `scripts/worker/task.sh` — единый текст, не вторая копия.
- **`scripts/gh/rate.sh`** — остаток `core`/`graphql`/`search` человеко-читаемо + напоминание
  про вторичный лимит. Пример вывода (живой прогон):
  ```
  core      5000/5000  сброс 19:29:31
  graphql   5000/5000  сброс 19:29:31
  search      30/30    сброс 18:30:31
  Вторичный лимит 500 content-generating запросов/час (dispatch) — без счётчика в API, см. docs/agents/INFRA-GH.md.
  ```
- **`scripts/gh/queue.py`** — вся очередь открытых PR одной строкой на каждый: метки,
  `mergeable_state`, причина, по которой не сливается (тот же предикат, что использует
  `scheduler.py` — `review_labels.merge_label_gate`, не отдельное определение). Листает до
  конца через `review_labels.list_pages` (класс #308/#310 — сырая первая страница по 100
  молча теряет хвост). Живой прогон, 2026-09-05: 24 открытых PR, из них несколько с
  `conflict` (нужен rebase), большинство ждут `ai:ok`.
- **`scripts/gh/pr_blockers.py <N>`** — то же самое для одного PR подробнее (state, draft,
  mergeable, mergeable_state, метки, диагноз). Пример живого прогона:
  ```
  PR #326: '#323: справочник и инструментарий по инфраструктуре GitHub (docs/agents/INFRA-GH.md + scripts/gh/*)'
    state=open draft=False mergeable=True mergeable_state=behind
    labels: review:ok, ai:changes-requested
    Блокирует:
      - нет вердикта ai:ok (ждёт AI-ревью, доработку или повтор после сбоя)
  ```

Все скрипты на отказе называют причину (сеть, gh упал, PR не найден) и не возвращают
пустой/silent-wrong результат.

## Границы — чего агент не делает

- **Push в `main` напрямую** — только ветка `agent/<N>-<slug>` и PR (`scripts/git/task-branch`).
- **Слияние PR** — делает только оркестратор (`orchestra.yml`), по одному за прогон.
- **Закрытие чужих задач** — issue закрывает исполнитель после своей пост-мерж проверки, не
  кто-то по факту слияния.
- **Ротация секретов и токенов** — создание PAT только в UI владельца (см. миграцию ADR 0008,
  шаг 2 — «единственный шаг, который нельзя делегировать»). Ротация `HANDS_TOKEN` — отдельный
  рунбук (#290), тоже не самостоятельное действие агента.
- **Правка защиты ветки `main`** (`branches/main/protection`) — required checks, `strict`,
  `enforce_admins` меняет только владелец осознанно.
- **Отключение обязательных проверок** — `test`/`contract` не отключаются и не обходятся;
  единственный предусмотренный обход контракта — метка `orchestra:skip` на PR для мелочей вне
  пула задач (см. `PROTOCOL.md`), не общий рецепт.

## Не подтверждено

Этот документ — не живой опрос API, а срез, снятый прогоном 2026-09-05 (дата стоит в
заголовках разделов выше). Защита ветки, список секретов/переменных и лимиты меняются
владельцем вне этого репозитория и протухнут первыми же изменениями настроек — документ
сам по себе не узнаёт об этом. Перепроверить срез теми же командами, что снимали его:

```
gh api repos/mytab0r/edge-harness/branches/main/protection
gh api repos/mytab0r/edge-harness/actions/secrets --jq '.secrets[].name'
gh api repos/mytab0r/edge-harness/actions/variables --jq '.variables[].name'
gh api rate_limit
```

Не подтверждено также: сохранится ли для `GH_DISPATCH_TOKEN_KIND` статус «не выставлена»
после того, как владелец завершит миграцию ADR 0008 шагом 2 — раздел «Секреты и переменные»
описывает состояние на дату снятия среза, не гарантию на будущее.

## Куда смотреть при отказе

| Симптом | Смотреть |
|---|---|
| `gh`/`git` виснет или `dial tcp` | Сеть до `api.github.com` недоступна напрямую — `scripts/gh/rate.sh` как быстрая проверка (см. «Инструменты» выше) |
| `gh issue view`/`pr view`/`pr create` падает, а `gh api` того же ресурса работает | GraphQL менее надёжен здесь — переходи на `gh api` |
| PR не сливается, checks зелёные | `python3 scripts/gh/pr_blockers.py <N>` — метки/mergeable_state, не только checks |
| PR «висит», непонятно кто виноват | `python3 scripts/gh/queue.py` — вся очередь разом |
| `schedule`-workflow «не сработал» | Нормально — измеренная доставка ~7% (research/21), не ошибка одного прогона |
| `dispatch` вернул 204, но run не появился | Файл workflow не на `main` (см. research/21, «файл обязан лежать на default branch») |
| Коммит блокирует pre-commit | `git rebase origin/main` — гвардия свежести (или `git config core.hooksPath .githooks` не выставлен) |
| `git commit --no-ff`/merge-коммит падает на pre-commit | Известное ограничение гвардии (#287: проверяет `HEAD`, не `MERGE_HEAD`) — не чинить самому, задача уже заведена |
| Секрет нужен, а его нет в списке выше | Не заводить самому — эскалация владельцу (см. «Границы») |
