#!/usr/bin/env python3
"""Гвардия проводки проверки свежести worker-configuration.d.ts (#1419).

Класс: генерат, свежесть которого проверяется ТОЛЬКО после слияния. Живой
случай — bump wrangler 4.129.1 → 4.131.2 (коммит 37f08c3f, PR #1357) привёз
runtime types workerd@1.20260911.1; закоммиченный `worker-configuration.d.ts`
остался от workerd@1.20260831.1. Проверка стояла одна, в `deploy-worker.yml`,
и на PR не запускалась вовсе — PR слился зелёным, а деплой морды падал на
этой самой проверке КАЖДЫЙ раз с 2026-09-13 по 2026-09-21. Красный деплой
ничего не блокирует и никем не читается: девять суток прод не обновлялся.

Что здесь сторожится и почему именно структурно. Тело проверки
(`cf-worker/scripts/check-types-fresh.mjs`) исполняется по-настоящему на
каждом прогоне обоих workflow — настоящий `wrangler types`, настоящий
`git diff`; отдельный стенд под него был бы пересказом внешнего инструмента
(AGENTS.md, «Заглушка внешнего инструмента — это пересказ»). Незащищённой
остаётся ПРОВОДКА: шаг можно удалить из workflow, и тело, каким бы верным
оно ни было, просто перестанет запускаться — ровно то, с чего начался #1419.
Утверждение «во всём репозитории нет второго места вызова этой формы»
проверяется чтением файлов, а не поведением; прецедент той же формы —
`scripts/gh/test_wake_orchestra.py::test_workflows_call_shared_script_not_raw_dispatch`.

Запуск: python -m pytest scripts/lib/test_worker_types_guard.py -q
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# Оба места запуска: ворота на PR и шаг перед деплоем. Удаление любого из них
# возвращает #1419 — первого целиком (устаревший генерат снова сольётся),
# второго частично (деплой перестанет отказываться от устаревшего генерата).
CALLERS = ("worker-ci.yml", "deploy-worker.yml")

GUARD_SCRIPT = REPO_ROOT / "cf-worker" / "scripts" / "check-types-fresh.mjs"
NPM_SCRIPT = "types:check"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_both_workflows_run_the_shared_check():
    for name in CALLERS:
        assert f"npm run {NPM_SCRIPT}" in workflow_text(name), (
            f"{name}: не зовёт `npm run {NPM_SCRIPT}` — свежесть генерата "
            "worker-configuration.d.ts в этом месте больше не проверяется (#1419)"
        )


def test_no_workflow_reinlines_the_check_body():
    """Вторая копия условия — отложенный рецидив: фильтр безобидных отличий
    разойдётся между копиями, и одна из них начнёт врать. Место правды одно —
    cf-worker/scripts/check-types-fresh.mjs."""
    for name in CALLERS:
        text = workflow_text(name)
        assert "npx wrangler types" not in text, (
            f"{name}: тело проверки снова вписано в YAML (`npx wrangler types`) — "
            f"это вторая копия условия мимо {GUARD_SCRIPT.relative_to(REPO_ROOT)}"
        )


def test_npm_script_points_at_the_guard():
    package = json.loads((REPO_ROOT / "cf-worker" / "package.json").read_text(encoding="utf-8"))
    command = package["scripts"].get(NPM_SCRIPT)
    assert command is not None, f"cf-worker/package.json: нет скрипта {NPM_SCRIPT}"
    assert "check-types-fresh.mjs" in command, (
        f"cf-worker/package.json: {NPM_SCRIPT} больше не зовёт check-types-fresh.mjs "
        f"(сейчас: {command!r}) — оба workflow зовут его именно этим именем"
    )
    assert GUARD_SCRIPT.exists(), f"{GUARD_SCRIPT} пропал, а оба workflow его зовут"


def test_guard_keeps_the_dev_vars_step():
    """Имена секретов `wrangler types` читает из .dev.vars. Без этого файла
    генерат теряет секретные биндинги из интерфейса Env — проверка краснеет
    ЛОЖНО, показывая удаление HANDS_TOKEN и соседей на исправном коде. Красный
    стенд на исправном коде опаснее, чем кажется: следующий агент чинит прод по
    ложному сигналу (AGENTS.md, тот же абзац про пересказ)."""
    body = GUARD_SCRIPT.read_text(encoding="utf-8")
    assert ".dev.vars.example" in body and ".dev.vars" in body, (
        "check-types-fresh.mjs перестал заводить .dev.vars — генерат потеряет "
        "секретные биндинги, и гвардия покраснеет на исправном коде"
    )


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
