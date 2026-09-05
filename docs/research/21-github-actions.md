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

### 🔴 Замер schedule на этом репозитории: `*/15` доставляет ~7% тиков (2026-09-04)

Кампания dispatch-задержки ([ADR 0005](../decisions/0005-dispatch-tail-campaign.md))
жила на cron `*/15` (workflow `dispatch-latency-probe`, тик = schedule-ран →
`POST /dispatches`). За окно 2026-08-31 00:39 → 2026-09-04 20:56 UTC (116.3 ч)
расписание должно было создать ~465 запусков; создано **31** (**6.7%**), интервалы
между доставками 1.9–7.8 ч, без привязки к четвертям часа. Все 31 доставленных
тика — success, каждый дошёл до диспатча и до строки CSV: терялась **доставка
schedule-события** (ран не создавался вовсе), а не исполнение тика. По данным
кампании это неотличимо от «всё идёт по плану»: ноль таймаутов, ноль сбоев —
видно только по истории runs workflow.

За то же окно dispatch-события (`repository_dispatch` + `workflow_dispatch`)
доставлены **32 из 32**. Вывод: долгие кампании на `schedule` не строятся —
задокументированная «задержка под нагрузкой» на этом репозитории вырождается в
потерю ~93% событий. Живой механизм каденции — самоподдержка: тик в конце
диспатчит следующий `workflow_dispatch` (шаг «Цепочка»), cron остаётся
страховкой смерти цепочки на минутах вне четвертей часа.

Главный оставшийся потребитель `schedule` в этом репо — `orchestra.yml`
(цикл слияний, `*/15`): по этому замеру его пробуждения доставляются в ~7%
случаев, остальное — задержка на десятки минут, отчего готовый PR ждёт слияния
часами. Перенос цикла слияний с канала с измеренной потерей — задача #297.

Улики: [CSV кампании](data/dispatch-latency-tail.csv) на ветке
`data/dispatch-latency-tail` (одна строка на каждый доставленный тик) и история
runs workflow `dispatch-latency-probe` (31 schedule-ран против ~465 ожидаемых).

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

## Метка, поставленная GITHUB_TOKEN, не создаёт событий (замер 2026-08-31, #18)

