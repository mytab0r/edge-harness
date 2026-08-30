//#region plugin-manager — раздел «Плагины» в настройках морды (issue #102)
//
// Это ТЕЛО фабрики клиентского бандла: обёртку window.__ModuleLoader__.load
// и константу MANIFEST (вшитый срез dsh-edge/plugins.json: id/server/client)
// дописывает build.mjs. Свободные переменные тела: require (параметр фабрики),
// MANIFEST, module/exports (обёртка).
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
// Контракт: GET /api/events?task_id=plugin:<id>&limit=10, ответ
// { events: [{id, seq, ts, source, kind, data}, …], has_more, next_after }
// (openspec/specs/journal-tasks-hands.md, реализация cf-worker/src/harness.ts).
// События идут по возрасту id, значит последний элемент выборки — свежайший.
// Браузер ходит сессионной кукой (после POST /api/session обмена кука едет
// сама) — Bearer у браузера нет и быть не должно.
//
// Ответ, не совпавший по форме контракта, — НЕ «статусов нет», а ошибка: тот
// же путь на чужом origin отвечает другим API, и тихо показать «установлен»
// по чужому ответу — silent-wrong. Форма проверяется явно.
const JOURNAL_QUERY = "/api/events?limit=10&task_id=";

async function fetchPluginStatus(id) {
  const response = await fetch(JOURNAL_QUERY + encodeURIComponent("plugin:" + id), {
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
  const statusEvents = body.events.filter((event) => event !== null && event.kind === "plugin_status");
  return statusEvents.length > 0 ? statusEvents[statusEvents.length - 1].data : null;
}

// Состояние ячейки статуса из data события plugin_status
// ({plugin, state: building|built|deploying|ready|failed, detail?}).
// note — detail события или пояснение ячейки (уходит в tooltip и строку).
// Неизвестное состояние журнала показывается как есть — не выдумываем.
// Нет событий — «установлен»: факт из манифеста, а не догадка о состоянии.
function statusView(t, data) {
  const state = data !== null && typeof data === "object" && typeof data.state === "string"
    ? data.state
    : null;
  const detail = data !== null && typeof data === "object" && typeof data.detail === "string"
    ? data.detail
    : null;
  if (state === null) {
    return { dot: null, text: t("statusInstalled"), note: t("statusInstalledHint") };
  }
  if (state === "ready") return { dot: "done", text: t("statusReady"), note: detail };
  if (state === "deploying" || state === "building" || state === "built") {
    return { dot: "ongoing", text: t("statusDeploying"), note: detail };
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
    statusFailed: "failed",
    statusInstalled: "installed",
    statusInstalledHint: "no plugin_status events in the journal yet",
    statusUnknown: "unknown state",
    journalError: "Journal unavailable",
    retry: "Retry",
    hintTitle: "New plugins",
    hintText: "Ask the agent in chat (the smart principle) — the runner will build and install them.",
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
    statusFailed: "失败",
    statusInstalled: "已安装",
    statusInstalledHint: "日志中还没有 plugin_status 事件",
    statusUnknown: "未知状态",
    journalError: "日志不可用",
    retry: "重试",
    hintTitle: "新插件",
    hintText: "在聊天里让 agent 构建（智能原则）— runner 会编译并安装。",
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
    statusFailed: "ошибка",
    statusInstalled: "установлен",
    statusInstalledHint: "событий plugin_status в журнале ещё нет",
    statusUnknown: "неизвестное состояние",
    journalError: "Журнал недоступен",
    retry: "Повторить",
    hintTitle: "Новые плагины",
    hintText: "Попроси агента в чате (умный принцип) — раннер соберёт и установит.",
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
  else if (view.dot === null) dot = h("span", { style: styles.dotIdle });  // установлен: событий в журнале нет
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
    view = statusView(t, outcome.data);
  }
  return h("li", { style: styles.row },
    h("code", { style: styles.id }, plugin.id),
    h("span", { style: styles.flags },
      plugin.server === true ? h(PluginFlag, { label: t("flagServer") }) : null,
      plugin.client === true ? h(PluginFlag, { label: t("flagClient") }) : null),
    statusCell(outcome, view));
}

function PluginsSection(props) {
  const { t } = props;
  // Первый рендер уже рисует все строки манифеста — стартуем в состоянии
  // загрузки, а не с пустым списком outcome'ов.
  const [outcomes, setOutcomes] = useState(() => MANIFEST.map(() => ({ data: undefined })));
  const [failed, setFailed] = useState(null);
  const load = useCallback(async () => {
    setFailed(null);
    setOutcomes(MANIFEST.map(() => ({ data: undefined })));
    const settled = await Promise.allSettled(MANIFEST.map((plugin) => fetchPluginStatus(plugin.id)));
    const next = settled.map((result) => (
      result.status === "fulfilled"
        ? { ok: true, data: result.value }
        : { ok: false, error: result.reason }
    ));
    const firstFailure = next.find((outcome) => !outcome.ok);
    setOutcomes(next);
    if (firstFailure !== undefined) setFailed(firstFailure.error);
  }, []);
  useEffect(() => { void load(); }, [load]);
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
    h("div", { style: styles.hint },
      h("p", { style: styles.hintTitle }, t("hintTitle")),
      h("p", { style: styles.hintText }, t("hintText"))));
}

// ── Монтаж ────────────────────────────────────────────────────────────────────
// Сервисы ctx, без которых apply не имеет смысла: slots (реестр слотов) даёт
// dsh-client-runtime, locale (словари) — dsh-client-locale.
const inject = ["slots", "locale"];

function apply(ctx) {
  if (!Array.isArray(MANIFEST)) throw new Error("plugin-manager: MANIFEST не массив — сборка битая");
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
