# Cloudflare Free: что реально доступно без единого доллара

> Исследовано 2026-08-28. Смежное: [GitHub Actions](21-github-actions.md), [dsh-edge](11-dsh-edge.md), [отвергнутое](30-rejected-alternatives.md)

## TL;DR

На Workers Free строится почти весь edge-харнес, кроме изоляции чужого кода:

- **Durable Objects есть на Free** с 7 апреля 2025 — но только SQLite-backed. KV-backend DO остаётся Paid.
- **Главный ограничитель — не деньги и не хранилище, а 10 ms CPU на invocation.** Это на порядки жёстче Paid (30 с по умолчанию, до 5 мин). Ожидание LLM по `fetch` CPU не жжёт, а вот любой синхронный разбор/склейка между `await`'ами — жжёт.
- **Ровно один непрерывно живой DO влезает в дневной бюджет.** 86 400 с × 0.128 GB = 11 059 GB-s из 13 000. Второй такой же — уже нет. С гибернацией бюджет практически не расходуется.
- **Исходящий WS-туннель к внешнему раннеру технически работает, но не гибернирует** и держит объект живым максимум 15 минут за соединение. Это и есть причина отказа от туннельной схемы: ~85 % дневного GB-s за один туннель.
- **Изоляция чужого кода на Free невозможна.** Dynamic Workers (бывш. Worker Loaders) — open beta только для paid. Containers / Sandbox SDK — в таблице прайсинга Free = N/A. Минимальный вход $5/мес.
- **Статика SPA (Workers Assets) бесплатна и безлимитна по запросам** на обоих планах.

Проверено по докам 2026-08-28: лимиты Workers, лимиты и прайсинг DO, FAQ DO, лимиты D1, прайсинг R2, прайсинг Containers, биллинг Workers Assets. Расхождений с исходным исследованием не найдено; три уточнения помечены ниже словом «уточнение».

---

## Durable Objects на Free

Дословно: *«Durable Objects are available both on Workers Free and Workers Paid plans»*, но *«Workers Free plan can only create and access SQLite-backed Durable Objects»*. KV-backed DO — Paid.

Хронология, чтобы не искать заново:

| Дата | Что произошло |
|---|---|
| 2025-04-07 | changelog «Durable Objects on Workers Free plan» + «SQLite in Durable Objects GA with 10GB storage per object» |
| 2025-12-12 | changelog: биллинг SQLite-хранилища включается с 2026-01-07 |
| 2026-01-07 | биллинг storage включён. **Free за storage не платит** — у него жёсткие потолки вместо счёта |

### Лимиты Free vs Paid

| | Free | Paid |
|---|---|---|
| Requests | 100 000 / день | 1 млн / мес, далее $0.15/млн |
| Duration | 13 000 GB-s / день | 400 000 GB-s / мес |
| SQLite stored data | 5 GB на аккаунт | без лимита |
| Rows read | 5 млн / день | 25 млрд / мес включено |
| Rows written | 100 000 / день | 50 млн / мес включено |
| Число объектов | Unlimited | Unlimited |
| DO-классов на аккаунт | 100 | 500 |
| Storage на объект | см. «Ловушка» ниже | 10 GB |
| Soft-лимит на объект | 1000 req/s | то же |

При превышении: *«further operations of that type will fail with an error»*. Сброс — в 00:00 UTC.

**Уточнение по источникам.** Requests/Duration/rows живут на странице *pricing*, а не *limits* — на странице лимитов их нет. Ищи в двух местах, иначе решишь, что лимита нет.

### Инцидент: rows_read исчерпан на живом аккаунте (#320)

Не гипотеза — измеренный факт из письма Cloudflare владельцу, 2026-09-03,
дословно: *«You have exceeded the daily Durable Objects free tier limit of
5000000 rows_read. Durable Objects requests that incur rows_read will return
errors until the limit resets on 2026-09-04 at 00:00:00 UTC.»* Account:
Mytab0r@icloud.com's Account, operation `rows_read`, лимит 5 000 000/сутки.

