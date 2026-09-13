#!/usr/bin/env python3
"""Тесты scripts/lib/check_result.py (issue #1096).

Запуск: python -m pytest scripts/lib/test_check_result.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import pytest

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
cr = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(cr)  # type: ignore[union-attr]


# ══════════════════════════════════════════════════════════════════════════
# Конструкторы — контракт по значению
# ══════════════════════════════════════════════════════════════════════════

def test_ok_is_clean_with_no_violations_and_no_reason():
    result = cr.ok()
    assert result.status == cr.STATUS_OK
    assert result.violations == []
    assert result.reason is None


def test_violation_carries_items():
    items = [{"pr": 1}, {"pr": 2}]
    result = cr.violation(items)
    assert result.status == cr.STATUS_VIOLATION
    assert result.violations == items
    assert result.reason is None


def test_violation_rejects_empty_list_not_silently_becomes_ok():
    # Пустой список нарушений — это ok(), не "violation с нулём находок":
    # два разных факта в одной форме — тот же класс дефекта, который весь
    # модуль и существует, чтобы не повторить внутри себя.
    with pytest.raises(ValueError):
        cr.violation([])


def test_unknown_carries_reason():
    result = cr.unknown("транспорт gh недоступен: RuntimeError('rate limited')")
    assert result.status == cr.STATUS_UNKNOWN
    assert result.violations == []
    assert "rate limited" in result.reason


def test_unknown_rejects_empty_reason():
    with pytest.raises(ValueError):
        cr.unknown("")


def test_unknown_rejects_whitespace_only_reason():
    with pytest.raises(ValueError):
        cr.unknown("   ")


def test_from_violations_empty_is_ok():
    assert cr.from_violations([]) == cr.ok()


def test_from_violations_nonempty_is_violation():
    assert cr.from_violations([{"x": 1}]) == cr.violation([{"x": 1}])


def test_status_emoji_never_confuses_unknown_with_ok():
    # Критерий готовности 2 (issue #1096): ❓ и 💚 обязаны быть РАЗНЫМИ
    # значками — рендер, слив их в одно, не должен быть возможен по построению.
    assert cr.status_emoji(cr.STATUS_OK) != cr.status_emoji(cr.STATUS_UNKNOWN)
    assert cr.status_emoji(cr.STATUS_OK) == "💚"
    assert cr.status_emoji(cr.STATUS_VIOLATION) == "🚨"
    assert cr.status_emoji(cr.STATUS_UNKNOWN) == "❓"


def test_status_emoji_rejects_unknown_status_value():
    with pytest.raises(ValueError):
        cr.status_emoji("not-a-real-status")


# ══════════════════════════════════════════════════════════════════════════
# scan_silent_except — гвардия регресса (доказательство мутацией внутри
# самого теста: снять unknown() из except — находка появляется)
# ══════════════════════════════════════════════════════════════════════════

_SILENT_SNIPPET = """
def check_something(repo):
    try:
        data = gh(repo)
    except RuntimeError:
        return []
    return data
"""

_FIXED_SNIPPET = """
def check_something(repo):
    try:
        data = gh(repo)
    except RuntimeError as error:
        return check_result.unknown(f"gh недоступен: {error}")
    return data
"""

_UNRELATED_FUNCTION_SNIPPET = """
def fetch_other(repo):
    try:
        data = gh(repo)
    except RuntimeError:
        return []
    return data
"""


def test_scan_silent_except_flags_swallowed_runtimeerror():
    findings = cr.scan_silent_except(_SILENT_SNIPPET, {"check_something"})
    assert len(findings) == 1
    assert findings[0]["function"] == "check_something"


def test_scan_silent_except_mutation_proof_fix_clears_the_finding():
    # Ровно то доказательство мутацией, что требует issue #1096: сняли
    # unknown() (_SILENT_SNIPPET) -> находка есть; вернули unknown()
    # (_FIXED_SNIPPET) -> находок нет.
    assert len(cr.scan_silent_except(_SILENT_SNIPPET, {"check_something"})) == 1
    assert cr.scan_silent_except(_FIXED_SNIPPET, {"check_something"}) == []


def test_scan_silent_except_is_scoped_to_named_functions_only():
    # Функция, не входящая в реестр (ещё не мигрирована — шаг 2), не должна
    # красить гвардию: узкий линт, не общий "чини всё сразу".
    assert cr.scan_silent_except(_UNRELATED_FUNCTION_SNIPPET, {"check_something"}) == []


def test_scan_silent_except_ignores_exceptions_other_than_runtime_or_exception():
    snippet = """
def check_something(repo):
    try:
        data = gh(repo)
    except ValueError:
        return []
    return data
"""
    # ValueError здесь не входит в контракт "транспорт отказал" — не наша
    # находка (узкая форма гвардии, не общий линт по любому except).
    assert cr.scan_silent_except(snippet, {"check_something"}) == []


def test_scan_silent_except_flags_bare_except_too():
    snippet = """
def check_something(repo):
    try:
        data = gh(repo)
    except:
        return []
    return data
"""
    findings = cr.scan_silent_except(snippet, {"check_something"})
    assert len(findings) == 1


def test_scan_silent_except_flags_exception_tuple():
    snippet = """
def check_something(repo):
    try:
        data = gh(repo)
    except (RuntimeError, ValueError):
        return []
    return data
"""
    findings = cr.scan_silent_except(snippet, {"check_something"})
    assert len(findings) == 1


def test_scan_silent_except_accepts_qualified_unknown_call():
    # Вызывающий код может импортировать модуль под любым псевдонимом —
    # гвардия смотрит на имя атрибута ("unknown"), не на буквальное имя
    # модуля check_result (иначе она хрупкая ровно тем классом, что #332).
    snippet = """
def check_something(repo):
    try:
        data = gh(repo)
    except RuntimeError as error:
        return cr_alias.unknown(str(error))
    return data
"""
    assert cr.scan_silent_except(snippet, {"check_something"}) == []
