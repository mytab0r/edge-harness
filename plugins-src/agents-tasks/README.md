# agents-tasks — раздел «Агенты и задачи» в морде

Клиентский плагин `@edge-harness/dsh-agents-tasks` (задачи #111 → #407):
секция **Агенты и задачи** в сайдбаре dsh-edge (слот `sidebar.section`) и
второй вход в настройках (слот `settings.section`).

## Состав секции

- **Список задач из GitHub Issues** — запрос
  `GET /repos/{owner}/{repo}/issues?labels=task` (без токена). Показываются:
  номер, заголовок, статус (из журнала), исполнитель, ссылки на Issue/PR.
- **Лента событий журнала** — по клику на задачу раскрывается лента
  `GET /api/harness/events?task_id=issue-{number}&limit=20&after=...` с проходом
  до конца выборки (`has_more`/`next_after`). `issue-{number}` — ЕДИНСТВЕННАЯ
  каноническая форма task_id продюсера (`scripts/worker/task.sh`,
  `scripts/hands/dsh_task.sh`), без вариантов. Путь — прокси стороны морды
  (патч 0005), same-origin `/api/events` — чужой API морды, не журнал.
  - Системные события: `job_start`, `job_end`, `first_heartbeat`,
    `dispatch_failed` — определяют статус задачи; `task_queued`/
    `task_dispatched` в словаре есть, но под `issue-<N>` не пишутся
    (см. «Честные пробелы»).
  - События `session_event` (от `dsh-hands-streamer`, задача #69) парсятся как чат:
    `think` (agent/request), `tool/call`, `tool/result`, `assistant/message`.
- **Поллинг** — один цикл, каждые 90 с: и список задач (GitHub API), и журнал
  (прокси морды). 90 с — не случайность: список ходит в api.github.com без
  токена, анонимный лимит 60 req/h на IP; 90 с дают 40 req/h с запасом.
  При 403 автопроллинг встаёт на паузу (тики пропускаются, интерфейс
  показывает подсказку); газ паузы — кнопки «Обновить»/«Повторить» (обычный
  loadTasks: успех снимает паузу, повторный 403 ставит снова). Живого
  WS-стрима нет (см. «Честные пробелы»).
- **Подсказка при пустом пуле** — «Issue с меткой "task" появятся здесь»
  (`noTasksHint`, en/zh/ru в `src/body.js`).

## Честные пробелы (находки ревью PR #412)

- **Живой WS-стрим не реализован.** Журнал живёт в воркере `edge-harness`
  (cf-worker), морда — в воркере `dsh-edge`. Прокси стороны морды (патч 0005,
  `GET /api/harness/events`) закрывает белое пятно #105 только для REST —
  WebSocket-путь им не покрыт вовсе. Продюсер живого стрима через прокси
  морды — отдельная задача из ревью PR #412; сейчас всё живёт на поллинге 90 с.
- **Статус `queued`/`dispatched` для issue-задач не появится.** Под `issue-<N>`
  пишется весь батч job'а (`job_start`/`session_event`/`job_end` —
  `scripts/hands/dsh_task.sh::flush_events`, задача воркера бьётся heartbeat'ом
  `scripts/worker/task.sh`), поэтому `running`/`done`/`failed` работают. Но
  `task_queued`/`task_dispatched` пишет только UUID-очередь морды
  (`cf-worker/src/harness.ts`) — эти статусы относятся к задачам мордочной
  очереди, не к issue. Пул задач, ещё не взятых воркером, честно показывает
  `unknown`.
- **Двойная регистрация слота — не fallback.** Секция регистрируется
  безусловно в оба слота (`sidebar.section` и `settings.section`): клиентский
  API не позволяет проверить наличие слота перед регистрацией, а
  существование `sidebar.section` в собранной морде не подтверждено — в
  `docs/research/11-dsh-edge.md` описан только `settings.section`. Цена —
  дублирование секции в настройках при живом сайдбаре; сверить слот с деплоя
  владельца — отдельная задача из ревью PR #412.

## Как устроен бандл

`src/body.js` — тело фабрики; обёртку и константу `MANIFEST` дописывает
`build.mjs`. Форма обёртки — как у продовых бандлов ростера
(`window.__ModuleLoader__.load({ id: "<package>", factory })`, у фабрики
экспортируются `inject` и `apply`). Модули берутся из seed-карты шелла
(`react`, `@deepseek-ai/dsh-client-ui-primitives` — список зашит в
`build.mjs` и проверяется при сборке). `ctx.locale` приносит
`@deepseek-ai/dsh-client-locale` — единственная пакет-инъекция в
`dsh.client.inject`; её assemble-standalone-web.mjs проверяет по ростеру
и строит порядок загрузки. `ctx.slots` приносит
`@deepseek-ai/dsh-client-ui-renderer`, он всегда в ростере шелла и НЕ
декларируется (апстрим убрал `@deepseek-ai/dsh-client-runtime` в 0.10.0 —
инъекция несуществующего пакета красит гвардию
`check-upstream-namespace-collision.mjs` и деплой, класс #518).

Слоты:
- `sidebar.section` — основной слот для задачи #111 (order 10, вверху сайдбара)
- `settings.section` — второй вход (order 90, как plugin-manager)

Словари регистрируются в namespace `agents.tasks` (en/zh/ru, наборы ключей одинаковы).

## Публикация: релиз + запись в dsh-edge/plugins.json

`build.mjs` (и, транзитивно, `test/client.test.mjs`, который сам его
запускает) требует, чтобы пакет уже был объявлен записью в
`dsh-edge/plugins.json`, — релиз нельзя собрать из среза каталога, где
пакета нет. Плагин зарегистрирован записью, указывающей на релиз
[`plugins-agents-tasks-v0.1.2`](https://github.com/mytab0r/edge-harness/releases/tag/plugins-agents-tasks-v0.1.2)
(ассет `edge-harness-dsh-agents-tasks-0.1.2.tgz`, sha256 в записи).

Конвейер обновления версии (тот же, что у соседних плагинов, #80):

1. `node plugins-src/agents-tasks/build.mjs` — сборка + гвардии (манифест
   через `dsh-edge/manifest.mjs`, seed-require, обёртка);
2. поднять `version` в `package.json`, `npm pack` (из каталога плагина);
3. `gh release create plugins-agents-tasks-vX.Y.Z <tgz>` — релиз этого
   репозитория (плагины ставятся только tarball'ами из своих релизов);
4. записать реальный sha256 ассета в `dsh-edge/plugins.json` (тот же PR,
   что источник изменений, — правка `plugins-src/**` без апдейта записи
   не меняет то, что уедет в прод);
5. деплой сверяет sha256 ассета и ростер `manifest.json` из пакета
   (`deploy-dsh-edge.yml`, шаг «Скачать плагины и сверить sha256»).

Известный пробел конвейера: **plugin-forge не может сделать ПЕРВУЮ
регистрацию плагина** — форж собирает бандл до своего PR с манифестом, а
гвардия каталога в `build.mjs` без записи падает (`FORGE_EXTRA_PLUGIN`
подставляется только в дым, не в сборку). Релиз v0.1.1 собран с временной
локальной записью, как описано выше; бутстрап форжа — задача #914.
(Релиз v0.1.1 протух: собран до снятия inject-декларации
`@deepseek-ai/dsh-client-runtime`, на деплое упал бы на assemble, #518 —
живет только в истории, запись манифеста на него не смотрит.)

Сгенерированное (`client/`, `manifest.json`) в git не хранится: манифест
меняется — пересборка перед каждым `npm pack` обязательна, иначе в бандл
уйдёт устаревший состав. Гард-рейл сборки: свежий `parseManifest` и
проверка seed-require'ов на каждом запуске `build.mjs`.

## Тесты

```bash
node --test plugins-src/agents-tasks/test/client.test.mjs
```

Шаг подключён в `.github/workflows/repo-ci.yml` рядом с plugin-manager —
критерий #407 «клиентские тесты зелёные в repo-ci» выполняется этим шагом.

Поведенческая гвардия (`test/client.test.mjs`, `node --test`, без
зависимостей): обёртка бандла, монтаж в ОБОИХ слотах (потеря
`sidebar.section` красит набор — найдено ревью, доказано мутацией),
паритет ключей словарей и ячейки статусов на прод-форме ответа журнала —
`{events: [{id, kind, data}], has_more, next_after}` с каноническим
`task_id=issue-<N>`; системные события определяют статус задачи;
`session_event` парсится как чат (think/tool/assistant); пагинация добирает
свежие события; 403 от GitHub ставит паузу автопроллинга (тики не долбят
лимитированный API), успешный тик перечитывает список и журнал; отказ и
чужой ответ = громкая ошибка. Мутационная проверка: подмена «последнего
события» на «первое» красит набор.
