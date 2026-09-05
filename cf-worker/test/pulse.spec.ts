import { describe, expect, it } from "vitest";
import {
  attemptOrchestraDispatch,
  confirmPreviousRun,
  dshEdgeUpdateDecision,
  fetchLatestOrchestraRunId,
  pulseHealthy,
} from "../src/harness";
import { HEARTBEAT } from "../src/config";

// Чистое решение самообновления морды (#73): все ветки логики без сети.
// Проводка (fetch/storage/dispatch) тонкая и зеркалит уже доказанный
// dispatch orchestra; живая петля закрывается первым же npm-релизом.
const NOW = 1_800_000_000_000;
const THROTTLE = 4 * 60 * 60 * 1000;

describe("самообновление морды: решение", () => {
  it("расхождение версий без истории попыток — dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", undefined, NOW)).toBe("dispatch");
  });
  it("равенство версий — тихо, даже если попытка была давно", () => {
    expect(dshEdgeUpdateDecision("0.7.1", "0.7.1", NOW - THROTTLE - 1, NOW)).toBe("quiet");
  });
  it("расхождение на свежей попытке — throttled (штурма нет)", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", NOW - 1000, NOW)).toBe("throttled");
  });
  it("граница троттла: ровно throttleMs назад — снова dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.0", "0.7.1", NOW - THROTTLE, NOW)).toBe("dispatch");
  });
  it("даунгрейд тоже чинится: расхождение в любую сторону — dispatch", () => {
    expect(dshEdgeUpdateDecision("0.7.1", "0.7.0", undefined, NOW)).toBe("dispatch");
  });
});

// Гвардия issue #269: раньше alarm() не проверял статус ответа GitHub на dispatch
// оркестратора вовсе — fetch() не бросает на HTTP-ошибках, только на сетевых, и
// 403/404/429 тонули как «успех» (класс уже пофикшен в #postTask, но не здесь).
// Докажи мутацией: убери проверку `res.status !== 204` в attemptOrchestraDispatch
// (замени на `if (false)`) — тест «403 … — ok:false» покраснеет.
//
// НАХОДКА РЕВЬЮ: 204 — доказательство только ПРИЁМА dispatch'а GitHub'ом, НЕ
// запуска (docs/research/21-github-actions.md: файл workflow не на default
// branch тоже отвечает 204, и ничего не происходит — «успешный HTTP-код здесь
// не является доказательством запуска»). Название теста ниже отражает именно
// это; настоящее доказательство запуска — confirmPreviousRun ниже.
describe("пульс оркестрации: dispatch-тик (issue #269)", () => {
  function fakeFetch(status: number): typeof fetch {
    return (async () => new Response(null, { status })) as typeof fetch;
  }

  it("204 — доказательство ПРИЁМА dispatch'а (не запуска): ok:true, detail:null", async () => {
    const result = await attemptOrchestraDispatch("token", "owner/repo", fakeFetch(204));
    expect(result).toEqual({ ok: true, detail: null });
  });

  it("403 (вторичный rate-limit GitHub) — ok:false с кодом в detail, не тихий успех", async () => {
    const result = await attemptOrchestraDispatch("token", "owner/repo", fakeFetch(403));
    expect(result.ok).toBe(false);
    expect(result.detail).toContain("403");
  });

  it("404 (протухший токен/репозиторий) — ok:false", async () => {
    const result = await attemptOrchestraDispatch("token", "owner/repo", fakeFetch(404));
    expect(result.ok).toBe(false);
    expect(result.detail).toContain("404");
  });

  it("сетевая ошибка (fetch бросает) — ok:false, сообщение в detail", async () => {
    const throwingFetch = (async () => {
      throw new Error("network unreachable");
    }) as typeof fetch;
    const result = await attemptOrchestraDispatch("token", "owner/repo", throwingFetch);
    expect(result).toEqual({ ok: false, detail: "network unreachable" });
  });
});

