// Типы окружения воркера (только документация контракта: бандл wrangler типы
// снимает, tsc в CI не запускается). Значения приходят из Secrets воркера —
// их синхронизирует deploy-pulse.yml.
interface Env {
  /** Binding единственного DO (wrangler.jsonc). */
  PULSE: DurableObjectNamespace;
  /** Узкий fine-grained PAT (Contents+Actions на этот репозиторий, ADR 0008):
   *  workflow_dispatch orchestra.yml и deploy-dsh-edge.yml. */
  GH_DISPATCH_TOKEN?: string;
  /** Репозиторий пула (owner/repo), vars репозитория → var воркера. */
  GH_REPO?: string;
}
