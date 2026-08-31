# GitHub Actions как «руки»: лимиты, бесплатность и границы правил

> Исследовано 2026-08-28; дополнено 2026-08-31 (блок GitHub API из egress CF, живой замер). Смежное: [Cloudflare Free](20-cloudflare-free.md), [отвергнутое](30-rejected-alternatives.md)

## TL;DR

1. **Бесплатно — правда, и это не триал.** Standard GitHub-hosted runners на **публичном** репозитории не тратят минуты вообще. Larger runners платны всегда, даже на публичном репо. Self-hosted бесплатен, но для public repo GitHub его почти запрещает по безопасности.
2. **«Безлимит» не значит «без границ».** Job умирает на 6 часах принудительно. Free-план даёт 20 одновременных job'ов. Внешние триггеры упираются не в 5 000 запросов/час, а во **вторичный лимит 500 content-generating запросов в час** — это настоящий потолок dispatch'ей.
3. **Главный ограничитель — не техника, а Terms.** GitHub-hosted раннеры разрешены для «production, testing, deployment, or publication of the software project associated with the repository». Универсальные «руки» для произвольных задач — прямо вне рамок, и цена нарушения не «попросят перестать», а отключение репозитория или блокировка аккаунта.
4. **Схема «6-часовой job держит WS к Durable Object» отвергнута.** Технически она почти работает (см. [вердикт](#вердикт-по-схеме-6-часовой-job-держит-ws-к-cloudflare-do)), по правилам — нет. Наш выбор: **push-модель** — DO дёргает `repository_dispatch` под конкретную задачу, job живёт минуты и умирает.

---

## Бесплатность: что именно бесплатно

Дословно из биллинговой документации:

> "GitHub Actions usage is free for self-hosted runners and for public repositories that use standard GitHub-hosted runners."

> "The use of standard GitHub-hosted runners is free: In public repositories"

Ключевое слово — **standard**. Исключение сформулировано без оговорок:

> "Larger runners are always charged for, even when used by public repositories or when you have quota available from your plan."

Итого практическая матрица:

| Раннер | Public repo | Комментарий |
|---|---|---|
| standard GitHub-hosted (`ubuntu-latest`, `windows-latest`, `macos-latest`) | бесплатно, минуты не списываются | рабочий вариант |
| larger runners (кастомные размеры, GPU, большие vCPU) | **платно всегда** | случайно включается сменой `runs-on` — следить |
| self-hosted | бесплатно | но см. ниже про безопасность |

Про self-hosted на публичном репозитории GitHub высказывается недвусмысленно: self-hosted runners

> "should almost never be used for public repositories"

— потому что недоверенный код из чужого PR может персистентно скомпрометировать машину и добраться до секретов. Это не рекомендация по стилю: раннер у нас свой, изоляции между запусками нет.

**Не подтверждено:** отдельной формулировки про **приватный форк публичного репозитория** в документации нет. Бесплатность привязана к видимости репозитория, в котором идёт run; форк приватен → это приватный репозиторий → минуты списываются из квоты плана. Логика прямая, но дословной цитаты под неё в доках не нашлось.

---

## Лимиты, которые остаются при «безлимите»

Все значения проверены по `docs.github.com/en/actions/reference/limits`.

| Лимит | Значение | Дословно / примечание |
|---|---|---|
| Job на GitHub-hosted | **6 часов** | "Each job in a workflow can run for up to 6 hours of execution time." Job снимается принудительно и **фейлится** |
| Job на self-hosted | 5 дней | "Each job in a workflow can run for up to 5 days of execution time." |
| Workflow run целиком | 35 дней | "This period includes execution duration, and time spent on waiting and approval." Превышение → run **cancelled** |
| Job в очереди | 24 часа, затем автоотмена | ⚠️ см. поправку ниже — относится к **self-hosted** |
| Одновременные jobs | Free **20**, Pro 40, Team 60, Enterprise 500 | считается по всему аккаунту, не по репозиторию |
| Одновременные macOS jobs | 5 (Free/Pro/Team) | общий пул standard + larger |
| Job matrix | 256 | "A job matrix can generate a maximum of 256 jobs per workflow run." |
| Очередь запусков | 500 workflow runs / 10 секунд | при превышении запуски **блокируются**, а не откладываются — событие теряется |
| События-триггеры | 1 500 events / 10 секунд / репозиторий | |
| Размер workflow-файла | 500 KB | "A workflow file larger than 500 KB will not start runs." |
| Re-run | 50 на run | |

### ⚠️ Поправка к исходной сводке: 24 часа очереди — про self-hosted

Исходный черновик записал «job в очереди — 24 часа, затем отмена» как общий лимит. **Проверка по доке этого не подтверждает:** строка "A job can be in the queue for 24 hours before it is automatically cancelled" стоит в таблице в разделе **self-hosted runners**. Для GitHub-hosted отдельного потолка времени ожидания в очереди документация не называет — единственная граница сверху там 35-дневный лимит на весь workflow run (он явно включает время ожидания).

Практический вывод меняется в худшую сторону: **гарантированного «через сутки точно отменится» для GitHub-hosted job'а нет**. Зависший в очереди запуск может занимать слот сколь угодно долго в пределах 35 суток. Если строится что-то, зависящее от предсказуемости старта, ставить собственный watchdog обязательно, а не полагаться на 24 часа.

### 6 часов — не «мягкий» таймаут

Job не получает graceful-сигнала «доработай»: он terminated и fails. Всё, что должно пережить смерть job'а (состояние, курсор, недоделанная задача), обязано быть записано наружу **до** момента истечения, а не в обработчике завершения. Проектировать нужно от «job умирает в произвольный момент», а не от «job живёт 6 часов».

---

## API-лимиты: настоящий потолок — 500/час, а не 5 000

Первичные лимиты (`docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api`):

- **Personal access token пользователя:** "All of these requests count towards your personal rate limit of 5,000 requests per hour."
- **`GITHUB_TOKEN` внутри workflow:** "The rate limit for `GITHUB_TOKEN` is 1,000 requests per hour per repository." Это лимит на вызовы **из job'а наружу**, а не на триггеры извне — путать нельзя, это разные счётчики.

**Вторичные лимиты — вот где реальная граница:**

> "No more than 100 concurrent requests are allowed."

> "No more than 900 points per minute are allowed for REST API endpoints"

> "In general, no more than 80 content-generating requests per minute and no more than 500 content-generating requests per hour are allowed."

### 🔴 Потолок внешних триггеров ≈ 500 в час

`POST /repos/{owner}/{repo}/dispatches` и `POST .../actions/workflows/{id}/dispatches` — **content-generating** запросы. Значит эффективный потолок внешних запусков:

- **80 в минуту**
- **500 в час**

Не 5 000. Разница в десять раз, и наступают на неё легко: любая схема «событие извне → dispatch» с частотой чаще ~8 раз в минуту устойчиво упрётся в потолок. Вторичный лимит отдаёт 403 с `Retry-After` — обрабатывать его надо явно, иначе тихая потеря команд.

Проектное следствие для нашей push-модели: **батчить задачи** в один dispatch (`client_payload` вмещает до 10 top-level свойств), а не слать по одной. 500 запусков в час — это 1 запуск каждые 7 секунд; если бюджет упирается, значит архитектура неверная, а не лимит маленький.

---

## Запуск job'ов извне

### `repository_dispatch`

- **Токен:** classic PAT со scope `repo`; fine-grained token или GitHub App — **Contents: write**.
- `event_type` ≤ 100 символов.
- `client_payload` — до 10 top-level свойств.
- Размер payload: доки указывают **65 535 символов** (исходная сводка писала «< 64KB» — величина того же порядка, но дословная формулировка про символы, а не байты; при не-ASCII содержимом это не одно и то же).

### Узкий dispatch-токен этого репозитория (практика #6, ADR 0008)

Секрет `GH_DISPATCH_TOKEN` (репо и воркер) — **fine-grained PAT только на этот
репозиторий: Contents RW + Actions RW**. Actions нужен не для
repository_dispatch, а потому, что пульс морды тем же токеном зовёт
**workflow_dispatch** оркестратора и деплоя dsh-edge (см. ниже). Всё, чему
нужно шире (пуш веток, PR, метки, issues, update-branch), читает отдельный
широкий `GH_PIPELINE_PAT` — разделение гвардится тестом
`scripts/lib/test_dispatch_token_usage.py` в CI.

Отличие классического PAT от fine-grained по проводку: REST-ответ под classic
PAT несёт заголовок `X-OAuth-Scopes` со списком скоупов (проверено живым
запросом 2026-08-31), под fine-grained заголовка нет. На этом построена гвардия
узости в `deploy-worker.yml`. **Не подтверждено:** появится ли какой-нибудь
`X-OAuth-Scopes` у fine-grained при будущих изменениях GitHub — гвардия
проверяет ровно отсутствие заголовка и красит деплой при его наличии.

### `workflow_dispatch`

- **Токен:** classic PAT со scope `repo`; fine-grained — **Actions: write**.
- `inputs` — до 25 top-level свойств, payload ≤ 65 535 символов.

### 🔴 Ловушка: файл обязан лежать на default branch

Дословно, и **для обоих** событий:

> "This event will only trigger a workflow run if the workflow file exists on the default branch."

Это самая частая потеря времени при отладке: workflow написан в feature-ветке, dispatch отправляется, API отвечает **204 No Content** — и ничего не происходит. Успешный HTTP-код здесь не является доказательством запуска (ровно тот случай, когда «шаг success» врёт). Проверять надо появление run'а, а не код ответа.

### 🔴 События от `GITHUB_TOKEN`: что запускается, а что нет

Дословно:

> "When you use the repository's GITHUB_TOKEN to perform tasks, events triggered by the GITHUB_TOKEN, with the exception of `workflow_dispatch` and `repository_dispatch`, will not create a new workflow run."

То есть push/PR/issue-события от `GITHUB_TOKEN` новых run'ов не создают (защита от рекурсии), а `workflow_dispatch` и `repository_dispatch` — исключение: диспатч сквозь `GITHUB_TOKEN` run запускает. Исключение для `workflow_dispatch` подтверждено живьём: оркестратор диспатчит `worker.yml` через `github.token` (scripts/orchestra/scheduler.py), и воркер-раны происходят. Для `repository_dispatch` через REST из job'а — **не подтверждено** живым прогоном.

Практическое следствие всё равно одно: всё, что должно зажечь проверки (push ветки PR, создание PR, synchronize), делается под PAT (`GH_PIPELINE_PAT`; до задачи #6 эту роль молча выполнял широкий PAT из `GH_DISPATCH_TOKEN`) — иначе CI тихо не просыпается. Кампания замера задержки ([ADR 0005](../decisions/0005-dispatch-tail-campaign.md)) использует PAT на всём пути — и для диспатча в том числе, чтобы не зависеть от непроверенного исключения. В связке с ловушкой default-branch выше: «диспатч ушёл, run'а нет» — проверять обе причины: файл на main и токен-источник события.

### 🔴 GitHub API из egress Cloudflare Workers: измеренный блок 403

Замерено живым прогоном 2026-08-31 (приёмка эпика #17, первый живой тест
runner-bridge; задача #133): из воркера dsh-edge **все** вызовы
`api.github.com` получают **403** с телом, в котором нет JSON-поля `message`
(похоже на страницу блокировки edge-уровня, а не на ответ REST API — иначе
инструмент показал бы текст причины). Измерение:

- POST `/repos/mytab0r/edge-harness/issues` (создание задачи, валидный
  classic PAT из секрета воркера) → 403, повтор тем же ходом → 403 (не transient);
- GET `/repos/mytab0r/edge-harness/issues/17` → тот же 403 — блок не специфичен
  записи, недоступен весь `api.github.com` из этого egress;
- контроль: тот же PAT из job'а GitHub Actions → POST `/issues/17/labels`
  отвечает 200 — токен и права ни при чём.

Гипотеза (не подтверждена): GitHub отклоняет запросы с общих egress-IP
Cloudflare Workers (datacenter-абьюз-фильтр). Важная оговорка: skeleton-воркер
edge-harness успешно делал `repository_dispatch` из CF 2026-08-30 (прогоны
33301581080, 33296416354, form-task_id из морды) — блок либо IP/colo-зависим
и мигрирует, либо egress двух воркеров различается. Проектное следствие: любой
план «Cloudflare → GitHub API» обязан иметь контроль живости egress и честный
отказ; рабочим маршрутом сегодня остаётся «GitHub-раннер → CF» (наш push-контур).

### Задержка старта — не документирована

**SLA или типичной latency между dispatch и стартом job'а в документации нет вообще.** Ни целевого значения, ни «обычно N секунд». Косвенный сигнал того, что задержки бывают заметные, — признание про расписание:

> "The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs."

То есть закладываться на «job стартует через X секунд» нельзя: гарантии нет ни в какой форме.

### Если `schedule` как keepalive

- Минимальный интервал — 5 минут.
- И главное:

> "In a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days."

Расписание на публичном репозитории само себя выключит через 60 дней тишины. Схема, где cron — единственная живая часть, деградирует молча.

---

## Cold start: официальных данных нет

**GitHub нигде не публикует ни целевое, ни типичное время провижининга раннера.** Единственная числовая величина в справочнике по раннерам вообще относится к сети *пользователя* (требование "at least 70 kilobits per second upload and download speeds"), а не к скорости выдачи машины.

Эмпирика третьих лиц (замеры runs-on.com — **не GitHub**, порядок величины, не гарантия):

- ~6 секунд при попадании в тёплый пул;
- ~25 секунд при холодном провижининге.

При этом есть массовые сообщения о многочасовых зависаниях в очереди (community discussions #186198, #147604). Вывод по форме распределения: медиана хорошая, **хвост тяжёлый и ничем сверху не ограничен** — а с учётом поправки выше, для GitHub-hosted даже 24-часовой отсечки нет.

Проектное следствие: любое место, где «запустили и ждём результат», обязано иметь собственный таймаут и путь отката. Ожидание без таймаута здесь — это ожидание, которое иногда длится часы.

---

## Исходящая сеть из job'а: не документирована в обе стороны

Справочник по GitHub-hosted runners описывает **только** необходимые исходящие endpoint'ы к самому GitHub (`github.com`, `api.github.com`, `*.actions.githubusercontent.com`) и способ получить актуальные IP-диапазоны через `GET /meta`.

Чего в документации **нет**:

- явного разрешения произвольного исходящего трафика;
- явного запрета протоколов, портов, долгоживущих соединений.

**Помечено как не подтверждённое в обе стороны.** «Работает у всех» — не то же самое, что «разрешено» и не то же самое, что «не сломается». Строить архитектуру на недокументированном поведении можно только с готовностью, что оно изменится без анонса.

Отдельно:

- **Входящих соединений к раннеру нет** — он за NAT; доки об этом молчат, но обратного никто не обещал.
- Расхожее утверждение про **блокировку SMTP/порт 25** на Azure-инфраструктуре официально **не подтверждено**.

---

## Правила: раздел, из-за которого отвергнут WS-туннель

Источник — GitHub Terms for Additional Products and Features, секция Actions. Рамка задана сразу:

> "Actions and any elements of the Actions product or service may not be used in violation of the Agreement, the GitHub Acceptable Use Policies, or the GitHub Actions service limitations."

Actions запрещено использовать для (дословно):

> - "Cryptomining"
> - "Disrupting, gaining, or attempting to gain unauthorized access to, any service, device, data, account, or network"
> - "The provision of a stand-alone or integrated application or service offering the Actions product or service … for commercial purposes"
> - "Any activity that places a burden on our servers, where that burden is disproportionate to the benefits provided to users"
> - "or any other activity unrelated to the production, testing, deployment, or publication of the software project associated with the repository where GitHub Actions are used"

Последний пункт сформулирован именно для **GitHub-hosted** раннеров — и он же самый широкий.

### Санкции — не «попросят перестать»

> "GitHub may monitor your use of GitHub Actions"

> "Misuse of GitHub Actions may result in termination of jobs, restrictions in your ability to use GitHub Actions, disabling of repositories created to run Actions in a way that violates these Terms, or in some cases, suspension or termination of your GitHub account."

В Acceptable Use Policies к этому добавляется запрет на "automated excessive bulk activity … to place undue burden on our servers" и право GitHub "suspend your Account, throttle your file hosting, or otherwise limit your activity".

Асимметрия рисков здесь решающая: выигрыш — бесплатный вычислительный ресурс; проигрыш — блокировка аккаунта, к которому привязана вся разработка. Это не тот размен, который делают ради экономии на compute.

### Явного пункта про «reverse tunnel» нет

**Помечено:** дословной формулировки, запрещающей именно обратный туннель или удалённый шелл, в Terms **нет**. Но последний буллет покрывает паттерн по смыслу полностью: держать соединение, через которое исполняются произвольные внешние команды, — это ровно "activity unrelated to the production, testing, deployment, or publication of the software project associated with the repository".

### 🚧 Граница, которую надо держать в голове

| Что делает job | Статус |
|---|---|
| Собирает / тестирует / деплоит / публикует **этот же** репозиторий | формально в рамках |
| Работает с артефактами и релизами этого репозитория | в рамках |
| Универсальные «руки»: произвольные задачи, чужие репозитории, скрейпинг, посторонние вычисления, проксирование трафика | **вне рамок однозначно** |

Проверочный вопрос при любой новой автоматизации: *«связана ли эта работа с софтом, который живёт в этом репозитории?»* Если ответ требует объяснений — ответ «нет».

---

## Секреты в публичном репозитории

- Actions secrets **работают** в public repo и доступны job'ам, запущенным событиями `push`, `workflow_dispatch`, `repository_dispatch`, `schedule`.
- Форкам секреты не отдаются, дословно:

  > "With the exception of `GITHUB_TOKEN`, secrets are not passed to the runner when a workflow is triggered from a forked repository."

- Секреты **не** доступны workflow'ам, запущенным событиями Dependabot, и **не** передаются автоматически в reusable workflows.

### ⚠️ Поправка: формулировка про доступ к секретам

Исходный черновик приводил цитату «Any user with write access to your repository have read access to all secrets configured in your repository». **На проверенной странице `use-secrets` этой фразы нет.** Что подтверждается дословно — требование write-доступа для *создания* секретов: "To create secrets or variables on GitHub for an organization repository, you must have `write` access".

Практический вывод при этом не меняется и остаётся верным по другому пути: обладатель write-доступа может закоммитить workflow, который выведет секрет наружу, и запустить его (`workflow_dispatch` требует ровно write). Поэтому **write-доступ следует считать эквивалентным доступу ко всем секретам репозитория** — но это наш вывод из механики, а не цитата из доки. Аналогично, утверждение «GITHUB_TOKEN read-only в PR из форка» на этой странице не найдено (оно живёт в документации по permissions `GITHUB_TOKEN`) — помечено.

### Маскирование в логах ненадёжно

> "Redacting of secrets is performed by your workflow runners. This means a secret will only be redacted if it was used within a job and is accessible by the runner."

Следствие: **производное значение не маскируется**. Base64 от секрета, его подстрока, отдельная часть JWT, хеш — всё это в логах видно.

> "Structured data can cause secret redaction within logs to fail, because redaction largely relies on finding an exact match for the specific secret value."

Следствие: **не класть JSON/XML/YAML в один секрет** — GitHub рекомендует заводить отдельный секрет на каждое значение. Для несекретных, но чувствительных значений — workflow-команда `::add-mask::VALUE`.

При состоявшейся утечке: удалить лог **и** ротировать секрет. Удаление лога без ротации — silent-wrong: выглядит как решение, а компрометация остаётся.

### Главный вектор в public repo — `pull_request_target`

> "Workflows that use these triggers must not explicitly check out untrusted code, including from pull request forks or from repositories that are not under your control"

Относится к `pull_request_target` и `workflow_run`. Оба выполняются в контексте базового репозитория **с секретами** — checkout кода из PR внутри них отдаёт секреты автору PR.

---

## Кто может запустить workflow в публичном репозитории

- **`workflow_dispatch`:** "Write access to the repository is required to perform these steps." Через REST — то же самое (`repo` / `Actions: write`).
- **`repository_dispatch`:** требует `Contents: write` → тоже только write-доступ.
- **Посторонний** может спровоцировать запуск только через `pull_request` из форка. По умолчанию: "By default, all first-time contributors require approval to run workflows". Даже после апрува у такого job'а **нет секретов**, а `GITHUB_TOKEN` — read-only.

**Вывод:** внешний злоумышленник не дёрнет наш «командный» workflow — канал управления закрыт правом на запись. Остаточный риск другой: через поток PR можно **жечь concurrency-квоту** (20 слотов на Free), если политика апрува ослаблена. То есть угроза — не выполнение чужой команды, а **отказ в обслуживании наших собственных задач**. Политику «require approval for all outside collaborators» ослаблять нельзя.

## Новый workflow-файл в PR не получает события этого PR (замер 2026-08-31, #18)

Файл `.github/workflows/ai-review.yml`, существующий ТОЛЬКО на ветке PR (на main его
нет), не запускается от `pull_request`-событий этого PR — при том что:

- `push` этой же ветки файл получает (включая валидацию схемы: битый файл даёт
  run с именем-путём, 0 jobs, conclusion=failure — «This run likely failed
  because of a workflow file issue»);
- соседние workflows, существующие на main, от тех же событий запускаются,
  причём в версии ИЗ ПР-ветки (так в PR #137 гонялись его новые тесты).

Замер: head `d0f6e2b` с валидным (actionlint-чистым) файлом, `labeled:review:ok`
на PR доставлен в 17:39:02Z — run workflow ai-review не создан вовсе.

**Практические следствия:** самоприменение нового workflow на его собственном PR
невозможно до мержа — проверка «работает ли» переносится в пост-мерж (живой
dispatch или первый следующий PR); миграция гейтов, добавляющих НОВОЕ условие
слияния, обязана учитывать уже открытые PR: их события уже отгорели, триггер не
придёт — нужен ручной `workflow_dispatch` (для ai-review: `gh workflow run
ai-review -f pr=<N>`).

**Не подтверждено:** точная формулировка в документации GitHub (искать в
«Events that trigger workflows» оговорку про workflows, отсутствующие на
default branch); замер покрывает `pull_request` + свой файл, случай форка не
проверялся.

---

## Вердикт по схеме «6-часовой job держит WS к Cloudflare DO»

### Технически

Работает, с оговорками, каждая из которых съедает надёжность:

1. **6 часов — жёсткий потолок.** Job убивается принудительно и фейлится. Нужна эстафета: умирающий job инициирует запуск преемника через dispatch.
2. **Файл эстафеты обязан быть на default branch** — иначе dispatch тихо не сработает при 204.
3. **Разрыв между смертью и стартом преемника ничем не ограничен сверху.** С учётом поправки — даже 24-часовой отсечки по очереди для GitHub-hosted нет; формальная граница только 35 дней на весь run. Непрерывности не гарантирует ничто.
4. **Постоянно занятый job съедает 1 из 20 concurrency-слотов** — вычитается из возможности делать реальную работу.
5. **Внешние триггеры упираются в 80/мин и 500/час** (content-generating), а не в 5 000.
6. **Исходящий WS документированно не ограничен, но и не разрешён.** Клиент обязан сам держать ping/reconnect; гарантий по idle-таймаутам промежуточных прокси нет никаких.

Итог технической части: схема даёт не «always-on», а «обычно-on с необъявленными провалами». Для исполнителя команд это худший из режимов — не работает и не сообщает об этом.

### По правилам — ломается

См. [раздел про Terms](#правила-раздел-из-за-которого-отвергнут-ws-туннель). Держать WS, через который исполняются произвольные внешние команды, — деятельность, не связанная с софтом этого репозитория. Риск не «попросят перестать», а отключение репозитория и блокировка аккаунта.

### 🎯 Рекомендация

**Как бесплатный always-on исполнитель произвольных команд — не строить.** Отвергнуто по правилам, а не по технике; техника лишь подтверждает, что и выигрыш был бы сомнительным.

Честные пути:

1. **Self-hosted runner.** Тоже бесплатен, и ограничение про "activity unrelated to the … software project" сформулировано именно для GitHub-hosted. Своя машина — свои правила. Цена: на публичном репозитории небезопасно (см. выше) и надо содержать железо.
2. **Push-модель — НАШ ВЫБОР.** Durable Object дёргает `repository_dispatch` под конкретную задачу; job живёт минуты, делает работу **над этим репозиторием** и умирает. Укладывается:
   - в лимиты — 500 dispatch/час хватает с запасом, слоты не заняты постоянно;
   - в Terms — работа связана с софтом репозитория;
   - и **не требует держать WS вовсе** — исчезает весь класс проблем с обрывами, эстафетой и непрерывностью.

Ключевая мысль для того, кто вернётся к вопросу через полгода: соблазн был не в туннеле как таковом, а в желании получить «постоянно доступный бесплатный компьютер». GitHub Actions им не является — ни по договору, ни по гарантиям. Push-модель решает исходную задачу, не притворяясь, что это не так.

---

## Что не подтверждено

Явный список того, где документация молчит. Строить на этом можно только осознанно.

| Утверждение | Статус |
|---|---|
| Бесплатность **приватного форка** публичного репозитория | отдельной формулировки в доках нет; по общей логике видимости — платно, но цитаты нет |
| Задержка (latency/SLA) старта job'а после dispatch | **не документирована вообще**; косвенно известно лишь, что `schedule` задерживается под нагрузкой |
| Cold start `ubuntu-latest` | официальных данных нет ни в каком виде; ~6 с (тёплый) / ~25 с (холодный) — сторонние замеры runs-on.com, порядок величины |
| Разрешён ли произвольный исходящий трафик из job'а | **не документировано в обе стороны**: ни разрешения, ни запрета протоколов/портов/долгих соединений |
| Ограничения на входящие соединения к раннеру | доки молчат; фактически раннер за NAT, входящих нет |
| Блокировка SMTP / порта 25 на Azure-инфраструктуре | расхожее утверждение, официально **не подтверждено** |
| Явный запрет reverse tunnel в Terms | дословного пункта **нет**; паттерн покрывается буллетом про "activity unrelated to the … software project" |
| «Any user with write access … has read access to all secrets» | ⚠️ **дословной цитаты на странице use-secrets не нашлось**. Подтверждено: write нужен для создания секретов. Эквивалентность write ≈ доступ к секретам — наш вывод из механики (write → коммит workflow → запуск) |
| «GITHUB_TOKEN read-only в PR из форка» | на странице use-secrets не найдено; относится к документации по permissions `GITHUB_TOKEN` |
| 24 часа в очереди для GitHub-hosted | ⚠️ **опровергнуто**: лимит стоит в разделе self-hosted. Для GitHub-hosted аналога нет — только 35 дней на весь run |
| `client_payload` «< 64KB» | ⚠️ **уточнено**: доки говорят 65 535 **символов**, не байт |
| Причина 403 GitHub API из egress CF Workers (замер 2026-08-31, см. раздел выше) | тело ответа не прочитано (нужен observability воркера); масштаб блока — IP/colo/аккаунт — не определён; влияет ли на skeleton-диспетч сегодня — не проверено |

---

## Источники

Все ссылки проверены 2026-08-28; цитаты в тексте сверены с живыми страницами.

- [Billing: GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions) — бесплатность public / standard runners, платность larger runners
- [Actions reference: limits](https://docs.github.com/en/actions/reference/limits) — 6 часов, 35 дней, concurrency, matrix, очередь запусков, размер workflow-файла
- [REST API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) — 5 000/час PAT, 1 000/час `GITHUB_TOKEN`, вторичные лимиты 80/мин и 500/час
- [Events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows) — `repository_dispatch`, `workflow_dispatch`, default-branch, лимиты payload, `schedule`
- [GitHub Terms for Additional Products and Features](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features) — секция Actions: запреты и санкции
- [Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies) — bulk activity, право ограничить аккаунт
- [Use secrets in GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets) — форки, Dependabot, reusable workflows
- [Security hardening / secure use reference](https://docs.github.com/en/actions/reference/security/secure-use) — маскирование, structured data, `pull_request_target`, self-hosted на public repo
- [GitHub-hosted runners reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) — сетевые требования, endpoint'ы, `GET /meta`
- Сторонние (не GitHub, для порядка величины): замеры cold start runs-on.com; community discussions #186198, #147604 — жалобы на многочасовые очереди
