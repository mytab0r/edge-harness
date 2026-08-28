import { DurableObject } from "cloudflare:workers";

// ── Константы — каждое значение объявлено здесь один раз и читается отсюда ──────────

/** «Руки живы» = последняя отметка свежее этого порога. Единственное место правды. */
export const HEARTBEAT_FRESH_MS = 60_000;
/** Сколько событий отдавать на страницу replay'ем, если лимит не назван. */
export const DEFAULT_REPLAY_LIMIT = 100;
/** Потолок одной страницы replay. */
export const MAX_REPLAY_LIMIT = 500;
/** Потолок батча в POST /api/events. Ограничен лимитом плейсхолдеров DO SQLite
 *  (100 на statement): предчтение дублей тратит 1 + размер батча. */
export const MAX_BATCH_EVENTS = 50;
/** event_type для repository_dispatch. */
export const DISPATCH_EVENT_TYPE = "harness-task";
/** Имя единственного объекта. Мультитенантности нет, владелец один. */
export const OWNER_OBJECT_NAME = "owner";

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

export interface Status {
  now: number;
  heartbeat_fresh_ms: number;
  hands_alive: boolean;
  last_heartbeat: { ts: number; job_id: string } | null;
  tasks: Record<TaskRow["status"], number>;
  last_event_id: number;
}

/** Чистая функция порога — проверяется тестом отдельно от хранилища. */
export function handsAreAlive(now: number, lastHeartbeatTs: number | null): boolean {
  return lastHeartbeatTs !== null && now - lastHeartbeatTs < HEARTBEAT_FRESH_MS;
}

// ── Ошибки API ──────────────────────────────────────────────────────────────────────

class ApiError extends Response {
  constructor(status: number, code: string, message: string) {
    super(JSON.stringify({ error: { code, message } }), {
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
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      return await this.#route(request, url);
    } catch (error) {
      if (error instanceof ApiError) return error;
      // Неизвестная ошибка — громко, с текстом в ответе: silent-wrong дороже падения.
      const message = error instanceof Error ? error.message : String(error);
      return new ApiError(500, "internal", message);
    }
  }

  #rows(cursor: SqlStorageCursor<Record<string, SqlStorageValue>>): Record<string, SqlStorageValue>[] {
    return cursor.toArray() as Record<string, SqlStorageValue>[];
  }

  async #route(request: Request, url: URL): Promise<Response> {
    if (!this.#authorized(request, url)) {
      throw new ApiError(401, "unauthorized", "Неверный или отсутствующий HANDS_TOKEN");
    }

