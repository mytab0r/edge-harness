#!/usr/bin/env python3
"""Тесты сборщика снимка здоровья конвейера (scripts/measure/pipeline_health.py,
openspec/changes/pipeline-health-self-audit).

Кормятся прод-формой: PR/issues/search-ответы — реальная форма GitHub REST/
Search API (dict с ключами created_at/labels/assignees/total_count), не
пересказ. Git-транспорт проверяется живым локальным bare-репозиторием (тот
же приём, что scripts/measure/test_dispatch_tail.py::git_writer_fixture).

Запуск: python -m pytest scripts/measure/test_pipeline_health.py -q
"""

import importlib.util
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "pipeline_health.py"
spec = importlib.util.spec_from_file_location("pipeline_health", SCRIPT)
ph = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ph)  # type: ignore[union-attr]


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── percentile / pr_age_stats ──────────────────────────────────────────────


def test_percentile_nearest_rank():
    # nearest-rank без интерполяции (тот же метод, что dispatch_tail.percentile):
    # round(p * n) - 1 — для n=5, p=0.5 даёт индекс 1 (round(2.5) → 2 банковским
    # округлением Python), не «средний» элемент в бытовом смысле.
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert ph.percentile(values, 0.5) == 2.0
    assert ph.percentile(values, 0.95) == 5.0


def test_percentile_empty_is_loud():
    with pytest.raises(ValueError):
        ph.percentile([], 0.5)


def test_pr_age_stats_empty_pulls_is_none_not_zero():
    """Честное «нет данных» — 0 открытых PR не значит «возраст 0», значит
    «метрику сегодня не из чего посчитать»."""
    assert ph.pr_age_stats([], utc(2026, 9, 9)) == {"p50": None, "p95": None}


def test_pr_age_stats_prod_form():
    now = utc(2026, 9, 9, 12, 0)
    pulls = [
        {"number": 1, "created_at": "2026-09-09T00:00:00Z"},   # 12ч
        {"number": 2, "created_at": "2026-09-08T12:00:00Z"},   # 24ч
        {"number": 3, "created_at": "2026-09-01T12:00:00Z"},   # 192ч
    ]
    stats = ph.pr_age_stats(pulls, now)
    assert stats["p50"] == 24.0
    assert stats["p95"] == 192.0


# ── backlog_counts ──────────────────────────────────────────────────────────


def _issue(number, labels=(), assignees=()):
    return {
        "number": number,
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": a} for a in assignees],
    }


def test_backlog_counts_prod_form_classification():
    issues = [
        _issue(1),  # свободна
        _issue(2, assignees=["agent"]),  # в работе
        _issue(3, labels=["blocked"], assignees=["agent"]),  # blocked побеждает assignee
        _issue(4, labels=["stale-unclaimed"]),  # свободна + stale
    ]
    backlog = ph.backlog_counts(issues)
    assert backlog == {
        "free": 2, "in_progress": 1, "blocked": 1, "stale_unclaimed": 1, "total": 4,
    }


# ── worker_success_rate ──────────────────────────────────────────────────────


def test_worker_success_rate_prod_form():
    runs = [
        {"conclusion": "success"},
        {"conclusion": "failure"},
        {"conclusion": "success"},
        {"conclusion": None},  # ещё бежит — не судим по нему
    ]
    assert ph.worker_success_rate(runs) == pytest.approx(66.7, abs=0.1)


def test_worker_success_rate_no_concluded_runs_is_none():
    assert ph.worker_success_rate([{"conclusion": None}]) is None
    assert ph.worker_success_rate([]) is None


def test_worker_success_rate_respects_window():
    runs = [{"conclusion": "failure"}] * 2 + [{"conclusion": "success"}] * 10
    assert ph.worker_success_rate(runs, window=2) == 0.0


# ── pulse_cadence ─────────────────────────────────────────────────────────


def test_pulse_cadence_prod_form():
    now = utc(2026, 9, 9, 12, 0)
    tick_runs = [
        {"created_at": (now - timedelta(hours=h)).isoformat().replace("+00:00", "Z")}
        for h in (1, 2, 25)  # третий тик старше окна 24ч — не считается
    ]
    cadence = ph.pulse_cadence(tick_runs, now)
    assert cadence == {"actual": 2, "expected": 96, "ratio": round(2 / 96, 3)}


def test_pulse_cadence_mutation_boundary_is_inclusive_window():
    now = utc(2026, 9, 9, 12, 0)
    exactly_24h = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    cadence = ph.pulse_cadence([{"created_at": exactly_24h}], now)
    assert cadence["actual"] == 1  # >= since, не > since


# ── merge_throughput_from_search ─────────────────────────────────────────────


def test_merge_throughput_from_search_prod_form():
    # Прод-форма ответа GitHub Search API (`search/issues`).
    search_result = {
        "total_count": 3, "incomplete_results": False,
        "items": [{"number": 1}, {"number": 2}, {"number": 3}],
    }
    assert ph.merge_throughput_from_search(search_result) == 3


def test_merge_throughput_from_search_missing_key_is_zero_not_crash():
    assert ph.merge_throughput_from_search({}) == 0


