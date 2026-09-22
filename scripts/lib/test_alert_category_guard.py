#!/usr/bin/env python3
"""Тесты гвардии «сигнал без категории» (#1461).

Поведенческие: каждый сценарий — настоящий .py-файл на диске, который гвардия
разбирает своим обычным путём (AGENTS.md, #891/#893).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "alert_category_guard", Path(__file__).resolve().parent / "alert_category_guard.py")
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["alert_category_guard"] = guard
_SPEC.loader.exec_module(guard)


def _repo(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / "repo"
    (root / "scripts" / "orchestra").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "orchestra" / name).write_text(source, encoding="utf-8")
    return root


def test_call_without_category_is_found(tmp_path):
    root = _repo(tmp_path, "sender.py", "send_telegram('текст')\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "sender.py:1" in problems[0]
    assert "alert_category" in problems[0], "сообщение обязано назвать, где взять категорию"


def test_call_with_category_passes(tmp_path):
    root = _repo(tmp_path, "sender.py", "send_telegram('текст', category='breakage')\n")

    assert guard.check(root) == []


def test_multiline_call_with_category_below_is_not_a_false_positive(tmp_path):
    """Урок #1438, оплаченный в этой же сессии: текстовый поиск `send_telegram(`
    видит только первую строку и назвал бы этот вызов нарушением. AST видит
    вызов целиком — именно поэтому разбор здесь AST, а не регулярка."""
    root = _repo(tmp_path, "sender.py", """
send_telegram(
    build_text(),
    as_html=True,
    category='pipeline',
)
""")

    assert guard.check(root) == []


def test_multiline_call_without_category_is_still_found(tmp_path):
    """Обратная сторона той же монеты: перенос строк не должен ПРЯТАТЬ
    нарушение. Гвардия, ловящая только однострочные вызовы, ложно-зелёная."""
    root = _repo(tmp_path, "sender.py", """
send_telegram(
    build_text(),
    as_html=True,
)
""")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "sender.py:2" in problems[0]


def test_attribute_call_is_seen_too(tmp_path):
    """Отправитель зовут и как `pulse_guard.send_telegram(...)` — через
    атрибут модуля. Проверка только по голому имени пропустила бы весь
    scheduler.py, где он вызывается именно так."""
    root = _repo(tmp_path, "caller.py", "pulse_guard.send_telegram('текст')\n")

    assert len(guard.check(root)) == 1


def test_the_sender_definition_itself_is_not_an_offender(tmp_path):
    """`def send_telegram(...)` — не вызов. Считать его нарушением значило бы
    штрафовать файл, который эту возможность и предоставляет."""
    root = _repo(tmp_path, "sender.py",
                 "def send_telegram(text, *, category):\n    return True\n")

    assert guard.check(root) == []


def test_tests_are_not_scanned(tmp_path):
    """Тесты зовут отправитель со стендами, владельцу они ничего не шлют.
    Требовать категорию там — тормоз без причины."""
    root = tmp_path / "repo"
    (root / "scripts" / "orchestra").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "orchestra" / "test_x.py").write_text(
        "send_telegram('текст')\n", encoding="utf-8")

    assert guard.check(root) == []


def test_live_repository_is_clean():
    """На живом дереве гвардия обязана быть зелёной: все девять замеренных
    точек отправки несут категорию."""
    assert guard.check() == []


# ── значение аргумента, а не только его наличие (#1461, живая ошибка автора) ──


def test_unresolvable_attribute_chain_is_found(tmp_path):
    """Живая ошибка автора в этом же PR: `category=` на месте, аргумент есть,
    гвардия молчала — а имя `pulse_guard` в тот модуль не импортировано, и
    упало NameError'ом в рантайме. Проверять наличие аргумента мало."""
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=pulse_guard.alert_category.BREAKAGE)\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "не на реестр" in problems[0]


def test_registry_constant_passes(tmp_path):
    root = _repo(tmp_path, "caller.py",
                 "send_telegram('текст', category=alert_category.BREAKAGE)\n")

    assert guard.check(root) == []


def test_known_string_literal_passes(tmp_path):
    """Строка допустима — но только объявленная: реестр читается по-настоящему,
    а не сверяется с «похоже на категорию»."""
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category='pipeline')\n")

    assert guard.check(root) == []


def test_unknown_string_literal_is_found(tmp_path):
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category='прочее')\n")

    problems = guard.check(root)

    assert len(problems) == 1
    assert "не объявлена" in problems[0]


def test_runtime_computed_value_is_found(tmp_path):
    """Переменная или вызов статически не разрешаются. Пропустить их молча
    значило бы оставить ровно ту дверь, через которую ошибка автора и вошла."""
    root = _repo(tmp_path, "caller.py", "send_telegram('текст', category=pick())\n")

    assert len(guard.check(root)) == 1
