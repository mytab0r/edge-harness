# Ворота #1, второй проход: сверка цитат

Дата: 2026-09-01. Продолжение [analyze-2026-09-01.md](analyze-2026-09-01.md),
раздел «Не проверял и почему» (первый проход прерван лимитом сессии).

Проверял агент, спеку не писавший. Метод: `npm pack` пакетов в scratchpad и
чтение исходников морды по пину `pawaca/dsh-edge@113a96913c51881993122afbf42e776882c4beb7`.
Утверждения формулировались списком, независимо от номеров строк спеки, чтобы
проверка не зависела от параллельной правки.

## ВЕРДИКТ: PASS с двумя обязательными правками цитат

Архитектурный вопрос, способный обрушить выбранный носитель, закрыт
положительно. Два факта опровергнуты — оба не блокируют, но должны быть
исправлены в спеке до реализации, иначе реализатор будет искать несуществующий
класс.

---

## Главный вопрос: directory `llm.providers` — ЖИВАЯ, не снапшот

Это условие работоспособности выбранного носителя: плагин монтируется в хвосте
`initialize()`, и если бы directory снапшотилась раньше, в пикер он бы не попал.

**Подтверждено четырьмя независимыми точками:**

1. `@deepseek-ai/dsh-llm/lib/index.js:1174-1193` (`registerAdapter`) и
   `:1251-1293` (`registerConfigurableProviders`) мутируют внутренние `Map`
   (`this.adapters`, `this.directory`) через `ctx.effect(...)` — эффект
   срабатывает в момент монтажа плагина, в любой момент жизни рантайма, без
   привязки к «стартовому окну».
2. `dsh-llm/lib/index.js:1240-1242` — `listProviders()`:
   `return [...this.adapters.values()].map(({ provider }) => ({ ...provider }))`
   — массив строится заново при каждом вызове, кэша нет.
3. Морда, `session-store.ts:436-454` (`listConfigurableProviders`) — сначала
   `await this.ready` (то есть дожидается полного завершения `initialize()`
   ВКЛЮЧАЯ хвостовой `installEdgePlugins`), затем читает
   `llm.listProviders()` / `llm.listConfigurableProviders()` живьём.
4. `edge-api.ts:754-766` — обработчик `llm.providers` зовёт
   `await runtime.listConfigurableProviders()` внутри самого запроса.

**Порядок монтажа подтверждён:** патч `0002-edge-plugins-module.patch` вставляет
`await this.installEdgePlugins(storage)` последней строкой тела `initialize()`,
тогда как `dshLlmDeepseek` монтируется на `session-store.ts:243` — то есть
плагин действительно монтируется ПОСЛЕ, и это не мешает.

Блокером было бы кэширование списка в конструкторе или при первом обращении —
не подтверждено нигде в прочитанном коде.

---

## Опровергнуто 1 (обязательная правка): класс называется иначе, и `node:crypto` шире

**Что утверждала спека:** Node-only импорты (`node:crypto`, `node:fs/promises`,
`node:path`, `lib/index.js:8-10`) живут только в `LocalUploadIndex` — дефолтном
файловом upload-index, который морда обходит.

**Что на самом деле:**

- Класса `LocalUploadIndex` в пакете **нет вовсе**. Настоящее имя —
  **`DeepSeekUploadIndex`**, `lib/index.js:539-646`.
- `mkdir` / `readFile` / `dirname` / `join` действительно вызываются только
  внутри этого класса (`:545`, `:550`, `:585`, `:615`, `:633`).
- Но `createHash` (`node:crypto`) вызывается на **`lib/index.js:494`** — в
  функции `deepSeekFileScope()`, объявленной ВНЕ класса (`:493`, класс
  начинается на `:539`), и она зовётся из `DeepSeekFileStore` (`:733`, `:762`,
  `:814`, `:825`, `:874`) при каждой сборке upload-scope для файла/изображения —
  **независимо от того, какой `index` передан в стор.**

**Практический вывод не меняется:** `node:crypto` штатно доступен в Workers под
`nodejs_compat`, блокера нет. Но формулировка «Node-only живёт только в
дефолтном upload-index» неверна, и имя класса неверно — реализатор пойдёт искать
несуществующий `LocalUploadIndex`.

**Правка:** заменить имя на `DeepSeekUploadIndex`, уточнить, что вне класса
остаётся `createHash` в `deepSeekFileScope()` и что он лежит на всём Files-пути,
а Workers-совместимость держится на `nodejs_compat`, а не на «путь мёртв».

## Опровергнуто 2 (обязательная правка): номер строки `writable`

`do-settings-provider.ts:19` → фактически **`:21`** (`override readonly writable = true`;
строка 19 пустая). Содержание верно, номер сдвинут на два.

