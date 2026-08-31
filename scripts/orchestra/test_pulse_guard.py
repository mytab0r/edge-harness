#!/usr/bin/env python3
"""Тесты предохранителя конвейера и пульса orchestra (scripts/orchestra/pulse_guard.py, #120).

Кормятся прод-формой: таймстампы и payloads — как их реально отдаёт GitHub API
(conclusion-строки, created_at с Z/.000Z/+00:00, workflow_runs/jobs). Проводка
conveyor_gate/heartbeat_check проверяется на моке gh — сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_pulse_guard.py -q
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("pulse_guard.py")
spec = importlib.util.spec_from_file_location("pulse_guard", SCRIPT)
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)  # type: ignore[union-attr]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def run(conclusion, created_at="2026-08-31T10:00:00Z", run_id=1, title="worker run"):
    """Прод-форма элемента workflow_runs (поля, которые читает модуль)."""
    return {
        "id": run_id,
        "conclusion": conclusion,
        "created_at": created_at,
        "html_url": f"https://github.com/mytab0r/edge-harness/actions/runs/{run_id}",
        "display_title": title,
    }


# ── Серия красных: подсчёт подряд ────────────────────────────────────────────────


@pytest.mark.parametrize("conclusions,expected", [
    ([], 0),
    (["success"], 0),
    (["failure"], 1),
    (["failure", "failure", "failure"], 3),
    (["failure", "cancelled", "failure"], 3),   # отмена — тоже не успех
    (["failure", "failure", "success", "failure"], 2),  # success сбрасывает серию
    (["success", "failure", "failure"], 0),     # самый новый зелёный — серия закрыта
    ([None, "failure", "failure"], 0),          # незавершённый прогон — решать рано
    (["failure", None, "failure"], 1),          # серия прервана незавершённым
])
def test_count_consecutive_failures(conclusions, expected):
    assert pg.count_consecutive_failures(conclusions) == expected


@pytest.mark.parametrize("failures,allowed", [
    (0, True), (2, True),
    (3, False), (4, False), (10, False),
])
def test_decide_dispatch_threshold_is_single_constant(failures, allowed):
    assert pg.decide_dispatch(failures) is allowed
    # порог — аргумент по умолчанию из одной константы: сменили константу —
    # сменилось решение, второй копии порога в коде быть не должно
    assert pg.decide_dispatch(pg.WORKER_FAILURE_PAUSE_AFTER - 1) is True
    assert pg.decide_dispatch(pg.WORKER_FAILURE_PAUSE_AFTER) is False


# ── «Один сигнал на серию»: маркер живёт до следующего success ───────────────────


M = lambda h: utc(2026, 8, 31, h)  # noqa: E731


def test_notification_pending_when_no_marker_yet():
    assert pg.pause_notification_pending([], utc(2026, 8, 31, 9)) is True


def test_notification_silent_while_marker_newer_than_success():
    # маркер (10:00) оставлен позже последнего success (09:00) — серия та же
    assert pg.pause_notification_pending([M(10)], M(9)) is False
    assert pg.pause_notification_pending([M(8), M(10)], M(9)) is False


def test_notification_fires_again_after_success_resets_series():
    # пульсы успели восстановиться (success 12:00 новее маркера 10:00) и снова
    # упали — сигнал обязан прозвучать заново
    assert pg.pause_notification_pending([M(10)], M(12)) is True


def test_notification_silent_without_any_success_once_marker_exists():
    # успехов нет вовсе: серия бесконечна, живого маркера достаточно
    assert pg.pause_notification_pending([M(10)], None) is False


# ── Пульс orchestra: возраст против порога, прод-формы таймстампов ───────────────


def test_parse_prod_timestamp_forms():
    for raw in ("2026-08-31T11:00:00Z", "2026-08-31T11:00:00.000Z", "2026-08-31T11:00:00+00:00"):
        assert pg.heartbeat_age_minutes(raw, utc(2026, 8, 31, 11, 30)) == 30.0


def test_decide_heartbeat_within_threshold():
    # ровно 45 минут — ещё не «старше»; пульс в норме
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 45)) == "ok"
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 15)) == "ok"


def test_decide_heartbeat_stale_beyond_three_intervals():
    # 46 минут > 45 = 3 интервала по 15 — пульсы пропадали
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 46)) == "stale"
    assert pg.decide_heartbeat(utc(2026, 8, 31, 10, 0), utc(2026, 8, 31, 11, 0)) == "stale"


# ── Тексты сигналов: маркеры, улики, путь возобновления ──────────────────────────


def test_pause_alert_carries_marker_evidence_and_resume():
    text = pg.pause_alert_text(3, run("failure", run_id=42, title="worker: задача"),
                               "task — шаги: Задача через DSH headless")
    assert pg.PAUSE_MARKER in text                       # маркер, по которому ищется «уже оповещено»
    assert "3 красных прогонов" in text
    assert str(pg.WORKER_FAILURE_PAUSE_AFTER) in text    # порог назван в тексте
    assert "actions/runs/42" in text                     # улика — ссылка на прогон
    assert "Задача через DSH headless" in text           # последняя ошибка
    assert "сбрасывает счётчик" in text                  # как снять паузу


def test_heartbeat_alert_carries_marker_evidence_and_threshold():
    text = pg.heartbeat_alert_text(61.0, run("success", run_id=7))
    assert pg.HEARTBEAT_MARKER in text
    assert "61 мин" in text
    assert str(pg.HEARTBEAT_MAX_AGE_MINUTES) in text
    assert "actions/runs/7" in text
    assert "внешний монитор" in text  # честный пробел: полный охват отложен


# ── Причина последнего красного: прод-форма jobs ─────────────────────────────────


JOBS_PAYLOAD = {
    "total_count": 2,
    "jobs": [
        {"name": "worker", "conclusion": "success", "steps": []},
        {"name": "task", "conclusion": "failure", "steps": [
            {"name": "Git-авторизация", "conclusion": "success"},
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ],
}


def test_last_failure_error_names_failed_job_and_steps(monkeypatch):
    monkeypatch.setattr(pg, "gh", lambda *a: JOBS_PAYLOAD)
    text = pg.last_failure_error("o/r", {"id": 42, "conclusion": "failure"})
    assert "task" in text and "Задача через DSH headless" in text
    assert "Git-авторизация" not in text  # зелёные шаги не шумят


def test_last_failure_error_loud_when_details_unavailable(monkeypatch):
    def broken(*a):
        raise RuntimeError("gh api: 502")
    monkeypatch.setattr(pg, "gh", broken)
    assert "детали недоступны" in pg.last_failure_error("o/r", {"id": 1})


# ── Проводка: gate останавливает диспатч, heartbeat кричит ───────────────────────


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов пишется."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


NOW = utc(2026, 8, 31, 12, 0)
RECENT_FAILURES = {"workflow_runs": [
    run("failure", "2026-08-31T11:50:00Z", 3),
    run("failure", "2026-08-31T11:35:00Z", 2),
    run("failure", "2026-08-31T11:20:00Z", 1),
]}
RECENT_OK = {"workflow_runs": [
    run("success", "2026-08-31T11:50:00Z", 9),
    run("failure", "2026-08-31T11:35:00Z", 3),
    run("failure", "2026-08-31T11:20:00Z", 2),
]}


def test_gate_blocks_dispatch_after_streak_and_notifies_once(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [],          # маркеров ещё нет
    })
    posted = []
    sent = []
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)

    lines, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False                # диспатч остановлен
    assert any("паузе" in line for line in lines)
    assert len(posted) == 1 and len(sent) == 1
    assert pg.PAUSE_MARKER in posted[0] and "actions/runs/3" in sent[0]

    # второй пульс той же серии: молчит (не спамит), диспатч всё ещё закрыт
    # (формат ответа issues/{N}/comments — голый массив, как у GitHub API)
    fake.routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:59:00Z", "body": f"x {pg.PAUSE_MARKER}"}]
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: posted.append("spam"))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append("spam") or True)
    _, allowed2 = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed2 is False
    assert posted == [posted[0]] and sent == [sent[0]]


def test_gate_allows_dispatch_when_series_reset_by_success(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_OK,
        "issues/120/comments": [
            {"created_at": "2026-08-31T10:00:00Z", "body": pg.PAUSE_MARKER}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    lines, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert any("разрешён" in line for line in lines)


def test_heartbeat_ok_is_quiet_and_stale_cries(monkeypatch):
    ok_runs = {"workflow_runs": [run("success", "2026-08-31T11:50:00Z", 5)]}
    fake = FakeGh({"workflows/orchestra.yml/runs": ok_runs, "issues/120/comments": []})
    monkeypatch.setattr(pg, "gh", fake)
    sent = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)
    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 8, 31, 12, 0))
    assert sent == [] and any("в норме" in line for line in lines)

    # последний успех 60 мин назад (порог 45) — опоздавший запуск кричит
    fake.routes["workflows/orchestra.yml/runs"] = {
        "workflow_runs": [run("failure", "2026-08-31T11:59:00Z", 6),
                          run("success", "2026-08-31T11:00:00Z", 5)]}
    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 8, 31, 12, 1))
    assert len(sent) == 1 and "пропадал" in sent[0]
    assert any("пропадал" in line for line in lines)
