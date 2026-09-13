#!/usr/bin/env python3
"""Гвардия «взял не ту функцию task_ref» (#259).

Класс проблемы: task_ref.py давно документирует разделение «источник
задачи PR» (узкая семантика: `resolve_pr_task`/`task_from_branch`) и
«упоминания» (широкая:
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
# scripts/orchestra/scheduler.py — pr_references_issue (reap_stale),
# after_merge и partial_disclaimer, см. их докстринги. unhealthy_pulls
# (#286) сюда больше НЕ входит — сопоставление PR → задача там сузилось до task_ref.resolve_pr_task
# (не «не тронули вовремя», а «сняли аренду ЧУЖОЙ задачи» — цена той же
# широты там другая, см. докстринг unhealthy_pulls). scripts/orchestra/
# repo_invariants.py — declared_change_task (инвариант 4, второй путь
# завершённости, docs/agents/OPENSPEC-PROTOCOL.md): другой вопрос, чем
# «какая задача у этого PR» (резолюция PR из #259 сюда не относится вовсе,
# это proposal.md openspec change) — но всё равно широкая семантика
# `#N`-паттерна, поэтому назван здесь явно. Безопасно уже потому, что вход
# ОГРАНИЧЕН абзацем-декларацией («Задач…» первой строкой, см. докстринг
# declared_change_task), а не всем файлом — ровно та защита, которой не
# хватало `ai_review.py::task_section` в живом случае #259.
#
# Честно про границу этого списка (#699 code review): узость unhealthy_pulls
# выше — заявление в комментарии и в докстринге функции, эта гвардия его
# НЕ проверяет и проверить не может. WIDE_CALL_RE ловит только прямой вызов
# task_ref.extract_task_refs/references_task; unhealthy_pulls, вернись она к
# широкой семантике через локальную обёртку pr_references_issue (тоже в
# scheduler.py), останется невидима гвардии — весь файл scheduler.py в
# ALLOWED_WIDE_USAGE целиком, а не построчно (доказано мутацией при ревью
# PR #699: возврат pr_references_issue в unhealthy_pulls тест
# test_wide_task_ref_functions_only_used_where_allowed НЕ красит). Носитель
# правила «unhealthy_pulls узкая» — три юнит-теста в
# scripts/orchestra/test_scheduler.py (see docstring unhealthy_pulls),
# не эта гвардия. Пофайловый механизм здесь не заводим — отдельная работа.
ALLOWED_WIDE_USAGE = {
    Path("scripts/orchestra/scheduler.py"),
    Path("scripts/orchestra/repo_invariants.py"),
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
        "(как reap_stale/after_merge/partial_disclaimer в scheduler.py или "
        "declared_change_task в repo_invariants.py) — назови это в "
        "докстринге места вызова и добавь его в ALLOWED_WIDE_USAGE явно."
    )


# Мутация, которой доказана гвардия (#259): временно верни в task_section
# (scripts/review/ai_review.py) вызов `task_ref.extract_task_refs(pull_body)`
# вместо `task_ref.resolve_pr_task(pull)` — этот тест красный, потому что
# ai_review.py не в ALLOWED_WIDE_USAGE. Верни resolve_pr_task — тест снова
# зелёный.
