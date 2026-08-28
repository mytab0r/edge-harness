#!/usr/bin/env node
// Генерирует docs/api.md из api-spec.json — документация API не пишется руками,
// а рендерится из источника правды, поэтому не может разойтись с кодом.
// Запуск: npm run docs (вызывается и из npm run check, который ловит устаревание).

import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const spec = JSON.parse(readFileSync(join(root, "api-spec.json"), "utf8"));

const lines = [
  "# API edge-harness",
  "",
  "<!-- СГЕНЕРИРОВАНО из cf-worker/api-spec.json командой `npm run docs`. Руками не править. -->",
  "",
  "Все маршруты требуют `Authorization: Bearer <HANDS_TOKEN>` (WebSocket — `?token=` в query).",
  "Ошибки — JSON `{\"error\": {\"code\", \"message\"}}`, коды стабильны и проверяются тестами.",
  "",
];

for (const route of spec.routes) {
  const methods = route.methods.join("/");
  lines.push(`## \`${methods} ${route.path}\``, "", route.summary, "");
  if (route.rest) {
    lines.push(`Остаток пути после \`${route.path}\` — параметр.`, "");
  }
}

writeFileSync(join(root, "..", "docs", "api.md"), lines.join("\n"));
console.log("docs/api.md сгенерирован");
