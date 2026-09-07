#!/usr/bin/env python3
"""Тесты дедуп-эскалации и автозаведения задачи на переходе через порог
квоты (scripts/measure/quota_alert.py, #605).

Сетевые вызовы (pulse_guard.gh/escalate/post_issue_comment, subprocess.run
для scripts/gh/issue-create) подменяются monkeypatch — тот же приём, что
scripts/orchestra/test_pulse_guard.py/scripts/measure/test_quotas.py.

Запуск: python -m pytest scripts/measure/test_quota_alert.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quota_alert.py")
spec = importlib.util.spec_from_file_location("quota_alert", SCRIPT)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


def _comment(body: str, created_at: str = "2026-09-06T18:00:00Z") -> dict:
    return {"body": body, "created_at": created_at}


# ── last_state: разбор маркера ────────────────────────────────────────────


def test_last_state_none_when_no_marker(monkeypatch):
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)


def test_last_state_reads_breach_with_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "breach", 999)
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [_comment(f"текст\n{marker}")])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("breach", 999)


def test_last_state_reads_ok_without_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "ok", None)
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [_comment(f"текст\n{marker}")])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_picks_the_latest_marker_not_the_first(monkeypatch):
    """Два маркера одного ресурса — состояние решает САМЫЙ СВЕЖИЙ по времени,
    не первый в списке (порядок ответа GitHub не гарантирован хронологией)."""
    older = _comment(qa.state_marker("cf_do_rows_read_day", "breach", 1), "2026-09-06T10:00:00Z")
    newer = _comment(qa.state_marker("cf_do_rows_read_day", "ok", None), "2026-09-06T20:00:00Z")
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [older, newer])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_ignores_marker_of_a_different_resource(monkeypatch):
    """Маркер другого ресурса не должен путаться с искомым — иначе состояние
    одного ресурса решалось бы по эскалации совсем другого (независимость
    дедупа по ресурсам, см. докстринг модуля)."""
    other = _comment(qa.state_marker("cf_workers_requests_day", "breach", 1))
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [other])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)


# ── check_and_alert: дедуп по переходу ────────────────────────────────────


def _no_prior_state(monkeypatch):
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (None, None))


def test_first_observation_breach_alerts_and_creates_task(monkeypatch):
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (1234, "задача заведена"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append(text) or "Telegram: доставлен; след в #120: оставлен")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_487_640, 5_000_000, 149.8)

    assert len(escalated) == 1
    assert "#1234" in escalated[0]
    assert "breach" in escalated[0].lower() or "перевалила" in escalated[0]
    assert "issue=#1234" in escalated[0]
    assert "breach" in result


def test_mutation_guard_breach_boundary_is_inclusive(monkeypatch):
    """Мутационная проверка (по образцу test_quotas.py): ровно на границе
    порога сигнал обязан сработать (>=, не >)."""
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (1, "ok"))
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: "escalated")
    result = qa.check_and_alert(REPO, "k", "метка", 80, 100, 80.0, threshold=80.0)
    assert "breach" in result


def test_no_change_does_not_alert_again(monkeypatch):
    """Порог держится (breach→breach) — второй алерт НЕ уходит: дедуп по
    переходу, не по каждому прогону."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", 1234))
    calls = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: calls.append(text) or "x")
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (None, "x"))

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_600_000, 5_000_000, 152.0)

    assert calls == []
    assert create_calls == []
    assert "без изменений" in result


def test_recovery_transition_alerts_without_creating_task(monkeypatch):
    """breach→ok: уведомление о восстановлении уходит, но НОВАЯ задача не
    заводится (см. докстринг модуля, «не закрывается автоматически»)."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", 1234))
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (999, "не должно вызываться"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "ok")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert create_calls == []
    assert len(escalated) == 1
    assert "#1234" in escalated[0]
    assert "issue=#1234" in escalated[0]
    assert "восстановилась" in escalated[0].lower() or "вернулась" in escalated[0].lower()
    assert "recovery" in result


def test_recovery_without_prior_issue_number_still_alerts(monkeypatch):
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", None))
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: "ok")
    result = qa.check_and_alert(REPO, "k", "метка", 10, 100, 10.0)
    assert "recovery" in result


# ── create_or_note_task: проводка на scripts/gh/issue-create ─────────────


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_create_task_parses_issue_number_from_url(monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run",
                         lambda *a, **k: _FakeResult(0, "https://github.com/mytab0r/edge-harness/issues/4242\n"))
    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_487_640, 5_000_000, 149.8, 80.0)
    assert number == 4242
    assert "заведена" in note


def test_create_task_duplicate_guard_comments_existing_instead_of_new(monkeypatch):
    """Гвардия дублей (#566) нашла уже открытую задачу — модуль не считает
    это отказом: комментирует найденную вместо второй задачи того же класса."""
    stderr = (
        "::error::issue-create: похожие ОТКРЫТЫЕ задачи пула уже есть "
        "(класс #566, живой случай #518/#547/#564):\n"
        "  #777 (score 0.91): Квота харнеса перевалила за 80.0%: DO rows_read/сутки — https://...\n"
    )
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _FakeResult(1, "", stderr))
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append((issue, text)))

    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_600_000, 5_000_000, 152.0, 80.0)

    assert number == 777
    assert len(posted) == 1 and posted[0][0] == 777
    assert "уже открыта" in note


def test_create_task_unrelated_failure_reports_none(monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _FakeResult(1, "", "::error::issue заводится без метки task"))
    number, note = qa.create_or_note_task(REPO, "x", "k", 1, 2, 50.0, 80.0)
    assert number is None
    assert "отказал" in note
