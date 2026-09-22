#!/usr/bin/env python3
"""Гвардия: `wrangler rollback` не запускается без вердикта канарейки (#1426).

Класс. Шаг автооката раньше срабатывал по условию «job красный И деплой
прошёл» — то есть по ФАКТУ красноты, без единого вопроса о её причине. Живой
случай (прогон 35639422589, `deploy-worker.yml` на `61b185aa`): красной
канарейку сделала исчерпанная суточная квота rows_read Durable Objects
(#1411), а откачен был корректный деплой. Кольцо: фикс квоты не задеплоить,
пока квота исчерпана, — канарейка упрётся в тот же 500 и откатит фикс.

Починить один `if:` значит починить случай. Класс в том, что второй такой
шаг (а он уже есть — `deploy-dsh-edge.yml`) и любой будущий третий
откатывают прод по той же неразличающей логике, и заметить это можно только
прочитав YAML глазами.

Здесь `wrangler rollback` ищется в самих файлах workflow, и каждый найденный
шаг обязан либо спрашивать вердикт канарейки в своём `if:`, либо стоять в
`UNGUARDED_ROLLBACK_WORKFLOWS` с НАЗВАННОЙ причиной и номером задачи.

Запуск:
  python scripts/lib/canary_rollback_guard.py
  python -m pytest scripts/lib/test_canary_rollback_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

_ROLLBACK_RE = re.compile(r"wrangler(?:@[^\s]+)?\s+rollback\b")
# Признак «шаг спрашивает вердикт канарейки»: обращение к output'у с таким
# именем. Имя выхода — контракт между канарейкой и шагом, и он один на все
# workflow: второй синоним означал бы вторую версию того же правила.
VERDICT_OUTPUT = "outputs.verdict"

# Workflow с откатом, СОЗНАТЕЛЬНО оставленные без вердикта: имя файла →
# причина со ссылкой на задачу. Тормоз без газа не принимается (AGENTS.md) —
# запись без причины красит гвардию так же, как и незарегистрированный шаг.
UNGUARDED_ROLLBACK_WORKFLOWS: dict[str, str] = {
    "deploy-dsh-edge.yml":
        "канарейка здесь другая — bash/curl через scripts/lib/canary_http.sh "
        "против ЧУЖОГО воркера dsh-edge: её шаг не издаёт вердикт вовсе, "
        "нечего спрашивать в `if:`; правило #1426 (статика отдаётся + /api/* "
        "5xx = не откатывать) к ней не применено, прод-форма её инфраструктурных "
        "отказов известна ('Exceeded allowed rows read in Durable Objects "
        "free tier' в detail) — отдельная задача #1440",
}


def _strip_shell_comments(script: str) -> str:
    """Тело `run:` без строк-комментариев: рассказ об откате — не откат.
    В `deploy-worker.yml` про `wrangler rollback` написано в пяти
    комментариях, и без вырезания гвардия требовала бы вердикт от шагов,
    которые ничего не откатывают."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def rollback_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[tuple[str, str, str]]:
    """(файл, имя шага, его `if`) для КАЖДОГО шага, реально запускающего
    `wrangler rollback`. `if` пустой — значит условия нет вовсе."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(workflows_dir.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in (job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                if _ROLLBACK_RE.search(_strip_shell_comments(run)):
                    found.append((
                        path.name,
                        str(step.get("name") or step.get("id") or "(без имени)"),
                        str(step.get("if") or ""),
                    ))
    return found


def check_rollback_guarded(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Нарушения (пустой список — гвардия зелёная)."""
    steps = rollback_steps(workflows_dir)
    present = {path.name for path in workflows_dir.glob("*.y*ml")}
    problems: list[str] = []

    for workflow, name, condition in steps:
        if VERDICT_OUTPUT in condition:
            continue
        if workflow in UNGUARDED_ROLLBACK_WORKFLOWS:
            continue
        problems.append(
            f"{workflow}, шаг «{name}»: запускает wrangler rollback, но его "
            f"`if:` не спрашивает вердикт канарейки ({VERDICT_OUTPUT}). Так "
            "был откачен корректный деплой в прогоне 35639422589: красноту "
            "вызвала исчерпанная квота DO, а не код (#1426). Газ: добавь в "
            f"условие `steps.<канарейка>.{VERDICT_OUTPUT} != 'backend-down'`, "
            "либо внеси workflow в UNGUARDED_ROLLBACK_WORKFLOWS с причиной и "
            "номером задачи."
        )

    with_rollback = {workflow for workflow, _, _ in steps}
    for workflow, reason in sorted(UNGUARDED_ROLLBACK_WORKFLOWS.items()):
        if workflow not in present:
            problems.append(
                f"UNGUARDED_ROLLBACK_WORKFLOWS ссылается на {workflow!r}, "
                "которого нет в .github/workflows/ — мёртвая запись")
            continue
        if workflow not in with_rollback:
            problems.append(
                f"{workflow} числится в UNGUARDED_ROLLBACK_WORKFLOWS, но шага "
                "с wrangler rollback в нём нет — либо откат убрали (тогда "
                "убери и запись), либо признак уехал и гвардия перестала его "
                "узнавать; ложно-зелёная гвардия хуже отсутствующей")
        if not (reason or "").strip():
            problems.append(
                f"{workflow} исключён без причины — тормоз без газа: "
                "следующий читатель не узнает, когда исключение снимать")
    return problems


def main() -> int:
    problems = check_rollback_guarded(WORKFLOWS_DIR)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    steps = rollback_steps(WORKFLOWS_DIR)
    guarded = sum(1 for _, _, condition in steps if VERDICT_OUTPUT in condition)
    print(
        f"canary-rollback: {len(steps)} шагов с wrangler rollback, "
        f"{guarded} спрашивают вердикт канарейки, "
        f"{len(UNGUARDED_ROLLBACK_WORKFLOWS)} осознанно исключены"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
