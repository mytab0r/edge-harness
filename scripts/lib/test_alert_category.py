#!/usr/bin/env python3
"""Тесты реестра категорий сигнала владельцу (#1461).

Сверку трёх языковых копий держит соседний test_alert_category_sync.py — здесь
поведение самого реестра.
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
    "alert_category", Path(__file__).resolve().parent / "alert_category.py")
ac = importlib.util.module_from_spec(_SPEC)
sys.modules["alert_category"] = ac
_SPEC.loader.exec_module(ac)


def test_four_categories_decided_by_the_owner():
    """Набор — решение владельца 2026-09-22, выведенное из замера отправителей.
    Пятая категория не заводится молча: она меняет и порядок тем, и три
    языковые копии."""
    assert ac.CATEGORY_ORDER == ("decision", "breakage", "pipeline", "infra")


def test_owner_decision_comes_first():
    """Порядок значим: от «требует человека» к «к сведению». Он же станет
    порядком тем. Решение владельца, уехавшее вниз, — это ровно та потеря,
    ради которой категории и заводятся."""
    assert ac.CATEGORY_ORDER[0] == ac.OWNER_DECISION


def test_prefix_is_emoji_plus_human_title():
    assert ac.category_prefix(ac.OWNER_DECISION) == "🙋 Решение владельца"
    assert ac.category_prefix(ac.BREAKAGE) == "🔴 Поломка"


def test_unknown_category_is_loud():
    with pytest.raises(ac.UnknownAlertCategory):
        ac.category_prefix("прочее")


def test_prefixes_are_all_distinct():
    """Две категории с одинаковым префиксом читатель не различит, а машина
    различит — расхождение видимого и машинного и есть дефект."""
    prefixes = [ac.category_prefix(name) for name in ac.CATEGORY_ORDER]

    assert len(set(prefixes)) == len(prefixes)
