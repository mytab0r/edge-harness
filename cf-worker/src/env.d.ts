// Дополнение к сгенерированному worker-configuration.d.ts: секреты не видны wrangler types,
// потому живут здесь. Имена только здесь — значения только в `wrangler secret put`.
interface Env {
  /** Разделяемый секрет браузера/job'а. Пока не задан — API отвечает 401 на всё. */
  HANDS_TOKEN?: string;
  /** GitHub PAT с Contents:write для repository_dispatch из POST /api/tasks. */
  GH_DISPATCH_TOKEN?: string;
}
