import { runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it, vi } from "vitest";
import { asString, classifyStorageError, handsAreAlive, messageStuck, storageErrorResponse } from "../src/harness";

import { asString, handsAreAlive, messageStuck } from "../src/harness";
import { redact } from "../src/redact";
import { LIMITS } from "../src/config";

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

  it("разбор: директива без GH_ISSUES_TOKEN повторяется (issue_retry), после капа попыток — честный failed", async () => {
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
    expect(mine?.error).toBe("issues_not_configured");
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

  it("разбор: директива с настроенным GH_ISSUES_TOKEN создаёт issue (fetch заглушен прод-формой ответа GitHub)", async () => {
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
    env.GH_ISSUES_TOKEN = "test-issue-token";
    // Заглушка кормится прод-формой: 201 + JSON issue (number, html_url).
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("api.github.com") && url.includes("/issues")) {
        calls.push({ url, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return new Response(JSON.stringify({ number: 4242, html_url: "https://github.com/mytab0r/edge-harness/issues/4242" }), { status: 201 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string; issue_number?: number }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_created");
      expect(mine?.issue_number).toBe(4242);

      // Улика: запрос ушёл в GitHub API под узким токеном и с метками пула.
      // Хранилище общее — в очереди есть директивы прошлых тестов, берём свою.
      const mineCall = calls.find((c) => String(c.body.title).includes("Заведи задачу из инбокса"));
      expect(mineCall).toBeDefined();
      expect(mineCall!.url).toContain("/repos/mytab0r/edge-harness/issues");
      expect(mineCall!.body.labels).toContain("task");

      const msg = await getJson<{ message: { status: string; result: string } }>(`/api/messages/${created.message_id}`);
      expect(msg.message.status).toBe("done");
      expect(JSON.parse(msg.message.result)).toEqual({
        issue_number: 4242,
        issue_url: "https://github.com/mytab0r/edge-harness/issues/4242",
        secrets_redacted: false,
      });
    } finally {
      vi.unstubAllGlobals();
      env.GH_ISSUES_TOKEN = "";
    }
  });

  it("текст владельца с секретом уезжает в публичный issue замаскированным (первый наружный путь фриформа)", async () => {
    const s = sender();
    const created = await (
      await postJson("/api/messages", {
        source: "test-process",
        source_msg_id: `secret-${s}`,
        sender_id: s,
        text: "/task Проверь ключ sk-abcdefgh12345678 в конфиге",
      })
    ).json<{ message_id: number }>();

    const realFetch = globalThis.fetch;
    env.GH_ISSUES_TOKEN = "test-issue-token";
    const calls: { body: { title?: string; body?: string } }[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("api.github.com") && url.includes("/issues")) {
        calls.push({ body: JSON.parse(String(init?.body)) as { title?: string; body?: string } });
        return new Response(JSON.stringify({ number: 4243, html_url: "https://github.com/mytab0r/edge-harness/issues/4243" }), { status: 201 });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      const res = await postJson("/api/messages/process", { limit: 100 });
      const body = await res.json<{ results: { message_id: number; action: string; secrets_redacted?: boolean }[] }>();
      const mine = body.results.find((r) => r.message_id === created.message_id);
      expect(mine?.action).toBe("issue_created");

      const mineCall = calls.find((c) => String(c.body.title).includes("Проверь ключ"));
      expect(mineCall).toBeDefined();
      // Ни заголовок, ни тело не содержат сырого ключа — ни в title, ни в body.
      expect(mineCall!.body.title).not.toContain("sk-abcdefgh12345678");
      expect(mineCall!.body.body).not.toContain("sk-abcdefgh12345678");
      expect(mineCall!.body.body).toContain("sk-[REDACTED]");
      expect(mine?.secrets_redacted).toBe(true);

      const msg = await getJson<{ message: { result: string } }>(`/api/messages/${created.message_id}`);
      expect(JSON.parse(msg.message.result).secrets_redacted).toBe(true);
    } finally {
      vi.unstubAllGlobals();
      env.GH_ISSUES_TOKEN = "";
    }
  });

  it("разбор: 4xx от GitHub — не штурмуем (сразу failed), 5xx — повторяем до капа", async () => {
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
    env.GH_ISSUES_TOKEN = "test-issue-token";
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("api.github.com") && url.includes("/issues")) {
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

      // 403 — детерминированный отказ (токен без права/кривая форма): failed сразу.
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
      env.GH_ISSUES_TOKEN = "";
    }
  });

  it("разбор: чат и правка доков припаркованы с пометкой, raw уходит в ignored на ручной триаж", async () => {
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

    const res = await postJson("/api/messages/process", { limit: 100 });
    const body = await res.json<{ processed: number; results: { message_id: number; action: string }[] }>();
    const byId = new Map(body.results.map((r) => [r.message_id, r.action]));
    expect(byId.get(chat.message_id)).toBe("parked");
    expect(byId.get(doc.message_id)).toBe("parked");
    expect(byId.get(raw.message_id)).toBe("ignored");

    // raw больше не возвращается в new: очередь не забивается непроходящим сырьём.
    const rawMsg = await getJson<{ message: { status: string; kind: string; result: string } }>(`/api/messages/${raw.message_id}`);
    expect(rawMsg.message.status).toBe("ignored");
    expect(rawMsg.message.kind).toBe("raw");
    expect(JSON.parse(rawMsg.message.result).note).toBe("needs_manual_triage");

    const chatMsg = await getJson<{ message: { status: string; kind: string; priority: number; result: string } }>(`/api/messages/${chat.message_id}`);
    expect(chatMsg.message.status).toBe("done");
    expect(chatMsg.message.priority).toBe(1);
    expect(JSON.parse(chatMsg.message.result).note).toBe("classified_for_manual_review");

    const docMsg = await getJson<{ message: { kind: string; priority: number; grouped_with: number | null } }>(`/api/messages/${doc.message_id}`);
    expect(docMsg.message.kind).toBe("doc_edit");
    expect(docMsg.message.priority).toBe(5);
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

  it("ватчдог и водитель работают через публичный alarm(): зависший processing доводится, свежий new разбирается без ручного вызова", async () => {
    const s = sender();
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

  it("таймаут вызова GitHub заведомо меньше ватчдога — иначе висящий fetch доживёт до ретрая другой проходки (двойной issue)", () => {
    expect(LIMITS.messageIssueFetchTimeoutMs).toBeLessThan(LIMITS.messageStuckProcessingMs);
  });
});

describe("маскирование наружных текстов инбокса (тот же класс паттернов, что dsh-ci.sh::redact)", () => {
  // Фикстуры — те же формы секретов, что гасит scripts/lib/dsh-ci.sh::redact:
  // nvapi-/sk-/ghp_/github_pat_ в середину текста и без следов. Новая форма
  // секрета добавляется в dsh-ci.sh и сюда одним классом правки.
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
