// Единственное место правды для лимитов и внешних констант.
// Маршруты API — в api-spec.json (обёртка src/api-spec.ts), оттуда же — серверный
// роутинг, клиентская таблица (public/assets/config.js) и документация (docs/api.md).

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
  /** Watchdog (issue #7): задача в статусе dispatched дольше этого порога без
   *  heartbeat — ненормальное состояние, морда показывает предупреждение.
   *  Медиана старта 8.3 с (ADR 0003), хвост ничем не ограничен — порог щедрый. */
  staleDispatchMs: 30 * 60_000,
} as const;

export const HEARTBEAT = {
  /** Пульс оркестрации: DO сам дёргает workflow_dispatch оркестратора через alarm.
   * GitHub'овский cron на репо не тикает (0 schedule-запусков за 4 часа — измерено),
   * поэтому пульс живёт в мозге, а не снаружи. Alarm будит DO из гибернации
   * и стоит 1 request — комфортный режим Free. */
  selfOrchestrationMs: 15 * 60_000,
  /** Задержка первого пульса после холодного старта объекта. */
  selfOrchestrationFirstMs: 15_000,
  /** Имя workflow оркестратора для workflow_dispatch. */
  orchestraWorkflow: "orchestra.yml",
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
