#!/usr/bin/env node
// Гвардия свежести worker-configuration.d.ts. Запуск: npm run types:check.
//
// Класс, оплаченный инцидентом #1419: сгенерированный файл, свежесть которого
// проверялась ТОЛЬКО в deploy-worker.yml, то есть ПОСЛЕ слияния. Dependabot
// поднял wrangler 4.129.1 → 4.131.2 (коммит 37f08c3f, PR #1357), вместе с ним
// приехали runtime types workerd@1.20260911.1, а закоммиченный генерат остался
// от workerd@1.20260831.1. PR слился зелёным (эта проверка на PR не стояла), а
// каждый прогон деплоя с 2026-09-13 падал на ней — девять суток прод-морда не
// обновлялась вовсе, и увидеть это можно было только вручную, открыв Actions.
//
// Носитель решения — не текст, а место запуска: тот же скрипт зовут ОБА
// workflow, и на PR (worker-ci.yml, job worker-test — обязательная проверка),
// и на деплое (deploy-worker.yml). Одно место правды: фильтр заведомо
// безобидных отличий живёт здесь один раз, а не двумя копиями в YAML.
//
// Почему фильтр вообще нужен: строка `hash:` в шапке генерата зависит от
// окружения (путь, версия), а комментарий про секреты в новых версиях
// wrangler отсутствует — эти отличия не означают устаревания. Всё остальное
// расхождение означает.
//
// Имена секретов wrangler types читает из .dev.vars (важны КЛЮЧИ, не
// значения) — без этого файла генерат молча теряет секретные биндинги из
// интерфейса Env, и гвардия падает ложно, показывая удаление HANDS_TOKEN и
// соседей. Поэтому .dev.vars создаётся из .dev.vars.example, если его нет,
// и удаляется после генерации, если создан здесь (окружение шага остаётся
// как найдено).

import { copyFileSync, existsSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { execFileSync } from "node:child_process";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const GENERATED = "worker-configuration.d.ts";

// Отличия, которые НЕ означают устаревания (см. шапку). Паттерны применяются к
// строкам diff'а, поэтому учитывают ведущие -/+.
//
// hash-фильтр — форма ШАПКИ генерата `(hash: ...)`, не любой `hash:`:
// в теле генерата живут одноимённые члены интерфейсов (`hash:
// CryptoKeyKeyAlgorithm`, `hash: URLPatternComponentResult`), и широкий
// `.*hash: ` проглотил бы содержательную правку вида `+ hash: string;` молча
// (находка ai-ревью PR #1420, чеклист).
const BENIGN = [
  /^[-+].*\(hash: /,
  /^[-+].*Secrets \(HANDS_TOKEN/,
  /^[-+].*they are set via/,
  /^--- /,
  /^\+\+\+ /,
];

// Имена секретов wrangler types читает из .dev.vars (важны КЛЮЧИ, не значения).
// Файл заводится здесь ТОЛЬКО если его не было, и удаляется после генерации:
// шаг не должен менять окружение соседних шагов job'а (находка ai-ревью
// PR #1420, чеклист — непустой .dev.vars красил vitest-тесты на исправном коде).
const devVars = join(root, ".dev.vars");
let createdDevVars = false;
if (!existsSync(devVars)) {
  copyFileSync(join(root, ".dev.vars.example"), devVars);
  createdDevVars = true;
}

try {
  execFileSync("npx", ["wrangler", "types"], { cwd: root, stdio: "inherit" });
} finally {
  if (createdDevVars) rmSync(devVars);
}

const diff = execFileSync("git", ["diff", "--", GENERATED], { cwd: root, encoding: "utf8" });
const meaningful = diff
  .split("\n")
  .filter((line) => /^[+-]/.test(line))
  .filter((line) => !BENIGN.some((re) => re.test(line)));

if (meaningful.length === 0) process.exit(0);

console.error(diff.split("\n").slice(0, 40).join("\n"));
console.error(`::error::${GENERATED} устарел: запусти 'npm run types' и закоммить.`);
process.exit(1);
