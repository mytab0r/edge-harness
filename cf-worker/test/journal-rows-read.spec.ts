import { runInDurableObject } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { describe, expect, it } from "vitest";

// Гвардия цены чтения журнала по задаче (#1411).
//
// Класс: запрос горячего пути, чья цена растёт вместе с таблицей. Третья
// дверь того же класса после #320 (полный скан tasks) и #575 (полный скан
// messages) — и, в отличие от них, вскрылась не в агрегате, а в сортировке.
//
// Живой инцидент 2026-09-21: 12 436 246 rows_read за ОДИН час при суточном
// лимите 5 000 000 (93.2% суточного расхода), в тот же час 34 632
// rows_written — ~359 прочитанных строк на каждую записанную. Квота
// кончилась, и морда начала отвечать `HTTP 500: Exceeded allowed rows read
// in Durable Objects free tier` на ЛЮБОЙ SELECT: молча перестали
// архивироваться сессии раннера и дописываться логи итогов.
//
// Причина: `GET /api/events?task_id=…&after=…&limit=…` (клиенты —
// scripts/hands/dsh_task.sh и scripts/lib/journal_status.sh, опрос в цикле
// пока идёт job) исполняет `WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?`.
// Индекса, ведущего (task_id, id), не было; UNIQUE(task_id, seq) даёт порядок
// по seq, не по id — SQLite читал ВСЕ события задачи и сортировал их во
// временном B-дереве, и только потом применял LIMIT. Курсор `after` не
// ограничивал скан вовсе.
//
// Гвардия меряет РОВНО ту величину, которая кончилась: `cursor.rowsRead`
// настоящего SqlStorage на настоящем workerd. Не план запроса (его
// формулировка меняется от версии к версии SQLite) и не время (оно шумит на
// раннере) — само число прочитанных строк, то же, что считает Cloudflare.

const EVENTS = 1200;   // заметно больше лимита выборки: без индекса читаются все
const LIMIT = 100;     // столько строк запрашивает клиент

/** Во сколько раз прочитанному позволено превысить запрошенное. Не «ровно
 *  LIMIT»: поиск по индексу читает ещё и строки самой таблицы, а порядок
 *  учёта rowsRead в workerd — их дело, не наше. Запас втрое отличает
 *  «ограничено курсором» от «прочитана вся задача» (12 крат при этих
 *  числах) с гарантией, что тест не станет хрупким от версии рантайма. */
const BUDGET = LIMIT * 3;

describe("цена чтения журнала по задаче (#1411)", () => {
  it("курсор ограничивает чтение, а не только выдачу", async () => {
    const taskId = `rows-read-${Date.now()}`;
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    const rowsRead = await runInDurableObject(stub, async (_instance, state) => {
      const sql = state.storage.sql;
      for (let seq = 0; seq < EVENTS; seq++) {
        sql.exec(
          "INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, 'agent', 'k', 'd')",
          taskId, seq, Date.now(),
        );
      }
      // Тот же SQL, что исполняет маршрут: курсор `after` = 0 (клиент только
      // начал читать) — худший и самый частый случай опроса.
      const cursor = sql.exec(
        "SELECT id, task_id, seq, ts, source, kind, data FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
        taskId, 0, LIMIT,
      );
      const rows = cursor.toArray();
      expect(rows.length).toBe(LIMIT);
      return cursor.rowsRead;
    });

    expect(rowsRead).toBeLessThanOrEqual(BUDGET);
  });

  it("цена не растёт вместе с числом событий задачи", async () => {
    // Вторая дверь того же класса, и именно она отличает «сейчас дёшево» от
    // «дёшево навсегда»: инцидент случился не потому, что запрос был дорог с
    // самого начала, а потому, что он дорожал с каждым событием сессии.
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    const measure = async (events: number): Promise<number> => {
      const taskId = `rows-read-growth-${events}-${Date.now()}`;
      return await runInDurableObject(stub, async (_instance, state) => {
        const sql = state.storage.sql;
        for (let seq = 0; seq < events; seq++) {
          sql.exec(
            "INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, 'agent', 'k', 'd')",
            taskId, seq, Date.now(),
          );
        }
        const cursor = sql.exec(
          "SELECT id, task_id, seq, ts, source, kind, data FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
          taskId, 0, LIMIT,
        );
        cursor.toArray();
        return cursor.rowsRead;
      });
    };

    const small = await measure(200);
    const large = await measure(2000);

    // Десятикратный рост таблицы задачи не имеет права десятикратно поднять
    // цену чтения: курсор берёт с начала индекса и останавливается на LIMIT.
    expect(large).toBeLessThanOrEqual(small * 2);
  });

  it("системное событие стоит одинаково на короткой и на длинной сессии", async () => {
    // Вторая дверь того же класса, найденная ai-review PR #1425:
    // `#emitSystemEvent` брал следующий отрицательный seq через
    // `COUNT(*) … WHERE task_id = ? AND source = 'system'`. `source` ни в
    // одном индексе не ведёт, поэтому COUNT читал ВСЕ события задачи — цена
    // системного события росла вместе с длиной сессии, ровно как у чтения
    // журнала до индекса. Замер на 100 000 событий: 14.25 мс → 0.003 мс.
    //
    // Гвардия мерит `rowsRead` того запроса, который выдаёт seq, на двух
    // сессиях разной длины: закрытая дверь значит «одинаково», открытая —
    // «во столько же раз больше, во сколько длиннее сессия».
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    const measureSeqLookup = async (events: number): Promise<number> => {
      const taskId = `sys-seq-${events}-${Date.now()}`;
      return await runInDurableObject(stub, async (_instance, state) => {
        const sql = state.storage.sql;
        for (let seq = 1; seq <= events; seq++) {
          sql.exec(
            "INSERT INTO events (task_id, seq, ts, source, kind, data) VALUES (?, ?, ?, 'agent', 'k', 'd')",
            taskId, seq, Date.now(),
          );
        }
        const cursor = sql.exec("SELECT MIN(seq) AS m FROM events WHERE task_id = ?", taskId);
        cursor.toArray();
        return cursor.rowsRead;
      });
    };

    const short = await measureSeqLookup(100);
    const long = await measureSeqLookup(2000);

    // Не «short * 2», а жёстко: поиск минимума по префиксу индекса — это один
    // seek, и его цена от длины сессии не зависит вовсе.
    expect(long).toBeLessThanOrEqual(Math.max(short, 5));
  });
});

