#!/usr/bin/env python3
"""Тесты сторожа квот (scripts/measure/quota_watch.py, #605).

Запуск: python -m pytest scripts/measure/test_quota_watch.py -q
"""

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quota_watch.py")
spec = importlib.util.spec_from_file_location("quota_watch", SCRIPT)
qw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qw)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runs_response(runs: list[dict]) -> dict:
    return {"workflow_runs": runs}


def _run(run_id: int, created_at: str) -> dict:
    return {"id": run_id, "created_at": created_at, "status": "completed"}


def _jobs_response(steps: list[dict]) -> dict:
    """Форма прод-ответа `gh api repos/{repo}/actions/runs/{id}/jobs` —
    один job, список шагов с `name`/`conclusion` (те поля, что читает
    _find_step_conclusion)."""
    return {"jobs": [{"id": 1, "name": "watch", "steps": steps}]}


def _step(name: str, conclusion: str | None) -> dict:
    return {"name": name, "conclusion": conclusion}


def _gh_router(runs_payload: dict, jobs_by_run: dict[int, dict]):
    """Фейковый pulse_guard.gh, различающий два реальных вызова
    last_real_measurement_age_minutes по форме аргументов, как это делает
    настоящий `gh api` CLI (первый аргумент "--method" — список прогонов;
    иначе — эндпоинт .../jobs с id прогона в пути)."""
    def fake_gh(*args):
        if args and args[0] == "--method":
            return runs_payload
        endpoint = args[0]
        m = re.search(r"/actions/runs/(\d+)/jobs", endpoint)
        assert m, f"неожиданный вызов gh(): {args!r}"
        return jobs_by_run[int(m.group(1))]
    return fake_gh


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


# ── last_real_measurement_age_minutes: троттлинг + простой замера,
# только реальный замер открывает окно (found: ревью PR #607) ──────────────


def test_last_real_measurement_age_minutes_finds_real_recent_measurement(monkeypatch):
    """Направление (в) мутационной проверки: только что выполнившийся
    РЕАЛЬНЫЙ замер по-прежнему обнаруживается — иначе фикс сломал бы защиту
    от лишних CF-вызовов, которую и вводит троттлинг (gate_main проверяет
    `age < CHECK_INTERVAL_MINUTES` на этом же результате)."""
    now = datetime.now(timezone.utc)
    runs = _runs_response([_run(9, _iso(now - timedelta(minutes=3)))])
    jobs = {9: _jobs_response([_step(qw.MEASURE_STEP_NAME, "success")])}
    monkeypatch.setattr(qw.pulse_guard, "gh", _gh_router(runs, jobs))
    age, api_ok = qw.last_real_measurement_age_minutes(REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, now, 15.0)
    assert api_ok is True
    assert age is not None and age < 15.0


def test_last_real_measurement_age_minutes_counts_failed_measurement_step(monkeypatch):
    """Замер, упавший ПОСЛЕ вызова Cloudflare (например, эскалация breach не
    доставлена), всё равно потратил CF-запрос — обязан троттлить так же, как
    успешный (см. докстринг модуля)."""
    now = datetime.now(timezone.utc)
    runs = _runs_response([_run(9, _iso(now - timedelta(minutes=3)))])
    jobs = {9: _jobs_response([_step(qw.MEASURE_STEP_NAME, "failure")])}
    monkeypatch.setattr(qw.pulse_guard, "gh", _gh_router(runs, jobs))
    age, api_ok = qw.last_real_measurement_age_minutes(REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, now, 15.0)
    assert (api_ok, age is not None) == (True, True)


def test_last_real_measurement_age_minutes_none_for_measurement_outside_lookback(monkeypatch):
    now = datetime.now(timezone.utc)
    runs = _runs_response([_run(9, _iso(now - timedelta(minutes=30)))])
    jobs = {9: _jobs_response([_step(qw.MEASURE_STEP_NAME, "success")])}
    monkeypatch.setattr(qw.pulse_guard, "gh", _gh_router(runs, jobs))
    age, api_ok = qw.last_real_measurement_age_minutes(REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, now, 15.0)
    assert (age, api_ok) == (None, True)


