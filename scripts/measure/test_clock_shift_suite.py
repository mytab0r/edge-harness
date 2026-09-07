"""Тесты классификатора прогона со сдвинутыми часами (issue #649).

Фикстуры — вербатим срезы РЕАЛЬНОГО вывода `python -m pytest scripts/ -q` на
этом репозитории (не пересказ формата pytest): красный кусок снят живым
прогоном 2026-09-07 на дереве ДО ребейза с фиксом #646 (тест
`test_dispatch_failure_writes_note_not_row` объяснимо падал реальным
`git clone` — тот самый живой случай, который #649 обязана ловить
механически). Зелёный и harness_broken куски — по документированной форме
финальной строки pytest (`N passed`/`N failed .. in Ns`, `no tests ran`).

Запуск: python -m pytest scripts/measure/test_clock_shift_suite.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("clock_shift_suite.py")
spec = importlib.util.spec_from_file_location("clock_shift_suite", SCRIPT)
css = importlib.util.module_from_spec(spec)
spec.loader.exec_module(css)  # type: ignore[union-attr]


# ── Прод-форма: живой красный вывод (снят до ребейза с фиксом #646) ──────────

REAL_RED_OUTPUT = """\
E           fatal: repository 'https://github.com/o/r.git/' not found

scripts\\measure\\dispatch_tail.py:415: RuntimeError
---------------------------- Captured stdout call -----------------------------
Кампания: 10/100 замеров
=========================== short test summary info ===========================
FAILED scripts/measure/test_dispatch_tail.py::test_dispatch_failure_writes_note_not_row
3 failed, 1230 passed in 330.27s (0:05:30)
"""

REAL_GREEN_OUTPUT = "1233 passed in 12.34s\n"

REAL_ZERO_COLLECTED_OUTPUT = "no tests ran in 0.01s\n"

REAL_COLLECTION_ERROR_OUTPUT = """\
ERROR scripts/measure/test_clock_shift_suite.py - ImportError: cannot import name 'css'
!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.42s
"""


def test_parse_collected_total_sums_all_keywords():
    assert css.parse_collected_total(REAL_RED_OUTPUT) == 1233
    assert css.parse_collected_total(REAL_GREEN_OUTPUT) == 1233
    assert css.parse_collected_total(REAL_ZERO_COLLECTED_OUTPUT) == 0


def test_parse_failed_node_ids_extracts_exact_names():
    ids = css.parse_failed_node_ids(REAL_RED_OUTPUT)
    assert ids == ["scripts/measure/test_dispatch_tail.py::test_dispatch_failure_writes_note_not_row"]


def test_classify_result_green():
    result = css.classify_result(0, REAL_GREEN_OUTPUT)
    assert result["outcome"] == "green"
    assert result["collected"] == 1233
    assert result["failed_nodes"] == []


def test_classify_result_red_on_real_bomb_output():
    result = css.classify_result(1, REAL_RED_OUTPUT)
    assert result["outcome"] == "red"
    assert result["collected"] == 1233
    assert "test_dispatch_failure_writes_note_not_row" in result["failed_nodes"][0]


def test_classify_result_harness_broken_on_zero_collected_even_if_exit_zero():
    """Урок 2026-09-07 (задание #649, п.3): 0 собранных тестов — красный
    исход «проверить не удалось», даже если pytest сам вернул бы 0/5."""
    result = css.classify_result(0, REAL_ZERO_COLLECTED_OUTPUT)
    assert result["outcome"] == "harness_broken"
    result5 = css.classify_result(5, REAL_ZERO_COLLECTED_OUTPUT)
    assert result5["outcome"] == "harness_broken"


def test_classify_result_harness_broken_on_collection_error_exit_code():
    """Коды 2..5 (здесь: 1 — но при 0 собранных, реальный код коллекционной
    ошибки pytest — 2) НЕ читаются как «тесты прошли»."""
    result = css.classify_result(2, REAL_COLLECTION_ERROR_OUTPUT)
    assert result["outcome"] == "harness_broken"
    assert "exit=2" in result["detail"]


def test_classify_result_nonzero_exit_with_tests_collected_is_harness_broken_not_red():
    """exit=1 с непустым набором — красный класс; любой ДРУГОЙ ненулевой код
    (например usage error 4 при нормальном сборе — синтетический случай) не
    выдаётся за «красный от тестов», а помечается отдельно."""
    result = css.classify_result(4, REAL_GREEN_OUTPUT)
    assert result["outcome"] == "harness_broken"
    assert "exit=4" in result["detail"]


def test_consequence_message_names_facts_not_internal_state():
    result = css.classify_result(1, REAL_RED_OUTPUT)
    text = css.consequence_message(8, result)
    assert "::error::" in text
    assert "test_dispatch_failure_writes_note_not_row" in text
    assert "8 календарных дней" in text
    assert "#643" in text and "#649" in text


def test_consequence_message_harness_broken_names_horizon_and_reason():
    result = css.classify_result(0, REAL_ZERO_COLLECTED_OUTPUT)
    text = css.consequence_message(40, result)
    assert "+40" in text
    assert "не смог проверить" in text


def test_horizon_days_include_the_mandatory_8_day_threshold():
    """#649: горизонт +8 дней обязателен — переживает MAX_CAMPAIGN_DAYS=7
    (scripts/measure/dispatch_tail.py), ровно порог живого случая 2026-09-07."""
    assert 8 in css.HORIZON_DAYS


def test_list_horizons_cli_prints_single_source_of_truth(capsys):
    assert css.main.__module__ == "clock_shift_suite"
    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["clock_shift_suite.py", "--list-horizons"]
        assert css.main() == 0
    finally:
        _sys.argv = old_argv
    out = capsys.readouterr().out.strip()
    assert out == ",".join(str(d) for d in css.HORIZON_DAYS)
