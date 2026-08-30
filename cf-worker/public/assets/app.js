/* edge-harness: клиент морды. Живёт на статике Workers Assets, говорит с Durable Object
   через /api. Пути берутся ТОЛЬКО ключами из window.EDGE_CONFIG.routes (таблица хранит
   полные пути, ничего не склеивается — класс ошибки «двойной префикс» невозможен):
   неизвестный ключ — громкое исключение, а не тихий запрос в никуда.
   Вход: HANDS_TOKEN вводится один раз в гейте, сервер обменяет его на подписанную
   HttpOnly-куку (POST /api/session). Токен не хранится нигде в браузере и не ходит
   в URL — все запросы, включая WebSocket, авторизуются кукой автоматически. */

"use strict";

const CONFIG = window.EDGE_CONFIG;
const t = (key, params) => {
  let text = (window.EDGE_I18N[CONFIG.locale] || {})[key] || key;
  if (params) text = text.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
  return text;
};

const route = (name) => {
  const path = CONFIG.routes[name];
  if (!path) throw new Error(`Неизвестный маршрут: ${name}`);
  return path;
};

// Путь — только из таблицы; параметры — отдельным query. Склеивать префиксы негде.
const routeQ = (name, params = {}) => {
  const query = new URLSearchParams(params).toString();
  return query ? `${route(name)}?${query}` : route(name);
};

const $ = (id) => document.getElementById(id);

let lastEventId = 0; // курсор журнала: и для replay, и для after у WebSocket
let socket = null;
let socketRecycleTimer = null;
let reconnectDelay = CONFIG.reconnectBaseMs;

const api = (path, options = {}) =>
  fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...options.headers,
    },
  });

async function apiJson(path, options) {
  const res = await api(path, options);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const message = body?.error?.message || `HTTP ${res.status}`;
    $("tasks").textContent = t("task.error", { detail: message });
    if (res.status === 401) {
      // Сессия кончилась (или её не было): сокет молча переподключаться не должен.
      showGate(t("gate.error_http", { status: 401 }));
    }
    const error = new Error(message);
    error.status = res.status;
    throw error;
  }
  return body;
}

// ── Журнал ────────────────────────────────────────────────────────────────────────

// Проекция session_event (слайс 2, design dsh-streaming): строка читаемого вида
// `turn 3 / step 2 · tool:bash · ok` с усечённым текстом; дерево сессий — вне
// слайса. Рендер строится из данных события, новых маршрутов нет (ADR 0004).
const SESSION_TEXT_LIMIT = 160;

function blockText(blocks) {
  return (Array.isArray(blocks) ? blocks : [])
    .filter((b) => b && (b.type === "text" || b.type === "reasoning") && typeof b.text === "string")
    .map((b) => b.text)
    .join(" ");
}

function clip(text, limit = SESSION_TEXT_LIMIT) {
  const oneLine = String(text).replace(/\s+/g, " ").trim();
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine;
}

function sessionEventSummary(data) {
  if (!data || typeof data !== "object") return "";
  const p = data.payload;
  const place = data.turn !== undefined ? `turn ${data.turn}` : "";
  const placeStep = data.step !== undefined ? `${place} / step ${data.step}` : place;
  switch (data.type) {
    case "turn/start":
      return `${place} — старт`;
    case "turn/end": {
      const kind = p?.reason?.kind ?? "?";
      const detail = p?.reason?.error?.message ? ` · ${clip(p.reason.error.message, 120)}` : "";
      return `${place} — конец: ${kind}${detail}`;
    }
    case "step/start":
      return `${placeStep} — старт`;
    case "step/end":
      return `${placeStep} — конец`;
    case "user/message":
      return `вход · ${clip(blockText(p?.content))}`;
    case "assistant/message":
      return `ответ · ${clip(blockText(p?.message?.content))}${p?.interrupted ? " (прервано)" : ""}`;
    case "tool/call":
      return `${placeStep} · tool:${p?.name ?? "?"} · ${clip(p?.arguments ?? "", 80)}`;
    case "tool/result":
      return `${placeStep} · tool-result · ${p?.error ? "error" : "ok"} · ${clip(blockText(p?.message?.content?.[0]?.content))}`;
    default:
      // Незнакомый тип не прячем: усечённый JSON остаётся читаемым.
      return `${data.type ?? "?"} · ${clip(JSON.stringify(p ?? data))}`;
  }
}

