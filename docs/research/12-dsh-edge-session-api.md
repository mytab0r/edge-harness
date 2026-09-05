# dsh-edge: session API и шов импорта транскрипта

> Исследовано 2026-08-30/31 в рамках #119; нативные вызовы выверены на живом
> проде (dsh-edge.mytab0r.workers.dev, release 0.7.1 = пин 113a969). Пин
> поднят задачей #134 до release 0.8.0 (`b9a8ddd`) — API, описанный ниже, не
> менялся между релизами (патч 0004-harness-ingest применился без правок
> в затронутых им местах session-store.ts). Смежное:
> [архитектура dsh-edge](11-dsh-edge.md).

## Auth: кука владельца, не Bearer

- Единственная аутентификация API — подписанная HttpOnly-кука
  `__Host-dsh_edge_owner` (или `dsh_edge_owner` на http). Bearer-токены и
  заголовок `Authorization` API не принимает: `index.ts` вырезает
  `authorization` перед передачей в DO (`requestForInstance`).
- Логин: `POST /api/auth/login`, form-urlencoded `accessKey=<ключ воркера
  DSH_EDGE_ACCESS_KEY>` → `303` + `set-cookie` (TTL 30 дней). Проверка:
  `GET /api/auth/session` → `{"authenticated":true}`.
- Кросс-доменные запросы с кукой отклоняются (403) по заголовку `Origin`;
  curl/серверные клиенты без `Origin` проходят.
