#!/usr/bin/env python3
"""Тесты scripts/lib/orchestra_workflow_lint.py (#887).

Запуск: python -m pytest scripts/lib/test_orchestra_workflow_lint.py -q
"""

import importlib.util
from pathlib import Path

import yaml

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "orchestra_workflow_lint.py"
spec = importlib.util.spec_from_file_location("orchestra_workflow_lint", SCRIPT)
owl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owl)  # type: ignore[union-attr]


def doc_with_steps(steps):
    return {"jobs": {"orchestra": {"steps": steps}}}


def test_step_with_continue_on_error_and_id_is_healthy():
    doc = doc_with_steps([
        {"name": "Гвардия X", "id": "guard_x", "continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_step_with_continue_on_error_without_id_is_a_violation():
    doc = doc_with_steps([
        {"name": "Гвардия протухшей метки blocked (#334)", "continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == ["Гвардия протухшей метки blocked (#334)"]


def test_step_without_continue_on_error_never_flagged():
    doc = doc_with_steps([
        {"name": "Обычный шаг", "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_unnamed_step_uses_positional_fallback():
    doc = doc_with_steps([
        {"continue-on-error": True, "run": "true"},
    ])
    assert owl.continue_on_error_steps_without_id(doc) == ["шаг #0"]


def test_other_job_is_not_scanned():
    # WATCHED_JOB — только 'orchestra': best_effort_outcome_guard.py читает
    # steps ИМЕННО этого job, не 'contract'.
    doc = {"jobs": {"contract": {"steps": [
        {"name": "Что угодно", "continue-on-error": True, "run": "true"},
    ]}}}
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_empty_doc_is_healthy():
    assert owl.continue_on_error_steps_without_id({}) == []
    assert owl.continue_on_error_steps_without_id(None) == []


def test_live_orchestra_workflow_has_no_violations():
    """Живой прогон на настоящем .github/workflows/orchestra.yml — доказывает,
    что фикс (#887: добавлены id ко всем continue-on-error шагам) реально
    применён, не только описан в тесте на синтетике."""
    doc = yaml.safe_load(owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert owl.continue_on_error_steps_without_id(doc) == []


def test_mutation_guard_missing_id_in_live_file_would_be_caught(tmp_path):
    """Мутация: снимаем id с реального шага живого файла (текстом, не через
    YAML round-trip — сохраняет форматирование) — гвардия обязана покраснеть."""
    text = owl.ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8")
    mutated = text.replace("        id: stale_blocked\n", "", 1)
    assert mutated != text, "фикстура мутации не нашла строку — тест устарел вместе с файлом"
    doc = yaml.safe_load(mutated)
    violations = owl.continue_on_error_steps_without_id(doc)
    assert violations, "снятие id с continue-on-error шага обязано быть найдено"
