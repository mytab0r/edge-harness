import { runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it } from "vitest";
import { AUTOMATIONS, OWNER_OBJECT_NAME } from "../src/config";
import {
  AUTOMATION_ID_PATTERN,
  digestPeriod,
  journalTriggerDue,
  parseAutomationConfig,
  runTaskId,
  scheduleDue,
} from "../src/automations";

// ВАЖНО: vitest-плагин Cloudflare НЕ изолирует хранилище DO между тестами одного
// файла (проверено пробами, см. harness.spec.ts). Каждый тест работает только со
// своими id (уникальный суффикс) и делает утверждения, отфильтрованные по ним.
//
// GH_DISPATCH_TOKEN в тестовой среде сознательно НЕ задан (vitest.config.ts),
// поэтому «успешный» прогон автоматизации здесь заканчивается честным
// dispatched:false / not_configured: путь до GitHub — тонкая проводка того же
// #dispatchToGitHub, что у POST /api/tasks, живой 204 доказывается первым же
// прогоном раннера (см. pulse.spec.ts — тот же приём для пульса).
const AUTH = { Authorization: "Bearer test-token" };
const WEBHOOK_SECRET = "test-webhook-secret";

// Loopback к дефолтному экспорту воркера.
const WORKER = { fetch: (input: string, init?: RequestInit) => exports.default.fetch(input, init) };

let counter = 0;
function uniqueId(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${counter++}`;
}

async function putAutomation(id: string, config: unknown): Promise<Response> {
  return WORKER.fetch(`https://example.com/api/automations/${id}`, {
    method: "PUT",
    headers: { ...AUTH, "content-type": "application/json" },
    body: JSON.stringify({ config }),
  });
}

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  return WORKER.fetch(`https://example.com${path}`, { headers: AUTH, ...init }).then((res) => res.json<T>());
}

async function allEventsFor(taskId: string): Promise<{ kind: string; task_id: string; data: unknown }[]> {
  const events: { kind: string; task_id: string; data: unknown }[] = [];
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

function digestConfig(enabled = true): Record<string, unknown> {
  return {
    enabled,
    trigger: { type: "schedule", intervalHours: 168 },
    task: { kind: "digest" },
    report: { channels: [{ type: "slack", target: "#harness" }, { type: "telegram" }] },
  };
}

async function hmacHex(secret: string, payload: string): Promise<string> {
  const mac = await crypto.subtle.sign(
    "HMAC",
    await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]),
    new TextEncoder().encode(payload),
  );
  return [...new Uint8Array(mac)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// ── Чистая модель: форма конфига ────────────────────────────────────────────────

describe("automations: валидация конфига", () => {
  it("полный конфиг проходит и нормализуется (pool.body дописывается пустым)", () => {
    // trigger=schedule, не journal: сочетание journal+pool отдельно отклоняется
    // ниже (находка AI-ревью PR #241, третий раунд) — эта проверка про
    // нормализацию task.kind=pool как таковую, не про конкретный триггер.
    const parsed = parseAutomationConfig({
      enabled: false,
      trigger: { type: "schedule", intervalHours: 24 },
      task: { kind: "pool", title: "разобрать" },
      report: { channels: [] },
    });
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.config.task).toEqual({ kind: "pool", title: "разобрать", body: "" });
    }
  });

  it("чужой trigger.type, неизвестные поля и опечатки отклоняются (fail loud)", () => {
    expect(parseAutomationConfig({ ...digestConfig(), trigger: { type: "cron" } }).ok).toBe(false);
    expect(parseAutomationConfig({ ...digestConfig(), extra: 1 }).ok).toBe(false);
    expect(parseAutomationConfig({ ...digestConfig(), enabled: "yes" }).ok).toBe(false);
    expect(parseAutomationConfig({ ...digestConfig(), trigger: { type: "schedule", intervalHours: 0 } }).ok).toBe(false);
    expect(parseAutomationConfig({ ...digestConfig(), report: { channels: [] } }).ok).toBe(false); // digest без каналов
    expect(parseAutomationConfig({ ...digestConfig(), task: { kind: "hands", text: "" } }).ok).toBe(false);
    expect(parseAutomationConfig(null).ok).toBe(false);
  });

  it("journal-триггер со служебным kind самой автоматизации отклоняется (никогда не сработал бы, находка ревью PR #241)", () => {
    for (const kind of AUTOMATIONS.reservedJournalKinds) {
      const parsed = parseAutomationConfig({ ...digestConfig(), trigger: { type: "journal", kind } });
      expect(parsed.ok, `kind=${kind} должен быть отклонён`).toBe(false);
    }
    // Обычный kind события job'а — по-прежнему валиден.
    expect(parseAutomationConfig({ ...digestConfig(), trigger: { type: "journal", kind: "job_end" } }).ok).toBe(true);
  });

  it("сочетание trigger=journal + task=pool отклоняется — самоподдерживающаяся петля (находка ревью PR #241, третий раунд)", () => {
    const parsed = parseAutomationConfig({
      enabled: true,
      trigger: { type: "journal", kind: "job_end" },
      task: { kind: "pool", title: "разобрать" },
      report: { channels: [] },
    });
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) {
      expect(parsed.error).toMatch(/петл/);
    }
    // journal + digest/hands — по-прежнему валидны, петля возможна только с pool.
    expect(parseAutomationConfig({ ...digestConfig(), trigger: { type: "journal", kind: "job_end" } }).ok).toBe(true);
    expect(parseAutomationConfig({
      enabled: true,
      trigger: { type: "journal", kind: "job_end" },
      task: { kind: "hands", text: "разобрать" },
      report: { channels: [] },
    }).ok).toBe(true);
  });
});

