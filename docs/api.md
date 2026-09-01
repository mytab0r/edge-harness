# API edge-harness

<!-- СГЕНЕРИРОВАНО из cf-worker/api-spec.json командой `npm run docs`. Руками не править. -->

Все маршруты требуют сессионную куку (браузер, выдаётся `POST /api/session` в обмен на Bearer) или `Authorization: Bearer <HANDS_TOKEN>` (job). Токен в query (`?token=`) отклоняется кодом 400 `query_token_removed`.
Ошибки — JSON `{"error": {"code", "message"}}`, коды стабильны и проверяются тестами.

## `GET /api/status`

Статус рук и счётчики задач. Руки живы, если heartbeat свежее порога.

## `GET/POST /api/events`

Журнал: POST — батч событий (идемпотентность по task_id+seq); GET — replay с пагинацией (after, limit, ?task_id=), заголовки x-has-more / x-next-after.

## `GET /api/events.live`

WebSocket, только на приём (гибернация). ?after=<event id> — не присылать старое. Клиентская запись закрывается кодом 1008. Требует Upgrade: websocket, иначе 400.

## `GET/POST /api/tasks`

POST — задача в очередь + repository_dispatch (без GH_DISPATCH_TOKEN — честный dispatch=not_configured); GET — последние задачи.

## `GET /api/tasks/`

Одна задача по id; содержит latency_ms — замер «dispatch → первый heartbeat».

Остаток пути после `/api/tasks/` — параметр.

## `POST /api/heartbeat`

Отметка живости рук {job_id, task_id?}. Первая отметка задачи фиксирует latency_ms.

## `POST/DELETE /api/session`

Вход браузера: POST обменивает Authorization: Bearer <HANDS_TOKEN> на подписанную сессионную куку (HttpOnly, SameSite=Strict, Secure, TTL в src/config.ts); DELETE сбрасывает куку. Job продолжает ходить Bearer'ом; токен в query (?token=) отклоняется кодом 400 query_token_removed.

## `POST /api/messages/webhook`

Вебхук для входящих сообщений (Telegram, и др.). Принимает JSON с полями source, source_msg_id, chat_id, sender_id, sender_name, text. Идемпотентен по source+source_msg_id. Возвращает {message_id, status}.

## `GET/POST /api/messages`

GET — список сообщений с фильтрами (status, kind, limit, after). POST — ручное создание сообщения (для тестов/админа).

## `GET /api/messages/`

Одна сообщение по id.

Остаток пути после `/api/messages/` — параметр.

## `POST /api/messages/process`

Запустить обработку новых сообщений: классификация, группировка, создание задач для директив. Возвращает {processed, created_issues, errors}.
