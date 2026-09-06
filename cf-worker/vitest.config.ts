import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// Тесты гоняются на настоящем рантайме workerd и настоящем SQLite Durable Object.
// HANDS_TOKEN, SESSION_SECRET и TELEGRAM_WEBHOOK_SECRET здесь тестовые;
// GH_DISPATCH_TOKEN и TELEGRAM_BOT_TOKEN сознательно не заданы — постановка задач
// и ответ Telegram обязаны честно отвечать «не настроено», пока секрета нет
// (fail loud); тесты, которым нужен конкретный секрет, ставят его сами (env.X = …)
// и восстанавливают в finally (см. inbox-тесты harness.spec.ts).
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          HANDS_TOKEN: "test-token",
          SESSION_SECRET: "test-session-secret",
          TELEGRAM_WEBHOOK_SECRET: "test-webhook-secret",
        },
      },
    }),
  ],
});
