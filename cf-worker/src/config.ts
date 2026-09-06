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

/** Инлайн-кнопки решения владельца в Telegram (#254). callback_data ограничен
 *  64 байтами (лимит Bot API) — формат `{callbackPrefix}:<issue>:<option>`
 *  укладывается с большим запасом даже при 10-значном номере issue и
 *  двузначном номере варианта (не подтверждено больше пары цифр вариантов —
 *  UI не предполагает десятки кнопок в одном сообщении). */
export const TELEGRAM = {
  apiBase: "https://api.telegram.org",
  /** Заголовок вебхука Telegram (`setWebhook(secret_token=...)`), которым
   *  морда отличает настоящий апдейт от чужого POST на тот же путь. */
  webhookSecretHeader: "X-Telegram-Bot-Api-Secret-Token",
  /** Префикс callback_data — не про секретность, а разбор формата. */
  callbackPrefix: "wo",
  /** event_type repository_dispatch, которым решение владельца уходит в
   *  тонкий job (только issues:write, .github/workflows/owner-decision.yml) —
   *  НЕ переиспользует GITHUB.dispatchEventType: тот поднимает полноценный
   *  DSH-джоб hands.yml, здесь только один комментарий в issue. */
  ownerDecisionDispatchType: "owner-decision",
  /** Первая строка комментария — единственный формат, который (по решению
   *  #470/#471) снимает метку waiting:owner; кнопка производит тот же
   *  артефакт, что и ручной ответ владельца, второй способ применения не
   *  заводится. */
  decisionCommentPrefix: "РЕШЕНИЕ",
} as const;

/** Ретеншн DO SQLite (#306/#305): без него `events`/`tasks` растут вечно, и
 *  любой скан со временем дорожает — тот же класс, что подпалил суточную квоту
 *  rows_read (#320, docs/research/20-cloudflare-free.md, раздел «Инцидент:
 *  rows_read исчерпан на живом аккаунте (#320)»). Политика разная для каждой
 *  таблицы — см. proposal.md change'а do-sqlite-retention. */
export const RETENTION = {
  /** Сколько строк снимает один прогон ОДНОЙ таблицы за alarm-тик — пачка по
   *  индексу, не полный скан (см. RETENTION_TABLES в harness.ts). Число не
   *  привязано к объёму истории: 500 строк — доли миллисекунды CPU что на
   *  пустой, что на огромной таблице, потому что фильтруются по индексу. */
  batchSize: 500,
  /** Журнал событий: возраст, БЕЗ разбора по статусу задачи. Для РАБОТАЮЩЕГО
   *  job'а это безопасно: он физически не может идти дольше 6 ч (лимит job'а
   *  GitHub Actions — таблица в AGENTS.md), 14 суток не заденут ни одно ещё
   *  формирующееся событие. Но это НЕ гарантия для вечно-незавершённой задачи
   *  (находка ревью PR #329): `queued` без GH_DISPATCH_TOKEN/GH_REPO не
   *  трогается никем (сам прод это поддерживает), а зависший `dispatched`
   *  систему сама не лечит (п.14.2 спеки, watchdog только считает
   *  `stale_dispatch`) — переход в `failed` делает только обработчик
   *  `job_end`. У такой задачи `task_queued`/`task_dispatched` сгорят при
   *  живой строке `tasks`, если её события старше порога — объявленная
   *  потеря журнала, не «гарантированно не заденет». Ценность журнала —
   *  отладка конкретного прогона и replay; после недель разбора никто к
   *  нему не возвращается. */
  eventsMaxAgeMs: 14 * 24 * 60 * 60 * 1000,
  /** Задачи: только терминальные статусы (`done`/`failed`) старше порога —
   *  `queued`/`dispatched`/`running` не трогаем ни при каком возрасте: зависшая
   *  задача — забота watchdog'а (LIMITS.staleDispatchMs), не ретеншена, и
   *  тихо терять данные о том, что реально происходит с активной задачей,
   *  нельзя. 30 суток дольше, чем у events, — строка задачи дешевле (5 колонок
   *  против журнала), а данные питают анализ latency (ADR 0003). */
  tasksMaxAgeMs: 30 * 24 * 60 * 60 * 1000,
  /** Сколько подряд тиков с полной пачкой (или ошибкой чистки) на любой
   *  таблице считается «не успеваем», а не разовый всплеск после отката/
   *  первого деплоя на большом бэклоге. 4 тика × HEARTBEAT.selfOrchestrationMs
   *  (15 мин) = час устойчивого отставания. */
  backlogStreakThreshold: 4,
} as const;

/** Имя единственного объекта. Мультитенантности нет, владелец один. */
export const OWNER_OBJECT_NAME = "owner";

/** Локаль сообщений API. Словари — в messages.ts. */
export const LOCALE = "ru" as const;
