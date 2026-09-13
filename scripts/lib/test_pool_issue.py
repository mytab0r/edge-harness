#!/usr/bin/env python3
"""Тесты create_pool_issue (#526) — единственная точка программного заведения
issue пула, которая физически не даёт создать issue без `task`.

Мутация, которой доказана проверка: закомментируй `if REQUIRED_LABEL not in
labels: raise ...` в scripts/lib/pool_issue.py — test_missing_task_label_*
перестают падать при отсутствии task и начинают звать fake gh, тест
краснеет (см. test_missing_task_label_never_calls_gh — считает вызовы gh).

Запуск: python -m pytest scripts/lib/test_pool_issue.py -q
"""

import importlib.util
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "pool_issue.py"
spec = importlib.util.spec_from_file_location("pool_issue", SCRIPT)
pool_issue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool_issue)  # type: ignore[union-attr]


class RecordingGh:
    """Фейковый gh(*args): считает вызовы и отдаёт прод-форму ответа
    POST .../issues (число + html_url), без реальной сети."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        return {"number": 999, "html_url": "https://github.com/o/r/issues/999"}


def test_missing_task_label_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="task"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", ["white-spot"])
    assert gh.calls == []


def test_empty_labels_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="task"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", [])
    assert gh.calls == []


def test_task_label_present_calls_gh_with_post_issues():
    gh = RecordingGh()
    created = pool_issue.create_pool_issue(gh, "o/r", "title", "body text", ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1
    args = gh.calls[0]
    assert args[:3] == ("-X", "POST", "repos/o/r/issues")
    assert "-f" in args and "title=title" in args
    assert "body=body text" in args
    assert "labels[]=task" in args


def test_multiple_labels_all_forwarded():
    gh = RecordingGh()
    pool_issue.create_pool_issue(gh, "o/r", "t", "b", ["task", "white-spot"])
    args = gh.calls[0]
    assert "labels[]=task" in args
    assert "labels[]=white-spot" in args
