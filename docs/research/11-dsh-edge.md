# dsh-edge: как DSH уже портировали на Cloudflare

> Исследовано 2026-08-28. Смежное: [архитектура DSH](10-dsh-architecture.md), [Cloudflare Free](20-cloudflare-free.md)

## TL;DR

- `github.com/pawaca/dsh-edge` (MIT, TypeScript) — не форк Harness, а **обвязка**: pnpm-монорепо, которое собирает upstream-пакеты `@deepseek-ai/dsh-*` в один Cloudflare Worker. Собственного кода ~304 KB в 27 файлах; всё остальное переиспользовано.
- Самый дорогой кусок портирования — sandbox и файловая система — **не писался руками**: отдан продукту Cloudflare `@cloudflare/computer` 0.2.0. Свой код появился ровно там, где Computer не покрывает: persistence, storage backend, auth, API-каретка.
- Дистрибуция решена так, что у пользователя нет ни сборки, ни резолва upstream: в npm уезжает **предсобранный воркер**, установщик заливает его через wrangler с `no_bundle: true`. Поэтому все `dsh-*` лежат в `devDependencies`.
- Free и Paid — **два режима из одного графа приложения**, единственная точка ветвления `this.env.LOADER === undefined` (`apps/dsh-edge/src/instance.ts:161`). Без форка протокола, стораджа и UI.
- Шелл — `just-bash` внутри Durable Object. Это **не Linux**: нет нативных бинарей, фоновых процессов, PTY, сети из команд. Не портированы filesystem editor tools, MCP, skills, workflows, jobs и **subagents**.
- Отсюда наша задача: как чат-агент с правкой файлов dsh-edge работает, как харнес самразработки с реальными прогонами (git, pytest) — нет. Нужен **третий бэкенд** рядом с `DirectShellBackend`/`WorkerShellBackend`.

## Что это за репозиторий

| Факт | Значение |
|---|---|
| Владелец / имя | `pawaca/dsh-edge` |
| Лицензия | MIT |
| Язык | TypeScript |
| Форк? | Нет — `"fork": false`, `parent` отсутствует |
| Создан / последний push | 2026-08-20 / 2026-08-28 |
| Звёзды | 11 |
| Коммитов / контрибьюторов | 152 / ровно один (`pawaca`) |
| Файлов в git-дереве | 292 |
| Воркспейсы | `apps/dsh-edge`, `packages/client/ui-edge` |
| npm-пакет | `dsh-edge` 0.6.0-alpha.3, `bin: scripts/cli.mjs` |

Корневой `package.json` описывает себя как «development workspace for the independent dsh-edge Cloudflare wrapper» — заявка на обвязку, а не на форк, сделана явно. Пин на upstream объявлен одним полем: `dshEdge.upstreamVersion = "0.1.1-rc.2"` в `apps/dsh-edge/package.json`.

## Дистрибуция: предсобранный воркер, а не сборка у пользователя

Это центральное архитектурное решение репозитория, и его видно прямо в манифесте.

Все 35 пакетов `@deepseek-ai/dsh-*` (все — `0.1.1-rc.2`) плюс `@deepseek-ai/cordis` 4.0.1, `@cloudflare/computer` 0.2.0 и `just-bash` 3.3.0 лежат в **`devDependencies`** публикуемого пакета. Runtime-зависимости CLI — только пять штук:

```
@clack/prompts 1.7.0, compare-versions 6.1.1, execa ^10.0.0,
jsonc-parser ^3.3.1, wrangler 4.123.0
```

Механика: мейнтейнер линкует Harness в бандл на этапе сборки (`pnpm bundle:workers` → `standalone/scripts/bundle-standalone.mjs`), в npm уезжают готовые артефакты `worker/direct/index.js` и `worker/isolated/index.js`, а `npx dsh-edge install` заливает артефакт через wrangler с `no_bundle: true` (`apps/dsh-edge/scripts/wrangler-config-core.mjs:67`). README формулирует это дословно (строка 211):

> "The installer generates a private mode-specific config, points it at the selected artifact, and uploads with `no_bundle`. The user's machine does not rebuild dsh-edge or resolve Harness packages into a new Worker."

