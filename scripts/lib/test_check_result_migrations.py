#!/usr/bin/env python3
"""Гвардия регресса носителя третьего состояния (issue #1096) на уже
мигрированных функциях `scripts/orchestra/repo_invariants.py`.

Две проверки, не одна (AGENTS.md, «Поведенческий тест находит то, чего
структурный не видит», #891/#893):

1. Структурная (`test_migrated_functions_have_no_silent_except_swallow`) —
   дешёвая, без импорта модуля: AST-скан `check_result.scan_silent_except`
   по реестру `MIGRATED_REPO_INVARIANTS_FUNCTIONS` ниже. Реестр — данные
   ЭТОГО теста (не константа `check_result.py`, у каждого потребителя свой
   список мигрированных функций), расширяется задачей шага 2 (issue #1096)
   по мере миграции остальных инвариантов.
2. Поведенческая (`test_*_is_unknown_on_dead_transport`) — реально вызывает
   мигрированную функцию с транспортом `gh`, кидающим RuntimeError на КАЖДЫЙ
   вызов, и проверяет `result.status == STATUS_UNKNOWN`. Структурная гвардия
   могла бы остаться зелёной, даже если `unknown(...)` вызывается, но
   результат по ошибке не возвращается вызывающему (баг в другом месте той
   же функции) — только поведенческий прогон реально исполняет код.

Мутация, которую обе гвардии обязаны ловить: снять `unknown(...)` внутри
`except RuntimeError` одной из мигрированных функций, оставив `return []` —
структурная краснеет немедленно (без сети), поведенческая краснеет при
прогоне (транспорт мёртв, но функция молча вернула ok()).

Запуск: python -m pytest scripts/lib/test_check_result_migrations.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys
from datetime import datetime, timezone

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRA_DIR = _REPO_ROOT / "scripts" / "orchestra"
# scheduler.py (импортируется repo_invariants.py) делает `from pulse_guard
# import (...)`  — относительный импорт по имени модуля, не по атрибуту,
# требует scripts/orchestra в sys.path (тот же приём, каким pytest сам
# добавляет директорию test_repo_invariants.py при обычном запуске оттуда;
# этот файл лежит в scripts/lib/, директорию нужно добавить явно).
if str(_ORCHESTRA_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRA_DIR))

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
cr = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(cr)  # type: ignore[union-attr]

_RI_SPEC = importlib.util.spec_from_file_location(
    "repo_invariants", _ORCHESTRA_DIR / "repo_invariants.py")
ri = importlib.util.module_from_spec(_RI_SPEC)
_RI_SPEC.loader.exec_module(ri)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# Реестр функций repo_invariants.py, мигрированных на CheckResult (issue
# #1096, шаг 1 — 13/14; issue #1109, шаг 2, пачка 1 — 8/10). Расширяется
# следующими пачками по мере миграции остальных инвариантов — не трогать этот
# файл ради миграции без обновления списка, иначе регресс новой функции эта
# гвардия не увидит.
MIGRATED_REPO_INVARIANTS_FUNCTIONS = {
    "check_conveyor_gate_phantom_pause",
    "check_worker_false_success_comment",
    "check_wasted_ai_review_runs",
    "check_recurring_worker_failure",
}


def test_migrated_functions_have_no_silent_except_swallow():
    source = (_ORCHESTRA_DIR / "repo_invariants.py").read_text(encoding="utf-8")
    findings = cr.scan_silent_except(source, MIGRATED_REPO_INVARIANTS_FUNCTIONS)
    assert findings == [], (
        f"регресс класса #1096 — except без unknown() в уже мигрированной "
        f"функции repo_invariants.py: {findings}"
    )


class DeadTransport:
    """`gh()`, кидающий RuntimeError на КАЖДЫЙ вызов — то самое «мёртвый
    транспорт» из критерия готовности issue #1096: мигрированные функции
    обязаны вернуть unknown(), не тихий []/ok()."""

    def __call__(self, *args, **kwargs):
        raise RuntimeError("dead transport (test double, issue #1096)")


def test_check_conveyor_gate_phantom_pause_is_unknown_on_dead_transport(monkeypatch):
    # recent_runs/issue_markers_any вызывают pulse_guard.gh изнутри
    # pulse_guard.py — подменяем атрибут МОДУЛЯ pulse_guard, не локальную
    # копию имени в repo_invariants.py (та уже связана со старой функцией
    # на момент импорта — см. докстринг файла).
    monkeypatch.setattr(ri.pulse_guard, "gh", DeadTransport())
    result = ri.check_conveyor_gate_phantom_pause(REPO, utc(2026, 9, 13, 0, 0))
    assert result.status == cr.STATUS_UNKNOWN
    assert result.reason  # причина непуста и человекочитаема (см. unknown())


def test_check_worker_false_success_comment_is_unknown_on_dead_transport(monkeypatch):
    # check_worker_false_success_comment зовёт `gh(...)` напрямую — имя
    # связано в МОДУЛЕ repo_invariants (`gh = pulse_guard.gh`), подменяем
    # именно там.
    monkeypatch.setattr(ri, "gh", DeadTransport())
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == cr.STATUS_UNKNOWN
    assert result.reason


def test_check_wasted_ai_review_runs_is_unknown_on_dead_transport(monkeypatch):
    # check_wasted_ai_review_runs зовёт review_labels.latest_ai_comment(repo,
    # number, gh) с `gh`, взятым из МОДУЛЯ repo_invariants — подменяем ri.gh.
    monkeypatch.setattr(ri, "gh", DeadTransport())
    pull = {"number": 333, "labels": [{"name": "ai:ok"}]}
    result = ri.check_wasted_ai_review_runs(REPO, [pull])
    assert result.status == cr.STATUS_UNKNOWN
    assert result.reason


def test_check_recurring_worker_failure_is_unknown_on_dead_transport(monkeypatch):
    # check_recurring_worker_failure зовёт pulse_guard.recent_runs, которая
    # зовёт pulse_guard.gh изнутри pulse_guard.py — подменяем атрибут МОДУЛЯ.
    monkeypatch.setattr(ri.pulse_guard, "gh", DeadTransport())
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == cr.STATUS_UNKNOWN
    assert result.reason
