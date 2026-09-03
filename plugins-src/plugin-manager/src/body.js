//#region plugin-manager — раздел «Плагины» в настройках морды (issue #102, заказ — #113)
//
// Это ТЕЛО фабрики клиентского бандла: обёртку window.__ModuleLoader__.load
// и константы MANIFEST (вшитый срез dsh-edge/plugins.json: id/server/client)
// и CATALOG (вшитый каталог заказа dsh-edge/plugins-catalog.json целиком)
// дописывает build.mjs. Свободные переменные тела: require (параметр
// фабрики), MANIFEST, CATALOG, module/exports (обёртка).
//
// Откуда модули:
//  - react и @deepseek-ai/dsh-client-ui-primitives отдаёт seed-карта шелла
//    (staticModules при boot: react, react/jsx-runtime, react-dom,
//    react-dom/client, @deepseek-ai/cordis, @deepseek-ai/dsh-client-ui-slots,
//    @deepseek-ai/dsh-client-ui-primitives) — любой бандл ростера может их
//    require без деклараций. Снято с продового бандла dsh-edge 0.7.1
//    (assets/index-*.js, карта staticModules) и проверяется build.mjs.
//  - ctx.slots приносит клиентский плагин @deepseek-ai/dsh-client-runtime
//    (сервис "slots"), ctx.locale — @deepseek-ai/dsh-client-locale. Оба
//    объявлены в dsh.client.inject package.json: assemble-standalone-web.mjs
//    проверяет этот список по ростеру (неизвестный пакет = красная сборка),
//    а порядок загрузки строит orderByModuleGraph — сервисы уже на месте к
//    моменту apply.
//
// Форма монтажа списочного слота — по образцу ui-edge (пин 0.7.1):
// ctx.slots.inject(слот, () => ctx.slots.register({name, id, order, label,
// locale}, Component)); навигация настроек читает entries("settings.section")
// и рендерит только активную секцию, передавая ей { close } и t (переводчик
// пространства имён из поля locale декларации).
const { useState, useEffect, useCallback, createElement: h } = require("react");
const { Button, StateDot } = require("@deepseek-ai/dsh-client-ui-primitives");

// ── Журнал ────────────────────────────────────────────────────────────────────
// Контракт: GET /api/harness/events?task_id=plugin:<id>&limit=&after=, ответ
// { events: [{id, seq, ts, source, kind, data}, …], has_more, next_after }
// (openspec/specs/journal-tasks-hands.md, реализация cf-worker/src/harness.ts).
// Путь — ПРОКСИ СТОРОНЫ МОРДЫ (/api/harness/* → журнал edge-harness), которым
// владелец закрыл белое пятно #105 вариантом 1 (релиз-ноты plugins-manager-
// v0.1.2: «журнал читается через /api/harness/* (патч 0004)»): журнал —
// другой origin, кука морды SameSite=Strict, CORS журнал не отдаёт, а
// same-origin /api/events попадает в чужой API морды (401/HTML). Форма
// запроса и ответа — контракт журнала, прокси прозрачен.
// События идут по возрасту id, а страницы отдаются от старейших: свежайший
// статус может лежать за пределами первой страницы (каждый деплой пишет 2+
// plugin_status, журнал растёт), поэтому идём до конца выборки по next_after,
// пока has_more. Одна страница = протухший статус навсегда.
// Браузер ходит сессионной кукой владельца морды (credentials: include) —
// Bearer у браузера нет и быть не должно.
//
// Ответ, не совпавший по форме контракта, — НЕ «статусов нет», а ошибка: тот
// же путь на чужом origin отвечает другим API, и тихо показать «установлен»
// по чужому ответу — silent-wrong. Форма проверяется явно.
const JOURNAL_PAGE_SIZE = 10;
const JOURNAL_QUERY = "/api/harness/events?task_id=";