Побочный эффект, который стоит осознать: вопрос лицензий на пересборку upstream снимается — пользователь ничего не пересобирает. И вопрос доступности `@deepseek-ai/*` в публичном npm для конечного пользователя тоже снимается: резолвит их только мейнтейнер.

Собственного кода — 27 файлов, ~304 KB TS в `apps/dsh-edge/src`. Крупнейшие: `session-store.ts` (62 KB), `do-session-persistence.ts` (44 KB), `edge-api.ts` (40 KB), `instance.ts` (39 KB), `edge-attachment-store.ts` (24 KB), `auth.ts` (13 KB), `direct-shell.ts` (11 KB).

## Патчи upstream — готовый список несовместимостей Harness с Workers

Пять пакетов пропатчены через `pnpm patch`, патчи лежат в `apps/dsh-edge/standalone/patches/`. Рядом — `patches/audit.json`, который называет причину каждого патча и **условие его снятия**. Это самый ценный один файл во всём репозитории для нас: он перечисляет, обо что именно Harness спотыкается в Workers.

| Пакет | Что чинит |
|---|---|
| `dsh-llm` | версия пакета читалась через `node:module` path discovery — недоступно в бандле воркера |
| `dsh-llm-deepseek` | пробросить слот `resolveFiles` в конструктор адаптера (DO-backed file store для Files API) |
| `dsh-session-persistence` | bounded validated reads + отмена нематериализованной сессии при провале первой записи |
| `dsh-web-search-deepseek` | redirect mode, который Workers переживают (не следовать редиректам поддерживаемым способом) |
| `dsh-workspace` | убрать `node:fs/promises` realpath/stat (в Edge VFS нет симлинков, валидность пути гарантирует Computer) |

**Прочитайте `audit.json` целиком** — у каждой записи есть поле `removeWhen`, то есть точное условие, при котором патч перестанет быть нужен. Это карта того, что upstream должен изменить у себя, и одновременно карта того, что придётся патчить нам.

## Что заменено: разделительная линия проходит не там, где ждёшь

ФС и sandbox **не написаны с нуля** — отданы продукту Cloudflare `@cloudflare/computer` 0.2.0.

**Shell** — `src/direct-shell.ts`: `DirectShellBackend implements WorkspaceBackend` (интерфейс из `@cloudflare/computer`), внутри гоняет `just-bash` 3.3.0 через импорт `just-bash/browser` — прямо в Durable Object. Конфигурация исполнения: `executionLimitProfile: 'hardened'`, `defenseInDepth: { enabled: 'auto' }`, `maxOutputSize` = 65536 (`direct-shell.ts:161-164`).

**Переключатель режимов** — `src/instance.ts:161`, единственная точка ветвления во всём приложении:

```ts
const backend = this.env.LOADER === undefined
  ? new DirectShellBackend()
  : new WorkerShellBackend({
      loader: this.env.LOADER,
      workspace: { binding: 'DSH_EDGE_INSTANCE', id: this.ctx.id.toString() },
      ctx: this.ctx,
    })
```

**ФС** — своей нет. `withWorkspace(DshEdgeObjectBase, self => self.workspaceOptions())` (`instance.ts:180`) отдаёт `ctx.storage` в Computer, тот держит `/workspace` VFS поверх DO SQLite.

**Session persistence** — `src/do-session-persistence.ts` + `src/session-store.ts`: реализация upstream-контракта поверх DO SQL (таблица `dsh_session_events`). Сам `SessionPersistence` и `PersistenceCoordinator` переиспользуются как есть; Edge реализует только storage primitives. Одна Edge-специфичная таблица держит пустые заголовки сессий через прозрачную гибернацию и удаляется, когда материализуются канонические строки.

**KV-стор** — `src/do-storage-backend.ts`: `DurableObjectStorageBackend implements StorageBackend` (интерфейс из `@deepseek-ai/dsh-storage`), ключи вида `dsh-kv:{unit}:{table}:{key}` в `ctx.storage`.

**Settings / credentials** — `src/do-settings-provider.ts`, `src/edge-credentials.ts`: `EdgeCredentialProvider` резолвит сначала из DO KV, потом фолбэком из env-секретов воркера. Страница Settings → Models умеет менять base URL, каталог моделей, ссылку на API-ключ и reasoning effort **без передеплоя**.

