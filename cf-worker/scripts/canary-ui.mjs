#!/usr/bin/env node
// Канарейка UI: реальный браузер (Playwright) открывает морду с токеном и проверяет,
// что полный цикл входа работает — гейт скрыт, статус отрисован, живой поток подключён.
// Именно этот класс багов не ловят ни API-тесты, ни смок: страница сломана — сервер здоров.
//
// Использование:
//   node scripts/canary-ui.mjs --url http://127.0.0.1:8808 --token dev-token
// Без --url проверяется прод (https://edge-harness.mytab0r.workers.dev).
// Нужен установленный браузер: npx playwright install chromium.

import { chromium } from "playwright";

const args = process.argv.slice(2);
const urlArg = args.includes("--url") ? args[args.indexOf("--url") + 1] : "https://edge-harness.mytab0r.workers.dev";
const token = process.env.HANDS_TOKEN;
if (!token) {
  console.error("canary-ui: нужен HANDS_TOKEN в окружении");
  process.exit(2);
}

const base = urlArg.replace(/\/$/, "");
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const fails = [];
  page.on("pageerror", (error) => fails.push(`JS-ошибка на странице: ${error.message}`));

  // Токен адресной строкой — как задаётся для владельца; из истории он сразу стирается.
  await page.goto(`${base}/?token=${encodeURIComponent(token)}`, { waitUntil: "domcontentloaded" });

  // 1. Главный признак успешного входа: живой поток подключён (до этого момента
  //    должны пройти и /api/status, и replay, и upgrade WebSocket).
  const connOk = await page.evaluate(() => window.EDGE_I18N[window.EDGE_CONFIG.locale]["conn.ok"]);
  let connFailed = await page
    .waitForFunction(
      (expected) => document.getElementById("conn")?.textContent === expected,
      connOk,
      { timeout: 20000 },
    )
    .then(() => null)
    .catch(() => "живой поток не подключился");
  if (connFailed) {
    // Диагностика на месте: что написал гейт и какой статус у бейджа рук.
    const state = await page.evaluate(() => ({
      gateHidden: document.getElementById("gate")?.hidden,
      gateError: document.getElementById("gate-error")?.textContent,
      hands: document.getElementById("hands")?.textContent,
      conn: document.getElementById("conn")?.textContent,
    }));
    console.error(`  ✗ ${connFailed}; состояние страницы: ${JSON.stringify(state)}`);
  }
  if (connFailed) fails.push(connFailed);

  // 2. Статус рук отрисован реальным текстом локализации (а не «…»).
  const ok = await page.evaluate(() => ({
    gone: window.EDGE_I18N[window.EDGE_CONFIG.locale]["hands.gone"],
    alive: window.EDGE_I18N[window.EDGE_CONFIG.locale]["hands.alive"],
  }));
  const badgeOk = await page.waitForFunction(
    ([gone, alivePrefix]) => {
      const text = document.getElementById("hands")?.textContent ?? "";
      return text === gone || text.startsWith(alivePrefix.split("{")[0]);
    },
    [ok.gone, ok.alive],
    { timeout: 5000 },
  ).then(() => true).catch(() => false);
  if (!badgeOk) fails.push("бейдж статуса рук не отрисован");

  // 3. Гейт остался скрытым после полного цикла (порядок важен: сразу после клика
  //    он скрыт всегда, а вот после неудачного входа появляется снова).
  const gateHidden = await page.evaluate(() => document.getElementById("gate")?.hidden === true);
  if (!gateHidden) {
    const gateError = await page.evaluate(() => document.getElementById("gate-error")?.textContent);
    fails.push(`гейт виден: ${gateError}`);
  }

  // 4. Никаких обращений к несуществующим маршрутам (класс «/api/api/*»).
  const badRequests = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/api/")) badRequests.push(response.url());
  });

  if (fails.length || badRequests.length) {
    for (const message of [...fails, ...badRequests]) console.error(`  ✗ ${message}`);
    console.error("canary-ui: FAIL");
    process.exit(1);
  }
  const hands = await page.evaluate(() => document.getElementById("hands").textContent);
  console.log(`canary-ui: OK — вход прошёл, статус «${hands}», живой поток подключён`);
} finally {
  await browser.close();
}
