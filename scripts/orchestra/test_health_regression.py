#!/usr/bin/env python3
"""Тесты детектора регрессии здоровья конвейера
(scripts/orchestra/health_regression.py, openspec/changes/
pipeline-health-self-audit).

Кормится прод-формой снимков `pipeline_health.py::build_snapshot` (список
dict с полем `date` + числовыми метриками), не пересказом. Мутационные
проверки границ — те же классы, что уже применяются к `stall_detector.py`/
`pulse_guard.py` (>= а не >, честный «недостаточно данных» вместо тихого 0).

Запуск: python -m pytest scripts/orchestra/test_health_regression.py -q
"""

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("health_regression.py")
spec = importlib.util.spec_from_file_location("health_regression", SCRIPT)
hr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hr)  # type: ignore[union-attr]


def rows_for(metric: str, values: list[float], start: date = date(2026, 8, 1)) -> list[dict]:
    """Снимки прод-формы: только интересующая метрика + дата, остальные поля
    снимка не нужны классификатору (`row.get(metric)`, а не строгая схема)."""
    return [
        {"date": (start + timedelta(days=i)).isoformat(), metric: value}
        for i, value in enumerate(values)
    ]


# ── Честный третий исход: недостаточно данных ────────────────────────────


def test_insufficient_data_below_min_samples():
    rows = rows_for("merge_throughput", [5, 5, 5, 5])  # 4 < MIN_SAMPLES_FOR_BASELINE(5)
    c = hr.classify_metric(rows, "merge_throughput")
    assert c.status == "insufficient_data"
    assert "4" in c.reason and str(hr.MIN_SAMPLES_FOR_BASELINE) in c.reason


def test_insufficient_data_ignores_none_days():
    """Дни без наблюдения (None) не считаются точками — честно исключены,
    не превращены в 0."""
    rows = [{"date": "2026-08-0" + str(i), "worker_success_rate": None} for i in range(1, 5)]
    rows.append({"date": "2026-08-05", "worker_success_rate": 90})
    c = hr.classify_metric(rows, "worker_success_rate")
    assert c.status == "insufficient_data"


def test_exactly_min_samples_is_enough_boundary():
    """Мутация границы: ровно MIN_SAMPLES_FOR_BASELINE точек — уже достаточно
    для baseline (не строго больше)."""
    rows = rows_for("merge_throughput", [5] * hr.MIN_SAMPLES_FOR_BASELINE)
    c = hr.classify_metric(rows, "merge_throughput")
    assert c.status != "insufficient_data"


# ── ok: отклонение ниже порога или не держится дольше стрика ────────────


def test_ok_when_deviation_below_threshold():
    # baseline=10 (медиана 14х10), последние 3 дня — 6 (40% просадка < 50%)
    values = [10] * 11 + [6, 6, 6]
    rows = rows_for("merge_throughput", values)
    c = hr.classify_metric(rows, "merge_throughput")
    assert c.status == "ok"
    assert c.baseline == 10
    assert c.streak_days == 0


def test_ok_when_deviation_meets_threshold_but_streak_too_short():
    # отклонение 60% (за порогом), но держится только 2 дня, не 3
    values = [10] * 12 + [4, 4]
    rows = rows_for("merge_throughput", values)
    c = hr.classify_metric(rows, "merge_throughput")
    assert c.status == "ok"
    assert c.streak_days == 2


# ── regression: держится >= MIN_REGRESSION_STREAK_DAYS, но не пожар ──────


def test_regression_when_streak_and_deviation_meet_threshold():
    values = [10] * 11 + [4, 4, 4]  # baseline не сдвигается медианой (11 vs 3)
    rows = rows_for("merge_throughput", values)
    c = hr.classify_metric(rows, "merge_throughput")
    assert c.baseline == 10
    assert c.deviation_pct == 60.0
    assert c.streak_days == 3
    assert c.status == "regression"


def test_regression_streak_boundary_mutation_guard():
    """Мутация: streak == MIN_REGRESSION_STREAK_DAYS обязан уже переводить в
    regression (не >)."""
    values = [10] * 12 + [4, 4]  # streak=2 -> ok
    assert hr.classify_metric(rows_for("merge_throughput", values), "merge_throughput").status == "ok"
    values3 = [10] * 11 + [4, 4, 4]  # streak=3 -> regression
    assert hr.classify_metric(rows_for("merge_throughput", values3), "merge_throughput").status == "regression"