const NOW = 1_800_000_000_000;
const WEEK = 168 * 3_600_000;

describe("automations: решение расписания и период", () => {
  it("первый запуск — пора всегда", () => {
    expect(scheduleDue(168, null, NOW)).toBe(true);
  });
  it("интервал не истёк — рано; ровно на границе — пора", () => {
    expect(scheduleDue(168, NOW - WEEK + 60_000, NOW)).toBe(false);
    expect(scheduleDue(168, NOW - WEEK, NOW)).toBe(true);
  });
  it("период дайджеста: от прошлого запуска, при первом — интервал назад", () => {
    expect(digestPeriod(NOW - WEEK, 168, NOW)).toEqual({ since_ts: NOW - WEEK, until_ts: NOW });
    expect(digestPeriod(null, 1, NOW)).toEqual({ since_ts: NOW - 3_600_000, until_ts: NOW });
  });
  it("task_id прогона живёт под префиксом-гвардией петли; nonce разводит одну миллисекунду", () => {
    expect(runTaskId("weekly-digest", 123, "abcd1234")).toMatch(/^automation:weekly-digest:[0-9a-z]+-abcd1234$/);
    expect(runTaskId("weekly-digest", 123, "abcd1234")).not.toBe(runTaskId("weekly-digest", 123, "deadbeef"));
    expect(() => runTaskId("weekly-digest", 123, "!!")).toThrow(/nonce/);
  });
  it("journal-триггер: кулдаун рвёт цикл через чужие события", () => {
    expect(journalTriggerDue(null, NOW, AUTOMATIONS.journalCooldownMs)).toBe(true);
    expect(journalTriggerDue(NOW - AUTOMATIONS.journalCooldownMs + 1, NOW, AUTOMATIONS.journalCooldownMs)).toBe(false);
    expect(journalTriggerDue(NOW - AUTOMATIONS.journalCooldownMs, NOW, AUTOMATIONS.journalCooldownMs)).toBe(true);
  });
  it("шаблон id: без «:», % и «_» — id едет в путях webhook'а и LIKE-паттернах", () => {
    expect("weekly-digest".match(new RegExp(AUTOMATION_ID_PATTERN))).not.toBeNull();
    expect("Bad_Id".match(new RegExp(AUTOMATION_ID_PATTERN))).toBeNull();
    expect("a:b".match(new RegExp(AUTOMATION_ID_PATTERN))).toBeNull();
  });
});

// ── DO: CRUD конфигураций ───────────────────────────────────────────────────────

