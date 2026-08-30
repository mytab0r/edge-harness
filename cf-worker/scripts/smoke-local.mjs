#!/usr/bin/env node
// Smoke-проверка воркера на локальном wrangler dev (настоящий workerd, настоящие сокеты).
// Запуск: node scripts/smoke-local.mjs [base-url]
// Требует: запущенный `npm run dev` и .dev.vars с HANDS_TOKEN и SESSION_SECRET.
// Закрывает то, чего не может vitest-петля: доставку broadcast'а живому сокету.
// Вход браузера — обмен токена на сессионную куку; WS-клиент здесь ходит кукой
// (пакет ws умеет заголовки, браузерный API — нет).

import { WebSocket } from "ws";

const BASE = process.argv[2] ?? "http://127.0.0.1:8787";
const TOKEN = process.env.HANDS_TOKEN ?? "dev-token";
const AUTH = { Authorization: `Bearer ${TOKEN}` };

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  ✓" : "  ✗"} ${name}${detail ? ` — ${detail}` : ""}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function openSocket(after, headers = {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${BASE.replace(/^http/, "ws")}/api/events.live?after=${after}`, { headers });
    const messages = [];
    ws.addEventListener("message", (event) => messages.push(JSON.parse(event.data)));
    // ws шлёт в error-слушатель и сырую ошибку, и обёрнутое событие — берём что есть.
    ws.addEventListener("error", (error) => {
      reject(new Error(`сокет не открылся: ${error?.message ?? error?.error?.message ?? "причина неизвестна"}`));
    });
    ws.addEventListener("open", () => resolve({ ws, messages }));
  });
}

const page = await fetch(`${BASE}/`);
check("страница отдаётся", page.status === 200 && (await page.text()).includes("edge-harness"));

const unauth = await fetch(`${BASE}/api/status`);
check("API без входа — 401", unauth.status === 401, `status=${unauth.status}`);

// Громкий отказ классу «токен в URL»: даже совпадающий по значению токен в query
// отклоняется отдельным кодом, а не принимается молча.
const queryToken = await fetch(`${BASE}/api/status?token=${encodeURIComponent(TOKEN)}`, { headers: AUTH });
const queryTokenBody = await queryToken.json().catch(() => null);
check(
  "?token= отклонён громко (400 query_token_removed)",
  queryToken.status === 400 && queryTokenBody?.error?.code === "query_token_removed",
  `status=${queryToken.status}, code=${queryTokenBody?.error?.code}`,
);

// Обмен Bearer-токена на сессионную куку — путь браузера.
const login = await fetch(`${BASE}/api/session`, { method: "POST", headers: AUTH });
const setCookie = login.headers.get("set-cookie") ?? "";
const cookiePair = setCookie.split(";")[0];
check(
  "POST /api/session выдал куку HttpOnly SameSite=Strict Secure",
  login.status === 200 &&
    cookiePair.startsWith("harness_session=") &&
    /httponly/i.test(setCookie) &&
    /samesite=strict/i.test(setCookie) &&
    /secure/i.test(setCookie) &&
    Number(setCookie.match(/Max-Age=(\d+)/)?.[1] ?? 0) > 0,
  setCookie ? setCookie.split(";").map((s) => s.trim()).slice(1).join(", ") : "set-cookie пуст",
);
const badLogin = await fetch(`${BASE}/api/session`, { method: "POST", headers: { Authorization: "Bearer wrong" } });
check("обмен с чужим токеном — 401", badLogin.status === 401, `status=${badLogin.status}`);

const withCookie = await fetch(`${BASE}/api/status`, { headers: { Cookie: cookiePair } });
check("API по куке без Bearer — 200", withCookie.status === 200, `status=${withCookie.status}`);

const { ws, messages } = await openSocket(0, { Cookie: cookiePair });
await sleep(300);
check("WebSocket по куке: hello со статусом", messages[0]?.type === "hello", JSON.stringify(messages[0]?.status?.hands_alive));

let querySocketClosed = false;
try {
  const bad = await openSocket(0, {});
  await sleep(300);
  bad.ws.close();
} catch {
  querySocketClosed = true;
}
check("WebSocket без куки не открывается", querySocketClosed);

const taskId = crypto.randomUUID();
await fetch(`${BASE}/api/events`, {
  method: "POST",
  headers: { ...AUTH, "content-type": "application/json" },
  body: JSON.stringify({ task_id: taskId, events: [{ seq: 1, kind: "smoke_probe", data: { n: 1 } }] }),
});
await sleep(500);
const gotEvent = messages.find((m) => m.type === "event" && m.event?.kind === "smoke_probe");
check("broadcast: POST события дошёл в открытый сокет", Boolean(gotEvent));

const heartbeat = await fetch(`${BASE}/api/heartbeat`, {
  method: "POST",
  headers: { ...AUTH, "content-type": "application/json" },
  body: JSON.stringify({ job_id: "smoke", task_id: taskId }),
});
const hbBody = await heartbeat.json();
check("heartbeat: Bearer для job работает, руки живы", heartbeat.status === 200 && hbBody.hands_alive === true);
await sleep(300);
const statusPush = messages.filter((m) => m.type === "status").at(-1);
check("broadcast: статус «руки живы» пришёл в сокет", statusPush?.status?.hands_alive === true);

const task = await fetch(`${BASE}/api/tasks`, {
  method: "POST",
  headers: { ...AUTH, "content-type": "application/json" },
  body: JSON.stringify({ payload: null }),
});
const taskBody = await task.json();
check(
  "задача без GH_DISPATCH_TOKEN — «не настроен», не поломка",
  task.status === 201 && taskBody.dispatch === "not_configured",
  `task_id=${taskBody.task_id?.slice(0, 8)}…`,
);

const logout = await fetch(`${BASE}/api/session`, { method: "DELETE", headers: { Cookie: cookiePair } });
const logoutCookie = (logout.headers.get("set-cookie") ?? "").split(";").map((s) => s.trim().toLowerCase());
check(
  "DELETE /api/session отвечает кукой с Max-Age=0 (выбрасывает браузер)",
  logout.status === 200 && logoutCookie.some((s) => s === "max-age=0"),
);

ws.close();

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\nSMOKE: ${failed.length} проверок провалено` : "\nSMOKE: всё зелёное");
process.exit(failed.length ? 1 : 0);
