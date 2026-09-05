#!/usr/bin/env python3
"""Тесты чистой логики замера rows_read (scripts/measure/do_rows_read.py, #320).

Сеть не трогаем: кормим функции прод-формой ответа GraphQL introspection и
данных (структуры вложенных объектов ровно как их отдаёт API Cloudflare —
`__type`, `dimensions`, `sum`), проверяем разбор и арифметику.

Запуск: python -m pytest scripts/measure/test_do_rows_read.py -q
"""

import importlib.util
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("do_rows_read.py")
spec = importlib.util.spec_from_file_location("do_rows_read", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]


# ── unwrap_type_name: разворот NON_NULL/LIST до named type ────────────────────────


def test_unwrap_type_name_plain():
    assert mod.unwrap_type_name({"name": "Foo", "kind": "OBJECT"}) == "Foo"


def test_unwrap_type_name_non_null_list():
    # [Foo!]! — как GraphQL introspection реально заворачивает поля датасетов
    wrapped = {
        "name": None, "kind": "NON_NULL",
        "ofType": {"name": None, "kind": "LIST",
                   "ofType": {"name": "Foo", "kind": "OBJECT", "ofType": None}},
    }
    assert mod.unwrap_type_name(wrapped) == "Foo"


def test_unwrap_type_name_none():
    assert mod.unwrap_type_name(None) is None


def test_unwrap_type_name_empty_string_name_on_wrapper():
    # Живая схема Cloudflare (прогон #320, 2026-09-05): NON_NULL/LIST отдают
    # name="" вместо null — разворот обязан продолжаться, а не считать это именем.
    wrapped = {
        "name": "", "kind": "NON_NULL",
        "ofType": {"name": "", "kind": "LIST",
                   "ofType": {"name": "AccountsFilterable", "kind": "OBJECT", "ofType": None}},
    }
    assert mod.unwrap_type_name(wrapped) == "AccountsFilterable"


# ── find_field / find_arg ──────────────────────────────────────────────────────────


def test_find_field_found():
    type_obj = {"name": "T", "fields": [{"name": "sum", "type": {}}, {"name": "dimensions", "type": {}}]}
    assert mod.find_field(type_obj, "sum")["name"] == "sum"


def test_find_field_missing_raises():
    with pytest.raises(RuntimeError, match="не найдено"):
        mod.find_field({"name": "T", "fields": []}, "rowsRead")


def test_find_arg_missing_returns_none():
    field = {"name": "ds", "args": [{"name": "limit"}]}
    assert mod.find_arg(field, "filter") is None


# ── choose_query_shape ──────────────────────────────────────────────────────────────


def test_choose_query_shape_range_when_range_filter_and_date_dimension():
    shape = mod.choose_query_shape({"date_geq", "date_leq", "namespaceId"}, {"date", "namespaceId"})
    assert shape == "range"


def test_choose_query_shape_per_day_when_only_exact_date():
    shape = mod.choose_query_shape({"date", "namespaceId"}, {"datetimeHour", "namespaceId"})
    assert shape == "per_day"


def test_choose_query_shape_raises_when_no_date_filter_at_all():
    with pytest.raises(RuntimeError, match="ни диапазон, ни точную дату"):
        mod.choose_query_shape({"namespaceId"}, {"namespaceId"})


# ── check_not_truncated: молчаливую обрезку GraphQL-ответа ловим, а не доверяем ──────


def test_check_not_truncated_passes_under_limit():
    mod.check_not_truncated([{"sum": {}}] * 5, limit=10)


def test_check_not_truncated_raises_at_limit():
    # Cloudflare не отдаёт признак обрезки — при len(rows) == limit неотличимо
    # от «ровно limit строк было на самом деле», поэтому падаем громко.
    with pytest.raises(RuntimeError, match="молчаливую обрезку"):
        mod.check_not_truncated([{"sum": {}}] * 10, limit=10)


def test_check_not_truncated_raises_over_limit():
    with pytest.raises(RuntimeError, match="молчаливую обрезку"):
        mod.check_not_truncated([{"sum": {}}] * 11, limit=10)


# ── group_rows_by_day: склейка на стыке суток ────────────────────────────────────────


def test_group_rows_by_day_splits_two_days_in_one_range_response():
    rows = [
        {"dimensions": {"date": "2026-09-01", "namespaceId": "ns-a"}, "sum": {"rowsRead": 10}},
        {"dimensions": {"date": "2026-09-01", "namespaceId": "ns-b"}, "sum": {"rowsRead": 20}},
        {"dimensions": {"date": "2026-09-02", "namespaceId": "ns-a"}, "sum": {"rowsRead": 30}},
    ]
    by_day = mod.group_rows_by_day(rows)
    assert set(by_day) == {date(2026, 9, 1), date(2026, 9, 2)}
    assert len(by_day[date(2026, 9, 1)]) == 2
    assert len(by_day[date(2026, 9, 2)]) == 1
    assert by_day[date(2026, 9, 2)][0]["sum"]["rowsRead"] == 30


