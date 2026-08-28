#!/usr/bin/env node
// Гвардия контракта фронтенда: пути и локализационные ключи не должны разъезжаться
// между сервером (src/config.ts) и клиентом (public/assets/*). Запуск: npm run check.
// Нарушение паритета — тот же silent-wrong: страница молча начинает звать несуществующий
// маршрут или показывать сырые ключи вместо текста.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const fail = (message) => {
  console.error(`check-frontend-contract: FAIL — ${message}`);
  process.exit(1);
};

// 1. Пути: ROUTES из config.ts против window.EDGE_CONFIG.routes из assets/config.js
const configTs = readFileSync(join(root, "src/config.ts"), "utf8");
const clientConfig = readFileSync(join(root, "public/assets/config.js"), "utf8");

const serverRoutes = {};
for (const match of configTs.matchAll(/(\w+):\s*"(\/api[^"]*)"/g)) {
  serverRoutes[match[1]] = match[2];
}
const clientRoutes = {};
const routesBlock = clientConfig.match(/routes:\s*\{([^}]*)\}/s);
if (!routesBlock) fail("в public/assets/config.js не найден блок routes");
for (const match of routesBlock[1].matchAll(/(\w+):\s*"(\/api[^"]*)"/g)) {
  clientRoutes[match[1]] = match[2];
}

for (const [key, value] of Object.entries(serverRoutes)) {
  if (!(key in clientRoutes)) fail(`путь ${key} (${value}) есть в config.ts, но отсутствует в assets/config.js`);
  if (clientRoutes[key] !== value) {
    fail(`путь ${key} разошёлся: сервер "${value}", клиент "${clientRoutes[key]}"`);
  }
}
for (const key of Object.keys(clientRoutes)) {
  if (!(key in serverRoutes)) fail(`путь ${key} есть в assets/config.js, но отсутствует в config.ts`);
}
if (!Object.keys(serverRoutes).length) fail("в config.ts не найдено ни одного маршрута — изменился формат?");

// 2. Локализация: каждый t("key") из app.js и каждый data-i18n из index.html
//    должны быть в каждом словаре i18n/*.js
const appJs = readFileSync(join(root, "public/assets/app.js"), "utf8");
const indexHtml = readFileSync(join(root, "public/index.html"), "utf8");
const usedKeys = [
  ...appJs.matchAll(/\bt\("([\w.]+)"/g)].map((m) => m[1]);
usedKeys.push(...[...indexHtml.matchAll(/data-i18n(?:-attr)?="(?:[\w-]+:)?([\w.]+)"/g)].map((m) => m[1]));
const usedKeysUnique = [...new Set(usedKeys)];
if (!usedKeysUnique.length) fail("не найдено ни одного ключа локализации (t(\"...\") или data-i18n) — изменился формат?");

const { readdirSync } = await import("node:fs");
const i18nDir = join(root, "public/assets/i18n");
for (const file of readdirSync(i18nDir).filter((name) => name.endsWith(".js"))) {
  const dict = readFileSync(join(i18nDir, file), "utf8");
  const defined = new Set([...dict.matchAll(/"([\w.]+)":\s*"/g)].map((m) => m[1]));
  for (const key of usedKeys) {
    if (!defined.has(key)) fail(`ключ "${key}" используется в app.js, но отсутствует в i18n/${file}`);
  }
  for (const key of defined) {
    if (!usedKeysUnique.includes(key)) console.warn(`warn: ключ "${key}" определён в i18n/${file}, но не используется`);
  }
}

console.log(`check-frontend-contract: OK (${Object.keys(serverRoutes).length} маршрутов, ${usedKeysUnique.length} ключей локализации)`);
