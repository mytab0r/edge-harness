//#region integrations — раздел «Интеграции» в настройках морды (issue #115)
//
// Это ТЕЛО фабрики клиентского бандла: обёртку window.__ModuleLoader__.load
// и константу INTEGRATIONS (вшитый dsh-edge/integrations.json ЦЕЛИКОМ —
// id, title, summary, tools, имена секретов с описаниями, wired, docs)
// дописывает build.mjs. Свободные переменные тела: require (параметр
// фабрики), INTEGRATIONS, module/exports (обёртка).
//
// Прецедент и приёмка формы — plugin-manager (#102): тот же списочный слот
// settings.section, те же seed-модули (react, ui-primitives), сервисы
// ctx.slots (dsh-client-runtime) и ctx.locale (dsh-client-locale) приходят
// из dsh.client.inject package.json.
//
// Секция показывает: что подключено (реестр), чей ключ (имена секретов с
// описаниями — ЗНАЧЕНИЙ секретов не существует в реестре и в бандле), что
// умеет (инструменты агента) и живой статус из журнала (после #105 — прокси
// /api/harness/* на стороне морды): каждый деплой пишет integration_status
// (шаг «Статусы интеграций» в deploy-dsh-edge.yml) в псевдо-задачу
// integration:<id>. Состояния: ready (все секреты на месте), not_configured
// (часть секретов отсутствует — в detail их имена), failed (деплой упал),
// building/built/deploying (промежуточные).
const { useState, useEffect, useCallback, createElement: h } = require("react");
const { Button, StateDot } = require("@deepseek-ai/dsh-client-ui-primitives");

// ── Журнал ────────────────────────────────────────────────────────────────────
// Контракт: GET /api/harness/events?task_id=integration:<id>&limit=&after=,
// ответ { events: [{id, seq, ts, source, kind, data}, …], has_more, next_after }
// (openspec/specs/journal-tasks-hands.md). Путь — прокси морды, как у
// plugin-manager: журнал — другой origin, кука морды SameSite=Strict.
// События идут по возрасту id, страницы — от старейших: свежайший статус
// может лежать за пределами первой страницы, идём до конца выборки по
// next_after, пока has_more. Ответ не по форме контракта — ошибка секции,
// а не «статусов нет»: тихий «подключено» по чужому ответу — silent-wrong.
const JOURNAL_PAGE_SIZE = 10;
const JOURNAL_QUERY = "/api/harness/events?task_id=";
const STATUS_KIND = "integration_status";

