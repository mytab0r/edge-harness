#!/usr/bin/env python3
"""Тесты детектора регрессии здоровья конвейера, привязанного к слиянию
(scripts/orchestra/merge_health_watch.py, issue #967).

Ключевой блок — «Доказательство на реальной истории»: фикстура ниже — это
ДОСЛОВНЫЙ вывод `gh api repos/mytab0r/edge-harness/actions/workflows/
worker.yml/runs` за 2026-09-08..2026-09-11 (87 прогонов, снято 2026-09-11),
не пересказ и не синтетика. Доказывает: детектор поймал бы регрессию
PR #878 (слит 2026-09-10T22:54:34Z) в пределах часов, не суток.

Запуск: python -m pytest scripts/orchestra/test_merge_health_watch.py -q
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("merge_health_watch.py")
spec = importlib.util.spec_from_file_location("merge_health_watch", SCRIPT)
mhw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mhw)  # type: ignore[union-attr]


def utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def run(created_at: str, conclusion: str | None) -> dict:
    return {"created_at": created_at, "conclusion": conclusion}


# ── success_rate / runs_in_window: чистая логика ────────────────────────────


def test_success_rate_prod_form_ignores_in_progress():
    runs = [run("2026-09-11T00:00:00Z", "success"),
            run("2026-09-11T01:00:00Z", "failure"),
            run("2026-09-11T02:00:00Z", None)]  # ещё бежит
    rate, n = mhw.success_rate(runs)
    assert rate == 50.0
    assert n == 2


def test_success_rate_no_concluded_is_honest_none():
    assert mhw.success_rate([]) == (None, 0)
    assert mhw.success_rate([run("2026-09-11T00:00:00Z", None)]) == (None, 0)


def test_runs_in_window_half_open_boundaries():
    runs = [run("2026-09-11T00:00:00Z", "success"),
            run("2026-09-11T06:00:00Z", "success")]
    start, end = utc("2026-09-11T00:00:00Z"), utc("2026-09-11T06:00:00Z")
    window = mhw.runs_in_window(runs, start, end)
    assert [r["created_at"] for r in window] == ["2026-09-11T00:00:00Z"]  # [start, end) — конец не включён


# ── evaluate: три честных исхода + мутация порога ───────────────────────────


def test_evaluate_insufficient_data_below_min_recent():
    now = utc("2026-09-11T00:00:00Z")
    runs = [run("2026-09-10T23:00:00Z", "success")]  # 1 < MIN_SAMPLES_RECENT(3)
    v = mhw.evaluate(runs, now)
    assert v.status == "insufficient_data"
    assert "recent=1" in v.reason


def test_evaluate_insufficient_data_below_min_baseline():
    now = utc("2026-09-11T00:00:00Z")
    recent = [run(f"2026-09-10T{h:02d}:00:00Z", "success") for h in (19, 20, 21)]
    v = mhw.evaluate(recent, now)  # ни одного прогона в baseline-окне вовсе
    assert v.status == "insufficient_data"
    assert "baseline=0" in v.reason


def test_evaluate_ok_when_deviation_below_threshold():
    now = utc("2026-09-11T00:00:00Z")
    baseline = [run(f"2026-09-08T{h:02d}:00:00Z", "success" if i % 2 else "failure")
                for i, h in enumerate(range(0, 20))]  # 50% baseline, n=20
    recent = [run("2026-09-10T19:00:00Z", "success"),
              run("2026-09-10T20:00:00Z", "success"),
              run("2026-09-10T21:00:00Z", "failure")]  # 66.7% — лучше baseline
    v = mhw.evaluate(baseline + recent, now)
    assert v.status == "ok"
    assert v.deviation_pct == 0.0  # lower_is_worse: значение выше baseline — не хуже


def test_evaluate_regression_vs_fire_threshold_mutation():
    """Мутационная граница: >= REGRESSION_THRESHOLD_PCT(50), не >; >=
    ESCALATION_THRESHOLD_PCT(100), не >. Baseline ровно 50% (n=10), recent
    ровно 25% (n=4) — отклонение ровно 50% → regression, не fire."""
    now = utc("2026-09-11T00:00:00Z")
    baseline = [run(f"2026-09-08T{h:02d}:00:00Z", "success" if i % 2 else "failure")
                for i, h in enumerate(range(0, 10))]  # 50%, n=10
    recent = [run("2026-09-10T19:00:00Z", "success"),
              run("2026-09-10T20:00:00Z", "failure"),
              run("2026-09-10T21:00:00Z", "failure"),
              run("2026-09-10T22:00:00Z", "failure")]  # 25%, n=4
    v = mhw.evaluate(baseline + recent, now)
    assert v.baseline_rate == 50.0
    assert v.recent_rate == 25.0
    assert v.deviation_pct == 50.0
    assert v.status == "regression"  # держится >= порога, но < ESCALATION(100)


def test_evaluate_fire_at_full_collapse():
    now = utc("2026-09-11T00:00:00Z")
    baseline = [run(f"2026-09-08T{h:02d}:00:00Z", "success" if i % 2 else "failure")
                for i, h in enumerate(range(0, 10))]  # 50%, n=10
    recent = [run("2026-09-10T19:00:00Z", "failure"),
              run("2026-09-10T20:00:00Z", "failure"),
              run("2026-09-10T21:00:00Z", "failure")]  # 0%
    v = mhw.evaluate(baseline + recent, now)
    assert v.deviation_pct == 100.0
    assert v.status == "fire"


# ── Доказательство на реальной истории (issue #967, живой корень #878) ─────
#
# Дословный вывод `gh api repos/mytab0r/edge-harness/actions/workflows/
# worker.yml/runs` (снято 2026-09-11), окно 2026-09-07T16:54:34Z..
# 2026-09-11T04:54:34Z (78ч до слияния PR #878 + 6ч после) — 87 прогонов.
MERGE_878_AT = "2026-09-10T22:54:34Z"

REAL_WORKER_RUNS_AROUND_878 = [
    run("2026-09-11T04:04:32Z", "failure"), run("2026-09-11T03:39:39Z", "failure"),
    run("2026-09-11T03:09:01Z", "failure"), run("2026-09-11T01:23:28Z", "failure"),
    run("2026-09-11T00:53:31Z", "failure"), run("2026-09-10T21:43:11Z", "success"),
    run("2026-09-10T21:14:12Z", "success"), run("2026-09-10T15:49:42Z", "success"),
    run("2026-09-10T15:03:21Z", "failure"), run("2026-09-10T13:47:26Z", "failure"),
    run("2026-09-10T13:03:25Z", "failure"), run("2026-09-10T12:18:39Z", "failure"),
    run("2026-09-10T11:57:36Z", "failure"), run("2026-09-10T11:33:30Z", "failure"),
    run("2026-09-10T11:26:37Z", "success"), run("2026-09-10T11:26:18Z", "failure"),
    run("2026-09-10T11:18:49Z", "failure"), run("2026-09-10T11:14:01Z", "failure"),
    run("2026-09-10T10:46:36Z", "success"), run("2026-09-10T09:43:26Z", "failure"),
    run("2026-09-10T08:25:54Z", "failure"), run("2026-09-10T05:32:49Z", "failure"),
    run("2026-09-10T01:13:08Z", "failure"), run("2026-09-09T23:10:42Z", "failure"),
    run("2026-09-09T22:08:24Z", "failure"), run("2026-09-09T21:12:22Z", "failure"),
    run("2026-09-09T20:34:24Z", "failure"), run("2026-09-09T20:28:57Z", "failure"),
    run("2026-09-09T20:22:09Z", "failure"), run("2026-09-09T19:44:51Z", "success"),
    run("2026-09-09T18:19:48Z", "success"), run("2026-09-09T17:31:23Z", "success"),
    run("2026-09-09T17:08:13Z", "success"), run("2026-09-09T17:00:52Z", "success"),
    run("2026-09-09T16:52:14Z", "success"), run("2026-09-09T16:17:35Z", "success"),
    run("2026-09-09T14:45:41Z", "success"), run("2026-09-09T14:19:22Z", "success"),
    run("2026-09-09T10:27:41Z", "cancelled"), run("2026-09-09T10:21:51Z", "cancelled"),
    run("2026-09-09T09:17:11Z", "failure"), run("2026-09-09T08:13:57Z", "failure"),
    run("2026-09-09T07:48:37Z", "failure"), run("2026-09-09T06:37:07Z", "success"),
    run("2026-09-09T06:33:59Z", "success"), run("2026-09-09T06:14:51Z", "success"),
    run("2026-09-09T05:41:29Z", "failure"), run("2026-09-09T03:03:27Z", "failure"),
    run("2026-09-09T02:08:07Z", "failure"), run("2026-09-08T22:05:29Z", "failure"),
    run("2026-09-08T17:45:51Z", "failure"), run("2026-09-08T15:16:40Z", "failure"),
    run("2026-09-08T14:14:52Z", "failure"), run("2026-09-08T12:17:39Z", "failure"),
    run("2026-09-08T09:05:29Z", "failure"), run("2026-09-08T08:58:55Z", "failure"),
    run("2026-09-08T08:45:55Z", "failure"), run("2026-09-08T08:09:44Z", "success"),
    run("2026-09-08T07:56:12Z", "success"), run("2026-09-08T07:51:04Z", "success"),
    run("2026-09-08T07:46:35Z", "success"), run("2026-09-08T07:34:45Z", "success"),
    run("2026-09-08T07:28:42Z", "success"), run("2026-09-08T07:12:33Z", "success"),
    run("2026-09-08T06:39:03Z", "failure"), run("2026-09-08T06:13:29Z", "failure"),
    run("2026-09-08T06:01:19Z", "failure"), run("2026-09-08T05:31:23Z", "failure"),
    run("2026-09-08T05:03:04Z", "success"), run("2026-09-08T04:58:16Z", "success"),
    run("2026-09-08T04:51:04Z", "success"), run("2026-09-08T04:45:01Z", "failure"),
    run("2026-09-08T04:41:28Z", "success"), run("2026-09-08T04:19:23Z", "success"),
    run("2026-09-08T04:04:18Z", "success"), run("2026-09-08T03:49:33Z", "success"),
    run("2026-09-08T03:29:31Z", "failure"), run("2026-09-08T03:26:29Z", "failure"),
    run("2026-09-08T03:19:05Z", "failure"), run("2026-09-08T02:46:25Z", "success"),
    run("2026-09-08T02:42:54Z", "success"), run("2026-09-08T02:39:57Z", "success"),
    run("2026-09-08T02:36:37Z", "success"), run("2026-09-08T02:28:41Z", "success"),
    run("2026-09-08T02:25:14Z", "success"), run("2026-09-08T02:22:07Z", "success"),
    run("2026-09-08T01:40:37Z", "success"),
]


def test_real_history_baseline_before_878_matches_manual_count():
    """Baseline-окно, кончающееся РОВНО в момент слияния (при now=слияние+6ч,
    baseline=[now-78ч, now-6ч)=[слияние-72ч, слияние)) — независимо посчитан
    `gh api` вручную (issue #967: 39/82 = 47.6%), сходится с тем, что вычисляет
    сам модуль — доказывает, что фикстура не подогнана под ответ."""
    now = utc(MERGE_878_AT) + timedelta(hours=6)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.baseline_n == 82
    assert v.baseline_rate == 47.6


@pytest.mark.parametrize("hours_after,expected_status", [
    (0, "insufficient_data"),  # слияние только что — прогонов после ещё почти нет
    (1, "insufficient_data"),
    (4, "ok"),   # рано: в recent-окне ещё видны успехи ДО мержа (21:14/21:43)
    (5, "fire"),  # окно очистилось от предыдущих успехов — падение видно
    (6, "fire"),
])
def test_real_history_878_regression_detected_within_hours(hours_after, expected_status):
    """ГЛАВНОЕ доказательство issue #967: на РЕАЛЬНОЙ истории детектор
    впервые кричит `fire` через 5ч после слияния PR #878 (22:54:34Z →
    03:54:34Z) — часы, не сутки (суточный снимок `pipeline_health.py`
    увидел бы это не раньше следующих суток)."""
    now = utc(MERGE_878_AT) + timedelta(hours=hours_after)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.status == expected_status


def test_real_history_878_fire_verdict_numbers():
    now = utc(MERGE_878_AT) + timedelta(hours=6)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.status == "fire"
    assert v.recent_rate == 0.0
    assert v.recent_n == 5
    assert v.baseline_rate == 47.6
    assert v.deviation_pct == 100.0


# ── suspects_from_search: несколько подозреваемых честно, без угадывания ───


def _merged_item(number, title, merged_at):
    return {"number": number, "title": title, "pull_request": {"merged_at": merged_at}}


# Дословный вывод `gh api "search/issues?q=repo:mytab0r/edge-harness+is:pr+
# is:merged+merged:2026-09-10T16:00:00..2026-09-11T05:00:00"` (снято 2026-09-11,
# только релевантные поля).
REAL_MERGED_PRS_AROUND_878 = {
    "total_count": 6, "incomplete_results": False,
    "items": [
        _merged_item(771, "#749: каталог гвардий scripts/ci/guards", "2026-09-10T20:33:07Z"),
        _merged_item(895, "#882: авторизация push снимков здоровья", "2026-09-10T21:12:04Z"),
        _merged_item(890, "#884: эскалация ai-review о крупном PR", "2026-09-10T21:22:54Z"),
        _merged_item(640, "#635: гвардия скана .github/workflows", "2026-09-10T22:06:39Z"),
        _merged_item(878, "#876: успех воркера требует rc/провайдера/новых коммитов", "2026-09-10T22:54:34Z"),
        _merged_item(862, "#860: Claude-пул только в worker/hands", "2026-09-11T01:41:27Z"),
    ],
}


def test_suspects_from_search_real_history_lists_all_not_one():
    """Окно атрибуции 12ч перед now=+6ч после слияния #878 (2026-09-11T04:54:34Z)
    накрывает ШЕСТЬ реальных слияний той ночи — детектор перечисляет все
    шесть, не угадывает одно «самое вероятное» (design.md, «Корреляция, не
    причинность»)."""
    now = utc(MERGE_878_AT) + timedelta(hours=6)
    suspects, truncated = mhw.suspects_from_search(REAL_MERGED_PRS_AROUND_878, now)
    assert not truncated
    assert [s.number for s in suspects] == [771, 895, 890, 640, 878, 862]  # хронологически


def test_suspects_from_search_excludes_outside_window():
    now = utc("2026-09-11T00:00:00Z")
    search_result = {
        "total_count": 2, "items": [
            _merged_item(1, "старое", "2026-09-01T00:00:00Z"),  # далеко до окна
            _merged_item(2, "в окне", "2026-09-10T20:00:00Z"),
        ],
    }
    suspects, truncated = mhw.suspects_from_search(search_result, now, lookback_hours=12)
    assert [s.number for s in suspects] == [2]


def test_suspects_from_search_truncated_flag():
    search_result = {"total_count": 5, "items": [_merged_item(1, "x", "2026-09-10T20:00:00Z")]}
    _, truncated = mhw.suspects_from_search(search_result, utc("2026-09-11T00:00:00Z"))
    assert truncated is True


# ── render_report: текст называет числа, не гадает ──────────────────────────


def test_render_report_no_suspects_says_so_honestly():
    v = mhw.Verdict("fire", 47.6, 82, 0.0, 5, 100.0, "2026-09-10T21:54:34+00:00",
                    "2026-09-11T04:54:34+00:00")
    text = mhw.render_report(v, [], False)
    assert "не найдено" in text
    assert "внешней причиной" in text


def test_render_report_multiple_suspects_lists_each_with_number():
    v = mhw.Verdict("fire", 47.6, 82, 0.0, 5, 100.0, "2026-09-10T21:54:34+00:00",
                    "2026-09-11T04:54:34+00:00")
    suspects = [mhw.Suspect(640, "a", "2026-09-10T22:06:39Z"),
                mhw.Suspect(878, "b", "2026-09-10T22:54:34Z")]
    text = mhw.render_report(v, suspects, False)
    assert "Подозреваемых 2" in text
    assert "#640" in text and "#878" in text
    assert "НЕ причинность" in text


# ── run_watch: проводка (mock gh/pool_issue/escalate), без сети ─────────────


class FakeGh:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


def test_run_watch_ok_does_not_create_task_or_escalate(monkeypatch):
    now = utc("2026-09-09T00:00:00Z")
    baseline = [run(f"2026-09-06T{h:02d}:00:00Z", "success" if i % 2 else "failure")
                for i, h in enumerate(range(0, 10))]
    recent = [run("2026-09-08T20:00:00Z", "success"),
              run("2026-09-08T21:00:00Z", "success"),
              run("2026-09-08T22:00:00Z", "failure")]
    fake = FakeGh({
        "actions/workflows/worker.yml/runs": {"workflow_runs": baseline + recent},
    })
    monkeypatch.setattr(mhw.pulse_guard, "gh", fake)
    report = mhw.run_watch("mytab0r/edge-harness", now)
    assert any("ok" in line for line in report)
    assert not any("search/issues" in call for call in fake.calls)  # подозреваемых не искали вовсе


def test_run_watch_fire_creates_task_and_escalates(monkeypatch):
    now = utc(MERGE_878_AT) + timedelta(hours=6)
    fake = FakeGh({
        "actions/workflows/worker.yml/runs": {"workflow_runs": REAL_WORKER_RUNS_AROUND_878},
        "search/issues": REAL_MERGED_PRS_AROUND_878,
        "issues?state=open&labels=area%3Aprocess": [],  # нет открытой задачи — заводим новую
        "-X POST repos/mytab0r/edge-harness/issues ": {"number": 999},
    })
    monkeypatch.setattr(mhw.pulse_guard, "gh", fake)
    posted_comments = []
    monkeypatch.setattr(mhw.pulse_guard, "post_issue_comment",
                        lambda repo, num, text: posted_comments.append((num, text)))
    escalated = []
    monkeypatch.setattr(mhw.pulse_guard, "escalate",
                        lambda repo, num, text, options=None: escalated.append((num, text)) or "ok")

    report = mhw.run_watch("mytab0r/edge-harness", now)

    assert any("задача #999 заведена" in line for line in report)
    assert len(escalated) == 1
    assert escalated[0][0] == mhw.pulse_guard.WATCHDOG_ISSUE
    assert "fire" in escalated[0][1]
    assert not posted_comments  # первая улика — issue создаётся, не комментируется


def test_run_watch_fire_second_time_comments_existing_task(monkeypatch):
    now = utc(MERGE_878_AT) + timedelta(hours=6)
    fp = mhw._fingerprint(now)
    existing_issue = {"number": 555, "body": f"...\n{mhw._fingerprint_line(fp)}\n..."}
    fake = FakeGh({
        "actions/workflows/worker.yml/runs": {"workflow_runs": REAL_WORKER_RUNS_AROUND_878},
        "search/issues": REAL_MERGED_PRS_AROUND_878,
        "issues?state=open&labels=area%3Aprocess": [existing_issue],
    })
    monkeypatch.setattr(mhw.pulse_guard, "gh", fake)
    posted_comments = []
    monkeypatch.setattr(mhw.pulse_guard, "post_issue_comment",
                        lambda repo, num, text: posted_comments.append((num, text)))
    monkeypatch.setattr(mhw.pulse_guard, "escalate", lambda *a, **k: "ok")

    report = mhw.run_watch("mytab0r/edge-harness", now)

    assert any("#555" in line and "улика" in line for line in report)
    assert posted_comments and posted_comments[0][0] == 555
