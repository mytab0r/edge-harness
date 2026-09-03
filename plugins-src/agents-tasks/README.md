# agents-tasks — раздел «Агенты и задачи» в морде

Клиентский плагин `@edge-harness/dsh-agents-tasks` (задача #111): секция
**Агенты и задачи** в сайдбаре dsh-edge (слот `sidebar.section`) и дубликат в
настройках (слот `settings.section`).

## Состав секции

- **Список задач из GitHub Issues** — запрос `GET /repos/{owner}/{repo}/issues?labels=task`.
  Показываются: номер, заголовок, статус (из журнала), исполнитель, ссылки на Issue/PR.
- **Лента событий журнала** — по клику на задачу раскрывается лента
  `GET /api/events?task_id=issue:#{number}&limit=20&after=...` с проходом
  до конца выборки (`has_more`/`next_after`).
  - Системные события: `task_queued`, `task_dispatched`, `job_start`, `job_end`,
    `first_heartbeat`, `dispatch_failed` — определяют статус задачи.
  - События `session_event` (от `dsh-hands-streamer`, задача #69) парсятся как чат:
    `think` (agent/request), `tool/call`, `tool/result`, `assistant/message`.
- **Живое обновление** — WebSocket `/api/events.live?after=0` (гибернация DO),
  события пушатся в UI без поллинга. Поллинг GitHub API каждые 15 с, журнал —
  каждые 5 с как фолбэк.
- **Подсказка** — «Новые задачи: создай Issue с меткой `task` — оркестратор
  подхватит и запустит агента».

## Блокер

Журнал живёт в воркере `edge-harness` (cf-worker), морда — в воркере `dsh-edge`.
Секция ходит по относительному пути `/api/events` и `/api/events.live` того же
origin, где стоит страница, — а этот путь в морде **не проксирован** к журналу
(issue #105). Пока #105 не закрыт, журнал вернёт 404/HTML, и content-type-гвардия
превратит это в громкую ошибку статусов; список задач (GitHub API) при этом
работает. Разбор и варианты закрытия — issue #105.

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

## Пересборка tarball

```bash
cd plugins-src/agents-tasks
node build.mjs          # сгенерирует client/client.js + manifest.json, прогонит гвардии
npm pack                # edge-harness-dsh-agents-tasks-0.1.0.tgz
```

Публикация (конвейер #80, по образцу hello-world): релиз **этого**
репозитория с тегом `plugins-agents-tasks-v0.1.0`, asset
`agents-tasks-0.1.0.tgz` (то же содержимое, что у npm-pack'а, имя asset'а
фиксирует манифест), затем sha256 — PR'ом в `dsh-edge/plugins.json`:

```bash
cp edge-harness-dsh-agents-tasks-0.1.0.tgz agents-tasks-0.1.0.tgz
sha256sum agents-tasks-0.1.0.tgz
gh release create plugins-agents-tasks-v0.1.0 agents-tasks-0.1.0.tgz \
  --title "plugins-agents-tasks-v0.1.0 — agents-tasks: раздел «Агенты и задачи» в морде" \
  --notes "Клиентский плагин #111: GitHub Issues пул, журнал как чат, live-обновления."
```

Цикличность sha (принято, по образцу hello/runner): вшитая `manifest.json`
в пакете содержит каталог с sha256 **этого же** пакета — финальной своей sha
там быть не может. Проверяется всегда sha артефакта против каталога репо;
вшитая копия нужна ради ростера `[id, server, client]`, который циклы не имеет.

Сгенерированное (`client/`, `manifest.json`) в git не хранится: манифест
меняется — пересборка перед каждым `npm pack` обязательна, иначе в бандл
уйдёт устаревший состав. Гард-рейл сборки: свежий `parseManifest` и
проверка seed-require'ов на каждом запуске `build.mjs`.

## Тесты

```bash
node --test plugins-src/agents-tasks/test/client.test.mjs
```

Поведенческая гвардия (`test/client.test.mjs`, `node --test`, без
зависимостей): обёртка бандла, монтаж в `sidebar.section`/`settings.section`,
паритет ключей словарей и ячейки статусов на прод-форме ответа журнала —
`{events: [{id, kind, data}], has_more, next_after}`; системные события
определяют статус задачи; `session_event` парсится как чат (think/tool/assistant);
пагинация добирает свежие события; отказ и чужой ответ = громкая ошибка.
Шаг включён в `repo-ci.yml`. Мутационная проверка: подмена «последнего события»
на «первое» красит набор.