def test_last_real_measurement_age_minutes_none_when_no_runs(monkeypatch):
    monkeypatch.setattr(qw.pulse_guard, "gh", _gh_router(_runs_response([]), {}))
    age, api_ok = qw.last_real_measurement_age_minutes(
        REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, datetime.now(timezone.utc), 15.0)
    assert (age, api_ok) == (None, True)


def test_last_real_measurement_age_minutes_mutation_guard_failure_reports_api_not_ok(monkeypatch):
    """Сбой самой проверки истории — не повод молчать о квоте (троттлинг), но
    и не повод трактовать как «замер простаивал» (см. gate_main). Мутация:
    сделай эту функцию возвращать api_ok=True при исключении — gate_main
    станет либо слепо троттлить, либо ложно эскалировать простой на каждом
    транзитном сбое GitHub API."""
    def broken(*a):
        raise RuntimeError("HTTP 403")
    monkeypatch.setattr(qw.pulse_guard, "gh", broken)
    age, api_ok = qw.last_real_measurement_age_minutes(
        REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, datetime.now(timezone.utc), 15.0)
    assert (age, api_ok) == (None, False)


def test_last_real_measurement_age_minutes_none_despite_constant_activity_without_real_measurement(monkeypatch):
    """Мутационная проверка направления (а), находка ревью PR #607: череда
    ЗАВЕРШЁННЫХ прогонов — упавший ДО замера (тесты красные, гейт и замер
    оба skipped) и холостые (гейт сам решил не измерять, замер skipped) —
    НЕ должна открывать окно троттлинга. Старый `recent_run_within` смотрел
    только на факт «есть свежий completed-прогон» и в этом сценарии вернул
    бы True (слепой троттлинг именно в активные часы)."""
    now = datetime.now(timezone.utc)
    runs = _runs_response([
        _run(3, _iso(now - timedelta(minutes=1))),   # холостой: гейт сказал "нет"
        _run(2, _iso(now - timedelta(minutes=6))),   # упавший ДО замера (тесты красные)
        _run(1, _iso(now - timedelta(minutes=10))),  # холостой
    ])
    jobs = {
        3: _jobs_response([
            _step("Тесты сторожа квот", "success"),
            _step(qw.GATE_STEP_NAME, "success"),
            _step(qw.MEASURE_STEP_NAME, "skipped"),
        ]),
        2: _jobs_response([
            _step("Тесты сторожа квот", "failure"),
            _step(qw.GATE_STEP_NAME, "skipped"),
            _step(qw.MEASURE_STEP_NAME, "skipped"),
        ]),
        1: _jobs_response([
            _step("Тесты сторожа квот", "success"),
            _step(qw.GATE_STEP_NAME, "success"),
            _step(qw.MEASURE_STEP_NAME, "skipped"),
        ]),
    }
    monkeypatch.setattr(qw.pulse_guard, "gh", _gh_router(runs, jobs))

    age, api_ok = qw.last_real_measurement_age_minutes(REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, now, 15.0)
    assert (age, api_ok) == (None, True)


def test_last_real_measurement_age_minutes_stops_scanning_past_lookback(monkeypatch):
    """Сканирование останавливается на первом прогоне старше lookback —
    прогон ДО него (даже если реально измерил) не должен запрашиваться:
    экономия REST-вызовов при частых холостых прогонах."""
    now = datetime.now(timezone.utc)
    runs = _runs_response([
        _run(2, _iso(now - timedelta(minutes=20))),  # уже старше lookback=15
        _run(1, _iso(now - timedelta(minutes=25))),  # не должен запрашиваться вовсе
    ])
    calls = []

    def fake_gh(*args):
        if args and args[0] == "--method":
            return runs
        calls.append(args[0])
        raise AssertionError("jobs прогона старше lookback не должны запрашиваться")

    monkeypatch.setattr(qw.pulse_guard, "gh", fake_gh)
    age, api_ok = qw.last_real_measurement_age_minutes(REPO, "quota-watch.yml", qw.MEASURE_STEP_NAME, now, 15.0)
    assert (age, api_ok) == (None, True)
    assert calls == []


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


