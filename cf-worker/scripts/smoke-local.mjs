#!/usr/bin/env node
// Smoke-проверка воркера на локальном wrangler dev (настоящий workerd, настоящие сокеты).
// Запуск: node scripts/smoke-local.mjs [base-url]
// Требует: запущенный `npm run dev` и .dev.vars с HANDS_TOKEN=dev-token.
// Закрывает то, чего не может vitest-петля: доставку broadcast'а живому сокету.

const BASE = process.argv[2] ?? "http://127.0.0.1:8787";
const TOKEN = process.env.HANDS_TOKEN ?? "dev-token";
const AUTH = { Authorization: `Bearer ${TOKEN}` };

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "  ✓" : "  ✗"} ${name}${detail ? ` — ${detail}` : ""}`);
}

function openSocket(after) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${BASE.replace(/^http/, "ws")}/api/events.live?after=${after}&token=${encodeURIComponent(TOKEN)}`);
    const messages = [];
    ws.addEventListener("message", (event) => messages.push(JSON.parse(event.data)));
    ws.addEventListener("open", () => resolve({ ws, messages }));
    ws.addEventListener("error", () => reject(new Error("сокет не открылся")));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const page = await fetch(`${BASE}/`);
check("страница отдаётся", page.status === 200 && (await page.text()).includes("edge-harness"));

const unauth = await fetch(`${BASE}/api/status`);
check("API без токена — 401", unauth.status === 401, `status=${unauth.status}`);

const { ws, messages } = await openSocket(0);
await sleep(300);
check("WebSocket: hello со статусом", messages[0]?.type === "hello", JSON.stringify(messages[0]?.status?.hands_alive));

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
check("heartbeat: руки живы", heartbeat.status === 200 && hbBody.hands_alive === true);
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

ws.close();

const failed = results.filter((r) => !r.ok);
console.log(failed.length ? `\nSMOKE: ${failed.length} проверок провалено` : "\nSMOKE: всё зелёное");
process.exit(failed.length ? 1 : 0);
