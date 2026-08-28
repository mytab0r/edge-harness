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
} satisfies ExportedHandler<Env>;

export { Harness } from "./harness";
