import { DurableObject } from "cloudflare:workers";
import { DSH_EDGE_UPDATE, GITHUB, HEARTBEAT, LIMITS, SESSION } from "./config";
import { msg } from "./messages";
import { matchRoute } from "./api-spec";

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
 */
export async function fetchLatestOrchestraRunId(
  token: string,
  repo: string,
  fetchImpl: typeof fetch,
): Promise<number | null> {
  try {
    const res = await fetchImpl(
      `${GITHUB.apiBase}/repos/${repo}/actions/workflows/${HEARTBEAT.orchestraWorkflow}/runs?per_page=1`,
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

export function handsAreAlive(now: number, lastHeartbeatTs: number | null): boolean {
  return lastHeartbeatTs !== null && now - lastHeartbeatTs < LIMITS.heartbeatFreshMs;
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
  if (lastPulse.detail === HEARTBEAT.notConfiguredDetail) return true;
  if (!lastPulse.dispatch_ok) return false;
  if (lastPulse.run_confirmed === false) return false;
  return now - lastPulse.ts < HEARTBEAT.selfOrchestrationMs * 2;
}

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

// ── Durable Object ──────────────────────────────────────────────────────────────────

export class Harness extends DurableObject<Env> {
  #sql: SqlStorage;

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
      .catch(() => {});
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      return await this.#route(request, url);
    } catch (error) {
      if (error instanceof ApiError) return error;
      // Неизвестная ошибка — громко, с текстом в ответе: silent-wrong дороже падения.
      const detail = error instanceof Error ? error.message : String(error);
      return new ApiError(500, "internal", { detail });
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
    if (!(await this.#authorized(request))) {
      throw new ApiError(401, "unauthorized");
    }

    // Роутинг табличный: метод+путь ищутся в api-spec.json. Добавить маршрут можно
    // только через спеку — «объявлен в спеке, но не подключён» и наоборот невозможны.
    const matched = matchRoute(request.method, url.pathname);
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

  async #hmac(payload: string): Promise<string> {
    const encoder = new TextEncoder();
    const key = await crypto.subtle.importKey(
      "raw",
      encoder.encode(this.env.SESSION_SECRET),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(payload));
    return [...new Uint8Array(mac)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
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
    const text = await request.text();
    if (text.length > LIMITS.bodyMaxBytes) {
      throw new ApiError(413, "too_large", { limit: LIMITS.bodyMaxBytes });
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

  #status(): Status {
    const now = Date.now();
    const hb = this.#rows(this.#sql.exec("SELECT ts, job_id FROM heartbeat WHERE id = 1"))[0];
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
    const lastEventId = Number(this.#rows(this.#sql.exec("SELECT COALESCE(MAX(id), 0) AS m FROM events"))[0].m);
    const stale = this.#rows(
      this.#sql.exec(
        `SELECT COUNT(*) AS n, MIN(dispatch_ts) AS oldest FROM tasks
         WHERE status = 'dispatched' AND dispatch_ts IS NOT NULL AND dispatch_ts < ?`,
        now - LIMITS.staleDispatchMs,
      ),
    )[0];
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

    return this.#json({ accepted: accepted.length, duplicates, task_id: taskId });
  }

  /** Переходы задач и сброс heartbeat живут здесь и нигде больше. */
  #applySideEffects(row: EventRow): void {
    if (row.kind === "job_start") {
      this.#sql.exec(
        "UPDATE tasks SET status = 'running' WHERE id = ? AND status NOT IN ('done', 'failed')",
        row.task_id,
      );
    }
    if (row.kind === "job_end") {
      const failed = (row.data as { result?: string } | null)?.result === "fail";
      this.#sql.exec("UPDATE tasks SET status = ? WHERE id = ?", failed ? "failed" : "done", row.task_id);
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
    // Тик перезакладывается ПЕРВОЙ строкой, до какого-либо I/O: ничто ниже не
    // может убить цепочку (issue #269 — таблица ловушек для этого места).
    await this.ctx.storage.setAlarm(Date.now() + HEARTBEAT.selfOrchestrationMs);
    const token = this.env.GH_DISPATCH_TOKEN;
    const repo = this.env.GH_REPO;
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
    this.#recordPulse(result.ok, result.detail, latestRunId, runConfirmed);
    if (!result.ok || runConfirmed === false) {
      // Пульс не роняет объект: тик уже перезаложен. Теперь исход ещё и в
      // durable-состоянии (#status().last_pulse) — раньше он тонул в console.log,
      // который никто не смотрит между дедами (fail loud, issue #269).
      console.log(
        `heartbeat dispatch: accepted=${result.ok} detail=${result.detail} run_confirmed=${runConfirmed}`,
      );
    }
    try {
      await this.#checkDshEdgeUpdate(token, repo);
    } catch (error) {
      console.log(`dsh-edge update check failed: ${error instanceof Error ? error.message : error}`);
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
    this.#emitSystemEvent(id, "task_queued", { payload });
    this.#broadcastStatus();

    const token = this.env.GH_DISPATCH_TOKEN;
    const repo = this.env.GH_REPO;
    if (!token || !repo) {
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

    let response: Response;
    try {
      response = await fetch(`${GITHUB.apiBase}/repos/${repo}/dispatches`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": GITHUB.userAgent,
          "X-GitHub-Api-Version": GITHUB.apiVersion,
        },
        body: JSON.stringify({
          event_type: GITHUB.dispatchEventType,
          client_payload: { task_id: id },
        }),
      });
    } catch (error) {
      this.#emitSystemEvent(id, "dispatch_failed", { detail: String(error) });
      this.#broadcastStatus();
      const detail = error instanceof Error ? error.message : String(error);
      throw new ApiError(502, "dispatch_network_failed", { detail });
    }

    if (response.status !== 204) {
      // 204 от dispatch — не доказательство запуска job'а (и 204 при отсутствии
      // workflow-файла на default branch тоже). Ловится отсутствием job_start, см.
      // docs/research/21-github-actions.md. Здесь доказываем только приём события API.
      this.#emitSystemEvent(id, "dispatch_failed", { github_status: response.status });
      this.#broadcastStatus();
      throw new ApiError(502, "dispatch_rejected", { status: response.status });
    }

    this.#sql.exec("UPDATE tasks SET status = 'dispatched', dispatch_ts = ? WHERE id = ?", now, id);
    this.#emitSystemEvent(id, "task_dispatched", {});
    this.#broadcastStatus();
    return this.#json({ task_id: id, dispatched: true }, { status: 201 });
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
}
