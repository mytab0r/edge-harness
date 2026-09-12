"""Тесты классификатора прогона со сдвинутыми часами (issue #649).

Фикстуры — срезы РЕАЛЬНОГО вывода `python -m pytest scripts/ -q` на
этом репозитории (не пересказ формата pytest): красный кусок снят живым
прогоном 2026-09-07 на дереве ДО ребейза с фиксом #646 (тест
`test_dispatch_failure_writes_note_not_row` объяснимо падал реальным
`git clone` — тот самый живой случай, который #649 обязана ловить
механически); пути в нём приведены к форме CI-раннера ubuntu-latest
(разделитель `/`) — прод-форма этого прогона, не той платформы, где
снимался срез. Зелёный и harness_broken куски — по документированной форме
финальной строки pytest (`N passed`/`N failed .. in Ns`, `no tests ran`).

Запуск: python -m pytest scripts/measure/test_clock_shift_suite.py -q
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SCRIPT = Path(__file__).with_name("clock_shift_suite.py")
spec = importlib.util.spec_from_file_location("clock_shift_suite", SCRIPT)
css = importlib.util.module_from_spec(spec)
spec.loader.exec_module(css)  # type: ignore[union-attr]

WORKFLOW = SCRIPT.parents[2] / ".github" / "workflows" / "clock-shift-tests.yml"


# ── Прод-форма: живой красный вывод (снят до ребейза с фиксом #646) ──────────

REAL_RED_OUTPUT = """\
E           fatal: repository 'https://github.com/o/r.git/' not found

scripts/measure/dispatch_tail.py:415: RuntimeError
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
    assert "сдвинутых на 8 дней" in text
    # Различение бомбы класса от обычной поломки названо действием (прогон
    # узла без сдвига), не предсказанием будущего — находка второго гейта
    # ревью PR #667: «переживёт N дней и станет обязательным красным» было
    # гипотезой, выданной за факт.
    assert "без CLOCK_SHIFT_DAYS" in text
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


def test_workflow_matrix_stays_in_sync_with_horizon_days():
    """Находка второго гейта ревью PR #667: GitHub Actions не умеет читать
    matrix из внешнего файла на этапе планирования job'ов — буквальный список
    `[1, 8, 40, 400]` в clock-shift-tests.yml был ВТОРЫМ незащищённым местом
    правды горизонтов рядом с HORIZON_DAYS, и рассинхрон (например забыли
    докинуть +40/+400 при правке одного из файлов) не покрасил бы ни один
    существующий чек — тихая потеря горизонта, ровно класс молчаливой
    деградации носителя, против которого весь #649. Этот тест читает workflow
    как данные (не текстом) и сверяет матрицу с HORIZON_DAYS напрямую."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    matrix_horizons = workflow["jobs"]["clock-shift"]["strategy"]["matrix"]["horizon_days"]
    assert matrix_horizons == list(css.HORIZON_DAYS), (
        f"матрица workflow {matrix_horizons} разошлась с HORIZON_DAYS "
        f"{list(css.HORIZON_DAYS)} — второе место правды рассинхронизировано"
    )


# ── Верификация применённого сдвига (находка второго гейта, раунд 2) ─────────

_MARKER_NOW = datetime(2026, 9, 10, 22, 0, tzinfo=timezone.utc)
_MARKER_WINDOW = (_MARKER_NOW, _MARKER_NOW + timedelta(minutes=5))


def _marker_output(days: int = 8, base: datetime = _MARKER_NOW) -> str:
    """Прод-форма маркера наблюдаемости — строка из scripts/conftest.py::
    pytest_configure (печатается только после реального start() freezer'а)."""
    target = (base + timedelta(days=days)).isoformat()
    return f"::notice::CLOCK_SHIFT_DAYS={days} — часы сдвинуты на {target}\n"


def test_green_without_shift_marker_is_harness_broken_not_green():
    """Тот же вывод, что раньше читался как green, но без маркера conftest:
    сдвиг мог молча не примениться (conftest не загружен, freezegun перестал
    патчить) — исход «не доказан», не «зелёный»."""
    result = css.run_verdict(8, 0, REAL_GREEN_OUTPUT, *_MARKER_WINDOW)
    assert result["outcome"] == "harness_broken"
    assert "нет маркера" in result["detail"]
    assert "CLOCK_SHIFT_DAYS" in result["detail"]


def test_red_without_shift_marker_is_harness_broken_too():
    """Красный без доказанного сдвига так же неинтерпретируем: это может быть
    обычная поломка по настоящему времени, а не бомба класса."""
    result = css.run_verdict(8, 1, REAL_RED_OUTPUT, *_MARKER_WINDOW)
    assert result["outcome"] == "harness_broken"


def test_run_verdict_classifies_when_marker_proves_the_shift():
    out = _marker_output() + REAL_GREEN_OUTPUT
    result = css.run_verdict(8, 0, out, *_MARKER_WINDOW)
    assert result["outcome"] == "green"
    assert result["collected"] == 1233

    red_out = _marker_output() + "3 failed, 1230 passed in 330.27s\n"
    result = css.run_verdict(8, 1, red_out, *_MARKER_WINDOW)
    assert result["outcome"] == "red"


def test_run_verdict_rejects_marker_with_foreign_horizon():
    out = _marker_output(days=1) + REAL_GREEN_OUTPUT
    result = css.run_verdict(8, 0, out, *_MARKER_WINDOW)
    assert result["outcome"] == "harness_broken"
    assert "+1д" in result["detail"] and "+8д" in result["detail"]


def test_run_verdict_rejects_marker_base_outside_parent_real_window():
    """Маркер ставит базовое «сейчас» за месяц до реального старта прогона:
    часы шли не от заявленного сдвига — исход не имеет силы."""
    out = _marker_output(base=_MARKER_NOW - timedelta(days=30)) + REAL_GREEN_OUTPUT
    result = css.run_verdict(8, 0, out, *_MARKER_WINDOW)
    assert result["outcome"] == "harness_broken"
    assert "вне окна" in result["detail"]


def test_shift_marker_decided_by_first_occurrence_not_captured_echo():
    """Находка ревью PR #667 (раунд 3, некритичная): stdout УПАВШЕГО теста
    попадает в вывод прогона как captured output и может эхом нести ЧУЖУЮ
    строку маркера (например, отснятую фикстурой от другого прогона).
    Настоящий маркер conftest печатается ПЕРВЫМ (pytest_configure, до любого
    тестового вывода) — решение обязан нести он, не эхо."""
    genuine = _marker_output()
    stale_echo = _marker_output(base=_MARKER_NOW - timedelta(days=30))
    out = genuine + "3 failed, 1230 passed in 330.27s\n" + stale_echo
    result = css.run_verdict(8, 1, out, *_MARKER_WINDOW)
    assert result["outcome"] == "red", (
        "эхо-строка в captured output не должна подменять настоящий маркер "
        "conftest и перевирать вердикт горизонта")

    # Обратный случай: настоящего маркера нет, есть только эхо — эхо не
    # засчитывается доказательством сдвига (база эха вне окна).
    echo_only = "3 failed, 1230 passed in 330.27s\n" + stale_echo
    result = css.run_verdict(8, 1, echo_only, *_MARKER_WINDOW)
    assert result["outcome"] == "harness_broken"