async function fetchStatusPage(id, after) {
  const url = JOURNAL_QUERY + encodeURIComponent("plugin:" + id)
    + "&limit=" + JOURNAL_PAGE_SIZE + "&after=" + after;
  const response = await fetch(url, {
    credentials: "include",
    headers: { accept: "application/json" },
  });
  if (!response.ok) throw new Error("HTTP " + response.status);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("json")) {
    throw new Error("ответ не JSON (" + (contentType || "без content-type") + ") — это не журнал");
  }
  const body = await response.json();
  if (body === null || typeof body !== "object" || !Array.isArray(body.events)) {
    throw new Error("ответ без массива events — не по контракту журнала");
  }
  return body;
}

async function fetchPluginStatus(id) {
  const events = [];
  let after = 0;
  for (;;) {
    const page = await fetchStatusPage(id, after);
    events.push(...page.events);
    if (page.has_more !== true) break;
    if (typeof page.next_after !== "number") {
      throw new Error("has_more без числового next_after — не по контракту журнала");
    }
    after = page.next_after;
  }
  const statusEvents = events.filter((event) => event !== null && event.kind === "plugin_status");
  return statusEvents.length > 0 ? statusEvents[statusEvents.length - 1].data : null;
}

// ── Заказ установки: «умный принцип», НЕ рантайм-инсталл (#113) ──────────────
// Рантайм-установка кода на CF Free невозможна (docs/research/30-rejected-
// alternatives.md п.5): «Заказать» — это заказ конвейера #80 (forge → релиз +
// sha256 → PR в каталог → деплой), а не инсталляция. Транспорт — ШТАТНЫЙ RPC
// морды session.prompt: same-origin, владелец уже залогинен кукой воркера.
// Заказ попадает выделенную сессию plugin-orders как обычное сообщение чата —
// агент ведёт его по конвейеру. Задача в пул журнала (POST /api/tasks) из
// браузера морды недостижима без нового патча-прокси и секретов (белое
// пятно #105, кука SameSite=Strict, CORS журнал не отдаёт); путь через RPC
// морды — работающий сегодня без новых патчей и секретов, перенос на задачу
// в пул — после write-прокси (ADR 0009).
//
// Контракты RPC (сняты с типов @deepseek-ai/dsh-host-apiproxy 0.1.1-rc.2,
// docs/research/12-dsh-edge-session-api.md):
//   POST /api/<method>  {type:"client-request", rpcId, method, payload}
//   → {type:"server-response", rpcId, result:{ok:true,value}|{ok:false,error}}
//   workspace.create {path} → {workspace:{workspaceId}} (идемпотентен)
//   session.create {workspaceId, sessionId} → {sessionId} (sessionId задаём
//     сами — идемпотентный create-or-reuse)
//   session.rename {sessionId, title} → {title, seq}
//   session.prompt {sessionId, mode:"queue", content:[{type:"text",text}]}
//     → {accepted:true} — «принято», НЕ «агент справился»: исход заказа виден
//     в сессии plugin-orders и в статусах конвейера (plugin:<id>).
//   session.history {sessionId, maxMessages} → {events:[{event,…}], hasMore}
const ORDERS_SESSION_ID = "plugin-orders";
const ORDERS_SESSION_TITLE = "Заказ плагинов";
const ORDERS_WORKSPACE_PATH = "/workspace/edge-harness";
// Окно дедупликации: сколько последних сообщений сессии смотреть (включая
// ответы агента — session.history режет по всем сообщениям, не только по
// заказам, так что заказов в окно помещается меньше 20). Заказ, ушедший за
// окно, повторно отсекать нельзя молча и бессмысленно запирать навсегда:
// окно — объявленный газ, после установки плагин уходит из каталога сам
// (вычитание манифеста).
const DEDUP_WINDOW_MESSAGES = 20;
// Свободная переменная обёртки: маркер заказа ВЫВЕДЕН сборкой из шаблона id
// (ID_PATTERN в dsh-edge/manifest.mjs — одно место правды; см. build.mjs —
// там же гвардия, что каждый id каталога и манифеста матчится маркером).
const ORDER_MARKER = new RegExp(ORDER_MARKER_SOURCE);

