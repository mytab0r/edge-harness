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
});
