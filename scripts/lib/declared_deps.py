#!/usr/bin/env python3
"""Обнаружение рассинхрона «тело задачи называет блокирующую issue — нативный
граф её не знает» (задача #371, продолжение #361/task_deps.py).

НЕ источник истины и НИЧЕГО не пишет в граф: `find_desync` — только детектор
для человека/CI, эвристика по формулировкам прозы («после #N», «зависит от
#N», «блокируется #N», «заблокирован(а) #N», «блокер — #N», «до закрытия
#N»). Ложные срабатывания здесь ожидаемы (та же формулировка встречается не
только для реальной межзадачной зависимости — «после слияния» про порядок
шагов внутри одной задачи, не про #N) — именно поэтому находка ЭТОГО модуля
не пишет связь автоматически (в отличие от `file_tasks.py::
wire_declared_dependency`, который переносит СТРУКТУРНУЮ строку «БЛОКИРУЕТСЯ:
…», не любое упоминание в прозе) и не проваливает обязательный CI-гейт —
только видимое предупреждение (`::warning::`), см. `.github/workflows/
repo-ci.yml`. Живой замер при внедрении (2026-09-06): более широкий захват
(окно `\\D{0,20}` без исключения `)`/`.`) давал ложный матч «Поглощает часть
#227 (закрытие задач после слияния) и часть #201» — триггер «после» из
несвязанной скобки цеплял #201 через закрывающую скобку. Окно сужено до
`[^#).\\n]{0,20}` (не пересекает конец скобки/предложения) — тот же прогон
после сужения даёт 0 таких ложных матчей на реальном пуле репозитория.

Ссылка на уже ЗАКРЫТУЮ/несуществующую/не-task issue — НЕ рассинхрон:
приоритет читает только ОТКРЫТЫХ блокирующих (`blocked_by_open`), отсутствие
связи на закрытую задачу не влияет ни на что (design.md task-priority-
blocking-graph, задача #371 report).

CLI:
    python scripts/lib/declared_deps.py check <owner/repo> [label]
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

_TRIGGER_RE = re.compile(
    r"(?:после|зависит\s+от|блокируется|заблокирован[а-я]*|блокер|до\s+закрытия)"
    r"[^#).\n]{0,20}#(\d+)",
    re.IGNORECASE,
)


def declared_candidates(body: str) -> set[int]:
    """Номера issue рядом с формулировкой зависимости — эвристика для
    предупреждения, не факт (см. docstring модуля)."""
    return {int(n) for n in _TRIGGER_RE.findall(body or "")}


def find_desync(issues: list[dict]) -> list[dict]:
    """`issues` — форма `task_deps.fetch_pool(..., include_body=True)`:
    ключи `number`, `body`, `blocked_by_open`. Возвращает находки
    `{"issue": N, "declared_blocking": M}` — тело N называет M формулировкой
    зависимости, M сам открыт и есть в пуле, но нативного `blockedBy` на M
    у issue N нет. Пусто — рассинхрона не обнаружено (легитимный, ожидаемый
    исход большую часть времени: тело либо совпадает с графом, либо ссылка
    на что-то, что не является межзадачной зависимостью вовсе)."""
    open_numbers = {issue["number"] for issue in issues}
    findings: list[dict] = []
    for issue in issues:
        number = issue["number"]
        declared = declared_candidates(issue.get("body") or "")
        native = set(issue.get("blocked_by_open") or [])
        missing = {n for n in declared if n in open_numbers and n != number} - native
        for n in sorted(missing):
            findings.append({"issue": number, "declared_blocking": n})
    return findings


def _load_task_deps():
    spec = importlib.util.spec_from_file_location(
        "task_deps", Path(__file__).resolve().with_name("task_deps.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3) and argv[0] == "check":
        task_deps = _load_task_deps()
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        issues = task_deps.fetch_pool(repo, label=label, include_body=True)
        findings = find_desync(issues)
        if not findings:
            print("declared_deps: рассинхрона не найдено")
            return 0
        for finding in findings:
            print(
                f"::warning::declared_deps: #{finding['issue']} называет в теле "
                f"формулировкой зависимости открытую #{finding['declared_blocking']}, "
                f"но нативного blockedBy на неё нет — проверь и при подтверждении "
                f"поставь `python scripts/lib/task_deps.py block {repo} "
                f"{finding['issue']} {finding['declared_blocking']}`"
            )
        return 0
    print("использование: declared_deps.py check <owner/repo> [label]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
