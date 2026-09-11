import { createScheduledController, runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it, vi } from "vitest";
import { asString, classifyStorageError, handsAreAlive, messageStuck, parseOwnerDecisionCallback, storageErrorResponse } from "../src/harness";
import worker from "../src/index";

import { redact } from "../src/redact";
import { DSH_EDGE_UPDATE, HEARTBEAT, LIMITS, RETENTION } from "../src/config";

// ВАЖНО: vitest-плагин Cloudflare НЕ изолирует хранилище DO между тестами одного файла
// (проверено пробами). Поэтому каждый тест работает только со своими task_id (uuid) и
// делает утверждения, отфильтрованные по ним, — никогда по общему количеству строк.
const AUTH = { Authorization: "Bearer test-token" };

// Loopback к дефолтному экспорту воркера (SELF из cloudflare:test — deprecated).
const WORKER = { fetch: (input: string, init?: RequestInit) => exports.default.fetch(input, init) };

let counter = 0;
function uniqueTaskId(prefix: string): string {
  return `${prefix}-${Date.now()}-${counter++}`;
}

async function postJson(path: string, body: unknown): Promise<Response> {
  return WORKER.fetch(`https://example.com${path}`, {
    method: "POST",
    headers: { ...AUTH, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

// Вебхук Telegram (#254): реальный вызов не несёт ни Bearer, ни куки — только
// секретный заголовок из setWebhook(secret_token=...). Тестовое значение — то
// же, что в vitest.config.ts (TELEGRAM_WEBHOOK_SECRET: "test-webhook-secret").
async function postTelegramWebhook(body: unknown, secret: string | null = "test-webhook-secret"): Promise<Response> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (secret !== null) headers["X-Telegram-Bot-Api-Secret-Token"] = secret;
  return WORKER.fetch("https://example.com/api/messages/ingest", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

async function getJson<T>(path: string): Promise<T> {
  return WORKER.fetch(`https://example.com${path}`, { headers: AUTH }).then((res) => res.json<T>());
}

async function allEventsFor(taskId: string): Promise<{ id: number; seq: number; kind: string; task_id: string }[]> {
  const events: { id: number; seq: number; kind: string; task_id: string }[] = [];
  let after = 0;
  for (;;) {
    const page = await getJson<{ events: typeof events; has_more: boolean; next_after: number }>(
      `/api/events?after=${after}&limit=200`,
    );
    events.push(...page.events.filter((event) => event.task_id === taskId));
    if (!page.has_more) return events;
    after = page.next_after;
  }
}

// Заглушки fetch распознают repository_dispatch по хосту и пути, а не
// подстрокой: «api.github.com» может сидеть где угодно в URL
// (CodeQL js/incomplete-url-substring-sanitization, ревью PR #173).
// Путь — голый /repos/<owner>/<repo>/dispatches (тот же REST-вызов, что и
// очередь задач /api/tasks — событие различают по event_type в теле), а НЕ
// /repos/.../actions/workflows/<name>/dispatches (workflow_dispatch пульса
// оркестратора и деплоя dsh-edge — другой класс вызова, той же заглушкой не
// перехватывается).
function isGitHubRepositoryDispatchCall(input: string | URL | Request): boolean {
  try {
    const url = new URL(String(input));
    return url.hostname === "api.github.com" && /^\/repos\/[^/]+\/[^/]+\/dispatches$/.test(url.pathname);
  } catch {
    return false;
  }
}

// Тот же приём для repository_dispatch решения владельца (#254) и для
// вызовов Bot API (host+хвост пути метода, не подстрокой).
function isGitHubDispatchCall(input: string | URL | Request): boolean {
  try {
    const url = new URL(String(input));
    return url.hostname === "api.github.com" && url.pathname.endsWith("/dispatches");
  } catch {
    return false;
  }
}

function telegramApiMethod(input: string | URL | Request): string | null {
  try {
    const url = new URL(String(input));
    if (url.hostname !== "api.telegram.org") return null;
    return url.pathname.split("/").pop() ?? null;
  } catch {
    return null;
  }
}

describe("аутентификация", () => {
  it("без токена — 401", async () => {
    const res = await WORKER.fetch("https://example.com/api/status");
    expect(res.status).toBe(401);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("unauthorized");
  });

  it("с чужим токеном — 401, с правильным — 200", async () => {
    const bad = await WORKER.fetch("https://example.com/api/status", {
      headers: { Authorization: "Bearer wrong" },
    });
    expect(bad.status).toBe(401);
    const good = await WORKER.fetch("https://example.com/api/status", { headers: AUTH });
    expect(good.status).toBe(200);
  });

  it("неизвестный маршрут — 404 с кодом", async () => {
    const res = await WORKER.fetch("https://example.com/api/nope", { headers: AUTH });
    expect(res.status).toBe(404);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("not_found");
  });

  it("токен в query отклоняется громко (400 query_token_removed), даже совпадающий по значению", async () => {
    const res = await WORKER.fetch("https://example.com/api/status?token=test-token", { headers: AUTH });
    expect(res.status).toBe(400);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("query_token_removed");
    // И у WebSocket — класс «токен в URL/логах/истории браузера» закрыт везде.
    const wsRes = await WORKER.fetch("https://example.com/api/events.live?token=test-token", {
      headers: { ...AUTH, Upgrade: "websocket" },
    });
    expect(wsRes.status).toBe(400);
    expect((await wsRes.json<{ error: { code: string } }>()).error.code).toBe("query_token_removed");
  });
});

describe("сессия браузера: обмен токена на куку", () => {
  // Секрет — тот же, что вшит в vitest.config.ts (bindings.miniflare).
  const SECRET = "test-session-secret";
  const encoder = new TextEncoder();

  async function hmac(payload: string): Promise<string> {
    const key = await crypto.subtle.importKey("raw", encoder.encode(SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const mac = await crypto.subtle.sign("HMAC", key, encoder.encode(payload));
    return [...new Uint8Array(mac)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async function login(): Promise<Response> {
    return WORKER.fetch("https://example.com/api/session", { method: "POST", headers: AUTH });
  }

  function cookiePairOf(res: Response): string {
    return (res.headers.get("set-cookie") ?? "").split(";")[0];
  }

  it("обмен Bearer-токена на куку: HttpOnly, SameSite=Strict, Secure, TTL около 30 дней", async () => {
    const res = await login();
    expect(res.status).toBe(200);
    const setCookie = res.headers.get("set-cookie") ?? "";
    expect(cookiePairOf(res).startsWith("harness_session=")).toBe(true);
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=Strict");
    expect(setCookie).toContain("Secure");
    const maxAge = Number(setCookie.match(/Max-Age=(\d+)/)?.[1]);
    expect(maxAge).toBeGreaterThan(29 * 24 * 3600);
    const body = await res.json<{ ok: boolean; expires_at: number }>();
    expect(body.ok).toBe(true);
    expect(body.expires_at).toBeGreaterThan(Date.now());
  });

  it("обмен с чужим токеном — 401", async () => {
    const res = await WORKER.fetch("https://example.com/api/session", {
      method: "POST",
      headers: { Authorization: "Bearer wrong" },
    });
    expect(res.status).toBe(401);
  });

  it("кукой можно ходить в API и открывать WebSocket без Bearer и без токена в URL", async () => {
    const cookie = cookiePairOf(await login());
    const status = await WORKER.fetch("https://example.com/api/status", { headers: { Cookie: cookie } });
    expect(status.status).toBe(200);

    const wsRes = await WORKER.fetch("https://example.com/api/events.live?after=0", {
      headers: { Cookie: cookie, Upgrade: "websocket" },
    });
    expect(wsRes.status).toBe(101);
    wsRes.webSocket!.accept();
    wsRes.webSocket!.close();
  });

  it("поддельная подпись — 401", async () => {
    const payload = `v1:${Math.floor(Date.now() / 1000) + 3600}`;
    const value = `${payload}.${"0".repeat(64)}`;
    const res = await WORKER.fetch("https://example.com/api/status", {
      headers: { Cookie: `harness_session=${value}` },
    });
    expect(res.status).toBe(401);
  });

  it("протухшая, но честно подписанная кука — 401", async () => {
    const payload = `v1:${Math.floor(Date.now() / 1000) - 10}`;
    const value = `${payload}.${await hmac(payload)}`;
    const res = await WORKER.fetch("https://example.com/api/status", {
      headers: { Cookie: `harness_session=${value}` },
    });
    expect(res.status).toBe(401);
    expect((await res.json<{ error: { code: string } }>()).error.code).toBe("unauthorized");
  });

  it("DELETE /api/session отвечает кукой с Max-Age=0 — браузер её выбрасывает", async () => {
    const cookie = cookiePairOf(await login());
    const del = await WORKER.fetch("https://example.com/api/session", { method: "DELETE", headers: { Cookie: cookie } });
    expect(del.status).toBe(200);
    const setCookie = (del.headers.get("set-cookie") ?? "").split(";").map((part) => part.trim().toLowerCase());
    expect(setCookie).toContain("max-age=0");
    // Кука stateless (подпись без серверного списка сессий — принятая плата,
    // openspec/changes/session-cookie-auth): отзыв на сервере = вращение
    // SESSION_SECRET. Выбрасывает куку именно браузер, получив Max-Age=0.
  });
});

describe("журнал: приём батчей", () => {
  it("принимает батч и возвращает его replay'ем", async () => {
    const taskId = uniqueTaskId("accept");
    const post = await postJson("/api/events", {
      task_id: taskId,
      events: [
        { seq: 1, kind: "job_start", data: { job: "run-1" } },
        { seq: 2, kind: "log", data: "строка вывода" },
      ],
    });
    expect(post.status).toBe(200);
    const postBody = await post.json<{ accepted: number; duplicates: number; task_id: string }>();
    expect(postBody).toEqual({ accepted: 2, duplicates: 0, task_id: taskId });

    const events = await allEventsFor(taskId);
    expect(events.map((event) => event.kind)).toEqual(["job_start", "log"]);
  });

  it("повторная доставка батча не двоит журнал — тест-гвардия идемпотентности", async () => {
    const taskId = uniqueTaskId("idem");
    const batch = {
      task_id: taskId,
      events: [
        { seq: 1, kind: "a" },
        { seq: 2, kind: "b" },
        { seq: 3, kind: "c" },
      ],
    };
    const first = await (await postJson("/api/events", batch)).json<{ accepted: number }>();
    expect(first.accepted).toBe(3);

    // Ретрай после сетевой ошибки: те же seq, те же события.
    const retry = await (await postJson("/api/events", batch)).json<{ accepted: number; duplicates: number }>();
    expect(retry).toMatchObject({ accepted: 0, duplicates: 3 });

    const events = await allEventsFor(taskId);
    expect(events).toHaveLength(3);
  });

  it("смешанный батч (новые + дубли) учитывается точно", async () => {
    const taskId = uniqueTaskId("mixed");
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 1, kind: "a" }] });
    const res = await postJson("/api/events", {
      task_id: taskId,
      events: [
        { seq: 1, kind: "a" }, // дубль
        { seq: 2, kind: "b" }, // новое
      ],
    });
    const body = await res.json<{ accepted: number; duplicates: number }>();
    expect(body).toMatchObject({ accepted: 1, duplicates: 1 });
    expect(await allEventsFor(taskId)).toHaveLength(2);
  });

  it("одинаковые seq разных задач не конфликтуют", async () => {
    const taskA = uniqueTaskId("tta");
    const taskB = uniqueTaskId("ttb");
    await postJson("/api/events", { task_id: taskA, events: [{ seq: 1, kind: "a" }] });
    await postJson("/api/events", { task_id: taskB, events: [{ seq: 1, kind: "a" }] });
    expect(await allEventsFor(taskA)).toHaveLength(1);
    expect(await allEventsFor(taskB)).toHaveLength(1);
  });

  it("кривой батч — 400, а не тихая потеря", async () => {
    const noSeq = await postJson("/api/events", { task_id: uniqueTaskId("bad"), events: [{ kind: "a" }] });
    expect(noSeq.status).toBe(400);
    const emptyBatch = await postJson("/api/events", { task_id: uniqueTaskId("bad"), events: [] });
    expect(emptyBatch.status).toBe(400);
    const noTask = await postJson("/api/events", { events: [{ seq: 1, kind: "a" }] });
    expect(noTask.status).toBe(400);
  });
});

describe("журнал: replay", () => {
  it("пагинация с заголовком «есть ещё», без дублей и пропусков", async () => {
    const taskId = uniqueTaskId("page");
    await postJson("/api/events", {
      task_id: taskId,
      events: Array.from({ length: 5 }, (_, i) => ({ seq: i + 1, kind: "e" + i })),
    });

    // Собираем весь журнал страницами по 2 — в общем хранилище вклиниваются чужие события.
    const collected: { id: number; task_id: string; seq: number }[] = [];
    let after = 0;
    for (;;) {
      const res = await WORKER.fetch(`https://example.com/api/events?after=${after}&limit=2`, { headers: AUTH });
      const page = await res.json<{ events: { id: number; task_id: string; seq: number }[]; has_more: boolean; next_after: number }>();
      collected.push(...page.events);
      if (!page.has_more) {
        expect(res.headers.get("x-has-more")).toBe("false");
        break;
      }
      expect(res.headers.get("x-has-more")).toBe("true");
      expect(page.next_after).toBe(page.events[page.events.length - 1].id);
      after = page.next_after;
    }

    const mine = collected.filter((event) => event.task_id === taskId);
    expect(mine.map((event) => event.seq)).toEqual([1, 2, 3, 4, 5]);
    // Идентификаторы монотонны и без повторов — сквозной порядок журнала не нарушен.
    const ids = collected.map((event) => event.id);
    expect([...ids].sort((a, b) => a - b)).toEqual(ids);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("статус рук", () => {
  it("свежая отметка — «руки живы»; job_end уводит статус сразу, без порога", async () => {
    const taskId = uniqueTaskId("hb");
    await postJson("/api/heartbeat", { job_id: "run-" + taskId, task_id: taskId });
    let status = await getJson<{ hands_alive: boolean; last_heartbeat: { job_id: string } }>("/api/status");
    expect(status.hands_alive).toBe(true);
    expect(status.last_heartbeat.job_id).toBe("run-" + taskId);

    await postJson("/api/events", { task_id: taskId, events: [{ seq: 1, kind: "job_end", data: { result: "ok" } }] });
    status = await getJson("/api/status");
    expect(status.hands_alive).toBe(false);
  });

  it("handsAreAlive: порог объявлен одной константой и работает на границе", async () => {
    expect(handsAreAlive(1000, null)).toBe(false);
    expect(handsAreAlive(60_000, 0)).toBe(false); // ровно порог — уже не живы
    expect(handsAreAlive(59_999, 0)).toBe(true);
  });
});

describe("очередь задач", () => {
  it("без GH_DISPATCH_TOKEN задача честно отвечает «dispatch не настроен»", async () => {
    const res = await postJson("/api/tasks", { payload: { what: "smoke" } });
    expect(res.status).toBe(201);
    const body = await res.json<{ task_id: string; dispatched: boolean; dispatch: string }>();
    expect(body.dispatched).toBe(false);
    expect(body.dispatch).toBe("not_configured");

    const task = await getJson<{ task: { status: string } }>(`/api/tasks/${body.task_id}`);
    expect(task.task.status).toBe("queued");
  });

  it("job_start → running; job_end(ok) → done; job_end(fail) → failed", async () => {
    const created = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const taskId = created.task_id;
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 1, kind: "job_start" }] });
    let task = await getJson<{ task: { status: string } }>(`/api/tasks/${taskId}`);
    expect(task.task.status).toBe("running");

    await postJson("/api/events", { task_id: taskId, events: [{ seq: 2, kind: "job_end", data: { result: "ok" } }] });
    task = await getJson(`/api/tasks/${taskId}`);
    expect(task.task.status).toBe("done");

    const second = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    await postJson("/api/events", { task_id: second.task_id, events: [{ seq: 1, kind: "job_end", data: { result: "fail" } }] });
    task = await getJson(`/api/tasks/${second.task_id}`);
    expect(task.task.status).toBe("failed");
  });

  it("список задач содержит созданную", async () => {
    const created = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const list = await getJson<{ tasks: { id: string }[] }>("/api/tasks");
    expect(list.tasks.map((task) => task.id)).toContain(created.task_id);
  });
});

describe("watchdog зависших задач", () => {
  it("dispatched-задача старше порога видна в stale_dispatch; свежая — нет", async () => {
    const created = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const taskId = created.task_id;
    // Имитируем dispatch час назад — напрямую в SQL DO (runInDurableObject).
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET status = 'dispatched', dispatch_ts = ? WHERE id = ?",
        Date.now() - 31 * 60_000, taskId,
      );
    });
    const status = await getJson<{ stale_dispatch: { count: number } }>("/api/status");
    expect(status.stale_dispatch.count).toBeGreaterThanOrEqual(1);
  });

  it("свежая queued-задача в stale_dispatch не попадает", async () => {
    const before = await getJson<{ stale_dispatch: { count: number } }>("/api/status");
    await postJson("/api/tasks", {});
    const after = await getJson<{ stale_dispatch: { count: number } }>("/api/status");
    expect(after.stale_dispatch.count).toBe(before.stale_dispatch.count);
  });
});

// Ретеншн DO SQLite (#306/#305): без него `events`/`tasks` растут вечно — тот же
// класс, что подпалил суточную квоту rows_read (#320): любой скан со временем
// дорожает. Пачка на тик, по индексу, alarm() уже существующий — новый таймер
// не заводится. Докажи мутацией: убери `LIMIT ?`/подмени на `LIMIT 100000` в
// RETENTION_TABLES[…].sql (src/harness.ts) — тест «пачка ограничена
// RETENTION.batchSize» ниже покраснеет (останется 0 старых вместо остатка).
describe("ретеншн DO SQLite (#306/#305)", () => {
  const HARNESS_ID = () => env.HARNESS.get(env.HARNESS.idFromName("owner"));

  async function insertOldEvents(taskId: string, count: number, ageMs: number): Promise<void> {
    const stub = HARNESS_ID();
    const oldTs = Date.now() - ageMs;
    await runInDurableObject(stub, async (_instance, state) => {
      for (let seq = 1; seq <= count; seq++) {
        state.storage.sql.exec(
          "INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, 'system', 'retention_test', NULL)",
          taskId, seq, oldTs,
        );
      }
    });
  }

  it("события старше RETENTION.eventsMaxAgeMs уходят, свежие того же task_id остаются", async () => {
    const taskId = uniqueTaskId("retention-events");
    await insertOldEvents(taskId, 3, RETENTION.eventsMaxAgeMs + 60_000);
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 100, kind: "fresh" }] });

    await runInDurableObject(HARNESS_ID(), async (instance) => {
      await instance.alarm();
    });

    const remaining = await allEventsFor(taskId);
    expect(remaining.map((event) => event.kind)).toEqual(["fresh"]);
  });

  it("пачка ограничена RETENTION.batchSize — не полный снос за один тик", async () => {
    const taskId = uniqueTaskId("retention-batch");
    const seeded = RETENTION.batchSize + 3;
    await insertOldEvents(taskId, seeded, RETENTION.eventsMaxAgeMs + 60_000);

    const stub = HARNESS_ID();
    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });
    const afterOneTick = await runInDurableObject(stub, async (_instance, state) =>
      state.storage.sql.exec("SELECT COUNT(*) AS n FROM events WHERE task_id = ?", taskId).toArray()[0].n,
    );
    // Один тик снимает РОВНО batchSize строк, не все 503 разом — доказательство,
    // что удаление идёт пачкой (LIMIT), а не одним запросом на всю таблицу.
    expect(Number(afterOneTick)).toBe(seeded - RETENTION.batchSize);

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });
    const afterSecondTick = await runInDurableObject(stub, async (_instance, state) =>
      state.storage.sql.exec("SELECT COUNT(*) AS n FROM events WHERE task_id = ?", taskId).toArray()[0].n,
    );
    expect(Number(afterSecondTick)).toBe(0);
  });

  it("задача done старше RETENTION.tasksMaxAgeMs уходит; queued того же возраста — нет", async () => {
    const doneTask = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const queuedTask = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const stub = HARNESS_ID();
    const oldCreatedTs = Date.now() - (RETENTION.tasksMaxAgeMs + 60_000);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET status = 'done', created_ts = ? WHERE id = ?", oldCreatedTs, doneTask.task_id,
      );
      // queued остаётся queued, но с тем же старым возрастом — ретеншн обязан
      // не трогать активные статусы независимо от того, сколько им лет.
      state.storage.sql.exec("UPDATE tasks SET created_ts = ? WHERE id = ?", oldCreatedTs, queuedTask.task_id);
    });

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });

    const doneRes = await WORKER.fetch(`https://example.com/api/tasks/${doneTask.task_id}`, { headers: AUTH });
    expect(doneRes.status).toBe(404);
    const queuedRes = await getJson<{ task: { id: string; status: string } }>(`/api/tasks/${queuedTask.task_id}`);
    expect(queuedRes.task.status).toBe("queued");
  });

  it("задача failed старше порога уходит; dispatched/running того же возраста — нет (находка ревью PR #329)", async () => {
    const failedTask = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const dispatchedTask = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const runningTask = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const stub = HARNESS_ID();
    const oldCreatedTs = Date.now() - (RETENTION.tasksMaxAgeMs + 60_000);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE tasks SET status = 'failed', created_ts = ? WHERE id = ?", oldCreatedTs, failedTask.task_id,
      );
      // dispatched/running остаются активными статусами — ретеншн обязан не
      // трогать их независимо от возраста, только queued/dispatched/running
      // проверялись раньше только через queued.
      state.storage.sql.exec(
        "UPDATE tasks SET status = 'dispatched', created_ts = ? WHERE id = ?", oldCreatedTs, dispatchedTask.task_id,
      );
      state.storage.sql.exec(
        "UPDATE tasks SET status = 'running', created_ts = ? WHERE id = ?", oldCreatedTs, runningTask.task_id,
      );
    });

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });

    const failedRes = await WORKER.fetch(`https://example.com/api/tasks/${failedTask.task_id}`, { headers: AUTH });
    expect(failedRes.status).toBe(404);
    const dispatchedRes = await getJson<{ task: { id: string; status: string } }>(
      `/api/tasks/${dispatchedTask.task_id}`,
    );
    expect(dispatchedRes.task.status).toBe("dispatched");
    const runningRes = await getJson<{ task: { id: string; status: string } }>(`/api/tasks/${runningTask.task_id}`);
    expect(runningRes.task.status).toBe("running");
  });

  it("/api/status.retention виден после тика — pruned по таблицам", async () => {
    const taskId = uniqueTaskId("retention-status");
    await insertOldEvents(taskId, 1, RETENTION.eventsMaxAgeMs + 60_000);
    await runInDurableObject(HARNESS_ID(), async (instance) => {
      await instance.alarm();
    });
    const status = await getJson<{ retention: { ts: number; pruned: Record<string, number>; backlog: boolean } | null }>(
      "/api/status",
    );
    expect(status.retention).not.toBeNull();
    expect(status.retention!.pruned).toHaveProperty("events");
    expect(status.retention!.pruned).toHaveProperty("tasks");
  });

  // Находка ревью PR #329: имя теста обещало «backlog не паникует на первом
  // полном тике», но тик в нём не был полным (1 строка из batchSize) и
  // backlog не проверялся вовсе. Здесь — настоящий полный тик (ровно
  // batchSize строк) и явный assert на backlog === false: один полный тик
  // после отката/первого деплоя — не тревога (см. docstring retentionBacklog).
  it("backlog остаётся false после одного полного тика — не паникуем на разовом всплеске", async () => {
    const taskId = uniqueTaskId("retention-single-full-tick");
    await insertOldEvents(taskId, RETENTION.batchSize, RETENTION.eventsMaxAgeMs + 60_000);
    await runInDurableObject(HARNESS_ID(), async (instance) => {
      await instance.alarm();
    });
    const status = await getJson<{ retention: { pruned: Record<string, number>; backlog: boolean } | null }>(
      "/api/status",
    );
    expect(status.retention).not.toBeNull();
    expect(status.retention!.pruned.events).toBe(RETENTION.batchSize);
    expect(status.retention!.backlog).toBe(false);
  });

  // Находка ревью PR #329: серия из RETENTION.backlogStreakThreshold подряд
  // ПОЛНЫХ тиков обязана дать backlog=true, даже если КАЖДЫЙ тик выполняется в
  // НОВОМ инстансе DO (runInDurableObject создаёт и утилизирует инстанс на
  // каждый вызов — та же модель, что и в проде: DO выгружается из памяти
  // между тиками alarm). Докажи мутацией: верни streak в поле класса
  // (`#retentionStreak`) вместо строки `retention_state` — тест обязан
  // покраснеть, потому что каждый новый инстанс увидит streak=0 и никогда не
  // накопит 4 подряд.
  it("backlog становится true после серии полных тиков даже через пересоздание DO", async () => {
    const taskId = uniqueTaskId("retention-backlog-streak");
    // С запасом на любой остаточный старый мусор от предыдущих тестов файла
    // (общее хранилище DO, см. предупреждение вверху файла): гарантированно
    // хватает на RETENTION.backlogStreakThreshold ПОЛНЫХ тиков только из
    // собственных данных этого теста.
    await insertOldEvents(taskId, RETENTION.batchSize * (RETENTION.backlogStreakThreshold + 1),
      RETENTION.eventsMaxAgeMs + 60_000);

    const stub = HARNESS_ID();
    for (let tick = 0; tick < RETENTION.backlogStreakThreshold; tick++) {
      // Каждый вызов runInDurableObject — новый инстанс класса (утилизируется
      // после callback'а): любое состояние в памяти объекта здесь не выжило бы.
      await runInDurableObject(stub, async (instance) => {
        await instance.alarm();
      });
    }
    const streak = await runInDurableObject(stub, async (_instance, state) =>
      state.storage.sql.exec("SELECT streak FROM retention_state WHERE id = 1").toArray()[0].streak,
    );
    expect(Number(streak)).toBeGreaterThanOrEqual(RETENTION.backlogStreakThreshold);

    const status = await getJson<{ retention: { backlog: boolean } | null }>("/api/status");
    expect(status.retention).not.toBeNull();
    expect(status.retention!.backlog).toBe(true);
  });

  // #575: messages была единственной таблицей БЕЗ ретеншена вовсе (рядом с
  // events/tasks выше) — росла вечно, полный скан #status() дорожал с каждым
  // днём. ГЛАВНОЕ ОГРАНИЧЕНИЕ (#20/#173): непрочитанное/необработанное
  // сообщение владельца ('new'/'processing') не трогается ни при каком
  // возрасте — фильтр по статусу в RETENTION_TABLES[…].sql структурно не
  // видит эти статусы, независимо от processed_ts/ts.
  //
  // 'processing' (не 'new') — намеренный выбор для этого теста: настоящий
  // alarm() запускает не только ретеншн, но и водитель инбокса
  // (#processInbox), который сам разбирает ВСЕ 'new' сообщения на каждом
  // тике (это штатное поведение, не потеря данных) — тест на 'new' был бы
  // ложноположительным на обычную работу драйвера, а не на регресс ретеншена.
  // processing_ts — СВЕЖИЙ (не завис): ватчдог (#reclaimStuckMessages) тоже
  // не должен трогать эту строку, изолируем именно ретеншн.
  it("сообщение done старше RETENTION.messagesMaxAgeMs уходит; processing того же возраста — нет (#20/#173)", async () => {
    const doneMsg = await (
      await postJson("/api/messages", { text: "старое обработанное", source_msg_id: uniqueTaskId("ret-msg-done") })
    ).json<{ message_id: number }>();
    const processingMsg = await (
      await postJson("/api/messages", { text: "старое в обработке", source_msg_id: uniqueTaskId("ret-msg-processing") })
    ).json<{ message_id: number }>();
    // #586, находка ревью: пара строк выше (processing/processed_ts=NULL) не
    // проверяет нагрузку фильтра по status на самом деле — сравнение
    // `processed_ts < ?` само отсеивает NULL, снятие фильтра тот случай не
    // красит. Реальная нагрузка — непустой (протухший) processed_ts на
    // НЕтерминальном статусе: ровно то, что оставляет за собой bulk
    // retry_failed (`harness.ts:1877`), если не чистить processed_ts в самом
    // UPDATE (см. отдельный тест ниже, что теперь чистит). Статус здесь —
    // 'processing' (не 'new'), тем же приёмом, что и выше в этом тесте: 'new'
    // забрал бы водитель инбокса в этом же alarm-тике (после ретеншена, но до
    // конца alarm()) и сменил бы статус независимо от результата ретеншена —
    // тест на 'new' был бы не про регресс ретеншена, а про обычную работу
    // драйвера. processing_ts — СВЕЖИЙ, чтобы и ватчдог не тронул строку:
    // изолируем именно чистку.
    const staleProcessedMsg = await (
      await postJson("/api/messages", { text: "в обработке с протухшим processed_ts", source_msg_id: uniqueTaskId("ret-msg-stale-processed") })
    ).json<{ message_id: number }>();
    const stub = HARNESS_ID();
    const oldTs = Date.now() - (RETENTION.messagesMaxAgeMs + 60_000);
    await runInDurableObject(stub, async (_instance, state) => {
      // done со старым processed_ts — кандидат на чистку.
      state.storage.sql.exec(
        "UPDATE messages SET status = 'done', processed_ts = ? WHERE id = ?", oldTs, doneMsg.message_id,
      );
      // processing того же возраста ПО ts, но статус не терминальный — чистка
      // не смотрит на этот возраст вовсе (processed_ts у processing — NULL).
      state.storage.sql.exec(
        "UPDATE messages SET ts = ?, status = 'processing', processing_ts = ? WHERE id = ?",
        oldTs, Date.now(), processingMsg.message_id,
      );
      // processing с протухшим processed_ts — непрочитанное/необработанное
      // сообщение владельца (#20/#173), не должно уйти НИ ПРИ КАКОМ возрасте,
      // а `processed_ts < ?` само по себе его бы поймало без фильтра по status.
      state.storage.sql.exec(
        "UPDATE messages SET status = 'processing', processing_ts = ?, processed_ts = ? WHERE id = ?",
        Date.now(), oldTs, staleProcessedMsg.message_id,
      );
    });

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });

    const doneRes = await WORKER.fetch(`https://example.com/api/messages/${doneMsg.message_id}`, { headers: AUTH });
    expect(doneRes.status).toBe(404);
    const processingRes = await getJson<{ message: { id: number; status: string } }>(
      `/api/messages/${processingMsg.message_id}`,
    );
    expect(processingRes.message.status).toBe("processing");
    const staleProcessedRes = await getJson<{ message: { id: number; status: string } }>(
      `/api/messages/${staleProcessedMsg.message_id}`,
    );
    expect(staleProcessedRes.message.status).toBe("processing");
  });

  // #586: retry_failed теперь чистит и processed_ts (harness.ts:1877), не
  // только status/attempts/processing_ts — иначе строка возвращалась в 'new'
  // со старым processed_ts, и «processed_ts ≠ NULL ⇒ терминальный» держалось
  // бы только фильтром по status в ретеншене, без структурной опоры в коде.
  it("bulk retry_failed чистит processed_ts вместе со status/attempts/processing_ts (#586)", async () => {
    const s = uniqueTaskId("retry-clean");
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: s,
        sender_id: s,
        text: "/task Доведись до конца",
      })
    ).json<{ message_id: number }>();

    // Доводим до failed тремя прогонами — тем самым #finishMessage ставит
    // processed_ts (терминальный переход, harness.ts:1751-1754).
    for (let i = 0; i < 3; i++) await postJson("/api/messages/process", { limit: 100 });
    const failed = await getJson<{ message: { status: string; processed_ts: number | null } }>(
      `/api/messages/${created.message_id}`,
    );
    expect(failed.message.status).toBe("failed");
    expect(failed.message.processed_ts).not.toBeNull();

    // Газ: bulk retry_failed обязан обнулить processed_ts вместе с
    // status/attempts/processing_ts — иначе строка вернулась бы в 'new' со
    // старым (протухшим) processed_ts, ровно тот случай, который фильтр по
    // status в ретеншене (14.10) прикрывает как последний рубеж, а не как
    // единственную защиту.
    await postJson("/api/messages/process", { limit: 1, retry_failed: true });
    const retried = await getJson<{ message: { status: string; processed_ts: number | null } }>(
      `/api/messages/${created.message_id}`,
    );
    expect(retried.message.status).toBe("new");
    expect(retried.message.processed_ts).toBeNull();
  });
});

