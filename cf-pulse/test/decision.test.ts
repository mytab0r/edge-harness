import assert from "node:assert/strict";
import { test } from "node:test";
import { dshEdgeUpdateDecision } from "../src/decision.ts";
import { DSH_EDGE_UPDATE, GITHUB, PULSE } from "../src/config.ts";

// Чистое решение самообновления морды (#73): все ветки логики без сети.
// Порт cf-worker/test/pulse.spec.ts (#86) — сценарии сохранены, форма та же.
const NOW = 1_800_000_000_000;
const THROTTLE = DSH_EDGE_UPDATE.throttleMs;

test("расхождение версий без истории попыток — dispatch", () => {
  assert.equal(dshEdgeUpdateDecision("0.7.0", "0.8.0", undefined, NOW), "dispatch");
});
test("равенство версий — тихо, даже если попытка была давно", () => {
  assert.equal(dshEdgeUpdateDecision("0.8.0", "0.8.0", NOW - THROTTLE - 1, NOW), "quiet");
});
test("расхождение на свежей попытке — throttled (штурма нет)", () => {
  assert.equal(dshEdgeUpdateDecision("0.8.0", "0.8.1", NOW - 1000, NOW), "throttled");
});
test("граница троттла: ровно throttleMs назад — снова dispatch", () => {
  assert.equal(dshEdgeUpdateDecision("0.8.0", "0.8.1", NOW - THROTTLE, NOW), "dispatch");
});
test("даунгрейд тоже чинится: расхождение в любую сторону — dispatch", () => {
  assert.equal(dshEdgeUpdateDecision("0.8.1", "0.8.0", undefined, NOW), "dispatch");
});

// Константы — контракт с внешними системами; опечатка в имени workflow или URL
// молча ломала бы пульс на проде (деплой зелёный, диспетчей нет). Здесь — в CI.
test("пульс диспетчит ровно те workflow, что живут в репо", () => {
  assert.equal(GITHUB.orchestraWorkflow, "orchestra.yml");
  assert.equal(DSH_EDGE_UPDATE.workflow, "deploy-dsh-edge.yml");
});
test("морда и реестр — прод-адреса", () => {
  assert.equal(DSH_EDGE_UPDATE.healthUrl, "https://dsh-edge.mytab0r.workers.dev/api/health");
  assert.equal(DSH_EDGE_UPDATE.registryUrl, "https://registry.npmjs.org/dsh-edge/latest");
});
test("тик пульса — 15 минут, как у пульса списываемого воркера", () => {
  assert.equal(PULSE.intervalMs, 15 * 60_000);
  assert.equal(PULSE.firstMs < PULSE.intervalMs, true, "первый тик обязан прийти раньше штатного интервала");
});
test("репозиторий диспетча приходит из env, не зашит", async () => {
  // Класс #153: место правды имени репозитория — vars/секреты, код не дублирует.
  const fs = await import("node:fs");
  const src = fs.readFileSync(new URL("../src/index.ts", import.meta.url), "utf8");
  assert.equal(/mytab0r\/edge-harness/.test(src), false, "имя репозитория зашито в коде воркера");
});
