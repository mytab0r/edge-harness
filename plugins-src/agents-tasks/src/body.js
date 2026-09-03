//#region agents-tasks — раздел «Агенты и задачи» в сайдбаре морды (issue #111)
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
const { useState, useEffect, useCallback, useRef, createElement: h } = require("react");
const { Button, StateDot, Pill, IconCopy, IconExternalLink, IconRefreshCw, IconChevronDown, IconChevronRight, IconMessageSquare, IconTerminal, IconBrain, IconTool, IconX } = require("@deepseek-ai/dsh-client-ui-primitives");

// ── Константы и конфигурация ────────────────────────────────────────────────────

const GITHUB_API_BASE = "https://api.github.com";
const GITHUB_REPO = "mytab0r/edge-harness"; // TODO: сделать настраиваемым через env или манифест
const TASK_LABEL = "task";

const JOURNAL_PAGE_SIZE = 20;
const JOURNAL_QUERY = "/api/events?task_id=";
const JOURNAL_LIVE = "/api/events.live?after=";

const POLL_INTERVAL_MS = 5000; // поллинг статусов задач и журнала
const TASKS_POLL_INTERVAL_MS = 15000; // поллинг списка задач (GitHub API)

// ── Типы данных (JSDoc для документации) ────────────────────────────────────────

/**
 * @typedef {Object} GitHubIssue
 * @property {number} number
 * @property {string} title
 * @property {string} state
 * @property {string} html_url
 * @property {Array<{login: string}>} assignees
 * @property {Array<{name: string}>} labels
 * @property {string} created_at
 * @property {string} updated_at
 * @property {Object|null} pull_request
 */

/**
 * @typedef {Object} JournalEvent
 * @property {number} id
 * @property {string} task_id
 * @property {number} seq
 * @property {number} ts
 * @property {string} source
 * @property {string} kind
 * @property {any} data
 */

/**
 * @typedef {Object} TaskWithStatus
 * @property {GitHubIssue} issue
 * @property {string} status // 'queued' | 'dispatched' | 'running' | 'done' | 'failed' | 'unknown'
 * @property {JournalEvent[]} events
 * @property {boolean} loading
 * @property {string|null} error
 */

// ── Утилиты ────────────────────────────────────────────────────────────────────

function formatRelativeTime(ts) {
  const diff = Date.now() - ts;
  if (diff < 60000) return "только что";
  if (diff < 3600000) return Math.floor(diff / 60000) + " мин назад";
  if (diff < 86400000) return Math.floor(diff / 3600000) + " ч назад";
  return Math.floor(diff / 86400000) + " дн назад";
}