// Гвардия issue #269: пульс оркестрации не должен молчать шестнадцать часов
// незамеченным. Ловит именно «alarm не переустановился после сбоя» — докажи
// мутацией: перенеси `await this.ctx.storage.setAlarm(...)` в src/harness.ts#alarm()
// на строку ПОСЛЕ `if (!token || !repo) { ...; return; }` — тест «тик
// перезакладывается» покраснеет (getAlarm() вернёт null, потому что в тестовом
// окружении GH_DISPATCH_TOKEN сознательно не задан и alarm() уходит в ранний return).
describe("пульс оркестрации: alarm() всегда перезакладывает следующий тик (issue #269)", () => {
  it("тик перезакладывается и исход фиксируется, даже когда dispatch невозможен (нет секретов)", async () => {
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (instance, state) => {
      await state.storage.deleteAlarm();
      expect(await state.storage.getAlarm()).toBeNull();
      await instance.alarm();
      // Главное утверждение гвардии: alarm() обязан перезаложить будильник
      // ДО какой-либо попытки dispatch'а — падение/отсутствие возможности внутри
      // не может убить цепочку.
      expect(await state.storage.getAlarm()).not.toBeNull();
    });
    // И исход не тонет молча: /api/status видит причину и не паникует зря —
    // «возможности нет» отличается от «возможность есть, но сломана».
    const status = await getJson<{
      last_pulse: { dispatch_ok: boolean; detail: string | null } | null;
      pulse_healthy: boolean;
      pulse_not_configured: boolean;
      pulse_stale: boolean;
    }>("/api/status");
    expect(status.last_pulse).toMatchObject({ dispatch_ok: false, detail: "not_configured" });
    expect(status.pulse_healthy).toBe(true);
    // #303, находка ревью: фронт больше не сравнивает literal "not_configured"
    // сам — сервер отдаёт готовый флаг, чтобы переименование сентинела в
    // config.ts не могло молча сломать бейдж app.js.
    expect(status.pulse_not_configured).toBe(true);
    // #303, вторая находка ревью того же PR: «возможности нет» — не «подвис
    // alarm», это разные ветки бейджа с разными причинами (см. pulseStale).
    expect(status.pulse_stale).toBe(false);
  });
});

