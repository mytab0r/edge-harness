#!/usr/bin/env python3
"""Три языковые копии реестра категорий не расходятся молча (#1461).

Реестр категорий сигнала живёт в ТРЁХ местах, и это вынужденно:

    scripts/lib/alert_category.py    канон, читают оркестратор и гвардии
    scripts/lib/alert_category.sh    task.sh шлёт отчёт из bash в job'е
    cf-worker/src/config.ts          воркер живёт в Cloudflare, Python ему
                                     физически недоступен

Копия допустима ровно потому, что расхождение красит CI. Без этого теста оно
прошло бы зелёным: юнит-тест каждой стороны прибит к своему литералу и чужой
не читает — тот же класс, за который репозиторий уже заплатил на формате
callback_data решения владельца (#254, находка ревью PR #486,
`test_telegram_callback_format_sync.py`). Этот тест — повтор того приёма, а не
новое изобретение: он ЧИТАЕТ ВСЕ ТРИ ИСХОДНИКА и сверяет их между собой.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import sys

import pytest

_LIB = Path(__file__).resolve().parent
REPO_ROOT = _LIB.parent.parent

_SPEC = importlib.util.spec_from_file_location("alert_category", _LIB / "alert_category.py")
alert_category = importlib.util.module_from_spec(_SPEC)
sys.modules["alert_category"] = alert_category
_SPEC.loader.exec_module(alert_category)

BASH_SOURCE = _LIB / "alert_category.sh"
TS_SOURCE = REPO_ROOT / "cf-worker" / "src" / "config.ts"

# bash: `    decision) printf '🙋 Решение владельца' ;;`
_BASH_CASE_RE = re.compile(r"^\s*([a-z_]+)\)\s*printf '([^']+)'", re.MULTILINE)
# ts:   `  decision: "🙋 Решение владельца",`
_TS_ENTRY_RE = re.compile(r'^\s*([a-z_]+):\s*"([^"]+)",\s*$', re.MULTILINE)


def _python_registry() -> dict[str, str]:
    return {name: alert_category.category_prefix(name)
            for name in alert_category.CATEGORY_ORDER}


def _bash_registry() -> dict[str, str]:
    return dict(_BASH_CASE_RE.findall(BASH_SOURCE.read_text(encoding="utf-8")))


def _ts_registry() -> dict[str, str]:
    text = TS_SOURCE.read_text(encoding="utf-8")
    start = text.index("export const ALERT_CATEGORY = {")
    end = text.index("} as const;", start)
    return dict(_TS_ENTRY_RE.findall(text[start:end]))


def test_all_three_sources_are_actually_parsed():
    """Сначала — что разбор вообще что-то нашёл. Пустой словарь с обеих сторон
    сравнялся бы сам с собой, и тест был бы зелёным и бесполезным: ровно та
    ложная зелень, ради которой он и пишется."""
    assert len(_python_registry()) == 4
    assert len(_bash_registry()) == 4
    assert len(_ts_registry()) == 4


def test_bash_copy_matches_the_python_canon():
    assert _bash_registry() == _python_registry()


def test_ts_copy_matches_the_python_canon():
    assert _ts_registry() == _python_registry()


def test_order_is_the_same_in_python_and_ts():
    """Порядок значим: он задаёт порядок тем, когда включатся треды, и порядок
    в сводке, пока не включились. bash отвечает по одной категории за вызов и
    порядка не несёт — сверять там нечего, и это сказано, а не умолчано."""
    text = TS_SOURCE.read_text(encoding="utf-8")
    start = text.index("export const ALERT_CATEGORY = {")
    end = text.index("} as const;", start)
    ts_order = tuple(name for name, _ in _TS_ENTRY_RE.findall(text[start:end]))

    assert ts_order == alert_category.CATEGORY_ORDER


def test_order_lists_every_declared_category():
    """CATEGORY_ORDER и CATEGORIES — два списка одного набора. Разойтись они
    могут только молча: добавивший категорию в словарь и забывший про порядок
    не увидит ничего, пока не удивится порядку тем."""
    assert set(alert_category.CATEGORY_ORDER) == set(alert_category.CATEGORIES)
    assert len(alert_category.CATEGORY_ORDER) == len(alert_category.CATEGORIES)


def test_every_category_says_what_belongs_in_it():
    """Третье поле реестра — единственное место, где написано, КАК выбирать
    категорию. Пустое описание превращает выбор в угадайку для того, кто
    заводит новую точку отправки."""
    for name, (emoji, title, what) in alert_category.CATEGORIES.items():
        assert emoji.strip(), name
        assert title.strip(), name
        assert len(what.strip()) > 30, f"{name}: описание не объясняет, что сюда относится"


def test_unknown_category_is_loud_not_other():
    """«Прочее» молча — это нынешнее состояние под новым именем."""
    with pytest.raises(alert_category.UnknownAlertCategory) as error:
        alert_category.category_prefix("nope")

    assert "не объявлена" in str(error.value)
    assert "decision" in str(error.value), "сообщение обязано назвать известные категории"


def test_decorate_puts_the_category_on_its_own_line():
    """Отдельной строкой, а не склейкой: тексты сигналов уже начинаются со
    своих эмодзи, и склейка дала бы два эмодзи подряд."""
    decorated = alert_category.decorate(alert_category.BREAKAGE, "🚨 всё пропало")

    assert decorated.splitlines() == ["🔴 Поломка", "🚨 всё пропало"]


def test_bash_helper_refuses_an_unknown_category():
    """Поведенческая проверка настоящего bash-файла, не пересказ его текста:
    вырезанная ветка `*)` прошла бы структурный тест и провалила бы этот."""
    import subprocess

    result = subprocess.run(
        ["bash", "-c", f"source {BASH_SOURCE}; alert_prefix nosuch"],
        capture_output=True, text=True, encoding="utf-8")

    assert result.returncode != 0
    assert "не объявлена" in result.stderr


def test_bash_helper_prints_the_same_prefix_as_python():
    """Тот же приём: не сравнение исходников, а запуск настоящего bash."""
    import subprocess

    for name in alert_category.CATEGORY_ORDER:
        result = subprocess.run(
            ["bash", "-c", f"source {BASH_SOURCE}; alert_prefix {name}"],
            capture_output=True, text=True, encoding="utf-8")

        assert result.returncode == 0, name
        assert result.stdout == alert_category.category_prefix(name), name


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
