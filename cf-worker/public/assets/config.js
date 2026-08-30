/* Клиентская таблица маршрутов — ключ → полный путь. Источник правды: cf-worker/api-spec.json
   (паритет охраняет scripts/check-frontend-contract.mjs). В коде страницы пути не
   склеиваются и литералов "/api…" нет: только route("ключ") из app.js. */
window.EDGE_CONFIG = {
  locale: "ru",
  routes: {
    status: "/api/status",
    events: "/api/events",
    eventsLive: "/api/events.live",
    tasks: "/api/tasks",
    task: "/api/tasks/",
    heartbeat: "/api/heartbeat",
    session: "/api/session",
  },
  replayPageSize: 200,
  /* Сокет переподключается проактивно: точное значение idle-timeout Cloudflare
     недокументировано, гарантированно живого соединения ждать нельзя. */
  socketRecycleMs: 240000,
  reconnectBaseMs: 1000,
  reconnectMaxMs: 15000,
  journalMaxRows: 500,
};