// Cron Trigger (issue #693): scheduledTick() — страховка, вызываемая
// cf-worker/src/index.ts::scheduled() по расписанию (wrangler.jsonc
// triggers.crons), не второй основной тик. Проверяем ровно четыре пункта
// приёмки задачи: (a) будильник перезакладывается, если его не было; (b)
// dispatch срабатывает, когда пульс stale (alarm подвис); (c) не срабатывает,
// когда пульс свежий (alarm жив — не дублируем dispatch); (d) неудача
// dispatch'а попадает в пульс, а не теряется молча.
function isGitHubRunsCall(input: string | URL | Request): boolean {
  try {
    const url = new URL(String(input));
    return url.hostname === "api.github.com" && url.pathname.endsWith("/runs");
  } catch {
    return false;
  }
}

async function seedPulse(
  stub: DurableObjectStub,
  opts: { ts: number; dispatch_ok: boolean; detail: string | null; last_run_id: number | null; run_confirmed: boolean | null },
): Promise<void> {
  await runInDurableObject(stub, async (_instance, state) => {
    state.storage.sql.exec(
      `INSERT INTO pulse (id, ts, dispatch_ok, detail, last_run_id, run_confirmed) VALUES (1, ?, ?, ?, ?, ?)
       ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, dispatch_ok = excluded.dispatch_ok,
         detail = excluded.detail, last_run_id = excluded.last_run_id, run_confirmed = excluded.run_confirmed`,
      opts.ts,
      opts.dispatch_ok ? 1 : 0,
      opts.detail,
      opts.last_run_id,
      opts.run_confirmed === null ? null : opts.run_confirmed ? 1 : 0,
    );
  });
}

