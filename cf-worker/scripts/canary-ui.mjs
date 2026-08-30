#!/usr/bin/env node
// Канарейка UI: реальный браузер (Playwright) входит в морду как владелец — через
// гейт, обменом HANDS_TOKEN на сессионную куку — и проверяет полный цикл: гейт
// скрыт, кука HttpOnly+SameSite=Strict, токен не гуляет по URL, статус отрисован,
// живой поток подключён. Именно этот класс багов не ловят ни API-тесты, ни смок:
// страница сломана — сервер здоров.
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

  // Критерий задачи #5: ни один запрос к /api/* не несёт токен в URL. Слушатели
  // ставятся ДО входа, чтобы поймать в том числе запросы логина.
  const apiRequests = [];
  page.on("request", (request) => {
    if (request.url().includes("/api")) apiRequests.push(request.url());
  });
  // Атрибуты куки берём с провода (Set-Cookie ответа обмена): CDP-отчёт cookie jar
  // на http://localhost не показывает Secure-куку, а заголовок показывает всегда.
  let sessionSetCookie = "";
  page.on("response", (response) => {
    if (response.url().includes("/api/session")) {
      // set-cookie в headers() не попадает; allHeaders() склеивает несколько
      // Set-Cookie переводом строки — достаточно.
      response.allHeaders().then((headers) => { sessionSetCookie = headers["set-cookie"] ?? ""; }).catch(() => {});
    }
  });

  // Вход как у владельца: без ?token= в адресной строке (этот путь выпилен вместе
  // с query-токеном), а через гейт — обмен токена на сессионную куку.
  await page.goto(`${base}/`, { waitUntil: "domcontentloaded" });
  await page.fill("#gate-token", token);
  await page.click("#gate-enter");

  // 1. Главный признак успешного входа: живой поток подключён (до этого момента
  //    должны пройти и обмен на куку, и /api/status, и replay, и upgrade WebSocket).
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

  // 2. Кука сессии выдана и невидима JS: HttpOnly, SameSite=Strict, Secure (dsh-edge).
  if (!sessionSetCookie.includes("harness_session=")) {
    fails.push("обмен /api/session не выдал куку harness_session");
  } else {
    const need = ["HttpOnly", "SameSite=Strict", "Secure"];
    for (const attribute of need) {
      if (!sessionSetCookie.includes(attribute)) fails.push(`в Set-Cookie нет ${attribute}: ${sessionSetCookie}`);
    }
  }

  // 3. Токен нигде не в URL (критерий #5) — ни в одном запросе к /api, ни в адресе.
  const tokenInUrl = apiRequests.filter((url) => url.includes("token="));
  if (tokenInUrl.length) fails.push(`токен в URL запросов: ${tokenInUrl.join(", ")}`);
  if (page.url().includes("token=")) fails.push(`токен в адресе страницы: ${page.url()}`);

  // 4. Статус рук отрисован реальным текстом локализации (а не «…»).
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

  // 5. Гейт остался скрытым после полного цикла (порядок важен: сразу после клика
  //    он скрыт всегда, а вот после неудачного входа появляется снова).
  const gateHidden = await page.evaluate(() => document.getElementById("gate")?.hidden === true);
  if (!gateHidden) {
    const gateError = await page.evaluate(() => document.getElementById("gate-error")?.textContent);
    fails.push(`гейт виден: ${gateError}`);
  }

  // 6. Никаких обращений к несуществующим маршрутам (класс «/api/api/*»).
  const badRequests = apiRequests.filter((url) => url.includes("/api/api/"));
  if (badRequests.length) fails.push(`обращения к несуществующим маршрутам: ${badRequests.join(", ")}`);

  if (fails.length) {
    for (const message of fails) console.error(`  ✗ ${message}`);
    console.error("canary-ui: FAIL");
    process.exit(1);
  }
  const hands = await page.evaluate(() => document.getElementById("hands").textContent);
  console.log(
    `canary-ui: OK — вход через обмен на куку прошёл (HttpOnly, SameSite=Strict, Secure), ` +
    `токена в URL нет, статус «${hands}», живой поток подключён`,
  );
} finally {
  await browser.close();
}