describe("automations: CRUD", () => {
  it("PUT создаёт, GET отдаёт с конфигом и пустым last_run; повторный PUT обновляет", async () => {
    const id = uniqueId("crud");
    const created = await putAutomation(id, digestConfig());
    expect(created.status).toBe(201);
    const updated = await putAutomation(id, digestConfig(false));
    expect(updated.status).toBe(200);
    const list = await getJson<{ automations: { id: string; enabled: boolean; config: { enabled: boolean }; last_run: null; last_fired_ts: null }[] }>(
      "/api/automations",
    );
    const row = list.automations.find((a) => a.id === id);
    expect(row?.enabled).toBe(false);
    expect(row?.config.enabled).toBe(false);
    expect(row?.last_run).toBeNull();
    expect(row?.last_fired_ts).toBeNull();
    // Чистка: тест не должен оставлять конфиг следующим (лимит automationsMax общий).
    await WORKER.fetch(`https://example.com/api/automations/${id}`, { method: "DELETE", headers: AUTH });
  });

  it("кривой id/конфиг — 400 с кодом; чужой (не владелец) — 401; DELETE мимо — 404", async () => {
    const badId = await WORKER.fetch("https://example.com/api/automations/Bad_Id", {
      method: "PUT", headers: { ...AUTH, "content-type": "application/json" }, body: JSON.stringify({ config: digestConfig() }),
    });
    expect(badId.status).toBe(400);
    expect((await badId.json<{ error: { code: string } }>()).error.code).toBe("automation_id_invalid");

    const badConfig = await putAutomation(uniqueId("bad"), { enabled: true });
    expect(badConfig.status).toBe(400);
    expect((await badConfig.json<{ error: { code: string } }>()).error.code).toBe("automation_config_invalid");

    const anon = await WORKER.fetch(`https://example.com/api/automations/${uniqueId("anon")}`, {
      method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ config: digestConfig() }),
    });
    expect(anon.status).toBe(401);

    const missing = await WORKER.fetch(`https://example.com/api/automations/${uniqueId("ghost")}`, {
      method: "DELETE", headers: AUTH,
    });
    expect(missing.status).toBe(404);
    expect((await missing.json<{ error: { code: string } }>()).error.code).toBe("automation_not_found");
  });

  it("DELETE оставляет след automation_deleted в журнале — симметрично PUT (находка ревью PR #241)", async () => {
    const id = uniqueId("del");
    await putAutomation(id, digestConfig());
    const deleted = await WORKER.fetch(`https://example.com/api/automations/${id}`, { method: "DELETE", headers: AUTH });
    expect(deleted.status).toBe(200);
    const events = await allEventsFor(`automation:${id}`);
    expect(events.some((event) => event.kind === "automation_deleted")).toBe(true);
  });
});

// ── DO: webhook-триггер — подпись обязательна, отказ виден в журнале ────────────