async function fetchStatusPage(id, after) {
  const url = JOURNAL_QUERY + encodeURIComponent("integration:" + id)
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

async function fetchIntegrationStatus(id) {
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
  const statusEvents = events.filter((event) => event !== null && event.kind === STATUS_KIND);
  return statusEvents.length > 0 ? statusEvents[statusEvents.length - 1].data : null;
}

// Состояния ячейки статуса из data события integration_status
// ({integration, state: ready|not_configured|failed|building|built|deploying, detail?}).
// Неизвестное состояние показывается как есть — не выдумываем. Событие БЕЗ
// state — warning с сырыми данными: событие было, и молча притворяться, что
// его не было, — silent-wrong. Нет событий вовсе — факт («деплой ещё не
// отчитывался»), а не догадка о подключённости.
function statusView(t, data) {
  const isObject = data !== null && typeof data === "object";
  const state = isObject && typeof data.state === "string" ? data.state : null;
  const detail = isObject && typeof data.detail === "string" ? data.detail : null;
  if (state === null) {
    if (isObject) {
      return { dot: "warning", text: t("statusUnknown"), note: JSON.stringify(data) };
    }
    return { dot: null, text: t("statusNoEvents"), note: t("statusNoEventsHint") };
  }
  if (state === "ready") return { dot: "done", text: t("statusReady"), note: detail };
  if (state === "not_configured") {
    return { dot: "warning", text: t("statusNotConfigured"), note: detail };
  }
  if (state === "failed") return { dot: "failed", text: t("statusFailed"), note: detail };
  if (state === "building" || state === "built" || state === "deploying") {
    return { dot: "ongoing", text: t("statusOngoing"), note: detail };
  }
  return { dot: "warning", text: state, note: detail ?? t("statusUnknown") };
}

// ── Словари ───────────────────────────────────────────────────────────────────
// Набор ключей одинаков во всех локалях (гвардия теста); zh — по конвенции
// апстрима источник набора, en и ru с ним сверены.
const dictionaries = {
  en: {
    nav: "Integrations",
    title: "Integrations",
    intro: "External systems the harness is wired to, from dsh-edge/integrations.json. Secret values never appear here — names and descriptions only.",
    wiredJobs: "keys live with runner jobs",
    wiredMorde: "keys live in the morde worker",
    credsTitle: "keys:",
    statusLoading: "…",
    statusReady: "connected",
    statusNotConfigured: "not configured",
    statusOngoing: "in progress",
    statusFailed: "failed",
    statusNoEvents: "no deploy reports yet",
    statusNoEventsHint: "the deploy pipeline has not reported integration_status for this row yet",
    statusUnknown: "unknown state",
    journalError: "Journal unavailable",
    retry: "Retry",
    docsLink: "docs",
    hintTitle: "How to connect",
    hintText: "Add the repository secrets named in the row, then redeploy the morde — the deploy step syncs them into the worker and reports integration_status to the journal. Tools become available to the chat agent immediately after the deploy.",
  },
  zh: {
    nav: "集成",
    title: "集成",
    intro: "harness 已接入的外部系统，来自 dsh-edge/integrations.json。此处不会出现密钥值——只有名称和说明。",
    wiredJobs: "密钥存放在 runner job 中",
    wiredMorde: "密钥存放在 morde worker 中",
    credsTitle: "密钥：",
    statusLoading: "…",
    statusReady: "已连接",
    statusNotConfigured: "未配置",
    statusOngoing: "进行中",
    statusFailed: "失败",
    statusNoEvents: "尚无部署报告",
    statusNoEventsHint: "部署流水线尚未为该行写入 integration_status",
    statusUnknown: "未知状态",
    journalError: "日志不可用",
    retry: "重试",
    docsLink: "文档",
    hintTitle: "如何接入",
    hintText: "添加行中列出的仓库 secret，然后重新部署 morde——部署步骤会把它们同步进 worker 并向日志写入 integration_status。部署完成后聊天 agent 立即可用这些工具。",
  },
  ru: {
    nav: "Интеграции",
    title: "Интеграции",
    intro: "Внешние системы, к которым подключён харнес, из dsh-edge/integrations.json. Значения секретов здесь не показываются — только имена и описания.",
    wiredJobs: "ключи у job'ов раннеров",
    wiredMorde: "ключи в воркере морды",
    credsTitle: "ключи:",
    statusLoading: "…",
    statusReady: "подключено",
    statusNotConfigured: "не настроено",
    statusOngoing: "в процессе",
    statusFailed: "ошибка",
    statusNoEvents: "деплой ещё не отчитывался",
    statusNoEventsHint: "конвейер деплоя ещё не писал integration_status для этой строки",
    statusUnknown: "неизвестное состояние",
    journalError: "Журнал недоступен",
    retry: "Повторить",
    docsLink: "документация",
    hintTitle: "Как подключить",
    hintText: "Добавь в репозиторий секреты с именами из строки и передеплой морду: шаг деплоя синхронизирует их в воркер и напишет integration_status в журнал. Инструменты станут доступны агенту чата сразу после деплоя.",
  },
};

// ── UI ────────────────────────────────────────────────────────────────────────
// Стили инлайновые намеренно (как у plugin-manager): CSS-модулей у рукописного
// бандла нет; цвета из переменных темы шелла.
const styles = {
  section: { display: "flex", flexDirection: "column", gap: "16px", padding: "4px 0" },
  intro: { margin: 0, color: "var(--dsh-text-3, #6b6b6b)", fontSize: "13px" },
  list: { listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" },
  row: {
    display: "flex", alignItems: "flex-start", gap: "12px", flexWrap: "wrap",
    padding: "10px 0", borderBottom: "1px solid var(--dsh-border, #3e3e42)",
  },
  id: { fontFamily: "var(--dsh-mono, ui-monospace, monospace)", fontSize: "13px" },
  entry: { display: "flex", flexDirection: "column", gap: "4px", minWidth: 0, flex: "1 1 240px" },
  entryTitle: { display: "flex", alignItems: "baseline", gap: "8px", flexWrap: "wrap" },
  name: { fontSize: "13px", fontWeight: 600, overflowWrap: "anywhere" },
  summary: { color: "var(--dsh-text-3, #6b6b6b)", fontSize: "12px", overflowWrap: "anywhere" },
  creds: { display: "flex", flexDirection: "column", gap: "2px", fontSize: "12px" },
  credsLine: { display: "flex", gap: "6px", alignItems: "baseline", flexWrap: "wrap" },
  credName: { fontFamily: "var(--dsh-mono, ui-monospace, monospace)", fontSize: "11px" },
  credDesc: { color: "var(--dsh-text-3, #6b6b6b)", fontSize: "11px", overflowWrap: "anywhere" },
  flags: { display: "flex", gap: "6px", flexWrap: "wrap" },
  flag: {
    fontSize: "11px", padding: "1px 8px", borderRadius: "999px",
    border: "1px solid var(--dsh-border, #3e3e42)",
    color: "var(--dsh-text-3, #6b6b6b)",
    fontFamily: "var(--dsh-mono, ui-monospace, monospace)",
  },
  status: {
    marginLeft: "auto", display: "flex", alignItems: "center", gap: "8px",
    fontSize: "13px", minWidth: 0, paddingTop: "10px",
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
  docs: { fontSize: "12px" },
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

/** Секретные имена интеграции — «чей ключ», значения не существуют в реестре. */
function credentialEntries(entry) {
  const secrets = entry?.credentials?.secrets;
  if (secrets === null || typeof secrets !== "object") return [];
  return Object.entries(secrets).map(([name, description]) => ({ name, description }));
}

function statusCell(outcome, view) {
  let dot;
  if (outcome.ok === undefined) dot = null;                                // грузится
  else if (outcome.ok === false) dot = h(StateDot, { state: "warning" });  // журнал не ответил по контракту
  else if (view.dot === null) dot = h("span", { style: styles.dotIdle });  // событий в журнале нет
  else dot = h(StateDot, { state: view.dot });
  return h("span", { style: styles.status, title: view.note ?? undefined },
    dot,
    h("span", { style: styles.statusText }, view.text),
    outcome.ok === true && view.note !== null && view.note !== undefined
      ? h("span", { style: styles.detail }, view.note)
      : null);
}

function IntegrationRow({ integration, outcome, t }) {
  const wired = integration.wired === "jobs";
  const wiredLabel = wired ? t("wiredJobs") : t("wiredMorde");
  const creds = credentialEntries(integration);
  let view;
  if (outcome.ok === false) {
    view = { dot: "warning", text: t("journalError"), note: String(outcome.error?.message ?? outcome.error) };
  } else if (outcome.data === undefined) {
    view = { dot: null, text: t("statusLoading"), note: null };
  } else {
    view = statusView(t, outcome.data);
  }
  return h("li", { style: styles.row },
    h("span", { style: styles.entry },
      h("span", { style: styles.entryTitle },
        h("code", { style: styles.id }, integration.id),
        h("span", { style: styles.name }, integration.title),
        h("span", { style: styles.flags },
          integration.tools.map((tool) => h("span", { key: tool, style: styles.flag }, tool)),
          h("span", { key: "wired", style: styles.flag, title: wiredLabel }, wired ? "jobs" : "morde"))),
      h("span", { style: styles.summary }, integration.summary),
      creds.length > 0
        ? h("span", { style: styles.creds },
            h("span", { style: styles.credsLine }, t("credsTitle")),
            creds.map((cred) => h("span", { key: cred.name, style: styles.credsLine, title: cred.description },
              h("code", { style: styles.credName }, cred.name),
              h("span", { style: styles.credDesc }, cred.description))))
        : null,
      typeof integration.docs === "string"
        ? h("a", { style: styles.docs, href: integration.docs, target: "_blank", rel: "noreferrer" }, t("docsLink"))
        : null),
    statusCell(outcome, view));
}

// Гвардия сборки — ЗДЕСЬ, до первого использования константы: модульный код
// исполняется раньше apply(), гвардия внутри apply() была бы мёртвой
// (по образцу plugin-manager, ревью PR #232, находка 1).
if (!Array.isArray(INTEGRATIONS)) throw new Error("integrations: INTEGRATIONS не массив — сборка битая");

function IntegrationsSection(props) {
  const { t } = props;
  // Первый рендер уже рисует все строки — стартуем в состоянии загрузки,
  // а не с пустым списком outcome'ов.
  const [outcomes, setOutcomes] = useState(() => INTEGRATIONS.map(() => ({ data: undefined })));
  const [failed, setFailed] = useState(null);
  const load = useCallback(async () => {
    setFailed(null);
    setOutcomes(INTEGRATIONS.map(() => ({ data: undefined })));
    const settled = await Promise.allSettled(INTEGRATIONS.map((entry) => fetchIntegrationStatus(entry.id)));
    const next = settled.map((result) => (
      result.status === "fulfilled"
        ? { ok: true, data: result.value }
        : { ok: false, error: result.reason }
    ));
    setOutcomes(next);
    const firstFailure = next.find((outcome) => !outcome.ok);
    if (firstFailure !== undefined) setFailed(firstFailure.error);
  }, []);
  useEffect(() => { void load(); }, [load]);
  return h("div", { style: styles.section },
    h("header", null,
      h("h2", null, t("title")),
      h("p", { style: styles.intro }, t("intro"))),
    h("ul", { style: styles.list, "aria-label": t("title") },
      INTEGRATIONS.map((integration, index) => h(IntegrationRow, {
        key: integration.id, integration, outcome: outcomes[index], t,
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
  ctx.effect(
    () => ctx.locale.register("settings.integrations", dictionaries),
    "integrations: settings.integrations dictionaries",
  );
  ctx.slots.inject("settings.section", () => ctx.slots.register({
    name: "settings.section",
    id: "integrations",
    order: 94,
    label: () => ctx.locale.bind("settings.integrations")("nav"),
    locale: "settings.integrations",
  }, IntegrationsSection));
}
//#endregion
