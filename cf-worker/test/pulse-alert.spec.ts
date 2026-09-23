import { runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it, vi } from "vitest";
import { DSH_EDGE_UPDATE, HEARTBEAT } from "../src/config";

// Живость пульса владельцу (issue #1103): конвейер простоял 40+ минут
// 2026-09-13 и не ожил сам — движение вернул ручной workflow_dispatch. Ни
// один существующий сторож не мог закричать: pulse_guard.py::
// independent_pulse_check (#689) исполняется ВНУТРИ scheduler.py, то есть
// молчит ровно тогда, когда ни один workflow не запускается вовсе. Этот
// файл проверяет ЕДИНСТВЕННЫЙ наблюдатель, который живёт вне GitHub Actions:
// тик alarm()/scheduledTick() DO (Cloudflare-инфраструктура), решение —
// pulseHealthy() (одно место правды, не вторая копия порога), доставка —
// тот же #telegramApi, что и остальные алерты владельца (#470/#471/#575).

const WORKER = { fetch: (input: string, init?: RequestInit) => exports.default.fetch(input, init) };

// Алерты владельцу с #1495 идут В ТЕМУ категории, поэтому перед первым
// sendMessage воркер зовёт createForumTopic (один раз на категорию —
// message_thread_id кэшируется в SQL и переживает выгрузку DO). Тесты ниже
// про ДЕДУП АЛЕРТОВ, а не про темы: заводится тема один раз или каждый раз —
// отдельный вопрос с отдельным тестом («тема заводится один раз…» в этом же
// файле). Поэтому здесь сравнивается только поток sendMessage.
function alertsOnly(calls: string[]): string[] {
  return calls.filter((method) => method === "sendMessage");
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

function isGitHubRunsCall(input: string | URL | Request): boolean {
  try {
    const url = new URL(String(input));
    return url.hostname === "api.github.com" && url.pathname.endsWith("/runs");
  } catch {
    return false;
  }
}

function isGitHubDispatchCall(input: string | URL | Request): boolean {
  try {
    const url = new URL(String(input));
    return url.hostname === "api.github.com" && url.pathname.endsWith("/dispatches");
  } catch {
    return false;
  }
}

// alarm() дёргает и #checkDshEdgeUpdate (issue #73) на каждом тике — тот же
// приём, что harness.spec.ts (мок по точному URL, не реальная сеть): версии
// совпадают ("quiet", dshEdgeUpdateDecision) — самообновление dsh-edge не
// участвует в сценарии этого файла и не должно делать реальных исходящих
// вызовов в тестах (недетерминированная сеть — источник таймаутов, не
// относящийся к тому, что здесь проверяется).
function stubDshEdgeUpdate(input: string | URL | Request): Response | null {
  const href = String(input);
  if (href === DSH_EDGE_UPDATE.healthUrl) {
    return new Response(JSON.stringify({ version: "0.1.0" }), { status: 200 });
  }
  if (href === DSH_EDGE_UPDATE.registryUrl) {
    return new Response(JSON.stringify({ version: "0.1.0" }), { status: 200 });
  }
  return null;
}

async function getJson<T>(path: string): Promise<T> {
  return WORKER.fetch(`https://example.com${path}`, { headers: { Authorization: "Bearer test-token" } }).then(
    (res) => res.json<T>(),
  );
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

// Сценарий #1103: искусственный обрыв цепочки — серия падающих dispatch'ей
// (реальная прод-форма: GitHub 403 «rate limit exceeded», как в живом
// инциденте 2026-09-13), затем восстановление. Доказывает: (1) владелец
// получает РОВНО ОДИН алерт на весь инцидент, а не на каждый неудачный тик;
// (2) алерт приходит на ПЕРВОМ неудачном тике, не после N минут ожидания —
// раньше не бился НИКТО, теперь бьётся немедленно (0 добавленных вызовов
// GitHub API — Telegram, не GitHub, поэтому не может усугубить причину
// самого падения, исчерпание квоты #1100); (3) восстановление — тоже один
// алерт, не второй на каждый последующий здоровый тик.
describe("живость пульса владельцу: серия падающих dispatch'ей → один алерт, восстановление → один алерт (issue #1103)", () => {
  it("цепочка рвётся (403 несколько тиков подряд) — один incident-алерт; восстановление — один recovery-алерт", async () => {
    // "owner" — тот же фиксированный OWNER_OBJECT_NAME, что и WORKER.fetch
    // (index.ts): getJson("/api/status") ниже читает ИМЕННО этот инстанс.
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    let rateLimited = true;
    // Растущий id последнего run'а: реальный GitHub заводит НОВЫЙ run на
    // каждый принятый dispatch — константный id (found ревью PR #269/#303)
    // после ДВУХ подряд успешных тиков сам ложно объявил бы run_confirmed:
    // false (confirmPreviousRun(100,100) === false) и не давал бы алерту
    // дойти до чистого recovery. Инкремент только на 204 — отклонённый (403)
    // dispatch НЕ заводит run.
    let runId = 100;
    const telegramCalls: string[] = [];
    const telegramTexts: string[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const tgMethod = telegramApiMethod(input);
      if (tgMethod) {
        telegramCalls.push(tgMethod);
        // Только тексты САМИХ алертов: createForumTopic (#1495) несёт `name`
        // темы, не `text`, и попав сюда сдвинул бы индексы проверок ниже.
        if (tgMethod === "sendMessage" && typeof init?.body === "string")
          telegramTexts.push((JSON.parse(init.body) as { text: string }).text);
        return new Response(JSON.stringify({ ok: true, result: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (isGitHubRunsCall(input)) {
        // Находка ревью PR #1104: живой инцидент 2026-09-13 валил КАЖДЫЙ
        // вызов GitHub API, включая /runs — не только /dispatches. 403 без
        // JSON-тела здесь тоже, пока квота исчерпана; fetchLatestOrchestraRunId
        // честно отдаёт null на не-2xx (не бросает), поведение теста не меняет.
        if (rateLimited) return new Response(null, { status: 403 });
        return new Response(JSON.stringify({ workflow_runs: [{ id: runId }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) {
        // Прод-форма живого инцидента 2026-09-13: 403 без JSON-тела на
        // исчерпании квоты API installation (docs/research/21).
        if (rateLimited) return new Response(null, { status: 403 });
        runId++;
        return new Response(null, { status: 204 });
      }
      const dshEdge = stubDshEdgeUpdate(input);
      if (dshEdge) return dshEdge;
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        // Восемь подряд неудачных тиков (форма замера issue #1103: 08:19-08:40,
        // все workflow_dispatch, все failure) — цепочка рвётся, но алерт
        // обязан уйти ровно один раз, не восемь.
        for (let i = 0; i < 8; i++) {
          await instance.alarm();
        }
      });
      expect(alertsOnly(telegramCalls)).toEqual(["sendMessage"]);
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean; detail: string | null } | null }>(
        "/api/status",
      );
      expect(status.last_pulse?.dispatch_ok).toBe(false);
      expect(status.last_pulse?.detail).toContain("403");
      // Содержание алерта, не только факт «что-то ушло» (находка ревью PR
      // #1104): называет конкретную причину этого тика (не «null», не
      // общая фраза) и план — когда считать неполадку требующей ручной
      // проверки.
      expect(telegramTexts[0]).toContain("403");
      expect(telegramTexts[0]).not.toContain("null");
      expect(telegramTexts[0]).toContain("GH_DISPATCH_TOKEN");

      // Квота сброшена (живой инцидент: восстановилась в течение часа) —
      // следующий тик снова успешен, ровно один recovery-алерт.
      rateLimited = false;
      await runInDurableObject(stub, async (instance) => {
        await instance.alarm();
      });
      expect(alertsOnly(telegramCalls)).toEqual(["sendMessage", "sendMessage"]);

      // Ещё один здоровый тик подряд — второй recovery не шлём (флаг уже 0).
      await runInDurableObject(stub, async (instance) => {
        await instance.alarm();
      });
      expect(alertsOnly(telegramCalls)).toEqual(["sendMessage", "sendMessage"]);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  // Живой класс issue #713/#1103: alarm() подвис целиком (ни одного тика),
  // страховка Cron Trigger (scheduledTick) — единственный, кто вообще
  // вызывается. Алерт обязан уйти и с этого пути тоже: #tickPulseAlert
  // вызывается из общего #dispatchOrchestraTick, а не только из alarm().
  it("alarm подвис — страховка scheduledTick тоже шлёт алерт (issue #693/#1103, общий тик)", async () => {
    const id = env.HARNESS.idFromName("pulse-alert-cron-recovery");
    const stub = env.HARNESS.get(id);
    const staleTs = Date.now() - (HEARTBEAT.selfOrchestrationMs * 2 + 60_000);
    await seedPulse(stub, { ts: staleTs, dispatch_ok: true, detail: null, last_run_id: 100, run_confirmed: true });
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const telegramCalls: string[] = [];
    const telegramTexts: string[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const tgMethod = telegramApiMethod(input);
      if (tgMethod) {
        telegramCalls.push(tgMethod);
        // Только тексты САМИХ алертов: createForumTopic (#1495) несёт `name`
        // темы, не `text`, и попав сюда сдвинул бы индексы проверок ниже.
        if (tgMethod === "sendMessage" && typeof init?.body === "string")
          telegramTexts.push((JSON.parse(init.body) as { text: string }).text);
        return new Response(JSON.stringify({ ok: true, result: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (isGitHubRunsCall(input)) {
        return new Response(JSON.stringify({ workflow_runs: [{ id: 101 }] }), { status: 200 });
      }
      if (isGitHubDispatchCall(input)) return new Response(null, { status: 403 });
      const dshEdge = stubDshEdgeUpdate(input);
      if (dshEdge) return dshEdge;
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await instance.scheduledTick();
      });
      // Резервный dispatch, который scheduledTick() выполнила, сам провалился
      // (403) — #tickPulseAlert видит СВЕЖУЮ (ts: now) неудачную попытку, а
      // не «тик давно не обновлялся» (pulseStale здесь не участвует, см.
      // докстринг pulseAlertText, находка ревью PR #1104): текст несёт
      // причину ИМЕННО этого тика, не общую фразу.
      expect(alertsOnly(telegramCalls)).toEqual(["sendMessage"]);
      expect(telegramTexts[0]).toContain("403");
      expect(telegramTexts[0]).not.toContain("null");
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("TELEGRAM_CHAT_ID не задан — тик не падает и не звонит в Telegram («возможности нет»)", async () => {
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const savedChatId = env.TELEGRAM_CHAT_ID;
    env.TELEGRAM_CHAT_ID = "";
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (telegramApiMethod(input)) throw new Error("Telegram не должен звониться без TELEGRAM_CHAT_ID");
      if (isGitHubRunsCall(input)) return new Response(JSON.stringify({ workflow_runs: [] }), { status: 200 });
      if (isGitHubDispatchCall(input)) return new Response(null, { status: 403 });
      const dshEdge = stubDshEdgeUpdate(input);
      if (dshEdge) return dshEdge;
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance) => {
        await expect(instance.alarm()).resolves.toBeUndefined();
      });
      const status = await getJson<{ last_pulse: { dispatch_ok: boolean } | null }>("/api/status");
      expect(status.last_pulse?.dispatch_ok).toBe(false);
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
      env.TELEGRAM_CHAT_ID = savedChatId;
    }
  });

  // Класс «глубже квоты rows_read»: сама таблица дедуп-флага недоступна
  // (аналог DROP TABLE storage_probe в storage-ready.spec.ts) — тик не
  // должен падать и не должен спамить без записанного флага.
  it("таблица pulse_alert недоступна — тик не падает и не спамит (первый удавшийся тик догонит)", async () => {
    const id = env.HARNESS.idFromName("pulse-alert-no-dedup-table");
    const stub = env.HARNESS.get(id);
    const realFetch = globalThis.fetch;
    env.GH_DISPATCH_TOKEN = "test-dispatch-token";
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const telegramCalls: string[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const tgMethod = telegramApiMethod(input);
      if (tgMethod) {
        telegramCalls.push(tgMethod);
        return new Response(JSON.stringify({ ok: true, result: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (isGitHubRunsCall(input)) return new Response(JSON.stringify({ workflow_runs: [{ id: 1 }] }), { status: 200 });
      if (isGitHubDispatchCall(input)) return new Response(null, { status: 403 });
      const dshEdge = stubDshEdgeUpdate(input);
      if (dshEdge) return dshEdge;
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance, state) => {
        state.storage.sql.exec("DROP TABLE pulse_alert");
        await expect(instance.alarm()).resolves.toBeUndefined();
        expect(alertsOnly(telegramCalls)).toEqual([]);
        state.storage.sql.exec(
          "CREATE TABLE IF NOT EXISTS pulse_alert (id INTEGER PRIMARY KEY CHECK (id = 1), alerted INTEGER NOT NULL DEFAULT 0)",
        );
        await instance.alarm();
        expect(alertsOnly(telegramCalls)).toEqual(["sendMessage"]);
      });
    } finally {
      vi.unstubAllGlobals();
      env.GH_DISPATCH_TOKEN = "";
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });
});