describe("automations: webhook", () => {
  const BODY = JSON.stringify({ action: "ping" });

  async function signedWebhook(id: string, signature: string | null, body = BODY): Promise<Response> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (signature !== null) headers["X-Harness-Signature"] = signature;
    return WORKER.fetch(`https://example.com/api/webhooks/${id}`, { method: "POST", headers, body });
  }

  it("без подписи — 401 и событие automation_webhook_rejected в журнале (громко)", async () => {
    const id = uniqueId("nosig");
    const res = await signedWebhook(id, null);
    expect(res.status).toBe(401);
    expect((await res.json<{ error: { code: string } }>()).error.code).toBe("webhook_signature_invalid");
    const rejected = await allEventsFor("automation:webhook:rejected");
    const mine = rejected.filter((event) => (event.data as { automation?: string })?.automation === id);
    expect(mine.at(-1)?.kind).toBe("automation_webhook_rejected");
    expect((mine.at(-1)?.data as { reason?: string })?.reason).toBe("signature_missing");
  });

  it("неверная подпись — 401 c reason bad_signature", async () => {
    const id = uniqueId("badsig");
    const res = await signedWebhook(id, "sha256=" + "0".repeat(64));
    expect(res.status).toBe(401);
    const rejected = await allEventsFor("automation:webhook:rejected");
    expect((rejected.at(-1)?.data as { reason?: string })?.reason).toBe("bad_signature");
  });

  it("верная подпись + включённая автоматизация — 202, задача и события прогона в журнале, last_fired проставлен", async () => {
    const id = uniqueId("hooked");
    expect((
      await putAutomation(id, {
        enabled: true,
        trigger: { type: "webhook" },
        task: { kind: "digest" },
        report: { channels: [{ type: "slack", target: "#harness" }, { type: "telegram" }] },
      })
    ).status).toBe(201);
    const res = await signedWebhook(id, "sha256=" + (await hmacHex(WEBHOOK_SECRET, BODY)));
    expect(res.status).toBe(202);
    const answer = await res.json<{ ok: boolean; task_id: string; dispatched: boolean }>();
    expect(answer.dispatched).toBe(false); // dispatch-токен в тестовой среде не задан
    const taskId = answer.task_id;
    expect(taskId.startsWith(`automation:${id}:`)).toBe(true);

    const tasks = await getJson<{ tasks: { id: string; status: string }[] }>("/api/tasks");
    expect(tasks.tasks.find((task) => task.id === taskId)?.status).toBe("queued");

    const kinds = (await allEventsFor(taskId)).map((event) => event.kind);
    expect(kinds).toContain("automation_triggered");
    expect(kinds.filter((kind) => kind === "automation_triggered").length).toBe(1); // дублей нет

    const list = await getJson<{ automations: { id: string; last_fired_ts: number | null; last_run: { task_id: string } | null }[] }>(
      "/api/automations",
    );
    const row = list.automations.find((a) => a.id === id);
    expect(row?.last_fired_ts).not.toBeNull();
    expect(row?.last_run?.task_id).toBe(taskId);
  });

  it("прогон автоматизации инвалидирует кеш счётчиков задач (#320): новый queued виден в /api/status сразу (находка ревью PR #241, дельта)", async () => {
    const id = uniqueId("counts");
    expect((await putAutomation(id, {
      enabled: true,
      trigger: { type: "webhook" },
      task: { kind: "digest" },
      report: { channels: [{ type: "slack", target: "#harness" }, { type: "telegram" }] },
    })).status).toBe(201);

    const before = await getJson<{ tasks: Record<string, number> }>("/api/status");
    const res = await signedWebhook(id, "sha256=" + (await hmacHex(WEBHOOK_SECRET, BODY)));
    expect(res.status).toBe(202);
    // dispatch-токен в тестовой среде не задан: задача остаётся queued —
    // и она обязана быть видна счётчиками сразу, а не после чужой мутации,
    // иначе кеш (#320) молча врёт дашборду (#111).
    const after = await getJson<{ tasks: Record<string, number> }>("/api/status");
    expect(after.tasks.queued ?? 0).toBe((before.tasks.queued ?? 0) + 1);
  });

  it("не webhook-триггерная автоматизация — 409 automation_not_webhook: вход не сдвигает фазу расписания", async () => {
    const id = uniqueId("sched-hook");
    expect((await putAutomation(id, digestConfig())).status).toBe(201); // trigger: schedule
    const before = await getJson<{ automations: { id: string; last_fired_ts: number | null }[] }>("/api/automations");
    const res = await signedWebhook(id, "sha256=" + (await hmacHex(WEBHOOK_SECRET, BODY)));
    expect(res.status).toBe(409);
    expect((await res.json<{ error: { code: string } }>()).error.code).toBe("automation_not_webhook");
    const after = await getJson<{ automations: { id: string; last_fired_ts: number | null }[] }>("/api/automations");
    expect(after.automations.find((a) => a.id === id)?.last_fired_ts).toBe(
      before.automations.find((a) => a.id === id)?.last_fired_ts,
    );
  });

  it("выключенная автоматизация — 409 automation_disabled, прогона нет", async () => {
    const id = uniqueId("off");
    expect((await putAutomation(id, digestConfig(false))).status).toBe(201);
    const res = await signedWebhook(id, "sha256=" + (await hmacHex(WEBHOOK_SECRET, BODY)));
    expect(res.status).toBe(409);
    expect((await res.json<{ error: { code: string } }>()).error.code).toBe("automation_disabled");
    const rejected = await allEventsFor(`automation:${id}`);
    expect(rejected.at(-1)?.kind).toBe("automation_webhook_rejected");
  });

  it("неизвестная автоматизация при верной подписи — 404", async () => {
    const res = await signedWebhook(uniqueId("ghost-hook"), "sha256=" + (await hmacHex(WEBHOOK_SECRET, BODY)));
    expect(res.status).toBe(404);
    expect((await res.json<{ error: { code: string } }>()).error.code).toBe("automation_not_found");
  });
});

// ── DO: триггер «событие журнала» и гвардия петли ───────────────────────────────

