import { OWNER_OBJECT_NAME } from "./config";
import { API_PREFIX } from "./api-spec";

// Один Durable Object с фиксированным именем: мультитенантности нет, владелец один.
// Всё с префикса /api уходит в него; остальное — статика Workers Assets.
// Префикс объявлен в api-spec.json вместе с маршрутами.
export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith(`${API_PREFIX}/`)) {
      const id = env.HARNESS.idFromName(OWNER_OBJECT_NAME);
      return env.HARNESS.get(id).fetch(request);
    }
    return env.ASSETS.fetch(request);
  },

  /**
   * Cron Trigger (issue #693, wrangler.jsonc `triggers.crons`): внешний
   * источник тактов, не зависящий от активности alarm() самого DO — см.
   * докстринг Harness#scheduledTick (cf-worker/src/harness.ts) для полного
   * обоснования (страховка, не второй основной тик; условие дедупликации;
   * граница наблюдаемости отказа). Здесь — только проводка: RPC-вызов
   * стаба (тот же биндинг HARNESS, то же имя объекта, что и у fetch выше)
   * конструирует DO заново, если он был выгружен из памяти, и запускает
   * ровно ту же dispatch-логику, что и alarm() (attemptOrchestraDispatch/
   * fetchLatestOrchestraRunId/confirmPreviousRun/pulseDetailForRecord —
   * одно место правды, не вторая копия).
   */
  async scheduled(_controller, env): Promise<void> {
    const id = env.HARNESS.idFromName(OWNER_OBJECT_NAME);
    try {
      await env.HARNESS.get(id).scheduledTick();
    } catch (error) {
      // Честная граница (см. docstring scheduledTick): если РПЦ-вызов упал
      // ДО того, как scheduledTick() успел что-либо записать в pulse,
      // /api/status не увидит и этого тика вовсе — единственный след здесь,
      // в Cloudflare Logs (`wrangler tail`/дашборд). Живой прогон крона до
      // деплоя этим тестом не проверяется — только прод-форма в cf-worker/test/.
      console.error(`scheduled: RPC scheduledTick упал: ${error instanceof Error ? error.message : error}`);
    }
  },
} satisfies ExportedHandler<Env>;

export { Harness } from "./harness";
