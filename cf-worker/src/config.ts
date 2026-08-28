// Единственное место правды для маршрутов, лимитов и имён.
// Клиентская копия путей живёт в public/assets/config.js — паритет двух файлов
// охраняет scripts/check-frontend-contract.mjs (npm run check).

export const ROUTES = {
  apiPrefix: "/api", // общий префикс: всё, что с него начинается, уходит в Durable Object
  status: "/api/status",
  events: "/api/events",
  eventsLive: "/api/events.live",
  tasks: "/api/tasks",
  task: "/api/tasks/", // + <id>
  heartbeat: "/api/heartbeat",
} as const;

export const LIMITS = {
  /** «Руки живы» = последняя отметка свежее этого порога. */
  heartbeatFreshMs: 60_000,
  /** Сколько событий отдавать replay'ем, если лимит не назван. */
  replayDefault: 100,
  /** Потолок одной страницы replay. */
  replayMax: 500,
  /** Потолок батча в POST /api/events. Ограничен лимитом плейсхолдеров DO SQLite
   *  (100 на statement): предчтение дублей тратит 1 + размер батча. */
  batchMax: 50,
  /** Тело запроса больше этого размера отклоняется. */
  bodyMaxBytes: 1_048_576,
  /** payload задачи больше этого числа символов отклоняется. */
  payloadMaxChars: 8192,
  /** Сколько последних задач отдаёт список. */
  tasksListMax: 100,
} as const;

export const GITHUB = {
  apiBase: "https://api.github.com",
  apiVersion: "2022-11-28",
  userAgent: "edge-harness-do",
  /** event_type для repository_dispatch. */
  dispatchEventType: "harness-task",
} as const;

/** Имя единственного объекта. Мультитенантности нет, владелец один. */
export const OWNER_OBJECT_NAME = "owner";

/** Локаль сообщений API. Словари — в messages.ts. */
export const LOCALE = "ru" as const;