// Гвардия цены ватчдога застрявших сообщений (#1503).
//
// Четвёртая дверь того же класса, и самая дорогая: запрос жил не на пути
// запроса, а на ПЕРИОДИЧЕСКОМ тике, то есть платился сам по себе, без
// единого обращения владельца.
//
// Живой инцидент 2026-09-23/24: DO вычитывал 1 837 397 строк за 85 минут при
// 277 запросах воркера, суточный лимит rows_read (5 000 000) выжигался за
// несколько часов. Морда отвечала «Exceeded allowed rows read in Durable
// Objects free tier» — дословно из Workers Logs, — и кнопки владельца в
// Telegram умирали вместе с ней: нажатие идёт тем же путём, через DO.
//
// Причина: `#reclaimStuckMessages` выбирал ВСЕ сообщения в processing
// (`WHERE status='processing' AND processing_ts IS NOT NULL`, без LIMIT), а
// порог застревания проверялся уже в JS. Свежие сообщения, которым до порога
// ещё минуты, читались каждый тик наравне с застрявшими. Отказ шёл по кругу:
// чем больше в очереди, тем дороже тик, тем вероятнее следующий отказ.
//
// Мерим ту же величину, что кончилась, тем же способом, что выше: rowsRead
// настоящего SqlStorage, не план запроса и не время.

const FRESH = 800;          // сообщений в processing, которым ещё далеко до порога
const RECLAIM_BUDGET = 200; // потолок: батч 50 + накладные индекса, втрое с запасом

describe("цена ватчдога застрявших сообщений (#1503)", () => {
  it("свежие processing не читаются: порог в SQL, а не в JS", async () => {
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    const rowsRead = await runInDurableObject(stub, async (_instance, state) => {
      const sql = state.storage.sql;
      const now = Date.now();
      for (let i = 0; i < FRESH; i++) {
        // processing_ts = сейчас: до порога messageStuckProcessingMs (10 мин)
        // этим сообщениям ещё далеко, разбирать их нечего.
        sql.exec(
          "INSERT INTO messages (source, source_msg_id, ts, text, status, attempts, processing_ts) " +
            "VALUES ('test', ?, ?, 'x', 'processing', 0, ?)",
          `stuck-guard-${now}-${i}`, now, now,
        );
      }
      // Тот же SQL, что исполняет ватчдог.
      const cursor = sql.exec(
        `SELECT id, attempts, processing_ts FROM messages
         WHERE status = 'processing' AND processing_ts IS NOT NULL AND processing_ts < ?
         ORDER BY processing_ts ASC LIMIT ?`,
        now - 10 * 60_000, 50,
      );
      const rows = cursor.toArray();
      // Ни одно свежее сообщение не отобрано — это и есть суть фикса.
      expect(rows.length).toBe(0);
      return cursor.rowsRead;
    });

    expect(rowsRead).toBeLessThanOrEqual(RECLAIM_BUDGET);
  });

  it("цена не растёт вместе с очередью processing", async () => {
    // Отличает «сейчас дёшево» от «дёшево навсегда»: инцидент случился не
    // потому, что тик был дорог изначально, а потому, что он дорожал с каждым
    // новым сообщением в очереди.
    const stub = env.HARNESS.get(env.HARNESS.idFromName("owner"));

    const measure = async (count: number): Promise<number> => {
      return await runInDurableObject(stub, async (_instance, state) => {
        const sql = state.storage.sql;
        const now = Date.now();
        const tag = `stuck-growth-${count}-${now}`;
        for (let i = 0; i < count; i++) {
          sql.exec(
            "INSERT INTO messages (source, source_msg_id, ts, text, status, attempts, processing_ts) " +
              "VALUES ('test', ?, ?, 'x', 'processing', 0, ?)",
            `${tag}-${i}`, now, now,
          );
        }
        const cursor = sql.exec(
          `SELECT id, attempts, processing_ts FROM messages
           WHERE status = 'processing' AND processing_ts IS NOT NULL AND processing_ts < ?
           ORDER BY processing_ts ASC LIMIT ?`,
          now - 10 * 60_000, 50,
        );
        cursor.toArray();
        return cursor.rowsRead;
      });
    };

    const small = await measure(200);
    const large = await measure(2000);
    // Десятикратный рост очереди не имеет права удорожать тик даже вдвое.
    expect(large).toBeLessThanOrEqual(Math.max(small * 2, RECLAIM_BUDGET));
  });
});