Эта же цитата с неверным номером попала в `docs/research/11-dsh-edge.md`
(ветка `agent/153-single-source-provider-default`) — правится там же.

## Разрешено: неоднозначная цитата из minor 7 первого прохода

`LlmConfigurableProvider.settingsNs` — полный путь
`@deepseek-ai/dsh-llm@0.1.1-rc.2/lib/types/types.d.ts:156`, поле
`settingsNs: string;` внутри интерфейса, начинающегося на `:150`. Не в
`dsh-client-ui-settings-models` и не в `dsh-host-apiproxy`.

---

## Подтверждено точно

| Утверждение | Факт |
|---|---|
| `DeepSeekAdapter` публично экспортирован, пригоден для `new` извне | `lib/index.js:1817` — именованный экспорт; класс объявлен `:1332`, в типах `adapter.d.ts:156`; конструктор публичный, принимает `DeepSeekAdapterOptions` |
| Один инстанс = один провайдер | `adapter.d.ts:86-88` — один `options: () => DeepSeekConnectionOptions` и один `resolveApiKey`; поля provider id в опциях НЕТ (id приходит извне через `registerAdapter([PROVIDER], …)`) |
| `resolveFiles?` в опциях есть | `adapter.d.ts:101` |
| Provider id — жёсткая модульная константа, не конфиг | `lib/index.js:1591` `const PROVIDER = "deepseek-official"`; `:1795-1800` `registerConfigurableProviders`; `:1801` `registerAdapter`. `buildEdgeLlmPluginConfig` (`session-store.ts:1606-1619`) передаёт только `baseURL`/`maxTokens`/`reasoningEffort`/`streamIdleTimeoutMs` — ключа для смены id нет. Совпадает с апстримной заметкой «upstream plugin's provider ID is hardcoded» |
| Монтаж адаптера в морде | `session-store.ts:243` — точно |
| Морда подставляет свой upload-index | `session-store.ts:241-242` — `new DurableObjectUploadIndex(storage)` + `new DeepSeekFileStore({ index: doUploadIndex })` |
| Provider id морды | `session-store.ts:115` — `const EDGE_PROVIDER = 'deepseek-official'` |
| Креды generic по любому ref | `edge-credentials.ts:17` + `:107-109` (`storageKey` = `'dsh-edge:credential:' + ref`), `:64-69` (`set`), спецветка одна — фолбэк на worker-secret только для `EDGE_DEEPSEEK_API_KEY_REF` (`:44-49`, константа `:15`). **Прямой ответ: менять файл для второго провайдера НЕ нужно** |
| Апстримная заметка про отказ от pi-ai | Существует на пине; раздел `### Excluded` (`:49`), дословно `:53` — «`dsh-llm-pi-ai` bundles Node.js-only SDKs incompatible with Workers and exceeds the gzip budget», `:55` — «Multi-provider parallel registration (upstream plugin's provider ID is hardcoded…)», `:94` — про `@earendil-works/pi-ai`, `http-proxy-agent` и 900 KB gzip |

## Новое «не подтверждено», найденное этим проходом

**`edgeFileStore` кладётся в контекст как произвольное свойство, а не передаётся
адаптеру.** `session-store.ts:241-242` пишет
`(this.context as never as Record<string, unknown>)['edgeFileStore'] = new DeepSeekFileStore(...)`,
но в `buildEdgeLlmPluginConfig` (`session-store.ts:1606-1619`) поля `resolveFiles`
нет. Читает ли плагин `ctx.get('edgeFileStore')` при построении резолвера — не
проверено. Следствие для спеки: утверждение «морда обходит дефолтный upload-index
штатным `resolveFiles`» пока опирается на факт «стор создан и положен в
контекст», а не на факт «адаптер его получает». Для этого change не критично
(мы вообще не трогаем Files-путь), но опираться на это как на доказательство
Workers-совместимости нельзя — Workers-совместимость держится на `nodejs_compat`.

## Не проверялось

- Копии интерфейса `LlmConfigurableProvider` в `dsh-client-ui-settings-models`
  и `dsh-host-apiproxy` — искомый найден однозначно в `dsh-llm`, дублирование не
  исключено, но и не проверено.
- Связка `edgeFileStore` → `resolveFiles` (см. выше).
- Файлы спеки намеренно не читались, чтобы не зависеть от параллельной правки.

## Что требуется до старта реализации

1. Правка двух опровергнутых цитат в `design.md` (имя класса + `node:crypto`
   шире; `writable` — `:21`).
2. Правка `do-settings-provider.ts:19` → `:21` в
   `docs/research/11-dsh-edge.md` (ветка #153).
3. Разрешённый путь `types.d.ts` перенести из «Не подтверждено» в факты.

После этих трёх правок ворота #1 считаются пройденными: блокеров нет,
архитектурное допущение подтверждено кодом.
