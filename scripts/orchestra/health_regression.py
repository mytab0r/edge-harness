#!/usr/bin/env python3
"""Детектор регрессии здоровья конвейера (openspec/changes/
pipeline-health-self-audit, design.md §2.3) — «зелёно, но хуже».

Три существующих детектора (`pulse_guard.py::failure_watch`,
`stall_detector.py`, `upstream_drift.py`) реагируют на КРАСНОЕ. Этот модуль
читает УЖЕ СНЯТУЮ историю метрик (`scripts/measure/pipeline_health.py`) и
отвечает на другой вопрос: не «что сломалось сейчас», а «стало ли хуже за
последние дни при формально зелёном конвейере».

## Алгоритм (design.md §2.3, дословно)

  baseline = медиана значений метрики за последние BASELINE_WINDOW_DAYS дней;
  регрессия, если сегодняшнее значение отклоняется от baseline более чем на
  REGRESSION_THRESHOLD_PCT В ХУДШУЮ ДЛЯ ЭТОЙ МЕТРИКИ СТОРОНУ, и это отклонение
  держится MIN_REGRESSION_STREAK_DAYS дней подряд.

Три честных исхода на каждую метрику (не два — «недостаточно данных» не
«всё хорошо» и не ложная тревога, тот же принцип, что `stall_detector.py`):
  - `insufficient_data` — меньше MIN_SAMPLES_FOR_BASELINE снятых точек;
  - `ok`                — отклонение либо ниже порога, либо не держится
                           MIN_REGRESSION_STREAK_DAYS дней подряд;
  - `regression`         — держится, отклонение ниже ESCALATION_THRESHOLD_PCT;
  - `fire`                — держится, отклонение >= ESCALATION_THRESHOLD_PCT
                           ИЛИ streak >= ESCALATE_AFTER_DAYS — тот же приём,
                           что `pulse_guard.decide_gate_state` (несколько
                           исходов из ОДНОГО расчёта, не отдельные if-цепочки
                           без общего критерия).

## Честный потолок

Детектор видит РОВНО те метрики, что снимает `pipeline_health.py`
(§1 design.md) — регрессия, не отражённая ни в одной из них (например:
конкретный агент стал писать код хуже, но объём/скорость слияний не
изменились), этим детектором не поймана и не может быть поймана — тот же
класс честного потолка, что докстринг `stall_detector.py` «видит только
отчёт СВОЕГО прогона».

## Пороги — НЕ подтверждены (proposal.md, «Не подтверждено», п.1)

Все константы ниже названы по аналогии с уже существующими порогами
(STALL_PERSIST_MINUTES и т.п.), ни один не выведен из данных этого
репозитория: истории снимков ещё не существует на момент написания этого
модуля. Подбор по факту первых недель эксплуатации — отдельная, не
сделанная здесь работа.

Запуск тестов: python -m pytest scripts/orchestra/test_health_regression.py -q
"""

from __future__ import annotations

import statistics
from typing import NamedTuple

# ── Пороги (одно место правды этого детектора, см. докстринг выше) ────────

BASELINE_WINDOW_DAYS = 14
MIN_SAMPLES_FOR_BASELINE = 5
REGRESSION_THRESHOLD_PCT = 50.0
MIN_REGRESSION_STREAK_DAYS = 3
ESCALATION_THRESHOLD_PCT = 100.0
ESCALATE_AFTER_DAYS = 7

# Ровно 6 метрик — design.md §3.1: "Максимум 6 метрик -> потолок можно
# поставить равным их числу (SELF_AUDIT_DAILY_CAP = 6) без риска
# бесконечного роста пула". pr_age_p50 и gh_rate_remaining_pct снимаются
# (pipeline_health.py), но не входят в регрессионный набор: p95 — более
# чувствительный хвостовой прокси той же величины (не дублируем сигнал по
# одному измерению дважды), gh_rate_remaining сбрасывается каждый час
# (нестабильная база для суточного baseline, честно исключена, не выведена
# в регрессию — design.md не называет её кандидатом на baseline явно).
METRICS: dict[str, tuple[str, str]] = {
    "merge_throughput": ("Merge throughput (PR слито/сутки)", "lower_is_worse"),
    "pr_age_p95_hours": ("PR age p95 (часы)", "higher_is_worse"),
    "backlog_total": ("Backlog задач пула (всего открытых)", "higher_is_worse"),
    "worker_success_rate": ("Worker success-rate (%)", "lower_is_worse"),
    "pulse_cadence_ratio": ("Каденс пульса orchestra (доля тиков от ожидаемых)", "lower_is_worse"),
    "do_rows_read_pct": ("Стоимость DO (rows_read, % от суточного лимита)", "higher_is_worse"),
}


class Classification(NamedTuple):
    status: str  # 'insufficient_data' | 'ok' | 'regression' | 'fire'
    metric: str
    label: str
    direction: str
    baseline: float | None = None
    today: float | None = None
    today_date: str | None = None
    deviation_pct: float | None = None
    streak_days: int = 0
    reason: str = ""


def deviation_pct(value: float, baseline: float, direction: str) -> float:
    """>0 значит «хуже baseline» в терминах направления этой метрики; 0 —
    не хуже или лучше. `baseline == 0` — особый случай (само по себе 0 не
    обязано быть недостижимым, например merge_throughput в тихие выходные):
    отклонение считается только если value тоже не 0, иначе неопределённость
    деления честно даёт "бесконечно хуже", а не тихий 0/0 → 0."""
    if baseline == 0:
        return 0.0 if value == 0 else float("inf")
    if direction == "lower_is_worse":
        return max(0.0, (baseline - value) / baseline * 100.0)
    return max(0.0, (value - baseline) / baseline * 100.0)


def classify_metric(rows: list[dict], metric: str) -> Classification:
    """`rows` — снимки в ХРОНОЛОГИЧЕСКОМ порядке (старые → новые), тот же
    порядок, что `pipeline_health.read_rows` отдаёт по построению (append-only
    JSONL). Точки с `None` по этой метрике (честное «нет данных» дня) не
    участвуют — они не «0», это отсутствие наблюдения."""
    label, direction = METRICS[metric]
    dated_values = [(row["date"], row[metric]) for row in rows if row.get(metric) is not None]
    if len(dated_values) < MIN_SAMPLES_FOR_BASELINE:
        return Classification(
            "insufficient_data", metric, label, direction,
            reason=f"снято {len(dated_values)} точек метрики, нужно минимум {MIN_SAMPLES_FOR_BASELINE}",
        )

    values = [value for _, value in dated_values]
    window = values[-BASELINE_WINDOW_DAYS:]
    baseline = statistics.median(window)
    today_date, today_value = dated_values[-1]
    today_deviation = deviation_pct(today_value, baseline, direction)

    streak = 0
    for _, value in reversed(dated_values):
        if deviation_pct(value, baseline, direction) >= REGRESSION_THRESHOLD_PCT:
            streak += 1
        else:
            break

    base = Classification(
        "ok", metric, label, direction, baseline=round(baseline, 3), today=today_value,
        today_date=today_date, deviation_pct=round(today_deviation, 1), streak_days=streak,
    )
    if streak < MIN_REGRESSION_STREAK_DAYS:
        return base

    is_fire = today_deviation >= ESCALATION_THRESHOLD_PCT or streak >= ESCALATE_AFTER_DAYS
    return base._replace(status="fire" if is_fire else "regression")


def classify_all(rows: list[dict]) -> list[Classification]:
    return [classify_metric(rows, metric) for metric in METRICS]