def test_group_rows_by_day_empty_rows_gives_empty_dict():
    assert mod.group_rows_by_day([]) == {}


# ── daily_totals_from_rows: арифметика суточного итога ───────────────────────────────

ROWS_ONE_DAY = [
    {"dimensions": {"datetimeHour": "2026-09-01T00:00:00Z", "namespaceId": "ns-a"},
     "sum": {"rowsRead": 100, "rowsWritten": 10}},
    {"dimensions": {"datetimeHour": "2026-09-01T13:00:00Z", "namespaceId": "ns-a"},
     "sum": {"rowsRead": 900000, "rowsWritten": 20}},
    {"dimensions": {"datetimeHour": "2026-09-01T14:00:00Z", "namespaceId": "ns-b"},
     "sum": {"rowsRead": 50, "rowsWritten": 5}},
]


def test_daily_totals_sums_rows_read_and_written():
    summary = mod.daily_totals_from_rows(ROWS_ONE_DAY, {"rowsRead", "rowsWritten"},
                                         {"datetimeHour", "namespaceId"})
    assert summary["rows_read"] == 100 + 900000 + 50
    assert summary["rows_written"] == 10 + 20 + 5


def test_daily_totals_finds_peak_hour():
    summary = mod.daily_totals_from_rows(ROWS_ONE_DAY, {"rowsRead", "rowsWritten"},
                                         {"datetimeHour", "namespaceId"})
    assert summary["peak_label"] == "2026-09-01T13:00:00Z"
    assert summary["peak_rows_read"] == 900000


def test_daily_totals_breaks_down_by_namespace():
    summary = mod.daily_totals_from_rows(ROWS_ONE_DAY, {"rowsRead", "rowsWritten"},
                                         {"datetimeHour", "namespaceId"})
    assert summary["by_namespace"] == {"ns-a": 100 + 900000, "ns-b": 50}


def test_daily_totals_empty_rows():
    summary = mod.daily_totals_from_rows([], {"rowsRead"}, {"datetimeHour"})
    assert summary["rows_read"] == 0
    assert summary["peak_label"] is None
    assert summary["by_namespace"] == {}


def test_daily_totals_without_namespace_dimension_skips_breakdown():
    rows = [{"dimensions": {"datetimeHour": "2026-09-01T00:00:00Z"}, "sum": {"rowsRead": 5}}]
    summary = mod.daily_totals_from_rows(rows, {"rowsRead"}, {"datetimeHour"})
    assert summary["by_namespace"] == {}


# ── format_table / format_namespace_breakdown: рендер ────────────────────────────────


def test_format_table_shows_percent_of_daily_limit():
    day = date(2026, 9, 3)
    summary = {"rows_read": 2_500_000, "rows_written": 0, "peak_label": "13:00", "peak_rows_read": 900000}
    table = mod.format_table([(day, summary)])
    assert "2026-09-03" in table
    assert "2,500,000" in table
    assert "50.0%" in table


def test_format_namespace_breakdown_sorted_descending():
    day = date(2026, 9, 3)
    summary_a = {"by_namespace": {"ns-a": 10, "ns-b": 999}}
    text = mod.format_namespace_breakdown([(day, summary_a)])
    assert text.index("ns-b") < text.index("ns-a")


def test_format_namespace_breakdown_empty_is_explicit():
    day = date(2026, 9, 3)
    text = mod.format_namespace_breakdown([(day, {"by_namespace": {}})])
    assert "недоступна" in text


# ── build_data_query: форма запроса зависит от shape, не от догадки ──────────────────


def test_build_data_query_range_shape_uses_date_geq_leq():
    query = mod.build_data_query("durableObjectsPeriodicGroups", "range", ["rowsRead"], ["date"])
    assert "date_geq" in query and "date_leq" in query
    assert "rowsRead" in query and "date" in query


def test_build_data_query_per_day_shape_uses_exact_date():
    query = mod.build_data_query("durableObjectsPeriodicGroups", "per_day", ["rowsRead"], ["datetimeHour"])
    assert "filter: { date: $date }" in query
    assert "datetimeHour" in query


def test_build_data_query_limit_matches_check_not_truncated_constant():
    # Одно место правды: запрос и проверка обрезки обязаны ссылаться на одно
    # и то же число — раздельные литералы уже расходились бы незаметно.
    query = mod.build_data_query("durableObjectsPeriodicGroups", "range", ["rowsRead"], ["date"])
    assert f"limit: {mod.GRAPHQL_ROW_LIMIT}" in query
