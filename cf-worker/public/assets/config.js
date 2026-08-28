/* Клиентская копия маршрутов и порогов. Единственное место правды на сервере —
   cf-worker/src/config.ts; паритет двух файлов охраняет scripts/check-frontend-contract.mjs. */
window.EDGE_CONFIG = {
  locale: "ru",
  routes: {
    apiPrefix: "/api",
    status: "/api/status",
    events: "/api/events",
    eventsLive: "/api/events.live",
    tasks: "/api/tasks",
    task: "/api/tasks/",
    heartbeat: "/api/heartbeat",
  },
  replayPageSize: 200,
  /* Сокет переподключается проактивно: точное значение idle-timeout Cloudflare
     недокументировано, гарантированно живого соединения ждать нельзя. */
  socketRecycleMs: 240000,
  reconnectBaseMs: 1000,
  reconnectMaxMs: 15000,
  journalMaxRows: 500,
};
