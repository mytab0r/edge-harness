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
    }
  ]
};

// Типизированная обёртка над api-spec.json — единственного места правды по API.
// Роутинг сервера, клиентская таблица (public/assets/config.js), документация
// (docs/api.md) и все проверки строятся из этого файла.

export interface ApiRoute {
  name: string;
  path: string;
  methods: ("GET" | "POST")[];
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
    if (route.methods.includes(method as "GET" | "POST") && route.path === pathname) {
      return { route, rest: "" };
    }
  }
  for (const route of API_SPEC) {
    if (!route.rest) continue;
    if (route.methods.includes(method as "GET" | "POST") && pathname.startsWith(route.path)) {
      return { route, rest: decodeURIComponent(pathname.slice(route.path.length)) };
    }
  }
  return null;
}