    if (request.method === "GET" && url.pathname === "/api/status") {
      return this.#json(this.#status());
    }
    if (request.method === "POST" && url.pathname === "/api/events") {
      return this.#postEvents(request);
    }
    if (request.method === "GET" && url.pathname === "/api/events") {
      return this.#getEvents(url);
    }
    if (request.method === "GET" && url.pathname === "/api/events.live") {
      return this.#openLiveSocket(url);
    }
    if (request.method === "POST" && url.pathname === "/api/tasks") {
      return this.#postTask(request);
    }
    if (request.method === "GET" && url.pathname === "/api/tasks") {
      return this.#json({ tasks: this.#recentTasks() });
    }
    if (request.method === "GET" && url.pathname.startsWith("/api/tasks/")) {
      const id = decodeURIComponent(url.pathname.slice("/api/tasks/".length));
      const task = this.#task(id);
      if (!task) throw new ApiError(404, "not_found", `Задача ${id} не найдена`);
      return this.#json({ task });
    }
    if (request.method === "POST" && url.pathname === "/api/heartbeat") {
      return this.#postHeartbeat(request);
    }
    throw new ApiError(404, "not_found", `Нет маршрута ${request.method} ${url.pathname}`);
  }

  // ── Аутентификация ────────────────────────────────────────────────────────────────

  #authorized(request: Request, url: URL): boolean {
    const expected = this.env.HANDS_TOKEN;
    if (!expected) return false; // секрет не задан: «возможности нет», см. ответ /api/status
    const header = request.headers.get("Authorization");
    if (header === `Bearer ${expected}`) return true;
    // WebSocket из браузера заголовки поставить не может — разрешаем токен в query.
    return url.searchParams.get("token") === expected;
  }

  #json(body: unknown, init?: ResponseInit): Response {
    return new Response(JSON.stringify(body), {
      ...init,
      headers: { "content-type": "application/json", ...init?.headers },
    });
  }

  async #readJson(request: Request): Promise<Record<string, unknown>> {
    const text = await request.text();
    if (text.length > 1_048_576) {
      throw new ApiError(413, "too_large", "Тело больше 1 MiB");
    }
    try {
      const parsed: unknown = JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("тело должно быть JSON-объектом");
      }
      return parsed as Record<string, unknown>;
    } catch (error) {
      throw new ApiError(400, "bad_json", `Некорректный JSON: ${error instanceof Error ? error.message : error}`);
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
    const hbTs = hb ? Number(hb.ts) : null;
    return {
      now,
      heartbeat_fresh_ms: HEARTBEAT_FRESH_MS,
      hands_alive: handsAreAlive(now, hbTs),
      last_heartbeat: hb ? { ts: hbTs as number, job_id: String(hb.job_id) } : null,
      tasks: counts,
      last_event_id: lastEventId,
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
      throw new ApiError(400, "bad_request", "Нужно поле task_id");
    }
    const source = typeof body.source === "string" && body.source ? body.source : "job";
    const rawEvents = body.events;
    if (!Array.isArray(rawEvents) || rawEvents.length === 0) {
      throw new ApiError(400, "bad_request", "Нужно непустое поле events — массив");
    }
    if (rawEvents.length > MAX_BATCH_EVENTS) {
      throw new ApiError(413, "too_many", `Батч больше MAX_BATCH_EVENTS=${MAX_BATCH_EVENTS}`);
    }

    const now = Date.now();
    const incoming: { seq: number; ts: number; kind: string; data: string | null }[] = [];
    for (const raw of rawEvents) {
      const event = raw as Record<string, unknown>;
      const seq = event.seq;
      if (typeof seq !== "number" || !Number.isInteger(seq) || seq <= 0) {
        throw new ApiError(400, "bad_request", "Каждое событие требует целого seq > 0");
      }
      const kind = event.kind;
      if (typeof kind !== "string" || !kind) {
        throw new ApiError(400, "bad_request", "Каждое событие требует непустой kind");
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
      accepted.push({ id, task_id: taskId, seq: event.seq, ts: event.ts, source, kind: event.kind, data: event.data === null ? null : JSON.parse(event.data) });
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
    const limit = Math.min(this.#intParam(url, "limit", DEFAULT_REPLAY_LIMIT), MAX_REPLAY_LIMIT);
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
      throw new ApiError(400, "bad_request", `Параметр ${name} должен быть целым >= 0`);
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

  // ── Очередь задач ─────────────────────────────────────────────────────────────────

  async #postTask(request: Request): Promise<Response> {
    const body = await this.#readJson(request);
    const payload = body.payload === undefined ? null : body.payload;
    if (JSON.stringify(payload)?.length > 8192) {
      throw new ApiError(413, "too_large", "payload больше 8 KiB");
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
          detail: "GH_DISPATCH_TOKEN или GH_REPO не заданы; задача лежит в очереди",
        },
        { status: 201 },
      );
    }

    let response: Response;
    try {
      response = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "Content-Type": "application/json",
          "User-Agent": "edge-harness-do",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          event_type: DISPATCH_EVENT_TYPE,
          client_payload: { task_id: id },
        }),
      });
    } catch (error) {
      this.#emitSystemEvent(id, "dispatch_failed", { detail: String(error) });
      this.#broadcastStatus();
      throw new ApiError(502, "dispatch_failed", `GitHub API недоступен: ${error instanceof Error ? error.message : error}`);
    }

    if (response.status !== 204) {
      // 204 от dispatch — не доказательство запуска job'а (и 204 при отсутствии
      // workflow-файла на default branch тоже). Ловится отсутствием job_start, см.
      // docs/research/21-github-actions.md. Здесь доказываем только приём события API.
      this.#emitSystemEvent(id, "dispatch_failed", { github_status: response.status });
      this.#broadcastStatus();
      throw new ApiError(502, "dispatch_failed", `GitHub ответил ${response.status} на dispatch (ожидался 204)`);
    }

    this.#sql.exec("UPDATE tasks SET status = 'dispatched', dispatch_ts = ? WHERE id = ?", now, id);
    this.#emitSystemEvent(id, "task_dispatched", {});
    this.#broadcastStatus();
    return this.#json({ task_id: id, dispatched: true }, { status: 201 });
  }

  #recentTasks(): TaskRow[] {
    return this.#rows(
      this.#sql.exec("SELECT id, created_ts, dispatch_ts, latency_ms, status FROM tasks ORDER BY created_ts DESC LIMIT 100"),
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
      throw new ApiError(400, "bad_request", "Нужно поле job_id");
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
