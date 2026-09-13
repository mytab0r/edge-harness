import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// Тесты гоняются на настоящем рантайме workerd и настоящем SQLite Durable Object.
// HANDS_TOKEN, SESSION_SECRET, TELEGRAM_WEBHOOK_SECRET и TELEGRAM_CHAT_ID здесь
// тестовые; GH_DISPATCH_TOKEN и TELEGRAM_BOT_TOKEN сознательно не заданы —
// постановка задач и ответ Telegram обязаны честно отвечать «не настроено», пока
// секрета нет (fail loud); тесты, которым нужен конкретный секрет, ставят его
// сами (env.X = …) и восстанавливают в finally (см. inbox-тесты harness.spec.ts).
// TELEGRAM_CHAT_ID — chat_id «владельца» по умолчанию для webhook-тестов
// (находка ревью PR #486): то же значение -1001234567890, что уже используют
// фикстуры message/callback апдейтов в harness.spec.ts, — один канонический
// «свой» chat вместо рассинхрона строкового/числового представления.
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          HANDS_TOKEN: "test-token",
          SESSION_SECRET: "test-session-secret",
          TELEGRAM_WEBHOOK_SECRET: "test-webhook-secret",
          TELEGRAM_CHAT_ID: "-1001234567890",
        },
      },
    }),
  ],
});
