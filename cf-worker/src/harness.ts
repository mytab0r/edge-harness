import { DurableObject } from "cloudflare:workers";
import { AUTOMATIONS, DSH_EDGE_UPDATE, GITHUB, HEARTBEAT, LIMITS, SESSION } from "./config";
import { msg } from "./messages";
import { matchRoute } from "./api-spec";
import { redact } from "./redact";
import {
  type AutomationConfig,
  digestPeriod,
  isValidAutomationId,
  journalTriggerDue,
  parseAutomationConfig,
  runTaskId,
  scheduleDue,
} from "./automations";

// ── Inbox constants (from config) ──────────────────────────────────────────────────────
const MESSAGE_MAX_CHARS = LIMITS.messageMaxChars;
const MESSAGE_PROCESS_MAX = LIMITS.messageProcessMax;
const MESSAGE_GROUP_WINDOW_MS = LIMITS.messageGroupWindowMs;
const MESSAGE_MAX_ATTEMPTS = LIMITS.messageMaxAttempts;

// Командные слова директив — ОДИН список: классификатор (явный префикс и
// глагольные формы) и чистка заголовка issue строятся из него; новый глагол
// добавляется в одном месте, расхождение «классификатор знает — заголовок не
// чистит» невозможно (ревью head a9c4cce).
export const DIRECTIVE_WORDS = [
  "task", "issue", "задача", "сделай", "создай", "добавь", "исправь",
  "проверь", "проанализируй", "исследуй", "напиши", "обнови",
] as const;
const DIRECTIVE_WORDS_RE = DIRECTIVE_WORDS.join("|");
const TITLE_PREFIX_RE = new RegExp(`^[\\/]\\s*(?:${DIRECTIVE_WORDS_RE})\\s*`, "i");

// ── Схема данных ────────────────────────────────────────────────────────────────────
//
// events    — журнал. Идемпотентность по UNIQUE(task_id, seq): повторная доставка батча
//             после сетевой ошибки не двоит записи (design.md, «Идемпотентность»).
//             seq у job'а — положительные, у системных событий — отрицательные (−1, −2…).
// tasks     — очередь. latency_ms — замер «repository_dispatch → первый heartbeat»,
//             ради которого всё и делалось (tasks.md, задача 5).
// heartbeat — одна строка (id=1): последняя живая отметка рук.

const SCHEMA = [
  `CREATE TABLE IF NOT EXISTS events (
     id      INTEGER PRIMARY KEY AUTOINCREMENT,
     task_id TEXT NOT NULL,
     seq     INTEGER NOT NULL,
     ts      INTEGER NOT NULL,
     source  TEXT NOT NULL,
     kind    TEXT NOT NULL,
     data    TEXT,
     UNIQUE(task_id, seq)
   )`,
  `CREATE INDEX IF NOT EXISTS events_by_id ON events(id)`,
  `CREATE TABLE IF NOT EXISTS tasks (
     id          TEXT PRIMARY KEY,
     created_ts  INTEGER NOT NULL,
     dispatch_ts INTEGER,
     latency_ms  INTEGER,
     status      TEXT NOT NULL DEFAULT 'queued'
   )`,
  // Квота rows_read (#320): без индекса #status() сканировал ВСЮ историческую
  // таблицу tasks на каждый heartbeat (раз в 20 с при работающей job), чтобы
  // найти зависшие dispatched-задачи. Индекс сводит эту выборку к текущим
  // dispatched-строкам — их всегда мало, в отличие от истории.
  `CREATE INDEX IF NOT EXISTS tasks_status_dispatch ON tasks(status, dispatch_ts)`,
  `CREATE TABLE IF NOT EXISTS heartbeat (
     id     INTEGER PRIMARY KEY CHECK (id = 1),
     ts     INTEGER NOT NULL,
     job_id TEXT NOT NULL
   )`,
  // Пульс оркестрации (issue #269): одна строка (id=1) — исход ПОСЛЕДНЕЙ попытки
  // alarm() дёрнуть workflow_dispatch оркестратора. Сам будильник не может застрять
  // (setAlarm перезакладывается первой строкой alarm()), но раньше исход dispatch'а
  // (не настроен / сеть упала / GitHub отклонил не-204) тонул в console.log, который
  // никто не смотрит между дедами. Эта строка — видимый артефакт вместо тихой лжи.
  // dispatch_ok — принят ли dispatch GitHub'ом (HTTP 204). Это НЕ доказательство
  // запуска (docs/research/21-github-actions.md: файл workflow не на default
  // branch тоже отвечает 204 и ничего не запускает) — запуск подтверждает
  // last_run_id/run_confirmed: id последнего run'а orchestra.yml на момент ЭТОГО
  // тика (баланс для проверки на СЛЕДУЮЩЕМ) и тройное состояние подтверждения
  // ПРЕДЫДУЩЕГО dispatch'а (NULL — рано судить, 0 — запуск не появился, 1 — появился).
  `CREATE TABLE IF NOT EXISTS pulse (
     id            INTEGER PRIMARY KEY CHECK (id = 1),
     ts            INTEGER NOT NULL,
     dispatch_ok   INTEGER NOT NULL,
     detail        TEXT,
     last_run_id   INTEGER,
     run_confirmed INTEGER
   )`,
  `CREATE TABLE IF NOT EXISTS messages (
     id            INTEGER PRIMARY KEY AUTOINCREMENT,
     ts            INTEGER NOT NULL,
     source        TEXT NOT NULL,           -- 'telegram', 'api', etc.
     source_msg_id TEXT,                    -- original message ID from source
     chat_id       TEXT,                    -- chat/channel identifier
     sender_id     TEXT,                    -- sender identifier
     sender_name   TEXT,                    -- sender display name
     text          TEXT NOT NULL,           -- message text
     kind          TEXT NOT NULL DEFAULT 'raw',  -- 'raw', 'directive', 'chat', 'doc_edit', 'system'
     priority      INTEGER NOT NULL DEFAULT 0,   -- higher = more urgent
     status        TEXT NOT NULL DEFAULT 'new',  -- 'new', 'processing', 'done', 'failed', 'ignored'
     result        TEXT,                    -- JSON: {issue_number, issue_url, error, note, ...}
     grouped_with  INTEGER,                 -- message id this was grouped with
     attempts      INTEGER NOT NULL DEFAULT 0,   -- попыток обработки (кап — LIMITS.messageMaxAttempts)
     processing_ts INTEGER,                 -- момент атомарного захвата; NULL вне processing
     processed_ts  INTEGER,
     UNIQUE(source, source_msg_id)          -- идемпотентность приёма
   )`,
  `CREATE INDEX IF NOT EXISTS messages_by_ts ON messages(ts DESC)`,
  `CREATE INDEX IF NOT EXISTS messages_by_status ON messages(status)`,
  `CREATE INDEX IF NOT EXISTS messages_by_kind ON messages(kind)`,
  `CREATE INDEX IF NOT EXISTS messages_by_group ON messages(grouped_with)`,
  // Автоматизации (#116): конфиг — единственное место правды, правится из морды
  // (секция «Автоматизации» через прокси /api/harness/*). last_fired_ts — троттл
  // триггеров-расписаний: обновляется при любом исходе запуска, чтобы упавший
  // dispatch не штурмовался каждым 15-минутным пульсом.
  `CREATE TABLE IF NOT EXISTS automations (
     id            TEXT PRIMARY KEY,
     enabled       INTEGER NOT NULL,
     config        TEXT NOT NULL,
     created_ts    INTEGER NOT NULL,
     updated_ts    INTEGER NOT NULL,
     last_fired_ts INTEGER
   )`,
];

export interface EventRow {
  id: number;
  task_id: string;
  seq: number;
  ts: number;
  source: string;
  kind: string;
  data: unknown;
}

export interface TaskRow {
  id: string;
  created_ts: number;
  dispatch_ts: number | null;
  latency_ms: number | null;
  status: "queued" | "dispatched" | "running" | "done" | "failed";
}

/** Пульс оркестрации (#269): исход последней попытки alarm() дёрнуть orchestra.
 *  dispatch_ok — принят ли GitHub'ом dispatch (HTTP 204) — это ПРИЁМ, не запуск
 *  (см. docstring pulseHealthy). run_confirmed — появился ли реальный run
 *  orchestra.yml после ПРЕДЫДУЩЕГО принятого dispatch'а: null (рано судить,
 *  ещё не было следующего тика или fetch не удался), true/false. */
export interface PulseStatus {
  ts: number;
  dispatch_ok: boolean;
  detail: string | null;
  run_confirmed: boolean | null;
}

/** То же самое плюс last_run_id — baseline id run'а orchestra.yml, замеченный
 *  на момент записи этой строки, для сверки на следующем тике
 *  (confirmPreviousRun). Внутреннее представление хранилища, наружу (Status)
 *  не течёт — фронту это число не нужно. */
interface StoredPulse extends PulseStatus {
  last_run_id: number | null;
}

export interface MessageRow {
  id: number;
  ts: number;
  source: string;
  source_msg_id: string | null;
  chat_id: string | null;
  sender_id: string | null;
  sender_name: string | null;
  text: string;
  kind: string;
  priority: number;
  status: string;
  result: string | null;
  grouped_with: number | null;
  attempts: number;
  processing_ts: number | null;
  processed_ts: number | null;
}

/** Результат обработки одного сообщения инбокса. */
export interface MessageProcessResult {
  message_id: number;
  action: "issue_created" | "issue_failed" | "issue_retry" | "parked" | "ignored" | "skipped" | "not_found";
  issue_number?: number;
  issue_url?: string;
  error?: string;
  attempts?: number;
  secrets_redacted?: boolean;
}

export interface Status {
  now: number;
  heartbeat_fresh_ms: number;
  hands_alive: boolean;
  last_heartbeat: { ts: number; job_id: string } | null;
  tasks: Record<TaskRow["status"], number>;
  last_event_id: number;
  /** Watchdog (#7): задачи, висящие в dispatched дольше порога без рук. */
  stale_dispatch: { count: number; oldest_age_ms: number | null };
  last_pulse: PulseStatus | null;
  /** Предвычислено сервером — фронту не нужно знать пороги (те же соображения,
   *  что у hands_alive). См. pulseHealthy. */
  pulse_healthy: boolean;
  /** Предвычислено сервером (#303, находка ревью): фронт не должен знать
   *  литерал-сентинел notConfiguredDetail — переименование в одном месте
   *  (config.ts) не должно требовать второй правки в app.js. См. pulseNotConfigured. */
  pulse_not_configured: boolean;
  /** Предвычислено сервером (#303, вторая находка ревью того же PR): unhealthy
   *  по причине «alarm подвис» — единственная ветка pulseHealthy(), где
   *  last_pulse.detail остаётся null (dispatch ЭТОГО тика был не при чём,
   *  просто следующий тик не пришёл). Раньше фронт в этой ветке рендерил
   *  сырой detail и получал буквальную строку "null" — тот же класс, что и
   *  pulse_not_configured выше, просто вторая ветка того же if/else. См.
   *  pulseStale. */
  pulse_stale: boolean;
  /** Inbox: сообщения по статусам. */
  messages: Record<string, number>;
}

/** Чистая функция порога — проверяется тестом отдельно от хранилища. */
/** Чистое решение самообновления морды (#73): проводка (fetch/storage/dispatch)
 *  остаётся тонкой в #checkDshEdgeUpdate, а все ветки логики крутятся тестами. */
export function dshEdgeUpdateDecision(
  deployed: string,
  latest: string,
  lastAttemptTs: number | undefined,
  now: number,
): "dispatch" | "throttled" | "quiet" {
  if (lastAttemptTs !== undefined && now - lastAttemptTs < DSH_EDGE_UPDATE.throttleMs) {
    return "throttled";
  }
  return deployed === latest ? "quiet" : "dispatch";
}