- Перед мордой стоит Cloudflare с отдельным фильтром по подписи HTTP-клиента
  (не то же самое, что проверка `Origin` выше — этот фильтр режет запрос ДО
  приложения). Библиотечные User-Agent без явного значения детектируются и
  блокируются: `Python-urllib/3.x` (дефолт `urllib.request`, если не задать
  заголовок явно) получает `403` с `Content-Type: text/plain` и телом
  `error code: 1010` — это задокументированный код Cloudflare «browser
  signature blocked», а не ошибка приложения (там `Content-Type:
  application/json`/`text/html`, другой формат). Проверено прямым
  экспериментом 2026-09-02 (#225): тот же `POST /api/auth/login` с одним и
  тем же неверным телом, единственное отличие — заголовок `User-Agent`:
  `curl/8.x` → `401` (ответ приложения), `Python-urllib/3.11` → `403 error
  code: 1010`. Собственное имя клиента (`edge-harness-orchestra/1.0 (+…)`)
  фильтр пропускает — маскироваться под браузер/curl не потребовалось.
  Практический вывод: любой библиотечный HTTP-клиент к морде обязан явно
  задавать `User-Agent`, иначе получит 403 раньше, чем дойдёт до auth/RPC.

## RPC: конверт и полный список методов

- `POST /api/<method>` (например `/api/session.create`) с телом
  `{type:"client-request", rpcId:"<непустая строка>", method:"<тот же method>",
  payload:{…}}`. Ответ: `{type:"server-response", rpcId, result:{ok:true,value}|{ok:false,error}}`.
- `method` в теле обязан совпадать с путём; content-type — `application/json`.
- Методы (из UNARY_ROUTES `@deepseek-ai/dsh-host-apiproxy` 0.1.1-rc.2):
  `session.list/search/create/history/models/selectModel/rename/fork/prompt/
  attachment/updateQueue/cancel`, `subagent.*`, `host.describe/pickDirectory/
  listDirectory/createDirectory/openPath`, `workspace.list/create/rename/delete/
  insertBefore/insertSessionBefore/archiveSession`, `skill.list`, `goal.*`,
  `settings.*`, `credentials.*`, `llm.*`.
- Полезные payload'ы (все проверены живьём):
  - `workspace.create {path}` → `{workspace:{workspaceId,…}, created}`.
    Идемпотентен по каноническому пути: повтор даёт `created:false` и тот же id.
    Проверку «каталог существует» в Edge снял upstream-патч dsh-workspace, так
    что `/workspace/edge-harness` создаётся без файлов в VFS.
  - `session.create {workspaceId XOR cwd, sessionId?, agentPreset?}` →
    `{sessionId, agentPreset}`. `sessionId` можно задать САМОМУ (любая непустая
    строка ≤ MAX_SESSION_ID_LENGTH) — это даёт идемпотентный «create-or-reuse».
  - `session.rename {sessionId, title}` → `{title, seq}`. Заголовок хранится
    как `session/title` с `source:{kind:"user"}` — а такой заголовок
    ПИННИТСЯ: `SessionTitleService.onUserMessage` не планирует авторексайр
    (проверено по `@deepseek-ai/dsh-session-title` 0.1.1-rc.2), то есть
    FirstPromptTitle не перезапишет «#N: задача» после первого сообщения.
  - `workspace.archiveSession {sessionId}` → `{archivedSessionIds:[…]}` —
    архив глобальный; архивированная сессия исчезает из активных, история
    читаема. Неизвестная сессия → `result.ok:false`, `error.code:"session-not-found"`.
  - `session.list {}` → `items[].projections.values.title` — поиск сессии
    по заголовку клиентом (оркестратором) возможен без patch.
- Replay-чтение: `GET /api/sessions/:id/events` — SSE-кадры (`data: {JSON}`).

## session.prompt / session.history: контракт для клиентских плагинов (#113)

Снято с типов `@deepseek-ai/dsh-host-apiproxy` 0.1.1-rc.2 (`lib/types/api/
sessions.d.ts`) и `@deepseek-ai/dsh-session` 0.1.1-rc.2 (`SessionEventMap`,
`SessionEvent`); читано 2026-09-03 для заказа плагинов из plugin-manager.
Живым прогоном на проде НЕ проверялось — отмечено в «Не подтверждено».

- `session.prompt {sessionId, mode:'queue'|'steer', content:[{type:'text',
  text}|{type:'image',…}], clientTimeZone?}` → `{accepted:true, command?}`.
  `accepted` = сообщение принято к ходу (поставлено в очередь или отправлено),
  НЕ «агент справился». Сообщение из одного текст-блока, начинающееся с `/`,
  хост исполняет как slash-команду, а модели НЕ отправляет. Режим `queue`
  при активном ходе ставит сообщение в очередь (FIFO), `steer` вмешивается
  в текущий ход — для автоматических заказов корректен `queue`.
- `session.history {sessionId, beforeSeq?, maxMessages?}` → `{events:[{event,
  view?}], hasMore, projections?}`. Страницы режутся по границам
  «append-origin» сообщений (одна страница = целое число сообщений со всеми
  их chunk/tool-событиями); первый запрос без `beforeSeq` возвращает ХВОСТ
  (новейшие `maxMessages` сообщений), вглубь — по `beforeSeq`. Сырое событие:
  `{type, seq, time, data, ignorable?, surfaceOp?}`; для `user/message`
  `data` — UserMessage `{id, role:'user', content:[ContentBlock], source}`,
  текстовые блоки — `{type:'text', text}`.
- Неизвестная сессия в сессионных методах → `result.ok:false`,
  `error.code:"session-not-found"` — клиентский код обязан отличать это от
  сетевого отказа (для заказов плагинов это штатное «заказов ещё нет», не
  ошибка).

## Форма канонических событий (то, что можно дописать в сессию)

Словарь — `SessionEventMap` (`@deepseek-ai/dsh-session` 0.1.1-rc.2,
`lib/types/types.d.ts`). Поверхностные (влияют на derived history и чат):
`user/message` (data = само сообщение), `assistant/message` `{turn, step,
message, usage?, interrupted?}`, `tool/result` `{turn, step, message, error?, meta?}`.
Журнальные: `turn/start {turn}`, `turn/end {turn, reason:{kind}}`,
`step/start`, `step/end` `{turn, step}`, `tool/call {turn, step, callId, name,
arguments}`. Поверхностное событие при `Session.append` ОБЯЗАНО нести
`surfaceOp` (для дописывания — `'append'`); `sourceEventSeqs` опционален.
Контент-блоки (`@deepseek-ai/dsh-llm`): `text`, `reasoning` (think),
`tool-call`, `tool-result`.

Спул плагина dsh-hands-streamer уже несёт эту форму (`{v, session_id, seq,
time, type, data}`) с allowlist из 8 типов — то есть раннерский транскрипт
пересаживается в DO-сессию почти дословно; морда назначает свои seq.

## Чего в dsh-edge НЕТ и что дал патч 0004

- Апстрим не имеет API дописать события в чужую сессию: поверхностные события
  пишет только агент-цикл. Импорт транскрипта раннера закрыт нашим патчем
  `dsh-edge/patches/0004-harness-ingest.patch`: `POST /api/sessions/:id/ingest`
  (батч `{events:[{type,data}]}`, allowlist = allowlist спула, перенумерация
  turn поверх хранимого лога, `surfaceOp:'append'`, flush до ответа,
  публикация живым подписчикам через штатный late-event путь стора).
- Двойная публикация не возникает: стор сам публикует события не-turn'овых
  агентов (конфиг `onLateSessionEvent`), поэтому маршруту достаточно append+flush.
- Бренд-нейтрально: `session.rename` живого прода корректно хранит UTF-8
  заголовки (кириллица проверена round-trip'ом).

## Не подтверждено

- Прокси статусов `/api/harness/*` на воркере морды (закрытие белого пятна
  #105 владельцем, релиз-ноты plugins-manager-v0.1.2 от 2026-08-31: «журнал
  читается через /api/harness/* (патч 0004)») существует в ДЕПЛОЕ владельца;
  источник в этом репо появился с `dsh-edge/patches/0005-harness-status-proxy.patch`
  (PATCHES.md, #235): маршрут `GET /api/harness/events` пересылает запрос в
  `{HARNESS_URL}/api/events` с Bearer HANDS_TOKEN, форма запроса/ответа —
  контракт журнала, прокси прозрачен; контракт прогоняется на собранном
  артефакте `dsh-edge/proxy-integration/check.mjs` на каждом деплое. ЖИВОЙ
  прод это по-прежнему не подтверждает: неавторизованный зонд
  `GET /api/harness/events` отвечает тем же `401 {"ok":false,"error":"Owner
  authentication required."}`, что и заведомо несуществующий путь, — auth в
  морде стоит ДО роутинга, поэтому существование/форма маршрута на проде
  зондом не доказывается; plugin-manager (#113; текущий пин — dsh-edge/plugins.json) опирается на
  прокси с проверкой формы ответа журнала и громкой ошибкой при
  несоответствии. Живая проверка прокси И заказа с кукой владельца —
  беклог-задача #233 (рамка расширена комментарием в задаче).
- Поведение UI при `tool/call` без предшествующего `assistant/message` с
  tool-call-блоком глазами не проверялось (браузер, живая сессия), но структура
  прояснена (#131): `tool/call` — журнальный тип (`{turn, step, callId, name,
  arguments}`), в производную историю чата не входит; нативный рендер тула
  (`@deepseek-ai/dsh-client-ui-tool`, npm-пакет `@deepseek-ai/dsh-llm`
  `types.d.ts` `ContentBlockMap['tool-call']`) строит «детали тула» ИЗ
  content-блока `{type:'tool-call', id, name, arguments}` внутри
  `message.content` самого `assistant/message` — это тот же вызов, просто в
  форме модельного вывода, а не бухгалтерии хода. Наш собственный
  `dsh-edge/ingest-integration/check.mjs` (батч 1, protocol-only тест) кладёт
  `tool/call` БЕЗ такого блока — намеренное упрощение уровня HTTP-контракта, не
  образец прод-формы; спутать его с реальным батчем и было ровно тем, что
  владелец увидел на демо-сессии (заглушки `provider`/`model` = буквальные
  значения фикстуры `edge-harness`/`runner-model`, «unavailable» тула =
  отсутствие content-блока). Живой раннер (`dsh-hands-streamer`) пересылает
  РЕАЛЬНЫЕ канонические события DSH без изменения формы — блок должен приезжать
  естественно, когда модель вызывает тул. Гвардия
  `scripts/lib/verify_transcript.py` (вызывается `dsh_edge_verify_transcript`,
  `scripts/lib/dsh-edge-session.sh`) читает события КАЖДОГО прогона обратно
  через `GET /api/sessions/:id/events` и проверяет оба инварианта
  структурно — «не подтверждено» глазами, но подтверждается автоматически на
  каждом прогоне вместо разового ручного просмотра.
- Гонка «ingest против живого нативного хода» в той же сессии даёт BUSY (409)
  из `openAgentForTurn`; поведение UI при повторе после BUSY не изучалось.
- Контракт `session.prompt`/`session.history` (#113, раздел выше) снят с
  типов пакетов 0.1.1-rc.2, но живым RPC-прогоном на проде не проверялся:
  фактическая семантика `accepted`, коды ошибок помимо `session-not-found`
  и поведение UI при `mode:'queue'` для заказа плагина — до первого живого
  заказа. Плагин при несоответствии громко покажет ошибку секции (форма
  конверта проверяется).

## Источники

- Живой прод (RPC-пробы 2026-08-31): workspace/session/rename/archive/list,
  кодировки заголовков, идемпотентность workspace.create.
- npm-пакеты `@deepseek-ai/dsh-session`, `dsh-llm`, `dsh-workspace`,
  `dsh-host-apiproxy`, `dsh-session-title*` 0.1.1-rc.2 (types и lib).
- `apps/dsh-edge/src/{index,instance,session-store,http,auth,edge-api}.ts`
  на пине 113a969; `standalone/patches/audit.json`.