Сходится с симптомом: пульс оркестрации (`cf-worker/src/harness.ts::alarm`,
DO alarm раз в 15 мин) не бился с 04:35 UTC 2026-09-05 — меньше 5 часов после
полуночного сброса того же дня, то есть 5 млн rows_read сожжены заново и
очень быстро. Причина найдена в коде: `#status()` (дергается на КАЖДЫЙ
heartbeat от job — раз в 20 с, `HEARTBEAT_SECS` в `scripts/hands/dsh_task.sh`,
— и на каждый принятый батч событий) гонял `GROUP BY status` и вторую выборку
без индекса по ВСЕЙ исторической таблице `tasks` (ретеншена для неё нет,
растёт вечно), а не по актуальному срезу. Фикс — issue #320: индекс
`tasks(status, dispatch_ts)` под точечный watchdog-запрос plus in-memory кэш
агрегата счётчиков по статусу, инвалидируемый только записью (не временем).
Подробности и оценка объёма чтений — в PR к #320.

**Поведение при исчерпании**, подтверждено этим же письмом: операции с
rows_read (то есть практически любой `SELECT`, включая чтение `alarm`/`get`
через `ctx.storage`, раз оно бэкается той же SQLite) возвращают ошибку до
сброса счётчика в 00:00 UTC. Точный текст/код исключения на стороне workerd
не проверен на живом отказе — см. «Что не подтверждено» ниже.

### Замер факта: rows_read в проде (2026-09-05, задача #320)

Письмо Cloudflare от 2026-09-03 сообщило об исчерпании квоты rows_read; по коду
нашлась причина (`#status()` в `cf-worker/src/harness.ts` делает два полных
скана таблицы `tasks` на каждый heartbeat, раз в 20 с), но числа не было —
оценка «десятки-сотни тысяч на job» ничем не подтверждалась. Инструмент
`scripts/measure/do_rows_read.py` снял факт через Cloudflare GraphQL Analytics
API **до мержа фикса** (индекс + кэш агрегата, PR #321 на момент замера ещё
открыт) — это база для сравнения «было / стало».

**Датасет — не угадан, а найден интроспекцией живой схемы.** Официальная
страница метрик DO называет 4 датасета-кандидата и отсылает к самостоятельной
интроспекции, не называя точное поле. Скрипт идёт по схеме от корня
(`Query.viewer.accounts.<датасет>`) и берёт первый, где объект `sum` реально
содержит `rowsRead`: это `durableObjectsPeriodicGroups`, не
`durableObjectsStorageGroups` (то же имя фигурирует в неофициальном источнике,
но при живой проверке `sum` этого датасета `rowsRead` не содержит).
`durableObjectsPeriodicGroups.filter` поддерживает диапазон (`date_geq`/
`date_leq`), а `dimensions` — и `date`, и `datetimeHour` одновременно, поэтому
неделя снимается одним запросом с почасовой группировкой внутри.

Находка на пути: живая схема Cloudflare нарушает спецификацию GraphQL
introspection — у обёрточных типов (`NON_NULL`/`LIST`) поле `name` приходит
как пустая строка `""`, а не `null`. Разворот типа, писаный по спецификации
(«останавливаться, когда `name is None`»), с этим падал; исправлено на
«falsy» (`None` ИЛИ `""`). Второй нюанс: `[X!]!` — это три уровня `ofType` до
именованного типа, не два, как выглядело бы по типичным примерам в доках.

