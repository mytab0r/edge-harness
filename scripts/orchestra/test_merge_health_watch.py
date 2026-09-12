#!/usr/bin/env python3
"""Тесты детектора регрессии здоровья конвейера, привязанного к слиянию
(scripts/orchestra/merge_health_watch.py, issue #967).

Ревизия 2 (критик, отдельная сессия ревью PR #970): мотивирующее
утверждение про `pulse_guard` было ложным (предохранитель СРАБОТАЛ и был
трижды погашен `RESUME_MARKER` — см. докстринг модуля), а пороги v1
(относительное отклонение recent-окна от соседнего baseline) давали 17
ложных срабатываний из 41 здорового часа. Обе правки — в модуле; здесь —
фикстуры, исправленные до полноты (91 прогон, не 87; 15 подозреваемых в
16-часовом окне, не 6 в старом 12-часовом), и тесты нового, эмпирически
проверенного алгоритма (абсолютный порог recent-rate, две страховки —
дедуп по фиксированному отпечатку + суточный потолок).

Кормится прод-формой: фикстура ниже — ДОСЛОВНЫЙ вывод `gh api
repos/mytab0r/edge-harness/actions/workflows/worker.yml/runs` за
2026-09-07T18:25Z..2026-09-11T04:54Z (91 прогон, снято 2026-09-11 повторно
после находки критика о неполноте v1-фикстуры) и `search/issues` за ту же
ночь (снято 2026-09-11).

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


# ── evaluate: абсолютный порог, три честных исхода ──────────────────────────


def test_evaluate_insufficient_data_below_min_recent():
    now = utc("2026-09-11T00:00:00Z")
    runs = [run("2026-09-10T23:00:00Z", "success")] * 2  # 2 < MIN_SAMPLES_RECENT(5)
    v = mhw.evaluate(runs, now)
    assert v.status == "insufficient_data"
    assert "recent=2" in v.reason


def test_evaluate_ok_above_fire_threshold():
    now = utc("2026-09-11T00:00:00Z")
    recent = [run(f"2026-09-10T{h:02d}:00:00Z", "success" if i % 2 else "failure")
              for i, h in enumerate(range(17, 23))]  # 50% — выше FIRE_RATE_THRESHOLD_PCT(10%)
    v = mhw.evaluate(recent, now)
    assert v.status == "ok"
    assert v.recent_rate == 50.0


def test_evaluate_fire_at_or_below_threshold_mutation():
    """Мутационная граница: `<=`, не `<` — recent_rate ровно на пороге (10%)
    тоже `fire`, не `ok`. Десять прогонов (9 отказов + 1 успех), все внутри
    8-часового окна [now-8ч, now)."""
    now = utc("2026-09-11T00:00:00Z")
    recent = [run("2026-09-10T16:00:00Z", "failure"), run("2026-09-10T16:48:00Z", "failure"),
              run("2026-09-10T17:36:00Z", "failure"), run("2026-09-10T18:24:00Z", "failure"),
              run("2026-09-10T19:12:00Z", "failure"), run("2026-09-10T20:00:00Z", "failure"),
              run("2026-09-10T20:48:00Z", "failure"), run("2026-09-10T21:36:00Z", "failure"),
              run("2026-09-10T22:24:00Z", "failure"), run("2026-09-10T23:12:00Z", "success")]
    v = mhw.evaluate(recent, now)
    assert v.recent_n == 10
    assert v.recent_rate == 10.0
    assert v.status == "fire"


def test_evaluate_fire_at_full_collapse():
    now = utc("2026-09-11T00:00:00Z")
    recent = [run(f"2026-09-10T{h:02d}:00:00Z", "failure") for h in range(17, 23)]
    v = mhw.evaluate(recent, now)
    assert v.recent_rate == 0.0
    assert v.status == "fire"


def test_evaluate_baseline_is_context_only_not_decision():
    """Baseline может быть сколь угодно плохим или отсутствовать — решение
    fire/ok зависит ТОЛЬКО от recent (снимает самозаглушение относительного
    дизайна v1, см. докстринг модуля «Ревизия 2»)."""
    now = utc("2026-09-11T00:00:00Z")
    recent = [run(f"2026-09-10T{h:02d}:00:00Z", "success") for h in range(17, 23)]  # 100%, ok
    v_no_baseline = mhw.evaluate(recent, now)  # ни одного прогона в baseline-окне вовсе
    assert v_no_baseline.status == "ok"
    assert v_no_baseline.baseline_rate is None
    assert v_no_baseline.baseline_n == 0


def test_evaluate_baseline_truncated_flag_on_full_page():
    """Страница Actions API заполнена целиком (`RUNS_FETCH_LIMIT`) И самый
    старый полученный прогон новее начала baseline-окна — baseline честно
    помечен усечённым, decision (fire/ok) при этом не портится (baseline не
    решающий фактор)."""
    now = utc("2026-09-11T00:00:00Z")
    recent = [run(f"2026-09-10T{h:02d}:00:00Z", "failure") for h in range(17, 23)]
    # RUNS_FETCH_LIMIT(100) записей, самая старая — куда моложе, чем требует
    # BASELINE_LOOKBACK_HOURS(72) до начала recent-окна (10-минутный шаг —
    # хватает записей на сутки с запасом).
    padding = [run(f"2026-09-09T{(h * 10) // 60:02d}:{(h * 10) % 60:02d}:00Z", "success")
               for h in range(100 - len(recent))]
    runs = recent + padding
    assert len(runs) == mhw.RUNS_FETCH_LIMIT
    v = mhw.evaluate(runs, now)
    assert v.baseline_truncated is True


# ── Доказательство на реальной истории (issue #967, живой корень #878) ─────
#
# Дословный вывод `gh api repos/mytab0r/edge-harness/actions/workflows/
# worker.yml/runs` (снято 2026-09-11, ИСПРАВЛЕНО после находки критика о
# неполноте v1: 91 прогон, не 87 — четыре success 2026-09-07 отсутствовали
# из-за ошибки диапазона запроса, не выбраны намеренно), окно
# 2026-09-07T18:25:04Z..2026-09-11T04:04:32Z (78ч до слияния PR #878 + 6ч
# после).
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
    run("2026-09-08T01:40:37Z", "success"), run("2026-09-07T23:26:08Z", "success"),
    run("2026-09-07T22:16:52Z", "success"), run("2026-09-07T20:26:01Z", "success"),
    run("2026-09-07T18:25:04Z", "success"),
]


def test_real_history_fixture_is_complete_91_runs():
    """Полнота фикстуры — проверяемый факт, не заявление (критик поймал v1
    на неполной фикстуре 87/91, заявившей о полноте на слово)."""
    assert len(REAL_WORKER_RUNS_AROUND_878) == 91


@pytest.mark.parametrize("hours_after,expected_status", [
    (0, "insufficient_data"), (1, "insufficient_data"), (2, "insufficient_data"),
    (3, "insufficient_data"), (4, "insufficient_data"),
    (5, "ok"), (6, "ok"),
    (7, "fire"), (8, "fire"),
])
def test_real_history_878_regression_detected_within_hours(hours_after, expected_status):
    """ГЛАВНОЕ доказательство issue #967 (пороги — ревизия 2, эмпирические):
    на РЕАЛЬНОЙ истории детектор впервые кричит `fire` через 7ч после
    слияния PR #878 (22:54:34Z → 05:54:34Z) — часы, не сутки. Медленнее, чем
    заявляла v1 (5ч), но с многократно лучшим соотношением сигнал/шум (см.
    design.md §4 — независимая проверка критика: относительный порог v1 давал
    17 ложных из 41 здорового часа, этот — 6 из 119)."""
    now = utc(MERGE_878_AT) + timedelta(hours=hours_after)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.status == expected_status


def test_real_history_878_fire_verdict_numbers():
    now = utc(MERGE_878_AT) + timedelta(hours=7)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.status == "fire"
    assert v.recent_rate == 0.0
    assert v.recent_n == 5


def test_real_history_baseline_context_matches_manual_count():
    """Baseline-КОНТЕКСТ (не решение) при now=слияние+8ч (recent-окно
    заканчивается ровно на слиянии — baseline = «72ч до слияния») —
    независимо посчитан `gh api` вручную (issue #967, находка критика:
    83/48.2%, не 82/47.6% как в неполной v1-фикстуре) — сходится с модулем."""
    now = utc(MERGE_878_AT) + timedelta(hours=8)
    v = mhw.evaluate(REAL_WORKER_RUNS_AROUND_878, now)
    assert v.baseline_n == 83
    assert v.baseline_rate == 48.2


# ── suspects_from_search: несколько подозреваемых честно, без угадывания ───


def _merged_item(number, title, merged_at):
    return {"number": number, "title": title, "pull_request": {"merged_at": merged_at}}


# Дословный вывод `gh api "search/issues?q=repo:mytab0r/edge-harness+is:pr+
# is:merged+merged:2026-09-10T13:54:34..2026-09-11T05:54:34"` (снято
# 2026-09-11, окно атрибуции 16ч перед now=слияние+7ч, только релевантные
# поля) — ИСПРАВЛЕНО после находки критика: v1 заявляла «шесть», реальный
# ответ на эквивалентное окно — ПЯТНАДЦАТЬ.
REAL_MERGED_PRS_AROUND_878 = {
    "total_count": 15, "incomplete_results": False,
    "items": [
        _merged_item(922, "Bump actions/upload-artifact from 4 to 7", "2026-09-11T03:42:50Z"),
        _merged_item(921, "Bump @cloudflare/vitest-plugin from 1.1.3 to 1.1.5", "2026-09-11T03:48:11Z"),
        _merged_item(903, "#899: гонка created_at/completion в conveyor_gate", "2026-09-10T22:00:32Z"),
        _merged_item(895, "#882: авторизация push снимков здоровья", "2026-09-10T21:12:04Z"),
        _merged_item(893, "#891: автоуборка worktree'ов", "2026-09-10T23:33:51Z"),
        _merged_item(890, "#884: эскалация ai-review о крупном PR", "2026-09-10T21:22:54Z"),
        _merged_item(878, "#876: успех воркера требует rc/провайдера/новых коммитов", "2026-09-10T22:54:34Z"),
        _merged_item(872, "#871: session.rename переживает испорченную холодную загрузку", "2026-09-10T15:42:32Z"),
        _merged_item(862, "#860: Claude-пул только в worker/hands", "2026-09-11T01:41:27Z"),
        _merged_item(771, "#749: каталог гвардий scripts/ci/guards", "2026-09-10T20:33:07Z"),
        _merged_item(640, "#635: гвардия скана .github/workflows", "2026-09-10T22:06:39Z"),
        _merged_item(617, "#614: deploy-worker — автооткат прода при красной канарейке", "2026-09-11T05:41:24Z"),
        _merged_item(412, "feat: agents-tasks plugin", "2026-09-11T02:46:50Z"),
        _merged_item(409, "#389: инбокс создаёт issues без нового секрета", "2026-09-11T03:37:23Z"),
        _merged_item(344, "#341: аудит нативных возможностей GitHub/Cloudflare", "2026-09-11T04:26:40Z"),
    ],
}


def test_suspects_from_search_real_history_exact_set_and_order():
    """Окно атрибуции 16ч перед now=+7ч после слияния #878 (2026-09-11T05:54:34Z)
    накрывает ПЯТНАДЦАТЬ реальных слияний той ночи (включая #617, слитый
    05:41:24Z — до `now`, значит внутри окна) — детектор перечисляет все,
    хронологически по времени слияния, не угадывает одно «самое вероятное»
    (design.md, «Корреляция, не причинность»)."""
    now = utc(MERGE_878_AT) + timedelta(hours=7)  # 2026-09-11T05:54:34Z
    suspects, truncated = mhw.suspects_from_search(REAL_MERGED_PRS_AROUND_878, now)
    assert not truncated
    numbers = [s.number for s in suspects]
    assert numbers == [872, 771, 895, 890, 903, 640, 878, 893, 862, 412, 409, 922, 921, 344, 617]


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
    v = mhw.Verdict("fire", 0.0, 5, 47.6, 82, False, "2026-09-11T04:54:34+00:00")
    text = mhw.render_report(v, [], False)
    assert "не найдено" in text
    assert "внешней причиной" in text


def test_render_report_multiple_suspects_lists_each_with_number():
    v = mhw.Verdict("fire", 0.0, 5, 47.6, 82, False, "2026-09-11T04:54:34+00:00")
    suspects = [mhw.Suspect(640, "a", "2026-09-10T22:06:39Z"),
                mhw.Suspect(878, "b", "2026-09-10T22:54:34Z")]
    text = mhw.render_report(v, suspects, False)
    assert "Подозреваемых 2" in text
    assert "#640" in text and "#878" in text
    assert "НЕ причинность" in text


def test_render_report_ok_does_not_claim_recovery_of_root_cause():
    v = mhw.Verdict("ok", 90.0, 6, 47.6, 82, False, "2026-09-11T04:54:34+00:00")
    text = mhw.render_report(v, [], False)
    assert "ok" in text
    assert "90.0" in text


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
    recent = [run("2026-09-08T20:00:00Z", "success"),
              run("2026-09-08T21:00:00Z", "success"),
              run("2026-09-08T21:30:00Z", "success"),
              run("2026-09-08T22:00:00Z", "success"),
              run("2026-09-08T22:30:00Z", "failure")]
    fake = FakeGh({"actions/workflows/worker.yml/runs": {"workflow_runs": recent}})
    monkeypatch.setattr(mhw.pulse_guard, "gh", fake)
    report = mhw.run_watch("mytab0r/edge-harness", now)
    assert any("ok" in line for line in report)
    assert not any("search/issues" in call for call in fake.calls)  # подозреваемых не искали вовсе


def test_run_watch_fire_creates_task_and_escalates(monkeypatch):
    now = utc(MERGE_878_AT) + timedelta(hours=7)
    fake = FakeGh({
        "actions/workflows/worker.yml/runs": {"workflow_runs": REAL_WORKER_RUNS_AROUND_878},
        "search/issues": REAL_MERGED_PRS_AROUND_878,
        "issues?state=open&labels=area%3Aprocess": [],  # нет открытой задачи — заводим новую
        "issues?state=all&labels=area%3Aprocess": [],  # потолок суток не исчерпан
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


def test_run_watch_fire_same_day_dedup_comments_once(monkeypatch):
    """Фиксированный отпечаток (не по часу, см. `_fingerprint_line`) — второй
    прогон того же календарного дня комментирует существующую задачу, но
    только ОДИН раз в сутки (issue_marker_times дедупит по `_evidence_marker`)."""
    now = utc(MERGE_878_AT) + timedelta(hours=7)
    existing_issue = {"number": 555, "body": f"...\n{mhw._fingerprint_line()}\n..."}
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
    monkeypatch.setattr(mhw.pulse_guard, "issue_marker_times", lambda repo, num, marker: [])

    report = mhw.run_watch("mytab0r/edge-harness", now)

    assert any("#555" in line and "новая улика" in line for line in report)
    assert posted_comments and posted_comments[0][0] == 555


def test_run_watch_fire_dedup_skips_second_evidence_same_day(monkeypatch):
    now = utc(MERGE_878_AT) + timedelta(hours=7)
    existing_issue = {"number": 555, "body": f"...\n{mhw._fingerprint_line()}\n..."}
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
    # улика за сегодня уже стоит — дедуп молчит
    monkeypatch.setattr(mhw.pulse_guard, "issue_marker_times",
                        lambda repo, num, marker: [now])

    report = mhw.run_watch("mytab0r/edge-harness", now)

    assert any("уже оставлена" in line for line in report)
    assert not posted_comments


def test_run_watch_fire_daily_cap_exhausted_escalates_without_new_task(monkeypatch):
    now = utc(MERGE_878_AT) + timedelta(hours=7)
    fake = FakeGh({
        "actions/workflows/worker.yml/runs": {"workflow_runs": REAL_WORKER_RUNS_AROUND_878},
        "search/issues": REAL_MERGED_PRS_AROUND_878,
        "issues?state=open&labels=area%3Aprocess": [],
        # MERGE_HEALTH_DAILY_CAP(3) уже исчерпан задачами ЭТОГО механизма
        "issues?state=all&labels=area%3Aprocess": [
            {"created_at": (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z"),
             "body": mhw._fingerprint_line()}
            for h in (1, 2, 3)
        ],
    })
    monkeypatch.setattr(mhw.pulse_guard, "gh", fake)
    created = []
    monkeypatch.setattr(mhw.pool_issue, "create_pool_issue",
                        lambda *a, **k: created.append(1) or {"number": 1})
    escalated = []
    monkeypatch.setattr(mhw.pulse_guard, "escalate",
                        lambda repo, num, text, options=None: escalated.append(text) or "ok")

    report = mhw.run_watch("mytab0r/edge-harness", now)

    assert not created
    assert any("потолок исчерпан" in line for line in report)
    assert len(escalated) == 1