describe("Cron Trigger: Harness#scheduledTick() — страховка, не второй основной тик (issue #693)", () => {
  const HARNESS_ID = () => env.HARNESS.get(env.HARNESS.idFromName("owner"));
  const STALE_TS = () => Date.now() - (HEARTBEAT.selfOrchestrationMs * 2 + 60_000);
  const FRESH_TS = () => Date.now() - (HEARTBEAT.selfOrchestrationMs * 2 - 60_000);

  // (a) Докажи мутацией: убери `await this.#ensureHeartbeat();` из начала
  // scheduledTick() (src/harness.ts) — этот тест покраснеет (getAlarm()
  // останется null, потому что конструктор в этом вызове НЕ перезапускался:
  // runInDurableObject уже создал инстанс ДО callback'а, а deleteAlarm() и
  // scheduledTick() вызываются внутри одного и того же тёплого инстанса).
  it("(a) перезакладывает будильник, если его не было — даже без секретов dispatch'а", async () => {
    const stub = HARNESS_ID();
    await runInDurableObject(stub, async (instance, state) => {
      await state.storage.deleteAlarm();
      expect(await state.storage.getAlarm()).toBeNull();
      await instance.scheduledTick();
      expect(await state.storage.getAlarm()).not.toBeNull();
    });
  });

  // (b)+(d) Докажи мутацией: замени `if (!pulseStale(...)) return;` на
  // `if (false) return;` — тест (c) ниже покраснеет вместо этого (dispatch
  // случился бы всегда); замени и на `if (true) return;` — этот тест (b)
  // покраснеет (dispatch не случится, потому что stale-тик выйдет раньше).
  it("(b) пульс stale (alarm подвис) — scheduledTick сам дёргает dispatch", async () => {
    const stub = HARNESS_ID();
    await seedPulse(stub, { ts: STALE_TS(), dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    let dispatchCalls = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        dispatchCalls++;
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
      expect(dispatchCalls).toBe(1);
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean; ts: number } | null }>("/api/status");
      expect(status.last_pulse?.dispatch_ok).toBe(true);
      // Пульс обновился (не остался на старом STALE_TS) — тик реально выполнился.
      expect(status.last_pulse!.ts).toBeGreaterThan(STALE_TS());
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("(c) пульс свежий (alarm жив) — scheduledTick НЕ дублирует dispatch", async () => {
    const stub = HARNESS_ID();
    const freshTs = FRESH_TS();
    await seedPulse(stub, { ts: freshTs, dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    // Любой вызов GitHub здесь — ошибка теста: alarm жив, дублировать нечего.
    vi.stubGlobal("fetch", (async () => {
      throw new Error("scheduledTick не должен звонить в GitHub, когда пульс свежий");
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
      const status = await getJson<{ last_pulse: { ts: number } | null }>("/api/status");
      // Пульс не тронут — тот же ts, что был засеян, ни одной новой записи.
      expect(status.last_pulse?.ts).toBe(freshTs);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  // (d) Докажи мутацией: в #dispatchOrchestraTick (src/harness.ts) замени
  // `this.#recordPulse(result.ok, detail, latestRunId, runConfirmed);` на
  // отсутствие вызова (закомментируй строку) — этот тест покраснеет: пульс
  // остался бы на старом STALE_TS, а не на новом с dispatch_ok:false.
  it("(d) неудача dispatch'а (GitHub отклонил) попадает в пульс, а не теряется", async () => {
    const stub = HARNESS_ID();
    await seedPulse(stub, { ts: STALE_TS(), dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        return new Response(null, { status: 403 }); // вторичный rate-limit GitHub
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean; detail: string | null } | null }>("/api/status");
      expect(status.last_pulse).toMatchObject({ dispatch_ok: false });
      expect(status.last_pulse?.detail).toContain("403");
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  // Живой случай issue #713: последний ЗАПИСАННЫЙ пульс перед тем, как alarm()
  // подвис, сам был неудачным (dispatch_ok: false) — pulseStale() для такого
  // пульса навсегда возвращает false (не доходит до проверки возраста), и
  // старый scheduledTick() молчал бы неограниченно долго, даже пройдя порог
  // 2×selfOrchestrationMs. Докажи мутацией: замени в scheduledTick()
  // `pulseNeedsRecoveryDispatch` обратно на `pulseStale` — этот тест
  // покраснеет (dispatchCalls останется 0, pulse не обновится).
  it("(e) пульс stale И последняя попытка провалилась (dispatch_ok=false) — резервный dispatch всё равно случается (issue #713)", async () => {
    const stub = HARNESS_ID();
    await seedPulse(stub, {
      ts: STALE_TS(),
      dispatch_ok: false,
      detail: "dispatch отклонён: 403",
      last_run_id: 100,
      run_confirmed: null,
    });
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    let dispatchCalls = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        dispatchCalls++;
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
      expect(dispatchCalls).toBe(1);
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean; ts: number } | null }>("/api/status");
      expect(status.last_pulse?.dispatch_ok).toBe(true);
      expect(status.last_pulse!.ts).toBeGreaterThan(STALE_TS());
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  // Гвардия класса #133 на стороне cf-worker (находка AI-ревью PR #943):
  // Cloudflare Workers' fetch() User-Agent сам не подставляет, GitHub REST
  // отвечает 403 без JSON-тела на запрос без заголовка, а Cloudflare перед
  // мордой dsh-edge режет такие запросы по подписи UA (эксперимент #225,
  // docs/research/12). Проверяется ВЕСЬ исходящий путь alarm(): пульс
  // (runs/dispatch) и self-update dsh-edge (health/registry) — каждый вызов
  // обязан нести непустой User-Agent. Мутация-доказательство: сними заголовок
  // в ЛЮБОМ из мест harness.ts (fetchLatestOrchestraRunId,
  // attemptOrchestraDispatch, #checkDshEdgeUpdate → health/registry) —
  // этот тест краснеет.
  it("исходящие вызовы тика alarm() несут User-Agent — класс #133 (внешние API отвечают 403 без заголовка)", async () => {
    const stub = HARNESS_ID();
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const realFetch = globalThis.fetch;
    const calls: Array<{ url: URL; userAgent: string | null }> = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({ url, userAgent: new Headers(init?.headers).get("user-agent") });
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        return new Response(null, { status: 204 });
      }
      if (url.href === DSH_EDGE_UPDATE.healthUrl) {
        return new Response(JSON.stringify({ version: "9.9.9-test" }), { status: 200 });
      }
      if (url.href === DSH_EDGE_UPDATE.registryUrl) {
        return new Response(JSON.stringify({ version: "9.9.9-test" }), { status: 200 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.alarm();
      });
      // Обе ветки self-update дошли до сети: гвардия проверяет реальный путь,
      // а не пустой набор вызовов.
      const hrefs = calls.map((c) => c.url.href);
      expect(hrefs).toContain(DSH_EDGE_UPDATE.healthUrl);
      expect(hrefs).toContain(DSH_EDGE_UPDATE.registryUrl);
      for (const { url, userAgent } of calls) {
        expect(userAgent, `fetch(${url.href}) без User-Agent — внешние API отвечают 403 (класс #133)`).toBeTruthy();
      }
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("возможности нет (GH_DISPATCH_TOKEN не задан) — scheduledTick тихо выходит, не звонит в GitHub", async () => {
    const stub = HARNESS_ID();
    await seedPulse(stub, { ts: STALE_TS(), dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    vi.stubGlobal("fetch", (async () => {
      throw new Error("без GH_DISPATCH_TOKEN scheduledTick не должен звонить в GitHub вовсе");
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  // Последнее звено цепочки (находка ревью PR #696): до этого теста ни один
  // тест не вызывал сам src/index.ts::scheduled() — только Harness#scheduledTick()
  // напрямую. Докажи мутацией: замени тело scheduled() на пустую функцию (не
  // вызывай env.HARNESS.get(id).scheduledTick() вовсе) — этот тест покраснеет,
  // потому что dispatch не случится и pulse останется на STALE_TS.
  it("scheduled(): index.ts дёргает тот же биндинг HARNESS и тот же scheduledTick()", async () => {
    const stub = HARNESS_ID();
    await seedPulse(stub, { ts: STALE_TS(), dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    let dispatchCalls = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        dispatchCalls++;
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const controller = createScheduledController({ cron: "*/5 * * * *" });
      await worker.scheduled!(controller, env);
      expect(dispatchCalls).toBe(1);
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean; ts: number } | null }>("/api/status");
      expect(status.last_pulse?.dispatch_ok).toBe(true);
      expect(status.last_pulse!.ts).toBeGreaterThan(STALE_TS());
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  // Докажи мутацией: убери try/catch вокруг `env.HARNESS.get(id).scheduledTick()`
  // в src/index.ts::scheduled() — этот тест покраснеет (необработанное исключение
  // из RPC-вызова упадёт наружу вместо честного console.error).
  it("scheduled(): падение RPC-вызова ловится, обработчик не роняется наружу", async () => {
    const getSpy = vi.spyOn(env.HARNESS, "get").mockImplementation(() => {
      throw new Error("boom: RPC-обвязка упала до scheduledTick()");
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const controller = createScheduledController({ cron: "*/5 * * * *" });
      await expect(worker.scheduled!(controller, env)).resolves.toBeUndefined();
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining("scheduled: RPC scheduledTick упал"));
    } finally {
      getSpy.mockRestore();
      errorSpy.mockRestore();
    }
  });
});

describe("живой поток", () => {
  async function openSocket(after = 0): Promise<WebSocket> {
    const res = await WORKER.fetch(`https://example.com/api/events.live?after=${after}`, {
      headers: { ...AUTH, Upgrade: "websocket" },
    });
    expect(res.status).toBe(101);
    const ws = res.webSocket!;
    ws.accept();
    return ws;
  }

  function nextMessage(ws: WebSocket, timeoutMs = 2000): Promise<MessageEvent> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("сокет не прислал сообщение вовремя")), timeoutMs);
      ws.addEventListener("message", (event) => {
        clearTimeout(timer);
        resolve(event);
      }, { once: true });
    });
  }

  function nextClose(ws: WebSocket, timeoutMs = 2000): Promise<CloseEvent> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("сокет не закрылся вовремя")), timeoutMs);
      ws.addEventListener("close", (event) => {
        clearTimeout(timer);
        resolve(event);
      }, { once: true });
    });
  }

  // Примечание: доставку broadcast'ом из других вызовов vitest-петля плагина не
  // гарантирует (проверено пробами: send на сервере успешен, клиент не получает).
  // Живой поток end-to-end проверяет scripts/smoke-local.mjs на wrangler dev —
  // там настоящий workerd с настоящими сокетами.

  it("клиент, пишущий в сокет, теряет соединение (1008 downlink only)", async () => {
    const ws = await openSocket();
    await nextMessage(ws); // hello
    ws.send("клиент не должен писать в downlink-only сокет");
    const close = await nextClose(ws);
    expect(close.code).toBe(1008);
  });

  it("hello несёт статус с порогом свежести из одного места", async () => {
    const ws = await openSocket(0);
    const hello = JSON.parse((await nextMessage(ws)).data as string);
    expect(hello.type).toBe("hello");
    expect(hello.status.heartbeat_fresh_ms).toBe(60_000);
    ws.close();
  });
});

describe("класс ошибки хранилища (#320)", () => {
  it("ловит формулировки квоты rows_read/rows_written и общий 'quota'", () => {
    expect(classifyStorageError("exceeded the daily Durable Objects free tier limit of 5000000 rows_read"))
      .toBe("quota_exceeded");
    expect(classifyStorageError("rows_written limit exceeded")).toBe("quota_exceeded");
    expect(classifyStorageError("quota exceeded")).toBe("quota_exceeded");
  });

  it("не путает обычную ошибку с квотой", () => {
    expect(classifyStorageError("network timeout")).toBe("unknown");
    expect(classifyStorageError("unexpected token in JSON")).toBe("unknown");
  });
});

// Спека 5.1: маппинг classifyStorageError → HTTP-код проверялся раньше только на
// самой функции классификации, не на месте применения — общем catch #fetch()
// (находка ревью PR #321). storageErrorResponse — тот же код, что реально уходит
// клиенту (fetch() зовёт именно эту функцию), гоняем его напрямую вместо того,
// чтобы реально исчерпывать суточную квоту DO в тесте — такой отказ вне окна
// инцидента не воспроизвести (docs/research/20, «не подтверждено»).
describe("ответ на ошибку хранилища (спека 5.1, #320)", () => {
  it("текст похож на исчерпание квоты — 500 storage_quota_exceeded", async () => {
    const res = storageErrorResponse("exceeded the daily Durable Objects free tier limit of rows_read");
    expect(res.status).toBe(500);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("storage_quota_exceeded");
  });

  it("обычная ошибка — 500 internal, не квота", async () => {
    const res = storageErrorResponse("network timeout");
    expect(res.status).toBe(500);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("internal");
  });
});

// Спека 14.4: тест 14.3 («dispatched-задача старше порога видна в stale_dispatch»)
// гвардит только видимость зависшей задачи, не использование индекса — это другое
// требование (находка ревью PR #321). EXPLAIN QUERY PLAN того же запроса, что
// #status() реально исполняет, обязан ссылаться на tasks_status_dispatch —
// докажи мутацией: убери строку `CREATE INDEX tasks_status_dispatch` из SCHEMA,
// тест покраснеет (план перейдёт на SCAN TABLE tasks).
describe("watchdog-запрос использует индекс tasks(status, dispatch_ts) (спека 14.4, #320)", () => {
  it("EXPLAIN QUERY PLAN ссылается на tasks_status_dispatch", async () => {
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (_instance, state) => {
      const plan = state.storage.sql
        .exec(
          `EXPLAIN QUERY PLAN SELECT COUNT(*) AS n, MIN(dispatch_ts) AS oldest FROM tasks
           WHERE status = 'dispatched' AND dispatch_ts IS NOT NULL AND dispatch_ts < ?`,
          Date.now(),
        )
        .toArray() as Record<string, SqlStorageValue>[];
      const detail = plan.map((row) => String(row.detail)).join(" | ");
      expect(detail).toContain("tasks_status_dispatch");
    });
  });
});

// #575 (инвентаризация всех SQL в harness.ts на предмет полного скана):
// #recentTasks (GET /api/tasks) сортирует ORDER BY created_ts DESC LIMIT без
// WHERE — без индекса, ведущего created_ts, план обязан SCAN TABLE + сортировку
// в памяти на ВСЮ историю tasks. Докажи мутацией: убери строку
// `CREATE INDEX tasks_by_created` из SCHEMA — тест покраснеет (план перейдёт
// на SCAN TABLE tasks с USE TEMP B-TREE FOR ORDER BY).
describe("список задач использует индекс tasks(created_ts DESC) (#575)", () => {
  it("EXPLAIN QUERY PLAN ссылается на tasks_by_created", async () => {
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (_instance, state) => {
      const plan = state.storage.sql
        .exec(
          "EXPLAIN QUERY PLAN SELECT id, created_ts, dispatch_ts, latency_ms, status FROM tasks ORDER BY created_ts DESC LIMIT ?",
          LIMITS.tasksListMax,
        )
        .toArray() as Record<string, SqlStorageValue>[];
      const detail = plan.map((row) => String(row.detail)).join(" | ");
      expect(detail).toContain("tasks_by_created");
    });
  });
});

describe("кэш счётчиков задач по статусу (#320)", () => {
  // Регрессия на возврат полного GROUP BY в горячий путь: если инвалидация
  // кэша при записи в tasks пропадёт, счётчики застынут на значении первого
  // вызова #status() и перестанут отражать реальные переходы — тест это ловит
  // дельтой, а не абсолютным числом (в файле общее хранилище DO между тестами).
  it("queued/running/done меняются по фактическим переходам, не только по факту чтения", async () => {
    const created = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const taskId = created.task_id;

    const afterCreate = await getJson<{ tasks: Record<string, number> }>("/api/status");
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 1, kind: "job_start" }] });
    const afterStart = await getJson<{ tasks: Record<string, number> }>("/api/status");
    expect(afterStart.tasks.queued).toBe(afterCreate.tasks.queued - 1);
    expect(afterStart.tasks.running).toBe(afterCreate.tasks.running + 1);

    await postJson("/api/events", { task_id: taskId, events: [{ seq: 2, kind: "job_end", data: { result: "ok" } }] });
    const afterEnd = await getJson<{ tasks: Record<string, number> }>("/api/status");
    expect(afterEnd.tasks.running).toBe(afterStart.tasks.running - 1);
    expect(afterEnd.tasks.done).toBe(afterStart.tasks.done + 1);
  });

  it("ретеншн сбрасывает кэш счётчиков так же, как dispatch/job_end (находка ревью PR #329)", async () => {
    const created = await (await postJson("/api/tasks", {})).json<{ task_id: string }>();
    const taskId = created.task_id;
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    // Обычный путь до done (через #applySideEffects) — кэш честно инвалидируется
    // и пересчитывается, как в соседнем тесте выше. afterDone — реальное число.
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 1, kind: "job_start" }] });
    await postJson("/api/events", { task_id: taskId, events: [{ seq: 2, kind: "job_end", data: { result: "ok" } }] });
    const afterDone = await getJson<{ tasks: Record<string, number> }>("/api/status");

    // Состариваем ТОЛЬКО created_ts напрямую через SQL — статус уже done через
    // штатный путь выше, это не новый переход и инвалидации кэша не касается.
    const oldCreatedTs = Date.now() - (RETENTION.tasksMaxAgeMs + 60_000);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec("UPDATE tasks SET created_ts = ? WHERE id = ?", oldCreatedTs, taskId);
    });

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });

    const after = await getJson<{ tasks: Record<string, number> }>("/api/status");
    // Ретеншн физически снёс эту done-строку. Без сброса #taskCountsCache в
    // #pruneRetention кэш остался бы на значении afterDone (лишняя done-запись
    // на бейдже до следующего перехода) — ровно та ложь, что нашёл ревьюер.
    expect(after.tasks.done).toBe(afterDone.tasks.done - 1);
  });
});

