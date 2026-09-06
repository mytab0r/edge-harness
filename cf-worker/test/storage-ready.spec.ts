import { runInDurableObject } from "cloudflare:test";
import { env, exports } from "cloudflare:workers";
import { describe, expect, it, vi } from "vitest";

// Готовность хранилища (#575, вторая половина «зелёный health при мёртвой
// морде»): /api/health морды (config.ts DSH_EDGE_UPDATE.healthUrl) отдаёт
// только строку версии и не трогает хранилище вообще — этот файл проверяет
// ОТДЕЛЬНУЮ живую проверку (GET /api/ready), которая реально дёргает DO
// SQLite и краснеет при его отказе, и пульс (alarm), который замечает отказ
// непрерывно (единственный работающий 24/7 монитор — деплойная канарейка и
// failure-watch ловят другие классы отказов, см. докстринг
// #tickStorageReadyAlert в src/harness.ts).

const AUTH = { Authorization: "Bearer test-token" };
const WORKER = { fetch: (input: string, init?: RequestInit) => exports.default.fetch(input, init) };

async function getReady(): Promise<Response> {
  return WORKER.fetch("https://example.com/api/ready", { headers: AUTH });
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

describe("готовность хранилища: GET /api/ready (#575)", () => {
  it("хранилище отвечает — 200 {ok:true}", async () => {
    const res = await getReady();
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ ok: true });
  });

  it("без токена — 401, как и у остального API", async () => {
    const res = await WORKER.fetch("https://example.com/api/ready");
    expect(res.status).toBe(401);
  });

  // Мутация: реальный SQL-раундтрип ready-проверки ломается вместе с таблицей,
  // на которую он опирается (heartbeat) — тот же класс отказа, что и
  // исчерпание суточной квоты rows_read/rows_written (#320): ЛЮБОЙ exec на
  // DO SQLite отказывает, не только полные сканы. Докажи мутацией: замени
  // тело #checkStorageReady() на `return { ok: true, detail: null }` без
  // реального exec — этот тест перестанет ловить обрыв и останется зелёным
  // даже после DROP TABLE ниже.
  it("хранилище отвечает ошибкой (таблица снесена) — /api/ready красный, тем же кодом, что и storageErrorResponse", async () => {
    const id = env.HARNESS.idFromName("owner");
    const stub = env.HARNESS.get(id);
    try {
      await runInDurableObject(stub, async (_instance, state) => {
        state.storage.sql.exec("DROP TABLE heartbeat");
      });
      const res = await getReady();
      expect(res.status).toBe(500);
      const body = await res.json<{ error: { code: string; message: string } }>();
      // classifyStorageError не узнаёт формулировку SQLite "no such table" как
      // quota-паттерн — код внутренний, но ФОРМА ответа (error.code/message)
      // обязана быть той же, что и у остального API (storageErrorResponse,
      // ОДНО место правды классификации — не вторая копия для /api/ready).
      expect(body.error.code).toBe("internal");
      expect(body.error.message).toMatch(/heartbeat/i);
    } finally {
      // Инстанс общий с остальными тестами файла (Miniflare не изолирует
      // хранилище между тестами одного файла) — таблица обязана вернуться,
      // иначе следующий тест этого файла (и /api/status где угодно ещё)
      // унаследует снесённую heartbeat.
      await runInDurableObject(stub, async (_instance, state) => {
        state.storage.sql.exec(
          "CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY CHECK (id = 1), ts INTEGER NOT NULL, job_id TEXT NOT NULL)",
        );
      });
    }
  });
});

describe("пульс: готовность хранилища замечается непрерывно, не только по запросу (#575)", () => {
  it("тик пульса пишет живой исход в storage_probe при здоровом хранилище", async () => {
    const id = env.HARNESS.idFromName("pulse-storage-ready-ok");
    const stub = env.HARNESS.get(id);
    await runInDurableObject(stub, async (instance, state) => {
      await instance.alarm();
      const row = state.storage.sql.exec("SELECT ok, detail FROM storage_probe WHERE id = 1").toArray()[0] as
        | { ok: number; detail: string | null }
        | undefined;
      expect(row).toBeTruthy();
      expect(Number(row!.ok)).toBe(1);
      expect(row!.detail).toBeNull();
    });
  });

  it("хранилище отвечает ошибкой во время тика — переход в unhealthy шлёт алерт владельцу ровно один раз, не каждый тик", async () => {
    const id = env.HARNESS.idFromName("pulse-storage-ready-broken");
    const stub = env.HARNESS.get(id);
    const realFetch = globalThis.fetch;
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const telegramCalls: string[] = [];
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      const method = telegramApiMethod(input);
      if (method) {
        telegramCalls.push(method);
        return new Response(JSON.stringify({ ok: true, result: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance, state) => {
        state.storage.sql.exec("DROP TABLE heartbeat");
        // Первый тик: переход healthy(нет строки)→unhealthy — алерт обязан уйти.
        await instance.alarm();
        const row = state.storage.sql.exec("SELECT ok FROM storage_probe WHERE id = 1").toArray()[0] as
          | { ok: number }
          | undefined;
        expect(Number(row!.ok)).toBe(0);
        expect(telegramCalls).toEqual(["sendMessage"]);
        // Второй тик подряд: хранилище всё ещё сломано — второй алерт не шлём,
        // иначе спам раз в 15 минут до сброса квоты в 00:00 UTC.
        await instance.alarm();
        expect(telegramCalls).toEqual(["sendMessage"]);
        // Восстановление: таблица вернулась — переход unhealthy→healthy шлёт
        // ровно один алерт о восстановлении.
        state.storage.sql.exec(
          "CREATE TABLE IF NOT EXISTS heartbeat (id INTEGER PRIMARY KEY CHECK (id = 1), ts INTEGER NOT NULL, job_id TEXT NOT NULL)",
        );
        await instance.alarm();
        expect(telegramCalls).toEqual(["sendMessage", "sendMessage"]);
      });
    } finally {
      vi.unstubAllGlobals();
      env.TELEGRAM_BOT_TOKEN = "";
    }
  });

  it("TELEGRAM_CHAT_ID не задан — тик не падает и не пытается звонить в Telegram («возможности нет»)", async () => {
    const id = env.HARNESS.idFromName("pulse-storage-ready-no-chat");
    const stub = env.HARNESS.get(id);
    const realFetch = globalThis.fetch;
    env.TELEGRAM_BOT_TOKEN = "test-bot-token";
    const savedChatId = env.TELEGRAM_CHAT_ID;
    env.TELEGRAM_CHAT_ID = "";
    vi.stubGlobal("fetch", (async (input: string | URL | Request, init?: RequestInit) => {
      if (telegramApiMethod(input)) throw new Error("Telegram не должен звониться без TELEGRAM_CHAT_ID");
      return realFetch(input as RequestInfo, init);
    }) as typeof fetch);
    try {
      await runInDurableObject(stub, async (instance, state) => {
        state.storage.sql.exec("DROP TABLE heartbeat");
        await expect(instance.alarm()).resolves.toBeUndefined();
        const row = state.storage.sql.exec("SELECT ok FROM storage_probe WHERE id = 1").toArray()[0] as
          | { ok: number }
          | undefined;
        expect(Number(row!.ok)).toBe(0);
      });
    } finally {
      vi.unstubAllGlobals();
      env.TELEGRAM_BOT_TOKEN = "";
      env.TELEGRAM_CHAT_ID = savedChatId;
    }
  });
});
