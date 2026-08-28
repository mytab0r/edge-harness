/* edge-harness: клиент морды. Живёт на статике Workers Assets, говорит с Durable Object
   через /api (пути — window.EDGE_CONFIG, тексты — window.EDGE_I18N). */

"use strict";

const CONFIG = window.EDGE_CONFIG;
const t = (key, params) => {
  let text = (window.EDGE_I18N[CONFIG.locale] || {})[key] || key;
  if (params) text = text.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
  return text;
};

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "edge-harness-token";

let token = localStorage.getItem(TOKEN_KEY) || "";
let lastEventId = 0; // курсор журнала: и для replay, и для after у WebSocket
let socket = null;
let socketRecycleTimer = null;
let reconnectDelay = CONFIG.reconnectBaseMs;

const api = (path, options = {}) => {
  const url = `${CONFIG.routes.apiPrefix}${path}`;
  return fetch(url, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      Authorization: `Bearer ${token}`,
      ...options.headers,
    },
  });
};

async function apiJson(path, options) {
  const res = await api(path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const message = body?.error?.message || `HTTP ${res.status}`;
    $("tasks").textContent = t("task.error", { detail: message });
    const error = new Error(message);
    error.status = res.status;
    throw error;
  }
  return body;
}

// ── Журнал ────────────────────────────────────────────────────────────────────────

function renderEvent(event) {
  const li = document.createElement("li");
  const data = event.data === null || event.data === undefined ? "" : JSON.stringify(event.data);
  li.innerHTML =
    `<span class="ts"></span><span class="kind"></span><span class="data"></span>`;
  li.querySelector(".ts").textContent = new Date(event.ts).toLocaleTimeString(CONFIG.locale);
  li.querySelector(".kind").textContent = `${event.kind} #${event.seq}`;
  li.querySelector(".kind").classList.add(event.source);
  li.querySelector(".data").textContent = data;
  const journal = $("journal");
  journal.prepend(li);
  while (journal.children.length > CONFIG.journalMaxRows) journal.lastChild.remove();
}

async function replay() {
  // Догоняем журнал страницами до последнего известного события.
  for (;;) {
    const body = await apiJson(`${CONFIG.routes.events}?after=${lastEventId}&limit=${CONFIG.replayPageSize}`);
    for (const event of body.events) {
      renderEvent(event);
      lastEventId = Math.max(lastEventId, event.id);
    }
    if (!body.has_more) return;
  }
}

// ── Статус ────────────────────────────────────────────────────────────────────────

function renderStatus(status) {
  const hands = $("hands");
  if (status.hands_alive) {
    const age = Math.round((status.now - status.last_heartbeat.ts) / 1000);
    hands.textContent = t("hands.alive", { age, job: status.last_heartbeat.job_id });
    hands.className = "badge ok";
  } else {
    hands.textContent = t("hands.gone");
    hands.className = "badge bad";
  }
  const s = status.tasks;
  $("queue").textContent = t("queue.line", { queued: s.queued, running: s.running, done: s.done, failed: s.failed });
  $("queue").className = "badge" + (s.failed ? " warn" : "");
}

// ── Живой поток ───────────────────────────────────────────────────────────────────

function stopSocketTimers() {
  if (socketRecycleTimer) { clearTimeout(socketRecycleTimer); socketRecycleTimer = null; }
}

function scheduleReconnect() {
  const seconds = Math.round(reconnectDelay / 1000);
  $("conn").textContent = t("conn.reconnect", { seconds });
  setTimeout(connectWebSocket, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, CONFIG.reconnectMaxMs);
}

