# agents-tasks — раздел «Агенты и задачи» в морде

Клиентский плагин `@edge-harness/dsh-agents-tasks` (задача #111): секция
**Агенты и задачи** в сайдбаре dsh-edge (слот `sidebar.section`) и дубликат в
настройках (слот `settings.section`).

## Состав секции

- **Список задач из GitHub Issues** — запрос `GET /repos/{owner}/{repo}/issues?labels=task`.
  Показываются: номер, заголовок, статус (из журнала), исполнитель, ссылки на Issue/PR.
- **Лента событий журнала** — по клику на задачу раскрывается лента
  `GET /api/harness/events?task_id=issue-{number}&limit=20&after=...` с проходом
  до конца выборки (`has_more`/`next_after`). `issue-{number}` — ЕДИНСТВЕННАЯ
  каноническая форма task_id продюсера (`scripts/worker/task.sh`,
  `scripts/hands/dsh_task.sh`), без вариантов.
  - Системные события: `task_queued`, `task_dispatched`, `job_start`, `job_end`,
    `first_heartbeat`, `dispatch_failed` — определяют статус задачи.
  - События `session_event` (от `dsh-hands-streamer`, задача #69) парсятся как чат:
    `think` (agent/request), `tool/call`, `tool/result`, `assistant/message`.
- **Поллинг** — GitHub API каждые 15 с, журнал — каждые 5 с. Живой WS-стрим
  не реализован (см. «Честные пробелы» ниже).
- **Подсказка** — «Новые задачи: создай Issue с меткой `task` — оркестратор
  подхватит и запустит агента».

## Честные пробелы (находки ревью PR #412)

- **Живой WS-стрим не реализован.** Журнал живёт в воркере `edge-harness`
  (cf-worker), морда — в воркере `dsh-edge`. Прокси стороны морды (патч 0005,
  `GET /api/harness/events`) закрывает белое пятно #105 только для REST —
  WebSocket-путь им не покрыт вовсе, same-origin WS упёрся бы в тот же
  401/HTML, что REST до фикса пути выше. Продюсер живого стрима через прокси
  морды — отдельная задача из чеклиста ревью; сейчас статусы обновляются
  поллингом.
- **Статусы пула сегодня в основном `unknown`.** Воркер пишет под
  `issue-<N>` только heartbeat (`first_heartbeat`) — события `task_queued`/
  `task_dispatched`/`job_start`/`job_end` живут под UUID задач мордочной
  очереди (dsh-edge), не под id issue. Пока нет отдельного продюсера,
  пишущего эти события под `issue-<N>` (см. чеклист ревью PR #412), статус
  пула виден только как `unknown`/`running` (по свежести heartbeat), не
  полный `queued → running → done`.

## Как устроен бандл

`src/body.js` — тело фабрики; обёртку и константу `MANIFEST` дописывает
`build.mjs`. Форма обёртки — как у продовых бандлов ростера
(`window.__ModuleLoader__.load({ id: "<package>", factory })`, у фабрики
экспортируются `inject` и `apply`). Модули берутся из seed-карты шелла
(`react`, `@deepseek-ai/dsh-client-ui-primitives` — список зашит в
`build.mjs` и проверяется при сборке), сервисы `ctx.slots`/`ctx.locale`
приносят пакеты из `dsh.client.inject` (`dsh-client-runtime`,
`dsh-client-locale`) — их assemble-standalone-web.mjs проверяет по ростеру
и строит порядок загрузки.

Слоты:
- `sidebar.section` — основной слот для задачи #111 (order 10, вверху сайдбара)
- `settings.section` — fallback (order 90, как plugin-manager)

Словари регистрируются в namespace `agents.tasks` (en/zh/ru, наборы ключей одинаковы).

## Публикация: пока НЕ зарегистрирован в dsh-edge/plugins.json (находка ревью PR #412)

`build.mjs` (и, транзитивно, `test/client.test.mjs`, который сам его
запускает) требует, чтобы пакет уже был объявлен записью в
`dsh-edge/plugins.json` — без неё `node build.mjs` падает громко
(`пакет @edge-harness/dsh-agents-tasks не объявлен в каталоге`). Первая
версия этого PR держала такую запись САМА, но с фиктивным sha256 (нули) и
ссылкой на несуществующий релиз `plugins-agents-tasks-v0.1.0` — repo-ci это
не ловит (не проверяет существование релизов), а ближайший `deploy-dsh-edge.yml`
упал бы громко на `sha256sum -c` ассета. Запись убрана из этого PR.

Штатный путь регистрации — **plugin-forge** (`.github/workflows/plugin-forge.yml`,
`workflow_dispatch` с `plugin_path: plugins-src/agents-tasks`): он сам
собирает пакет, публикует релиз с настоящим sha256 и открывает СВОЙ PR с
записью в `dsh-edge/plugins.json` (апсерт по имени пакета — не дублирует
запись при повторном запуске). До этого PR:

- локальная разработка/тесты требуют временной записи в `plugins.json` (не
  коммитить — только для `node build.mjs`/`node --test .../client.test.mjs`
  на своей машине);
- CI-шаг `node --test plugins-src/agents-tasks/test/client.test.mjs` **не
  подключён** в `repo-ci.yml` тем же самым чисто — подключать его раньше
  появления записи в манифесте означало бы постоянно красный обязательный
  чек; подключается той же следующей PR-парой (запись + шаг), что вводит
  plugin-forge.

Сгенерированное (`client/`, `manifest.json`) в git не хранится: манифест
меняется — пересборка перед каждым `npm pack` обязательна, иначе в бандл
уйдёт устаревший состав. Гард-рейл сборки: свежий `parseManifest` и
проверка seed-require'ов на каждом запуске `build.mjs`.

## Тесты

```bash
node --test plugins-src/agents-tasks/test/client.test.mjs
```

Требует временной записи в `dsh-edge/plugins.json` (см. раздел «Публикация»
выше) — без неё `build.mjs`, который тест сам запускает, откажет.

Поведенческая гвардия (`test/client.test.mjs`, `node --test`, без
зависимостей): обёртка бандла, монтаж в `sidebar.section`/`settings.section`,
паритет ключей словарей и ячейки статусов на прод-форме ответа журнала —
`{events: [{id, kind, data}], has_more, next_after}` с каноническим
`task_id=issue-<N>`; системные события определяют статус задачи;
`session_event` парсится как чат (think/tool/assistant); пагинация добирает
свежие события; отказ и чужой ответ = громкая ошибка. Мутационная проверка:
подмена «последнего события» на «первое» красит набор.