// Тот же класс, что #320/#321 закрыли для tasks, теперь и для messages
// (#575): #status() раньше делал полный GROUP BY по ВСЕЙ таблице messages на
// каждый вызов — эта регрессия ловит именно возврат такого поведения, не
// просто «счётчик корректен один раз». Докажи мутацией: убери
// `this.#msgCountsCache = null;` внутри #finishMessage (src/harness.ts) —
// тест ниже покраснеет: #status() продолжит отдавать messages.failed из
// первого вызова, не заметив реальный переход processing → failed.
describe("кэш счётчиков сообщений по статусу (#575)", () => {
  it("watchdog processing→failed (капа попыток) отражается в /api/status.messages без задержки", async () => {
    const sourceMsgId = `msgcounts-failed-${Date.now()}`;
    const created = await (
      await postJson("/api/messages", { text: "будет искусственно зависшим", source_msg_id: sourceMsgId })
    ).json<{ message_id: number }>();

    // #status() здесь — не просто чтение: он прогревает #msgCountsCache
    // значением, где это сообщение ещё 'new' (то самое состояние, которое
    // без инвалидации застыло бы навсегда).
    const before = await getJson<{ messages: Record<string, number> }>("/api/status");

    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));
    // Раздельно от нормального пути (#processSingleMessage): напрямую в SQL
    // переводим сообщение в processing с исчерпанным капом попыток — ватчдог
    // (#reclaimStuckMessages) обязан честно завершить его как failed
    // (stuck_reclaimed), а не вернуть в new (по образцу теста watchdog'а
    // dispatched-задач выше в файле).
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE messages SET status = 'processing', attempts = ?, processing_ts = ? WHERE id = ?",
        LIMITS.messageMaxAttempts,
        Date.now() - LIMITS.messageStuckProcessingMs - 1,
        created.message_id,
      );
    });

    await runInDurableObject(stub, async (instance) => {
      await instance.alarm();
    });

    const after = await getJson<{ messages: Record<string, number> }>("/api/status");
    // #586, находка ревью: строгое равенство по общему количеству строк
    // хранилища нарушало собственное правило шапки файла («никогда по общему
    // количеству строк») — чужая `new`-строка, исчерпавшая кап попыток в том
    // же alarm-тике, законно добавила бы ещё один `failed` и уронила бы тест
    // редко, но по делу. `>=` сохраняет мутационную силу (без инвалидации
    // кэша прироста не будет вовсе — assertion эту красит), но не привязан к
    // общему счёту строк.
    expect(after.messages.failed).toBeGreaterThanOrEqual((before.messages.failed || 0) + 1);
    // И собственная строка — по id, не по счётчику: ватчдог обязан был
    // завершить именно её как failed (stuck_reclaimed).
    const own = await getJson<{ message: { status: string } }>(`/api/messages/${created.message_id}`);
    expect(own.message.status).toBe("failed");
  });
});