**Сервер вместо Node** — `src/index.ts` (Worker `fetch`, 6.8 KB) + `src/instance.ts` (Durable Object `DshEdgeInstance`, 39 KB) + `src/edge-api.ts` (40 KB), поверх upstream-каретки `toFetchHandler` из `@deepseek-ai/dsh-host-apiproxy`.

**Bash-инструмент** — `src/agent.ts`: `createEdgeBashTool` регистрируется через upstream `defineTool`, то есть остаётся обычным upstream `ToolDefinition`, у которого подменено тело. Отмена инструмента шлёт `SIGINT` через execution handle Computer.

**Аттачменты** — `src/edge-attachment-store.ts` (24 KB): private R2 для новых постоянных деплоев либо DO-бэкенд (64 MiB, чанки по 512 KiB) для временных. Бэкенд пинится на инстанс владельца, чтобы апгрейд не осиротил существующие ссылки.

## UI: не переписан и не форкнут

Фронтенд собирается из **опубликованных upstream npm-пакетов** на этапе сборки. `apps/dsh-edge/standalone/scripts/assemble-standalone-web.mjs` резолвит `@deepseek-ai/dsh-web-app` и `@deepseek-ai/dsh-web-frontend`, копирует их `dist`, строит ростер клиентских плагинов из деклараций `dsh.client` в манифестах, инжектит `window.__DSH_BOOT__` и патчит `index.html` (`injectOwnerSessionGuard`). Результат отдаётся как Workers Assets:

```jsonc
"assets": {
  "binding": "ASSETS",
  "directory": "./dist",
  "not_found_handling": "single-page-application",
  "run_worker_first": ["/api/*", "/", "/login"]
}
```

Собственного UI-кода ровно один пакет — `packages/client/ui-edge` (~10 файлов React, приватный, `"name": "dsh-edge-client-ui"`): один плагин в слот `settings.section` со статусом деплоя, версией, sign-out и командой апгрейда. Его декларация `dsh.client.inject` перечисляет три upstream-пакета (`dsh-client-runtime`, `dsh-client-ui-settings`, `dsh-client-locale`), `platform: "web"`.

Клиентские плагины, чьи host-домены в Edge недоступны, **исключаются, а не форкаются** — работают общие правила занятости слотов, и действие без провайдера просто не показывается.

## Протокол UI↔бэк: гибрид из трёх механизмов

Отличается от upstream — это важно, если планируете свой клиент.

1. **HTTP RPC — основной путь.** `POST /api/<upstream-method>` с upstream-конвертом `ClientRequest`. Конверты, схемы, проекции, ленивое поведение пустой сессии, ограниченный поиск по контенту, мутации промпта/очереди/воркспейса — всё сохранено.
2. **Два downlink-only WebSocket.** `GET /api/events.mux` и `GET /api/events.host`. Держатся на DO-гибернации: `ctx.acceptWebSocket(server, [channel])` + `serializeAttachment({ channel, expiresAt })`. Клиент писать не может — `webSocketMessage` закрывает сокет кодом `1008 'downlink only'` (`instance.ts:333`). Протухание owner-сессии закрывает сокет через `alarm()`.
3. **SSE — диагностический путь.** `src/sse.ts`, кадр `id: {event.seq}\nevent: {type}\ndata: {json}`.

Каждый аутентифицированный запрос идёт в один фиксированный Durable Object с именем `owner`. Легаси-заголовок `x-dsh-edge-instance` и query-параметр `instance` **отвергаются**, а не трактуются как идентичность.

## Replay и границы истории

`GET /api/sessions/:id/events?after=&limit=` (обработчик в `instance.ts`, около строки 648). Константы объявлены в `instance.ts:96-98`:

```ts
const DEFAULT_REPLAY_EVENT_LIMIT = 128
const MAX_REPLAY_EVENT_LIMIT = 256
const MAX_REPLAY_RESPONSE_BYTES = 1_048_576
```

Ответ несёт заголовки продолжения `x-dsh-edge-has-more` и `x-dsh-edge-next-after` (`instance.ts:683-684`).

Отдельно, чтобы не спутать: **8192 — это не replay-окно**, а потолок страницы истории и форка. `EDGE_HISTORY_PAGE_LIMITS` в `do-session-persistence.ts:108` = `{ maxEvents: 8_192, maxStoredBytes: 8 MiB, maxMessages: 50 }`, плюс `MAX_FORK_EVENTS = 8_192` в `session-store.ts:88`. При превышении Edge **ОТКАЗЫВАЕТ, а не усекает** (README:101 — "refuses, rather than truncates").