`check_pr.py` ставит `review:ok` токеном job'а — и `on: pull_request:
types: [labeled]` у workflow ai-review НЕ срабатывает: воркфлоу лежит на main
(мерж b8a320c), событие `labeled:review:ok` на PR #138 доставлено в 17:58:45Z,
run не создан. Это документированная антирекурсия: «When you use the
repository's GITHUB_TOKEN to perform tasks, events triggered by the
GITHUB_TOKEN … will not create a new workflow run» — метки входят в список.

**Рабочий обход — `workflow_run`** (он же документирован для этого класса):
ai-review срабатывает по ЗАВЕРШЕНИЮ workflow pr-review (`workflow_run:
workflows: [pr-review], types: [completed]`) — завершение рана событием
считается независимо от того, что внутри рана ставил GITHUB_TOKEN. Номер PR
в этом событии отсутствует — разрешается по паре `workflow_run.head_branch` +
`head_repository.owner.login` (у форка head в чужом репо) запросом
`pulls?head=<owner>:<branch>` с кросс-чеком `head_sha` рана (уехавшая ветка
пропускается — её ревью придёт новым событием).
Паттерн «метка-триггер» сохраняется для ОРКЕСТРАТОРА (scheduler читает метки
polling'ом по cron — ему события не нужны), но событийный триггер второго
гейта обязан быть workflow_run, не labeled.

**Альтернатива, не выбранная:** ставить метки PAT (события тогда firing) —
второй токен в канале первого гейта ради события; workflow_run дешевле и уже
по построению сериализует «pr-review завершился → ai-review начался».

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
| Задержка (latency/SLA) старта job'а после dispatch | **не документирована вообще**; косвенно известно лишь, что `schedule` задерживается под нагрузкой — на этом репозитории деградация измерена: ~7% доставленных тиков для `*/15` (см. «Замер schedule») |
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

## Возможности платформы, которые мы не используем (аудит 2026-09-05)

Справочники до сих пор описывали то, что мы уже построили, а не то, что реально
даёт платформа — из-за этого месяц писали свою очередь слияний в
`scripts/orchestra/scheduler.py`, не спросив факт про нативную GitHub Merge Queue.
Ниже — по каждой возможности: используем / не используем / недоступно на нашем
плане, с проверкой на этом конкретном репозитории (`gh api`), не по памяти.
Собственник репозитория — пользователь `mytab0r` (не организация), план — Free,
видимость — public.

| Возможность | Статус | Проверено | Что даёт |
|---|---|---|---|
| **Merge Queue** (ruleset `merge_queue`) | **недоступно** | Живой `POST .../rulesets` с правилом `merge_queue` → `422` с пустой деталью; контроль `{"type":"deletion"}` → `201`, контроль богус-типа → другая ошибка схемы («no possible input»). Разница сообщений доказывает: тип распознан, отклонён бизнес-правилом плана/владельца, не формой запроса. Дословно из доки: «available in any public repository owned by an organization». Репозиторий во владении пользователя, не организации. Полный разбор — [ADR 0012](../decisions/0012-native-merge-queue-not-available.md) (задача #339 — ссылка красная до слияния этой ветки, см. заметку про INFRA-CF.md выше по тому же принципу) | Серийное слияние с ре-тестом каждого PR против актуального main, без гонок update-branch/ai-review (#208, #252, #189) |
| **Auto-merge** (`allow_auto_merge`) | доступно, **включено на репо, но не используется** | `gh api repos/mytab0r/edge-harness --jq .allow_auto_merge` → `true` (включено владельцем 2026-09-05) | Автослияние PR, когда required status checks и required reviews зелёные. У нас гейт **на метках** (`review:ok`/`ai:ok`), которые не являются required status checks (см. ниже) — включённый auto-merge слил бы PR сразу после `test`+`contract`, до вердикта ai-review. Не берём **без предварительного перевода меток в статусы** (см. раздел «Что взять вместо своего») |
| **Update branch** (`allow_update_branch`) | доступно, **включено на репо** | То же поле репо → `true` (включено вместе с auto-merge, 2026-09-05) | Кнопка «Update branch» в UI PR для человека. Сам REST-эндпоинт `PUT .../pulls/{n}/update-branch` **уже используется headless** `scripts/orchestra/scheduler.py::update_branch` (с бюджетом «один успешный подтяг за прогон», #252/#288) — переключатель управляет только видимостью кнопки людям, на нашу автоматизацию не влияет никак |
| **Required status checks** vs **метки** | required checks — используем (`test`, `contract`); `review:ok`/`ai:ok` — метки, не checks | `gh api repos/.../branches/main/protection` → `required_status_checks.contexts = ["test","contract"]`, `required_pull_request_reviews: null` | Метки нельзя включить в `required_status_checks.contexts` напрямую — гейт по ним живёт в логике `scheduler.py::merge_label_gate`, не в защите ветки GitHub |
| **CODEOWNERS** / required reviews | **не используем** | Файла `.github/CODEOWNERS`/`CODEOWNERS` в репозитории нет; `required_pull_request_reviews: null` в защите ветки | Автоназначение ревьюеров по пути файла + возможность требовать N approvals — бесплатно на public repo, просто не заведено |
| **Repository rulesets** vs классическая branch protection | используем **классическую** branch protection; rulesets — пусто | `gh api repos/.../rulesets` → `[]`. API работает (см. probe ADR 0012: тестовый ruleset создан и удалён), просто ни одного не оставлено активным | Rulesets умеют слоиться (org+repo) и типы вроде `merge_queue` — но `merge_queue` всё равно недоступен по владению, а остального classic protection хватает |
| `workflow_run` / `repository_dispatch` / `schedule` | используем все три | Инвентарь workflow выше, `docs/research/21` разделы про ловушки (default-branch, события от `GITHUB_TOKEN`) | Обходят антирекурсию GITHUB_TOKEN (`workflow_run`), внешний триггер конкретной задачи (`repository_dispatch`), периодику (`schedule`, но замерена доставка ~7% на `*/15`, см. выше) |
| `merge_group` | **недоступно** (следствие недоступности Merge Queue) | Событие стреляет только при слитой Merge Queue — она недоступна по владению, событие никогда не наступит | Прогон CI над временным объединением веток очереди до реального мержа |
| `concurrency` (группы) | используем | `worker.yml` (repo-wide), `hands.yml`, `orchestra.yml` (per-job, класс #189), `ai-review.yml` (по `workflow_run.id`), `dispatch-latency-probe.yml`, `deploy-dsh-edge.yml` | Сериализация: один воркер-прогон разом, не более одного ai-review на PR одновременно |
| **Reusable workflows** (`workflow_call`) | **не используем** | `grep -r workflow_call .github/workflows` — пусто | Общий job-шаблон вместо копипасты между workflow (например, checkout+setup-node дублируется в 9 файлах) |
| **Composite actions** (`.github/actions/*`) | **не используем** | Каталог `.github/actions/` не существует | То же дублирование шагов checkout/setup, вынесенное в один переиспользуемый шаг |
| `if: always()` | используем, ровно 1 раз | `dispatch-latency-probe.yml` — шаг «Цепочка» самоподдержки каденции | Гарантированный шаг вне зависимости от исхода предыдущих — здесь: продолжить цепочку тиков даже если сам тик отказал |
| **Dependabot** (version updates) | используем | `.github/dependabot.yml`: `npm` (`/cf-worker`, weekly) + `github-actions` (`/`, weekly) | Автоматические PR на обновление зависимостей и версий actions |
| Dependabot **security updates** | **выключено явно** | `gh api repos/... --jq .security_and_analysis.dependabot_security_updates.status` → `disabled` | Автоматические PR на уязвимости отдельно от версийных апдейтов |
| Dependabot **vulnerability alerts** | включено | `gh api .../vulnerability-alerts` → `204` (включено) | Алерты по известным CVE в зависимостях |
| **Secret scanning** / **push protection** | оба включены | `security_and_analysis.secret_scanning.status`/`secret_scanning_push_protection.status` → `enabled` | Обнаружение утечки секретов в истории и блокировка пуша с секретом до попадания в историю |
| Secret scanning **validity checks** / **non-provider patterns** | выключены | Те же поля → `disabled` | Проверка, что найденный секрет ещё живой (не отозван) / поиск по несигнатурным паттернам |
| `actions/cache` (явно) | **не используем**; встроенный `cache: npm` — в 2 из 9 workflow с `setup-node` | `grep -r "actions/cache"` — пусто; `cache: npm` есть только в `deploy-worker.yml` и `worker-ci.yml`, отсутствует в `worker.yml`/`hands.yml`/`deploy-dsh-edge.yml`/`ai-review.yml`/`repo-ci.yml` (несогласованно, не проверено — сознательно это или недосмотр) | Кэш `node_modules`/`~/.npm` между прогонами — ускоряет установку; аккаунт уже держит 18 активных кэшей, 601 MB (`gh api .../actions/cache/usage`) |
| **Artifacts** (`upload-artifact`) | **не используем** | `grep -r "upload-artifact"` — пусто; `gh api .../actions/artifacts --jq .total_count` → `0` | Кампания задержки (`dispatch-latency-probe.yml`) пишет CSV прямо в git-ветку `data/dispatch-latency-tail`, не в artifact — сознательный выбор: артефакты стираются по retention, ветка живёт бессрочно |
| **Environments** + deployment protection rules | **не используем** | `gh api repos/.../environments` → `{"total_count":0}` | Ручной approve перед деплоем, задержка, ограничение по веткам — у нас гейт целиком **до** мержа (required checks + метки), после мержа деплой идёт без паузы |
| GraphQL `closingIssuesReferences` | **не используем** | Живой GraphQL-запрос по PR #326 → пустой список (тело PR ссылается на `#323` прозой, не `Closes #323`) | Автозакрытие issue при мерже PR — у нас закрытие ручное и сознательное: исполнитель закрывает issue после своей пост-мерж проверки (см. «Границы» ниже), а не GitHub по факту слияния |
| GraphQL **sub-issues** | **не используем** | `issue(number:323){ subIssues { totalCount } }` → `0` | Иерархия issue → sub-issue вместо связи прозой/номером в теле |
| **Projects v2** | используем | Борда `edge-harness` (`projectsV2` → номер 2), задокументирована в `docs/agents/PROTOCOL.md` (#182) | Канбан-доска поверх issues/PR, автодобавление по фильтру |

### Что взять вместо своего — см. отдельный документ

Разбор «где своё написано, а платформа даёт готовое» (auto-merge + метки-как-статусы,
Cron Triggers CF vs alarm-пульс, git-ref-замок аренды задачи) — в
[`docs/research/23-platform-native-vs-custom.md`](23-platform-native-vs-custom.md),
чтобы не смешивать факт-аудит (этот раздел) с рекомендациями по замене.

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

Аудит 2026-09-05 (раздел «Возможности платформы, которые мы не используем»):

- [Managing a merge queue](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue) — «available in any public repository owned by an organization»
- [Create a repository ruleset (REST)](https://docs.github.com/rest/repos/rules#create-a-repository-ruleset) — форма ошибки при неподдержанном правиле
- [Create a commit status (REST)](https://docs.github.com/en/rest/commits/statuses) — «Users with push access … can create commit statuses», скоуп `repo:status`, лимит 1000 статусов на sha+context
- Живые `gh api` этого репозитория: `repos/mytab0r/edge-harness` (allow_auto_merge, allow_update_branch, security_and_analysis), `branches/main/protection`, `rulesets`, `actions/cache/usage`, `actions/artifacts`, `vulnerability-alerts`, GraphQL `closingIssuesReferences`/`subIssues`/`projectsV2` — 2026-09-05