class RpcError extends Error {
  constructor(code, message) {
    super((code ?? "rpc-error") + (message ? ": " + message : ""));
    this.code = typeof code === "string" ? code : null;
  }
}

let rpcSeq = 0;
async function rpcCall(method, payload) {
  const response = await fetch("/api/" + method, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      type: "client-request",
      rpcId: "pm-" + Date.now() + "-" + (rpcSeq += 1),
      method,
      payload,
    }),
  });
  if (!response.ok) throw new Error("HTTP " + response.status);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("json")) {
    throw new Error("ответ не JSON (" + (contentType || "без content-type") + ") — это не RPC морды");
  }
  const body = await response.json();
  if (body === null || typeof body !== "object"
    || body.type !== "server-response"
    || body.result === null || typeof body.result !== "object") {
    throw new Error("ответ не конверт RPC (server-response с result) — не по контракту морды");
  }
  if (body.result.ok !== true) {
    throw new RpcError(body.result.error?.code, body.result.error?.message);
  }
  return body.result.value;
}

// Готовая формулировка заказа: маркер для дедупликации + описание из каталога
// (одно место правды о том, ЧТО заказывается) + план конвейера. Текст
// протокольный и на локали UI не зависит: его читает агент, а не владелец.
function orderText(entry) {
  const lines = [
    "[plugin-order:" + entry.id + "] Закажи установку плагина из каталога морды (раздел «Плагины»).",
    "",
    entry.title + ".",
    entry.summary,
    entry.brief,
    "",
    entry.sources
      ? "Исходники уже в репозитории: " + entry.sources + " — собери и установи их."
      : "Исходников в репозитории ещё нет — напиши плагин с нуля по этому описанию.",
  ];
  if (entry.spec) {
    lines.push("Дизайн и приёмка: " + entry.spec + " — источник правды, этот заказ его не заменяет.");
  }
  lines.push(
    "",
    "Конвейер установки: tarball → релиз этого репозитория + sha256 → PR в dsh-edge/plugins.json → "
      + "деплой морды (рантайм-установки кода на Cloudflare Free нет). "
      + "Прогресс конвейера — журнал edge-harness, задача plugin:" + entry.id + " (события plugin_status).",
  );
  return lines.join("\n");
}

// Идемпотентная подготовка сессии заказов (create-or-reuse + пин заголовка) —
// по образцу scripts/lib/dsh-edge-session.sh (dsh_edge_session_begin).
async function ensureOrdersSession() {
  const created = await rpcCall("workspace.create", { path: ORDERS_WORKSPACE_PATH });
  const workspaceId = created?.workspace?.workspaceId;
  if (typeof workspaceId !== "string" || workspaceId === "") {
    throw new Error("workspace.create ответил без workspaceId — не по контракту морды");
  }
  await rpcCall("session.create", { workspaceId, sessionId: ORDERS_SESSION_ID });
  await rpcCall("session.rename", { sessionId: ORDERS_SESSION_ID, title: ORDERS_SESSION_TITLE });
}

async function sendOrder(entry) {
  await ensureOrdersSession();
  await rpcCall("session.prompt", {
    sessionId: ORDERS_SESSION_ID,
    mode: "queue",
    content: [{ type: "text", text: orderText(entry) }],
  });
}