Прогон: `deploy-worker.yml`, job `measure-rows-read` (workflow_dispatch,
секреты общие с деплоем), запуск на ветке задачи до мержа —
[run 33975023605](https://github.com/mytab0r/edge-harness/actions/runs/33975023605).

| Дата (UTC) | rows_read | % от лимита 5 000 000 | Пиковый час (rows_read) | rows_written |
|---|---|---|---|---|
| 2026-08-30 | 65 323 | 1.3 % | 15:00 (21 473) | 7 114 |
| 2026-08-31 | 1 406 775 | 28.1 % | 17:00 (398 829) | 17 584 |
| 2026-09-01 | 2 697 129 | 53.9 % | 23:00 (1 155 414) | 24 773 |
| 2026-09-02 | 1 547 850 | 31.0 % | 23:00 (888 338) | 8 106 |
| **2026-09-03** | **5 264 205** | **105.3 %** | 21:00 (678 075) | 81 578 |
| 2026-09-04 | 882 708 | 17.7 % | 19:00 (244 065) | 11 417 |
| 2026-09-05 (частично) | 1 346 393 | 26.9 % | 05:00 (496 129) | 13 811 |

**Подтверждает день инцидента напрямую:** 2026-09-03 — единственный день
недели, где факт (105.3 %) превысил лимит, и это ровно дата письма Cloudflare
в #320. Разбивка по `namespaceId` за неделю: один namespace —
`a59c9e7e4ef541878ab82100923cf4e4` — даёт 13 125 347 rows_read из
~13 210 383 суммарных (>99 %), второй — 85 036, третий — 0. Практически весь
расход идёт через одну DO-неймспейс (харнес), что согласуется с гипотезой
кода, а не с чем-то внешним.

**Порядок величины подтверждает гипотезу #status(), не опровергает.** Пиковый
час 2026-09-03 21:00 — 678 075 rows_read; при heartbeat раз в 20 с это 180
вызовов/час → ≈3 767 rows_read на один heartbeat (оба скана `#status()`
вместе), то есть таблица `tasks` в тот момент — порядка 1,5–2 тысяч строк на
скан. Один job с несколькими десятками heartbeat'ов (типичная длительность
задачи в этом харнесе — минуты) даёт **десятки-сотни тысяч rows_read за
job** — оценка из #320 была верна по порядку величины, теперь подтверждена
числом, а не предположением. Часовой пик сам по себе исчерпал бы суточную
квоту за ~7.4 часа (5 000 000 / 678 075), если бы держался весь день —
поэтому реальный пробой квоты (105.3 % за сутки) требует не одного пикового
часа, а устойчиво растущего профиля по мере роста нератенированной таблицы
`tasks` (рост изо дня в день 08-30 → 09-03 в таблице выше это подтверждает).

