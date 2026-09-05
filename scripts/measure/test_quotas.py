#!/usr/bin/env python3
"""Тесты сборщика квот (scripts/measure/quotas.py, #324).

Кормятся прод-формой, не пересказом: fixtures/github_rate_limit.json —
дословный `gh api rate_limit` этого репозитория (live, 2026-09-05);
fixtures/github_actions_runs_in_progress.json — реальный (урезан от
повторяющихся actor/urls) `gh api .../actions/runs?per_page=1`, `total_count` —
то самое поле, что читает collect_github; fixtures/cf_workers_invocations_doc_example.json —
вербатимный пример ответа GraphQL Analytics API из документации Cloudflare
(developers.cloudflare.com/analytics/graphql-api/tutorials/querying-workers-metrics/).

Сетевые вызовы (cf_query, gh_api) подменяются monkeypatch — как в
scripts/orchestra/test_pulse_guard.py.

Запуск: python -m pytest scripts/measure/test_quotas.py -q
"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quotas.py")
spec = importlib.util.spec_from_file_location("quotas", SCRIPT)
qz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qz)  # type: ignore[union-attr]

FIXTURES = Path(__file__).with_name("fixtures")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── Row: процент и порог ──────────────────────────────────────────────────────


@pytest.mark.parametrize("row", [
    qz.no_data("x", "y", 100, "u", "причина"),          # нет данных вовсе
    qz.Row("x", "y", 50, None, "u", "-", "ok"),          # лимит неизвестен
])
def test_pct_none_when_unmeasurable(row):
    assert row.pct is None


def test_pct_computed_and_rounded():
    row = qz.Row("x", "y", 4_800_000, 5_000_000, "rows", "-", "ok")
    assert row.pct == 96.0


def test_over_threshold_picks_only_breached():
    rows = [
        qz.Row("a", "s", 90, 100, "u", "-", "ok"),   # 90%
        qz.Row("b", "s", 10, 100, "u", "-", "ok"),   # 10%
        qz.no_data("c", "s", 100, "u", "нет данных — не считается порогом"),
    ]
    breached = qz.over_threshold(rows, threshold=80.0)
    assert [r.resource for r in breached] == ["a"]


def test_over_threshold_mutation_guard_boundary_is_inclusive():
    """Мутационная проверка: >= а не >, ровно на границе 80% сигнал обязан
    сработать — тест красится, если оператор ослабить до строгого >."""
    row = qz.Row("edge", "s", 80, 100, "u", "-", "ok")
    assert qz.over_threshold([row], threshold=80.0) == [row]


# ── format_table: недоступный источник виден, а не пропущен ─────────────────


def test_format_table_shows_no_data_reason_not_silently_dropped():
    rows = [qz.no_data("DO rows_read/сутки", "Cloudflare GraphQL Analytics",
                        5_000_000, "rows", "секрет не задан")]
    table = qz.format_table(rows)
    assert "нет данных" in table
    assert "секрет не задан" in table
    assert "DO rows_read/сутки" in table


# ── GitHub: rate_limit — реальная форма ответа ────────────────────────────────


def test_collect_github_rate_limit_reads_real_payload(monkeypatch):
    payload = load("github_rate_limit.json")

    def fake_gh_api(*args):
        if args == ("rate_limit",):
            return payload
        return {"total_count": 0}

    monkeypatch.setattr(qz, "gh_api", fake_gh_api)
    rows = qz.collect_github("mytab0r/edge-harness")
    core_row = next(r for r in rows if "REST rate limit" in r.resource)
    assert core_row.status == "ok"
    assert core_row.current == payload["resources"]["core"]["used"]
    assert core_row.limit == payload["resources"]["core"]["limit"]
    graphql_row = next(r for r in rows if "GraphQL rate limit" in r.resource)
    assert graphql_row.limit == payload["resources"]["graphql"]["limit"]


def test_collect_github_mutation_guard_wrong_key_is_no_data(monkeypatch):
    """Мутация класса: если бы код читал resources['core']['limit'] из
    несуществующего ключа — no_data, а не падение и не выдумка числа."""
    monkeypatch.setattr(qz, "gh_api", lambda *a: {"resources": {}})
    rows = qz.collect_github("mytab0r/edge-harness")
    core_row = next(r for r in rows if "REST rate limit" in r.resource)
    assert core_row.status == "no-data"
    assert core_row.current is None


def test_collect_github_in_progress_reads_real_payload(monkeypatch):
    payload = load("github_actions_runs_in_progress.json")

    def fake_gh_api(*args):
        if "status=in_progress" in args:
            return payload
        if args == ("rate_limit",):
            return {"resources": {"core": {}, "graphql": {}}}
        return {"total_count": 0}

    monkeypatch.setattr(qz, "gh_api", fake_gh_api)
    rows = qz.collect_github("mytab0r/edge-harness")
    row = next(r for r in rows if "In-progress" in r.resource)
    assert row.current == payload["total_count"] == 1
    assert row.limit == qz.LIMITS["gh_concurrent_jobs"]


def test_collect_github_reports_actions_minutes_not_applicable(monkeypatch):
    monkeypatch.setattr(qz, "gh_api", lambda *a: {"total_count": 0, "resources": {"core": {}, "graphql": {}}})
    rows = qz.collect_github("mytab0r/edge-harness")
    minutes_row = next(r for r in rows if "Actions минуты" in r.resource)
    assert minutes_row.status == "no-data"
    assert "безлимитно" in minutes_row.note


def test_gh_api_raises_loud_on_failure(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "HTTP 403: Forbidden"

    monkeypatch.setattr(qz.subprocess, "run", lambda *a, **k: FakeResult())
    with pytest.raises(RuntimeError, match="403"):
        qz.gh_api("rate_limit")


# ── Cloudflare: разбор реального примера из документации ────────────────────


def test_cf_workers_invocations_doc_example_sums_requests(monkeypatch):
    """Пример из доков Cloudflare даёт 3 события с sum.requests 1/1/4 = 6 —
    ровно то, что должна вернуть агрегация в collect_cloudflare."""
    doc = load("cf_workers_invocations_doc_example.json")

    def fake_cf_query(token, query, variables=None):
        if "workersInvocationsAdaptive" in query:
            return doc["data"]
        if "__schema" in query:
            return {"__schema": {"types": []}}
        return {"viewer": {"accounts": [{"durableObjectsStorageGroups": []}]}}

    monkeypatch.setattr(qz, "cf_query", fake_cf_query)
    rows = qz.collect_cloudflare("acct", "tok")
    workers_row = next(r for r in rows if r.resource == "Workers requests/сутки")
    assert workers_row.status == "ok"
    assert workers_row.current == 6
    assert workers_row.limit == qz.LIMITS["cf_workers_requests_day"]


def test_cf_rows_metric_not_found_is_no_data_not_guess(monkeypatch):
    """Если интроспекция не находит поле rowsRead/rowsWritten ни в одном
    Sum/Max-типе Durable Objects — «нет данных», а не нулевая выдумка."""
    def fake_cf_query(token, query, variables=None):
        if "__schema" in query:
            return {"__schema": {"types": [{"name": "AccountDurableObjectsPeriodicGroupsSum"}]}}
        if "__type" in query:
            return {"__type": {"fields": [{"name": "cpuTime"}]}}  # rowsRead отсутствует
        if "workersInvocationsAdaptive" in query:
            return {"viewer": {"accounts": [{"workersInvocationsAdaptive": []}]}}
        return {"viewer": {"accounts": [{"durableObjectsStorageGroups": []}]}}

    monkeypatch.setattr(qz, "cf_query", fake_cf_query)
    rows = qz.collect_cloudflare("acct", "tok")
    rows_read_row = next(r for r in rows if r.resource == "DO rows_read/сутки")
    assert rows_read_row.status == "no-data"
    assert "не найдено" in rows_read_row.note


def test_cf_rows_metric_found_via_introspection_real_field_names(monkeypatch):
    """Имена полей rowsRead/rowsWritten — реальные, задокументированные
    дословно Cloudflare для D1 (таблица «GraphQL Field Name» в
    developers.cloudflare.com/d1/observability/metrics-analytics/); для
    Durable Objects точное имя не опубликовано, поэтому find_row_metric ищет
    его интроспекцией по любому Sum/Max-типу датасетов DO — здесь схема
    отвечает, что поле нашлось в одном из них."""
    calls = {"data_query": None}

    def fake_cf_query(token, query, variables=None):
        if "__schema" in query:
            return {"__schema": {"types": [
                {"name": "AccountDurableObjectsPeriodicGroupsSum"},
                {"name": "AccountDurableObjectsStorageGroupsMax"},
            ]}}
        if "__type" in query and "PeriodicGroupsSum" in query:
            return {"__type": {"fields": [{"name": "cpuTime"}, {"name": "rowsRead"}]}}
        if "__type" in query:
            return {"__type": {"fields": [{"name": "storedBytes"}]}}
        if "workersInvocationsAdaptive" in query:
            return {"viewer": {"accounts": [{"workersInvocationsAdaptive": []}]}}
        if "durableObjectsStorageGroups" in query:
            return {"viewer": {"accounts": [{"durableObjectsStorageGroups": []}]}}
        calls["data_query"] = query
        return {"viewer": {"accounts": [{"durableObjectsPeriodicGroups": [{"sum": {"rowsRead": 4_800_000}}]}]}}

    monkeypatch.setattr(qz, "cf_query", fake_cf_query)
    rows = qz.collect_cloudflare("acct", "tok")
    rows_read_row = next(r for r in rows if r.resource == "DO rows_read/сутки")
    assert rows_read_row.status == "ok"
    assert rows_read_row.current == 4_800_000
    assert rows_read_row.pct == 96.0
    assert calls["data_query"] is not None


def test_cf_schema_introspection_failure_is_no_data_for_all_cf_rows(monkeypatch):
    def broken(token, query, variables=None):
        raise RuntimeError("HTTP 401: invalid token")

    monkeypatch.setattr(qz, "cf_query", broken)
    rows = qz.collect_cloudflare("acct", "tok")
    assert len(rows) == 4
    assert all(r.status == "no-data" for r in rows)
    assert all("интроспекция" in r.note for r in rows)


# ── LLM-провайдер: честное «нет данных» ───────────────────────────────────────


def test_provider_reports_no_quota_api_with_reason():
    rows = qz.collect_provider()
    assert len(rows) == 1
    assert rows[0].status == "no-data"
    assert "RATE_LIMIT" in rows[0].note
