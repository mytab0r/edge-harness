#!/usr/bin/env python3
"""Тесты сторожа квот (scripts/measure/quota_watch.py, #605).

Запуск: python -m pytest scripts/measure/test_quota_watch.py -q
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quota_watch.py")
spec = importlib.util.spec_from_file_location("quota_watch", SCRIPT)
qw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qw)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


# ── resource_key: сопоставление отображаемого имени и ключа quotas.LIMITS ──


@pytest.mark.parametrize("label,expected", [
    ("DO rows_read/сутки", "cf_do_rows_read_day"),
    ("DO rows_written/сутки", "cf_do_rows_written_day"),
    ("Workers requests/сутки", "cf_workers_requests_day"),
    ("DO storage/аккаунт", "cf_do_storage_account_bytes"),
    ("Диспатчи этого репо/час (приближение к вторичному лимиту 500/час, аккаунт-wide)", "gh_dispatch_hour"),
    ("In-progress workflow runs этого репо (приближение к concurrency 20, аккаунт-wide)", "gh_concurrent_jobs"),
])
def test_resource_key_matches_by_substring(label, expected):
    assert qw.resource_key(label) == expected


def test_resource_key_none_for_unknown_label():
    assert qw.resource_key("Actions минуты (billing)") is None


# ── recent_run_within: троттлинг ───────────────────────────────────────────


def test_recent_run_within_true_for_fresh_run(monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(qw.pulse_guard, "gh", lambda *a: {"workflow_runs": [{"created_at": fresh}]})
    assert qw.recent_run_within(REPO, "quota-watch.yml", 15.0) is True


def test_recent_run_within_false_for_old_run(monkeypatch):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(qw.pulse_guard, "gh", lambda *a: {"workflow_runs": [{"created_at": old}]})
    assert qw.recent_run_within(REPO, "quota-watch.yml", 15.0) is False


def test_recent_run_within_false_when_no_runs(monkeypatch):
    monkeypatch.setattr(qw.pulse_guard, "gh", lambda *a: {"workflow_runs": []})
    assert qw.recent_run_within(REPO, "quota-watch.yml", 15.0) is False


def test_recent_run_within_mutation_guard_failure_does_not_throttle(monkeypatch):
    """Находка класса: сбой самой проверки истории — не повод молчать о
    квоте. Мутация: сделай эту функцию возвращать True при исключении —
    прогон quota_watch будет пропускать проверки квоты каждый раз, когда
    GitHub API истории прогонов недоступен."""
    def broken(*a):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(qw.pulse_guard, "gh", broken)
    assert qw.recent_run_within(REPO, "quota-watch.yml", 15.0) is False


# ── cheap_check: проводка на do_rows_read + quota_alert ────────────────────


def test_cheap_check_computes_pct_and_calls_alert(monkeypatch):
    monkeypatch.setattr(qw.do_rows_read, "today_rows_read", lambda token, acct: 4_800_000)
    calls = []
    monkeypatch.setattr(qw.quota_alert, "check_and_alert",
                         lambda repo, key, label, current, limit, pct, threshold=None:
                             calls.append((key, label, current, limit, pct, threshold)) or "ok")

    result = qw.cheap_check(REPO, "tok", "acct")

    assert result == "ok"
    assert calls == [("cf_do_rows_read_day", "DO rows_read/сутки", 4_800_000, qw.do_rows_read.DAILY_LIMIT, 96.0,
                       qw.quotas.THRESHOLD_PCT)]


def test_cheap_check_threshold_tracks_quotas_single_source_of_truth(monkeypatch):
    """Порог не второй независимый литерал: правка quotas.THRESHOLD_PCT
    обязана долететь до вызова check_and_alert без правки quota_watch.py
    (found: ревью PR #607 — раньше был захардкожен дефолт 80.0 отдельно)."""
    monkeypatch.setattr(qw.do_rows_read, "today_rows_read", lambda token, acct: 4_800_000)
    monkeypatch.setattr(qw.quotas, "THRESHOLD_PCT", 55.0)
    calls = []
    monkeypatch.setattr(qw.quota_alert, "check_and_alert",
                         lambda repo, key, label, current, limit, pct, threshold=None:
                             calls.append(threshold) or "ok")

    qw.cheap_check(REPO, "tok", "acct")

    assert calls == [55.0]


def test_cheap_check_measurement_failure_is_not_fatal(monkeypatch):
    def broken(token, acct):
        raise RuntimeError("HTTP 500: transient")
    monkeypatch.setattr(qw.do_rows_read, "today_rows_read", broken)
    result = qw.cheap_check(REPO, "tok", "acct")
    assert result == ""


# ── full_sweep: переиспользует collect_cloudflare/collect_github, дедуп общий ──


def test_full_sweep_shares_resource_key_with_cheap_check(monkeypatch):
    """Ключ ресурса rows_read у full_sweep — ТОТ ЖЕ, что у cheap_check
    (см. докстринг модуля, «Эскалация и автозадача») — иначе два независимых
    пути дублировали бы алерт на один и тот же ресурс."""
    row = qw.quotas.Row("DO rows_read/сутки", "Cloudflare GraphQL Analytics",
                         4_800_000, 5_000_000, "rows", "00:00 UTC", "ok")
    monkeypatch.setattr(qw.quotas, "collect_cloudflare", lambda *a: [row])
    monkeypatch.setattr(qw.quotas, "collect_github", lambda *a: [])
    calls = []
    monkeypatch.setattr(qw.quota_alert, "check_and_alert",
                         lambda repo, key, label, current, limit, pct, threshold=None: calls.append(key) or "ok")

    qw.full_sweep(REPO, "tok", "acct")

    assert calls == [qw.ROWS_READ_KEY]


def test_full_sweep_skips_rows_without_key_or_pct(monkeypatch):
    unmatched = qw.quotas.Row("LLM-провайдер квота", "нет API", None, None, "-", "-", "no-data", "нет API")
    no_pct = qw.quotas.no_data("DO rows_read/сутки", "s", 5_000_000, "rows", "секрет не задан")
    monkeypatch.setattr(qw.quotas, "collect_cloudflare", lambda *a: [no_pct])
    monkeypatch.setattr(qw.quotas, "collect_github", lambda *a: [unmatched])
    calls = []
    monkeypatch.setattr(qw.quota_alert, "check_and_alert", lambda *a: calls.append(1) or "ok")

    qw.full_sweep(REPO, "tok", "acct")

    assert calls == []


def test_full_sweep_noop_without_credentials():
    assert qw.full_sweep(REPO, "", "") == []


# ── main(): проводка троттлинга/дешёвой/полной проверки ────────────────────


def _patch_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")


def test_main_returns_0_without_credentials(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    assert qw.main() == 0


def test_main_skips_when_throttled(monkeypatch):
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "recent_run_within", lambda *a: True)
    calls = []
    monkeypatch.setattr(qw, "cheap_check", lambda *a: calls.append(1) or "")
    assert qw.main() == 0
    assert calls == []


def test_main_runs_cheap_check_when_not_throttled(monkeypatch):
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "recent_run_within", lambda *a: False)
    calls = []
    monkeypatch.setattr(qw, "cheap_check", lambda *a: calls.append(1) or "cf_do_rows_read_day: без изменений (ok, 1.0%) — сигнал не отправлен (дедуп)")

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)  # minute=30 — вне окна полного среза
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.main() == 0
    assert calls == [1]


def test_main_also_runs_full_sweep_inside_first_quarter_hour(monkeypatch):
    """Полный срез — только внутри первой четверти часа (см. докстринг,
    «Стоимость и троттлинг»): минута < FULL_SWEEP_MINUTE_WINDOW."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "recent_run_within", lambda *a: False)
    monkeypatch.setattr(qw, "cheap_check", lambda *a: "")
    sweep_calls = []
    monkeypatch.setattr(qw, "full_sweep", lambda *a: sweep_calls.append(1) or [])

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 5, tzinfo=timezone.utc)  # minute=5 — внутри окна
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.main() == 0
    assert sweep_calls == [1]


def test_main_exits_nonzero_when_alert_channel_fails(monkeypatch):
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "recent_run_within", lambda *a: False)
    monkeypatch.setattr(qw, "cheap_check", lambda *a: "cf_do_rows_read_day: breach — Telegram: НЕ доставлен; след в #120: НЕ оставлен; x")

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.main() == 1
