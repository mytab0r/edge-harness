#!/usr/bin/env python3
"""Гвардия «взял не ту функцию task_ref» (#259).

Класс проблемы: task_ref.py давно документирует разделение «источник
задачи PR» (узкая семантика: `resolve_pr_task`/`task_from_branch`/
`declared_tasks`/`declares_task`) и «упоминания» (широкая:
`extract_task_refs`/`references_task`, годится только там, где широта
осознанна и названа в докстринге места вызова). Докстринг НЕ помешал
`ai_review.py` взять `extract_task_refs` для `task_section` — резолюции
задачи PR, где нужна была узкая семантика (живой замер #259: #253 судили по
#120 вместо объявленного #227, #248 — по #119, #247 — по #43, #263 — по #4).

Эта гвардия делает повтор невозможным механически, а не по памяти: любой
новый вызов широких функций task_ref вне явно перечисленных мест —
красный тест. Доказано мутацией (см. README-комментарий в конце файла).

Запуск: python -m pytest scripts/lib/test_task_ref_usage_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Файлы, где широкая семантика ОСОЗНАННА и заявлена в докстринге самого
# места вызова — одно место правды списка исключений, не разбросано по коду.
# Сейчас единственный: scripts/orchestra/scheduler.py — pr_references_issue
# (reap_stale/unhealthy_pulls) и after_merge, см. их докстринги.
ALLOWED_WIDE_USAGE = {
    Path("scripts/orchestra/scheduler.py"),
}

WIDE_CALL_RE = re.compile(r"task_ref\.(extract_task_refs|references_task)\(")


def _production_scripts() -> list[Path]:
    """Все .py в scripts/, кроме тестов — тесты вызывают широкие функции
    напрямую, чтобы проверить их собственное поведение (test_task_ref.py),
    это не то же самое, что взять их для резолюции задачи PR."""
    return [
        path for path in (REPO_ROOT / "scripts").rglob("*.py")
        if not path.name.startswith("test_")
    ]


def test_wide_task_ref_functions_only_used_where_allowed():
    offenders = []
    for path in _production_scripts():
        rel = path.relative_to(REPO_ROOT)
        if rel in ALLOWED_WIDE_USAGE:
            continue
        text = path.read_text(encoding="utf-8")
        if WIDE_CALL_RE.search(text):
            offenders.append(str(rel).replace("\\", "/"))
    assert offenders == [], (
        "task_ref.extract_task_refs/references_task (широкая семантика — "
        "любое упоминание #N) вызваны вне ALLOWED_WIDE_USAGE: "
        f"{offenders}. Для вопроса «какая задача у этого PR» используй "
        "task_ref.resolve_pr_task (#259), а не упоминание в прозе. Если "
        "новое место действительно нуждается в широкой семантике осознанно "
        "(как reap_stale/unhealthy_pulls/after_merge) — назови это в "
        "докстринге места вызова и добавь его в ALLOWED_WIDE_USAGE явно."
    )


# Мутация, которой доказана гвардия (#259): временно верни в task_section
# (scripts/review/ai_review.py) вызов `task_ref.extract_task_refs(pull_body)`
# вместо `task_ref.resolve_pr_task(pull)` — этот тест красный, потому что
# ai_review.py не в ALLOWED_WIDE_USAGE. Верни resolve_pr_task — тест снова
# зелёный.