**После мержа фикса (#321) замер стоит повторить тем же инструментом** —
`python scripts/measure/do_rows_read.py --days 7` через тот же
workflow_dispatch, и сравнить с этой базой.

### Ловушка: доки противоречат сами себе про storage на объект

Таблица лимитов даёт строку «Storage per Durable Object | 10 GB» **без разделения планов**. FAQ на том же сайте пишет дословно: *«When a SQLite-backed Durable Object reaches its maximum storage limit (10 GB on Workers Paid, or 1 GB on the Free plan)»*.

Оба места перепроверены 2026-08-28 — расхождение живое, не устранено. **Проектируй по 1 GB на объект на Free.** Аккаунтовый потолок 5 GB жёсткий в любом прочтении и упрётся раньше при числе объектов > 5.

### Расчёт бюджета Duration — важнейший практический вывод

Объект в памяти держит 128 MB = 0.128 GB. Один непрерывно живой (негибернирующий) DO:

```
86 400 с/сутки × 0.128 GB = 11 059 GB-s/сутки
```

из 13 000 доступных. То есть **ровно один всегда-в-памяти DO влезает в Free, второй уже нет** — и на первый остаётся всего ~1940 GB-s запаса на всё остальное.

С гибернацией картина другая. Дословно с прайсинга: *«Durable Objects that are idle and eligible for hibernation are not billed for duration, even before the runtime has hibernated them»* — то есть счётчик останавливается даже до фактической гибернации, по факту простоя. Бюджет Duration при гибернирующей архитектуре практически не тратится, и узким местом становятся requests и rows written.

**Проектный вывод:** архитектура на Free должна быть гибернирующей или alarm-driven. «Один вечный воркер-демон» — это весь твой бюджет целиком.

---

## CPU-лимиты — главный подводный камень

| | Free | Paid |
|---|---|---|
| CPU на invocation | **10 ms** | 30 с по умолчанию, до 5 мин через `limits.cpu_ms` |

DO подчиняются тому же. FAQ дословно: *«Durable Objects are Worker scripts, and have the same per invocation CPU limits as any Workers do»*.

Что считается invocation для DO — дословно: *«the maximum CPU time per Durable Objects invocation (HTTP request, WebSocket message, or Alarm)»*. **Каждое входящее WS-сообщение — отдельный invocation** со своим бюджетом.

### Механизм сброса

Footnote 4 на странице лимитов DO, дословно:

> «Each incoming HTTP request or WebSocket message resets the remaining available CPU time to 30 seconds. This allows the Durable Object to consume up to 30 seconds of compute after each incoming network request, with each new network request resetting the timer. If you consume more than 30 seconds of compute between incoming network requests, there is a heightened chance that the individual Durable Object is evicted and reset.»

Механизм сброса per-message действует на обоих планах. Но **величина сброса берётся из account-plan-limit**, то есть на Free это 10 ms, а не 30 s. Прямой фразы «на Free сбрасывается до 10 ms» в доках НЕТ — см. [Что не подтверждено](#что-не-подтверждено).

### Ключевое смягчение

Дословно: *«CPU time is active processing time: not time spent waiting on network requests, storage calls, or other general I/O»*. Перепроверено: *«Waiting on network requests (such as fetch() calls, KV reads, or database queries) does not count toward CPU time»*.

**Ожидание ответа LLM по `fetch` CPU не жжёт вообще.** Агент, который 40 секунд ждёт модель, укладывается в 10 ms CPU — если между `await`'ами не делает тяжёлой синхронной работы.

Что реально съедает 10 ms и потому запрещено на горячем пути: разбор больших JSON, конкатенация/regex по мегабайтным строкам, криптография в цикле, сортировка/дедуп больших массивов, генерация диффов. Всё это выносится либо на клиент, либо во внешний раннер, либо разбивается на несколько invocation'ов.

`limits.cpu_ms` (до 300 000) поднимает потолок с дефолтных 30 с. На Free потолок плана 10 ms; разрешения повышать его на Free доки не дают — тоже помечено как не подтверждённое.

---

## WebSocket на Free

*«WebSockets are supported on all Cloudflare plans»* — без разделения по тарифам.

| Параметр | Значение |
|---|---|
| Соединений на объект (Hibernation API) | 32 768 |
| Размер входящего сообщения | 32 MiB |
| Время жизни входящего WS | жёсткого лимита нет; HTTP Request Duration = «No limit» на обоих планах |
| Биллинг requests | 20:1 — *«a 20:1 ratio is applied to incoming WebSocket messages»* |

**Биллинг requests, следствие 20:1:** 100 000 requests/день на Free ≈ **2 млн входящих WS-сообщений в сутки**. Это неожиданно много и практически не является ограничителем для чат-подобной нагрузки.

**Биллинг duration — вот где ловушка:**
- `ws.accept()` держит объект в памяти и жжёт GB-s всё время соединения;
- `state.acceptWebSocket()` (Hibernation API) — нет.

Разница между этими двумя строчками кода — это разница между «бюджет сгорел за сутки одним соединением» и «бюджет практически не тратится».

**Idle-timeout.** Дословно: *«Cloudflare will close a WebSocket connection when no data is transmitted in either direction for a period of time»* — конкретное значение НЕ названо, кастомный timeout доступен только Enterprise. Рекомендация доков — client-side heartbeat ping/pong. Протокольные ping-фреймы рантайм обрабатывает сам: *«Ping/pong handling does not interrupt hibernation»* — то есть heartbeat не будит объект и не ломает экономику гибернации.

---

## Критично: WS-туннель к внешнему раннеру

Это отдельный раздел, потому что это **причина отказа от туннельной схемы**, а не просто ограничение.

1. **Гибернация не работает в эту сторону.** Дословно: *«Hibernation is only supported when a Durable Object acts as a WebSocket server. Outgoing WebSockets do not hibernate.»*

2. **Защита от eviction ограничена 15 минутами.** Changelog 19.06.2026: *«an active outbound WebSocket connection keeps the Durable Object alive and prevents eviction for up to 15 minutes per connection»*. После 15 минут соединение продолжает работать, но перестаёт защищать от eviction — включаются обычные правила (по прежним докам eviction через 70–140 с без входящего трафика). То есть долгий туннель может быть оборван посреди работы, и переподключение обязано быть частью протокола, а не аварийной веткой.

3. **Следствие для Free — арифметика.** Постоянный исходящий WS = объект постоянно в памяти = **~11 059 GB-s/сутки из 13 000, то есть 85 % дневного бюджета**. Один туннель влезает впритык, два — нет. Ни на что другое бюджета не остаётся.

4. **Лимит «Simultaneous open connections = 6» — не то, чем кажется.** Он одинаков на Free и Paid, считает outbound WebSocket тоже, но только пока соединения ждут заголовков ответа: *«Once response headers arrive for a connection, it no longer counts toward the six-connection limit»*. То есть это лимит на 6 одновременных **рукопожатий**, а не на 6 установленных соединений. Планировать архитектуру вокруг «всего 6 коннектов» — ошибка чтения.

**Вывод:** туннель на Free — это не «дорого», это «занимает весь бюджет и всё равно рвётся каждые 15 минут». Схема жизнеспособна только с внешним инициатором соединения (раннер подключается к DO как клиент, DO — сервер, гибернация работает).

---

## Workers Assets (статика SPA) — бесплатно

Дословно: *«Requests to static assets are free and unlimited»* и *«There is no additional cost for storing Assets»* — на обоих планах.

| | Free | Paid |
|---|---|---|
| Files per Worker version | 20 000 | 100 000 |
| Individual file size | 25 MiB | 25 MiB |
| Суммарный размер | не заявлен | не заявлен |

**Оговорка для Free.** При `run_worker_first` совпавшие запросы всегда идут в воркер, и тогда вступают в силу обычные лимиты Workers: *«If you exceed your free tier request limits, these requests will receive a 429 (Too Many Requests) response instead of falling back to static asset serving.»* То есть `run_worker_first` превращает бесплатную безлимитную раздачу в биллящиеся запросы, которые могут кончиться 429 вместо отдачи файла. На Free включать осознанно и узко.

---

## Хранилище: чем платить за состояние

### DO SQLite

Основной вариант. Потолки — в таблице DO выше. **Узкое место — 100 000 записей строк в день**, не объём: 5 GB на аккаунт исчерпать сложнее, чем сотню тысяч write'ов при болтливом логировании в SQLite.

### D1

| | Free | Paid |
|---|---|---|
| БД на аккаунт | 10 | 50 000 |
| Макс. размер одной БД | 500 MB | 10 GB |
| Хранилище на аккаунт | 5 GB | 1 TB |
| Rows read | 5 млн / день | — |
| Rows written | 100 000 / день | — |
| Запросов на invocation | 50 | 1000 |

**Уточнение:** дневные rows read/written на странице лимитов D1 отсутствуют — они на странице прайсинга (и совпадают с общим Free-tier: 5 млн / 100 тыс.). Лимит «50 запросов на invocation» на Free против 1000 на Paid — отдельная засада для кода, который делает N+1 запросов в цикле.

### R2

| Ресурс | Free |
|---|---|
| Storage | 10 GB-month |
| Class A ops | 1 млн / мес |
| Class B ops | 10 млн / мес |
| Egress | бесплатно |

Нулевой egress — фирменная фича, ради неё R2 и берут под блобы и логи. Оговорка: *«The free tier only applies to Standard storage, and does not apply to Infrequent Access storage»*.

### KV

На Free почти бесполезен для состояния: 100 000 чтений/день, **1000 записей/день**. Годится под редко меняющийся конфиг, не под сессии.

---

## Cron Triggers на Free — да

| | Free | Paid |
|---|---|---|
| Триггеров на аккаунт | 5 | 250 |
| Минимальный интервал | 1 минута (`* * * * *`) | то же |
| CPU на тик | 10 ms | 30 с при интервале < 1 ч; 15 мин при ≥ 1 ч |
| Wall-time duration | 15 мин | 15 мин |

Обрати внимание на асимметрию: wall-time 15 минут, а CPU 10 ms. Cron на Free — это «разбуди и сходи по сети», а не «посчитай».

Для DO-архитектуры важнее другое: **alarm = 1 request**, а requests на Free 100 000/день. Alarm-driven DO (проснулся, сходил, поспал) — самый комфортный режим на Free: и Duration почти не тратится, и requests с запасом.

---

## Изоляция чужого кода — на Free нельзя

### Dynamic Workers (бывш. Worker Loaders) — ТРЕБУЕТ PAID

Changelog 24.03.2026: *«Dynamic Workers are now in open beta for all paid Workers users»*. Прайсинг Paid: 1000 уникальных DW/мес + $0.002/DW/день, 10 млн requests/мес, 30 млн CPU-ms/мес.

На Free недоступно → **режим «isolated» dsh-edge на Free построить нельзя.** Точка, без обходных путей.

### Containers / Sandbox SDK — ТРЕБУЕТ PAID

В таблице прайсинга колонка Free = **N/A по всем ресурсам** (memory, CPU, disk) — перепроверено 2026-08-28. Биллинг: *«Containers are billed for every 10ms that they are actively running»*, в рамках $5/мес Workers Paid.

Включено в Paid: 25 GiB-h память, 375 vCPU-мин, 200 GB-h диск, 1 TB egress (NA/EU; 500 GB прочие регионы — **уточнение**, в исходных фактах регион-2 не назван).

Sandbox SDK: *«Available on Workers Paid plan»*, *«Built on Containers»*; DO там — только слой координации. Минимальная цена входа $5/мес.

**Справочно, если решим платить:**

- Типы инстансов: `lite` (1/16 vCPU, 256 MiB, 2 GB диска) → `standard-4` (4 vCPU, 12 GiB, 20 GB).
- Кастомные: 1–4 vCPU, ≤ 12 GiB памяти, ≤ 20 GB диска, минимум 3 GiB памяти на vCPU.
- Аккаунт целиком: 6 TiB памяти / 1500 vCPU / 30 TB диска.
- Время жизни: жёсткого потолка нет (*«Cloudflare does not stop a container instance after a fixed maximum runtime»*), но и гарантии нет (*«does not guarantee that any container instance will run for any set period of time»*).
- `sleepAfter` по умолчанию — 10 минут бездействия.
- Остановка: SIGTERM → до 15 минут → SIGKILL.
- Cold start *«often in the 1-3 second range»*.
- **ГЛАВНАЯ ЗАСАДА:** *«All disk is ephemeral. When a Container instance goes to sleep, the next time it is started, it will have a fresh disk as defined by its container image.»* То есть склонированный git-репозиторий не переживает сон — состояние обязано жить вне контейнера.
- Базовый образ Sandbox SDK: Ubuntu 22.04 с git, unzip, zip, jq, file, inotify-tools, Node 24 + Bun; есть python-вариант с Python 3.11.14.

---

## Общие лимиты Workers (перепроверено)

| Параметр | Free | Paid |
|---|---|---|
| CPU на invocation | 10 ms | 5 мин максимум, 30 с по умолчанию |
| Wall-clock HTTP | «No limit» пока клиент подключён | то же |
| Wall-clock cron / queue / DO alarm | 15 мин | 15 мин |
| Subrequests | 50 / request | 10 000 / request |
| Память | 128 MB на изолят | 128 MB |
| Размер воркера (после gzip) | 3 MB | 10 MB |
| Simultaneous open connections | 6 | 6 |

**Уточнение:** доки теперь дают Paid subrequests как «1000/10 000, up to 10M» — верхняя граница поднята; для Free по-прежнему 50.

**Чего в Workers нет и не будет:**
- `node:child_process` — только *«non-functional stub module»* (с compat date 2026-03-17), стабы *«do not provide a working implementation»*. Ничего не запустить.
- `node:fs` помечен supported, но это **виртуальная ФС изолята**, а не диск с git-репозиторием. Не путать: код «клонируем репо в /tmp» не заработает.

Отсюда прямое следствие: любая работа, требующая процессов и настоящего диска (git, сборки, тесты), физически невозможна внутри Worker/DO — только Containers (Paid) или внешний раннер.

### process.env воркера: секреты и vars доступны из бандла (проверено по докам 2026-08-29)

При включённом `nodejs_compat` воркер заполняет `process.env` всеми
переменными окружения, секретами и version metadata; флаг
`nodejs_compat_populate_process_env` включён **по умолчанию** для
compatibility date ≥ 2025-04-01 (у морды dsh-edge — 2026-08-14, то есть
включён без дополнительных флагов). `process.env` — изолят-глобал: читается
из любого места бандла, включая Durable Object; это канал, по которому
плагин морды (runner-bridge, #95) получает токен GitHub без прокидывания
env через инсталл-цикл. Нюансы: значения коэрцируются в строки;
`process.env.NODE_ENV` wrangler статически заменяет на этапе сборки (не
рантайм-значение); альтернатива — `import { env } from 'cloudflare:workers'`.
Источник: developers.cloudflare.com/workers/runtime-apis/nodejs/process/.
На живом деплое #95 подтверждается тем, что инструмент отвечает не
«токена нет», а осмысленным ответом GitHub.

---

## Workflows (для справки)

*«A Workflow instance can run forever, as long as each step does not take more than the CPU time limit»* — то есть тот же 10 ms на Free упирается в каждый шаг.

| Параметр | Значение |
|---|---|
| Шагов на инстанс | 1024 (free) / 10 000 по умолчанию, до 25 000 (paid) |
| Ретраев на шаг | 10 000 |
| `step.sleep` | до 365 дней |
| Результат шага | 1 MiB |
| Параллельных инстансов | 100 (free) / 50 000 (paid) |

*«Instances in a waiting state are excluded»* из счёта параллельных — то есть спящие инстансы слот не занимают.

---

## Cloudflare AI Gateway (для пула моделей)

**Universal Endpoint** принимает массив провайдеров: *«if the first provider fails, the request falls to the next entry in the array»*. Заголовок `cf-aig-step` показывает, кто отработал, — но помечен deprecated в пользу Dynamic Routing.

**Dynamic Routing:** ноды Rate Limit и Budget Limit *«switch to fallback when exceeded»*, нода Percentage для A/B. Правится без деплоя кода — это его главное преимущество.

**Rate limiting:** fixed/sliding window, действует на весь гейтвей; при превышении отдаёт 429 **без фолбэка**.

Есть кэш ответов и аналитика по запросам/токенам/стоимости.

**BYOK:** *«AI Gateway supports storing multiple API keys for the same provider»*, но выбор ключа **ручной** — по умолчанию алиас `default`, иначе заголовок `cf-aig-byok-alias`; через Unified Billing *«only the default alias is consulted»*.

**ВЫВОД:** автоматической ротации нескольких аккаунтов одного провайдера с кулдауном на 429 у AI Gateway **НЕТ**. Ротацию придётся писать самим (выбор алиаса заголовком + собственный учёт кулдаунов).

---

## Что строится на Free без единого доллара

| Компонент | На Free | Ограничитель, который упрётся первым |
|---|---|---|
| Статика SPA (Workers Assets) | да, полностью | 20 000 файлов, 25 MiB/файл; запросы бесплатны и безлимитны |
| DO с агент-циклом | да, но узко | **10 ms CPU на invocation** — вся синхронная логика между `await`'ами |
| — постоянно живой DO | ровно один | 13 000 GB-s/день; один негибернирующий = ~11 059 |
| — DO на alarm'ах | комфортно | alarm = 1 request; 100 000 requests/день |
| WS с браузером (входящий) | да | 32 768 conn/объект, 32 MiB/сообщение; hibernation → GB-s не капают |
| — расход requests | да | входящие WS-сообщения биллятся 20:1 → 100k requests/день ≈ 2 млн сообщений/день |
| WS-туннель к раннеру (исходящий) | работает, но дорого | не гибернирует; держит объект живым максимум 15 мин; ~85 % дневного GB-s |
| Хранение сессий (DO SQLite) | да | 5 GB аккаунт, ~1 GB объект, 5 млн чтений строк/день, **100 000 записей строк/день — записи и есть узкое место** |
| — альтернатива D1 | да | 10 БД × 500 MB, 5 GB аккаунт, 100k записей/день, 50 запросов на invocation |
| — альтернатива KV | почти бесполезно | 100k чтений/день, **1000 записей/день** |
| — альтернатива R2 (блобы, логи) | да | 10 GB, 1 млн Class A, 10 млн Class B, egress $0 |
| Cron-планировщик | да | 5 триггеров, минимум 1 минута, 10 ms CPU на тик |
| Dynamic Workers (изоляция) | **НЕТ** | open beta только для paid |
| Containers / Sandbox SDK | **НЕТ** | Free = N/A по всем ресурсам, вход $5/мес |

---

## Что не подтверждено

Не выдумывать факты вокруг этих пунктов — они реально не закрыты докой на 2026-08-28.

1. **Сброс CPU-бюджета DO на Free именно до 10 ms.** Следует логически из FAQ («DO — те же воркеры, те же per-invocation CPU limits») + account-plan-limits, но **прямой фразы в доках нет**. Footnote говорит про 30 s, не оговаривая план. Проверять эмпирически перед тем, как строить на этом расчёт.
2. **Что `limits.cpu_ms` не работает / не поднимает потолок на Free.** Разрешения повышать на Free доки не дают, но и явного запрета нет.
3. **Что Hibernation API (`state.acceptWebSocket`) доступен на Free.** Ограничений по планам в доках нет, но и явного «доступен на Free» тоже нет. Запрета нет, разрешения тоже.
4. **Точное значение idle-timeout, после которого CF рвёт WS.** Факт разрыва задокументирован, число — нет. Кастомный timeout только Enterprise.
5. **Нужна ли платёжка (карта) для активации R2.** В доках не сказано.
6. **Лимит числа одновременных WS именно для Free.** Цифра 32 768 дана для Hibernation API без разделения по планам.
7. **Расхождение в самих доках: 10 GB (таблица лимитов) vs 1 GB на Free (FAQ)** для storage на один DO. Перепроверено — противоречие живое. Считать по 1 GB.
8. **Точный текст/код исключения workerd при исчерпании rows_read/rows_written.** Письмо (#320) даёт только формулировку алерта, не текст ошибки, которую ловит код. `classifyStorageError` (`cf-worker/src/harness.ts`) ловит по ключевым словам эвристически — сузить/подтвердить нужно на живом отказе (следующий сброс квоты или CF Logs, если включён log push).
9. **Сколько раз и в каком окне CF ретраит упавший `alarm()`**, если исключение вышло из-под хендлера. Дословной цифры не нашли; полагаться на неё нельзя.

---

## Источники

Все ссылки проверены 2026-08-28; те, что помечены «✓ перепроверено», открывались при написании этого файла.

- [Workers — Limits](https://developers.cloudflare.com/workers/platform/limits/) ✓ перепроверено — CPU, wall-clock, subrequests, память, размер воркера, connections, cron
- [Durable Objects — Limits](https://developers.cloudflare.com/durable-objects/platform/limits/) ✓ перепроверено — storage на объект (10 GB), классы, connections, footnote про сброс CPU
- [Durable Objects — Pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/) ✓ перепроверено — requests/duration/rows Free, 20:1 для WS, гибернация и duration
- [Durable Objects — FAQ](https://developers.cloudflare.com/durable-objects/reference/faq/) ✓ перепроверено — «10 GB on Workers Paid, or 1 GB on the Free plan», «DO are Worker scripts… same per invocation CPU limits»
- [D1 — Limits](https://developers.cloudflare.com/d1/platform/limits/) ✓ перепроверено — БД, размер, storage, запросов на invocation
- [R2 — Pricing](https://developers.cloudflare.com/r2/pricing/) ✓ перепроверено — Forever Free tier, Standard-only
- [Containers — Pricing](https://developers.cloudflare.com/containers/pricing/) ✓ перепроверено — Free = N/A, включённые лимиты Paid, биллинг за 10ms
- [Workers Static Assets — Billing and limitations](https://developers.cloudflare.com/workers/static-assets/billing-and-limitations/) ✓ перепроверено — «free and unlimited», «no additional cost for storing Assets», 429 при `run_worker_first`
- [Durable Objects — WebSockets / Hibernation](https://developers.cloudflare.com/durable-objects/best-practices/websockets/)
- [Durable Objects — Use WebSockets](https://developers.cloudflare.com/durable-objects/api/websockets/)
- [Workers — Runtime APIs: WebSockets](https://developers.cloudflare.com/workers/runtime-apis/websockets/)
- [Workers — Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/) — стабы `node:child_process`
- [Workers — Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Durable Objects — Metrics and analytics](https://developers.cloudflare.com/durable-objects/observability/metrics-and-analytics/) — 4 датасета-кандидата GraphQL, отсылка к интроспекции; проверено 2026-09-05, использовано для замера rows_read (задача #320, `scripts/measure/do_rows_read.py`)
- [Analytics GraphQL API — Introspection](https://developers.cloudflare.com/analytics/graphql-api/features/discovery/introspection/) — механизм интроспекции; проверено 2026-09-05
- [Workflows — Limits](https://developers.cloudflare.com/workflows/reference/limits/)
- [Dynamic Workers (Worker Loaders)](https://developers.cloudflare.com/workers/runtime-apis/bindings/worker-loader/)
- [Sandbox SDK](https://developers.cloudflare.com/sandbox/)
- [AI Gateway — Universal Endpoint](https://developers.cloudflare.com/ai-gateway/universal/)
- [AI Gateway — Dynamic Routing](https://developers.cloudflare.com/ai-gateway/features/dynamic-routing/)
- [AI Gateway — BYOK](https://developers.cloudflare.com/ai-gateway/features/byok/)
- [Cloudflare Changelog](https://developers.cloudflare.com/changelog/) — записи 2025-04-07 (DO на Free, SQLite GA), 2025-12-12 (биллинг storage с 2026-01-07), 2026-03-24 (Dynamic Workers open beta для paid), 2026-06-19 (outbound WS держит объект живым до 15 мин)