def test_full_sweep_skips_rows_without_key_or_pct(monkeypatch, capsys):
    unmatched = qw.quotas.Row("LLM-провайдер квота", "нет API", None, None, "-", "-", "no-data", "нет API")
    no_pct = qw.quotas.no_data("DO rows_read/сутки", "s", 5_000_000, "rows", "секрет не задан")
    monkeypatch.setattr(qw.quotas, "collect_cloudflare", lambda *a: [no_pct])
    monkeypatch.setattr(qw.quotas, "collect_github", lambda *a: [unmatched])
    calls = []
    monkeypatch.setattr(qw.quota_alert, "check_and_alert", lambda *a: calls.append(1) or "ok")

    qw.full_sweep(REPO, "tok", "acct")

    assert calls == []
    # Пропуск ГРОМКИЙ, не тихий continue (некритичное замечание ревью PR
    # #607): обе причины пропуска обязаны попасть в лог прогона.
    out = capsys.readouterr().out
    assert "LLM-провайдер квота" in out and "вне списка эскалируемых метрик" in out
    assert "DO rows_read/сутки" in out and "нет данных" in out and "секрет не задан" in out


def test_full_sweep_noop_without_credentials():
    assert qw.full_sweep(REPO, "", "") == []


# ── measure_main(): проводка дешёвой/полной проверки (троттлинг решает gate_main) ──