События живут канонически в DO SQLite; второй, Edge-специфичной схемы событий нет — модельная история проецируется из канонических событий. Событие обязано пересечь `SessionStore.flush()` до того, как уйдёт в WS/SSE. Живой стрим держит очередь не больше 1 MiB на клиента: медленный читатель отключается, но это не отменяет ни ход, ни его персистенцию.

## Что НЕ работает

Матрица совместимости — в `apps/dsh-edge/README.md`, раздел «Cloudflare compatibility matrix», строки 107-127. Отдельного `docs/compatibility.md` в репозитории **нет**.

- **Нативные бинари, фоновые процессы, PTY, произвольное Linux-поведение — недоступны** (README:117). Это же заявлено модели в системном промпте: `agent.ts:8-12` — "The shell is just-bash, not Linux".
- **Сеть из команд** — "no network command" в direct-режиме (README:201).
- **`web_fetch` отключён** — "no arbitrary-URL network policy" (README:184). Портирован только Web Search с 30-секундным таймаутом tool-call.
- **Не портированы**: filesystem editor tools, MCP, skills, workflows, jobs, **subagents** (README:125).
- Нет HMR и локального boot-профиля (README:121); нет session-log export и локальных host-плагинов (README:26).
- Нет регистрации, базы пользователей, ролей, мультитенантного роутинга (README:127) — жёстко один DO с именем `owner`, вход по одному высокоэнтропийному секрету воркера, обмениваемому на подписанную 30-дневную HttpOnly `SameSite=Strict` куку.
- **Удаление сессии не выставлено** (README:295) — потому что upstream-сервис персистенции не определяет деструктивное удаление.
- В direct-бэкенде **sync отключён полностью** — методы кидают `ENOSYS` (`direct-shell.ts:324`, `:336`); `getExec` всегда `ENOENT` (`direct-shell.ts:114-115`), то есть **переподключиться к запущенной команде нельзя**.

Лимиты (README, раздел «Limits and request admission», строки ~305-317):

| Поверхность | Лимит |
|---|---|
| UTF-8 файл | 1 MiB |
| Команда шелла | 16 KiB |
| Сообщение пользователя / правка очереди | 64 KiB |
| Удерживаемые stdout + stderr | 64 KiB |
| JSON-тело создания сессии | 8 KiB |
| JSON-тело workspace-exec | 128 KiB |
| RPC с сообщением (turn / queue-update) | 10 MiB |
| Изображения | PNG/JPEG; 4 на сообщение; 3.5 MiB каждое; 7 MiB всего; 40 Мпикс; 2000 px на сторону |

Тела запросов потребляются инкрементально: как только лимит маршрута пересечён, дальнейшие чанки сливаются без удержания, маршрут отдаёт 413.

## wrangler-конфигурация и биндинги

`apps/dsh-edge/wrangler.jsonc` — весь файл около килобайта:

- `main: src/index.ts`, `compatibility_date: "2026-08-14"`, `compatibility_flags: ["nodejs_compat"]`, `minify: true`, `observability.enabled: true`;
- `assets` → binding `ASSETS` (см. выше);
- `durable_objects` → `DSH_EDGE_INSTANCE` → класс `DshEdgeInstance`;
- `migrations: [{ tag: "v1", new_sqlite_classes: ["DshEdgeInstance"] }]` — **SQLite-backed DO**;
- `env.isolated` → тот же DO + `worker_loaders: [{ binding: "LOADER" }]`.

Установщик дописывает конфиг динамически (`scripts/wrangler-config-core.mjs`): `r2_buckets: [{ binding: "DSH_EDGE_ATTACHMENTS", bucket_name: "<worker>-attachments" }]`, переменная `DSH_EDGE_ATTACHMENT_STORAGE` = `private-r2` при наличии бакета либо `temporary-do` при его отсутствии, опционально `images: { binding: "IMAGES" }`, и `no_bundle = true`.

**D1 и KV не используются вообще** — всё KV-подобное идёт через `ctx.storage` Durable Object.

