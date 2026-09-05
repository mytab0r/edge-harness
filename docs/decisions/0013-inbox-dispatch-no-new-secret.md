# ADR 0013. Инбокс создаёт issues репозиторным dispatch'ем в свой job — без нового секрета

- **Дата:** 2026-09-06
- **Статус:** принято (реализовано, задача #20). Заменяет [ADR 0011](0011-inbox-issues-token.md).
- **Смежное:** [ADR 0008](0008-narrow-dispatch-token.md) (разделение токенов
  морды и конвейера), [ADR 0011](0011-inbox-issues-token.md) (заменённое
  решение), [задача #20](https://github.com/mytab0r/edge-harness/issues/20),
  `docs/research/21-github-actions.md` («Успешный HTTP-код здесь не является
  доказательством запуска»), спека `openspec/specs/journal-tasks-hands.md`
  (п. 34), `openspec/changes/inbox-native-issue-dispatch/`

## Контекст

ADR 0011 решил создавать issue для директивы инбокса под третьим узким
секретом `GH_ISSUES_TOKEN` (fine-grained, только Issues:RW), вызывая GitHub
Issues API прямо из Durable Object. Решение сутки честно ждало шага владельца
(«Миграция», ADR 0011): создать PAT в UI GitHub и залить секрет. Шаг так и не
был выполнен — секрет `GH_ISSUES_TOKEN` отсутствовал (`gh secret list` пуст
по нему), issues с меткой `source:inbox` не заводилось ни одной. Владелец
решил не заводить секрет вовсе: каждый новый GitHub-токен — постоянная цена
(ещё один PAT, который нужно создать, вращать, документировать в реестре
ADR 0008), а нужная возможность — создание issue сущностью с правом на
Issues — уже реализована в этом репозитории БЕЗ секрета: `orchestra.yml`
объявляет `permissions: issues: write` и штатный `github.token` (эфемерный,
живёт только на время job'а).

ADR 0011 отверг альтернативу «директива → `POST /api/tasks` → issue создаёт
job» двумя доводами:

1. очередь задач — механизм немедленного запуска DSH headless, а не пула;
   семантика «директива лежит в пуле и ждёт исполнителя» — это issue, а не
   hands-ран;
2. job всё равно нужно было бы дать право на Issues — новая поверхность в
   `hands.yml`.

Ни один довод не относится к маршруту, который реализует это ADR. Довод 1 —
об очереди `/api/tasks` конкретно (она порождает `repository_dispatch`
`event_type: harness-task` → `hands.yml` → полноценный DSH headless); эта
раскладка создаёт СВОЙ `repository_dispatch` (`event_type: inbox-issue`) в
СВОЙ job — очередь `/api/tasks`/`hands.yml`/DSH здесь не участвуют вовсе, к
семантике «немедленный запуск исполнителя» это не имеет отношения. Довод 2 —
о новой поверхности прав в `hands.yml` конкретно (тот job уже намеренно
лишён прав на Issues по ADR 0006: аренда снимается до старта DSH, у агента
нет прав на пуш и issues); право `issues: write` этому ADR тоже не нужно
заводить заново — оно уже стоит у `orchestra.yml` в этом же репозитории,
доказанно работает (задачи заводятся в пул этим правом каждый прогон,
`scripts/review/file_tasks.py`), и новый job лишь ЗАИМСТВУЕТ ту же практику
в отдельном узкофокусном файле.

## Решение

**`repository_dispatch` (`event_type: inbox-issue`) под уже существующим
`GH_DISPATCH_TOKEN` → отдельный job `.github/workflows/inbox-issue.yml`,
создающий issue штатным `github.token` под `permissions: issues: write`.**

1. `#dispatchIssueCreation` (DO) шлёт `POST /repos/{repo}/dispatches` с
   `client_payload: {message_id, claimed_ts, title, body}` — title/body уже
   замаскированы (`redact()`) и усечены (title ≤ 80 символов) ДО отправки:
   `client_payload` виден в API/логах Actions, тот же публичный периметр, что
   раньше было тело запроса к Issues API.
2. `inbox-issue.yml` — три строки логики: `gh api -X POST repos/.../issues`
   с метками `task`+`source:inbox`, затем callback в DO. Никакого чекаута,
   никакого DSH: единственное разрешение — `permissions: issues: write` на
   этот job, `github.token` живёт только внутри его запуска.
3. **204 от `dispatches` не доказывает созданную issue** (тот же класс, что
   уже документирован для пульса оркестратора и очереди задач,
   `docs/research/21-github-actions.md`). Замыкание петли — явный callback
   `POST /api/messages/issue-created` (Bearer `HANDS_TOKEN` — тот же канал,
   что уже несёт heartbeat от `hands.yml`) с исходом job'а: `{issue_number,
   issue_url}` при успехе, `{error}` при явном отказе. До callback'а
   сообщение остаётся `processing`; не дождавшийся confirm job (упал, завис,
   не запустился вовсе — тот же необнаружимый по 204 класс) ловится тем же
   ватчдогом (`messageStuckProcessingMs`), что и любая другая зависшая
   проходка инбокса — не новый механизм, тот же самый.
4. `claimed_ts` — момент атомарного захвата сообщения, эхо через
   `client_payload` → job → callback. Callback подтверждает CAS-ом по этому
   значению (тот же инвариант, что у `#finishMessage` везде в инбоксе):
   запоздавшее подтверждение проходки, которую ватчдог уже увёл дальше
   (reclaim → новый dispatch → новый `claimed_ts`), не совпадает и не
   перезаписывает чужой результат — issue не задваивается молча.
5. Секрета `GH_ISSUES_TOKEN` больше нет нигде: `harness.ts`, типы окружения
   (`worker-configuration.d.ts`, генерируется `wrangler types`),
   `deploy-worker.yml`, `.dev.vars.example`, `wrangler.jsonc`. Ошибка
   `issues_not_configured` тоже исчезла — при отсутствии `GH_DISPATCH_TOKEN`/
   `GH_REPO` (тот же секрет несёт очередь задач, пульс оркестратора и теперь
   инбокс) директива честно повторяется с `dispatch_not_configured`.

## Рассмотренные альтернативы

- **Оставить `GH_ISSUES_TOKEN` (ADR 0011 как есть).** Отвергнуто: постоянная
  цена третьего секрета ради возможности, которая уже есть бесплатно —
  `issues: write` у `orchestra.yml`. Секрет к тому же не был заведён почти
  двое суток — накопленные директивы всё это время честно ждали.
- **Отдать создание issue в `orchestra.yml` напрямую (новый job в
  существующем файле).** Отвергнуто: `orchestra.yml` сериализован
  `concurrency: group: orchestra` (архитектурный замок — «два слияния
  никогда не идут параллельно», см. шапку файла) и триггерится `schedule`
  (доставка ~6.7% измерено, `docs/research/21`) плюс `pull_request`/
  `workflow_dispatch` — директива инбокса встала бы в ту же очередь слияний
  и ждала бы её сериализации, а принцип ADR 0011 «директива обязана попасть
  в пул сразу и видимо» это нарушает. Отдельный файл с своей `concurrency`
  (по `message_id`, не глобальной) не разделяет очередь ни с чем.
- **`GH_PIPELINE_PAT` в новом job'е.** Отвергнуто: это широкий PAT владельца
  вне морды (ADR 0008 закрыл именно класс «широкий PAT там, где хватает
  узкого/штатного права»); `github.token` job'а — эфемерный и уже достаточен.

## Последствия

- Реестр GitHub-токенов возвращается к двум: `GH_DISPATCH_TOKEN`
  (Contents+Actions, теперь дополнительно несёт `repository_dispatch`
  инбокса) и `GH_PIPELINE_PAT` (всё остальное, вне морды) — третий,
  `GH_ISSUES_TOKEN`, закрыт полностью.
- Видимый результат (issue с меткой `source:inbox`) наступает не сразу по
  приёму директивы, а после запуска отдельного job'а — то же самое было бы
  верно и для job'а, обслуживающего очередь `/api/tasks` (латентность
  `repository_dispatch → job`, измеренная ADR 0003), и для отдельного
  простого job'а тот же порядок — секунды, не минуты.
- Гвардия `scripts/lib/test_dispatch_token_usage.py::EXPECTED_WORKFLOWS`
  расширена на `inbox-issue.yml`; `secrets.GH_DISPATCH_TOKEN` этот workflow
  не читает вовсе (значение остаётся секретом воркера, дёргает его только
  DO) — правило «узкий токен только в deploy-worker.yml» не нарушено.
- PR #362 (задача #304, автодовод `failed`-директив после появления
  `GH_ISSUES_TOKEN`) потерял смысл целиком вместе с секретом, которого
  больше нет, — закрыт без слияния.