# ── search_merged_prs ────────────────────────────────────────────────────────


def test_search_merged_prs_builds_hour_precision_query():
    """Квалификатор `merged:` c полным временем, не датой суток (подтверждено
    живым запросом 2026-09-11) — окно атрибуции меряется часами."""
    calls = []

    def fake_gh(*args):
        calls.append(args[0])
        return {"total_count": 0, "items": []}

    start = utc(2026, 9, 10, 20, 0)
    end = utc(2026, 9, 11, 5, 0)
    ph.search_merged_prs("mytab0r/edge-harness", fake_gh, start, end)
    assert len(calls) == 1
    assert "merged:2026-09-10T20:00:00..2026-09-11T05:00:00" in calls[0]
    assert "sort=created&order=asc" in calls[0]


def test_search_merged_prs_returns_prod_form_dict():
    # Прод-форма живого ответа `search/issues` (2026-09-11, PR #903).
    fixture = {
        "total_count": 1, "incomplete_results": False,
        "items": [{
            "number": 903,
            "title": "#899: гонка created_at/completion в conveyor_gate",
            "pull_request": {"merged_at": "2026-09-10T22:00:32Z"},
        }],
    }
    result = ph.search_merged_prs("mytab0r/edge-harness", lambda *_: fixture,
                                  utc(2026, 9, 10, 20, 0), utc(2026, 9, 10, 23, 0))
    assert result == fixture


# ── build_snapshot ────────────────────────────────────────────────────────


def test_build_snapshot_assembles_all_fields():
    now = utc(2026, 9, 9, 12, 0)
    snapshot = ph.build_snapshot(
        now.date(),
        merged_search={"total_count": 5},
        open_pulls=[{"created_at": "2026-09-08T12:00:00Z"}],
        task_issues=[_issue(1)],
        worker_runs=[{"conclusion": "success"}],
        tick_runs=[{"created_at": "2026-09-09T11:00:00Z"}],
        do_rows_read_pct=12.5,
        gh_rate_remaining_pct=88.0,
        now=now,
    )
    assert snapshot["date"] == "2026-09-09"
    assert snapshot["merge_throughput"] == 5
    assert snapshot["pr_age_p50_hours"] == 24.0
    assert snapshot["backlog_total"] == 1
    assert snapshot["worker_success_rate"] == 100.0
    assert snapshot["pulse_ticks_24h"] == 1
    assert snapshot["do_rows_read_pct"] == 12.5
    assert snapshot["gh_rate_remaining_pct"] == 88.0


# ── JSONL rows / гейт снятия раз в сутки ─────────────────────────────────────


def test_read_rows_and_roundtrip():
    rows = [{"date": "2026-09-08", "merge_throughput": 3}, {"date": "2026-09-09", "merge_throughput": 1}]
    text = ph.rows_to_jsonl(rows)
    assert text.count("\n") == 2
    assert ph.read_rows(text) == rows


def test_read_rows_empty_text_is_empty_list():
    assert ph.read_rows("") == []
    assert ph.read_rows("   \n  \n") == []


def test_last_snapshot_date():
    assert ph.last_snapshot_date([]) is None
    rows = [{"date": "2026-09-01"}, {"date": "2026-09-08"}]
    assert ph.last_snapshot_date(rows).isoformat() == "2026-09-08"


@pytest.mark.parametrize("last,today,expected", [
    (None, "2026-09-09", True),
    ("2026-09-08", "2026-09-09", True),
    ("2026-09-09", "2026-09-09", False),   # мутация: должно быть > , не >=
    ("2026-09-10", "2026-09-09", False),
])
def test_should_snapshot_gate_boundary(last, today, expected):
    from datetime import date
    last_d = date.fromisoformat(last) if last else None
    assert ph.should_snapshot(last_d, date.fromisoformat(today)) is expected


# ── collect(): проводка вызовов, прод-форма роутинга ─────────────────────────


class FakeGh:
    """Маршрутизатор по подстроке пути — тот же приём, что
    scripts/orchestra/test_stall_detector.py::FakeGh."""

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


def test_collect_wires_all_sources_without_do_or_rate_env(monkeypatch):
    repo = "mytab0r/edge-harness"
    now = utc(2026, 9, 9, 12, 0)
    fake = FakeGh({
        "search/issues": {"total_count": 2, "items": []},
        "pulls?state=open": [{"number": 1, "created_at": "2026-09-08T12:00:00Z"}],
        "issues?state=open&labels=task": [_issue(1)],
        "actions/workflows/worker.yml/runs": {"workflow_runs": [{"conclusion": "success"}]},
        "actions/workflows/orchestra.yml/runs": {"workflow_runs": []},
        "rate_limit": {"resources": {"core": {"limit": 1000, "remaining": 900}}},
    })
    monkeypatch.setattr(ph.pulse_guard, "gh", fake)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    snapshot = ph.collect(repo, fake, now)
    assert snapshot["merge_throughput"] == 2
    assert snapshot["backlog_total"] == 1
    assert snapshot["worker_success_rate"] == 100.0
    assert snapshot["do_rows_read_pct"] is None  # честное «нет данных», токенов нет
    assert snapshot["gh_rate_remaining_pct"] == 90.0
    assert any("search/issues" in call for call in fake.calls)


