import { describe, expect, it } from "vitest";
import {
  attemptOrchestraDispatch,
  confirmPreviousRun,
  dshEdgeUpdateDecision,
  fetchLatestOrchestraRunId,
  pulseDetailForRecord,
  pulseHealthy,
  pulseNotConfigured,
  pulseStale,
  retentionBacklog,
} from "../src/harness";
import { HEARTBEAT, RETENTION } from "../src/config";

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

  // НАХОДКА РЕВЬЮ (#303): orchestra.yml запускается не только нашим
  // workflow_dispatch — job `contract` триггерится на каждый pull_request
  // (пуш, навешивание метки), плюс `schedule`. Без ?event=workflow_dispatch
  // запрос видит ЛЮБОЙ последний run, id почти всегда отличается от
  // baseline — confirmPreviousRun() почти всегда докладывает "запуск
  // подтверждён" именно тогда, когда наш dispatch не выстрелил. Докажи
  // мутацией: убери `&event=workflow_dispatch` из URL в
  // fetchLatestOrchestraRunId — этот тест покраснеет.
  it("запрос фильтрует run'ы по событию workflow_dispatch — не любой последний run", async () => {
    let requestedUrl: string | null = null;
    const fakeFetch = (async (url: string) => {
      requestedUrl = url;
      return new Response(JSON.stringify({ workflow_runs: [{ id: 1 }] }), { status: 200 });
    }) as typeof fetch;
    await fetchLatestOrchestraRunId("token", "owner/repo", fakeFetch);
    expect(requestedUrl).toContain("event=workflow_dispatch");
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

// #303, находка ревью: HEARTBEAT.notConfiguredDetail — единственное место
// правды в TypeScript, но литерал "not_configured" был продублирован во
// фронте (app.js сравнивал status.last_pulse.detail === "not_configured"
// сам) — переименование значения сломало бы обе ветки бейджа во фронте
// молча. pulseNotConfigured — то, что теперь пишется в Status.pulse_not_configured
// и заменяет литерал во фронте на предвычисленный флаг.
describe("пульс оркестрации: pulseNotConfigured (issue #303)", () => {
  it("холодный старт (null) — не 'возможности нет', это ещё рано", () => {
    expect(pulseNotConfigured(null)).toBe(false);
  });

  it("detail совпадает с сентинелом — возможности нет", () => {
    expect(
      pulseNotConfigured({ ts: NOW, dispatch_ok: false, detail: "not_configured", run_confirmed: null }),
    ).toBe(true);
  });

  it("detail другой (реальная поломка) — не 'возможности нет'", () => {
    expect(
      pulseNotConfigured({ ts: NOW, dispatch_ok: false, detail: "dispatch отклонён: 403", run_confirmed: null }),
    ).toBe(false);
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

// НАХОДКА РЕВЬЮ (#303, вторая находка того же PR): pulseHealthy() гасит бейдж по
// ДВУМ разным причинам — «dispatch/run сломан» (detail всегда человекочитаемый,
// см. pulseDetailForRecord) и «подвис alarm» (detail остаётся null, потому что
// dispatch ЭТОГО тика был успешен). pulseStale выделяет вторую причину отдельным
// флагом — фронт обязан различать их ДО того, как решит, можно ли подставлять
// detail в бейдж. Докажи мутацией: замени `>=` на `>` (или вовсе `return false`)
// в pulseStale — тест «alarm подвис ровно на пороге — stale» покраснеет либо
// граничный тест перестанет ловить регресс.
describe("пульс оркестрации: pulseStale — unhealthy из-за подвисшего alarm, не из-за dispatch/run (issue #303)", () => {
  const FRESH_MS = HEARTBEAT.selfOrchestrationMs * 2 - 1;
  const STALE_MS = HEARTBEAT.selfOrchestrationMs * 2;

  it("ни разу не тикал (холодный старт) — не stale", () => {
    expect(pulseStale(NOW, null)).toBe(false);
  });

  it("возможности нет (секреты не заданы) — не stale, это другая причина", () => {
    expect(
      pulseStale(NOW, { ts: NOW - STALE_MS, dispatch_ok: false, detail: "not_configured", run_confirmed: null }),
    ).toBe(false);
  });

  it("dispatch провалился — не stale, причина уже в detail (dispatch/run, не alarm)", () => {
    expect(
      pulseStale(NOW, { ts: NOW - STALE_MS, dispatch_ok: false, detail: "dispatch отклонён: 403", run_confirmed: null }),
    ).toBe(false);
  });

  it("run НЕ подтверждён — не stale, причина уже в detail (через pulseDetailForRecord)", () => {
    expect(pulseStale(NOW, { ts: NOW - STALE_MS, dispatch_ok: true, detail: null, run_confirmed: false })).toBe(false);
  });

  it("dispatch удался и свежий — не stale", () => {
    expect(pulseStale(NOW, { ts: NOW - FRESH_MS, dispatch_ok: true, detail: null, run_confirmed: true })).toBe(false);
  });

  it("dispatch удался, но давно (alarm подвис) — stale, detail в хранилище честно null", () => {
    expect(pulseStale(NOW, { ts: NOW - STALE_MS, dispatch_ok: true, detail: null, run_confirmed: true })).toBe(true);
  });
});

// НАХОДКА РЕВЬЮ (#303): главный сценарий этого фикса — dispatch ЭТОГО тика принят
// (attemptOrchestraDispatch честно отдаёт detail: null, причины нет), но
// run_confirmed о ПРЕДЫДУЩЕМ dispatch'е — false. Без подмены detail это null
// уходит в pulse.detail, а морда (public/assets/app.js:179,
// t("pulse.unhealthy", { detail: status.last_pulse.detail })) рендерит буквальную
// строку "null" — неотличимо от честной поломки. Докажи мутацией: верни в
// pulseDetailForRecord просто `result.detail` без спецслучая run_confirmed —
// и тест «бейдж не содержит null» ниже покраснеет.
describe("пульс оркестрации: pulseDetailForRecord — detail не должен тонуть в null (issue #303)", () => {
  it("dispatch принят, но run НЕ подтверждён — detail человекочитаемый, не null", () => {
    expect(pulseDetailForRecord({ ok: true, detail: null }, false)).toBe(HEARTBEAT.runNotConfirmedDetail);
  });

  it("dispatch принят и run подтверждён (или рано судить) — detail как есть (null)", () => {
    expect(pulseDetailForRecord({ ok: true, detail: null }, true)).toBeNull();
    expect(pulseDetailForRecord({ ok: true, detail: null }, null)).toBeNull();
  });

  it("dispatch провалился ЭТОГО тика — реальная причина в приоритете, не подмена run_confirmed", () => {
    expect(pulseDetailForRecord({ ok: false, detail: "dispatch отклонён: 403" }, false)).toBe(
      "dispatch отклонён: 403",
    );
  });
});

// Прод-форма рендера бейджа — тот же шаблон (i18n/ru.js: "pulse.unhealthy") и та
// же подстановка `{key}` → String(params[key]), что app.js:12-16 (t()) и
// app.js:179 (renderStatus). Без DOM/fs в тестовом workerd-раннере (нет
// document, node:fs читает только виртуальную ФС песочницы — проверено пробой)
// прогнать реальный app.js нельзя; вместо пересказа шаблон скопирован дословно
// из public/assets/i18n/ru.js — но копия сама по себе пересказ, а не гарантия:
// правку прод-текста в ru.js этот файл не заметит. Побайтовый паритет самой
// строки PULSE_UNHEALTHY_TEMPLATE ниже с i18n/ru.js["pulse.unhealthy"]
// охраняет отдельно scripts/check-frontend-contract.mjs (npm run check,
// #303, находка ревью) — красный check, если кто-то поправит один файл и
// забудет другой.
const PULSE_UNHEALTHY_TEMPLATE = "⚠️ пульс оркестрации не бьётся: {detail} ({minutes} мин назад)";
function renderPulseUnhealthy(params: Record<string, unknown>): string {
  return PULSE_UNHEALTHY_TEMPLATE.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
}

// Прод-текст второй ветки бейджа (i18n/ru.js: "pulse.stale") — «подвис alarm»,
// та самая ветка, где last_pulse.detail остаётся null (см. pulseStale в
// src/harness.ts). Шаблон без {detail}: это ровно фикс находки ревью — фронт
// не подставляет сюда сырой (потенциально null) detail вообще, текст
// самодостаточен и параметризован только {minutes}. Паритет со строкой в
// ru.js охраняет scripts/check-frontend-contract.mjs (пункт 6).
const PULSE_STALE_TEMPLATE = "⚠️ пульс оркестрации не бьётся: тик давно не обновлялся ({minutes} мин назад)";
function renderPulseStale(params: Record<string, unknown>): string {
  return PULSE_STALE_TEMPLATE.replace(/\{(\w+)\}/g, (_, k) => (k in params ? String(params[k]) : `{${k}}`));
}

describe("бейдж пульса: ветка 'dispatch/run сломан' не содержит null (issue #303, находка ревью)", () => {
  it("главный сценарий — dispatch принят, run не подтверждён: detail из pulseDetailForRecord, бейдж без 'null'", () => {
    const detail = pulseDetailForRecord({ ok: true, detail: null }, false);
    const rendered = renderPulseUnhealthy({ detail, minutes: 5 });
    expect(rendered).not.toContain("null");
    expect(rendered).toBe("⚠️ пульс оркестрации не бьётся: принят, запуск не появился (5 мин назад)");
  });

  it("сырой null (без фикса) — контрольный пример: бейдж содержит 'null'", () => {
    // Не проверка кода pulseDetailForRecord — иллюстрация того, что чинит фикс:
    // если бы detail остался null (как отдаёт attemptOrchestraDispatch при ok:true),
    // бейдж показывал бы буквальное "null" — ровно находка ревью.
    const rendered = renderPulseUnhealthy({ detail: null, minutes: 5 });
    expect(rendered).toContain("null");
  });
});

// НАХОДКА РЕВЬЮ (#303, вторая находка того же PR): гвардия выше покрывала только
// ветку «dispatch/run сломан» — вторая ветка unhealthy («подвис alarm», где
// last_pulse.detail остаётся честным null уже в хранилище, см. pulseStale)
// оставалась незамеченной: старый app.js подставлял туда тот же сырой detail
// и рендерил буквальную строку "null". Докажи мутацией: в app.js убери ветку
// `else if (status.pulse_stale) { … }` (оставь только `!status.pulse_healthy`
// с сырым detail) — «сырой null (stale, без фикса)» ниже иллюстрирует ровно
// то, что стало бы видно пользователю.
// Ретеншн DO SQLite (#306/#305): «не успеваем» — чистая функция порога, тот же
// приём, что pulseStale выше (проверяется отдельно от alarm()/#pruneRetention,
// чтобы не гонять реальные тики ради граничного случая). Докажи мутацией:
// замени `>=` на `>` в retentionBacklog — тест «ровно порог — уже backlog»
// покраснеет.
describe("ретеншн: retentionBacklog — «не успеваем», не разовый полный тик (#306/#305)", () => {
  it("ни одного полного/упавшего тика подряд — не backlog", () => {
    expect(retentionBacklog(0)).toBe(false);
  });

  it("на единицу меньше порога — ещё не backlog (один тик после большого бэклога — не тревога)", () => {
    expect(retentionBacklog(RETENTION.backlogStreakThreshold - 1)).toBe(false);
  });

  it("ровно порог подряд идущих полных/упавших тиков — уже backlog", () => {
    expect(retentionBacklog(RETENTION.backlogStreakThreshold)).toBe(true);
  });
});

describe("бейдж пульса: ветка 'подвис alarm' (stale) не содержит null (issue #303, вторая находка ревью)", () => {
  it("stale-ветка: свой самодостаточный текст, detail вообще не участвует — бейдж без 'null'", () => {
    const rendered = renderPulseStale({ minutes: 35 });
    expect(rendered).not.toContain("null");
    expect(rendered).toBe("⚠️ пульс оркестрации не бьётся: тик давно не обновлялся (35 мин назад)");
  });

  it("сырой null (stale, без фикса) — контрольный пример: если бы stale-ветка рендерила pulse.unhealthy с detail из хранилища, бейдж содержал бы 'null'", () => {
    // last_pulse.detail в stale-сценарии — честный null (dispatch этого тика
    // был успешен, alarm просто давно не тикал) — см. описание pulseStale.
    const rendered = renderPulseUnhealthy({ detail: null, minutes: 35 });
    expect(rendered).toContain("null");
  });
});