function renderEvent(event) {
  const li = document.createElement("li");
  const data = event.data === null || event.data === undefined ? ""
    : event.kind === "session_event" ? sessionEventSummary(event.data)
    : JSON.stringify(event.data);
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
    const body = await apiJson(routeQ("events", { after: lastEventId, limit: CONFIG.replayPageSize })).catch(() => null);
    if (!body) return; // сеть мигнула: живой поток догонит, иначе — перезагрузка
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
  // Watchdog (#7): dispatched дольше порога без признаков рук — громко, но не паника.
  const stale = status.stale_dispatch;
  if (stale?.count > 0) {
    const minutes = Math.round(stale.oldest_age_ms / 60000);
    $("watchdog").textContent = t("watchdog.stale", { count: stale.count, minutes });
    $("watchdog").hidden = false;
  } else {
    $("watchdog").hidden = true;
  }
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

  // Кука уходит с апгрейдом сама (same-origin), заголовки браузерному WS не нужны.
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}${routeQ("eventsLive", { after: lastEventId })}`);
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
      // Как признак плохого входа он не трактуется: вход проверяет HTTP-запрос.
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
    const body = await apiJson(routeQ("tasks"), { method: "POST", body: JSON.stringify({ payload }) });
    $("tasks").textContent = body.dispatched
      ? t("task.dispatched", { task_id: body.task_id })
      : t("task.not_configured", { task_id: body.task_id, detail: body.detail });
    $("payload").value = "";
  } catch (error) {
    if (error instanceof SyntaxError) $("tasks").textContent = t("task.error_bad_json");
    else if (error.status && error.status !== 401) $("tasks").textContent = t("task.error_http", { status: error.status });
  } finally {
    button.disabled = false;
  }
}

// ── Вход и сессия ─────────────────────────────────────────────────────────────────

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

// Обмен HANDS_TOKEN на сессионную куку. Токен живёт ровно один запрос —
// ни localStorage, ни переменная, ни URL его не хранят.
async function login() {
  const raw = cleanToken($("gate-token").value);
  if (!raw) { $("gate-error").textContent = t("gate.error_empty"); return; }
  $("gate").hidden = true;
  $("gate-error").textContent = "";
  $("gate-token").value = "";
  try {
    const res = await api(route("session"), { method: "POST", headers: { Authorization: `Bearer ${raw}` } });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const error = new Error(body?.error?.message || `HTTP ${res.status}`);
      error.status = res.status;
      throw error;
    }
  } catch (error) {
    // Любая неудача входа — громко и на гейте: тихая пустая страница хуже падения.
    if (error.status) showGate(t("gate.error_http", { status: error.status }));
    else showGate(t("gate.error_network", { detail: error.message }));
    return;
  }
  await start();
}

async function start() {
  $("gate").hidden = true;
  try {
    const status = await apiJson(routeQ("status"));
    renderStatus(status);
    await replay();
    connectWebSocket();
  } catch (error) {
    if (!error.status) showGate(t("gate.error_network", { detail: error.message }));
    // 401 уже показал гейт apiJson; прочие HTTP-коды видны в панели задач.
  }
}

$("gate-enter").addEventListener("click", login);
$("gate-token").addEventListener("keydown", (e) => { if (e.key === "Enter") $("gate-enter").click(); });
$("gate-show").addEventListener("change", (e) => {
  $("gate-token").type = e.target.checked ? "text" : "password";
});
$("submit").addEventListener("click", submitTask);
$("forget").addEventListener("click", async () => {
  try {
    await apiJson(route("session"), { method: "DELETE" }); // куку снимает только сервер
  } catch (error) {
    if (error.status !== 401) {
      // Громко: сброс не прошёл — сессия, возможно, ещё жива, перезагрузка солгала бы выход.
      $("tasks").textContent = t("task.error", { detail: error.message });
      return;
    }
  }
  location.reload();
});

// Стартуем всегда: живая кука открывает морду, её отсутствие даёт 401 и гейт.
start();
