#!/usr/bin/env python3
"""Тесты apply_owner_decision.py (#254) — job, применяющий нажатие кнопки в
Telegram тем же артефактом, что и ручной ответ владельца (#470/#471).

Запуск: python -m pytest scripts/orchestra/test_apply_owner_decision.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("apply_owner_decision.py")
spec = importlib.util.spec_from_file_location("apply_owner_decision", SCRIPT)
aod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aod)  # type: ignore[union-attr]


def test_decision_comment_first_line_matches_format_470_471():
    # Первая строка — единственный формат, который понимает
    # waiting_owner_guard.py (#470/#471): «РЕШЕНИЕ: N». Мутация «изменить
    # префикс/порядок» красит и этот тест, и симметричный тест на стороне TS
    # (parseOwnerDecisionCallback читает тот же формат в обратную сторону).
    text = aod.decision_comment(2)
    first_line = text.splitlines()[0]
    assert first_line == "РЕШЕНИЕ: 2"


def test_main_posts_comment_via_post_issue_comment_not_a_second_path(monkeypatch):
    calls = []
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: calls.append((repo, issue, text)))
    rc = aod.main(["--repo", "o/r", "--issue", "471", "--option", "2"])
    assert rc == 0
    assert calls == [("o/r", 471, aod.decision_comment(2))]


def test_main_propagates_post_issue_comment_failure_loudly(monkeypatch):
    # Мутация «проглотить RuntimeError» красит этот тест: провал записи решения
    # обязан покрасить job (exit 1 в __main__), а не тихо доложиться success.
    def boom(repo, issue, text):
        raise RuntimeError("gh api упал")

    monkeypatch.setattr(aod, "post_issue_comment", boom)
    with pytest.raises(RuntimeError):
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "1"])
