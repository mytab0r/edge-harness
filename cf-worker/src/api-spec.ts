// СГЕНЕРИРОВАНО из api-spec.json командой `npm run docs`. Руками не править:
// источник правды по маршрутам — api-spec.json, этот файл — его типизированный снимок.
const spec = {
  "$comment": "Единственное место правды по API: роутинг сервера, клиентская таблица, документация и проверки строятся отсюда. Путь как строка живёт только здесь и в public/assets/config.js (паритет охраняет scripts/check-frontend-contract.mjs). name — ключ, по которому клиент и тесты ссылаются на маршрут.",
  "prefix": "/api",
  "routes": [
    {
      "name": "status",
      "path": "/api/status",
      "methods": [
        "GET"
      ],
      "auth": true,
      "summary": "Статус рук и счётчики задач. Руки живы, если heartbeat свежее порога."
    },
    {
      "name": "events",
      "path": "/api/events",
      "methods": [
        "GET",
        "POST"
      ],
      "auth": true,
      "summary": "Журнал: POST — батч событий (идемпотентность по task_id+seq); GET — replay с пагинацией (after, limit, ?task_id=), заголовки x-has-more / x-next-after."
    },
    {
      "name": "eventsLive",
      "path": "/api/events.live",
      "methods": [
        "GET"
      ],
      "auth": true,
      "summary": "WebSocket, только на приём (гибернация). ?after=<event id> — не присылать старое. Клиентская запись закрывается кодом 1008. Требует Upgrade: websocket, иначе 400."
    },
    {
      "name": "tasks",
      "path": "/api/tasks",
      "methods": [
        "GET",
        "POST"
      ],
      "auth": true,
      "summary": "POST — задача в очередь + repository_dispatch (без GH_DISPATCH_TOKEN — честный dispatch=not_configured); GET — последние задачи."
    },
    {
      "name": "task",
      "path": "/api/tasks/",
      "methods": [
        "GET"
      ],
      "auth": true,
      "rest": true,
      "summary": "Одна задача по id; содержит latency_ms — замер «dispatch → первый heartbeat»."
    },
    {
      "name": "heartbeat",
      "path": "/api/heartbeat",
      "methods": [
        "POST"
      ],
      "auth": true,
      "summary": "Отметка живости рук {job_id, task_id?}. Первая отметка задачи фиксирует latency_ms."
    },
    {
      "name": "session",
      "path": "/api/session",
      "methods": [
        "POST",
        "DELETE"
      ],
      "auth": true,
      "summary": "Вход браузера: POST обменивает Authorization: Bearer <HANDS_TOKEN> на подписанную сессионную куку (HttpOnly, SameSite=Strict, Secure, TTL в src/config.ts); DELETE сбрасывает куку. Job продолжает ходить Bearer'ом; токен в query (?token=) отклоняется кодом 400 query_token_removed."
    },
    {
      "name": "messagesIngest",
      "path": "/api/messages/ingest",
      "methods": [
        "POST"
      ],
      "auth": true,
      "summary": "Приём сообщения владельца в инбокс (#20) под обычной авторизацией — эндпоинт админско-релейный; прямая доставка вебхуком Telegram не подключена. Понимает плоскую форму (source, source_msg_id, chat_id, sender_id, sender_name, text) и сырой Telegram update (update_id, message.from/chat — числа приводятся к строкам). Ключ идемпотентности: source_msg_id, иначе update_id, иначе message.message_id; без идентификатора — 400 need_source_msg_id. Возвращает {message_id, status: accepted|exists}."
    },
    {
      "name": "messages",
      "path": "/api/messages",
      "methods": [
        "GET",
        "POST"
      ],
      "auth": true,
      "summary": "GET — список сообщений с фильтрами (status, kind, sender_id; пагинация after — курсор по id против сортировки ts DESC — и limit). POST — ручное создание сообщения."
    },
    {
      "name": "message",
      "path": "/api/messages/",
      "methods": [
        "GET"
      ],
      "auth": true,
      "rest": true,
      "summary": "Одно сообщение по id."
    },
    {
      "name": "messagesProcess",
      "path": "/api/messages/process",
      "methods": [
        "POST"
      ],
      "auth": true,
      "summary": "Разбор новых сообщений: классификация (directive/chat/doc_edit/raw), группировка; для директив и doc_edit — repository_dispatch (event_type inbox-issue) под GH_DISPATCH_TOKEN (kind в теле issue; не задан токен, сеть или отказ job'а — повтор до LIMITS.messageMaxAttempts, потом честный failed; raw уходит в ignored на ручной триаж). 204 dispatch'а — не доказательство созданной issue, сообщение остаётся processing до подтверждения job'ом (messagesIssueCreated) либо возврата ватчдогом. Тело {limit, retry_failed: true} — вернуть failed в new с обнулёнными попытками. Возвращает {processed, results}. Тот же разбор ведёт пульс DO (alarm) — ручной вызов не обязателен."
    },
    {
      "name": "messagesIssueCreated",
      "path": "/api/messages/issue-created",
      "methods": [
        "POST"
      ],
      "auth": true,
      "summary": "Подтверждение job'а .github/workflows/inbox-issue.yml (Bearer HANDS_TOKEN — тот же канал, что heartbeat): repository_dispatch (204) не доказывает созданную issue (docs/research/21-github-actions.md), эта строка — единственное доказательство. Тело {message_id, issue_number, issue_url} — issue создана, сообщение → done; {message_id, error} — job сам сообщает об отказе, тот же кап попыток, что у ошибки dispatch'а. Не в processing (ватчдог уже вернул сообщение в очередь) — {accepted: false, reason: \"not_processing\"} без ошибки."
    }
  ]
};

// Типизированная обёртка над api-spec.json — единственного места правды по API.
// Роутинг сервера, клиентская таблица (public/assets/config.js), документация
// (docs/api.md) и все проверки строятся из этого файла.

export interface ApiRoute {
  name: string;
  path: string;
  methods: ("GET" | "POST" | "DELETE")[];
  auth: boolean;
  /** path — префикс маршрута; остаток пути — параметр (например, id задачи). */
  rest?: boolean;
  summary: string;
}

export const API_PREFIX: string = spec.prefix;

export const API_SPEC: ApiRoute[] = spec.routes as ApiRoute[];

/** name → path. Клиентская таблица (assets/config.js) обязана совпадать с этой. */
export const ROUTES: Record<string, string> = Object.fromEntries(
  API_SPEC.map((route) => [route.name, route.path]),
);

/** Табличный роутинг: точное совпадение method+path, затем rest-маршруты. */
export function matchRoute(method: string, pathname: string): { route: ApiRoute; rest: string } | null {
  for (const route of API_SPEC) {
    if (route.rest) continue;
    if (route.methods.includes(method as "GET" | "POST" | "DELETE") && route.path === pathname) {
      return { route, rest: "" };
    }
  }
  for (const route of API_SPEC) {
    if (!route.rest) continue;
    if (route.methods.includes(method as "GET" | "POST" | "DELETE") && pathname.startsWith(route.path)) {
      return { route, rest: decodeURIComponent(pathname.slice(route.path.length)) };
    }
  }
  return null;
}