// Дедупликация: id плагинов, заказ которых ещё виден в окне истории сессии
// заказов. Сессии нет (session-not-found) — заказов не было, это штатная
// пустота, а не отказ. Чужой/битый ответ — ошибка, не «заказов нет».
async function fetchOrderedIds() {
  let value;
  try {
    value = await rpcCall("session.history", { sessionId: ORDERS_SESSION_ID, maxMessages: DEDUP_WINDOW_MESSAGES });
  } catch (error) {
    if (error instanceof RpcError && error.code === "session-not-found") return new Set();
    throw error;
  }
  if (value === null || typeof value !== "object" || !Array.isArray(value.events)) {
    throw new Error("ответ history без массива events — не по контракту морды");
  }
  const ids = new Set();
  for (const pageEntry of value.events) {
    const event = pageEntry?.event;
    if (event?.type !== "user/message") continue;
    const blocks = event.data?.content;
    if (!Array.isArray(blocks)) continue;
    for (const block of blocks) {
      if (block?.type === "text" && typeof block.text === "string") {
        const match = block.text.match(ORDER_MARKER);
        if (match) ids.add(match[1]);
      }
    }
  }
  return ids;
}

// Состояния ячейки статуса из data события plugin_status
// ({plugin, state: building|built|deploying|ready|failed, detail?}).
// note — detail события или пояснение ячейки (уходит в tooltip и строку).
// Неизвестное состояние журнала показывается как есть — не выдумываем.
// Событие БЕЗ state — не «установлен», а warning с сырыми данными: событие
// было, и молча притворяться, что его не было, — silent-wrong.
// Нет событий вовсе — noEventsText: факт (для манифеста — «установлен»,
// для строки каталога — «конвейер не запускался»), а не догадка.
function statusView(t, data, noEventsText) {
  const isObject = data !== null && typeof data === "object";
  const state = isObject && typeof data.state === "string" ? data.state : null;
  const detail = isObject && typeof data.detail === "string" ? data.detail : null;
  if (state === null) {
    if (isObject) {
      return { dot: "warning", text: t("statusUnknown"), note: JSON.stringify(data) };
    }
    return { dot: null, text: noEventsText, note: t("statusInstalledHint") };
  }
  if (state === "ready") return { dot: "done", text: t("statusReady"), note: detail };
  if (state === "deploying") return { dot: "ongoing", text: t("statusDeploying"), note: detail };
  if (state === "building" || state === "built") {
    return { dot: "ongoing", text: t("statusOngoing"), note: detail };
  }
  if (state === "failed") return { dot: "failed", text: t("statusFailed"), note: detail };
  return { dot: "warning", text: state, note: detail ?? t("statusUnknown") };
}

