// Единственное место правды констант пульс-воркера. Порт из cf-worker/src/config.ts
// (задача #86): списываемый воркер держал те же значения, смена поведения — правка
// здесь и в тестах, не в двух местах.

/** Пульс оркестрации: DO сам дёргает workflow_dispatch оркестратора через alarm.
 *  GitHub'овский cron на репо не тикает/деградирует (замеры в #73 и #269), поэтому
 *  пульс живёт в alarm-цепочке DO, а не снаружи. Alarm будит DO из гибернации
 *  и стоит 1 request — комфортный режим Free (docs/research/20). */
export const PULSE = {
  /** Интервал тика. */
  intervalMs: 15 * 60_000,
  /** Задержка первого тика после холодного старта объекта (его будит канарейка деплоя). */
  firstMs: 15_000,
} as const;

/** Самообновление морды dsh-edge (#73): пульс сверяет версию, которую отдаёт
 *  публичный /api/health морды, с последней стабильной в npm. Расхождение при
 *  истёкшем троттле → workflow_dispatch деплой-воркфлоу. Ожидание сети в
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
  userAgent: "harness-pulse",
  /** Имя workflow оркестратора для workflow_dispatch. */
  orchestraWorkflow: "orchestra.yml",
} as const;

/** Имя единственного объекта. Мультитенантности нет, владелец один. */
export const OWNER_OBJECT_NAME = "owner";
