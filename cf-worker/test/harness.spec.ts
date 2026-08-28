import { exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import { handsAreAlive } from "../src/harness";

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