// ── Словари ───────────────────────────────────────────────────────────────────
// Набор ключей одинаков во всех локалях; zh — по конвенции апстрима источник
// набора, en и ru с ним сверены. ru — для владельца, если локаль морды когда-
// нибудь получит ru (каталог Language row сейчас en/zh).
const dictionaries = {
  en: {
    nav: "Plugins",
    title: "Plugins",
    intro: "Harness plugins installed from the release manifest.",
    flagServer: "server",
    flagClient: "client",
    statusLoading: "…",
    statusReady: "ready",
    statusDeploying: "deploying",
    statusOngoing: "in progress",
    statusFailed: "failed",
    statusInstalled: "installed",
    statusInstalledHint: "no plugin_status events in the journal yet",
    statusUnknown: "unknown state",
    journalError: "Journal unavailable",
    retry: "Retry",
    hintTitle: "New plugins",
    hintText: "«Order» sends a ready-made order to the agent in the Plugin orders session (the smart principle); the catalog lives in dsh-edge/plugins-catalog.json. Runtime install is impossible on Cloudflare Free — installation is a deploy.",
    catalogTitle: "Available to order",
    catalogEmpty: "The catalog is empty — add a plugin to dsh-edge/plugins-catalog.json.",
    catalogIdle: "pipeline not started",
    orderButton: "Order",
    orderBusy: "sending…",
    orderOrdered: "ordered",
    orderError: "Order was not sent",
    dedupError: "Cannot check for duplicate orders",
  },
  zh: {
    nav: "插件",
    title: "插件",
    intro: "从发布清单安装的 harness 插件。",
    flagServer: "服务端",
    flagClient: "客户端",
    statusLoading: "…",
    statusReady: "就绪",
    statusDeploying: "部署中",
    statusOngoing: "进行中",
    statusFailed: "失败",
    statusInstalled: "已安装",
    statusInstalledHint: "日志中还没有 plugin_status 事件",
    statusUnknown: "未知状态",
    journalError: "日志不可用",
    retry: "重试",
    hintTitle: "新插件",
    hintText: "「订购」会把现成的订单发给「插件订单」会话中的 agent（智能原则）；目录在 dsh-edge/plugins-catalog.json。Cloudflare Free 无法运行时安装代码——安装即部署。",
    catalogTitle: "可订购",
    catalogEmpty: "目录为空——请在 dsh-edge/plugins-catalog.json 中添加插件。",
    catalogIdle: "流水线未启动",
    orderButton: "订购",
    orderBusy: "发送中…",
    orderOrdered: "已订购",
    orderError: "订单未发送",
    dedupError: "无法检查重复订单",
  },
  ru: {
    nav: "Плагины",
    title: "Плагины",
    intro: "Плагины харнеса, установленные из релизного манифеста.",
    flagServer: "сервер",
    flagClient: "клиент",
    statusLoading: "…",
    statusReady: "готов",
    statusDeploying: "деплоится",
    statusOngoing: "в процессе",
    statusFailed: "ошибка",
    statusInstalled: "установлен",
    statusInstalledHint: "событий plugin_status в журнале ещё нет",
    statusUnknown: "неизвестное состояние",
    journalError: "Журнал недоступен",
    retry: "Повторить",
    hintTitle: "Новые плагины",
    hintText: "Кнопка «Заказать» отправляет готовую формулировку агенту в сессию «Заказ плагинов» (умный принцип); каталог — dsh-edge/plugins-catalog.json. Рантайм-установки кода на Cloudflare Free нет — установка = деплой.",
    catalogTitle: "Доступные для заказа",
    catalogEmpty: "Каталог пуст — добавь плагин в dsh-edge/plugins-catalog.json.",
    catalogIdle: "конвейер не запускался",
    orderButton: "Заказать",
    orderBusy: "отправляю…",
    orderOrdered: "заказано",
    orderError: "Заказ не отправлен",
    dedupError: "Повторные заказы проверить не удалось",
  },
};

