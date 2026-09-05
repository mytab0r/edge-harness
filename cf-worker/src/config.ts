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
  /** Потолок числа автоматизаций (#116). Пул владельца один — десятки правил
   *  это уже аномалия; потолок страхует таблицу и список от неограниченного роста. */
  automationsMax: 50,
  /** Конфиг автоматизации больше этого числа символов отклоняется. */
  automationConfigMaxChars: 8192,
  /** Watchdog (issue #7): задача в статусе dispatched дольше этого порога без
   *  heartbeat — ненормальное состояние, морда показывает предупреждение.
   *  Медиана старта 8.3 с (ADR 0003), хвост ничем не ограничен — порог щедрый. */
  staleDispatchMs: 30 * 60_000,
  /** Inbox: макс. длина текста сообщения. */
  messageMaxChars: 16384,
  /** Inbox: макс. сообщений за один вызов process. */
  messageProcessMax: 100,
  /** Inbox: окно группировки сообщений (мс). */
  messageGroupWindowMs: 5 * 60 * 1000,
  /** Inbox: попыток обработки директивы до честного failed (не настроенный токен
   *  и сеть — повторяемы; после капа сообщение видно в failed и ждёт retry_failed). */
  messageMaxAttempts: 3,
  /** Inbox: сообщение в processing дольше этого порога — изолят умер посреди
   *  внешнего вызова; пульс возвращает его в new (ватчдог по образцу stale_dispatch). */
  messageStuckProcessingMs: 10 * 60_000,
  /** Inbox: таймаут одного вызова GitHub при создании issue. Обязан быть
   *  заведомо меньше messageStuckProcessingMs (гвардится тестом): иначе висящий
   *  fetch доживёт до ретрая другой проходки — двойной issue. */
  messageIssueFetchTimeoutMs: 30_000,
  /** Inbox: сколько сообщений отдаёт список, если лимит не назван. */
  messagesListDefault: 50,
  /** Inbox: потолок одной страницы списка. */
  messagesListMax: 200,
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
  /** #303, находка ревью: dispatch принят (204, attemptOrchestraDispatch
   *  отдаёт detail: null — это НЕ поломка ЭТОГО тика), но подтверждения
   *  запуска ПРЕДЫДУЩЕГО dispatch'а не случилось (run_confirmed: false).
   *  Без этой строки в pulse.detail остаётся null, и морда (как и любой
   *  другой потребитель поля) не может отличить «поломка с текстом причины»
   *  от буквальной строки "null" — ровно тот же класс подмены, что и у
   *  notConfiguredDetail выше. Записывается один раз, в pulseDetailForRecord. */
  runNotConfirmedDetail: "принят, запуск не появился",
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

/** Автоматизации (#116): «триггер → задача агенту → отчёт». Модель и валидация
 *  формы — в src/automations.ts (чистые функции), здесь — проводочные константы. */
export const AUTOMATIONS = {
  /** event_type repository_dispatch, поднимающий job автоматизации
   *  (.github/workflows/automation.yml). Тот же механизм dispatch, что у задач
   *  (GITHUB.dispatchEventType), отдельный тип — у job'а другой контракт. */
  dispatchEventType: "harness-automation",
  /** Префикс task_id прогонов автоматизации в очереди/журнале:
   *  `automation:<id>:<ts>`. По этому же префиксу событие журнала считается
   *  «событием самой автоматизации» и не может ретриггерить journal-триггеры
   *  (гвардия петли). */
  runTaskPrefix: "automation:",
  /** Псевдо-задача журнала для отклонённых webhook-вызовов: попытки без
   *  подписи/с неверной подписью видны в журнале и дашборде, а не только 401'ем. */
  webhookRejectedTaskId: "automation:webhook:rejected",
  /** Имя секрета воркера с HMAC-ключом подписи webhook'ов. Значение живёт
   *  только в секретах (репозиторий публичный), здесь — имя. */
  webhookSecretEnv: "AUTOMATION_WEBHOOK_SECRET",
  /** Заголовок подписи webhook: `sha256=<hex HMAC-SHA256(raw body, secret)>`. */
  webhookSignatureHeader: "X-Harness-Signature",
  /** Минимальная пауза между запусками journal-триггера одной автоматизации.
   *  Работа прогона может сама порождать события журнала с чужими task_id
   *  (kind=pool → job_end воркера под issue-N): без кулдауна такая связка
   *  зацикливалась бы по одному агент-прогону за цикл (ревью #116). Каденс —
   *  как у пульса, которым тикают и расписания. */
  journalCooldownMs: 30 * 60_000,
  /** kind'ы системных событий, которые сам механизм автоматизаций эмитит ПОД
   *  task_id с префиксом runTaskPrefix ("automation:...") — #fireJournalTriggers
   *  исключает такие события из кандидатов гвардией петли ДО сравнения kind,
   *  так что journal-триггер с любым из этих kind никогда не сработает.
   *  Раньше PUT принимал такой конфиг как валидный — silent-wrong, находка
   *  AI-ревью PR #241 (мёртвый триггер без единой ошибки). */
  reservedJournalKinds: [
    "automation_updated",
    "automation_deleted",
    "automation_triggered",
    "automation_dispatched",
    "automation_webhook_rejected",
  ],
} as const;

/** Локаль сообщений API. Словари — в messages.ts. */
export const LOCALE = "ru" as const;