def _patch_env(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")


def test_measure_main_returns_0_without_credentials(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    assert qw.measure_main() == 0


def test_measure_main_runs_cheap_check(monkeypatch):
    _patch_env(monkeypatch)
    calls = []
    monkeypatch.setattr(qw, "cheap_check", lambda *a: calls.append(1) or "cf_do_rows_read_day: без изменений (ok, 1.0%) — сигнал не отправлен (дедуп)")

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)  # minute=30 — вне окна полного среза
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.measure_main() == 0
    assert calls == [1]


def test_measure_main_also_runs_full_sweep_inside_first_quarter_hour(monkeypatch):
    """Полный срез — только внутри первой четверти часа (см. докстринг,
    «Стоимость и троттлинг»): минута < FULL_SWEEP_MINUTE_WINDOW."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "cheap_check", lambda *a: "")
    sweep_calls = []
    monkeypatch.setattr(qw, "full_sweep", lambda *a: sweep_calls.append(1) or [])

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 5, tzinfo=timezone.utc)  # minute=5 — внутри окна
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.measure_main() == 0
    assert sweep_calls == [1]


def test_measure_main_exits_nonzero_when_alert_channel_fails(monkeypatch):
    _patch_env(monkeypatch)
    monkeypatch.setattr(qw, "cheap_check", lambda *a: "cf_do_rows_read_day: breach — Telegram: НЕ доставлен; след в #120: НЕ оставлен; x")

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 12, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(qw, "datetime", _Now)

    assert qw.measure_main() == 1


# ── gate_main(): троттлинг + громкий сигнал простоя замера ─────────────────


def test_gate_main_throttles_when_measured_recently(monkeypatch, tmp_path):
    """Направление (в): недавний реальный замер — гейт троттлит (proceed=false),
    закрывает эпизод простоя (система жива), не эскалирует."""
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(qw, "last_real_measurement_age_minutes", lambda *a, **k: (3.0, True))
    escalated = []
    monkeypatch.setattr(qw.pulse_guard, "escalate", lambda *a: escalated.append(a) or "x")
    closed = []
    monkeypatch.setattr(qw, "close_stale_episode_if_needed", lambda repo: closed.append(repo))

    assert qw.gate_main() == 0

    assert output_file.read_text(encoding="utf-8").strip() == "proceed=false"
    assert escalated == []
    assert closed == [REPO]


def test_gate_main_proceeds_and_closes_episode_when_measured_but_past_throttle_window(monkeypatch, tmp_path):
    """Замер найден (age=20 мин), но старше окна троттлинга (15 мин) —
    гейт пускает следующий замер (proceed=true) И считает систему живой
    (закрывает эпизод простоя, если он был), а не эскалирует."""
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(qw, "last_real_measurement_age_minutes", lambda *a, **k: (20.0, True))
    stale_calls = []
    monkeypatch.setattr(qw, "stale_alert", lambda repo, now: stale_calls.append(repo) or "x")
    closed = []
    monkeypatch.setattr(qw, "close_stale_episode_if_needed", lambda repo: closed.append(repo))

    assert qw.gate_main() == 0

    assert output_file.read_text(encoding="utf-8").strip() == "proceed=true"
    assert stale_calls == []
    assert closed == [REPO]


def test_gate_main_writes_proceed_true_when_no_recent_measurement(monkeypatch, tmp_path):
    """Направление (а): нет реального замера в окне троттлинга — гейт
    пропускает замер дальше (proceed=true), но это ЕЩЁ не значит простой
    (age is None здесь трактуется гейтом и как «нужно измерить», и — ниже —
    как повод проверить эпизод простоя; сам факт proceed=true не эскалирует
    сам по себе, эскалирует именно stale_alert при отсутствии измерения)."""
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(qw, "last_real_measurement_age_minutes", lambda *a, **k: (None, True))
    stale_calls = []
    monkeypatch.setattr(qw, "stale_alert", lambda repo, now: stale_calls.append(repo) or "замер простаивал — x")

    assert qw.gate_main() == 0

    assert output_file.read_text(encoding="utf-8").strip() == "proceed=true"
    assert stale_calls == [REPO]


def test_gate_main_skips_stale_check_on_api_failure(monkeypatch, tmp_path, capsys):
    """Сбой истории прогонов (api_ok=False) НЕ должен трактоваться как
    простой замера — иначе транзитный сбой инструмента дал бы ложную
    эскалацию простоя (см. докстринг модуля)."""
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(qw, "last_real_measurement_age_minutes", lambda *a, **k: (None, False))
    stale_calls = []
    monkeypatch.setattr(qw, "stale_alert", lambda repo, now: stale_calls.append(repo) or "x")

    assert qw.gate_main() == 0

    assert output_file.read_text(encoding="utf-8").strip() == "proceed=true"
    assert stale_calls == []
    assert "история прогонов недоступна" in capsys.readouterr().err


# ── stale_alert / close_stale_episode_if_needed: громкий канал простоя,
# эпизодный дедуп (found: ревью PR #607) ────────────────────────────────────


def test_stale_alert_escalates_on_first_observation(monkeypatch):
    """Направление (б): реального замера нет — сигнал уходит в ДОСТАВЛЯЮЩИЙ
    канал (pulse_guard.escalate: Telegram + след в #120), не только в лог."""
    monkeypatch.setattr(qw.pulse_guard, "issue_marker_times", lambda repo, issue, marker: [])
    escalated = []
    monkeypatch.setattr(qw.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append(text) or "Telegram: доставлен; след в #120: оставлен")

    result = qw.stale_alert(REPO, datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))

    assert len(escalated) == 1
    assert qw.STALE_MARKER in escalated[0]
    assert "доставлен" in result


def test_stale_alert_dedupes_within_same_open_episode(monkeypatch):
    """Эпизод уже открыт (маркер новее любого закрывающего) — повторный вызов
    НЕ шлёт второй алерт (тот же приём, что pulse_guard.heartbeat_check)."""
    open_time = datetime(2026, 9, 7, 11, 0, tzinfo=timezone.utc)

    def fake_marker_times(repo, issue, marker):
        return [open_time] if marker == qw.STALE_MARKER else []
    monkeypatch.setattr(qw.pulse_guard, "issue_marker_times", fake_marker_times)
    escalated = []
    monkeypatch.setattr(qw.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qw.stale_alert(REPO, datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))

    assert escalated == []
    assert "дедуп" in result


def test_stale_alert_reopens_after_episode_closed(monkeypatch):
    """Эпизод был закрыт (close новее open) — новый простой снова алертит."""
    def fake_marker_times(repo, issue, marker):
        if marker == qw.STALE_MARKER:
            return [datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)]
        return [datetime(2026, 9, 7, 10, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(qw.pulse_guard, "issue_marker_times", fake_marker_times)
    escalated = []
    monkeypatch.setattr(qw.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qw.stale_alert(REPO, datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc))

    assert len(escalated) == 1
    assert "простаивал" in result


def test_close_stale_episode_posts_resolved_marker(monkeypatch):
    monkeypatch.setattr(qw.pulse_guard, "issue_marker_times",
                         lambda repo, issue, marker: [datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)]
                         if marker == qw.STALE_MARKER else [])
    posted = []
    monkeypatch.setattr(qw.pulse_guard, "post_issue_comment", lambda repo, issue, text: posted.append(text))

    qw.close_stale_episode_if_needed(REPO)

    assert len(posted) == 1
    assert qw.STALE_RESOLVED_MARKER in posted[0]


def test_close_stale_episode_noop_when_no_open_episode(monkeypatch):
    monkeypatch.setattr(qw.pulse_guard, "issue_marker_times", lambda repo, issue, marker: [])
    posted = []
    monkeypatch.setattr(qw.pulse_guard, "post_issue_comment", lambda repo, issue, text: posted.append(text))

    qw.close_stale_episode_if_needed(REPO)

    assert posted == []


# ── main(): CLI-диспетчер gate|measure ──────────────────────────────────────


def test_main_dispatches_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(qw, "gate_main", lambda: calls.append("gate") or 0)
    assert qw.main(["gate"]) == 0
    assert calls == ["gate"]


def test_main_dispatches_measure(monkeypatch):
    calls = []
    monkeypatch.setattr(qw, "measure_main", lambda: calls.append("measure") or 0)
    assert qw.main(["measure"]) == 0
    assert calls == ["measure"]


def test_main_unknown_command_fails_loud():
    assert qw.main(["bogus"]) == 2
    assert qw.main([]) == 2


# ── Синхронность имён шагов workflow ↔ констант Python (found: ревью PR #607) ──


def test_workflow_step_names_match_constants():
    """Имена шагов в quota-watch.yml — единственный носитель, по которому
    last_real_measurement_age_minutes распознаёт «замер состоялся» через
    Jobs API (REST не отдаёт YAML `id:`, только `name`). Рассинхрон текста
    между YAML и Python-константами молча ослепил бы распознавание."""
    import yaml  # в repo-ci ставится рядом с pytest (см. quota-watch.yml)
    workflow = Path(__file__).parents[2] / ".github" / "workflows" / "quota-watch.yml"
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps = data["jobs"]["watch"]["steps"]
    gate_step = next(s for s in steps if "quota_watch.py gate" in (s.get("run") or ""))
    measure_step = next(s for s in steps if "quota_watch.py measure" in (s.get("run") or ""))
    assert gate_step["name"] == qw.GATE_STEP_NAME
    assert measure_step["name"] == qw.MEASURE_STEP_NAME
    # Гейт вычислен как id, который читает `if:` шага замера ниже.
    assert gate_step.get("id") == "gate"
    assert measure_step.get("if") == "steps.gate.outputs.proceed == 'true'"