/**
 * Один dispatch-тик оркестратора (issue #269). Никогда не бросает — исход
 * упакован в объект: HTTP 204 от GitHub доказывает только ПРИЁМ dispatch'а,
 * не запуск (docs/research/21-github-actions.md, «🔴 Ловушка: файл обязан
 * лежать на default branch» — workflow в feature-ветке даёт 204 и ничего не
 * происходит: «успешный HTTP-код здесь не является доказательством запуска»).
 * До этой правки статус ответа вообще не проверялся: 403 (вторичный
 * rate-limit GitHub, 500/час) или 404 (протухший токен/репо) тонули как
 * «успех», потому что fetch() не бросает на HTTP-ошибках сам по себе —
 * бросает только сеть. Настоящее доказательство запуска — появление нового
 * run'а orchestra.yml, это проверяет confirmPreviousRun() на следующем тике
 * (см. alarm()). fetchImpl инъецируется, чтобы тест не ходил в реальный GitHub.
 */
export async function attemptOrchestraDispatch(
  token: string,
  repo: string,
  fetchImpl: typeof fetch,
): Promise<{ ok: boolean; detail: string | null }> {
  try {
    const res = await fetchImpl(
      `${GITHUB.apiBase}/repos/${repo}/actions/workflows/${HEARTBEAT.orchestraWorkflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    if (res.status !== 204) {
      return { ok: false, detail: `dispatch отклонён: ${res.status}` };
    }
    return { ok: true, detail: null };
  } catch (error) {
    return { ok: false, detail: error instanceof Error ? error.message : String(error) };
  }
}

/**
 * id последнего run'а orchestra.yml — единственный способ отличить «GitHub
 * принял dispatch» от «GitHub принял dispatch и правда что-то запустил»
 * (issue #269, находка ревью). `null`, если запрос не удался или run'ов ещё
 * не было вовсе — вызывающий код обязан считать это «рано/нечем судить», а не
 * «run не появился». fetchImpl инъецируется — тест не ходит в реальный GitHub.
 *
 * `event=workflow_dispatch` в запросе обязателен (#303, находка ревью): у
 * orchestra.yml есть и другие триггеры — job `contract` на каждый
 * pull_request (пуш, навешивание метки) и `schedule`. Без фильтра последний
 * run почти всегда чужой (не наш dispatch), id почти всегда отличается от
 * baseline — confirmPreviousRun() почти всегда врёт true именно тогда, когда
 * дело плохо: 204 есть, а нашего run'а нет. Фильтр по событию делает
 * baseline/latest сравнением id внутри ОДНОЙ и той же выборки run'ов
 * workflow_dispatch, где различие действительно означает «появился новый».
 */
export async function fetchLatestOrchestraRunId(
  token: string,
  repo: string,
  fetchImpl: typeof fetch,
): Promise<number | null> {
  try {
    const res = await fetchImpl(
      `${GITHUB.apiBase}/repos/${repo}/actions/workflows/${HEARTBEAT.orchestraWorkflow}/runs?event=workflow_dispatch&per_page=1`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
      },
    );
    if (!res.ok) return null;
    const body = (await res.json()) as { workflow_runs?: Array<{ id: number }> };
    return body.workflow_runs?.[0]?.id ?? null;
  } catch {
    return null;
  }
}

/**
 * Подтверждение ПРЕДЫДУЩЕГО принятого dispatch'а (issue #269, находка ревью):
 * baseline — id последнего run'а orchestra.yml, замеченный ДО того dispatch'а;
 * latest — id последнего run'а на момент ЭТОГО тика. Появился новый run —
 * baseline и latest различаются (id только растут, поэтому строгое сравнение
 * без гонки за направлением). `null` — baseline или latest неизвестны (fetch
 * не удался, либо это самый первый тик и подтверждать ещё нечего) — тогда
 * решение «рано судить», а не «не подтверждено».
 */
export function confirmPreviousRun(baseline: number | null, latest: number | null): boolean | null {
  if (baseline === null || latest === null) return null;
  return latest !== baseline;
}

/**
 * detail, который попадает в pulse.detail (#303, находка ревью): dispatch ЭТОГО
 * тика принят (result.ok, detail: null — attemptOrchestraDispatch честно не
 * пишет причину, потому что причины нет), но run_confirmed о ПРЕДЫДУЩЕМ
 * dispatch'е — false. pulseHealthy() уже гасит бейдж в этом случае, но само
 * поле detail без этой подмены остаётся null — фронт (cf-worker/public/assets/app.js,
 * t("pulse.unhealthy", { detail })) отрендерит буквальную строку "null", что
 * неотличимо от честной поломки dispatch'а. Реальная ошибка ЭТОГО тика
 * (result.ok === false, detail уже заполнен) в приоритете — это не тот
 * случай, что «ok сейчас, но не подтвердилось раньше».
 */
export function pulseDetailForRecord(
  result: { ok: boolean; detail: string | null },
  runConfirmed: boolean | null,
): string | null {
  if (result.ok && runConfirmed === false) return HEARTBEAT.runNotConfirmedDetail;
  return result.detail;
}

export function handsAreAlive(now: number, lastHeartbeatTs: number | null): boolean {
  return lastHeartbeatTs !== null && now - lastHeartbeatTs < LIMITS.heartbeatFreshMs;
}

/**
 * Класс ошибки хранилища DO по тексту исключения (#320). Cloudflare не
 * документирует точную форму отказа при исчерпании суточной квоты
 * rows_read/rows_written (docs/research/20-cloudflare-free.md, «Durable
 * Objects на Free»/«Что не подтверждено») — известна только фраза из письма
 * «further operations of that type will fail with an error» и названия
 * операций (rows_read, rows_written). НЕ ПОДТВЕРЖДЕНО: точный текст
 * исключения workerd не проверен на живом отказе — ловим широко по
 * ключевым словам, а не по одной строке, и называем находку своим именем
 * вместо общего «internal»/«пульс не бьётся».
 */
export function classifyStorageError(message: string): "quota_exceeded" | "unknown" {
  return /rows[_ ]?(read|written)|quota|exceeded.*(limit|tier)/i.test(message) ? "quota_exceeded" : "unknown";
}

/**
 * «Возможности нет» (секреты не заданы) — единственное место, что читает
 * сентинел HEARTBEAT.notConfiguredDetail (#303, находка ревью): литерал жил
 * ещё и во фронте (app.js: status.last_pulse.detail === "not_configured")
 * второй копией строки в обход заявленного «одна константа вместо двух
 * копий» из docstring notConfiguredDetail — переименование сентинела молча
 * ломало бы обе ветки бейджа во фронте, а pulse_healthy оставался бы true.
 * Фронт теперь получает готовый флаг (pulse_not_configured в Status) и
 * вообще не знает про сам литерал сервера.
 */
export function pulseNotConfigured(lastPulse: PulseStatus | null): boolean {
  return lastPulse !== null && lastPulse.detail === HEARTBEAT.notConfiguredDetail;
}

/**
 * Пульс оркестрации (#269) здоров, если:
 *  - тика ещё не было (холодный старт DO — не повод кричать до первой попытки);
 *  - секретов нет («возможности нет» — конфигурация, а не поломка, отличать от
 *    «возможность есть, но сломана» обязательно: лечатся по-разному);
 *  - GitHub принял dispatch (HTTP 204) И это не единственное доказательство —
 *    run_confirmed не должен быть явно false (принят, но запуск не появился —
 *    находка ревью: 204 доказывает приём, не запуск; частый виновник —
 *    workflow-файл не на default branch, docs/research/21-github-actions.md);
 *  - последняя попытка была не раньше двух тактов назад — один пропущенный
 *    такт не тревога, а вот подвисший на дольше alarm — уже да.
 */
export function pulseHealthy(now: number, lastPulse: PulseStatus | null): boolean {
  if (lastPulse === null) return true;
  if (pulseNotConfigured(lastPulse)) return true;
  if (!lastPulse.dispatch_ok) return false;
  if (lastPulse.run_confirmed === false) return false;
  return now - lastPulse.ts < HEARTBEAT.selfOrchestrationMs * 2;
}

/**
 * unhealthy по причине «подвис alarm» (#303, вторая находка ревью): последний
 * тик прошёл успешно (dispatch_ok, run_confirmed не false), но сам тик был
 * давно — дольше 2×selfOrchestrationMs. Это ЕДИНСТВЕННАЯ ветка pulseHealthy(),
 * где last_pulse.detail остаётся null (attemptOrchestraDispatch пишет
 * detail: null именно на успехе) — dispatch ни при чём, просто следующий
 * тик не пришёл. pulseDetailForRecord() здесь ничего не подменяет (это не
 * его случай — там речь про run_confirmed именно ЭТОГО тика), поэтому фронт
 * не может достать причину из detail и обязан получить готовый флаг, как и
 * для pulse_not_configured. Первые два if повторяют начало pulseHealthy —
 * умышленно: stale относится ТОЛЬКО к третьей ветке (аларм подвис), не к
 * «возможности нет» и не к «dispatch/run сломан» — те уже несут свой
 * человекочитаемый detail и не должны попадать в эту ветку тоже.
 */
export function pulseStale(now: number, lastPulse: PulseStatus | null): boolean {
  if (lastPulse === null) return false;
  if (pulseNotConfigured(lastPulse)) return false;
  if (!lastPulse.dispatch_ok) return false;
  if (lastPulse.run_confirmed === false) return false;
  return now - lastPulse.ts >= HEARTBEAT.selfOrchestrationMs * 2;
}
/** Чистое решение ватчдога инбокса: сообщение висит в processing дольше порога —
 *  изолят умер посреди внешнего вызова, пульс вернёт его в new. */
export function messageStuck(processingTs: number | null, now: number): boolean {
  return processingTs !== null && now - processingTs >= LIMITS.messageStuckProcessingMs;
}

/** Telegram шлёт идентификаторы числами (update_id, message.id, from.id, chat.id):
 *  без приведения к строке реальный апдейт падает мимо UNIQUE-идемпотентности. */
export function asString(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** Безопасное чтение вложенного объекта (message/from/chat Telegram-update). */
export function asObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

/** Итог создания issue для директивы. retryable=false — повторять бессмысленно. */
type IssueOutcome =
  | { ok: true; number: number; url: string; redacted: boolean }
  | { ok: false; retryable: boolean; error: string };

/** Сравнение подписей без утечки длины совпадения по времени. */
export function constantTimeEqual(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

// ── Ошибки API ──────────────────────────────────────────────────────────────────────

class ApiError extends Response {
  constructor(status: number, code: Parameters<typeof msg>[0], params: Record<string, string | number> = {}) {
    super(JSON.stringify({ error: { code, message: msg(code, params) } }), {
      status,
      headers: { "content-type": "application/json" },
    });
  }
}

/**
 * Ответ на неизвестную ошибку, дошедшую до общего catch `#fetch()` (#320, спека
 * 5.1). Раньше `classifyStorageError` тестировался только сам по себе — маппинг
 * "quota_exceeded" → код `storage_quota_exceeded` (а не общий `internal`) и сама
 * сборка ответа не проверялись тестом (находка ревью PR #321). Вынесено отдельной
 * функцией, чтобы гонять именно тот код, который реально уходит клиенту, без
 * необходимости реально исчерпывать суточную квоту DO в тесте.
 */
export function storageErrorResponse(detail: string): Response {
  const code = classifyStorageError(detail) === "quota_exceeded" ? "storage_quota_exceeded" : "internal";
  return new ApiError(500, code, { detail });
}

// ── Durable Object ──────────────────────────────────────────────────────────────────

export class Harness extends DurableObject<Env> {
  #sql: SqlStorage;

  // Кэш агрегата «сколько задач в каждом статусе» (#320, рецепт rows_read).
  // Не зависит от времени — меняется ТОЛЬКО записью в tasks, поэтому
  // инвалидация по месту записи корректна (в отличие от stale_dispatch,
  // который зависит от текущего момента и обязан читаться заново). null —
  // «грязно», следующий #taskCounts() пересчитает одним GROUP BY.
  #taskCountsCache: Record<TaskRow["status"], number> | null = null;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.#sql = ctx.storage.sql;
    for (const stmt of SCHEMA) this.#sql.exec(stmt);
    void this.#ensureHeartbeat();
  }

  /**
   * Пульс оркестрации: если будильника нет — закладываем ближайший. Обработчик
   * alarm() сначала перезакладывает следующий тик, потом дёргает workflow_dispatch:
   * падение dispatch не может убить цепочку. Alarm переживает гибернацию
   * и стоит 1 request — комфортный режим Free (docs/research/20).
   */
  #ensureHeartbeat(): void {
    this.ctx.storage
      .getAlarm()
      .then((scheduled) => {
        if (scheduled === null) {
          return this.ctx.storage.setAlarm(Date.now() + HEARTBEAT.selfOrchestrationFirstMs);
        }
      })
      .catch((error) => {
        // #320: единственная пересборка цепочки после гибели пульса (см. alarm()) —
        // молчание здесь тот же класс «немое падение пульса», который PR закрывает
        // в alarm(). Здесь падать некуда (конструктор не может ждать промис), но
        // хотя бы громко в лог, тем же классификатором, что и alarm().
        const detail = error instanceof Error ? error.message : String(error);
        console.error(`ensureHeartbeat: упал (${classifyStorageError(detail)}): ${detail}`);
      });
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      return await this.#route(request, url);
    } catch (error) {
      if (error instanceof ApiError) return error;
      // Неизвестная ошибка — громко, с текстом в ответе: silent-wrong дороже падения.
      // Квоту storage называем по имени (#320) — иначе отказ DO SQLite виден
      // как обычный «internal», и никто не свяжет его с исчерпанием rows_read.
      const detail = error instanceof Error ? error.message : String(error);
      return storageErrorResponse(detail);
    }
  }

  #rows(cursor: SqlStorageCursor<Record<string, SqlStorageValue>>): Record<string, SqlStorageValue>[] {
    return cursor.toArray() as Record<string, SqlStorageValue>[];
  }

  async #route(request: Request, url: URL): Promise<Response> {
    // Токен в query больше не существует как механизм: он попадает в логи CF и
    // историю браузера. Отклоняем громко и отдельным кодом — не путать с 401
    // («куки нет») и не принимать молча даже совпадающий по значению токен.
    if (url.searchParams.has("token")) {
      throw new ApiError(400, "query_token_removed");
    }

    // Роутинг табличный: метод+путь ищутся в api-spec.json. Добавить маршрут можно
    // только через спеку — «объявлен в спеке, но не подключён» и наоборот невозможны.
    const matched = matchRoute(request.method, url.pathname);
    // Webhook автоматизации (#116) аутентифицируется подписью, а не кукой/Bearer —
    // у внешнего отправителя их нет и быть не должно. Спека помечает маршрут
    // auth: false, и он обслуживается ДО общей аутентификации. Остальное поведение
    // прежнее: без авторизации — 401 и на неизвестный путь тоже (не раскрываем
    // маршруты непрошеным).
    if (matched?.route.name === "webhook") {
      return this.#handleWebhook(request, matched.rest);
    }
    if (!(await this.#authorized(request))) {
      throw new ApiError(401, "unauthorized");
    }
    if (!matched) {
      throw new ApiError(404, "not_found", { method: request.method, path: url.pathname });
    }
    const { route } = matched;

    if (route.name === "status") {
      return this.#json(this.#status());
    }
    if (route.name === "events" && request.method === "POST") {
      return this.#postEvents(request);
    }
    if (route.name === "events" && request.method === "GET") {
      return this.#getEvents(url);
    }
    if (route.name === "eventsLive") {
      if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
        throw new ApiError(400, "need_websocket_upgrade");
      }
      return this.#openLiveSocket(url);
    }
    if (route.name === "tasks" && request.method === "POST") {
      return this.#postTask(request);
    }
    if (route.name === "tasks" && request.method === "GET") {
      return this.#json({ tasks: this.#recentTasks() });
    }
    if (route.name === "task") {
      const id = matched.rest;
      const task = id ? this.#task(id) : null;
      if (!task) throw new ApiError(404, "task_not_found", { task_id: id });
      return this.#json({ task });
    }
    if (route.name === "heartbeat") {
      return this.#postHeartbeat(request);
    }
    if (route.name === "session" && request.method === "POST") {
      return this.#issueSession();
    }
    if (route.name === "session" && request.method === "DELETE") {
      return this.#dropSession();
    }
    if (route.name === "messagesIngest") {
      return this.#postMessageIngest(request);
    }
    if (route.name === "messages" && request.method === "GET") {
      return this.#getMessages(url);
    }
    if (route.name === "messages" && request.method === "POST") {
      return this.#postMessage(request);
    }
    if (route.name === "message") {
      const id = matched.rest;
      const message = id ? this.#message(id) : null;
      if (!message) throw new ApiError(404, "message_not_found", { message_id: id });
      return this.#json({ message });
    }
    if (route.name === "messagesProcess") {
      return this.#processMessages(request);
    }
    if (route.name === "automations") {
      return this.#json({ automations: this.#listAutomations() });
    }
    if (route.name === "automation" && request.method === "PUT") {
      return this.#putAutomation(request, matched.rest);
    }
    if (route.name === "automation" && request.method === "DELETE") {
      return this.#deleteAutomation(matched.rest);
    }
    throw new ApiError(404, "not_found", { method: request.method, path: url.pathname });
  }

  // ── Аутентификация ────────────────────────────────────────────────────────────────

  // Два равноправных способа, ни одного токена в URL:
  //   job (scripts/hands)     — Authorization: Bearer <HANDS_TOKEN>;
  //   браузер                 — сессионная кука: подпись HMAC(SESSION_SECRET)
  //                             над сроком жизни; сам HANDS_TOKEN в браузер не
  //                             возвращается и там не хранится (issue #5).
  async #authorized(request: Request): Promise<boolean> {
    const expected = this.env.HANDS_TOKEN;
    if (!expected) return false; // секрет не задан: «возможности нет», см. ответ /api/status
    const header = request.headers.get("Authorization");
    if (header === `Bearer ${expected}`) return true;
    return this.#sessionValid(request);
  }

  /** hex HMAC-SHA256 — подпись сессионной куки (SESSION_SECRET) и подпись
   *  webhook'ов автоматизаций (AUTOMATION_WEBHOOK_SECRET, #116): одна механика. */
  async #hmacHex(secret: string, payload: string): Promise<string> {
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(payload));
    return [...new Uint8Array(mac)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async #hmac(payload: string): Promise<string> {
    return this.#hmacHex(this.env.SESSION_SECRET, payload);
  }

  #cookieValue(request: Request): string | null {
    const header = request.headers.get("Cookie");
    if (!header) return null;
    for (const pair of header.split(";")) {
      const [name, ...rest] = pair.trim().split("=");
      if (name === SESSION.cookieName && rest.length) return rest.join("=");
    }
    return null;
  }

  async #sessionValid(request: Request): Promise<boolean> {
    if (!this.env.SESSION_SECRET) return false;
    const value = this.#cookieValue(request);
    if (!value) return false;
    const dot = value.lastIndexOf(".");
    if (dot <= 0) return false;
    const payload = value.slice(0, dot);
    const signature = value.slice(dot + 1);
    if (!constantTimeEqual(signature, await this.#hmac(payload))) return false;
    const [, rawExpires] = payload.split(":");
    const expires = Number(rawExpires);
    return payload.startsWith("v1:") && Number.isFinite(expires) && Date.now() / 1000 < expires;
  }

  #setCookieHeader(value: string, maxAgeSeconds: number): string {
    return `${SESSION.cookieName}=${value}; HttpOnly; SameSite=Strict; Path=/; Secure; Max-Age=${maxAgeSeconds}`;
  }

  async #issueSession(): Promise<Response> {
    if (!this.env.SESSION_SECRET) {
      // «Возможности нет» — конфигурация, а не поломка запроса; браузерный вход
      // при этом невозможен, молча отдавать 200 без куки нельзя.
      throw new ApiError(500, "session_secret_missing");
    }
    const expiresAtSeconds = Math.floor((Date.now() + SESSION.ttlMs) / 1000);
    const payload = `v1:${expiresAtSeconds}`;
    const value = `${payload}.${await this.#hmac(payload)}`;
    return this.#json(
      { ok: true, expires_at: expiresAtSeconds * 1000 },
      { headers: { "set-cookie": this.#setCookieHeader(value, Math.floor(SESSION.ttlMs / 1000)) } },
    );
  }

  #dropSession(): Response {
    // Кука HttpOnly — убрать её может только сервер.
    return this.#json(
      { ok: true },
      { headers: { "set-cookie": this.#setCookieHeader("", 0) } },
    );
  }

  #json(body: unknown, init?: ResponseInit): Response {
    return new Response(JSON.stringify(body), {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  }

  async #readJson(request: Request): Promise<Record<string, unknown>> {
    return this.#parseJsonText(await request.text());
  }

  /** Битый JSON И пустое тело — громкий 400: пустой POST не должен молча
   *  проходить валидацией «все поля опциональны» и давать побочный эффект
   *  (ревью head 7a21536: пустой POST /api/tasks стрелял dispatch'ом).
   *  Единственный маршрут с опциональным телом — POST /api/messages/process:
   *  он сам допускает пустое тело, минуя этот общий путь. */
  #parseJsonText(text: string): Record<string, unknown> {
    if (text.length > LIMITS.bodyMaxBytes) {
      throw new ApiError(413, "too_large", { limit: LIMITS.bodyMaxBytes });
    }
    if (!text.trim()) {
      throw new ApiError(400, "bad_json", { detail: msg("body_not_object") });
    }
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error(msg("body_not_object"));
      }
      return parsed as Record<string, unknown>;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      throw new ApiError(400, "bad_json", { detail });
    }
  }

  // ── Состояние ─────────────────────────────────────────────────────────────────────

  /** #taskCountsCache лениво: пересчёт — единственное место, где GROUP BY по
   *  ВСЕЙ таблице tasks вообще выполняется, и то не чаще, чем реально меняется
   *  состав задач (создание/переход статуса), а не каждый heartbeat. */
  #taskCounts(): Record<TaskRow["status"], number> {
    if (this.#taskCountsCache === null) {
      const counts: Record<TaskRow["status"], number> = {
        queued: 0,
        dispatched: 0,
        running: 0,
        done: 0,
        failed: 0,
      };
      for (const row of this.#rows(this.#sql.exec("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"))) {
        counts[String(row.status) as TaskRow["status"]] = Number(row.n);
      }
      this.#taskCountsCache = counts;
    }
    return this.#taskCountsCache;
  }

  #status(): Status {
    const now = Date.now();
    const hb = this.#rows(this.#sql.exec("SELECT ts, job_id FROM heartbeat WHERE id = 1"))[0];
    const counts = this.#taskCounts();
    const lastEventId = Number(this.#rows(this.#sql.exec("SELECT COALESCE(MAX(id), 0) AS m FROM events"))[0].m);
    const stale = this.#rows(
      this.#sql.exec(
        `SELECT COUNT(*) AS n, MIN(dispatch_ts) AS oldest FROM tasks
         WHERE status = 'dispatched' AND dispatch_ts IS NOT NULL AND dispatch_ts < ?`,
        now - LIMITS.staleDispatchMs,
      ),
    )[0];
    const msgCounts: Record<string, number> = {};
    for (const row of this.#rows(this.#sql.exec("SELECT status, COUNT(*) AS n FROM messages GROUP BY status"))) {
      msgCounts[String(row.status)] = Number(row.n);
    }
    const hbTs = hb ? Number(hb.ts) : null;
    const lastPulse = this.#getPulse();
    return {
      now,
      heartbeat_fresh_ms: LIMITS.heartbeatFreshMs,
      hands_alive: handsAreAlive(now, hbTs),
      last_heartbeat: hb ? { ts: hbTs as number, job_id: String(hb.job_id) } : null,
      tasks: counts,
      last_event_id: lastEventId,
      stale_dispatch: {
        count: Number(stale.n),
        oldest_age_ms: stale.oldest === null || stale.oldest === undefined ? null : now - Number(stale.oldest),
      },
      last_pulse: lastPulse,
      pulse_healthy: pulseHealthy(now, lastPulse),
      pulse_not_configured: pulseNotConfigured(lastPulse),
      pulse_stale: pulseStale(now, lastPulse),
      messages: msgCounts,
    };
  }

  #broadcastStatus(): void {
    this.#broadcast({ type: "status", status: this.#status() });
  }

  #broadcastEvent(row: EventRow): void {
    const payload = JSON.stringify({ type: "event", event: row });
    for (const ws of this.ctx.getWebSockets()) {
      try {
        const att = ws.deserializeAttachment() as { after?: number } | null;
        if ((att?.after ?? 0) >= row.id) continue;
        ws.send(payload);
      } catch {
        // Умирающий сокет CF закроет сам; остальным подписчикам мешать нельзя.
      }
    }
  }

  #broadcast(message: Record<string, unknown>): void {
    const payload = JSON.stringify(message);
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(payload);
      } catch {
        // cf. #broadcastEvent
      }
    }
  }

  // ── Журнал: приём батчей ──────────────────────────────────────────────────────────

  async #postEvents(request: Request): Promise<Response> {
    const body = await this.#readJson(request);
    const taskId = body.task_id;
    if (typeof taskId !== "string" || !taskId) {
      throw new ApiError(400, "need_task_id");
    }
    const source = typeof body.source === "string" && body.source ? body.source : "job";
    const rawEvents = body.events;
    if (!Array.isArray(rawEvents) || rawEvents.length === 0) {
      throw new ApiError(400, "need_events_array");
    }
    if (rawEvents.length > LIMITS.batchMax) {
      throw new ApiError(413, "batch_too_many", { limit: LIMITS.batchMax });
    }

    const now = Date.now();
    const incoming: { seq: number; ts: number; kind: string; data: string | null }[] = [];
    for (const raw of rawEvents) {
      const event = raw as Record<string, unknown>;
      const seq = event.seq;
      if (typeof seq !== "number" || !Number.isInteger(seq) || seq <= 0) {
        throw new ApiError(400, "need_positive_int_seq");
      }
      const kind = event.kind;
      if (typeof kind !== "string" || !kind) {
        throw new ApiError(400, "need_nonempty_kind");
      }
      incoming.push({
        seq,
        ts: typeof event.ts === "number" ? event.ts : now,
        kind,
        data: event.data === undefined ? null : JSON.stringify(event.data),
      });
    }

    // Идемпотентность: DO исполняет запросы последовательно, поэтому предчтение
    // существующих seq точно. INSERT OR IGNORE страхует состояние при любом раскладе,
    // а дублями считаем то, что уже лежало. rowsWritten здесь не помощь: он не
    // различает «вставлено» и «проигнорировано по конфликту».
    const existing = new Set(
      this.#rows(
        this.#sql.exec(
          `SELECT seq FROM events WHERE task_id = ? AND seq IN (${incoming.map(() => "?").join(", ")})`,
          taskId,
          ...incoming.map((event) => event.seq),
        ),
      ).map((row) => Number(row.seq)),
    );
    const accepted: EventRow[] = [];
    for (const event of incoming) {
      if (existing.has(event.seq)) continue;
      const cursor = this.#sql.exec(
        `INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, ?, ?, ?)
         ON CONFLICT(task_id, seq) DO NOTHING`,
        taskId,
        event.seq,
        event.ts,
        source,
        event.kind,
        event.data,
      );
      if (cursor.rowsWritten === 0) continue; // гонка невозможна, но состояние важнее счётчика
      const id = Number(
        this.#rows(this.#sql.exec("SELECT id FROM events WHERE task_id = ? AND seq = ?", taskId, event.seq))[0].id,
      );
      accepted.push({
        id,
        task_id: taskId,
        seq: event.seq,
        ts: event.ts,
        source,
        kind: event.kind,
        data: event.data === null ? null : JSON.parse(event.data),
      });
    }
    const duplicates = incoming.length - accepted.length;

    // Побочные эффекты — только для действительно новых событий: повторная доставка
    // батча не должна ни двоить журнал, ни перезапускать переходы статуса задачи.
    for (const row of accepted) this.#applySideEffects(row);
    for (const row of accepted) this.#broadcastEvent(row);
    if (accepted.length) this.#broadcastStatus();
    // Триггер «событие журнала» (#116) — после записи: событие, которого нет в
    // журнале, триггерить не может; отказ триггера не отменяет приём батча.
    await this.#fireJournalTriggers(accepted);

    return this.#json({ accepted: accepted.length, duplicates, task_id: taskId });
  }

  /** Переходы задач и сброс heartbeat живут здесь и нигде больше. */
  #applySideEffects(row: EventRow): void {
    if (row.kind === "job_start") {
      const cursor = this.#sql.exec(
        "UPDATE tasks SET status = 'running' WHERE id = ? AND status NOT IN ('done', 'failed')",
        row.task_id,
      );
      if (cursor.rowsWritten > 0) this.#taskCountsCache = null;
    }
    if (row.kind === "job_end") {
      const failed = (row.data as { result?: string } | null)?.result === "fail";
      this.#sql.exec("UPDATE tasks SET status = ? WHERE id = ?", failed ? "failed" : "done", row.task_id);
      this.#taskCountsCache = null;
      // Руки закончили — «руки живы» уходит сразу, а не через порог свежести.
      this.#sql.exec("DELETE FROM heartbeat WHERE id = 1");
    }
  }

  // ── Журнал: replay ────────────────────────────────────────────────────────────────

  #getEvents(url: URL): Response {
    const after = this.#intParam(url, "after", 0);
    const limit = Math.min(this.#intParam(url, "limit", LIMITS.replayDefault), LIMITS.replayMax);
    // Фильтр по задаче нужен replay'ем конкретной задачи (клиент рук засевает seq
    // с максимума); без него — весь журнал подряд.
    const taskId = url.searchParams.get("task_id");
    const rows = this.#rows(
      taskId
        ? this.#sql.exec(
            "SELECT id, task_id, seq, ts, source, kind, data FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
            taskId,
            after,
            limit + 1,
          )
        : this.#sql.exec(
            "SELECT id, task_id, seq, ts, source, kind, data FROM events WHERE id > ? ORDER BY id LIMIT ?",
            after,
            limit + 1,
          ),
    );
    const hasMore = rows.length > limit;
    const events: EventRow[] = rows.slice(0, limit).map((row) => ({
      id: Number(row.id),
      task_id: String(row.task_id),
      seq: Number(row.seq),
      ts: Number(row.ts),
      source: String(row.source),
      kind: String(row.kind),
      data: row.data === null || row.data === undefined ? null : JSON.parse(String(row.data)),
    }));
    const nextAfter = events.length ? events[events.length - 1].id : after;
    return this.#json(
      { events, has_more: hasMore, next_after: nextAfter },
      { headers: { "x-has-more": hasMore ? "true" : "false", "x-next-after": String(nextAfter) } },
    );
  }

  #intParam(url: URL, name: string, fallback: number): number {
    const raw = url.searchParams.get(name);
    if (raw === null) return fallback;
    const value = Number(raw);
    if (!Number.isInteger(value) || value < 0) {
      throw new ApiError(400, "bad_int_param", { name });
    }
    return value;
  }

  // ── Живой поток ───────────────────────────────────────────────────────────────────

  #openLiveSocket(url: URL): Response {
    const after = this.#intParam(url, "after", 0);
    const pair = new WebSocketPair();
    // acceptWebSocket (Hibernation API): сокет живёт без объекта в памяти, GB-s не капают.
    this.ctx.acceptWebSocket(pair[1]);
    pair[1].serializeAttachment({ after });
    pair[1].send(JSON.stringify({ type: "hello", status: this.#status() }));
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  /** Сокет только на приём: клиент, пишущий в сокет, теряет соединение (1008). */
  override webSocketMessage(ws: WebSocket): void {
    ws.close(1008, "downlink only");
  }

  override webSocketClose(): void {}

  /** Пульс: следующим тиком гарантируем цепочку, потом дёргаем оркестратора
   *  и проверяем, не отстала ли морда dsh-edge от npm. */

  override async alarm(): Promise<void> {
    const token = this.env.GH_DISPATCH_TOKEN;
    const repo = this.env.GH_REPO;
    // Тик перезакладывается ПЕРВОЙ строкой, до какого-либо I/O (issue #269) — этот
    // инвариант не может жить внутри общего try ниже: если бы setAlarm упал там, catch
    // проглотил бы исключение и alarm() завершился бы успешно, а CF ретраит упавший
    // хендлер, только когда исключение ВЫХОДИТ из него, — успешный возврат ретрая не
    // вызовет. #320 добавил ровно тот класс отказа: setAlarm сам может упасть на
    // исчерпании суточной квоты rows_written. Ловим отдельно и пробуем один раз ещё —
    // если и повтор не удался, честно называем итог: цепочка встала до пересоздания DO.
    try {
      await this.ctx.storage.setAlarm(Date.now() + HEARTBEAT.selfOrchestrationMs);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      console.error(`alarm: setAlarm упал (${classifyStorageError(detail)}): ${detail} — повторная попытка`);
      try {
        await this.ctx.storage.setAlarm(Date.now() + HEARTBEAT.selfOrchestrationMs);
      } catch (retryError) {
        const retryDetail = retryError instanceof Error ? retryError.message : String(retryError);
        console.error(
          `alarm: повторная попытка setAlarm тоже упала (${classifyStorageError(retryDetail)}): ` +
            `${retryDetail} — цепочка пульса встала до пересоздания DO`,
        );
      }
      return;
    }

    // Инбокс владельца (#20): тот же пульс — ватчдог зависших и водитель разбора.
    // ДО раннего возврата по конфигурации dispatch: разбор не зависит ни от
    // GH_DISPATCH_TOKEN, ни от GH_REPO (п.33 спеки) — при пустом токене пульс
    // обязан продолжать разбирать инбокс, а не молча пропускать гарантию
    // непотери. Любой сбой здесь не роняет пульс: тик уже перезаложен.
    try {
      this.#reclaimStuckMessages();
    } catch (error) {
      console.log(`inbox reclaim failed: ${error instanceof Error ? error.message : error}`);
    }
    try {
      await this.#processInbox(MESSAGE_PROCESS_MAX, false);
    } catch (error) {
      console.log(`inbox process failed: ${error instanceof Error ? error.message : error}`);
    }

    try {
      // #sql.exec ниже (#getStoredPulse, #recordPulse) тоже может упасть на исчерпании
      // суточной квоты rows_read/rows_written — но тик уже перезаложен строкой выше,
      // поэтому падение здесь не убивает цепочку, только этот конкретный дозвон.
      if (!token || !repo) {
        // «Возможности нет» — не поломка, но и не тишина: видно в /api/status, что
        // секретов нет, а не гадать по отсутствию прогонов оркестратора.
        this.#recordPulse(false, HEARTBEAT.notConfiguredDetail, null, null);
        return;
      }
      const previous = this.#getStoredPulse();
      // Подтверждение запуска (issue #269, находка ревью): 204 доказывает только
      // приём, не запуск. Читаем id последнего run'а ДО нового dispatch'а — это
      // baseline для следующего тика — и одновременно сверяем, появился ли новый
      // run с baseline'а ПРЕДЫДУЩЕГО тика (без синхронного ожидания: подтверждение
      // всегда на такт позже, а не блокирует этот alarm).
      const latestRunId = await fetchLatestOrchestraRunId(token, repo, fetch);
      const runConfirmed =
        previous?.dispatch_ok ? confirmPreviousRun(previous.last_run_id, latestRunId) : null;
      const result = await attemptOrchestraDispatch(token, repo, fetch);
      // #303, находка ревью: detail, а не result.detail — иначе «принят, но не
      // подтвердилось» пишет в хранилище null (см. docstring pulseDetailForRecord).
      const detail = pulseDetailForRecord(result, runConfirmed);
      this.#recordPulse(result.ok, detail, latestRunId, runConfirmed);
      if (!result.ok || runConfirmed === false) {
        // Пульс не роняет объект: тик уже перезаложен. Теперь исход ещё и в
        // durable-состоянии (#status().last_pulse) — раньше он тонул в console.log,
        // который никто не смотрит между дедами (fail loud, issue #269).
        console.log(`heartbeat dispatch: accepted=${result.ok} detail=${detail} run_confirmed=${runConfirmed}`);
      }
    } catch (error) {
      // #320: похожая на исчерпание квоты storage (rows_read/rows_written) ошибка
      // здесь раньше уходила из alarm() непойманным — единственным следом оставалось
      // молчание пульса. Тик к этому моменту УЖЕ перезаложен (см. try выше) — цепочка
      // жива в любом случае, это ловит только конкретный неудавшийся дозвон и называет
      // причину по имени вместо тихого «пульс просто не бьётся».
      const detail = error instanceof Error ? error.message : String(error);
      console.error(`alarm: упал (${classifyStorageError(detail)}): ${detail}`);
      return;
    }
    try {
      // token/repo здесь гарантированно заданы: иначе выше уже был return.
      await this.#checkDshEdgeUpdate(token!, repo!);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      console.log(`dsh-edge update check failed (${classifyStorageError(detail)}): ${detail}`);
    }
    // Триггеры-расписания автоматизаций (#116): тот же пульс, что гоняет
    // orchestra — отдельного cron в Actions нет (research/21: не тикает).
    try {
      await this.#fireDueSchedules(Date.now());
    } catch (error) {
      console.log(`automation schedules failed: ${error instanceof Error ? error.message : error}`);
    }
  }

  /** Текущая строка пульса (issue #269), полное внутреннее представление —
   *  единственное место чтения, делят #status() (через #getPulse) и alarm()
   *  (baseline для confirmPreviousRun). */
  #getStoredPulse(): StoredPulse | null {
    const row = this.#rows(
      this.#sql.exec("SELECT ts, dispatch_ok, detail, last_run_id, run_confirmed FROM pulse WHERE id = 1"),
    )[0];
    if (!row) return null;
    return {
      ts: Number(row.ts),
      dispatch_ok: Number(row.dispatch_ok) === 1,
      detail: row.detail === null ? null : String(row.detail),
      last_run_id: row.last_run_id === null ? null : Number(row.last_run_id),
      run_confirmed: row.run_confirmed === null ? null : Number(row.run_confirmed) === 1,
    };
  }

  /** Проекция для #status()/pulseHealthy — last_run_id наружу не течёт, фронту
   *  это число не нужно (см. StoredPulse). */
  #getPulse(): PulseStatus | null {
    const row = this.#getStoredPulse();
    if (!row) return null;
    const { last_run_id: _lastRunId, ...pulse } = row;
    return pulse;
  }

  /** Единственное место, где обновляется видимый исход пульса (issue #269). */
  #recordPulse(ok: boolean, detail: string | null, lastRunId: number | null, runConfirmed: boolean | null): void {
    this.#sql.exec(
      `INSERT INTO pulse (id, ts, dispatch_ok, detail, last_run_id, run_confirmed) VALUES (1, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, dispatch_ok = excluded.dispatch_ok,
         detail = excluded.detail, last_run_id = excluded.last_run_id, run_confirmed = excluded.run_confirmed`,
      Date.now(),
      ok ? 1 : 0,
      detail,
      lastRunId,
      runConfirmed === null ? null : runConfirmed ? 1 : 0,
    );
  }

  /** #73: морда dsh-edge должна быть последней версии. Сверяем версию, которую
   *  отдаёт её публичный /api/health, с latest в npm; расхождение при истёкшем
   *  троттле → workflow_dispatch деплой-воркфлоу. GitHub'овский `schedule` на
   *  этом репозитории ненадёжен (см. HEARTBEAT, замер docs/research/21),
   *  поэтому живём на пульсе. Любой сбой здесь не роняет пульс: тик уже
   *  перезаложен, ошибка уходит в observability. */
  async #checkDshEdgeUpdate(token: string, repo: string): Promise<void> {
    const now = Date.now();
    const last = await this.ctx.storage.get<number>(DSH_EDGE_UPDATE.lastAttemptKey);
    const [healthRes, registryRes] = await Promise.all([
      fetch(DSH_EDGE_UPDATE.healthUrl),
      fetch(DSH_EDGE_UPDATE.registryUrl),
    ]);
    if (!healthRes.ok || !registryRes.ok) {
      throw new Error(`health=${healthRes.status} registry=${registryRes.status}`);
    }
    const health = (await healthRes.json<Record<string, unknown>>()) as { version?: unknown };
    const release = (await registryRes.json<Record<string, unknown>>()) as { version?: unknown };
    if (typeof health.version !== "string" || typeof release.version !== "string") {
      throw new Error("неожиданный формат версий: health/registry не отдали строку version");
    }
    const decision = dshEdgeUpdateDecision(health.version, release.version, last, now);
    if (decision !== "dispatch") return;
    // Пометку попытки ставим ДО диспетча: упавший деплой не должен превратить
    // пульс в штурм упавшего деплоя каждые 15 минут.
    await this.ctx.storage.put(DSH_EDGE_UPDATE.lastAttemptKey, now);
    console.log(`dsh-edge update: deployed ${health.version} != npm ${release.version} — dispatch`);
    const res = await fetch(
      `${GITHUB.apiBase}/repos/${repo}/actions/workflows/${DSH_EDGE_UPDATE.workflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
        body: JSON.stringify({ ref: "main" }),
      },
    );
    if (!res.ok) throw new Error(`dispatch отклонён: ${res.status}`);
  }

  // ── Очередь задач ─────────────────────────────────────────────────────────────────

  async #postTask(request: Request): Promise<Response> {
    const body = await this.#readJson(request);
    const payload = body.payload === undefined ? null : body.payload;
    if (JSON.stringify(payload)?.length > LIMITS.payloadMaxChars) {
      throw new ApiError(413, "payload_too_large", { limit: LIMITS.payloadMaxChars });
    }

    const id = crypto.randomUUID();
    const now = Date.now();
    this.#sql.exec("INSERT INTO tasks (id, created_ts, status) VALUES (?, ?, 'queued')", id, now);
    this.#taskCountsCache = null;
    this.#emitSystemEvent(id, "task_queued", { payload });
    this.#broadcastStatus();

    const result = await this.#dispatchToGitHub(GITHUB.dispatchEventType, { task_id: id });
    if (result.status === 0) {
      // «Возможности нет» — это не поломка: конфигурации нет, задача лежит в очереди.
      return this.#json(
        {
          task_id: id,
          dispatched: false,
          dispatch: "not_configured",
          detail: msg("dispatch_not_configured"),
        },
        { status: 201 },
      );
    }
    if (result.status === -1) {
      this.#emitSystemEvent(id, "dispatch_failed", { detail: result.error });
      this.#broadcastStatus();
      throw new ApiError(502, "dispatch_network_failed", { detail: result.error ?? "" });
    }

    if (result.status !== 204) {
      // 204 от dispatch — не доказательство запуска job'а (и 204 при отсутствии
      // workflow-файла на default branch тоже). Ловится отсутствием job_start, см.
      // docs/research/21-github-actions.md. Здесь доказываем только приём события API.
      this.#emitSystemEvent(id, "dispatch_failed", { github_status: result.status });
      this.#broadcastStatus();
      throw new ApiError(502, "dispatch_rejected", { status: result.status });
    }

    this.#sql.exec("UPDATE tasks SET status = 'dispatched', dispatch_ts = ? WHERE id = ?", now, id);
    this.#emitSystemEvent(id, "task_dispatched", {});
    this.#broadcastStatus();
    return this.#json({ task_id: id, dispatched: true }, { status: 201 });
  }

  /**
   * repository_dispatch в репозиторий — единственная точка механики dispatch
   * (ADR 0008: узкий GH_DISPATCH_TOKEN). Два вызывающих: очередь задач
   * (event_type harness-task) и триггеры автоматизаций (harness-automation, #116).
   * status: 0 — не сконфигурировано («возможности нет»), -1 — сеть, иначе —
   * HTTP-код ответа GitHub (204 = принято).
   */
  async #dispatchToGitHub(
    eventType: string,
    payload: Record<string, unknown>,
  ): Promise<{ status: number; error?: string }> {
    const token = this.env.GH_DISPATCH_TOKEN;
    const repo = this.env.GH_REPO;
    if (!token || !repo) return { status: 0 };
    try {
      const response = await fetch(`${GITHUB.apiBase}/repos/${repo}/dispatches`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
        body: JSON.stringify({ event_type: eventType, client_payload: payload }),
      });
      return { status: response.status };
    } catch (error) {
      return { status: -1, error: error instanceof Error ? error.message : String(error) };
    }
  }

  #recentTasks(): TaskRow[] {
    return this.#rows(
      this.#sql.exec("SELECT id, created_ts, dispatch_ts, latency_ms, status FROM tasks ORDER BY created_ts DESC LIMIT ?", LIMITS.tasksListMax),
    ).map((row) => this.#taskRow(row));
  }

  #task(id: string): TaskRow | null {
    const row = this.#rows(
      this.#sql.exec("SELECT id, created_ts, dispatch_ts, latency_ms, status FROM tasks WHERE id = ?", id),
    )[0];
    return row ? this.#taskRow(row) : null;
  }

  #taskRow(row: Record<string, SqlStorageValue>): TaskRow {
    return {
      id: String(row.id),
      created_ts: Number(row.created_ts),
      dispatch_ts: row.dispatch_ts === null || row.dispatch_ts === undefined ? null : Number(row.dispatch_ts),
      latency_ms: row.latency_ms === null || row.latency_ms === undefined ? null : Number(row.latency_ms),
      status: String(row.status) as TaskRow["status"],
    };
  }

  // ── Автоматизации (#116): конфиг, триггеры, webhook ───────────────────────────────

  #automationRow(id: string): Record<string, SqlStorageValue> | null {
    return this.#rows(
      this.#sql.exec(
        "SELECT enabled, config, created_ts, updated_ts, last_fired_ts FROM automations WHERE id = ?",
        id,
      ),
    )[0] ?? null;
  }

  /** Вид для UI: конфиг распарсен (писался только через валидацию), последний
   *  прогон — по префиксу task_id прогона; разделитель «:» в id запрещён, так
   *  что префикс `automation:<id>:` не пересекается с чужими автоматизациями. */
  #automationView(id: string, row: Record<string, SqlStorageValue>): Record<string, unknown> {
    const lastRun = this.#rows(
      this.#sql.exec(
        "SELECT id, status, created_ts FROM tasks WHERE id LIKE ? ORDER BY created_ts DESC LIMIT 1",
        `${AUTOMATIONS.runTaskPrefix}${id}:%`,
      ),
    )[0];
    return {
      id,
      enabled: Number(row.enabled) === 1,
      config: JSON.parse(String(row.config)) as AutomationConfig,
      created_ts: Number(row.created_ts),
      updated_ts: Number(row.updated_ts),
      last_fired_ts: row.last_fired_ts === null || row.last_fired_ts === undefined ? null : Number(row.last_fired_ts),
      last_run: lastRun
        ? { task_id: String(lastRun.id), status: String(lastRun.status), created_ts: Number(lastRun.created_ts) }
        : null,
    };
  }

  #listAutomations(): Record<string, unknown>[] {
    return this.#rows(
      this.#sql.exec("SELECT id, enabled, config, created_ts, updated_ts, last_fired_ts FROM automations ORDER BY id"),
    ).map((row) => this.#automationView(String(row.id), row));
  }

  async #putAutomation(request: Request, restId: string): Promise<Response> {
    if (!isValidAutomationId(restId)) {
      throw new ApiError(400, "automation_id_invalid");
    }
    const body = await this.#readJson(request);
    const parsed = parseAutomationConfig(body.config);
    if (!parsed.ok) {
      throw new ApiError(400, "automation_config_invalid", { detail: parsed.error });
    }
    const now = Date.now();
    const existing = this.#automationRow(restId);
    if (!existing) {
      const count = Number(
        this.#rows(this.#sql.exec("SELECT COUNT(*) AS n FROM automations"))[0].n,
      );
      if (count >= LIMITS.automationsMax) {
        throw new ApiError(400, "automation_limit", { limit: LIMITS.automationsMax });
      }
    }
    this.#sql.exec(
      `INSERT INTO automations (id, enabled, config, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET enabled = excluded.enabled, config = excluded.config, updated_ts = excluded.updated_ts`,
      restId,
      parsed.config.enabled ? 1 : 0,
      JSON.stringify(parsed.config),
      existing ? Number(existing.created_ts) : now,
      now,
    );
    // След конфигурации в журнале — виден с дашборда (#111); прогон это событие
    // НЕ запускает: прогон порождают только триггеры.
    this.#emitSystemEvent(`automation:${restId}`, "automation_updated", {
      created: !existing,
      enabled: parsed.config.enabled,
      trigger: parsed.config.trigger.type,
      task: parsed.config.task.kind,
    });
    const row = this.#automationRow(restId);
    return this.#json({ automation: this.#automationView(restId, row!) }, { status: existing ? 200 : 201 });
  }

  #deleteAutomation(restId: string): Response {
    if (!isValidAutomationId(restId)) {
      throw new ApiError(400, "automation_id_invalid");
    }
    const cursor = this.#sql.exec("DELETE FROM automations WHERE id = ?", restId);
    if (cursor.rowsWritten === 0) {
      throw new ApiError(404, "automation_not_found", { automation_id: restId });
    }
    // Симметрично PUT (automation_updated) — иначе журнал/дашборд (#111) молчит
    // об удалении: конфигурация исчезает без следа, а webhook на неё начинает
    // 404-ить без объяснения (находка AI-ревью PR #241). Под тем же task_id
    // automation:<id>, что и остальные системные события автоматизации —
    // гвардия петли journal-триггеров (префикс automation:) их уже исключает.
    this.#emitSystemEvent(`automation:${restId}`, "automation_deleted", {});
    return this.#json({ ok: true });
  }

  /** Вход внешних webhook'ов. Подпись — HMAC-SHA256 от сырого тела с
   *  AUTOMATION_WEBHOOK_SECRET; без подписи/с неверной — 401 и событие в журнале:
   *  отказ обязан быть виден, а не только угадан по коду ответа. */
  async #handleWebhook(request: Request, automationId: string): Promise<Response> {
    const secret = this.env[AUTOMATIONS.webhookSecretEnv];
    if (!secret) {
      // Конфигурации нет — это не «отклонено», а «приём невозможен»: другой ответ,
      // чтобы владелец отличал незавершённую настройку от атаки.
      throw new ApiError(500, "webhook_secret_missing");
    }
    const rawBody = await request.text();
    if (rawBody.length > LIMITS.bodyMaxBytes) {
      throw new ApiError(413, "too_large", { limit: LIMITS.bodyMaxBytes });
    }
    const provided = request.headers.get(AUTOMATIONS.webhookSignatureHeader) ?? "";
    const expected = `sha256=${await this.#hmacHex(secret, rawBody)}`;
    if (!provided || !constantTimeEqual(provided, expected)) {
      this.#emitSystemEvent(AUTOMATIONS.webhookRejectedTaskId, "automation_webhook_rejected", {
        automation: String(automationId).slice(0, 64),
        reason: provided ? "bad_signature" : "signature_missing",
      });
      throw new ApiError(401, "webhook_signature_invalid");
    }
    if (!isValidAutomationId(automationId)) {
      throw new ApiError(400, "automation_id_invalid");
    }
    const row = this.#automationRow(automationId);
    if (!row) {
      throw new ApiError(404, "automation_not_found", { automation_id: automationId });
    }
    const config = JSON.parse(String(row.config)) as AutomationConfig;
    if (!config.enabled) {
      this.#emitSystemEvent(`automation:${automationId}`, "automation_webhook_rejected", { reason: "disabled" });
      throw new ApiError(409, "automation_disabled", { automation_id: automationId });
    }
    if (config.trigger.type !== "webhook") {
      // Вход должен стрелять только webhook-триггерной автоматизацией: иначе
      // schedule/journal прогон сработал бы мимо своего триггера, а last_fired_ts
      // молча сдвинул бы фазу расписания (нашёл AI-ревью PR #241).
      this.#emitSystemEvent(`automation:${automationId}`, "automation_webhook_rejected", { reason: "not_webhook_trigger" });
      throw new ApiError(409, "automation_not_webhook", { automation_id: automationId });
    }
    const fired = await this.#fireAutomation(automationId, row.last_fired_ts, config, "webhook", Date.now());
    return this.#json(
      { ok: true, task_id: fired.taskId, dispatched: fired.dispatched },
      { status: 202 },
    );
  }

  /**
   * Поднять прогон автоматизации: задача в очереди + события журнала +
   * repository_dispatch. Отказ dispatch не выбрасывает исключение наружу —
   * он честно помечает прогон и остаётся в журнале; «не сконфигурировано»
   * оставляет прогон в очереди, как у POST /api/tasks.
   * last_fired_ts обновляется при ЛЮБОМ исходе: упавший/ненастроенный dispatch
   * не должен штурмоваться каждым 15-минутным пульсом — повтор по расписанию
   * только в следующем интервале.
   */
  async #fireAutomation(
    automationId: string,
    lastFiredTs: SqlStorageValue,
    config: AutomationConfig,
    trigger: "schedule" | "webhook" | "journal",
    now: number,
  ): Promise<{ taskId: string; dispatched: boolean }> {
    // nonce: два запуска в одну миллисекунду (параллельные webhook-выстрелы)
    // иначе столкнулись бы на PRIMARY KEY tasks (ревью #116, minor 6).
    const nonce = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
    const taskId = runTaskId(automationId, now, nonce);
    this.#sql.exec("INSERT INTO tasks (id, created_ts, status) VALUES (?, ?, 'queued')", taskId, now);
    this.#emitSystemEvent(taskId, "automation_triggered", { automation: automationId, trigger });
    const period = config.trigger.type === "schedule"
      ? digestPeriod(lastFiredTs === null || lastFiredTs === undefined ? null : Number(lastFiredTs), config.trigger.intervalHours, now)
      : null;
    const result = await this.#dispatchToGitHub(AUTOMATIONS.dispatchEventType, {
      automation_id: automationId,
      task_id: taskId,
      trigger,
      period,
    });
    let dispatched = false;
    if (result.status === 204) {
      dispatched = true;
      this.#sql.exec("UPDATE tasks SET status = 'dispatched', dispatch_ts = ? WHERE id = ?", now, taskId);
      this.#emitSystemEvent(taskId, "automation_dispatched", {});
    } else if (result.status === 0) {
      // «Возможности нет» (GH_DISPATCH_TOKEN не задан): прогон ждёт конфигурации
      // в очереди — как обычная задача, ответ честно называет not_configured.
    } else {
      this.#sql.exec("UPDATE tasks SET status = 'failed' WHERE id = ?", taskId);
      this.#emitSystemEvent(taskId, "dispatch_failed", { github_status: result.status, detail: result.error });
    }
    this.#sql.exec("UPDATE automations SET last_fired_ts = ? WHERE id = ?", now, automationId);
    this.#broadcastStatus();
    return { taskId, dispatched };
  }

  /** Тик расписаний: включённые schedule-автоматизации, у которых интервал истёк.
   *  Отказ одного запуска изолирован — выключение/поломка автоматизации не
   *  убивает остальные (критерий #116). */
  async #fireDueSchedules(now: number): Promise<void> {
    const rows = this.#rows(
      this.#sql.exec("SELECT id, config, last_fired_ts FROM automations WHERE enabled = 1 ORDER BY id"),
    );
    for (const row of rows) {
      const id = String(row.id);
      try {
        const config = JSON.parse(String(row.config)) as AutomationConfig;
        if (config.trigger.type !== "schedule") continue;
        const last = row.last_fired_ts === null || row.last_fired_ts === undefined ? null : Number(row.last_fired_ts);
        if (!scheduleDue(config.trigger.intervalHours, last, now)) continue;
        await this.#fireAutomation(id, row.last_fired_ts, config, "schedule", now);
      } catch (error) {
        console.log(`automation ${id} schedule fire failed: ${error instanceof Error ? error.message : error}`);
      }
    }
  }

  /** Триггер «событие журнала»: включённые journal-автоматизации, чей kind
   *  совпал с только что принятым событием. События самих автоматизаций
   *  (префикс task_id) ретриггерить не могут — гвардия петли. */
  async #fireJournalTriggers(accepted: EventRow[]): Promise<void> {
    const candidates = accepted.filter(
      (row) => !String(row.task_id).startsWith(AUTOMATIONS.runTaskPrefix),
    );
    if (!candidates.length) return;
    const rows = this.#rows(
      this.#sql.exec("SELECT id, config, last_fired_ts FROM automations WHERE enabled = 1 ORDER BY id"),
    );
    const now = Date.now();
    for (const row of rows) {
      const id = String(row.id);
      try {
        const config = JSON.parse(String(row.config)) as AutomationConfig;
        const trigger = config.trigger;
        if (trigger.type !== "journal") continue;
        // trigger — локальная константа: сужение типа в замыкании живёт,
        // у свойства config.trigger оно сбрасывается.
        if (!candidates.some((event) => event.kind === trigger.kind)) continue;
        // Кулдаун: работа прогона может порождать события с чужими task_id
        // (kind=pool → job_end воркера) — рвём цикл каденсом пульса.
        const last = row.last_fired_ts === null || row.last_fired_ts === undefined ? null : Number(row.last_fired_ts);
        if (!journalTriggerDue(last, now, AUTOMATIONS.journalCooldownMs)) continue;
        await this.#fireAutomation(id, row.last_fired_ts, config, "journal", now);
      } catch (error) {
        console.log(`automation ${id} journal trigger failed: ${error instanceof Error ? error.message : error}`);
      }
    }
  }


  /**
   * Системное событие. seq у системных событий отрицательные (−1, −2…): job нумерует
   * свои события с 1, и UNIQUE(task_id, seq) не должен сталкивать их между собой.
   */
  #emitSystemEvent(taskId: string, kind: string, data: unknown): void {
    const now = Date.now();
    const previous = Number(
      this.#rows(this.#sql.exec("SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND source = 'system'", taskId))[0].n,
    );
    const seq = -(previous + 1);
    const cursor = this.#sql.exec(
      "INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, 'system', ?, ?)",
      taskId,
      seq,
      now,
      kind,
      JSON.stringify(data),
    );
    if (cursor.rowsWritten === 0) return;
    const id = Number(
      this.#rows(this.#sql.exec("SELECT id FROM events WHERE task_id = ? AND seq = ?", taskId, seq))[0].id,
    );
    this.#broadcastEvent({ id, task_id: taskId, seq, ts: now, source: "system", kind, data });
  }

  // ── Heartbeat ─────────────────────────────────────────────────────────────────────

  async #postHeartbeat(request: Request): Promise<Response> {
    const body = await this.#readJson(request);
    const jobId = body.job_id;
    if (typeof jobId !== "string" || !jobId) {
      throw new ApiError(400, "need_job_id");
    }
    const taskId = typeof body.task_id === "string" && body.task_id ? body.task_id : null;
    const now = Date.now();
    this.#sql.exec(
      `INSERT INTO heartbeat (id, ts, job_id) VALUES (1, ?, ?)
       ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, job_id = excluded.job_id`,
      now,
      jobId,
    );

    // Замер «repository_dispatch → первый heartbeat»: фиксируем один раз, у первой отметки.
    if (taskId) {
      const task = this.#task(taskId);
      if (task && task.dispatch_ts !== null && task.latency_ms === null) {
        const latency = now - task.dispatch_ts;
        this.#sql.exec("UPDATE tasks SET latency_ms = ? WHERE id = ?", latency, taskId);
        this.#emitSystemEvent(taskId, "first_heartbeat", { latency_ms: latency, job_id: jobId });
      }
    }

    const status = this.#status();
    this.#broadcast({ type: "status", status });
    return this.#json({ hands_alive: status.hands_alive, ts: now });
  }

  // ── Inbox: сообщения владельца (#20) ────────────────────────────────────────────────
  //
  // Жизненный цикл: new → (атомарный захват) → processing → done | failed | ignored,
  // с двумя гарантиями непотери: зависший processing пульс возвращает в new
  // (ватчдог по образцу stale_dispatch), а повторяемая ошибка директивы живёт
  // в new до капа попыток (LIMITS.messageMaxAttempts) — потом честный failed.
  // Водитель разбора — пульс DO (alarm); POST /api/messages/process — ручной
  // прогон и retry_failed после устранения причины.

  /** Классификация сообщения: директива, чат, правка доков, неразобранное. */
  #classifyMessage(text: string): { kind: string; priority: number } {
    const trimmed = text.trim();
    // Граница слова: \b в JS — ASCII-граница и после кириллицы не возникает
    // никогда, русские директивы молча уезжали в raw (ревью head ffd6bfe).
    // «Далее не буква и не цифра», флаг u — для \p{...}.
    const wordEnd = "(?![\\p{L}\\p{N}])";
    // Явные префиксы — сильнейший сигнал, проверяются первыми.
    const explicitDirective = [
      new RegExp(`^[\\/]\\s*(?:${DIRECTIVE_WORDS_RE})${wordEnd}`, "iu"),
      /^\[TASK\]/i,
      /^#\d+\s/,
    ];
    for (const pattern of explicitDirective) {
      if (pattern.test(trimmed)) return { kind: "directive", priority: 10 };
    }
    // Правки доков — раньше глагольных директив: «обнови docs/INDEX.md» это
    // правка дока, а не абстрактная директива (пересечение классов, ревью ffd6bfe).
    if (/(docs\/|openspec\/|\.md\s|в\sдоке|в\sспеке|обнови\sдок)/i.test(trimmed)) {
      return { kind: "doc_edit", priority: 5 };
    }
    // Глагольные директивы без префикса — с той же не-ASCII границей слова.
    const verbDirective = new RegExp(`^(?:${DIRECTIVE_WORDS_RE})${wordEnd}`, "iu");
    if (verbDirective.test(trimmed)) return { kind: "directive", priority: 10 };
    // Чат/обсуждение: вопросы, комментарии без явного действия
    // Используем (^|\s) и (\s|[?,.!]|$) вместо \b для поддержки кириллицы
    if (/^[\?\¿]|(^|\s)(как|почему|что\sдумаешь|мнение|вопрос)(\s|[?,.!]|$)/i.test(trimmed)) {
      return { kind: "chat", priority: 1 };
    }
    // По умолчанию — неразобранное: классификатор детерминирован, повторная
    // обработка ничего не добавит — уходит в ignored на ручной триаж.
    return { kind: "raw", priority: 0 };
  }

  /** Группировка сообщений: серия от того же отправителя в том же чате в пределах
   *  окна — цепочка (grouped_with → предыдущее сообщение серии), чтобы триаж
   *  видел нить разговора и мог пройти её назад. Якорь строго раньше по id:
   *  пришедшие позже сообщения не тянут ранние в «звезду» вокруг себя. */
  #groupMessages(messageId: number): number | null {
    const msg = this.#rows(this.#sql.exec("SELECT sender_id, chat_id, ts FROM messages WHERE id = ?", messageId))[0];
    if (!msg) return null;
    const senderId = msg.sender_id;
    const chatId = msg.chat_id;
    const ts = Number(msg.ts);
    const windowAgo = ts - MESSAGE_GROUP_WINDOW_MS;
    const recent = this.#rows(this.#sql.exec(
      `SELECT id FROM messages
       WHERE sender_id = ? AND chat_id = ? AND ts > ? AND id < ?
       ORDER BY id DESC LIMIT 1`,
      senderId, chatId, windowAgo, messageId
    ));
    if (recent.length > 0) {
      const groupWith = Number(recent[0].id);
      this.#sql.exec("UPDATE messages SET grouped_with = ? WHERE id = ?", groupWith, messageId);
      return groupWith;
    }
    return null;
  }

  /**
   * Issue для директивы или правки доков (обе получают issue-след, kind —
   * в теле). Токен — собственный узкий секрет GH_ISSUES_TOKEN
   * (fine-grained, только Issues:RW; ADR 0011): GH_DISPATCH_TOKEN по ADR 0008
   * не имеет права на Issues — его 403 ловить здесь нечему. Пока секрет не
   * задан владельцем, директива честно повторяется (issues_not_configured) —
   * установка секрета сама доводит очередь, без ручного шага.
   */
  async #createIssue(messageId: number, text: string, kind: string): Promise<IssueOutcome> {
    const token = this.env.GH_ISSUES_TOKEN;
    const repo = this.env.GH_REPO;
    if (!token || !repo) {
      return { ok: false, retryable: true, error: "issues_not_configured" };
    }

    // Заголовок — первая строка без командного слова (общий с классификатором
    // список DIRECTIVE_WORDS), потолок 80 символов (меньше 422-потолка GitHub).
    // Маскирование ДО нарезки: усечённый секрет короче минимума паттерна уже
    // не маскируется и утекает в публичный заголовок сырым хвостом (ревью head
    // 3706d87). В GitHub уходит именно усечённый title: сырой titleRedacted.text
    // длиннее 256 символов — невозвратный 422 по разобраемому сообщению
    // (ревью head dfd167f, мёртвый потолок).
    const firstLine = text.trim().split(/\r?\n/)[0];
    const stripped = firstLine.replace(TITLE_PREFIX_RE, "").trim();
    const base = stripped || firstLine;
    const titleRedacted = redact(base);
    const title =
      titleRedacted.text.length > 80 ? titleRedacted.text.slice(0, 77) + "..." : titleRedacted.text;

    // Публичный issue — первый наружный путь произвольного фриформ-текста:
    // секреты маскируются тем же классом паттернов, что dsh-ci.sh::redact
    // (п.29 спеки), факт маскирования виден в result сообщения.
    const bodyRedacted = redact(text);
    const body =
      `## Сообщение владельца (inbox #${messageId})\n\n*kind: ${kind}*\n\n\`\`\`\n${bodyRedacted.text}\n\`\`\`\n\n---\n` +
      `*Создано автоматически из инбокса владельца (задача #20). Обработай по playbook.*`;

    try {
      // Таймаут заведомо меньше ватчдога (messageStuckProcessingMs): висящий
      // fetch не должен дожить до ретрая другой проходки (ревью PR #173).
      const res = await fetch(`${GITHUB.apiBase}/repos/${repo}/issues`, {
        method: "POST",
        signal: AbortSignal.timeout(LIMITS.messageIssueFetchTimeoutMs),
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
        body: JSON.stringify({ title, body, labels: ["task", "source:inbox"] }),
      });
      if (!res.ok) {
        const detail = redact((await res.text()).slice(0, 500)).text;
        // 5xx и 429 — временные; 4xx (403 без права, 422 кривая форма) —
        // повторять бессмысленно, это honest failed.
        return { ok: false, retryable: res.status >= 500 || res.status === 429, error: `github_${res.status}: ${detail}` };
      }
      const issue = await res.json<{ number: number; html_url: string }>();
      return { ok: true, number: issue.number, url: issue.html_url, redacted: titleRedacted.redacted || bodyRedacted.redacted };
    } catch (error) {
      return { ok: false, retryable: true, error: error instanceof Error ? error.message : String(error) };
    }
  }

  /**
   * Финальное состояние: статус + результат + отметка времени. CAS по моменту
   * захвата: если ватчдог уже вернул сообщение в new и его забрала другая
   * проходка — наша запись устарела, перезаписывать чужой результат нельзя
   * (иначе двойной issue «выиграл» бы молча, ревью PR #173, находка Б2).
   */
  #finishMessage(messageId: number, status: "done" | "failed" | "ignored", result: Record<string, unknown>, claimedTs: number): boolean {
    const cursor = this.#sql.exec(
      "UPDATE messages SET status = ?, result = ?, processed_ts = ? WHERE id = ? AND status = 'processing' AND processing_ts = ?",
      status, JSON.stringify(result), Date.now(), messageId, claimedTs,
    );
    return Number(cursor.rowsWritten) > 0;
  }

  /** Обработка одного сообщения. Захват атомарный: ровно один обработчик уводит
   *  сообщение в processing, двойная обработка при параллельном вызове невозможна. */
  async #processSingleMessage(messageId: number): Promise<Omit<MessageProcessResult, "message_id">> {
    const row = this.#rows(this.#sql.exec("SELECT status, text FROM messages WHERE id = ?", messageId))[0];
    if (!row) return { action: "not_found" };
    if (String(row.status) !== "new") return { action: "skipped" };
    const { kind, priority } = this.#classifyMessage(String(row.text));

    const claimedTs = Date.now();
    const claimed = this.#sql.exec(
      `UPDATE messages SET kind = ?, priority = ?, status = 'processing', attempts = attempts + 1, processing_ts = ?
       WHERE id = ? AND status = 'new'`,
      kind, priority, claimedTs, messageId,
    );
    if (claimed.rowsWritten === 0) return { action: "skipped" };
    this.#groupMessages(messageId);

    // directive и doc_edit — оба получают issue-след: «у каждой директивы есть
    // issue-след» относится и к правкам доков («обнови docs/INDEX.md» —
    // директива в обычном смысле), иначе целый класс команд владельца исчезает
    // из рабочих процессов (ревью head dfd167f). kind уезжает в тело issue.
    if (kind === "directive" || kind === "doc_edit") {
      const outcome = await this.#createIssue(messageId, String(row.text), kind);
      if (outcome.ok) {
        if (this.#finishMessage(messageId, "done", { issue_number: outcome.number, issue_url: outcome.url, secrets_redacted: outcome.redacted }, claimedTs)) {
          return { action: "issue_created", issue_number: outcome.number, issue_url: outcome.url, secrets_redacted: outcome.redacted };
        }
        // Наш захват устарел (ватчдог вернул сообщение в очередь): issue от
        // этой проходки мог задвоиться с новой — честно помечаем проходку.
        console.log(`inbox: pass on message ${messageId} lost ownership after issue #${outcome.number}`);
        return { action: "skipped", issue_number: outcome.number };
      }
      const attempts = Number(
        this.#rows(this.#sql.exec("SELECT attempts FROM messages WHERE id = ?", messageId))[0].attempts,
      );
      if (!outcome.retryable || attempts >= MESSAGE_MAX_ATTEMPTS) {
        if (this.#finishMessage(messageId, "failed", { error: outcome.error }, claimedTs)) {
          return { action: "issue_failed", error: outcome.error, attempts };
        }
        return { action: "skipped" };
      }
      // Повторяемая ошибка (токен не задан, сеть, 5xx) — обратно в очередь:
      // пульс или ручной process доведут; кап попыток не даст штурмовать вечно.
      // CAS: чужую захватку сбивать нельзя.
      const released = this.#sql.exec(
        "UPDATE messages SET status = 'new', processing_ts = NULL WHERE id = ? AND status = 'processing' AND processing_ts = ?",
        messageId, claimedTs,
      );
      if (Number(released.rowsWritten) === 0) return { action: "skipped" };
      return { action: "issue_retry", error: outcome.error, attempts };
    }

    if (kind === "raw") {
      // Неразобранное: классификатор детерминирован — в new возвращать нельзя
      // (сырьё забило бы очередь и голодало бы всё новое). ignored = припарковано
      // для ручного триажа, видно в списке и в счётчиках статуса.
      if (this.#finishMessage(messageId, "ignored", { note: "needs_manual_triage" }, claimedTs)) {
        return { action: "ignored" };
      }
      return { action: "skipped" };
    }

    // chat: автоматического действия в фазе 1 нет — разобрано и припарковано
    // с явной пометкой; триаж (морда, фаза 2) решает дальнейшее.
    if (this.#finishMessage(messageId, "done", { kind, note: "classified_for_manual_review" }, claimedTs)) {
      return { action: "parked" };
    }
    return { action: "skipped" };
  }

  /** Ватчдог: processing дольше порога — изолят умер посреди внешнего вызова.
   *  Возврат в new; attempts сохраняются, а исчерпанный кап на этом пути тоже
   *  проверяется — иначе сообщение ходит по кругу reclaim → claim вечно
   *  (ревью head dfd167f): кап исчерпан — честный failed со stuck_reclaimed,
   *  оживить может retry_failed после устранения причины. Порог — единственное
   *  место правды: чистая messageStuck (гвардится и юнит-тестом границы, и
   *  сквозным тестом alarm ниже). */
  #reclaimStuckMessages(): number {
    const now = Date.now();
    const candidates = this.#rows(
      this.#sql.exec("SELECT id, attempts, processing_ts FROM messages WHERE status = 'processing' AND processing_ts IS NOT NULL"),
    );
    let reclaimed = 0;
    for (const row of candidates) {
      if (!messageStuck(Number(row.processing_ts), now)) continue;
      if (Number(row.attempts) >= MESSAGE_MAX_ATTEMPTS) {
        // CAS по моменту захвата: чужой результат не перезаписываем.
        this.#finishMessage(Number(row.id), "failed", { error: "stuck_reclaimed", attempts: Number(row.attempts) }, Number(row.processing_ts));
        continue;
      }
      reclaimed += Number(
        this.#sql.exec(
          "UPDATE messages SET status = 'new', processing_ts = NULL WHERE id = ? AND status = 'processing'",
          Number(row.id),
        ).rowsWritten,
      );
    }
    return reclaimed;
  }

  /** Разбор очереди новых сообщений. retry_failed — ручной газ: failed снова в
   *  new с обнулёнными попытками (после устранения причины, например установки
   *  GH_ISSUES_TOKEN). */
  async #processInbox(limit: number, retryFailed: boolean): Promise<MessageProcessResult[]> {
    if (retryFailed) {
      this.#sql.exec(
        "UPDATE messages SET status = 'new', attempts = 0, processing_ts = NULL WHERE status = 'failed'",
      );
    }
    const rows = this.#rows(
      this.#sql.exec(
        "SELECT id FROM messages WHERE status = 'new' ORDER BY priority DESC, ts ASC, id ASC LIMIT ?",
        limit,
      ),
    );
    const results: MessageProcessResult[] = [];
    for (const row of rows) {
      const id = Number(row.id);
      results.push({ message_id: id, ...(await this.#processSingleMessage(id)) });
    }
    return results;
  }

  /**
   * Приём сообщения владельца в инбокс. Авторизация — обычная (Bearer/кука):
   * эндпоинт админско-релейный, прямая доставка вебхуком Telegram не подключена
   * (для неё нужен свой секрет — отдельное решение). Понимает и плоскую форму
   * (source, source_msg_id, …), и сырой Telegram update.
   */
  #postMessageIngest(request: Request): Promise<Response> {
    return this.#readJson(request).then((body) => {
      const message = asObject(body.message);
      const from = asObject(body.from) ?? asObject(message?.from);
      const chat = asObject(body.chat) ?? asObject(message?.chat);
      const source = asString(body.source) ?? (body.update_id !== undefined || message ? "telegram" : "api");
      // Ключ идемпотентности: явный source_msg_id, иначе update_id (ключ ретраев
      // Telegram), иначе message_id. Только числа Telegram нормализуются в строку.
      const sourceMsgId =
        asString(body.source_msg_id) ?? asString(body.update_id) ?? asString(message?.message_id);
      if (!sourceMsgId) throw new ApiError(400, "need_source_msg_id");

      const text = asString(body.text) ?? asString(message?.text) ?? "";
      if (!text.trim()) throw new ApiError(400, "need_text");
      if (text.length > MESSAGE_MAX_CHARS) {
        throw new ApiError(413, "message_too_large", { limit: MESSAGE_MAX_CHARS });
      }
      const chatId = asString(body.chat_id) ?? asString(chat?.id);
      const senderId = asString(body.sender_id) ?? asString(from?.id);
      const senderName =
        asString(body.sender_name) ?? asString(from?.username) ?? asString(from?.first_name);

      const stored = this.#putMessage({ source, sourceMsgId, chatId, senderId, senderName, text });
      if (!stored.fresh) {
        // Ретрай (Telegram повторяет апдейты с тем же update_id) — в ту же строку.
        // Статусы не менялись — broadcast не нужен: несимметрия осознанная.
        return this.#json({ message_id: stored.id, status: "exists" });
      }
      this.#broadcastStatus();
      return this.#json({ message_id: stored.id, status: "accepted" }, { status: 201 });
    });
  }

  /**
   * Единственная точка вставки сообщения (ingest и ручной POST — один класс).
   * Идемпотентность — пречтением (по образцу журнала: rowsWritten после
   * ON CONFLICT DO NOTHING в workerd признаком «свежести» не служит); блок
   * синхронный после readJson — гонки двух одновременных вставок нет.
   */
  #putMessage(f: {
    source: string; sourceMsgId: string;
    chatId: string | null; senderId: string | null; senderName: string | null;
    text: string;
  }): { id: number; fresh: boolean } {
    const existing = this.#rows(
      this.#sql.exec("SELECT id FROM messages WHERE source = ? AND source_msg_id = ?", f.source, f.sourceMsgId),
    )[0];
    if (existing) return { id: Number(existing.id), fresh: false };
    this.#sql.exec(
      `INSERT INTO messages (ts, source, source_msg_id, chat_id, sender_id, sender_name, text, kind, priority, status)
       VALUES (?, ?, ?, ?, ?, ?, ?, 'raw', 0, 'new')
       ON CONFLICT(source, source_msg_id) DO NOTHING`,
      Date.now(), f.source, f.sourceMsgId, f.chatId, f.senderId, f.senderName, f.text,
    );
    const row = this.#rows(
      this.#sql.exec("SELECT id FROM messages WHERE source = ? AND source_msg_id = ?", f.source, f.sourceMsgId),
    )[0];
    return { id: Number(row.id), fresh: true };
  }

  /** Ручное создание сообщения (для тестов/админа) — тот же класс идемпотентности,
   *  что и ingest: повтор с тем же source_msg_id отвечает существующей строкой.
   *  Без идентификатора — 400: молча сгенерированный одноразовый id превращает
   *  повтор админа во второй issue в пуле (п.31 спеки, ревью #173). */
  #postMessage(request: Request): Promise<Response> {
    return this.#readJson(request).then((body) => {
      const text = typeof body.text === "string" ? body.text : "";
      if (!text || !text.trim()) {
        throw new ApiError(400, "need_text");
      }
      if (text.length > MESSAGE_MAX_CHARS) {
        throw new ApiError(413, "message_too_large", { limit: MESSAGE_MAX_CHARS });
      }
      const sourceMsgId = asString(body.source_msg_id);
      if (!sourceMsgId) throw new ApiError(400, "need_source_msg_id");
      const stored = this.#putMessage({
        source: asString(body.source) ?? "api",
        sourceMsgId,
        chatId: asString(body.chat_id),
        senderId: asString(body.sender_id),
        senderName: asString(body.sender_name),
        text,
      });
      if (!stored.fresh) {
        return this.#json({ message_id: stored.id, status: "exists" });
      }
      this.#broadcastStatus();
      return this.#json({ message_id: stored.id, status: "created" }, { status: 201 });
    });
  }

  /** Список сообщений с фильтрами. Пагинация курсором по id ПРОТИВ сортировки:
   *  выдача newest-first (ts DESC, id DESC) — следующая страница это id < after. */
  #getMessages(url: URL): Response {
    const status = url.searchParams.get("status");
    const kind = url.searchParams.get("kind");
    const limit = Math.min(this.#intParam(url, "limit", LIMITS.messagesListDefault), LIMITS.messagesListMax);
    const after = this.#intParam(url, "after", 0);
    const senderId = url.searchParams.get("sender_id");

    const filters = ["WHERE 1=1"];
    const params: (string | number)[] = [];
    if (after > 0) {
      filters.push("AND id < ?");
      params.push(after);
    }
    if (status) {
      filters.push("AND status = ?");
      params.push(status);
    }
    if (kind) {
      filters.push("AND kind = ?");
      params.push(kind);
    }
    if (senderId) {
      filters.push("AND sender_id = ?");
      params.push(senderId);
    }

    const rows = this.#rows(
      this.#sql.exec(
        `SELECT id, ts, source, source_msg_id, chat_id, sender_id, sender_name, text, kind, priority,
                status, result, grouped_with, attempts, processing_ts, processed_ts
         FROM messages ${filters.join(" ")} ORDER BY ts DESC, id DESC LIMIT ?`,
        ...params, limit + 1,
      ),
    );

    const hasMore = rows.length > limit;
    const messages: MessageRow[] = rows.slice(0, limit).map((row) => this.#messageRow(row));
    const nextAfter = messages.length ? messages[messages.length - 1].id : after;
    return this.#json(
      { messages, has_more: hasMore, next_after: nextAfter },
      { headers: { "x-has-more": hasMore ? "true" : "false", "x-next-after": String(nextAfter) } },
    );
  }

  /** Одно сообщение по id. */
  #message(id: string): MessageRow | null {
    const row = this.#rows(this.#sql.exec("SELECT * FROM messages WHERE id = ?", id))[0];
    return row ? this.#messageRow(row) : null;
  }

  #messageRow(row: Record<string, SqlStorageValue>): MessageRow {
    return {
      id: Number(row.id),
      ts: Number(row.ts),
      source: String(row.source),
      source_msg_id: row.source_msg_id === null || row.source_msg_id === undefined ? null : String(row.source_msg_id),
      chat_id: row.chat_id === null || row.chat_id === undefined ? null : String(row.chat_id),
      sender_id: row.sender_id === null || row.sender_id === undefined ? null : String(row.sender_id),
      sender_name: row.sender_name === null || row.sender_name === undefined ? null : String(row.sender_name),
      text: String(row.text),
      kind: String(row.kind),
      priority: Number(row.priority),
      status: String(row.status),
      result: row.result === null || row.result === undefined ? null : String(row.result),
      grouped_with: row.grouped_with === null || row.grouped_with === undefined ? null : Number(row.grouped_with),
      attempts: Number(row.attempts ?? 0),
      processing_ts:
        row.processing_ts === null || row.processing_ts === undefined ? null : Number(row.processing_ts),
      processed_ts:
        row.processed_ts === null || row.processed_ts === undefined ? null : Number(row.processed_ts),
    };
  }

  /** Ручной запуск разбора (админ/тесты). Основной водитель — пульс DO.
   *  Тело опционально — единственный такой маршрут (пустое тело = значения по
   *  умолчанию, минуя общий #parseJsonText), но битый JSON — честный 400:
   *  молча трактовать его как «обработай всю очередь» нельзя (ревью PR #173). */
  async #processMessages(request: Request): Promise<Response> {
    const text = await request.text();
    const body = text.trim() ? this.#parseJsonText(text) : {};
    const limit =
      typeof body.limit === "number" && Number.isFinite(body.limit)
        ? Math.min(Math.max(1, Math.trunc(body.limit)), MESSAGE_PROCESS_MAX)
        : MESSAGE_PROCESS_MAX;
    const results = await this.#processInbox(limit, body.retry_failed === true);
    this.#broadcastStatus();
    return this.#json({ processed: results.length, results });
  }
}