def test_gh_rate_remaining_pct_none_on_failure(monkeypatch):
    def boom(*_args):
        raise RuntimeError("gh api rate_limit: HTTP 403")
    assert ph._gh_rate_remaining_pct(boom) is None


# ── git-транспорт: запись снимка + гейт дубля дня ────────────────────────────


def git_writer_fixture(tmp_path, name):
    bare = tmp_path / "bare.git"
    if not bare.exists():
        subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", str(bare)],
                       check=True)
        seed = tmp_path / "seed"
        subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True)
        subprocess.run(["git", "-C", str(seed), "checkout", "--quiet", "-B", "main"], check=True)
        (seed / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "--quiet", "-m", "seed"], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "--quiet", "origin", "main"], check=True)
    work = tmp_path / name
    subprocess.run(["git", "clone", "--quiet", str(bare), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "--quiet", "-B", ph.DATA_BRANCH, "origin/main"],
                   check=True)
    return bare, work


def test_append_snapshot_and_push_writes_row(tmp_path):
    _, work = git_writer_fixture(tmp_path, "a")
    snapshot = {"date": "2026-09-09", "merge_throughput": 3}
    assert ph.append_snapshot_and_push(str(work), snapshot) is True
    rows = ph.read_rows((work / ph.SNAPSHOT_PATH).read_text(encoding="utf-8"))
    assert rows == [snapshot]


def test_append_snapshot_and_push_skips_same_day_duplicate(tmp_path):
    """Тот же писатель (тот же локальный клон, уже видящий записанный день —
    детерминированная проверка гейта дубля без зависимости от таймингов
    гонки push, которую покрывает отдельный тест ниже)."""
    _, work = git_writer_fixture(tmp_path, "a")
    snapshot = {"date": "2026-09-09", "merge_throughput": 3}
    assert ph.append_snapshot_and_push(str(work), snapshot) is True
    assert ph.append_snapshot_and_push(str(work), snapshot) is False


def test_append_snapshot_and_push_survives_concurrent_writer(tmp_path):
    bare, work_a = git_writer_fixture(tmp_path, "a")
    _, work_b = git_writer_fixture(tmp_path, "b")
    assert ph.append_snapshot_and_push(str(work_a), {"date": "2026-09-08", "merge_throughput": 1})
    assert ph.append_snapshot_and_push(str(work_b), {"date": "2026-09-09", "merge_throughput": 2})
    # writer_a продолжает поверх подвинувшейся ветки — своя строка не теряется
    assert ph.append_snapshot_and_push(str(work_a), {"date": "2026-09-10", "merge_throughput": 3})

    subprocess.run(["git", "clone", "--quiet", "--branch", ph.DATA_BRANCH, str(bare),
                    str(tmp_path / "check")], check=True)
    rows = ph.read_rows((tmp_path / "check" / ph.SNAPSHOT_PATH).read_text(encoding="utf-8"))
    assert [r["date"] for r in rows] == ["2026-09-08", "2026-09-09", "2026-09-10"]


# ── Гвардия workflow: git-авторизация ДО push снимка здоровья (issue #882) ───


def test_orchestra_health_audit_step_authorizes_git_before_push():
    """Живой корень инцидента #882: `orchestra.yml` вызывал health_audit.py
    (который пушит на data/pipeline-health) БЕЗ `gh auth setup-git` —
    свежий клон в $RUNNER_TEMP не наследует креды `actions/checkout`, push
    падал 403. `dispatch-latency-probe.yml` уже несёт этот шаг перед КАЖДЫМ
    git-пишущим шагом (тот же секрет GH_PIPELINE_PAT) — orchestra.yml обязан
    держать то же самое перед своим единственным git-пишущим шагом."""
    import yaml
    workflow_path = (Path(__file__).parents[2] / ".github" / "workflows" / "orchestra.yml")
    data = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = data["jobs"]["orchestra"]["steps"]
    names = [s.get("name", "") for s in steps]
    audit_idx = next((i for i, n in enumerate(names) if "Само-аудит здоровья" in n), None)
    assert audit_idx is not None, "шаг само-аудита исчез из orchestra.yml — гвардия ослепла"

    auth_idx = next(
        (i for i, s in enumerate(steps)
         if "gh auth setup-git" in (s.get("run") or "") and i < audit_idx),
        None,
    )
    assert auth_idx is not None, (
        "перед шагом само-аудита здоровья нет `gh auth setup-git` — свежий клон "
        "pipeline_health.clone_data_branch не унаследует креды actions/checkout, "
        "push на data/pipeline-health упадёт 403 (issue #882)"
    )
    auth_env = steps[auth_idx].get("env") or {}
    assert "GH_PIPELINE_PAT" in str(auth_env.get("GH_TOKEN", "")), (
        "gh auth setup-git перед само-аудитом обязан авторизоваться широким "
        "GH_PIPELINE_PAT (тем же секретом, что читает pipeline_health.py для push), "
        f"а не {auth_env.get('GH_TOKEN')!r}"
    )
