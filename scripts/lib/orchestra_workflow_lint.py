#!/usr/bin/env python3
"""Гвардия класса «continue-on-error шаг без id остаётся невидимым» (#887).

`scripts/orchestra/best_effort_outcome_guard.py` читает `${{ toJSON(steps) }}`
в конце job `orchestra`, чтобы поймать РЕАЛЬНЫЙ провал `continue-on-error`
шага (Jobs API после завершения прогона отдаёт только замаскированный
`conclusion`, см. докстринг того скрипта). GitHub Actions кладёт в контекст
`steps` только шаги с явным `id:` — шаг без `id` физически не адресуем, и
`toJSON(steps)` его не покажет ни при каком провале. Значит: любой
`continue-on-error: true` шаг job `orchestra` БЕЗ `id:` — снова невидимый
провал тем же способом, каким уже был живой инцидент 2026-09-10 (прогон
34506949025, шаг «Гвардия протухшей метки blocked»).

`continue_on_error_steps_without_id` — чистая функция без IO: принимает уже
распарсенный YAML-документ workflow (`yaml.safe_load`), возвращает имена
шагов job `orchestra`, у которых `continue-on-error: true`, но нет `id`.
Пустой список — здоровое состояние.

Запуск:
  python scripts/lib/orchestra_workflow_lint.py         # печать отчёта, exit 1 при нарушении
  python -m pytest scripts/lib/test_orchestra_workflow_lint.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRA_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "orchestra.yml"

# Имя job, чей `steps` реально читает best_effort_outcome_guard.py — узкий
# охват НАРОЧНО (см. докстринг модуля): только у этого job есть механизм,
# которому нужен id каждого continue-on-error шага. Другие workflow
# (ai-review.yml/pr-review.yml/plugin-forge.yml/deploy-dsh-edge.yml)
# используют continue-on-error для другого класса задач (best-effort
# уведомления не про этот же механизм) — расширять охват по факту нового
# outcome-guard'а, не заранее.
WATCHED_JOB = "orchestra"


def continue_on_error_steps_without_id(doc: dict) -> list[str]:
    """doc — распарсенный YAML всего workflow-файла. `on:` в YAML 1.1
    иногда сворачивается в булев ключ `True` парсером — эта функция его не
    трогает, ей нужен только `jobs.<WATCHED_JOB>.steps`."""
    jobs = (doc or {}).get("jobs") or {}
    job = jobs.get(WATCHED_JOB) or {}
    steps = job.get("steps") or []
    violations = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("continue-on-error") is True and not step.get("id"):
            violations.append(step.get("name") or f"шаг #{index}")
    return violations


def main() -> int:
    if not ORCHESTRA_WORKFLOW_PATH.is_file():
        print(f"::error::orchestra_workflow_lint: файл не найден: {ORCHESTRA_WORKFLOW_PATH}")
        return 1
    doc = yaml.safe_load(ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8"))
    violations = continue_on_error_steps_without_id(doc)
    if not violations:
        print(f"💚 все continue-on-error шаги job '{WATCHED_JOB}' имеют id")
        return 0
    for name in violations:
        print(
            f"::error::orchestra.yml: continue-on-error шаг «{name}» без id — "
            "best_effort_outcome_guard.py его не увидит в toJSON(steps), "
            "реальный провал снова станет невидимым (#887)"
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