function connectWebSocket() {
  if (socket) { socket.onclose = null; socket.close(); }
  stopSocketTimers();
  $("conn").textContent = t("conn.connecting");

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const route = `${CONFIG.routes.eventsLive}?after=${lastEventId}&token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(`${proto}//${location.host}${route}`);
  socket = ws;

  ws.onopen = () => {
    reconnectDelay = CONFIG.reconnectBaseMs;
    $("conn").textContent = t("conn.ok");
    // Точное значение idle-timeout Cloudflare недокументировано, поэтому соединение
    // переподключается проактивно. Пингами сюда писать нельзя: сокет downlink-only,
    // сервер рвёт клиентскую запись кодом 1008 (это дизайн, а не ошибка связи).
    socketRecycleTimer = setTimeout(() => { if (ws.readyState === WebSocket.OPEN) ws.close(1000, "recycle"); }, CONFIG.socketRecycleMs);
  };
  ws.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.type === "hello" || data.type === "status") { renderStatus(data.status); return; }
    if (data.type === "event" && data.event.id > lastEventId) {
      lastEventId = data.event.id;
      renderEvent(data.event);
    }
  };
  ws.onclose = (event) => {
    stopSocketTimers();
    if (event.code === 1008) {
      // Единственный смысл 1008 на этом сокете — попытка ПИСАТЬ в downlink-only канал.
      // Как признак плохого токена он не трактуется: токен проверяет HTTP-вход.
      showGate(t("gate.error_socket_write"));
      return;
    }
    scheduleReconnect();
  };
  ws.onerror = () => ws.close();
}

// ── Действия ──────────────────────────────────────────────────────────────────────

async function submitTask() {
  const button = $("submit");
  button.disabled = true;
  $("tasks").textContent = "";
  try {
    let payload = null;
    const raw = $("payload").value.trim();
    if (raw) payload = JSON.parse(raw); // кривой JSON — громкое исключение, не тихая постановка
    const body = await apiJson(CONFIG.routes.tasks, { method: "POST", body: JSON.stringify({ payload }) });
    $("tasks").textContent = body.dispatched
      ? t("task.dispatched", { task_id: body.task_id })
      : t("task.not_configured", { task_id: body.task_id, detail: body.detail });
    $("payload").value = "";
  } catch (error) {
    if (error instanceof SyntaxError) $("tasks").textContent = t("task.error_bad_json");
    else if (error.status) $("tasks").textContent = t("task.error_http", { status: error.status });
  } finally {
    button.disabled = false;
  }
}

// ── Токен ─────────────────────────────────────────────────────────────────────────

// Вставленный из терминала токен часто несёт невидимые символы — срезаем их все.
function cleanToken(raw) {
  return raw.replace(/[\u200B-\u200D\uFEFF\s]/g, "");
}

function showGate(message) {
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  stopSocketTimers();
  $("gate").hidden = false;
  $("gate-error").textContent = message || "";
  $("gate-token").focus();
}

async function start() {
  $("gate").hidden = true;
  localStorage.setItem(TOKEN_KEY, token);
  try {
    const status = await apiJson(CONFIG.routes.status);
    renderStatus(status);
    await replay();
    connectWebSocket();
  } catch (error) {
    if (error.status === 401) {
      showGate(t("gate.error_http", { status: 401 }));
      localStorage.removeItem(TOKEN_KEY);
    } else if (!error.status) {
      showGate(t("gate.error_network", { detail: error.message }));
    }
    // ошибки с кодом, отличным от 401, уже показаны в строке задач
  }
}

$("gate-enter").addEventListener("click", () => {
  token = cleanToken($("gate-token").value);
  if (!token) { $("gate-error").textContent = t("gate.error_empty"); return; }
  start();
});
$("gate-token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("gate-enter").click(); });
$("gate-show").addEventListener("change", (e) => {
  $("gate-token").type = e.target.checked ? "text" : "password";
});
$("submit").addEventListener("click", submitTask);
$("forget").addEventListener("click", () => { localStorage.removeItem(TOKEN_KEY); location.reload(); });

// Токен можно передать адресной строкой (?token=…): для владельца, со страницы логов.
// Из истории браузера параметр сразу убирается.
const urlToken = new URLSearchParams(location.search).get("token");
if (urlToken) {
  history.replaceState(null, "", location.pathname);
  token = cleanToken(urlToken);
}

if (!token) showGate(""); else start();
