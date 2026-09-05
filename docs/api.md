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

## `POST /api/messages/ingest`

Приём сообщения владельца в инбокс (#20) под обычной авторизацией — эндпоинт админско-релейный; прямая доставка вебхуком Telegram не подключена. Понимает плоскую форму (source, source_msg_id, chat_id, sender_id, sender_name, text) и сырой Telegram update (update_id, message.from/chat — числа приводятся к строкам). Ключ идемпотентности: source_msg_id, иначе update_id, иначе message.message_id; без идентификатора — 400 need_source_msg_id. Возвращает {message_id, status: accepted|exists}.

## `GET/POST /api/messages`

GET — список сообщений с фильтрами (status, kind, sender_id; пагинация after — курсор по id против сортировки ts DESC — и limit). POST — ручное создание сообщения.

## `GET /api/messages/`

Одно сообщение по id.

Остаток пути после `/api/messages/` — параметр.

## `POST /api/messages/process`

Разбор новых сообщений: классификация (directive/chat/doc_edit/raw), группировка; для директив и doc_edit — issue под GH_ISSUES_TOKEN (kind в теле issue; не задан токен или сеть — повтор до LIMITS.messageMaxAttempts, потом честный failed; raw уходит в ignored на ручной триаж). Тело {limit, retry_failed: true} — вернуть failed в new с обнулёнными попытками. Возвращает {processed, results}. Тот же разбор ведёт пульс DO (alarm) — ручной вызов не обязателен.
