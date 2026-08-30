import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// Тесты гоняются на настоящем рантайме workerd и настоящем SQLite Durable Object.
// HANDS_TOKEN и SESSION_SECRET здесь тестовые; GH_DISPATCH_TOKEN сознательно не
// задаётся — постановка задач обязана отвечать «dispatch не настроен», пока секрета
// нет (fail loud).
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          HANDS_TOKEN: "test-token",
          SESSION_SECRET: "test-session-secret",
        },
      },
    }),
  ],
});