describe("automations: journal-триггер", () => {
  it("событие нужного kind поднимает прогон; события самих автоматизаций — нет", async () => {
    const id = uniqueId("jrn");
    const kind = `gate_${counter}`;
    // task=hands, не pool: сочетание journal+pool отдельно отклоняется на PUT
    // (находка ревью PR #241, третий раунд) — этот тест проверяет срабатывание
    // триггера и гвардию петли, для чего вид задачи не важен.
    expect((
      await putAutomation(id, {
        enabled: true,
        trigger: { type: "journal", kind },
        task: { kind: "hands", text: "разобрать событие" },
        report: { channels: [] },
      })
    ).status).toBe(201);
    const post = (taskId: string) =>
      WORKER.fetch("https://example.com/api/events", {
        method: "POST",
        headers: { ...AUTH, "content-type": "application/json" },
        body: JSON.stringify({ task_id: taskId, events: [{ seq: 1, kind }] }),
      });
    const countRuns = async (): Promise<number> => {
      const tasks = await getJson<{ tasks: { id: string }[] }>("/api/tasks");
      return tasks.tasks.filter((task) => task.id.startsWith(`automation:${id}:`)).length;
    };

    // Чужое событие — триггер срабатывает: прогон в очереди есть (диспатч в
    // тестовой среде не настроен — это честный not_configured, не отсутствие прогона).
    await post(`issue-${Date.now()}`);
    expect(await countRuns()).toBe(1);

    // Событие самой автоматизации — гвардия петли: нового прогона нет.
    const tasks = await getJson<{ tasks: { id: string }[] }>("/api/tasks");
    const runId = tasks.tasks.find((task) => task.id.startsWith(`automation:${id}:`))!.id;
    await post(runId);
    expect(await countRuns()).toBe(1);
  });
});

// ── DO: триггер «расписание» реально поднимается будильником DO ────────────────
//
// Единственная из трёх веток триггеров, которую остальные тесты этого файла не
// поднимали через настоящий alarm(): webhook и journal гоняются сквозь HTTP,
// schedule был покрыт только чистой scheduleDue() — #fireDueSchedules() внутри
// alarm() ни разу не вызывался (находка AI-ревью PR #241, третий раунд).
describe("automations: schedule-триггер поднимается будильником DO", () => {
  it("due-расписание запускает прогон на alarm(); дистанция уже пройдена — прогона нет", async () => {
    const dueId = uniqueId("sched-alarm-due");
    const notDueId = uniqueId("sched-alarm-not-due");
    // intervalHours=1: обеим PUT сразу ставит last_fired_ts=null → scheduleDue
    // вернёт true при первом alarm(); notDueId получит last_fired_ts свежим
    // прогоном ниже, чтобы вторая проверка была честной «рано».
    expect((await putAutomation(dueId, {
      enabled: true,
      trigger: { type: "schedule", intervalHours: 1 },
      task: { kind: "hands", text: "дайджест" },
      report: { channels: [] },
    })).status).toBe(201);
    expect((await putAutomation(notDueId, {
      enabled: true,
      trigger: { type: "schedule", intervalHours: 1 },
      task: { kind: "hands", text: "дайджест" },
      report: { channels: [] },
    })).status).toBe(201);

    const countRuns = async (prefix: string): Promise<number> => {
      const tasks = await getJson<{ tasks: { id: string }[] }>("/api/tasks");
      return tasks.tasks.filter((task) => task.id.startsWith(`automation:${prefix}:`)).length;
    };

    const id = env.HARNESS.idFromName(OWNER_OBJECT_NAME);
    const stub = env.HARNESS.get(id);
    // notDueId уже "отстрелялся" только что — следующий пульс ему рано.
    await runInDurableObject(stub, async (_instance, state) => {
      state.storage.sql.exec(
        "UPDATE automations SET last_fired_ts = ? WHERE id = ?", Date.now(), notDueId,
      );
    });

    expect(await countRuns(dueId)).toBe(0);
    expect(await countRuns(notDueId)).toBe(0);

    await runInDurableObject(stub, async (instance) => {
      await (instance as unknown as { alarm(): Promise<void> }).alarm();
    });

    // due — прогон появился (диспатч в тестовой среде честно not_configured,
    // сама очередь — доказательство, что #fireDueSchedules сработал).
    expect(await countRuns(dueId)).toBe(1);
    // not_due — интервал не истёк, alarm() не должен был его тронуть.
    expect(await countRuns(notDueId)).toBe(0);
  });
});