// ── UI ────────────────────────────────────────────────────────────────────────
// Стили инлайновые намеренно: CSS-модули требуют этапа сборки стилей, которого
// у рукописного бандла нет; цвета берутся из переменных темы шелла, чтобы
// секция не выпадала из тёмной/светлой темы.
const styles = {
  section: { display: "flex", flexDirection: "column", gap: "16px", padding: "4px 0" },
  intro: { margin: 0, color: "var(--dsh-text-3, #6b6b6b)", fontSize: "13px" },
  list: { listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" },
  row: {
    display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap",
    padding: "10px 0", borderBottom: "1px solid var(--dsh-border, #3e3e42)",
  },
  id: { fontFamily: "var(--dsh-mono, ui-monospace, monospace)", fontSize: "13px" },
  entry: { display: "flex", flexDirection: "column", gap: "2px", minWidth: 0, flex: "1 1 240px" },
  entryTitle: { fontSize: "13px", fontWeight: 600, overflowWrap: "anywhere" },
  entrySummary: {
    color: "var(--dsh-text-3, #6b6b6b)", fontSize: "12px", overflowWrap: "anywhere",
  },
  actions: { display: "flex", alignItems: "center", gap: "8px", flex: "none" },
  flags: { display: "flex", gap: "6px" },
  flag: {
    fontSize: "11px", padding: "1px 8px", borderRadius: "999px",
    border: "1px solid var(--dsh-border, #3e3e42)",
    color: "var(--dsh-text-3, #6b6b6b)",
  },
  status: {
    marginLeft: "auto", display: "flex", alignItems: "center", gap: "8px",
    fontSize: "13px", minWidth: 0,
  },
  statusText: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
  detail: {
    color: "var(--dsh-text-3, #6b6b6b)", fontSize: "12px",
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "320px",
  },
  dotIdle: {
    width: "8px", height: "8px", borderRadius: "50%", flex: "none",
    background: "var(--dsh-text-3, #6b6b6b)", opacity: 0.5,
  },
  catalogTitle: { margin: "8px 0 0 0", fontSize: "14px", fontWeight: 600 },
  hint: {
    border: "1px solid var(--dsh-border, #3e3e42)", borderRadius: "8px",
    padding: "10px 12px", display: "flex", flexDirection: "column", gap: "4px",
  },
  hintTitle: { margin: 0, fontSize: "13px", fontWeight: 600 },
  hintText: { margin: 0, color: "var(--dsh-text-3, #6b6b6b)", fontSize: "12px" },
  error: {
    display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap",
    color: "var(--dsh-error, #ef4444)", fontSize: "13px",
  },
  errorText: { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
};

function PluginFlag({ label }) {
  return h("span", { style: styles.flag }, label);
}

// outcome на строку: { data: undefined } — грузится; { ok: true, data } —
// ответ журнала (data = data последнего plugin_status или null, если событий
// нет); { ok: false, error } — журнал не ответил по контракту. Статусы грузятся
// по плагину независимо (allSettled): отказ одного запроса не изобретает
// статусы остальных.
function statusCell(outcome, view) {
  let dot;
  if (outcome.ok === undefined) dot = null;                                // грузится
  else if (outcome.ok === false) dot = h(StateDot, { state: "warning" });  // журнал не ответил по контракту
  else if (view.dot === null) dot = h("span", { style: styles.dotIdle });  // событий в журнале нет: факт из манифеста/каталога
  else dot = h(StateDot, { state: view.dot });
  return h("span", { style: styles.status, title: view.note ?? undefined },
    dot,
    h("span", { style: styles.statusText }, view.text),
    outcome.ok === true && view.note !== null && view.note !== undefined
      ? h("span", { style: styles.detail }, view.note)
      : null);
}

function PluginRow({ plugin, outcome, t }) {
  let view;
  if (outcome.ok === false) {
    view = { dot: "warning", text: t("journalError"), note: String(outcome.error?.message ?? outcome.error) };
  } else if (outcome.data === undefined) {
    view = { dot: null, text: t("statusLoading"), note: null };
  } else {
    view = statusView(t, outcome.data, t("statusInstalled"));
  }
  return h("li", { style: styles.row },
    h("code", { style: styles.id }, plugin.id),
    h("span", { style: styles.flags },
      plugin.server === true ? h(PluginFlag, { label: t("flagServer") }) : null,
      plugin.client === true ? h(PluginFlag, { label: t("flagClient") }) : null),
    statusCell(outcome, view));
}

// Строка каталога: что это, статус конвейера (тот же plugin:<id> журнала —
// заказ виден до установки) и кнопка заказа. kind: unknown — дедупликация ещё
// проверяется (кнопка заперта, молча разрешать заказ нельзя); ready — можно
// заказывать; busy — отправляется; ordered — заказ уже в окне истории сессии
// заказов. Гвард в onClick дублирует disabled: не полагаемся на то, что
// примитив Button прокидывает атрибут (контракт примитива не подтверждён).
function CatalogRow({ entry, outcome, orderState, t, onOrder }) {
  let view;
  if (outcome.ok === false) {
    view = { dot: "warning", text: t("journalError"), note: String(outcome.error?.message ?? outcome.error) };
  } else if (outcome.data === undefined) {
    view = { dot: null, text: t("statusLoading"), note: null };
  } else {
    view = statusView(t, outcome.data, t("catalogIdle"));
  }
  const ordered = orderState.kind === "ordered";
  const locked = orderState.kind !== "ready";
  return h("li", { style: styles.row },
    h("span", { style: styles.entry },
      h("code", { style: styles.id }, entry.id),
      h("span", { style: styles.entryTitle }, entry.title),
      h("span", { style: styles.entrySummary }, entry.summary)),
    statusCell(outcome, view),
    h("span", { style: styles.actions },
      h(Button, {
        variant: "outline",
        size: "sm",
        disabled: locked,
        onClick: () => { if (!locked) onOrder(entry); },
      },
      ordered ? t("orderOrdered")
        : orderState.kind === "busy" ? t("orderBusy")
          : t("orderButton"))));
}

// Доступные к заказу = каталог минус установленные. Вычитание — на клиенте:
// каталог объявляет «что можно заказать», манифест — «что установлено», ни
// одна из правд не дублирует другую. Плагин из заказа попадает в манифест
// (PR конвейера) и сам исчезает из этого списка после деплоя.
// Гвардии сборки — ЗДЕСЬ, до первого использования констант: модульный код
// исполняется раньше apply(), гвардия внутри apply() была бы мёртвой
// (ревью PR #232, находка 1).
if (!Array.isArray(MANIFEST)) throw new Error("plugin-manager: MANIFEST не массив — сборка битая");
if (!Array.isArray(CATALOG)) throw new Error("plugin-manager: CATALOG не массив — сборка битая");
const AVAILABLE = CATALOG.filter((entry) => !MANIFEST.some((plugin) => plugin.id === entry.id));

function PluginsSection(props) {
  const { t } = props;
  // Первый рендер уже рисует все строки — стартуем в состоянии загрузки,
  // а не с пустым списком outcome'ов.
  const [outcomes, setOutcomes] = useState(() => MANIFEST.map(() => ({ data: undefined })));
  const [catalogOutcomes, setCatalogOutcomes] = useState(() => AVAILABLE.map(() => ({ data: undefined })));
  const [orderStates, setOrderStates] = useState(() => AVAILABLE.map(() => ({ kind: "unknown" })));
  const [failed, setFailed] = useState(null);
  const [dedupError, setDedupError] = useState(null);
  const [orderError, setOrderError] = useState(null);
  // Статусы установленных и доступных грузятся независимо (allSettled):
  // отказ одного запроса не изобретает статусы остальных.
  const load = useCallback(async () => {
    setFailed(null);
    setOutcomes(MANIFEST.map(() => ({ data: undefined })));
    setCatalogOutcomes(AVAILABLE.map(() => ({ data: undefined })));
    const settled = await Promise.allSettled([
      ...MANIFEST.map((plugin) => fetchPluginStatus(plugin.id)),
      ...AVAILABLE.map((entry) => fetchPluginStatus(entry.id)),
    ]);
    const next = settled.map((result) => (
      result.status === "fulfilled"
        ? { ok: true, data: result.value }
        : { ok: false, error: result.reason }
    ));
    const firstFailure = next.find((outcome) => !outcome.ok);
    setOutcomes(next.slice(0, MANIFEST.length));
    setCatalogOutcomes(next.slice(MANIFEST.length));
    if (firstFailure !== undefined) setFailed(firstFailure.error);
  }, []);
  useEffect(() => { void load(); }, [load]);
  // Дедупликация заказов — отдельная от статусов нагрузка: пока она не
  // сошлась, кнопки заперты (kind: unknown). Отказ — громкий, с повтором.
  const loadOrders = useCallback(async () => {
    setDedupError(null);
    setOrderStates(AVAILABLE.map(() => ({ kind: "unknown" })));
    try {
      const ordered = await fetchOrderedIds();
      setOrderStates(AVAILABLE.map((entry) => ({ kind: ordered.has(entry.id) ? "ordered" : "ready" })));
    } catch (error) {
      setDedupError(error);
    }
  }, []);
  useEffect(() => { void loadOrders(); }, [loadOrders]);
  // Заказ: перед отправкой дедупликация перепроверяется (секция могла быть
  // открыта давно, вторая вкладка могла заказать раньше): заказ уже в истории
  // → «заказано» без повторного prompt; перепроверку выполнить не удалось →
  // заказ НЕ отправляется вслепую (fail-closed), отказ громкий. Остаточный
  // газ — гонка двух вкладок внутри окна перепроверки: серверной
  // уникальности заказов нет, дубликат в этой щели возможен и виден в
  // сессии заказов (объявлено в README/ADR). Отказ не оставляет кнопку в
  // «отправляется» и не притворяется успехом. Обновления функциональные:
  // заказ с двух строк одновременно не должен затирать чужой busy/ordered.
  const order = async (entry) => {
    const index = AVAILABLE.findIndex((candidate) => candidate.id === entry.id);
    if (index < 0) return;
    setOrderError(null);
    setOrderStates((states) => states.map((state, i) => (i === index ? { kind: "busy" } : state)));
    try {
      const stillOpen = await fetchOrderedIds();
      if (stillOpen.has(entry.id)) {
        setOrderStates((states) => states.map((state, i) => (i === index ? { kind: "ordered" } : state)));
        return;
      }
      await sendOrder(entry);
      setOrderStates((states) => states.map((state, i) => (i === index ? { kind: "ordered" } : state)));
    } catch (error) {
      setOrderError(error);
      setOrderStates((states) => states.map((state, i) => (i === index ? { kind: "ready" } : state)));
    }
  };
  return h("div", { style: styles.section },
    h("header", null,
      h("h2", null, t("title")),
      h("p", { style: styles.intro }, t("intro"))),
    h("ul", { style: styles.list, "aria-label": t("title") },
      MANIFEST.map((plugin, index) => h(PluginRow, {
        key: plugin.id, plugin, outcome: outcomes[index], t,
      }))),
    failed !== null
      ? h("div", { style: styles.error, role: "alert" },
          h("span", null, t("journalError") + ":"),
          h("span", { style: styles.errorText }, String(failed?.message ?? failed)),
          h(Button, { variant: "outline", size: "sm", onClick: () => { void load(); } }, t("retry")))
      : null,
    h("h3", { style: styles.catalogTitle }, t("catalogTitle")),
    AVAILABLE.length === 0
      ? h("p", { style: styles.intro }, t("catalogEmpty"))
      : h("ul", { style: styles.list, "aria-label": t("catalogTitle") },
          AVAILABLE.map((entry, index) => h(CatalogRow, {
            key: entry.id, entry, outcome: catalogOutcomes[index], orderState: orderStates[index], t,
            onOrder: (candidate) => { void order(candidate); },
          }))),
    dedupError !== null
      ? h("div", { style: styles.error, role: "alert" },
          h("span", null, t("dedupError") + ":"),
          h("span", { style: styles.errorText }, String(dedupError?.message ?? dedupError)),
          h(Button, { variant: "outline", size: "sm", onClick: () => { void loadOrders(); } }, t("retry")))
      : null,
    orderError !== null
      ? h("div", { style: styles.error, role: "alert" },
          h("span", null, t("orderError") + ":"),
          h("span", { style: styles.errorText }, String(orderError?.message ?? orderError)))
      : null,
    h("div", { style: styles.hint },
      h("p", { style: styles.hintTitle }, t("hintTitle")),
      h("p", { style: styles.hintText }, t("hintText"))));
}

// ── Монтаж ────────────────────────────────────────────────────────────────────
// Сервисы ctx, без которых apply не имеет смысла: slots (реестр слотов) даёт
// dsh-client-runtime, locale (словари) — dsh-client-locale.
const inject = ["slots", "locale"];

function apply(ctx) {
  ctx.effect(
    () => ctx.locale.register("settings.plugins", dictionaries),
    "plugin-manager: settings.plugins dictionaries",
  );
  ctx.slots.inject("settings.section", () => ctx.slots.register({
    name: "settings.section",
    id: "plugin-manager",
    order: 95,
    label: () => ctx.locale.bind("settings.plugins")("nav"),
    locale: "settings.plugins",
  }, PluginsSection));
}
//#endregion