describe("inbox: сообщения владельца", () => {
  // Каждый тест — со своим sender_id: хранилище DO между тестами одного файла
  // не изолируется, утверждения фильтруются по своим строкам.
  let senderSeq = 0;
  const sender = () => `inbox-tester-${Date.now()}-${senderSeq++}`;

  it("ingest принимает плоскую форму и возвращает id", async () => {
    const s = sender();
    const res = await postJson("/api/messages/ingest", {
      source: "telegram",
      source_msg_id: `ingest-${s}`,
      chat_id: "chat-456",
      sender_id: s,
      sender_name: "Owner",
      text: "Привет, это тестовое сообщение",
    });
    expect(res.status).toBe(201);
    const body = await res.json<{ message_id: number; status: string }>();
    expect(body.message_id).toBeGreaterThan(0);
    expect(body.status).toBe("accepted");
  });

  it("ingest понимает настоящую Telegram-форму update: числа приводятся к строкам (гвардия идемпотентности)", async () => {
    // Прод-форма: Telegram шлёт update_id/message_id/from.id/chat.id ЧИСЛАМИ.
    // Без нормализации реальный апдейт падает мимо UNIQUE(source, source_msg_id)
    // и каждый ретрай создавал бы новую строку.
    const s = sender();
    const update = {
      update_id: 918273645,
      message: {
        message_id: 42,
        from: { id: 777000, is_bot: false, first_name: "Владелец", username: s },
        chat: { id: -1001234567890, title: "dev", type: "supergroup" },
        date: 1756400000,
        text: "/task Проверь инбокс владельца",
      },
    };
    const first = await postJson("/api/messages/ingest", update);
    expect(first.status).toBe(201);
    const firstBody = await first.json<{ message_id: number }>();

    // Ретрай Telegram несёт тот же update_id — обязан вернуться в ту же строку.
    const retry = await postJson("/api/messages/ingest", update);
    expect(retry.status).toBe(200);
    const retryBody = await retry.json<{ message_id: number; status: string }>();
    expect(retryBody.message_id).toBe(firstBody.message_id);
    expect(retryBody.status).toBe("exists");

    const got = await getJson<{ message: { source: string; source_msg_id: string; chat_id: string; sender_id: string; sender_name: string; text: string } }>(
      `/api/messages/${firstBody.message_id}`,
    );
    expect(got.message.source).toBe("telegram");
    expect(got.message.source_msg_id).toBe("918273645");
    expect(got.message.chat_id).toBe("-1001234567890");
    expect(got.message.sender_id).toBe("777000");
    expect(got.message.sender_name).toBe(s);
    expect(got.message.text).toBe("/task Проверь инбокс владельца");
  });

  it("ingest без идентификатора — громкий 400: идемпотентность невозможна", async () => {
    const res = await postJson("/api/messages/ingest", { text: "без id" });
    expect(res.status).toBe(400);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("need_source_msg_id");
  });

  it("ingest отклоняет пустой text", async () => {
    const res = await postJson("/api/messages/ingest", {
      source: "api",
      source_msg_id: `empty-${Date.now()}`,
      text: "",
    });
    expect(res.status).toBe(400);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("need_text");
  });

  it("ingest отклоняет слишком длинное сообщение", async () => {
    const res = await postJson("/api/messages/ingest", {
      source: "api",
      source_msg_id: `long-${Date.now()}`,
      text: "а".repeat(20000),
    });
    expect(res.status).toBe(413);
    const body = await res.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("message_too_large");
  });

  it("GET /api/messages: обход пагинации без дублей и пропусков при равных ts (курсор согласован с DESC)", async () => {
    const s = sender();
    // Быстрая серия: ts в одну миллисекунду — гвардия тайбрейкера id DESC.
    for (let i = 0; i < 5; i++) {
      await postJson("/api/messages/ingest", {
        source: "api",
        source_msg_id: `walk-${s}-${i}`,
        sender_id: s,
        text: `Сообщение ${i}`,
      });
    }
    const seen: number[] = [];
    let after = 0;
    for (;;) {
      const page = await getJson<{ messages: { id: number; text: string }[]; has_more: boolean; next_after: number }>(
        `/api/messages?sender_id=${s}&limit=2&after=${after}`,
      );
      seen.push(...page.messages.map((m) => m.id));
      if (!page.has_more) break;
      after = page.next_after;
    }
    expect(seen).toHaveLength(5);
    expect(new Set(seen).size).toBe(5);
    // DESC по id: каждая следующая страница строго старше.
    for (let i = 1; i < seen.length; i++) expect(seen[i]).toBeLessThan(seen[i - 1]);
  });

  it("фильтр по status работает", async () => {
    const s = sender();
    await postJson("/api/messages/ingest", {
      source: "api",
      source_msg_id: `filter-new-${s}`,
      sender_id: s,
      text: "Новое сообщение",
    });
    const res = await getJson<{ messages: { status: string }[] }>(`/api/messages?status=new&sender_id=${s}`);
    expect(res.messages.length).toBeGreaterThan(0);
    for (const m of res.messages) {
      expect(m.status).toBe("new");
    }
  });

  it("POST /api/messages создаёт сообщение вручную; без id — громкий 400, повтор с id — exists (тот же класс, что ingest)", async () => {
    const res = await postJson("/api/messages", {
      source: "manual",
      source_msg_id: `manual-${Date.now()}`,
      text: "Ручное сообщение",
    });
    expect(res.status).toBe(201);
    const body = await res.json<{ message_id: number; status: string }>();
    expect(body.message_id).toBeGreaterThan(0);
    expect(body.status).toBe("created");

    // Молча сгенерированный одноразовый id превращает повтор админа во второй
    // issue — требуем id явно, как в ingest (п.31 спеки).
    const noId = await postJson("/api/messages", { text: "Без id — отказ" });
    expect(noId.status).toBe(400);
    const noIdBody = await noId.json<{ error: { code: string } }>();
    expect(noIdBody.error.code).toBe("need_source_msg_id");
  });

  it("разбор: директива без GH_DISPATCH_TOKEN повторяется (issue_retry), после капа попыток — честный failed", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `retry-${s}`,
        sender_id: s,
        text: "/task Сделай что-то важное",
      })
    ).json<{ message_id: number }>();

    // Попытки 1 и 2: токена нет — повторяемая ошибка, сообщение живёт в new.
    for (let attempt = 1; attempt <= 2; attempt++) {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ processed: number; results: { message_id: number; action: string; attempts?: number }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_retry");
      expect(mine?.attempts).toBe(attempt);
      const msg = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("new");
    }

    // Попытка 3 = кап: honest failed вместо вечного штурма.
    const res = await postJson("/api/messages/process", { limit: 100 });
    const body = await res.json<{ processed: number; results: { message_id: number; action: string; error?: string }[] }>();
    const mine = body.results.find((r) => r.message_id === created.message_id);
    expect(mine?.action).toBe("issue_failed");
    expect(mine?.error).toBe("dispatch_not_configured");
    const msg = await getJson<{ message: { status: string; kind: string; priority: number } }>(`/api/messages/${created.message_id}`);
    expect(msg.message.status).toBe("failed");
    expect(msg.message.kind).toBe("directive");
    expect(msg.message.priority).toBe(10);
  });

  it("retry_failed возвращает failed в new с обнулёнными попытками — газ после устранения причины", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `requeue-${s}`,
        sender_id: s,
        text: "/task Доведись до конца",
      })
    ).json<{ message_id: number }>();

    // Доводим до failed тремя прогонами.
    for (let i = 0; i < 3; i++) await postJson("/api/messages/process", { limit: 100 });
    let msg = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${created.message_id}`);
    expect(msg.message.status).toBe("failed");

    // Газ: failed → new, attempts=0; очередной прогон делает первую попытку и
    // (токена по-прежнему нет) возвращает в new, а не в failed.
    await postJson("/api/messages/process", { limit: 100, retry_failed: true });
    msg = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${created.message_id}`);
    expect(msg.message.status).toBe("new");
    expect(msg.message.attempts).toBe(1);
  });

  it("разбор: директива с GH_DISPATCH_TOKEN только дожидается 204 (issue_dispatched) — done наступает лишь после confirm job'а (issue-created), 204 сам по себе не доказательство", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `issue-${s}`,
        sender_id: s,
        text: "/task Заведи задачу из инбокса",
      })
    ).json<{ message_id: number }>();

    // Токен и fetch — только внутри теста: env-биндинги общие, восстанавливаем.
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (isGitHubRepositoryDispatchCall(input)) {
        calls.push({ url, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_dispatched");

      // Улика: repository_dispatch ушёл под GH_DISPATCH_TOKEN с нашим event_type
      // и claimed_ts — issue создаёт job (inbox-issue.yml), не сам DO.
      // Хранилище общее — в очереди есть директивы прошлых тестов, берём свою.
      const mineCall = calls.find(
        (c) => (c.body.client_payload as Record<string, unknown> | undefined)?.message_id === created.message_id,
      );
      expect(mineCall).toBeDefined();
      expect(mineCall!.url).toBe("https://api.github.com/repos/mytab0r/edge-harness/dispatches");
      expect(mineCall!.body.event_type).toBe("inbox-issue");
      const payload = mineCall!.body.client_payload as { title: string; body: string; claimed_ts: number };
      expect(payload.title).toContain("Заведи задачу из инбокса");
      expect(typeof payload.claimed_ts).toBe("number");

      // 204 — только приём: сообщение остаётся processing, НЕ done.
      const pending = await getJson<{ message: { status: string } }>(`/api/messages/${created.message_id}`);
      expect(pending.message.status).toBe("processing");

      // Job подтверждает созданную issue тем же каналом, что heartbeat (HANDS_TOKEN),
      // эхом возвращая claimed_ts из dispatch'а.
      const confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id,
        claimed_ts: payload.claimed_ts,
        issue_number: 4242,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4242",
      });
      expect(confirm.status).toBe(200);
      const confirmBody = await confirm.json<{ accepted: boolean; action: string }>();
      expect(confirmBody.accepted).toBe(true);
      expect(confirmBody.action).toBe("issue_created");

      const msg = await getJson<{ message: { status: string; result: string } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("done");
      expect(JSON.parse(msg.message.result)).toEqual({
        issue_number: 4242,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4242",
        secrets_redacted: false,
      });
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("подтверждение job'а с устаревшим claimed_ts (ватчдог уже увёл сообщение дальше) — accepted:false, чужой результат не перезаписывается (CAS)", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `cas-${s}`,
        sender_id: s,
        text: "/task Проверка CAS запоздавшего подтверждения",
      })
    ).json<{ message_id: number }>();

    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    let staleClaimedTs = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        const payload = (JSON.parse(String(init?.body)) as { client_payload: { claimed_ts: number } }).client_payload;
        staleClaimedTs = payload.claimed_ts;
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await postJson("/api/messages/process", { limit: 100 });
      expect(staleClaimedTs).toBeGreaterThan(0);

      // Ватчдог увёл сообщение дальше: processing_ts сменился на «новый» —
      // имитирует reclaim → повторный dispatch с НОВЫМ моментом захвата.
      const id = env.HARNESS.idFromName("owner");
      const stub = env.HARNESS.get(id);
      await runInDurableObject(stub, async (_instance, state) => {
        state.storage.sql.exec(
          "UPDATE messages SET processing_ts = ? WHERE id = ?",
          staleClaimedTs + 1, created.message_id,
        );
      });

      // Запоздавшее подтверждение первой (уже неактуальной) проходки.
      const confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id,
        claimed_ts: staleClaimedTs,
        issue_number: 5001,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/5001",
      });
      const confirmBody = await confirm.json<{ accepted: boolean }>();
      expect(confirmBody.accepted).toBe(false);

      // Результат не задвоился: чужая (новая) проходка ничего не потеряла.
      const msg = await getJson<{ message: { status: string; result: string | null } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("processing");
      expect(msg.message.result).toBeNull();
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("подтверждение без claimed_ts (тело без поля, null, 0, отрицательное) — 400 need_claimed_ts, а не молчаливое «accepted: false» устаревшей проходки (fail loud, спека п. 34)", async () => {
    for (const claimed_ts of [undefined, null, 0, -5]) {
      const confirm = await postJson("/api/messages/issue-created", {
        message_id: 1,
        claimed_ts,
        issue_number: 4242,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4242",
      });
      expect(confirm.status).toBe(400);
      expect((await confirm.json<{ error: { code: string } }>()).error.code).toBe("need_claimed_ts");
    }
  });

  it("job сообщает явный error через issue-created — тот же кап попыток, что у ошибки dispatch'а, не бесконечный штурм", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `job-error-${s}`,
        sender_id: s,
        text: "/task Job сам сообщит об отказе",
      })
    ).json<{ message_id: number }>();

    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    let claimedTs = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        claimedTs = (JSON.parse(String(init?.body)) as { client_payload: { claimed_ts: number } }).client_payload.claimed_ts;
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      // Попытка 1: dispatch принят, job сам сообщает об отказе — повторяемо.
      await postJson("/api/messages/process", { limit: 100 });
      let confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id, claimed_ts: claimedTs, error: "gh issue create упал",
      });
      let confirmBody = await confirm.json<{ accepted: boolean; action: string }>();
      expect(confirmBody.accepted).toBe(true);
      expect(confirmBody.action).toBe("issue_retry");
      let msg = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("new");
      expect(msg.message.attempts).toBe(1);

      // Попытка 2: тот же исход.
      await postJson("/api/messages/process", { limit: 100 });
      confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id, claimed_ts: claimedTs, error: "gh issue create упал",
      });
      expect((await confirm.json<{ action: string }>()).action).toBe("issue_retry");

      // Попытка 3 = кап: честный failed вместо вечного штурма.
      await postJson("/api/messages/process", { limit: 100 });
      confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id, claimed_ts: claimedTs, error: "gh issue create упал",
      });
      confirmBody = await confirm.json<{ accepted: boolean; action: string }>();
      expect(confirmBody.accepted).toBe(true);
      expect(confirmBody.action).toBe("issue_failed");
      msg = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("failed");
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("текст владельца с секретом уезжает в client_payload замаскированным (первый наружный путь фриформа, ДО передачи job'у)", async () => {
    const s = sender();
    // Граничный случай (ревью head 3706d87): секрет начинается близко к
    // порогу нарезки заголовка — усечение ДО маскирования оставляло сырой
    // хвост короче минимума паттерна.
    const fakeGhp = `ghp_${"a1".repeat(15)}`;
    const filler = "/task Секреты: " + "слово ".repeat(13); // ~80 символов до токена
    const boundary = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `secret-boundary-${s}`,
        sender_id: s,
        text: `${filler}${fakeGhp}`,
      })
    ).json<{ message_id: number }>();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `secret-${s}`,
        sender_id: s,
        text: "/task Проверь ключ sk-abcdefgh12345678 в конфиге",
      })
    ).json<{ message_id: number }>();

    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const calls: { body: { client_payload?: { message_id?: number; title?: string; body?: string; claimed_ts?: number } } }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        calls.push({ body: JSON.parse(String(init?.body)) });
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_dispatched");

      const mineCall = calls.find((c) => c.body.client_payload?.message_id === created.message_id);
      expect(mineCall).toBeDefined();
      const payload = mineCall!.body.client_payload!;
      // Ни заголовок, ни тело не содержат сырого ключа — ни в title, ни в body.
      expect(payload.title).not.toContain("sk-abcdefgh12345678");
      expect(payload.body).not.toContain("sk-abcdefgh12345678");
      expect(payload.body).toContain("sk-[REDACTED]");

      // Job подтверждает создание — secrets_redacted пересчитывается из уже
      // сохранённого текста сообщения (job его не видит и не решает: redact()
      // живёт только в DO).
      const confirm = await postJson("/api/messages/issue-created", {
        message_id: created.message_id,
        claimed_ts: payload.claimed_ts,
        issue_number: 4243,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4243",
      });
      expect((await confirm.json<{ accepted: boolean }>()).accepted).toBe(true);
      const msg = await getJson<{ message: { result: string } }>(`/api/messages/${created.message_id}`);
      expect(JSON.parse(msg.message.result).secrets_redacted).toBe(true);

      // Секрет на границе нарезки: никакого сырого фрагмента ghp_<символы>
      // в заголовке — маскирование случилось до усечения, до отправки job'у.
      const boundaryCall = calls.find((c) => c.body.client_payload?.message_id === boundary.message_id);
      expect(boundaryCall).toBeDefined();
      const boundaryPayload = boundaryCall!.body.client_payload!;
      expect(boundaryPayload.title).not.toContain(fakeGhp);
      expect(boundaryPayload.title).not.toMatch(/ghp_[A-Za-z0-9]{2,}/);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("в client_payload уходит усечённый заголовок (≤80 символов) — ниже 256-потолка issues API, с которым столкнётся job", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "t",
        source_msg_id: `long-title-${s}`,
        sender_id: s,
        text: `/task ${"очень длинная первая строка владельца ".repeat(8)}`,
      })
    ).json<{ message_id: number }>();

    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const titles: string[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        const payload = (JSON.parse(String(init?.body)) as { client_payload: { title?: string } }).client_payload;
        titles.push(String(payload.title));
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_dispatched");
      expect(titles).toHaveLength(1);
      // Мутация «в JSON уходит titleRedacted.text целиком» красит тест:
      // такой заголовок доехал бы до job'а и получил бы 422 от issues API
      // (256-потолок) — по сообщению, которое можно было разобрать.
      expect(titles[0].length).toBeLessThanOrEqual(80);
      expect(titles[0].endsWith("...")).toBe(true);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("разбор: 4xx от GitHub на dispatch — не штурмуем (сразу failed), 5xx — повторяем до капа", async () => {
    const s = sender();
    const make = async (id: string, text: string) =>
      (
        await postJson("/api/messages", {
          source: "test-process",
          source_msg_id: id,
          sender_id: s,
          text,
        })
      ).json<{ message_id: number }>();
    // Различаем ответы заглушки по тексту задачи — он уезжает в title.
    const forbidden = await make(`gh403-${s}`, "/task Сорок три навсегда");
    const serverError = await make(`gh500-${s}`, "/task Пятьсот время от времени");

    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        const bodyText = String(init?.body);
        if (bodyText.includes("Сорок три")) return new Response('{"message":"forbidden"}', { status: 403 });
        if (bodyText.includes("Пятьсот")) return new Response('{"message":"boom"}', { status: 500 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string; error?: string; attempts?: number }[] }>();
      const byId = new Map(body.results.map((r) => [r.message_id, r]));

      // 403 — детерминированный отказ (нет прав/кривая форма): failed сразу.
      const f = byId.get(forbidden.message_id);
      expect(f?.action).toBe("issue_failed");
      expect(f?.error).toContain("github_403");

      // 500 — временный: попытки 1 и 2 в new, третья (кап) — failed.
      expect(byId.get(serverError.message_id)?.action).toBe("issue_retry");
      await postJson("/api/messages/process", { limit: 100 });
      const res3 = await postJson("/api/messages/process", { limit: 100 });
      const body3 = await res3.json<{ results: { message_id: number; action: string; error?: string }[] }>();
      const se = body3.results.find((r) => r.message_id === serverError.message_id);
      expect(se?.action).toBe("issue_failed");
      expect(se?.error).toContain("github_500");
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("разбор: chat припаркован с пометкой, doc_edit получает issue-след (dispatch + confirm job'а), raw уходит в ignored на ручной триаж", async () => {
    const s = sender();
    const chat = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `kind-chat-${s}`, sender_id: s, text: "Как думаешь, что лучше?" })
    ).json<{ message_id: number }>();
    const doc = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `kind-doc-${s}`, sender_id: s, text: "Обнови docs/INDEX.md с новой инфой" })
    ).json<{ message_id: number }>();
    const raw = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `kind-raw-${s}`, sender_id: s, text: "просто заметка без действия" })
    ).json<{ message_id: number }>();

    // doc_edit идёт путём директивы: «у каждой директивы есть issue-след»
    // относится и к правкам доков — иначе весь класс императивов исчезает
    // из рабочих процессов (ревью head dfd167f). Заглушка как в директивных
    // тестах — только dispatch, issue создаёт job.
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const calls: { body: { client_payload?: { message_id?: number; title?: string; body?: string; claimed_ts?: number } } }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubRepositoryDispatchCall(input)) {
        calls.push({ body: JSON.parse(String(init?.body)) });
        return new Response(null, { status: 204 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ processed: number; results: { message_id: number; action: string }[] }>();
      const byId = new Map(body.results.map((r) => [r.message_id, r.action]));
      expect(byId.get(chat.message_id)).toBe("parked");
      expect(byId.get(doc.message_id)).toBe("issue_dispatched");
      expect(byId.get(raw.message_id)).toBe("ignored");

      // Улика dispatch'а правки доков: kind уехал в тело client_payload'а.
      const docCall = calls.find((c) => c.body.client_payload?.message_id === doc.message_id);
      expect(docCall).toBeDefined();
      const docPayload = docCall!.body.client_payload!;
      expect(docPayload.title).toContain("Обнови docs/INDEX.md");
      expect(docPayload.body).toContain("kind: doc_edit");

      // doc_edit остаётся processing до подтверждения — issue создаёт job.
      const docPending = await getJson<{ message: { status: string } }>(`/api/messages/${doc.message_id}`);
      expect(docPending.message.status).toBe("processing");
      const confirm = await postJson("/api/messages/issue-created", {
        message_id: doc.message_id,
        claimed_ts: docPayload.claimed_ts,
        issue_number: 4244,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4244",
      });
      expect((await confirm.json<{ accepted: boolean }>()).accepted).toBe(true);

      // raw больше не возвращается в new: очередь не забивается непроходящим сырьём.
      const rawMsg = await getJson<{ message: { status: string; kind: string; result: string } }>(`/api/messages/${raw.message_id}`);
      expect(rawMsg.message.status).toBe("ignored");
      expect(rawMsg.message.kind).toBe("raw");
      expect(JSON.parse(rawMsg.message.result).note).toBe("needs_manual_triage");

      const chatMsg = await getJson<{ message: { status: string; kind: string; priority: number; result: string } }>(`/api/messages/${chat.message_id}`);
      expect(chatMsg.message.status).toBe("done");
      expect(chatMsg.message.priority).toBe(1);
      expect(JSON.parse(chatMsg.message.result).note).toBe("classified_for_manual_review");

      const docMsg = await getJson<{ message: { kind: string; priority: number; status: string; result: string } }>(`/api/messages/${doc.message_id}`);
      expect(docMsg.message.kind).toBe("doc_edit");
      expect(docMsg.message.priority).toBe(5);
      expect(docMsg.message.status).toBe("done");
      expect(JSON.parse(docMsg.message.result).issue_number).toBe(4244);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("классификация: русские директивы без префикса — directive (гвардия не-ASCII границы слова, ревью ffd6bfe)", async () => {
    const s = sender();
    const make = async (id: string, text: string) =>
      (
        await postJson("/api/messages", {
          source: "test-classify",
          source_msg_id: id,
          sender_id: s,
          text,
        })
      ).json<{ message_id: number }>();
    // До фикса «\b после кириллицы не возникает» все эти тексты молча
    // уезжали в raw → ignored, минуя issue-след.
    const fix = await make(`ru-verb-${s}`, "исправь баг в инбоксе");
    const check = await make(`ru-verb2-${s}`, "проверь, почему пульс молчит");
    const prefixed = await make(`ru-prefix-${s}`, "/задача проверь инбокс владельца");

    await postJson("/api/messages/process", { limit: 200 });
    for (const [name, created] of [
      ["исправь", fix],
      ["проверь", check],
      ["/задача", prefixed],
    ] as const) {
      const msg = await getJson<{ message: { kind: string; priority: number; status: string } }>(
        `/api/messages/${created.message_id}`,
      );
      expect(msg.message.kind, name).toBe("directive");
      expect(msg.message.priority, name).toBe(10);
      // Токена нет — директива живёт в очереди с честным retry, не в ignored.
      expect(msg.message.status, name).toBe("new");
    }
  });

  it("группировка: серия из трёх сообщений одного отправителя в пределах окна — цепочка grouped_with (сценарий дельта-спеки)", async () => {
    const s = sender();
    const chat = `group-chat-${Date.now()}`;
    const make = async (i: number) =>
      (
        await postJson("/api/messages", {
          source: "telegram",
          source_msg_id: `group-${s}-${i}`,
          chat_id: chat,
          sender_id: s,
          text: `сообщение серии ${i}`,
        })
      ).json<{ message_id: number }>();
    const first = await make(1);
    const second = await make(2);
    const third = await make(3);

    await postJson("/api/messages/process", { limit: 200 });

    const got = await getJson<{ message: { grouped_with: number | null } }>(`/api/messages/${first.message_id}`);
    expect(got.message.grouped_with).toBeNull(); // начало серии
    const got2 = await getJson<{ message: { grouped_with: number | null } }>(`/api/messages/${second.message_id}`);
    expect(got2.message.grouped_with).toBe(first.message_id);
    const got3 = await getJson<{ message: { grouped_with: number | null } }>(`/api/messages/${third.message_id}`);
    expect(got3.message.grouped_with).toBe(second.message_id);
  });

  it("статус включает счётчики сообщений", async () => {
    const s = sender();
    const before = await getJson<{ messages: Record<string, number> }>("/api/status");
    await postJson("/api/messages/ingest", {
      source: "api",
      source_msg_id: `status-count-${s}`,
      sender_id: s,
      text: "Тест счетчика",
    });
    const after = await getJson<{ messages: Record<string, number> }>("/api/status");
    expect(after.messages.new).toBe((before.messages.new || 0) + 1);
  });

  it("process: битый JSON — честный 400 bad_json, пустое тело — валидный прогон (никаких 200 с побочным эффектом)", async () => {
    const bad = await WORKER.fetch("https://example.com/api/messages/process", {
      method: "POST",
      headers: { ...AUTH, "content-type": "application/json" },
      body: "{это не json",
    });
    expect(bad.status).toBe(400);
    const badBody = await bad.json<{ error: { code: string } }>();
    expect(badBody.error.code).toBe("bad_json");

    // Пустое тело — легально ТОЛЬКО у process (единственный маршрут с
    // опциональным телом): значения по умолчанию.
    const empty = await WORKER.fetch("https://example.com/api/messages/process", {
      method: "POST",
      headers: { ...AUTH },
    });
    expect(empty.status).toBe(200);
    const emptyBody = await empty.json<{ processed: number }>();
    expect(typeof emptyBody.processed).toBe("number");
  });

  it("пустой POST /api/tasks — громкий 400, а не молчаливая задача с dispatch (опциональное тело только у process, ревью head 7a21536)", async () => {
    const empty = await WORKER.fetch("https://example.com/api/tasks", {
      method: "POST",
      headers: { ...AUTH },
    });
    expect(empty.status).toBe(400);
    const body = await empty.json<{ error: { code: string } }>();
    expect(body.error.code).toBe("bad_json");

    // Мутация «вернуть if (!text.trim()) return {} в общий #parseJsonText»
    // красит тест: пустой POST уходит в #postTask с опциональными полями,
    // создаёт задачу и стреляет repository_dispatch — 200 с побочным эффектом.
  });

  it("ватчдог и водитель работают через публичный alarm(): зависший processing доводится, свежий new разбирается без ручного вызова", async () => {    const s = sender();
    // A — «изолят умер посреди внешнего вызова»: processing давний.
    const a = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `stuck-${s}`, sender_id: s, text: "завис в processing" })
    ).json<{ message_id: number }>();
    // B — просто новое: водитель обязан разобрать сам, без ручного POST.
    const b = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `fresh-${s}`, sender_id: s, text: "свежая заметка" })
    ).json<{ message_id: number }>();

    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE messages SET status = 'processing', attempts = 1, processing_ts = ? WHERE id = ?",
        Date.now() - LIMITS.messageStuckProcessingMs - 1, a.message_id,
      );
    });

    // Публичный alarm() БЕЗ GH_DISPATCH_TOKEN: разбор инбокса не зависит от
    // конфигурации dispatch (п.33 спеки) — ватчдог и водитель обязаны отработать
    // до раннего возврата по токену. Строгий стаб ловит любой неожидаемый
    // сетевой вызов: громко, а не в настоящий GitHub.
    vi.stubGlobal("fetch", (async (input: string | URL | Request) => {
      throw new Error(`неожиданный fetch в alarm-тесте: ${String(input)}`);
    }) as typeof fetch);
    let alarmRan = false;
    try {
      await runInDurableObject(stub, async (instance) => {
        await (instance as unknown as { alarm(): Promise<void> }).alarm();
        alarmRan = true;
      });
    } finally {
      vi.unstubAllGlobals();
    }
    expect(alarmRan).toBe(true);

    // A: ватчдог вернул в new, водитель довёл до терминала (raw → ignored).
    const gotA = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${a.message_id}`);
    expect(gotA.message.status).toBe("ignored");
    expect(gotA.message.attempts).toBe(2);
    // B: разобран тем же тиком.
    const gotB = await getJson<{ message: { status: string } }>(`/api/messages/${b.message_id}`);
    expect(gotB.message.status).toBe("ignored");
  });

  it("ватчдог уважает кап попыток: зависший processing с исчерпанным капом — честный failed stuck_reclaimed, а не вечный круг reclaim → claim (ревью head dfd167f)", async () => {
    const s = sender();
    const cap = LIMITS.messageMaxAttempts;
    const exhausted = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `stuck-cap-${s}`, sender_id: s, text: "завис навсегда" })
    ).json<{ message_id: number }>();
    const belowCap = await (
      await postJson("/api/messages", { source: "t", source_msg_id: `stuck-below-${s}`, sender_id: s, text: "завис однажды" })
    ).json<{ message_id: number }>();

    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE messages SET status = 'processing', attempts = ?, processing_ts = ? WHERE id = ?",
        cap, Date.now() - LIMITS.messageStuckProcessingMs - 1, exhausted.message_id,
      );
      state.storage.sql.exec(
        "UPDATE messages SET status = 'processing', attempts = ?, processing_ts = ? WHERE id = ?",
        cap - 1, Date.now() - LIMITS.messageStuckProcessingMs - 1, belowCap.message_id,
      );
    });

    vi.stubGlobal("fetch", (async (input: string | URL | Request) => {
      throw new Error(`неожиданный fetch в ватчдог-тесте: ${String(input)}`);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await (instance as unknown as { alarm(): Promise<void> }).alarm();
      });
    } finally {
      vi.unstubAllGlobals();
    }

    // Кап исчерпан: failed с именованной ошибкой — газ тот же (retry_failed),
    // бесконечный круг разорван. Мутация «возврат в new безусловно» красит тест.
    const gotEx = await getJson<{ message: { status: string; attempts: number; result: string } }>(`/api/messages/${exhausted.message_id}`);
    expect(gotEx.message.status).toBe("failed");
    expect(gotEx.message.attempts).toBe(cap);
    expect(JSON.parse(gotEx.message.result).error).toBe("stuck_reclaimed");
    // Ниже капа: ватчдог вернул в new, водитель того же тика довёл до терминала.
    const gotBelow = await getJson<{ message: { status: string; attempts: number } }>(`/api/messages/${belowCap.message_id}`);
    expect(gotBelow.message.status).toBe("ignored");
    expect(gotBelow.message.attempts).toBe(cap);
  });

  it("ручной POST идемпотентен: повтор с тем же source_msg_id отвечает существующей строкой (тот же класс, что ingest)", async () => {
    const dupKey = `dup-${Date.now()}`;
    const first = await postJson("/api/messages", { source: "manual", source_msg_id: dupKey, text: "первый" });
    expect(first.status).toBe(201);
    const dup = await postJson("/api/messages", { source: "manual", source_msg_id: dupKey, text: "второй" });
    expect(dup.status).toBe(200);
    const firstBody = await first.json<{ message_id: number }>();
    const dupBody = await dup.json<{ message_id: number; status: string }>();
    expect(dupBody.message_id).toBe(firstBody.message_id);
    expect(dupBody.status).toBe("exists");
  });
});

describe("Telegram: кнопки решения владельца (#254)", () => {
  let seq = 0;
  const updateId = () => 9_000_000_000 + Date.now() % 1_000_000 + seq++;

  // Прод-форма callback_query (Bot API): message несёт СВОЮ же исходную
  // клавиатуру обратно — метка нажатой кнопки читается из неё, второе
  // хранилище подписей не заводится.
  function callbackUpdate(opts: { data: string; messageText?: string; withKeyboard?: boolean; chatId?: number }) {
    return {
      update_id: updateId(),
      callback_query: {
        id: `cbq-${Date.now()}-${Math.random()}`,
        from: { id: 777000, is_bot: false, first_name: "Владелец", username: "owner" },
        message: {
          message_id: 555,
          chat: { id: opts.chatId ?? -1001234567890, type: "supergroup" },
          date: 1756400000,
          text: opts.messageText ?? "Нужно решение: вариант А или Б?",
          ...(opts.withKeyboard === false
            ? {}
            : {
                reply_markup: {
                  inline_keyboard: [
                    [
                      { text: "Вариант А", callback_data: "wo:471:1" },
                      { text: "Вариант Б", callback_data: "wo:471:2" },
                    ],
                  ],
                },
              }),
        },
        data: opts.data,
      },
    };
  }

  it("маршрут отклоняет вебхук без секретного заголовка и с чужим значением — тот же 401, что и у остального API", async () => {
    const noHeader = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1" }), null);
    expect(noHeader.status).toBe(401);
    expect((await noHeader.json<{ error: { code: string } }>()).error.code).toBe("unauthorized");

    const wrongSecret = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1" }), "wrong-secret-value");
    expect(wrongSecret.status).toBe(401);
  });

  it("обход секретом Telegram открывает ТОЛЬКО messagesIngest, не весь API (узость bypass'а)", async () => {
    const res = await WORKER.fetch("https://example.com/api/status", {
      headers: { "X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret" },
    });
    expect(res.status).toBe(401); // валидный секрет вебхука не открывает /api/status
  });

  // Находка ревью PR #486: секрет вебхука аутентифицирует БОТА, не отправителя —
  // после setWebhook морда получает апдейты из ЛЮБОГО чата, где боту написали.
  // Без сверки chat_id чужое сообщение легло бы в инбокс как «сообщение
  // владельца», а чужой callback увёл бы repository_dispatch в произвольную
  // задачу — ниже гвардируется оба пути.
  describe("привязка вебхука к владельцу по chat_id (находка ревью PR #486)", () => {
    it("callback_query из чужого чата — 401, dispatch и ответ Telegram не уходят", async () => {
      const realFetch = globalThis.fetch;
      env.GH_DISPATCH_TOKEN = "test-dispatch-token";
      env.TELEGRAM_BOT_TOKEN = "test-bot-token";
      vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
        if (isGitHubDispatchCall(input)) throw new Error("dispatch не должен звониться на чужой чат");
        if (telegramApiMethod(input)) throw new Error("Telegram API не должен звониться на чужой чат");
        return realFetch(input as RequestInfo, init);
      }) as typeof fetch);
      try {
        const res = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1", chatId: -999 }));
        expect(res.status).toBe(401);
        expect((await res.json<{ error: { code: string } }>()).error.code).toBe("unauthorized");
      } finally {
        vi.unstubAllGlobals();
        env.GH_DISPATCH_TOKEN = "";
        env.TELEGRAM_BOT_TOKEN = "";
      }
    });

    it("сообщение (не callback) через вебхук из чужого чата — 401, в инбокс не попадает", async () => {
      const res = await postTelegramWebhook({
        update_id: updateId(),
        message: {
          message_id: 1,
          from: { id: 1, is_bot: false, first_name: "Чужой" },
          chat: { id: -999, type: "private" },
          date: 1756400000,
          text: "я не владелец",
        },
      });
      expect(res.status).toBe(401);
      expect((await res.json<{ error: { code: string } }>()).error.code).toBe("unauthorized");
    });

    it("сообщение через вебхук из чата владельца — принято (положительная проверка того же пути)", async () => {
      const res = await postTelegramWebhook({
        update_id: updateId(),
        message: {
          message_id: 2,
          from: { id: 777000, is_bot: false, first_name: "Владелец" },
          chat: { id: -1001234567890, type: "supergroup" },
          date: 1756400000,
          text: "Привет от владельца через вебхук",
        },
      });
      expect(res.status).toBe(201);
      expect((await res.json<{ status: string }>()).status).toBe("accepted");
    });

    it("TELEGRAM_CHAT_ID не задан — вебхук-путь закрыт даже с верным секретом и чатом владельца", async () => {
      const saved = env.TELEGRAM_CHAT_ID;
      env.TELEGRAM_CHAT_ID = "";
      try {
        const res = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1" }));
        expect(res.status).toBe(401);
      } finally {
        env.TELEGRAM_CHAT_ID = saved;
      }
    });
  });

  it("callback_query с правильным секретом принят, отвечает Telegram'у и уходит repository_dispatch с event_type owner-decision", async () => {
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const dispatchCalls: Record<string, unknown>[] = [];
    const telegramCalls: { method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubDispatchCall(input)) {
        dispatchCalls.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return new Response(null, { status: 204 });
      }
      const method = telegramApiMethod(input);
      if (method) {
        telegramCalls.push({ method, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postTelegramWebhook(callbackUpdate({ data: "wo:471:2" }));
      expect(res.status).toBe(200);
      const body = await res.json<{ status: string }>();
      expect(body.status).toBe("callback_processed");

      expect(dispatchCalls).toHaveLength(1);
      expect(dispatchCalls[0].event_type).toBe("owner-decision");
      expect(dispatchCalls[0].client_payload).toEqual({ issue_number: 471, option: 2 });

      const answer = telegramCalls.find((c) => c.method === "answerCallbackQuery");
      expect(answer).toBeDefined();
      expect(answer!.body.text).toContain("Вариант Б"); // подпись именно нажатой кнопки

      const edit = telegramCalls.find((c) => c.method === "editMessageText");
      expect(edit).toBeDefined();
      expect(edit!.body.chat_id).toBe(-1001234567890);
      expect(edit!.body.message_id).toBe(555);
      expect(String(edit!.body.text)).toContain("Вариант Б");
      expect(edit!.body.reply_markup).toEqual({ inline_keyboard: [] }); // кнопки сняты — повторное нажатие невозможно
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("повторная доставка того же update_id (ретрай Telegram) не дублирует dispatch, но снова отвечает владельцу", async () => {
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    let dispatchCount = 0;
    let answerCount = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (isGitHubDispatchCall(input)) {
        dispatchCount++;
        return new Response(null, { status: 204 });
      }
      if (telegramApiMethod(input) === "answerCallbackQuery") answerCount++;
      if (telegramApiMethod(input)) return new Response(JSON.stringify({ ok: true }), { status: 200 });
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const update = callbackUpdate({ data: "wo:471:1" });
      const first = await postTelegramWebhook(update);
      expect((await first.json<{ status: string }>()).status).toBe("callback_processed");
      const retry = await postTelegramWebhook(update); // тот же update_id — ретрай
      expect((await retry.json<{ status: string }>()).status).toBe("callback_duplicate");

      expect(dispatchCount).toBe(1); // побочный эффект в GitHub — ровно один раз
      expect(answerCount).toBe(2); // владелец получает ответ на КАЖДУЮ доставку, кнопка не «висит»
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("кривой callback_data — честный callback_ignored, Telegram получает 200 (не 4xx, чтобы не ретраить бессмысленно)", async () => {
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const realFetch = globalThis.fetch;
    const telegramCalls: { method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const method = telegramApiMethod(input);
      if (method) {
        telegramCalls.push({ method, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) throw new Error("dispatch не должен звониться на кривой callback_data");
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postTelegramWebhook(callbackUpdate({ data: "чужой-формат" }));
      expect(res.status).toBe(200);
      expect((await res.json<{ status: string }>()).status).toBe("callback_ignored");
      const answer = telegramCalls.find((c) => c.method === "answerCallbackQuery");
      expect(answer?.body.text).toContain("Не понял формат");
    } finally {
      vi.unstubAllGlobals();
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("без GH_DISPATCH_TOKEN — честный текст «запись не ушла», кнопка всё равно отвечает и снимается (не тихая дыра)", async () => {
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const realFetch = globalThis.fetch;
    const telegramCalls: { method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const method = telegramApiMethod(input);
      if (method) {
        telegramCalls.push({ method, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1" }));
      expect(res.status).toBe(200);
      const answer = telegramCalls.find((c) => c.method === "answerCallbackQuery");
      expect(answer?.body.text).toContain("запись в задачу не ушла");
      const edit = telegramCalls.find((c) => c.method === "editMessageText");
      expect(edit?.body.reply_markup).toEqual({ inline_keyboard: [] }); // кнопки сняты и без успешного dispatch
    } finally {
      vi.unstubAllGlobals();
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("без TELEGRAM_BOT_TOKEN — ни один вызов Telegram не улетает, но dispatch и ответ вебхуку не падают", async () => {
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const realFetch = globalThis.fetch;
    let telegramCallsSeen = 0;
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (telegramApiMethod(input)) {
        telegramCallsSeen++;
        return new Response("не должен вызываться", { status: 500 });
      }
      if (isGitHubDispatchCall(input)) return new Response(null, { status: 204 });
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postTelegramWebhook(callbackUpdate({ data: "wo:471:1" }));
      expect(res.status).toBe(200);
      expect((await res.json<{ status: string }>()).status).toBe("callback_processed");
      expect(telegramCallsSeen).toBe(0);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
    }
  });

  it("без reply_markup в исходном сообщении подпись падает на честный «вариант N», не на исключение", async () => {
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    const realFetch = globalThis.fetch;
    const telegramCalls: { method: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const method = telegramApiMethod(input);
      if (method) {
        telegramCalls.push({ method, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) return new Response(null, { status: 204 });
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await postTelegramWebhook(callbackUpdate({ data: "wo:471:3", withKeyboard: false }));
      const answer = telegramCalls.find((c) => c.method === "answerCallbackQuery");
      expect(answer?.body.text).toContain("вариант 3");
    } finally {
      vi.unstubAllGlobals();
      env.TELEGRAM_BOT_TOKEN = "";
      env.GH_DISPATCH_TOKEN = "";
    }
  });
});

describe("чистые функции инбокса", () => {
  const NOW = 1_800_000_000_000;

  it("messageStuck: порог объявлен одной константой и работает на границе", () => {
    expect(messageStuck(null, NOW)).toBe(false);
    expect(messageStuck(NOW - LIMITS.messageStuckProcessingMs + 1, NOW)).toBe(false);
    expect(messageStuck(NOW - LIMITS.messageStuckProcessingMs, NOW)).toBe(true);
    expect(messageStuck(NOW - LIMITS.messageStuckProcessingMs - 1, NOW)).toBe(true);
  });

  it("asString: числа Telegram приводятся к строке, мусор — в null", () => {
    expect(asString(918273645)).toBe("918273645");
    expect(asString("918273645")).toBe("918273645");
    expect(asString(-1001234567890)).toBe("-1001234567890");
    expect(asString(undefined)).toBeNull();
    expect(asString(NaN)).toBeNull();
    expect(asString({ id: 1 })).toBeNull();
  });

  it("таймаут dispatch'а заведомо меньше ватчдога — иначе висящий fetch доживёт до ретрая другой проходки (двойной dispatch)", () => {
    expect(LIMITS.messageIssueDispatchTimeoutMs).toBeLessThan(LIMITS.messageStuckProcessingMs);
  });

  // ── callback_data инлайн-кнопки решения владельца (#254) ──────────────────────────
  it("parseOwnerDecisionCallback: разбирает валидный формат wo:<issue>:<option>", () => {
    expect(parseOwnerDecisionCallback("wo:471:2")).toEqual({ issue: 471, option: 2 });
    // Номер issue в этом репозитории уже трёхзначный — граница на будущее:
    // 6-значный issue + однозначный вариант всё равно укладывается в 64 байта.
    expect(parseOwnerDecisionCallback("wo:999999:9")).toEqual({ issue: 999999, option: 9 });
  });

  it("parseOwnerDecisionCallback: чужой префикс, дробные/отрицательные/нечисловые части, пусто — null, а не угаданное значение", () => {
    expect(parseOwnerDecisionCallback(null)).toBeNull();
    expect(parseOwnerDecisionCallback(undefined)).toBeNull();
    expect(parseOwnerDecisionCallback("")).toBeNull();
    expect(parseOwnerDecisionCallback("other:471:2")).toBeNull(); // чужой bot/старый формат
    expect(parseOwnerDecisionCallback("wo:471")).toBeNull(); // не хватает поля
    expect(parseOwnerDecisionCallback("wo:471:2:extra")).toBeNull();
    expect(parseOwnerDecisionCallback("wo:0:2")).toBeNull(); // issue #0 не существует
    expect(parseOwnerDecisionCallback("wo:471:0")).toBeNull(); // варианты нумеруются с 1
    expect(parseOwnerDecisionCallback("wo:-471:2")).toBeNull();
    expect(parseOwnerDecisionCallback("wo:471.5:2")).toBeNull();
    expect(parseOwnerDecisionCallback("wo:abc:2")).toBeNull();
  });

  it("callback_data формата wo:<issue>:<option> укладывается в лимит Bot API 64 байта даже на щедрой границе", () => {
    // Щедрая граница: issue до 10 цифр (текущий номер трёхзначный, запас на годы
    // вперёд), вариант до 2 цифр (UI не предполагает больше десятка кнопок).
    const generous = `wo:${"9".repeat(10)}:${"9".repeat(2)}`;
    expect(new TextEncoder().encode(generous).length).toBeLessThanOrEqual(64);
  });
});

describe("маскирование наружных текстов инбокса (тот же класс паттернов, что dsh-ci.sh::redact)", () => {
  // Фикстуры здесь — пересказ; ФОРМАЛЬНАЯ гвардия паритета —
  // scripts/lib/test_redact_parity.py (repo-ci): извлекает sed-подстановки
  // из dsh-ci.sh и сверяет с REDACT_PATTERNS один к одному.
  // Длинные формы собираются в рантайме: литерал из 20+ символов после
  // github_pat_/ghp_ — находка детерминированного ревью (check_pr), даже если
  // это фейковая фикстура теста.
  const fakeGhp = `ghp_${"a1".repeat(15)}`;
  const fakePat = `github_pat_${"b2".repeat(15)}`;
  it("маскирует формы секретов в середине текста и у краёв", () => {
    const text = `вот ключ sk-abcdefgh12345678 и nvapi-abcdefgh12, токен ${fakeGhp} и ${fakePat}`;
    const out = redact(text).text;
    expect(out).not.toContain("sk-abcdefgh12345678");
    expect(out).toContain("sk-[REDACTED]");
    expect(out).not.toContain("nvapi-abcdefgh12");
    expect(out).toContain("nvapi-[REDACTED]");
    expect(out).not.toContain(fakeGhp);
    expect(out).toContain("ghp_[REDACTED]");
    expect(out).not.toContain(fakePat);
    expect(out).toContain("github_pat_[REDACTED]");
  });

  it("секрет в начале строки тоже маскируется (прецедент начала текста)", () => {
    const out = redact("sk-abcdefgh12345678 в начале").text;
    expect(out).not.toContain("sk-abcdefgh12345678");
    expect(out.startsWith("sk-[REDACTED]")).toBe(true);
  });

  it("текст без секретов не трогается вовсе (факт замены = false)", () => {
    const source = "обычный текст владельца без секретов";
    expect(redact(source)).toEqual({ text: source, redacted: false });
  });
});