// Гвардия находки ревью (#269): 204 — не доказательство запуска. Настоящее
// доказательство — появление run'а orchestra.yml; проверяется на следующем
// тике сравнением id последнего run'а (baseline vs latest). Докажи мутацией:
// замени `return latest !== baseline` на `return true` в confirmPreviousRun —
// тест «baseline и latest совпали — запуск НЕ подтверждён» покраснеет.
describe("пульс оркестрации: fetchLatestOrchestraRunId (issue #269)", () => {
  it("успешный ответ — id последнего run'а из workflow_runs[0]", async () => {
    const fakeFetch = (async () =>
      new Response(JSON.stringify({ workflow_runs: [{ id: 555 }, { id: 111 }] }), { status: 200 })) as typeof fetch;
    expect(await fetchLatestOrchestraRunId("token", "owner/repo", fakeFetch)).toBe(555);
  });

  it("run'ов ещё не было (пустой список) — null, не 0 и не ошибка", async () => {
    const fakeFetch = (async () => new Response(JSON.stringify({ workflow_runs: [] }), { status: 200 })) as typeof fetch;
    expect(await fetchLatestOrchestraRunId("token", "owner/repo", fakeFetch)).toBeNull();
  });

  it("не-2xx — null (не бросает, вызывающий код не гадает о причине)", async () => {
    const fakeFetch = (async () => new Response(null, { status: 500 })) as typeof fetch;
    expect(await fetchLatestOrchestraRunId("token", "owner/repo", fakeFetch)).toBeNull();
  });

  it("сетевая ошибка — null, не бросает наружу", async () => {
    const throwingFetch = (async () => {
      throw new Error("network unreachable");
    }) as typeof fetch;
    expect(await fetchLatestOrchestraRunId("token", "owner/repo", throwingFetch)).toBeNull();
  });
});

describe("пульс оркестрации: confirmPreviousRun — реальный запуск, не только приём (issue #269)", () => {
  it("baseline и latest совпали — запуск НЕ подтверждён (файл не на default branch и т.п.)", () => {
    expect(confirmPreviousRun(100, 100)).toBe(false);
  });

  it("latest новее baseline — запуск подтверждён", () => {
    expect(confirmPreviousRun(100, 101)).toBe(true);
  });

  it("baseline неизвестен (первый тик) — null, рано судить, не «не подтверждено»", () => {
    expect(confirmPreviousRun(null, 101)).toBeNull();
  });

  it("latest неизвестен (fetch не удался) — null, рано судить", () => {
    expect(confirmPreviousRun(100, null)).toBeNull();
  });
});

describe("пульс оркестрации: pulseHealthy — здоров ли пульс (issue #269)", () => {
  const FRESH_MS = HEARTBEAT.selfOrchestrationMs * 2 - 1;
  const STALE_MS = HEARTBEAT.selfOrchestrationMs * 2 + 1;

  it("ни разу не тикал (холодный старт) — здоров, не повод кричать", () => {
    expect(pulseHealthy(NOW, null)).toBe(true);
  });

  it("возможности нет (секреты не заданы) — здоров, это конфигурация, не поломка", () => {
    expect(
      pulseHealthy(NOW, { ts: NOW - 10 * 60_000, dispatch_ok: false, detail: "not_configured", run_confirmed: null }),
    ).toBe(true);
  });

  it("последний dispatch удался и свежий, подтверждение ещё рано (null) — здоров", () => {
    expect(pulseHealthy(NOW, { ts: NOW - FRESH_MS, dispatch_ok: true, detail: null, run_confirmed: null })).toBe(true);
  });

  it("последний dispatch удался, но давно (alarm подвис) — нездоров", () => {
    expect(pulseHealthy(NOW, { ts: NOW - STALE_MS, dispatch_ok: true, detail: null, run_confirmed: true })).toBe(
      false,
    );
  });

  it("последний dispatch провалился (даже свежий тик) — нездоров сразу, без ожидания порога", () => {
    expect(
      pulseHealthy(NOW, { ts: NOW, dispatch_ok: false, detail: "dispatch отклонён: 403", run_confirmed: null }),
    ).toBe(false);
  });

  // НАХОДКА РЕВЬЮ: главный сценарий этого фикса — GitHub принял dispatch (204),
  // но запуска не случилось (файл workflow не на default branch — самая частая
  // причина по docs/research/21). Без этой ветки пульс молчал бы «здоров»
  // сколь угодно долго, потому что 204 продолжал бы приходить каждый тик.
  it("204 принят, но run НЕ подтверждён — нездоров сразу, даже на свежем тике", () => {
    expect(pulseHealthy(NOW, { ts: NOW, dispatch_ok: true, detail: null, run_confirmed: false })).toBe(false);
  });
});
