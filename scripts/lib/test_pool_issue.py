#!/usr/bin/env python3
"""Тесты create_pool_issue (#526, #720) — единственная точка программного заведения
issue пула, которая физически не даёт создать issue без `task` и без
машиночитаемого объявления связи.

Мутация, которой доказана проверка метки task: закомментируй `if REQUIRED_LABEL not in
labels: raise ...` в scripts/lib/pool_issue.py — test_missing_task_label_*
перестают падать при отсутствии task и начинают звать fake gh, тест
краснеет (см. test_missing_task_label_never_calls_gh — считает вызовы gh).

Мутация, которой доказана проверка объявления связи (#720): закомментируй
`if not _has_declared_dependency(body): raise ...` — тесты
test_missing_declared_dependency_* перестают падать и начинают звать fake gh,
тест краснеет.

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
    # Тело с валидным объявлением связи («ничем» через структурное поле)
    body = "### Чем блокируется\nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1
    args = gh.calls[0]
    assert args[:3] == ("-X", "POST", "repos/o/r/issues")
    assert "-f" in args and "title=title" in args
    # body передается как один аргумент '-f', 'body=...' — проверяем, что
    # среди аргументов есть строка, начинающаяся с 'body='
    assert any(a.startswith("body=") for a in args)
    assert "labels[]=task" in args


def test_multiple_labels_all_forwarded():
    gh = RecordingGh()
    body = "### Чем блокируется\nничем\n"
    pool_issue.create_pool_issue(gh, "o/r", "t", body, ["task", "white-spot"])
    args = gh.calls[0]
    assert "labels[]=task" in args
    assert "labels[]=white-spot" in args


# ── #720: проверка машиночитаемого объявления связи ────────────────────────

def test_missing_declared_dependency_never_calls_gh():
    """Тело без объявления связи — RuntimeError, gh не вызван."""
    gh = RecordingGh()
    with pytest.raises(RuntimeError, match="машиночитаемого объявления связи"):
        pool_issue.create_pool_issue(gh, "o/r", "title", "просто тело без связи", ["task"])
    assert gh.calls == []


def test_declared_dependency_nichem_structural_field_calls_gh():
    """Структурное поле с ответом 'ничем' — валидно, gh вызван."""
    gh = RecordingGh()
    body = "### Чем блокируется\nничем\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_nichem_inline_calls_gh():
    """Инлайн-строка 'БЛОКИРУЕТСЯ: ничем' — валидно, gh вызван."""
    gh = RecordingGh()
    body = "Какое-то тело\n\nБЛОКИРУЕТСЯ: ничем"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_with_numbers_structural_calls_gh():
    """Структурное поле с номерами — валидно, gh вызван."""
    gh = RecordingGh()
    body = "### Чем блокируется\n#123 #456\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_with_numbers_inline_calls_gh():
    """Инлайн-строка с номерами — валидно, gh вызван."""
    gh = RecordingGh()
    body = "Какое-то тело\n\nБЛОКИРУЕТСЯ: #123 #456"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1


def test_declared_dependency_various_header_levels():
    """Разные уровни заголовка (##, ###, ####) — все валидны."""
    for header in ["## Чем блокируется", "### Чем блокируется", "#### Чем блокируется"]:
        gh = RecordingGh()
        body = f"{header}\nничем\n"
        created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
        assert created["number"] == 999
        assert len(gh.calls) == 1


def test_declared_dependency_empty_answer_valid():
    """Пустой ответ после заголовка — валидно (как 'ничем')."""
    gh = RecordingGh()
    body = "### Чем блокируется\n\n"
    created = pool_issue.create_pool_issue(gh, "o/r", "title", body, ["task"])
    assert created["number"] == 999
    assert len(gh.calls) == 1