function formatTimestamp(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function getStatusDisplay(t, status) {
  switch (status) {
    case 'queued': return { dot: 'ongoing', text: t('statusQueued'), color: 'var(--dsh-warning, #f59e0b)' };
    case 'dispatched': return { dot: 'ongoing', text: t('statusDispatched'), color: 'var(--dsh-info, #3b82f6)' };
    case 'running': return { dot: 'ongoing', text: t('statusRunning'), color: 'var(--dsh-info, #3b82f6)' };
    case 'done': return { dot: 'done', text: t('statusDone'), color: 'var(--dsh-success, #22c55e)' };
    case 'failed': return { dot: 'failed', text: t('statusFailed'), color: 'var(--dsh-error, #ef4444)' };
    default: return { dot: null, text: t('statusUnknown'), color: 'var(--dsh-text-3, #6b6b6b)' };
  }
}

function getEventKindDisplay(t, kind) {
  switch (kind) {
    case 'session_event': return { icon: IconMessageSquare, label: t('eventSession'), color: 'var(--dsh-text-1, #e4e4e7)' };
    case 'task_queued': return { icon: IconTerminal, label: t('eventQueued'), color: 'var(--dsh-warning, #f59e0b)' };
    case 'task_dispatched': return { icon: IconTerminal, label: t('eventDispatched'), color: 'var(--dsh-info, #3b82f6)' };
    case 'job_start': return { icon: IconTerminal, label: t('eventJobStart'), color: 'var(--dsh-info, #3b82f6)' };
    case 'job_end': return { icon: IconTerminal, label: t('eventJobEnd'), color: 'var(--dsh-success, #22c55e)' };
    case 'first_heartbeat': return { icon: IconTerminal, label: t('eventHeartbeat'), color: 'var(--dsh-success, #22c55e)' };
    case 'dispatch_failed': return { icon: IconTerminal, label: t('eventDispatchFailed'), color: 'var(--dsh-error, #ef4444)' };
    case 'plugin_status': return { icon: IconTool, label: t('eventPlugin'), color: 'var(--dsh-text-2, #a1a1aa)' };
    default: return { icon: IconMessageSquare, label: kind, color: 'var(--dsh-text-2, #a1a1aa)' };
  }
}

// ── API: GitHub Issues ────────────────────────────────────────────────────────

async function fetchGitHubIssues(afterCursor = null) {
  const params = new URLSearchParams({
    state: 'all',
    labels: TASK_LABEL,
    per_page: '30',
    sort: 'updated',
    direction: 'desc',
  });
  if (afterCursor) params.set('after', afterCursor);

  const url = `${GITHUB_API_BASE}/repos/${GITHUB_REPO}/issues?${params.toString()}`;
  const response = await fetch(url, {
    headers: {
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      // Без токена — публичный доступ, лимит 60 req/hr. С токеном — выше.
    },
  });
  if (!response.ok) {
    throw new Error(`GitHub API ${response.status}: ${response.statusText}`);
  }
  const data = await response.json();
  // Фильтруем PR (у них есть pull_request поле)
  return data.filter(issue => !issue.pull_request);
}

// ── API: Журнал (cf-worker) ────────────────────────────────────────────────────

async function fetchJournalEvents(taskId, after = 0, limit = JOURNAL_PAGE_SIZE) {
  const url = JOURNAL_QUERY + encodeURIComponent(taskId) + `&limit=${limit}&after=${after}`;
  const response = await fetch(url, {
    credentials: 'include',
    headers: { accept: 'application/json' },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('json')) {
    throw new Error('ответ не JSON — это не журнал');
  }
  const body = await response.json();
  if (body === null || typeof body !== 'object' || !Array.isArray(body.events)) {
    throw new Error('ответ без массива events — не по контракту журнала');
  }
  return body;
}

async function fetchAllJournalEvents(taskId) {
  const events = [];
  let after = 0;
  for (;;) {
    const page = await fetchJournalEvents(taskId, after, JOURNAL_PAGE_SIZE);
    events.push(...page.events);
    if (page.has_more !== true) break;
    if (typeof page.next_after !== 'number') {
      throw new Error('has_more без числового next_after — не по контракту журнала');
    }
    after = page.next_after;
  }
  return events;
}

// ── Парсинг session_event данных ──────────────────────────────────────────────

/**
 * @param {any} data - данные события session_event
 * @returns {Array<{type: 'think'|'tool_call'|'tool_result'|'assistant'|'user', content: any, meta?: any}>}
 */
function parseSessionEvent(data) {
  const messages = [];
  if (!data || typeof data !== 'object') return messages;

  // session_event приходит от dsh-hands-streamer (задача #69)
  // Структура: { events: [...] } где каждое событие — каноническое SessionEvent
  // Канонические события: turn/start, step/start, agent/request, llm/stream,
  // assistant/chunk, assistant/message, tool/call, tool/result, step/end, turn/end

  if (Array.isArray(data.events)) {
    for (const evt of data.events) {
      if (!evt || typeof evt !== 'object' || !evt.type) continue;
      switch (evt.type) {
        case 'assistant/message':
          messages.push({ type: 'assistant', content: evt.content, meta: evt });
          break;
        case 'tool/call':
          messages.push({ type: 'tool_call', content: { name: evt.name, args: evt.args }, meta: evt });
          break;
        case 'tool/result':
          messages.push({ type: 'tool_result', content: { name: evt.name, result: evt.result, error: evt.error }, meta: evt });
          break;
        case 'llm/stream':
        case 'assistant/chunk':
          // Чанки стрима — пропускаем, ждём полное сообщение
          break;
        case 'agent/request':
          // Можно показать как "think" блок
          messages.push({ type: 'think', content: evt.prompt?.slice(0, 500), meta: evt });
          break;
        default:
          // Другие системные события — можно логировать
          break;
      }
    }
  }
  return messages;
}

// ── UI Компоненты ──────────────────────────────────────────────────────────────

const styles = {
  section: { display: 'flex', flexDirection: 'column', gap: '12px', padding: '8px 0', minWidth: 0 },
  header: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' },
  title: { margin: 0, fontSize: '14px', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px' },
  refreshBtn: { padding: '4px 8px', fontSize: '11px', minWidth: 0 },
  errorBanner: {
    display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap',
    padding: '8px 10px', borderRadius: '6px',
    background: 'var(--dsh-error-bg, rgba(239,68,68,0.15))',
    border: '1px solid var(--dsh-error, #ef4444)',
    color: 'var(--dsh-error, #ef4444)', fontSize: '12px',
  },
  list: { listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: '4px' },
  taskRow: {
    display: 'flex', flexDirection: 'column', gap: '6px',
    padding: '10px', borderRadius: '8px',
    background: 'var(--dsh-card-bg, #1e1e1e)',
    border: '1px solid var(--dsh-border, #3e3e42)',
    cursor: 'pointer', transition: 'border-color 0.15s, background 0.15s',
  },
  taskRowHover: { borderColor: 'var(--dsh-primary, #3b82f6)', background: 'var(--dsh-card-hover, #252525)' },
  taskRowSelected: { borderColor: 'var(--dsh-primary, #3b82f6)', boxShadow: '0 0 0 1px var(--dsh-primary, #3b82f6)' },
  taskHeader: { display: 'flex', alignItems: 'flex-start', gap: '8px' },
  taskNumber: { fontFamily: 'var(--dsh-mono, ui-monospace, monospace)', fontSize: '12px', color: 'var(--dsh-primary, #3b82f6)', fontWeight: 600, flexShrink: 0 },
  taskTitle: { flex: 1, fontSize: '13px', lineHeight: 1.4, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  taskMeta: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', fontSize: '11px', color: 'var(--dsh-text-3, #6b6b6b)' },
  assignee: { display: 'flex', alignItems: 'center', gap: '4px', padding: '1px 6px', borderRadius: '4px', background: 'var(--dsh-bg-2, #2a2a2a)', border: '1px solid var(--dsh-border, #3e3e42)' },
  statusBadge: { display: 'flex', alignItems: 'center', gap: '4px', padding: '1px 6px', borderRadius: '4px', fontWeight: 500 },
  expandBtn: { padding: '2px', color: 'var(--dsh-text-3, #6b6b6b)', flexShrink: 0 },
  eventLog: { display: 'flex', flexDirection: 'column', gap: '6px', marginTop: '8px', paddingTop: '8px', borderTop: '1px solid var(--dsh-border, #3e3e42)', maxHeight: '400px', overflowY: 'auto' },
  eventEntry: { display: 'flex', flexDirection: 'column', gap: '4px', padding: '8px', borderRadius: '6px', background: 'var(--dsh-bg-2, #2a2a2a)', border: '1px solid var(--dsh-border, #3e3e42)' },
  eventHeader: { display: 'flex', alignItems: 'center', gap: '8px', fontSize: '11px', color: 'var(--dsh-text-3, #6b6b6b)' },
  eventKind: { display: 'flex', alignItems: 'center', gap: '4px', fontWeight: 500 },
  eventTime: { fontFamily: 'var(--dsh-mono, ui-monospace, monospace)', fontSize: '10px' },
  eventSource: { padding: '1px 4px', borderRadius: '3px', background: 'var(--dsh-bg-3, #333)', fontSize: '10px' },
  eventContent: { fontSize: '12px', lineHeight: 1.5, color: 'var(--dsh-text-1, #e4e4e7)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'var(--dsh-mono, ui-monospace, monospace)' },
  thinkContent: { color: 'var(--dsh-info, #3b82f6)', fontStyle: 'italic', borderLeft: '2px solid var(--dsh-info, #3b82f6)', paddingLeft: '8px', marginLeft: '4px' },
  toolCallContent: { color: 'var(--dsh-warning, #f59e0b)', borderLeft: '2px solid var(--dsh-warning, #f59e0b)', paddingLeft: '8px', marginLeft: '4px' },
  toolResultContent: { color: 'var(--dsh-success, #22c55e)', borderLeft: '2px solid var(--dsh-success, #22c55e)', paddingLeft: '8px', marginLeft: '4px' },
  toolErrorContent: { color: 'var(--dsh-error, #ef4444)', borderLeft: '2px solid var(--dsh-error, #ef4444)', paddingLeft: '8px', marginLeft: '4px' },
  assistantContent: { color: 'var(--dsh-text-1, #e4e4e7)' },
  loadingRow: { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '16px', color: 'var(--dsh-text-3, #6b6b6b)', fontSize: '13px' },
  emptyState: { display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '24px', color: 'var(--dsh-text-3, #6b6b6b)', fontSize: '13px', textAlign: 'center' },
  linkBtn: { display: 'inline-flex', alignItems: 'center', gap: '4px', padding: '2px 6px', fontSize: '11px', borderRadius: '4px', border: '1px solid var(--dsh-border, #3e3e42)', background: 'transparent', color: 'var(--dsh-text-2, #a1a1aa)', cursor: 'pointer' },
  liveIndicator: { display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px', color: 'var(--dsh-success, #22c55e)' },
  liveDot: { width: '6px', height: '6px', borderRadius: '50%', background: 'var(--dsh-success, #22c55e)', animation: 'pulse 1.5s infinite' },
};

const styleSheet = `
@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
`;

// Инъекция стилей при первом рендере
let stylesInjected = false;
function injectStyles() {
  if (stylesInjected || typeof document === 'undefined') return;
  const style = document.createElement('style');
  style.textContent = styleSheet;
  document.head.appendChild(style);
  stylesInjected = true;
}

// ── Компонент строки задачи ────────────────────────────────────────────────────

function TaskRow({ task, isSelected, isExpanded, onClick, onToggleExpand, t }) {
  const statusInfo = getStatusDisplay(t, task.status);
  const updatedAt = new Date(task.issue.updated_at).getTime();
  const assignee = task.issue.assignees?.[0]?.login;

  return h('li', {
    style: {
      ...styles.taskRow,
      ...(isSelected ? styles.taskRowSelected : {}),
      ...(!isSelected && { ':hover': styles.taskRowHover }), // Note: inline styles don't support :hover, handled via onMouseEnter/Leave
    },
    onClick: () => onClick(task.issue.number),
    onMouseEnter: (e) => { if (!isSelected) e.currentTarget.style.borderColor = 'var(--dsh-primary, #3b82f6)'; e.currentTarget.style.background = 'var(--dsh-card-hover, #252525)'; },
    onMouseLeave: (e) => { if (!isSelected) { e.currentTarget.style.borderColor = 'var(--dsh-border, #3e3e42)'; e.currentTarget.style.background = 'var(--dsh-card-bg, #1e1e1e)'; } },
  },
    h('div', { style: styles.taskHeader },
      h('span', { style: styles.taskNumber }, `#${task.issue.number}`),
      h('span', { style: styles.taskTitle, title: task.issue.title }, task.issue.title),
      h('button', {
        style: styles.expandBtn,
        onClick: (e) => { e.stopPropagation(); onToggleExpand(task.issue.number); },
        'aria-label': isExpanded ? t('collapse') : t('expand'),
      }, isExpanded ? h(IconChevronDown, { size: 16 }) : h(IconChevronRight, { size: 16 })),
    ),
    h('div', { style: styles.taskMeta },
      assignee && h('span', { style: styles.assignee }, h(IconMessageSquare, { size: 10 }), assignee),
      h('span', { style: { ...styles.statusBadge, color: statusInfo.color } },
        statusInfo.dot ? h(StateDot, { state: statusInfo.dot, size: 8 }) : null,
        statusInfo.text
      ),
      task.error && h('span', { style: { ...styles.statusBadge, color: 'var(--dsh-error, #ef4444)', fontSize: '10px' } }, t('journalError')),
      h('a', { href: task.issue.html_url, target: '_blank', rel: 'noopener', style: styles.linkBtn, onClick: (e) => e.stopPropagation() },
        h(IconExternalLink, { size: 10 }), t('openIssue')
      ),
      task.issue.pull_request && h('a', { href: task.issue.pull_request.html_url, target: '_blank', rel: 'noopener', style: styles.linkBtn, onClick: (e) => e.stopPropagation() },
        h(IconExternalLink, { size: 10 }), t('openPR')
      ),
    ),
    isExpanded && h('div', { style: styles.eventLog },
      task.loading ? h('div', { style: styles.loadingRow }, t('loadingEvents')) :
      task.error ? h('div', { style: { ...styles.eventEntry, borderColor: 'var(--dsh-error, #ef4444)' } },
        h('div', { style: styles.eventHeader }, t('journalError') + ': ' + task.error),
        h('button', { style: styles.refreshBtn, onClick: () => onClick(task.issue.number) }, h(IconRefreshCw, { size: 10 }), t('retry'))
      ) :
      task.events.length === 0 ? h('div', { style: { ...styles.eventEntry, opacity: 0.6 } }, t('noEvents')) :
      task.events.map((event, idx) => h('div', { key: event.id, style: styles.eventEntry },
        h('div', { style: styles.eventHeader },
          (() => {
            const kindInfo = getEventKindDisplay(t, event.kind);
            return h('span', { style: { ...styles.eventKind, color: kindInfo.color } },
              h(kindInfo.icon, { size: 12 }), kindInfo.label
            );
          })(),
          h('span', { style: styles.eventTime }, formatTimestamp(event.ts)),
          h('span', { style: styles.eventSource }, event.source),
        ),
        h('div', { style: (() => {
          if (event.kind === 'session_event') return styles.assistantContent;
          // Пытаемся распарсить session_event данные
          const parsed = parseSessionEvent(event.data);
          if (parsed.length > 0) {
            // Показываем первое значимое сообщение
            const first = parsed[0];
            if (first.type === 'think') return styles.thinkContent;
            if (first.type === 'tool_call') return styles.toolCallContent;
            if (first.type === 'tool_result') return first.content?.error ? styles.toolErrorContent : styles.toolResultContent;
            return styles.assistantContent;
          }
          return styles.eventContent;
        })() },
          (() => {
            if (event.kind === 'session_event') {
              const parsed = parseSessionEvent(event.data);
              if (parsed.length === 0) return h('span', { style: { color: 'var(--dsh-text-3, #6b6b6b)' } }, t('emptySessionEvent'));
              return parsed.map((msg, mi) => h('div', { key: mi, style: { marginBottom: mi < parsed.length - 1 ? '8px' : 0 } },
                msg.type === 'think' && h('div', { style: styles.thinkContent }, h(IconBrain, { size: 12, style: { display: 'inline-block', marginRight: '4px', verticalAlign: 'middle' } }), t('thinkLabel'), msg.content),
                msg.type === 'tool_call' && h('div', { style: styles.toolCallContent }, h(IconTool, { size: 12, style: { display: 'inline-block', marginRight: '4px', verticalAlign: 'middle' } }), t('toolCallLabel', { name: msg.content.name }), h('pre', { style: { margin: '4px 0', fontSize: '11px', overflow: 'auto' } }, JSON.stringify(msg.content.args, null, 2))),
                msg.type === 'tool_result' && h('div', { style: msg.content.error ? styles.toolErrorContent : styles.toolResultContent }, h(IconTool, { size: 12, style: { display: 'inline-block', marginRight: '4px', verticalAlign: 'middle' } }), t('toolResultLabel', { name: msg.content.name }), msg.content.error ? h('pre', { style: { margin: '4px 0', fontSize: '11px', overflow: 'auto' } }, msg.content.error) : h('pre', { style: { margin: '4px 0', fontSize: '11px', overflow: 'auto' } }, String(msg.content.result).slice(0, 2000))),
                msg.type === 'assistant' && h('div', { style: styles.assistantContent }, h(IconMessageSquare, { size: 12, style: { display: 'inline-block', marginRight: '4px', verticalAlign: 'middle' } }), msg.content)
              ));
            }
            // Обычные системные события
            if (event.data && typeof event.data === 'object') {
              return h('pre', { style: { margin: 0, fontSize: '11px', overflow: 'auto', color: 'var(--dsh-text-2, #a1a1aa)' } }, JSON.stringify(event.data, null, 2));
            }
            return h('span', { style: { color: 'var(--dsh-text-3, #6b6b6b)' } }, String(event.data ?? ''));
          })()
        )
      ))
    )
  );
}

// ── Главный компонент секции ──────────────────────────────────────────────────

function AgentsTasksSection(props) {
  const { t } = props;
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selectedTaskNumber, setSelectedTaskNumber] = useState(null);
  const [expandedTasks, setExpandedTasks] = useState(new Set());
  const [liveConnected, setLiveConnected] = useState(false);
  const wsRef = useRef(null);
  const pollTimersRef = useRef({ tasks: null, journal: null });

  injectStyles();

  const loadTasks = useCallback(async () => {
    try {
      setError(null);
      const issues = await fetchGitHubIssues();
      const taskList = issues.map(issue => ({
        issue,
        status: 'unknown',
        events: [],
        loading: true, // Начинаем загрузку событий сразу
        error: null,
      }));
      setTasks(taskList);
      setLoading(false);

      // Загружаем события журнала для всех задач параллельно (как plugin-manager)
      const settled = await Promise.allSettled(
        taskList.map(task => loadTaskEventsInternal(task.issue.number))
      );
      settled.forEach((result, index) => {
        if (result.status === 'fulfilled') {
          const { events, status } = result.value;
          setTasks(prev => prev.map((t, i) =>
            i === index ? { ...t, events, status, loading: false } : t
          ));
        } else {
          setTasks(prev => prev.map((t, i) =>
            i === index ? { ...t, loading: false, error: result.reason?.message } : t
          ));
        }
      });
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  }, []);

  // Внутренняя функция загрузки событий для задачи (возвращает события и статус)
  const loadTaskEventsInternal = useCallback(async (taskNumber) => {
    const taskIdCandidates = [
      `issue:#${taskNumber}`,
      `task:#${taskNumber}`,
      `#${taskNumber}`,
      String(taskNumber),
    ];

    let events = [];
    let lastError = null;

    for (const taskId of taskIdCandidates) {
      try {
        events = await fetchAllJournalEvents(taskId);
        if (events.length > 0) break;
      } catch (e) {
        lastError = e;
      }
    }

    if (events.length === 0 && lastError) {
      throw lastError;
    }

    // Определяем статус из последних событий задачи
    let status = 'unknown';
    const taskEvents = events.filter(e => e.kind !== 'session_event' && e.kind !== 'plugin_status');
    if (taskEvents.length > 0) {
      const lastTaskEvent = taskEvents[taskEvents.length - 1];
      switch (lastTaskEvent.kind) {
        case 'task_queued': status = 'queued'; break;
        case 'task_dispatched': status = 'dispatched'; break;
        case 'job_start': status = 'running'; break;
        case 'job_end': status = (lastTaskEvent.data?.result === 'fail') ? 'failed' : 'done'; break;
        case 'dispatch_failed': status = 'failed'; break;
      }
    }

    return { events, status };
  }, []);

  const loadTaskEvents = useCallback(async (taskNumber) => {
    setTasks(prev => prev.map(task =>
      task.issue.number === taskNumber ? { ...task, loading: true, error: null } : task
    ));

    try {
      const { events, status } = await loadTaskEventsInternal(taskNumber);
      setTasks(prev => prev.map(task =>
        task.issue.number === taskNumber ? { ...task, events, status, loading: false, error: null } : task
      ));
    } catch (err) {
      setTasks(prev => prev.map(task =>
        task.issue.number === taskNumber ? { ...task, loading: false, error: err.message } : task
      ));
    }
  }, [loadTaskEventsInternal]);

  const handleTaskClick = useCallback((taskNumber) => {
    if (selectedTaskNumber === taskNumber) {
      setSelectedTaskNumber(null);
      return;
    }
    setSelectedTaskNumber(taskNumber);
    setExpandedTasks(prev => new Set([...prev, taskNumber]));
    loadTaskEvents(taskNumber);
  }, [selectedTaskNumber, loadTaskEvents]);

  const handleToggleExpand = useCallback((taskNumber) => {
    setExpandedTasks(prev => {
      const next = new Set(prev);
      if (next.has(taskNumber)) next.delete(taskNumber);
      else next.add(taskNumber);
      return next;
    });
  }, []);

  // Подключение к live-стриму журнала
  const connectLive = useCallback(() => {
    if (wsRef.current) return;
    try {
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${JOURNAL_LIVE}0`);
      wsRef.current = ws;
      ws.onopen = () => setLiveConnected(true);
      ws.onclose = () => { wsRef.current = null; setLiveConnected(false); };
      ws.onmessage = (msg) => {
        try {
          const data = JSON.parse(msg.data);
          if (data.type === 'event') {
            const event = data.event;
            // Обновляем задачу, к которой относится событие
            setTasks(prev => prev.map(task => {
              // Проверяем соответствие task_id
              const matches = task.issue.number.toString() === event.task_id.replace(/^(issue|task):#?/, '');
              if (!matches) return task;
              const newEvents = [...task.events, event];
              // Обновляем статус
              let status = task.status;
              switch (event.kind) {
                case 'task_queued': status = 'queued'; break;
                case 'task_dispatched': status = 'dispatched'; break;
                case 'job_start': status = 'running'; break;
                case 'job_end': status = (event.data?.result === 'fail') ? 'failed' : 'done'; break;
                case 'dispatch_failed': status = 'failed'; break;
              }
              return { ...task, events: newEvents, status };
            }));
          } else if (data.type === 'status') {
            // Статус рук — можно использовать для индикатора
          }
        } catch (e) {
          console.warn('[agents-tasks] live event parse error:', e);
        }
      };
      ws.onerror = () => { wsRef.current = null; setLiveConnected(false); };
    } catch (e) {
      console.warn('[agents-tasks] WebSocket connection failed:', e);
      setLiveConnected(false);
    }
  }, []);

  const disconnectLive = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
      setLiveConnected(false);
    }
  }, []);

  // Эффекты: загрузка, поллинг, live
  useEffect(() => {
    loadTasks();
    const timer = setInterval(loadTasks, TASKS_POLL_INTERVAL_MS);
    pollTimersRef.current.tasks = timer;
    connectLive();
    return () => {
      clearInterval(timer);
      disconnectLive();
    };
  }, [loadTasks, connectLive, disconnectLive]);

  // Автозагрузка событий для выбранной задачи
  useEffect(() => {
    if (selectedTaskNumber && !tasks.find(t => t.issue.number === selectedTaskNumber)?.events.length) {
      loadTaskEvents(selectedTaskNumber);
    }
  }, [selectedTaskNumber, tasks, loadTaskEvents]);

  // Рендер
  if (loading) {
    return h('div', { style: styles.section },
      h('div', { style: styles.loadingRow }, t('loadingTasks'))
    );
  }

  if (error && tasks.length === 0) {
    return h('div', { style: styles.section },
      h('div', { style: styles.errorBanner },
        t('loadError') + ': ' + error,
        h(Button, { variant: 'outline', size: 'sm', onClick: loadTasks }, h(IconRefreshCw, { size: 12 }), t('retry'))
      )
    );
  }

  return h('div', { style: styles.section },
    h('div', { style: styles.header },
      h('h2', { style: styles.title },
        h(IconTerminal, { size: 16 }), t('title')
      ),
      h('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } },
        liveConnected && h('span', { style: styles.liveIndicator },
          h('span', { style: styles.liveDot }), t('live')
        ),
        error && h('span', { style: { fontSize: '11px', color: 'var(--dsh-error, #ef4444)' } }, error),
        h(Button, { variant: 'outline', size: 'sm', onClick: loadTasks }, h(IconRefreshCw, { size: 12 }), t('refresh'))
      )
    ),
    tasks.length === 0 ? h('div', { style: styles.emptyState },
      h(IconMessageSquare, { size: 32, style: { opacity: 0.4 } }),
      t('noTasks'),
      h('p', { style: { margin: 0, fontSize: '12px' } }, t('noTasksHint'))
    ) : h('ul', { style: styles.list, 'aria-label': t('title') },
      tasks.map(task => h(TaskRow, {
        key: task.issue.number,
        task,
        isSelected: selectedTaskNumber === task.issue.number,
        isExpanded: expandedTasks.has(task.issue.number),
        onClick: handleTaskClick,
        onToggleExpand: handleToggleExpand,
        t,
      }))
    )
  );
}

// ── Монтаж ────────────────────────────────────────────────────────────────────

// Пробуем разные слоты: sidebar.section (если есть), settings.section (fallback)
const SIDEBAR_SLOT = 'sidebar.section';
const SETTINGS_SLOT = 'settings.section';

const inject = ['slots', 'locale'];

function apply(ctx) {
  if (typeof document !== 'undefined') injectStyles();

  ctx.effect(
    () => ctx.locale.register('agents.tasks', dictionaries),
    'agents-tasks: dictionaries',
  );

  // Регистрируем в sidebar.section (основной слот для задачи #111)
  ctx.slots.inject(SIDEBAR_SLOT, () => ctx.slots.register({
    name: SIDEBAR_SLOT,
    id: 'agents-tasks',
    order: 10, // высокий приоритет — вверху сайдбара
    label: () => ctx.locale.bind('agents.tasks')('nav'),
    locale: 'agents.tasks',
  }, AgentsTasksSection));

  // Fallback: также регистрируем в settings.section (как plugin-manager)
  ctx.slots.inject(SETTINGS_SLOT, () => ctx.slots.register({
    name: SETTINGS_SLOT,
    id: 'agents-tasks',
    order: 90,
    label: () => ctx.locale.bind('agents.tasks')('nav'),
    locale: 'agents.tasks',
  }, AgentsTasksSection));
}

// ── Словари ────────────────────────────────────────────────────────────────────

const dictionaries = {
  en: {
    nav: 'Agents & Tasks',
    title: 'Agents & Tasks',
    loadingTasks: 'Loading tasks…',
    loadingEvents: 'Loading events…',
    noTasks: 'No tasks found',
    noTasksHint: 'Issues with label "task" will appear here',
    noEvents: 'No events yet',
    expand: 'Expand',
    collapse: 'Collapse',
    openIssue: 'Open Issue',
    openPR: 'Open PR',
    retry: 'Retry',
    refresh: 'Refresh',
    live: 'Live',
    statusQueued: 'Queued',
    statusDispatched: 'Dispatched',
    statusRunning: 'Running',
    statusDone: 'Done',
    statusFailed: 'Failed',
    statusUnknown: 'Unknown',
    eventSession: 'Session',
    eventQueued: 'Queued',
    eventDispatched: 'Dispatched',
    eventJobStart: 'Job Started',
    eventJobEnd: 'Job Ended',
    eventHeartbeat: 'First Heartbeat',
    eventDispatchFailed: 'Dispatch Failed',
    eventPlugin: 'Plugin',
    thinkLabel: 'Think: ',
    toolCallLabel: 'Tool Call: ',
    toolResultLabel: 'Tool Result: ',
    loadError: 'Failed to load',
    journalError: 'Journal unavailable',
    emptySessionEvent: 'Empty session event',
  },
  zh: {
    nav: '智能体与任务',
    title: '智能体与任务',
    loadingTasks: '加载任务中…',
    loadingEvents: '加载事件中…',
    noTasks: '未找到任务',
    noTasksHint: '带有 "task" 标签的 Issue 将显示在这里',
    noEvents: '暂无事件',
    expand: '展开',
    collapse: '折叠',
    openIssue: '打开 Issue',
    openPR: '打开 PR',
    retry: '重试',
    refresh: '刷新',
    live: '实时',
    statusQueued: '排队中',
    statusDispatched: '已分发',
    statusRunning: '运行中',
    statusDone: '已完成',
    statusFailed: '失败',
    statusUnknown: '未知',
    eventSession: '会话',
    eventQueued: '已排队',
    eventDispatched: '已分发',
    eventJobStart: '作业开始',
    eventJobEnd: '作业结束',
    eventHeartbeat: '首次心跳',
    eventDispatchFailed: '分发失败',
    eventPlugin: '插件',
    thinkLabel: '思考: ',
    toolCallLabel: '工具调用: ',
    toolResultLabel: '工具结果: ',
    loadError: '加载失败',
    journalError: '日志不可用',
    emptySessionEvent: '空会话事件',
  },
  ru: {
    nav: 'Агенты и задачи',
    title: 'Агенты и задачи',
    loadingTasks: 'Загрузка задач…',
    loadingEvents: 'Загрузка событий…',
    noTasks: 'Задачи не найдены',
    noTasksHint: 'Issue с меткой "task" появятся здесь',
    noEvents: 'Событий пока нет',
    expand: 'Развернуть',
    collapse: 'Свернуть',
    openIssue: 'Открыть Issue',
    openPR: 'Открыть PR',
    retry: 'Повторить',
    refresh: 'Обновить',
    live: 'Онлайн',
    statusQueued: 'В очереди',
    statusDispatched: 'Распределена',
    statusRunning: 'Выполняется',
    statusDone: 'Готово',
    statusFailed: 'Ошибка',
    statusUnknown: 'Неизвестно',
    eventSession: 'Сессия',
    eventQueued: 'В очереди',
    eventDispatched: 'Распределена',
    eventJobStart: 'Запуск job',
    eventJobEnd: 'Завершение job',
    eventHeartbeat: 'Первый heartbeat',
    eventDispatchFailed: 'Ошибка диспетча',
    eventPlugin: 'Плагин',
    thinkLabel: 'Размышление: ',
    toolCallLabel: 'Вызов инструмента: ',
    toolResultLabel: 'Результат инструмента: ',
    loadError: 'Ошибка загрузки',
    journalError: 'Журнал недоступен',
    emptySessionEvent: 'Пустое событие сессии',
  },
};
//#endregion