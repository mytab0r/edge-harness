import { OWNER_OBJECT_NAME } from "./harness";

// Один Durable Object с фиксированным именем: мультитенантности нет, владелец один.
// Вся /api/* уходит в него; остальное — статика Workers Assets.
export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      const id = env.HARNESS.idFromName(OWNER_OBJECT_NAME);
      return env.HARNESS.get(id).fetch(request);
    }
    return env.ASSETS.fetch(request);
  },
} satisfies ExportedHandler<Env>;

export { Harness } from "./harness";
