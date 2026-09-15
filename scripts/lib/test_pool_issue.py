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
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", ["white-spot"], producer="x")
    assert gh.calls == []


def test_empty_labels_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="task"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "body", [], producer="x")
    assert gh.calls == []


def test_task_label_present_calls_gh_with_post_issues():
    gh = RecordingGh()
    created = pool_issue.create_pool_issue(
        gh, "o/r", "title", "body text", ["task"], producer="stall-detector")
    assert created["number"] == 999
    assert len(gh.calls) == 1
    args = gh.calls[0]
    assert args[:3] == ("-X", "POST", "repos/o/r/issues")
    assert "-f" in args and "title=title" in args
    assert "labels[]=task" in args
    body_arg = next(a for a in args if a.startswith("body="))
    assert body_arg == (
        "body=<!-- pool-issue-producer: stall-detector -->\nbody text")


def test_multiple_labels_all_forwarded():
    gh = RecordingGh()
    pool_issue.create_pool_issue(gh, "o/r", "t", "b", ["task", "white-spot"], producer="review-findings")
    args = gh.calls[0]
    assert "labels[]=task" in args
    assert "labels[]=white-spot" in args


# ── Маркер производителя (issue #1277) ──────────────────────────────────────

def test_missing_producer_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="producer"):
        pool_issue.create_pool_issue(gh, "o/r", "t", "b", ["task"], producer="")
    assert gh.calls == []


def test_invalid_producer_never_calls_gh():
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="producer"):
        pool_issue.create_pool_issue(gh, "o/r", "t", "b", ["task"], producer="Not Valid!")
    assert gh.calls == []


def test_producer_marker_prepended_to_body():
    gh = RecordingGh()
    pool_issue.create_pool_issue(gh, "o/r", "t", "исходное тело", ["task"], producer="upstream-drift")
    args = gh.calls[0]
    body_arg = next(a for a in args if a.startswith("body="))
    assert body_arg == "body=<!-- pool-issue-producer: upstream-drift -->\nисходное тело"


def test_extract_producer_round_trips():
    body = f"{pool_issue.producer_marker('failure-watch')}\nостальное тело\nещё строка"
    assert pool_issue.extract_producer(body) == "failure-watch"


def test_extract_producer_none_when_absent():
    assert pool_issue.extract_producer("обычное тело без маркера") is None
    assert pool_issue.extract_producer(None) is None
    assert pool_issue.extract_producer("") is None


def test_extract_producer_ignores_manually_edited_lookalike_text():
    # Честная граница (докстринг модуля): текст, ПОХОЖИЙ на маркер, но не
    # являющийся HTML-комментарием, не распознаётся — только точная форма,
    # которую сама create_pool_issue пишет.
    assert pool_issue.extract_producer("pool-issue-producer: failure-watch (без <!-- -->)") is None