Workers Paid нужен **только** для режима `isolated` (Worker Loaders). Direct-режим работает на Workers Free (README:199-204). CI режет gzip воркера выше 900 KiB, чтобы сохранить запас под лимитом 1 MiB анонимного временного аккаунта (README:212).

## Живость и качество

- **Issues**: две открытые, обе от владельца — #42 «Adopt upstream WorkspaceRegistry by implementing DurableObjectStorageBackend», #43 «Spike: Evaluate upstream AgentPresets + cordis-plugin-loader on Cloudflare Workers». Обе — про то, чтобы отдать ещё больше работы наверх, в upstream.
- **PR**: один открытый на момент перепроверки (#63, косметика UI). Номера дошли до 63 при 43 issue — значит через PR-поток прошло несколько десятков изменений.
- **Релизы**: 19 версий релиз-нот в `docs/releases/` (0.2.0-alpha.1 → 0.6.0-alpha.3) примерно за 8 дней.
- **Тесты серьёзнее исходников**: 26 `.spec.ts` плюс интеграционные харнессы, суммарно ~447 KB тестов против ~304 KB исходников. Snapshot-голдены (`tests/snapshots/`, включая ARIA и модель-видимые транскрипты), реальный браузерный прогон (playwright), интеграция на настоящем wrangler + DO SQLite + локальном R2 (`apps/dsh-edge/tests/session.integration.mjs`, 66 KB), фикстуры состояния версии 0.1.3 для проверки миграций.
- **CI** — `.github/workflows/edge-ci.yml`, джоб `edge / linux` (ubuntu-latest): установка пиновых зависимостей сборки → сборка standalone-артефактов → verify контракта → promote → doc-sync → lint (`oxlint --type-aware`) → тесты → интеграция обоих режимов на артефактах и на durable-состоянии 0.1.3 → typecheck → snapshot → pack установщика → **верификация установленного пакета вне воркспейса**. Плюс джоб `edge / windows installer` (windows-2025). Рядом — `release-edge.yml` и `request-release.yml`.
- **Репозиторий ведётся агентами**: корневые `AGENTS.md` и `CLAUDE.md`, каталог `.agents/` с архитектурными нотами по датам (`2026-08-14-cloudflare-computer-runtime-poc.md`, `2026-08-16-single-owner-edge-auth.md`, `2026-08-17-free-direct-edge-shell.md`, `2026-08-18-edge-client-plugin.md`) и скиллами `codex-review-loop`, `dsh-code-review`, `dsh-pre-push-checks`.
- **Документация продублирована en/zh** (включая релиз-ноты и архитектурные ноты) с машинной гвардией `scripts/verify-doc-pairs.mjs`, поднятой в `doc-sync`-шаге CI.

Резюме по качеству: это не демка. Тестов больше, чем кода; интеграция гоняется на настоящем wrangler; установленный пакет проверяется вне воркспейса. Читать этот репозиторий как источник решений можно с доверием.

## Что забрать

Три вывода, ради которых стоило смотреть.

**1. `@cloudflare/computer` закрывает и sandbox, и файловую систему.** Самый дорогой кусок портирования не писался руками вообще: и исполнение команд, и `/workspace` VFS поверх DO SQLite пришли готовым продуктом Cloudflare. Свой код появился ровно там, где Computer не покрывает — persistence поверх DO SQL, storage backend, auth, API-каретка. Прежде чем писать свой sandbox, надо упереться в границы Computer и уметь их назвать.

**2. Публикация предсобранного воркера с `no_bundle: true`.** У пользователя нет ни сборки, ни резолва upstream-пакетов — поэтому `dsh-*` и лежат в `devDependencies`, а runtime-зависимостей у CLI всего пять. Это же снимает вопрос лицензий на пересборку и делает установку одной командой без тулчейна на машине пользователя.

**3. `LOADER === undefined` как единственная точка ветвления Free/Paid.** Два режима исполнения из одного графа приложения, без форка протокола, стораджа, UI и без второй сборочной ветки в коде — различие живёт в манифесте (`env.isolated` добавляет `worker_loaders`), а в коде это одна тернарная операция. Ветвление, которое хочется размазать по десяти местам, здесь сведено в одно.

## Почему для нас этого недостаточно

`just-bash` — не Linux. Нет `git`, нет `pytest`, нет нативных бинарей и фоновых процессов, нельзя переподключиться к запущенной команде (`getExec` → `ENOENT`). Не портированы subagents, workflows и jobs — то есть ровно те механизмы, на которых держится многошаговая автономная работа.

Практический вывод: **как чат-агент с правкой файлов dsh-edge работает; как харнес самразработки с реальными прогонами тестов — нет.** Отсюда и вырастает наша задача — третий бэкенд рядом с `DirectShellBackend` и `WorkerShellBackend`. Хорошая новость в том, что место для него уже подготовлено чужими руками: `WorkspaceBackend` — интерфейс `@cloudflare/computer`, точка выбора одна, и всё остальное приложение о выборе бэкенда не знает.

## Что не подтверждено

- **Доступность самих `@deepseek-ai/dsh-*` в публичном npm и их лицензия.** Вывод косвенный — из того, что `npx dsh-edge install` работает у сторонних пользователей. Но именно эта установка ничего не доказывает: она ставит предсобранный воркер и upstream-пакеты не резолвит вовсе (см. раздел про дистрибуцию). Прямой проверки `npm view @deepseek-ai/dsh-agent` не делалось.
- Точное содержимое пяти `.patch`-файлов не читалось — читался только `audit.json` с причинами и условиями снятия.
- Заявления README о поведении (лимиты, отказ вместо усечения, 30-дневная кука) сверялись с текстом README и с константами в коде, но не прогоном живого деплоя.

### Расхождения, найденные при перепроверке 2026-08-28

Репозиторий активно двигается (последний push — день перепроверки), поэтому часть чисел из более ранней выжимки устарела. Исправлено в тексте выше:

| Утверждалось | Фактически (проверено) |
|---|---|
| 36 пакетов `@deepseek-ai/dsh-*` в devDependencies | **35** в `apps/dsh-edge/package.json`; в приватной сборочной обвязке `standalone/package.json` — 42 (там они обычные `dependencies`, но пакет не публикуется) |
| все `dsh-*` на `0.1.1-rc.2` | верно для `apps/dsh-edge` и `standalone`; **но** клиентские пакеты в `packages/client/ui-edge` пиньены на `0.1.1-rc.1` |
| открытых PR нет | открыт **#63** (косметика UI). Issues #42/#43 подтверждены |
| 22 `.spec.ts`, ~418 KB тестов | **26** `.spec.ts`, **~447 KB** тестов |
| 20 релиз-нот | **19** различных версий в `docs/releases/` |
| CI: `edge-ci.yml` | плюс `release-edge.yml` и `request-release.yml` |

Точные номера строк перепроверены и в тексте приведены по факту: `instance.ts:96-98` (replay-константы), `instance.ts:161` (ветвление LOADER), `instance.ts:180` (`withWorkspace`), `instance.ts:333` (`1008 'downlink only'`), `instance.ts:683-684` (заголовки продолжения), `direct-shell.ts:114-115` (`getExec` → ENOENT), `direct-shell.ts:324/336` (sync → ENOSYS), `do-session-persistence.ts:108`, `session-store.ts:88`.

## Источники

- Репозиторий: `https://github.com/pawaca/dsh-edge` (метаданные — GitHub API `repos/pawaca/dsh-edge`, дерево — `git/trees/main?recursive=1`)
- `apps/dsh-edge/README.md` — 345 строк; матрица совместимости 107-127, лимиты ~305-317, режимы 199-212
- `apps/dsh-edge/package.json`, `apps/dsh-edge/standalone/package.json`, `packages/client/ui-edge/package.json`, корневой `package.json`
- `apps/dsh-edge/wrangler.jsonc`
- `apps/dsh-edge/standalone/patches/audit.json` (+ пять `.patch`-файлов рядом)
- `apps/dsh-edge/src/`: `instance.ts`, `direct-shell.ts`, `do-session-persistence.ts`, `session-store.ts`, `agent.ts`, `sse.ts`, `do-storage-backend.ts`, `edge-credentials.ts`, `do-settings-provider.ts`, `edge-attachment-store.ts`, `index.ts`, `edge-api.ts`
- `apps/dsh-edge/scripts/wrangler-config-core.mjs`, `apps/dsh-edge/standalone/scripts/assemble-standalone-web.mjs`
- `.github/workflows/edge-ci.yml`
- Issues #42, #43; PR #63
