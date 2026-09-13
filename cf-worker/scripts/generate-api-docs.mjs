#!/usr/bin/env node
// Рендерит производные из api-spec.json — документация API и серверная обёртка
// не пишутся руками, а генерируются из источника правды, поэтому не могут с ним
// разойтись. Запуск: npm run docs (вызывается и из npm run check, который ловит
// устаревание обоих файлов через git diff).

import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const spec = JSON.parse(readFileSync(join(root, "api-spec.json"), "utf8"));

const docLines = [
  "# API edge-harness",
  "",
  "<!-- СГЕНЕРИРОВАНО из cf-worker/api-spec.json командой `npm run docs`. Руками не править. -->",
  "",
  "Все маршруты требуют сессионную куку (браузер, выдаётся `POST /api/session` в обмен на Bearer) или `Authorization: Bearer <HANDS_TOKEN>` (job). Токен в query (`?token=`) отклоняется кодом 400 `query_token_removed`.",
  "Ошибки — JSON `{\"error\": {\"code\", \"message\"}}`, коды стабильны и проверяются тестами.",
  "",
];

for (const route of spec.routes) {
  const methods = route.methods.join("/");
  docLines.push(`## \`${methods} ${route.path}\``, "", route.summary, "");
  if (route.rest) {
    docLines.push(`Остаток пути после \`${route.path}\` — параметр.`, "");
  }
}

writeFileSync(join(root, "..", "docs", "api.md"), docLines.join("\n"));
console.log("docs/api.md сгенерирован");

// Серверная обёртка: раньше была ручной копией JSON без гвардии («два места
// правды») — теперь снимок генерируется отсюда же, и паритет «JSON ≡ TS»
// охраняется git diff'ом в scripts/check-frontend-contract.mjs.
const tsLines = [
  "// СГЕНЕРИРОВАНО из api-spec.json командой `npm run docs`. Руками не править:",
  "// источник правды по маршрутам — api-spec.json, этот файл — его типизированный снимок.",
  `const spec = ${JSON.stringify(spec, null, 2)};`,
  "",
  "// Типизированная обёртка над api-spec.json — единственного места правды по API.",
  "// Роутинг сервера, клиентская таблица (public/assets/config.js), документация",
  "// (docs/api.md) и все проверки строятся из этого файла.",
  "",
  "export interface ApiRoute {",
  "  name: string;",
  "  path: string;",
  "  methods: (\"GET\" | \"POST\" | \"DELETE\")[];",
  "  auth: boolean;",
  "  /** path — префикс маршрута; остаток пути — параметр (например, id задачи). */",
  "  rest?: boolean;",
  "  summary: string;",
  "}",
  "",
  "export const API_PREFIX: string = spec.prefix;",
  "",
  "export const API_SPEC: ApiRoute[] = spec.routes as ApiRoute[];",
  "",
  "/** name → path. Клиентская таблица (assets/config.js) обязана совпадать с этой. */",
  "export const ROUTES: Record<string, string> = Object.fromEntries(",
  "  API_SPEC.map((route) => [route.name, route.path]),",
  ");",
  "",
  "/** Табличный роутинг: точное совпадение method+path, затем rest-маршруты. */",
  "export function matchRoute(method: string, pathname: string): { route: ApiRoute; rest: string } | null {",
  "  for (const route of API_SPEC) {",
  "    if (route.rest) continue;",
  "    if (route.methods.includes(method as \"GET\" | \"POST\" | \"DELETE\") && route.path === pathname) {",
  "      return { route, rest: \"\" };",
  "    }",
  "  }",
  "  for (const route of API_SPEC) {",
  "    if (!route.rest) continue;",
  "    if (route.methods.includes(method as \"GET\" | \"POST\" | \"DELETE\") && pathname.startsWith(route.path)) {",
  "      return { route, rest: decodeURIComponent(pathname.slice(route.path.length)) };",
  "    }",
  "  }",
  "  return null;",
  "}",
  "",
];

writeFileSync(join(root, "src", "api-spec.ts"), tsLines.join("\n"));
console.log("src/api-spec.ts сгенерирован");
