#!/usr/bin/env node
// Гвардия контракта API и фронтенда. Запуск: npm run check. Нарушение — красный CI.
// Закрывает класс ошибки «двойной /api/api/*»: путь как строка имеет право жить
// ровно в api-spec.json (источник) и public/assets/config.js (клиентская таблица);
// код страницы обращается к маршрутам только ключами route("name").
//   1. Клиентская таблица ≡ спеке API (имя → путь, без лишних и без пропавших).
//   2. В app.js нет ни одного литерала, начинающегося с /api — путь строится
//      только таблицей; склеивать префиксы физически не с чем.
//   3. Каждый route("ключ") в app.js существует в таблице.
//   4. Каждый ключ локализации t("…") и data-i18n из разметки есть в словарях i18n/*.
//   5. docs/api.md сгенерирован из текущей спеки (не устарел).

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { execSync } from "node:child_process";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

const fail = (message) => {
  console.error(`check-frontend-contract: FAIL — ${message}`);
  process.exit(1);
};

const spec = JSON.parse(readFileSync(join(root, "api-spec.json"), "utf8"));
const specTable = Object.fromEntries(spec.routes.map((route) => [route.name, route.path]));
if (!Object.keys(specTable).length) fail("api-spec.json не содержит маршрутов");

// 1. Клиентская таблица ≡ спеке
const clientConfig = readFileSync(join(root, "public/assets/config.js"), "utf8");
const routesBlock = clientConfig.match(/routes:\s*\{([^}]*)\}/s);
if (!routesBlock) fail("в public/assets/config.js не найден блок routes");
const clientRoutes = {};
for (const match of routesBlock[1].matchAll(/(\w+):\s*"([^"]+)"/g)) {
  clientRoutes[match[1]] = match[2];
}
for (const [name, path] of Object.entries(specTable)) {
  if (clientRoutes[name] !== path) {
    fail(`маршрут ${name}: в спеке "${path}", в assets/config.js "${clientRoutes[name] ?? "нет"}"`);
  }
}
for (const name of Object.keys(clientRoutes)) {
  if (!(name in specTable)) fail(`маршрут ${name} есть в assets/config.js, но отсутствует в api-spec.json`);
}

// 2. В коде страницы нет path-литералов — путь существует только в таблице
const appJs = readFileSync(join(root, "public/assets/app.js"), "utf8");
const pathLiterals = [...appJs.matchAll(/["'`](\/api[^"'`]*)["'`]/g)].map((m) => m[1]);
if (pathLiterals.length) {
  fail(`в app.js найдены литералы путей (${[...new Set(pathLiterals)].join(", ")}) — используй route("имя")`);
}

// 3. Ключи маршрутов из app.js существуют в таблице
const usedRouteKeys = [...appJs.matchAll(/\broute(?:Q)?\("(\w+)"/g)].map((m) => m[1]);
if (!usedRouteKeys.length) fail("в app.js не найдено обращений route(\"...\") — изменился формат?");
for (const key of usedRouteKeys) {
  if (!(key in specTable)) fail(`route("${key}") в app.js, но такого маршрута нет в спеке`);
}

// 4. Локализация: t("key") из app.js и data-i18n из index.html есть в каждом словаре
const indexHtml = readFileSync(join(root, "public/index.html"), "utf8");
const jsKeys = [...appJs.matchAll(/\bt\("([\w.]+)"/g)].map((m) => m[1]);
const htmlKeys = [...indexHtml.matchAll(/data-i18n(?:-attr)?="(?:[\w-]+:)?([\w.]+)"/g)].map((m) => m[1]);
const usedKeys = [...new Set([...jsKeys, ...htmlKeys])];
if (!usedKeys.length) fail("не найдено ни одного ключа локализации");

const i18nDir = join(root, "public/assets/i18n");
for (const file of readdirSync(i18nDir).filter((name) => name.endsWith(".js"))) {
  const dict = readFileSync(join(i18nDir, file), "utf8");
  const defined = new Set([...dict.matchAll(/"([\w.]+)":\s*"/g)].map((m) => m[1]));
  for (const key of usedKeys) {
    if (!defined.has(key)) fail(`ключ "${key}" используется, но отсутствует в i18n/${file}`);
  }
  for (const key of defined) {
    if (!usedKeys.includes(key)) console.warn(`warn: ключ "${key}" определён в i18n/${file}, но не используется`);
  }
}

// 5. Производные спеки не устарели (docs/api.md и серверный снимок src/api-spec.ts)
execSync("node scripts/generate-api-docs.mjs", { cwd: root, stdio: "pipe" });
const generatedDiff = execSync("git diff --stat -- docs/api.md src/api-spec.ts", { cwd: root }).toString().trim();
if (generatedDiff) fail("docs/api.md или src/api-spec.ts устарел — запусти npm run docs и закоммить");

console.log(`check-frontend-contract: OK (${Object.keys(specTable).length} маршрутов, ${usedKeys.length} ключей локализации)`);
