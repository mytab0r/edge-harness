#!/usr/bin/env python3
"""Тесты scripts/orchestra/best_effort_outcome_guard.py (#887).

Прод-форма `steps` контекста — форма, которую GitHub Actions реально кладёт
за `toJSON(steps)`: {id: {"outcome": ..., "conclusion": ..., "outputs": {}}}.

Запуск: python -m pytest scripts/orchestra/test_best_effort_outcome_guard.py -q
"""

import importlib.util
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "best_effort_outcome_guard.py"
spec = importlib.util.spec_from_file_location("best_effort_outcome_guard", SCRIPT)
beog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(beog)  # type: ignore[union-attr]


def steps_ctx(**by_id):
    """by_id: id -> outcome (conclusion зашит success — ровно та маскировка,
    которую continue-on-error реально производит)."""
    return {step_id: {"outcome": outcome, "conclusion": "success", "outputs": {}}
            for step_id, outcome in by_id.items()}


def test_find_masked_failures_empty_on_all_success():
    steps = steps_ctx(quota="success", scheduler="success", stale_blocked="success")
    assert beog.find_masked_failures(steps) == []


def test_find_masked_failures_reports_real_outcome_failure():
    # Живая форма (2026-09-10, прогон 34506949025): conclusion — success
    # (замаскирован continue-on-error), outcome — реальный failure.
    steps = steps_ctx(quota="success", stale_blocked="failure", waiting_owner="success")
    assert beog.find_masked_failures(steps) == ["stale_blocked"]


def test_find_masked_failures_multiple_sorted():
    steps = steps_ctx(health_audit="failure", stale_blocked="failure", waiting_owner="success")
    assert beog.find_masked_failures(steps) == ["health_audit", "stale_blocked"]


def test_find_masked_failures_ignores_conclusion_field():
    # conclusion == "failure" тоже покрывается (не только маскированный
    # случай) — этот путь и без гвардии уже красит job, но репорт не должен
    # молчать по нему просто потому, что "неинтересно".
    steps = {"scheduler": {"outcome": "failure", "conclusion": "failure"}}
    assert beog.find_masked_failures(steps) == ["scheduler"]


def test_find_masked_failures_non_dict_input_is_empty():
    assert beog.find_masked_failures([]) == []
    assert beog.find_masked_failures(None) == []


def test_main_escalates_once_per_distinct_set(monkeypatch, capsys):
    from types import SimpleNamespace

    calls = {"escalate": []}

    def fake_escalate_if_new(repo, invariant_id, marker_key, text):
        calls["escalate"].append((repo, invariant_id, marker_key, text))
        return "Telegram: доставлен; след в #120: оставлен"

    monkeypatch.setattr(beog.repo_invariants, "escalate_if_new", fake_escalate_if_new)
    monkeypatch.setenv("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    monkeypatch.setenv("STEPS_JSON", '{"stale_blocked": {"outcome": "failure", "conclusion": "success"}}')

    assert beog.main() == 0
    out = capsys.readouterr().out
    assert "эскалирован" in out
    assert calls["escalate"] == [
        ("mytab0r/edge-harness", "best-effort-outcome", "stale_blocked",
         calls["escalate"][0][3]),
    ]


def test_main_no_failures_is_quiet_success(monkeypatch, capsys):
    monkeypatch.setenv("STEPS_JSON", '{"quota": {"outcome": "success"}}')
    called = []
    monkeypatch.setattr(beog.repo_invariants, "escalate_if_new",
                         lambda *a, **k: called.append(a) or None)
    assert beog.main() == 0
    assert called == []
    assert "реальных провалов не найдено" in capsys.readouterr().out


def test_main_bad_json_fails_loud(monkeypatch, capsys):
    monkeypatch.setenv("STEPS_JSON", "{not json")
    assert beog.main() == 1
    assert "не разобран" in capsys.readouterr().out
