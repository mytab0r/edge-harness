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

/** Сессия браузера: подписанная кука вместо долгоживущего HANDS_TOKEN в query/JS
 *  (по образцу dsh-edge, docs/research/11: обмен секрета на подписанную куку). */
export const SESSION = {
  /** Имя куки. HttpOnly — JS её не читает, Secure — только по https (localhost
   *  браузеры считают trustworthy и принимают Secure-куку по http). */
  cookieName: "harness_session",
  /** TTL сессии браузера. dsh-edge держит 30 дней — тот же порядок. */
  ttlMs: 30 * 24 * 60 * 60 * 1000,
} as const;

export const HEARTBEAT = {
  /** Пульс оркестрации: DO сам дёргает workflow_dispatch оркестратора через alarm.
   * GitHub'овский `schedule` на этом репозитории доставляется лишь в ~7% тиков
   * (замер 116.3 ч, 31 из ~465 ожидаемых — docs/research/21-github-actions.md,
   * «Замер schedule на этом репозитории»), а не «не тикает вовсе» — поэтому
   * пульс живёт в мозге, а не только снаружи. Alarm будит DO из гибернации
   * и стоит 1 request — комфортный режим Free. */
  selfOrchestrationMs: 15 * 60_000,
  /** Задержка первого пульса после холодного старта объекта. */
  selfOrchestrationFirstMs: 15_000,
  /** Имя workflow оркестратора для workflow_dispatch. */
  orchestraWorkflow: "orchestra.yml",
  /** Пульс оркестрации (#269): единственное место правды для сентинела
   *  «возможности нет» (секреты не заданы) — записывается в pulse.detail и
   *  читается pulseHealthy(). Одна константа вместо двух копий строки —
   *  переименование в одном месте не должно молча ломать логику здоровья. */
  notConfiguredDetail: "not_configured",
} as const;

/** Самообновление морды dsh-edge (#73): пульс сверяет версию, которую отдаёт
 *  публичный /api/health морды, с последней стабильной в npm. Расхождение при
 *  истёкшем троттле → workflow_dispatch деплой-воркфлоу. GitHub'овский `schedule`
 *  на этом репозитории доставляется лишь в единицах процентов тиков (см.
 *  HEARTBEAT выше, замер docs/research/21), поэтому проверка живёт в том же
 *  DO-пульсе, что и оркестрация, а не только на cron. Ожидание сети в
 *  CPU-лимит не считается — fetch+compare+dispatch укладывается в 10 ms. */
export const DSH_EDGE_UPDATE = {
  /** Публичный health морды: отдаёт deployed version без авторизации. */
  healthUrl: "https://dsh-edge.mytab0r.workers.dev/api/health",
  /** latest стабильная версия пакета в npm. */
  registryUrl: "https://registry.npmjs.org/dsh-edge/latest",
  /** workflow_dispatch этого воркфлоу при расхождении версий. */
  workflow: "deploy-dsh-edge.yml",
  /** Минимальная пауза между попытками диспетча: npm релизится несколько раз в
   *  сутки, а деплой может падать по внешним причинам — штурмовать нельзя. */
  throttleMs: 4 * 60 * 60 * 1000,
  /** Ключ записи storage с временем последней попытки диспетча. */
  lastAttemptKey: "dsh-edge-update:last-dispatch-ts",
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
