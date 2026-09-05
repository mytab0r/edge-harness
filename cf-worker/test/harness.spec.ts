import { runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import { classifyStorageError, handsAreAlive, storageErrorResponse } from "../src/harness";

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
  });
});