def test_higher_is_worse_direction_for_pr_age():
    # PR age p95: рост — деградация. baseline=10ч, сегодня 20ч (100% рост) -> fire по deviation
    values = [10] * 11 + [20, 20, 20]
    c = hr.classify_metric(rows_for("pr_age_p95_hours", values), "pr_age_p95_hours")
    assert c.status == "fire"
    assert c.deviation_pct == 100.0


# ── fire: либо очень сильное отклонение, либо очень долгий стрик ─────────


def test_fire_when_deviation_crosses_escalation_threshold():
    values = [10] * 11 + [0, 0, 0]  # 100% просадка
    c = hr.classify_metric(rows_for("merge_throughput", values), "merge_throughput")
    assert c.deviation_pct == 100.0
    assert c.status == "fire"


def test_fire_escalation_threshold_boundary_mutation_guard():
    """Мутация >= vs >: ровно 100% отклонения обязано быть пожаром."""
    values = [10] * 11 + [0, 0, 0]
    assert hr.classify_metric(rows_for("merge_throughput", values), "merge_throughput").status == "fire"
    # чуть меньше 100% (не 0, а 1) — остаётся regression, не fire
    values_almost = [10] * 11 + [1, 1, 1]
    c = hr.classify_metric(rows_for("merge_throughput", values_almost), "merge_throughput")
    assert c.deviation_pct == 90.0
    assert c.status == "regression"


def test_fire_when_streak_exceeds_escalate_after_days_even_below_escalation_pct():
    """Стрик >= ESCALATE_AFTER_DAYS (7) переводит в fire, даже если
    сегодняшнее отклонение ниже ESCALATION_THRESHOLD_PCT (100%) — design.md
    §3.2: 'ИЛИ регрессия держится дольше ESCALATE_AFTER_DAYS'."""
    # 14 хороших (100) + 7 плохих (1): baseline (медиана окна 14) = 50.5,
    # отклонение плохих дней ≈98% (< 100% порога пожара по величине), но
    # стрик = 7 = ESCALATE_AFTER_DAYS.
    values = [100] * 14 + [1] * 7
    c = hr.classify_metric(rows_for("merge_throughput", values), "merge_throughput")
    assert c.streak_days == hr.ESCALATE_AFTER_DAYS
    assert c.deviation_pct < hr.ESCALATION_THRESHOLD_PCT
    assert c.status == "fire"


def test_streak_just_below_escalate_after_days_is_regression_not_fire():
    values = [100] * 14 + [1] * (hr.ESCALATE_AFTER_DAYS - 1)
    c = hr.classify_metric(rows_for("merge_throughput", values), "merge_throughput")
    assert c.streak_days == hr.ESCALATE_AFTER_DAYS - 1
    assert c.status == "regression"


# ── classify_all: покрывает весь набор метрик независимо друг от друга ────


def test_classify_all_covers_every_metric_independently():
    rows = []
    start = date(2026, 8, 1)
    for i in range(20):
        row = {"date": (start + timedelta(days=i)).isoformat()}
        row["merge_throughput"] = 10  # здоровая метрика
        # worker_success_rate отсутствует первые 10 дней — «нет данных»,
        # затем стабильна: недостаточно точек НЕ должно быть (10 >= 5)
        if i >= 10:
            row["worker_success_rate"] = 95
        rows.append(row)
    classifications = hr.classify_all(rows)
    by_metric = {c.metric: c for c in classifications}
    assert set(by_metric) == set(hr.METRICS)
    assert by_metric["merge_throughput"].status == "ok"
    assert by_metric["worker_success_rate"].status != "insufficient_data"
    # метрики, вообще не встречавшиеся в снимках (нет ключа ни в одной строке) — insufficient_data
    assert by_metric["backlog_total"].status == "insufficient_data"


def test_deviation_pct_zero_baseline_guard():
    assert hr.deviation_pct(0, 0, "lower_is_worse") == 0.0
    assert hr.deviation_pct(5, 0, "higher_is_worse") == float("inf")
