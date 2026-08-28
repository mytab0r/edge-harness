# DeepSeek Harness: архитектура и точки расширения

> Исследовано 2026-08-28. Источники — код и официальные доки, ссылки по тексту.
> Смежное: [dsh-edge](11-dsh-edge.md), [Cloudflare Free](20-cloudflare-free.md), [отвергнутые варианты](30-rejected-alternatives.md)

## TL;DR

- DSH построен на [Cordis](https://github.com/cordiverse/cordis) под слоганом «Everything is a Plugin»: 247 пакетов в 51 группе, всё сменяемое через YAML-патчи над деревом плагинов — **форк не нужен ни для одной подмены, о которых мы думаем**.
- Ключевая абстракция — **шов (seam)**: определение сервиса + провайдер + потребитель. Нужные нам швы: `ctx.subprocess` + `ctx.fs` (удалённое исполнение, ставятся ТОЛЬКО парой), `ctx.sessionPersistence` и `ctx.storage` (удалённое состояние), `ctx.llm` (провайдеры моделей), `ctx.shell`.
- Официальный образец удалённого исполнения уже существует — `packages/e2b/*` (`dsh-subprocess-e2b` + `dsh-fs-e2b`), но README прямо помечает его: «It is an experimental POC, and no shipped composition enables it by default». Это шаблон для нашего провайдера, а не готовый продукт.
- Ядро почти не зависит от Node: из 45 `.ts`-файлов `packages/core/*/src/` только **три** трогают `node:*` (`crypto`, `async_hooks` + `util/types`, `path`) — все доступны в Workers с `nodejs_compat`. Node жёстко нужен ПРОВАЙДЕРАМ (`subprocess-local`, `sandbox-local`, `fs-local`, `terminal-bash`, persistence, storage), не ядру. Репозиторий сам это доказывает пакетом `packages/experimental/webworker-runtime`.
- UI — полноценный сетевой клиент, не вшитый в процесс: унарка по HTTP `POST /api/<namespace>/<method>`, стримы и события по одному WebSocket-мультиплексору `/api/remote.mux`. Токены ассистента идут по WebSocket, **не по SSE** — прокси обязан уметь full-duplex апгрейд.
- Наш вход — профиль `sdk` (JSON-RPC поверх stdio) и/или подмена швов через `cordis.patch.yml`. Ниша удалённого исполнения в экосистеме перекошена в SSH; удалённое хранение сессий (Redis/D1/KV/HTTP-sync) — **пусто, конкурентов нет**.

---

## 1. Что это за проект

| Поле | Значение |
| --- | --- |
| Репозиторий | [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) |
| Ветка по умолчанию | `master` |
| Язык / сборка | TypeScript, pnpm-монорепо |
| Лицензия | MIT |
| Звёзды | ~201 400 (201 387 на 2026-08-28) |
| Создан | 2026-08-13 |
| Активность | живой, последний push 2026-08-27 |
| npm | `@deepseek-ai/dsh` |
| Доки | [deepseek-harness.github.io/deepseek-harness/](https://deepseek-harness.github.io/deepseek-harness/) |

Ядро композиции — [Cordis](https://github.com/cordiverse/cordis) (проект `cordiverse`), IoC-контейнер с контекстами, сервисами и событиями. Статус проекта — **developer preview**; авторы явно предупреждают о ломающих изменениях. Планируя интеграцию, закладывайся на pin-версию и регулярный передиф.

Проверено: `gh api repos/deepseek-ai/deepseek-harness` (2026-08-28).

## 2. Карта монорепо

`pnpm-workspace.yaml` глобит НЕ только `packages/*/*`. Полный список:

```yaml
packages:
  - vendor/*
  - packages/*/*
  - native/landlock-run
  - native/landlock-run/packages/*
  - apps/*
  - website
  - python/sdk-runtime
```

Форма `packages/<группа>/<пакет>` — двухуровневая, поэтому «пакет `fs`» и «группа `fs`» это разные вещи, и путать их дорого (см. ловушку про `fs-e2b` ниже).

**Приложения:**
- `apps/cli` — `@deepseek-ai/dsh`, **единственная точка входа** для всех профилей;
- `apps/web` — `@deepseek-ai/dsh-web-frontend`, Vite/React 18 SPA;
- `website` — VitePress-сайт документации.

**Счёт (проверено обходом git tree на `master`):** 247 пакетов в 51 группе.

- `packages/client/*` — 43 пакета: транспорт и оболочка (`web`, `connection`, `store`, `hmr`, `locale`, `modules`) плюс **37** фич-пакетов `ui-*`: `ui-primitives`, `ui-chat`, `ui-session`, `ui-approval`, `ui-plan`, `ui-subagent`, `ui-trajectory`, `ui-workflow-run`, `ui-settings-*` и т.д. UI нарезан по фичам — вырезать/заменить кусок интерфейса не значит трогать ядро.
- `packages/core/*` — ровно 8: `scope`, `session`, `system-prompt`, `tools`, `agent`, `agent-loop`, `agent-default-model`, `agent-tool-presentation`.

Ключи контекста, которые составляют «ядро агента»:

| Ключ | Что это |
| --- | --- |
| `ctx.sessions` | append-only лог `SessionEvent` |
| `ctx.systemPrompt` | сборка системного промпта |
| `ctx.tools` | реестр инструментов |
| `ctx.agents` | интерфейс `Agent` (создание, resume, инициаторы) |
| `ctx.agentLoop` | дефолтный драйвер цикла |

## 3. Шов (seam) — главная абстракция

`docs/architecture.md:111`, дословно:

> A **seam** is a swappable capability with three roles: a **Service Definition** declaring the interface, a **Service Provider** implementing it, and a **Consumer** using it, commonly a model-facing tool.

Реестр швов — машинно-генерируемый `docs/capability-seams.md`; роли размечены как `seam` / `core` / `bundle`. Практический смысл: если возможность объявлена швом, её МОЖНО заменить строчкой конфига, не форкая потребителей.

### 3.1 Швы, которые нам важны

**1. `ctx.shell` — `packages/shell/shell`**

`abstract class ShellExecutor extends Service`, методы `resolve()`, `run()`, `start()`. Из `docs/capability-seams.md`:

> sandboxed, **remote**, or PowerShell executors replace bash-local without touching them

Реализации в дереве: `bash-local`, `bash-sandbox`, `pwsh-local`, `pwsh-sandbox` (npm-имена `@deepseek-ai/dsh-bash-local` и т.д.). Ограничение: **один executor на контекст** — второй бросает.

**2. `ctx.subprocess` — `packages/subprocess/subprocess`**

`abstract class SubprocessRuntime extends Service`: `resolveExecutable()`, `spawn()`, `spawnTerminal()`. Из `docs/architecture.md:113`:

> Filesystem and subprocess providers share one execution world, so pointing them at a remote sandbox moves Bash, PTY, and LSP with them, with no provider forks.

**Это правильная точка врезки для удалённого исполнения.** Не `ctx.shell` — врезавшись ниже, в subprocess, ты бесплатно уносишь Bash, PTY и LSP разом. Но **обязательно парой с `ctx.fs`**: подменить один и оставить другой = агент читает файлы на одной машине, а запускает процессы на другой. Это класс ошибки, а не мелочь.

**3. `ctx.fs` — `packages/fs/*`**

Шов `fs` + провайдеры `fs-local`, `fs-sandbox` (плюс `fs-observation-policy` и инструменты `tool-fs`, `tool-fs-search`, `tool-str-replace-editor` в той же группе).

> **Поправка к раннему тезису.** `fs-e2b` живёт НЕ в `packages/fs/`, а в `packages/e2b/fs-e2b` — вместе с остальной семьёй E2B. Искать провайдеры удалённого fs в группе `fs` бесполезно; провайдеры группируются по СРЕДЕ, а не по шву. Проверено обходом git tree.

**4. `ctx.sessionPersistence` — `packages/session/session-persistence`**

`abstract class SessionPersistence`, под ним интерфейс `PersistenceBackend<TornMarker>` и переиспользуемый `PersistenceCoordinator` — то есть новый бэкенд не переписывает координацию, а только I/O. Реализации в дереве **только локальные**: `session-persistence-jsonl` (дефолт) и `session-persistence-sqlite`.

**5. `ctx.storage` — `packages/storage/*`**

Интерфейсы `StorageBackend` / `KvFacet` / `KvUnit`, узкий контракт: `open` / `loadAll` / `putRecord` / `deleteRecord` / `setGlobal` / `close`. Есть conformance-набор `tests/contract.ts` — свой бэкенд можно доказать чужими тестами, не выдумывая свои. Реализации: `storage-json`, `storage-sqlite` (+ `storage-domain`).

**6. `ctx.llm` — `packages/llm/llm`**

В отличие от `shell`, это **реестр, а не одиночка**: `ctx.llm.registerAdapter(providers: string[], adapter: LlmAdapter)`, адаптер — подкласс `abstract class LlmAdapter` с `abstract stream()`. Реализации: `llm-deepseek`, `llm-pi-ai`, `llm-retry`.

Разница «одиночка vs реестр» — прикладная: shell-провайдер ты ЗАМЕНЯЕШЬ, llm-адаптер ты ДОБАВЛЯЕШЬ.

## 4. Официальный образец удалённого исполнения: `packages/e2b/*`

Три пакета: `dsh-e2b` (сервис `ctx.e2b`), `dsh-subprocess-e2b`, `dsh-fs-e2b`. Форма провайдера — ровно та, что нам нужна:

```ts
class E2BSubprocessRuntime extends SubprocessRuntime {
  static inject = ['e2b']
  // ...
}
```

Из README `packages/e2b/subprocess-e2b`: агент выполняет Bash, открывает интерактивные терминалы и читает их вывод

> exactly as with local execution

при том, что на хосте не выполняется ничего.

**Три вещи, которые надо знать до того, как копировать:**

1. **Это POC.** README группы, дословно: «It is an experimental POC, and no shipped composition enables it by default.» Ни одна поставляемая композиция его не включает. Читать как референс-реализацию, не как продакшн.
2. **Объём работы измерим.** `subprocess-local` — 1966 строк src, `subprocess-e2b` — 1835, `fs-e2b` — 612. То есть удалённый subprocess-провайдер стоит примерно столько же, сколько локальный: интерфейс широкий, дешёвой обёртки не выйдет.
3. **Ловушка синхронного pid.** Из README: «Tooling that needs a process id immediately — for example the ACP child backend — cannot use this package.» Удалённый spawn не может отдать pid синхронно. Любой потребитель, которому pid нужен сразу, отваливается — проверяй это ДО выбора архитектуры, а не после.

## 5. Agent loop

`packages/core/agent-loop/src/agent.ts`, класс `ReactLoopAgent implements Agent` — 543 строки (проверено).

**Форма — машина состояний, а не резидентный цикл.** Фазы: `idle | maintenance | running`. Драйвер `kick()` крутит `while (await this.turn()) {}`, но запускается **только** из `wakeDriver()`, когда в инбокс что-то положили. Никакого `while (true)`: агент, которому нечего делать, не потребляет ничего. Для edge/serverless это принципиально — цикл естественно ложится на модель «разбудили → отработали → уснули».

Драйвер тоже сменяемый. Из `packages/README.md`:

> `dsh-agent-loop` is swappable; UI, hook, and tool plugins use `dsh-agent`

То есть UI и инструменты завязаны на интерфейс `Agent`, а не на конкретный ReAct-цикл.

**Поток одного хода:**

```
turn/start
  → сборка промпта
  → agent/pre-step
  → step/start
      → agent/request
      → llm/stream
      → assistant/chunk*
      → assistant/message
      → tool/call*
          → tools/pre-execute
          → tools/execute
          → tools/post-execute
      → tool/result*
  → step/end
  → agent/turn-stopping
turn/end
```

**Словарь (не путать):**
- **turn** — один слив допущенного ввода;
- **step** — один запрос к модели плюс вызванные им инструменты.

Один turn содержит N шагов.

## 6. Персистентность

**Раскладка на диске:**

```
$DSH_HOME/sessions/--<нормализованный-cwd>--/<session-id>/session.jsonl.zstd
```

Первая строка — `SessionHeader`, дальше по одному `SessionEvent` на строку.

`DSH_HOME` резолвится в `packages/util/home-paths` с приоритетом: **явный конфиг → `$DSH_HOME` → `~/.dsh`**. Сам путь задан конфигом, не хардкодом — `packages/bundle/base/cordis.patch.yml` (проверено):

```yaml
- id: session-persistence-jsonl
  name: '@deepseek-ai/dsh-session-persistence-jsonl'
  config:
    root: !!js dshHomePath('sessions')
```

**Инвариант «Model-visible means logged».** Всё, что дошло до модели, реконструируется из лога; проверяется рантайм-инвариантом, а не соглашением. Практическое следствие: лог — достаточное состояние для восстановления разговора.

**Что из этого следует:**
- `ctx.agents.resume(ownerCtx, { resumeSessionId })` — возобновление;
- `ctx.sessions.fork()` — ветвление;
- после краха незакрытый `turn/start` дописывается синтетическим `turn/end { reason: { kind: 'interrupted' } }` — лог самолечится, а не остаётся полубитым;
- компакция **не удаляет** события: surface-replacement `{ op: 'replace', start, end }` кладётся поверх. История неразрушима.

**КРИТИЧНО — переносится только ЛОГ.** Состояние драйвера (инбокс, фаза) не сериализуется: новый процесс стартует с фазой `idle` и пустым инбоксом. Значит «перенести живого агента между машинами» = перенести лог и заново разбудить, а не мигрировать процесс. Всё, что было в инбоксе на момент обрыва, теряется — если это неприемлемо, очередь ввода надо держать снаружи харнеса.

## 7. События (Cordis)

**Режимы диспетчеризации:**

| API | Семантика |
| --- | --- |
| `ctx.emit` | broadcast, без ожидания |
| `ctx.parallel` | все листенеры, ждём всех |
| `ctx.serial` | по очереди; первое non-null останавливает |
| `ctx.bail` | синхронный вариант serial |
| `ctx.waterfall` | around-middleware с `next()` |

`ctx.on(name, listener, options?)` возвращает disposer — отписка обязательна и предусмотрена.

**Waterfall — механизм вето.** Листенер получает `(...args, next)`. Вызвал `next()` — цепочка идёт дальше; вернул значение без `next()` — короткое замыкание, то есть вето/подмена. Это и есть штатный способ вклиниться в чужую операцию, не патча её.

**Режимы реальных событий:**

| Событие | Режим |
| --- | --- |
| `agent/pre-step` | waterfall |
| `agent/request` | waterfall |
| `tools/pre-execute`, `tools/execute`, `tools/post-execute` | waterfall |
| `llm/stream` | waterfall |
| `approval/request` | waterfall |
| `agent/turn-stopping` | serial |
| `session/event` | emit (~30 подписчиков) |

**События уже ходят по сети.** `packages/api/remotes/src/remote-events.ts` держит allowlist `API_REMOTE_FORWARDED_EVENTS` (~15 записей, проверено). Среди них в режиме `waterfall` — `approval/request` и `user-questions/request`: **браузер по сети ветирует операцию на хосте**. Прецедент распределённого waterfall уже в проде, изобретать транспорт для вето не нужно.

Но: `turn/*`, `step/*`, `tool/*` наружу как Cordis-события **не идут**. Это durable session events; до UI они доезжают через `session/event`. Не ищи их в allowlist — их там нет по устройству.

## 8. Граница UI ↔ ядро

Это полноценный сетевой клиент, а не UI, вшитый в процесс. Для нас (отделяем UI) — главный раздел.

**Унарные вызовы: HTTP.** `POST /api/<namespace>/<method>`. Префикс объявлен ровно одной константой — `packages/client/connection/src/api-path.ts` (проверено):

```ts
/** Route prefix owning every api request (`/api` and `/api/<anything>`). */
export const API_PATH = '/api'
```

Конверт валидируется Zod (`rpc-schema.ts`): запрос `clientRequestSchema { type: 'client-request', rpcId, method, payload }`, ответ — дискриминированный юнион `{ ok: true, value } | { ok: false, error }`.

**Стримы и события: один WebSocket-мультиплексор.** `packages/api/gateway/src/stream-protocol.ts` (проверено):

```ts
REMOTE_STREAM_MUX_PATH      = '/api/remote.mux'
REMOTE_EVENT_STREAM_ENDPOINT = '$events'
```

Кадры: клиент→хост `{ type: 'open' | 'cancel', streamId, endpoint, payload }`; хост→клиент `{ type: 'item' | 'error' | 'end' }`, плюс служебные `ready`, `emit`, `waterfall`.

> **Токены ассистента идут по WebSocket, НЕ по SSE.** Любой прокси между браузером и хостом обязан уметь full-duplex апгрейд. Прокси, умеющий только HTTP-стриминг, тихо сломает вывод модели — а «тихо» тут значит «сессия висит без ошибки».

**Typert** — генерируемый типобезопасный RPC (группа `packages/typert/*`): декоратор `@Remote('namespace/method')`, `@Remote({ mode: 'stream' })` отдаёт async-итератор. Последний параметр `AbortSignal` = отмена по проводу. Контракт клиент↔хост выводится из типов, руками не пишется.

**Аутентификация.** Команда печатает токенизированный startup URL; браузер обменивает токен на подписанную session cookie. По умолчанию слушает `127.0.0.1:3080`; `--host 0.0.0.0` **намеренно запрещён**. Выставлять наружу нужно туннелем/прокси, а не флагом — авторы закрыли эту дверь сознательно.

**In-process carrier без сокета.** `packages/api/gateway/src/client/index.ts` содержит ветку `if (connection.rpc.open === undefined) this.streams.start()` — тот же клиентский код работает без транспорта, вызывая хост напрямую. Значит граница UI↔ядро формальна и уже проверена в обоих режимах: это не «теоретически отделяемо», это отделено.

## 9. Профили — готовые топологии

Профиль — **упорядоченный стек YAML-патчей** над деревом плагинов. Посмотреть своё собранное дерево: `dsh --profile web --dump-config`. Все профили, кроме `sdk-minimal`, стоят на слое `dsh-base`.

| Профиль | Что делает |
| --- | --- |
| `web` | `npx @deepseek-ai/dsh web` → 127.0.0.1:3080, SPA + WS |
| `headless` | `dsh --profile headless "задача"` — одна персистентная сессия, печатает финальный ответ, выходит |
| `sdk` | JSON-RPC поверх stdio |
| `sdk-minimal` | то же без слоя `dsh-base` |
| `acp` | ACP-сервер, тоже JSON-RPC по stdio |

**`headless` и политика аппрувов.** Политика `'never'` **детерминированно резолвит всё в `rejected`** — fail-closed. Это не «аппрувы отключены», это «любой запрос на аппрув отклонён». Автоматизация, рассчитывающая на молчаливое разрешение, сломается — и это правильное поведение.

### 9.1 Профиль `sdk` — наш вход

`packages/sdk/*` — три пакета: `protocol`, `server`, `client`. Транспорт — JSON-RPC поверх stdio, то есть интеграция не требует ни порта, ни HTTP-стека, ни авторизации: поднял подпроцесс, говоришь по трубам.

Python-SDK **есть в самом репозитории**: `python/sdk-runtime` — отдельный workspace-член с `pyproject.toml`, `hatch_build.py` и `platforms.json`; он запускает `dsh --profile sdk` подпроцессом.

> **Поправка.** Ранее наличие Python-SDK числилось неподтверждённым (знали о нём из обзорной статьи). Подтверждено первоисточником: `python/sdk-runtime` перечислен в `pnpm-workspace.yaml` и присутствует в дереве `master`. Убрано из «не подтверждено».

## 10. Мультипровайдерность LLM (`llm-pi-ai`)

Пакет `packages/llm/llm-pi-ai` стоит поверх `@earendil-works/pi-ai` и представляет собой **один плагин-пул**, а не набор плагинов на провайдера.

- Конфиг: `$DSH_HOME/settings.yaml`, ключи `llm-pi-ai.providers.<id>`.
- Креды **отдельно**: `$DSH_HOME/.credentials.yaml` (по ссылкам `apiKeyEnv`), в основной конфиг не попадают.
- Каталожные провайдеры: DeepSeek, Anthropic, OpenAI, Bedrock, Vertex, Azure, Codex.
- Протоколы: `openai-completions`, `openai-responses`, `anthropic-messages`.
- Свой gateway = строчка конфига, кода писать не нужно.

Пакет смонтирован **дремлющим**: комментарий в `packages/bundle/base/cordis.patch.yml` (проверено дословно) описывает это как «zero routes (and no extra models in the picker) until a `llm-pi-ai:` settings section supplies provider profiles» — маршруты регистрируются, когда появились профили, и снимаются, когда секция опустела. Ровно это делает страница Models в веб-морде.

> **КРИТИЧНАЯ ПОПРАВКА.** Это пул **ВЫБОРА** — все модели сваливаются в один picker, пользователь выбирает. Это **НЕ роутер**. Автоматического fallback при ошибке, балансировки нагрузки и ротации нескольких учёток одного провайдера в документации **нет**. Если нужна отказоустойчивость или размазывание по ключам — это наша работа (свой `LlmAdapter` либо gateway снаружи), и планировать её надо явно. Единственный намёк на устойчивость в дереве — отдельный пакет `llm-retry`, то есть ретраи, а не маршрутизация.

## 11. Что требует Node-рантайма

Вывод, ради которого делалась вся проверка: **ядро почти чистое, Node нужен провайдерам.**

**Ядро.** Прогон по всем 45 `.ts`-файлам `packages/core/*/src/` дал ровно **три** файла с node-импортами (проверено обходом содержимого):

| Файл | Импорт | Зачем |
| --- | --- | --- |
| `agent-loop/src/index.ts` | `node:crypto` | `randomUUID` |
| `agent/src/index.ts` | `node:async_hooks`, `node:util/types` | `AsyncLocalStorage` (инициаторы), `isPromise` |
| `session/src/index.ts` | `node:path` | `isAbsolute` |

`core/tools`, `core/system-prompt`, `core/scope` — чисты полностью. Все четыре модуля доступны в Workers с `nodejs_compat`.

**Швы тоже почти чисты.** Ноль node-импортов: `shell/shell`, `fs/fs`, `session/session-persistence`. Исключения (точечные, но их надо знать):

- `subprocess/subprocess/src/types.ts` → `node:stream`
- `sandbox/sandbox/src/roots.ts` → `node:fs`, `node:os`
- `llm/llm/src/attribution.ts` → `node:module`

**Node ЖЁСТКО нужен:** `subprocess-local` (node-pty, koffi), `sandbox-local` (`@deepseek-ai/node-addon-landlock-run`; bwrap+Landlock / Seatbelt / Windows restricted token), `fs-local`, `terminal-bash` (node-pty/ConPTY), `session-persistence-jsonl|sqlite`, `storage-json|sqlite`, `workflow-worker-thread` (`node:worker_threads`), `apps/cli/src/bin.ts`.

**Node НЕ нужен:** `subprocess-e2b`, `fs-e2b` — они ходят HTTP'ом в удалённую песочницу. Это и есть доказательство, что удалённый провайдер снимает зависимость от Node, а не переносит её.

**Репозиторий сам объявляет браузерное подмножество:** `tsconfig.base.client.json` (lib ES2024 + DOM, **без** `types: ["node"]`) против `tsconfig.base.json` / `tsconfig.host.json` (с `types: ["node"]`). Граница «что живёт без Node» проведена авторами, а не нами.

### 11.1 Главная улика — `packages/experimental/webworker-runtime`

Из описания пакета: «The browser worker host: the whole harness plugin tree runs inside one dedicated Web Worker… Use it when a preview must run the packaged harness without a Node host».

То есть **весь плагин-tree харнеса уже запускается в браузерном Web Worker**. Устройство:

- `module-proxies.ts` — **единственная платформенная развилка**, подменяет `node:*` на VFS / tunnel / браузерные примитивы. Одно место правды, а не размазанные `if (isBrowser)`.
- Даже `node:child_process` там не заглушка, а реализация: `spawn` поднимает команду в отдельном Web Worker.
- Честный список **не вытянутого**: `node:dns/promises`, `node:vm`, `node:net`, `node:sqlite`, `node:worker_threads` — структурные заглушки, каждый вызов **громко падает** (fail loud, не silent-wrong).

**Чего этот путь не даёт:** изоляция — граница VFS, а не kernel Landlock; шелл не bash; нет git и сетевых утилит. Это «превью харнеса без Node-хоста», а не замена песочнице.

## 12. Как подменить провайдера без форка

**Механизм.** Пакет объявляет свой слой через `package.json`:

```json
{ "dsh": { "bundle": { "patch": "./cordis.patch.yml" } } }
```

**Порядок слоёв** (позже = сильнее): бандлы профиля → `cordis.patch.yml` профиля → `$DSH_HOME/cordis.patch.yml` → `--patch` из argv.

Правило слияния, дословно из доков:

> Later layers win per row, and a patch replaces a row's entire config value rather than deep-merging keys.

**Важное следствие:** патч заменяет `config` строки **целиком**, а не домердживает ключи. Хочешь поменять один ключ — переписываешь весь `config` этой строки. Ожидание deep-merge здесь тихо не сбудется.

**Подмена = переписать строку с нужным `id`:**

```yaml
- id: subprocess
  name: '@deepseek-ai/dsh-subprocess-local'   # ← заменить на своё
```

`id` — это идентичность строки; `name` — что в неё подставлено. Меняем `name`, `id` держим.

**Установка плагина:**

```bash
dsh plugin --profile demo add ./hello-plugin
dsh plugin --profile demo add github:you/repo
```

**Минимальный плагин:**

```ts
import type { Context } from '@deepseek-ai/cordis'

export const name = 'hello'

export function apply(ctx: Context) {
  console.log('hello')
}
```

**Сервис** — класс плюс declaration merging в `interface Context`, чтобы `ctx.<твой-сервис>` был типизирован у потребителей.

## 13. Экосистема сторонних плагинов

Официального каталога **нет**. Есть GitHub topic [`dsh-plugin`](https://github.com/topics/dsh-plugin) и community-списки: `awesome-dsh-plugin` (13.3k★), `dsh-market` (2.7k★).

**Удалённое исполнение — ниша перекошена в SSH.** `dsh-ssh`, `dsh-ssh-remote`, `dsh-remote-ssh`, `dsh-winrm` — около десятка конкурентов, канон не выбран. Остальное почти пусто: Docker — `frozo-ai/dsh-worlds` (2★); облако — `NeevCloudAI/dsh-neev-sandbox` (1★), `pawaca/dsh-edge` (11★).

**Совсем пусто:** Modal, Daytona, Firecracker, devcontainer, GitHub Actions как среда исполнения агента.

**Удалённое хранение сессий — почти пусто:** `weisanju/dsh-postgres-backends` (0★), `tancheng33/dsh-spill-s3` (1★). Redis / D1 / Cloudflare KV / HTTP-sync как бэкенд session persistence — **не найдено ничего**. Это самая свободная ниша из просмотренных.

> **Ловушка при поиске.** Рубрика «Remote & Mobile» в awesome-списке (~60 плагинов) выглядит как конкуренты, но это в основном удалённый **ДОСТУП** к локально работающему харнесу: телефон, Tailscale, Telegram-мосты. Исполнение остаётся на машине пользователя. Не считать их занятой нишей — они решают другую задачу.

---

## Что не подтверждено

- **Детали протокола E2B за пределами README.** Формат запросов к песочнице, семантика таймаутов, поведение при обрыве — читались только по README пакетов. Перед реализацией собственного удалённого провайдера по этому образцу надо читать `packages/e2b/*/src`.
- **Глубокая часть документации недоступна на сайте.** `docs/cordis-api/*`, `docs/subsystems/*`, `docs/capability-seams.md` на [deepseek-harness.github.io](https://deepseek-harness.github.io/deepseek-harness/) отдают 404 и живут **только в репозитории**. Ссылаться на сайт для этих разделов нельзя — только на `raw.githubusercontent.com`. (Из-за этого же реестр швов и часть цитат в этом документе взяты из репозитория, а не с сайта.)
- **Точные звёзды сторонних плагинов** — снимок на 2026-08-28, метрика подвижная; отсутствие плагина в списке значит «не нашли», а не «не существует».

### Снято с этого списка

- ~~Наличие Python-SDK~~ — **подтверждено первоисточником**: `python/sdk-runtime` перечислен в `pnpm-workspace.yaml` и присутствует в дереве `master` (`pyproject.toml`, `hatch_build.py`, `platforms.json`, `src`). Раньше знали только из обзорной статьи.

### Поправки к более ранним тезисам

| Было | Стало | Как проверено |
| --- | --- | --- |
| 247 пакетов в **50** группах | 247 пакетов в **51** группе | обход `git/trees/master?recursive=1` |
| «~30 фич-пакетов `ui-*`» | **37** пакетов `ui-*` (из 43 в `packages/client/*`) | там же |
| `fs-e2b` в `packages/fs/*` | `fs-e2b` в **`packages/e2b/fs-e2b`** | там же |
| workspace глобит `packages/*/*` | глобит также `apps/*`, `website`, `vendor/*`, `native/landlock-run{,/packages/*}`, **`python/sdk-runtime`** | `pnpm-workspace.yaml` |
| Определение шва кончается на «a Consumer using it» | у цитаты есть хвост: «…**, commonly a model-facing tool**» | `docs/architecture.md` |
| Из forwarded-событий waterfall — `approval/request` | waterfall также у **`user-questions/request`** | `packages/api/remotes/src/remote-events.ts` |

## Источники

**Проверено напрямую 2026-08-28:**

- [`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) — метаданные через GitHub API: `master`, TypeScript, MIT, 201 387★, создан 2026-08-13, push 2026-08-27, не архивирован
- [`pnpm-workspace.yaml`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/pnpm-workspace.yaml) — глобы workspace
- [`docs/architecture.md`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/docs/architecture.md) — определение шва (:111), «one execution world» (:113)
- [`packages/e2b/README.md`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/e2b/README.md) — «experimental POC… no shipped composition enables it by default»
- [`packages/e2b/subprocess-e2b/README.md`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/e2b/subprocess-e2b/README.md) — «exactly as with local execution», ловушка pid / ACP
- [`packages/client/connection/src/api-path.ts`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/client/connection/src/api-path.ts) — `API_PATH = '/api'`
- [`packages/api/gateway/src/stream-protocol.ts`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/api/gateway/src/stream-protocol.ts) — `/api/remote.mux`, `$events`, кадры
- [`packages/api/remotes/src/remote-events.ts`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/api/remotes/src/remote-events.ts) — allowlist `API_REMOTE_FORWARDED_EVENTS`
- [`packages/bundle/base/cordis.patch.yml`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/bundle/base/cordis.patch.yml) — `root: !!js dshHomePath('sessions')`, дремлющий `llm-pi-ai`
- [`packages/core/agent-loop/src/agent.ts`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/core/agent-loop/src/agent.ts) — `ReactLoopAgent`, 543 строки, фазы `idle|maintenance|running`, `wakeDriver`/`kick`
- [`packages/core/agent/src/index.ts`](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/master/packages/core/agent/src/index.ts) — `node:async_hooks`, `node:util/types`
- обход всех 45 `.ts` в `packages/core/*/src/` на предмет `node:*`

**Прочитано, но не перепроверено построчно:** `docs/capability-seams.md`, `packages/README.md`, README пакетов `shell`, `subprocess`, `fs`, `storage`, `session-persistence`, `llm`, `llm-pi-ai`, `webworker-runtime`, документация по профилям и `dsh plugin`.

**Внешнее:** [Cordis](https://github.com/cordiverse/cordis), [`@earendil-works/pi-ai`](https://www.npmjs.com/package/@earendil-works/pi-ai), GitHub topic [`dsh-plugin`](https://github.com/topics/dsh-plugin), community-списки `awesome-dsh-plugin`, `dsh-market`.
