#!/usr/bin/env python3
"""Тесты сторожа зомби-прогонов PR-чеков
(scripts/orchestra/zombie_run_watch.py, issue #1106).

Фикстуры — прод-форма, не пересказ:

- `ZOMBIE_RUN_ORCHESTRA` — дословно урезанный снимок `gh api
  repos/mytab0r/edge-harness/actions/runs/34748469966` (2026-09-14): один
  из восьми зомби-прогонов PR #1088 (`status=queued`, `created_at`
  2026-09-13T08:46:40Z), поля сокращены до тех, что реально читает модуль.
- `ZOMBIE_JOBS_EMPTY` — дословный `gh api
  .../actions/runs/34748469966/jobs` (`{"total_count": 0, "jobs": []}`).
- `HEALTHY_RUN`/`HEALTHY_JOBS` — дословный снимок живого успешного прогона
  34811586423 (2026-09-14, repo-ci): первый job создан через 1 секунду
  после `created_at` run'а — база для обоснования ZOMBIE_AGE_MINUTES=15 в
  докстринге модуля.
- `OPEN_PR_1209` — дословно урезанный `gh api
  repos/mytab0r/edge-harness/pulls/1209` (2026-09-14).

Запуск: python -m pytest scripts/orchestra/test_zombie_run_watch.py -q
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # zombie_run_watch.py делает `from pulse_guard import …`

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg

SCRIPT = _DIR / "zombie_run_watch.py"
spec = importlib.util.spec_from_file_location("zombie_run_watch", SCRIPT)
zrw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zrw)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    """zombie_run_watch.py импортирует gh/issue_marker_times/escalate ИЗ
    pulse_guard — те, что живут в pulse_guard, резолвят `gh` через
    __globals__ pulse_guard, поэтому обе привязки указывают на один и тот
    же fake (тот же приём, что test_dependabot_alert_watch.py::patch_gh)."""
    monkeypatch.setattr(zrw, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


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
                return result(self) if callable(result) else result
        raise AssertionError(f"нет маршрута для: {joined}")


REPO = "mytab0r/edge-harness"
NOW = utc(2026, 9, 14, 6, 0)

# Прод-форма (см. докстринг файла) — один из 8 зомби-прогонов PR #1088.
ZOMBIE_RUN_ORCHESTRA = {
    "id": 34748469966,
    "name": "orchestra",
    "event": "pull_request",
    "status": "queued",
    "conclusion": None,
    "created_at": "2026-09-13T08:46:40Z",
    "head_sha": "9a75c4142a020d8d57f50952acab25b957c086ce",
    "head_branch": "agent/1087-react-dom-peer-major-mismatch",
}
ZOMBIE_JOBS_EMPTY = {"total_count": 0, "jobs": []}

HEALTHY_RUN = {
    "id": 34811586423,
    "name": "repo-ci",
    "event": "pull_request",
    "status": "queued",
    "conclusion": None,
    "created_at": "2026-09-14T05:58:57Z",
    "head_sha": "b3655b9e041799db49427bea9ad08fadab368d78",
    "head_branch": "agent/1149-rebase-patch-series-0-14",
}
HEALTHY_JOBS = {
    "total_count": 2,
    "jobs": [
        {"name": "test", "created_at": "2026-09-14T05:58:58Z"},
        {"name": "archive-fixup", "created_at": "2026-09-14T05:58:58Z"},
    ],
}

OPEN_PR_1209 = {
    "number": 1209,
    "state": "open",
    "head": {
        "ref": "agent/1149-rebase-patch-series-0-14",
        "sha": "b3655b9e041799db49427bea9ad08fadab368d78",
    },
}
OPEN_PR_1088_STALE_BRANCH = {
    "number": 1088,
    "state": "open",
    "head": {
        "ref": "agent/1087-react-dom-peer-major-mismatch",
        # Ветка PR#1088 ушла вперёд с момента зомби-прогона — head другой.
        "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    },
}
OPEN_PR_1088_MATCHING = {
    "number": 1088,
    "state": "open",
    "head": {
        "ref": "agent/1087-react-dom-peer-major-mismatch",
        "sha": "9a75c4142a020d8d57f50952acab25b957c086ce",
    },
}


# ── Чистая логика ────────────────────────────────────────────────────────


def test_stale_candidates_prod_form_age_boundary():
    # ZOMBIE_RUN_ORCHESTRA создан 2026-09-13T08:46:40Z, NOW=2026-09-14T06:00Z
    # — больше суток, точно за порогом.
    assert zrw.stale_candidates([ZOMBIE_RUN_ORCHESTRA], NOW) == [ZOMBIE_RUN_ORCHESTRA]


def test_stale_candidates_fresh_run_not_a_candidate():
    fresh = {**ZOMBIE_RUN_ORCHESTRA, "created_at": "2026-09-14T05:50:00Z"}  # 10 мин назад
    assert zrw.stale_candidates([fresh], NOW) == []


def test_stale_candidates_ignores_non_queued():
    completed = {**ZOMBIE_RUN_ORCHESTRA, "status": "completed"}
    assert zrw.stale_candidates([completed], NOW) == []


def test_runs_created_after_excludes_old_permanent_zombie():
    reopen_time = utc(2026, 9, 13, 12, 0)  # позже создания зомби-прогона
    assert zrw.runs_created_after([ZOMBIE_RUN_ORCHESTRA], reopen_time) == []


def test_runs_created_after_includes_genuinely_new_run():
    new_run = {**ZOMBIE_RUN_ORCHESTRA, "id": 999, "created_at": "2026-09-14T00:00:00Z"}
    reopen_time = utc(2026, 9, 13, 12, 0)
    assert zrw.runs_created_after([ZOMBIE_RUN_ORCHESTRA, new_run], reopen_time) == [new_run]


def test_open_prs_by_branch_prod_form():
    mapping = zrw.open_prs_by_branch([OPEN_PR_1209])
    assert mapping["agent/1149-rebase-patch-series-0-14"]["number"] == 1209


def test_group_candidates_matches_current_head_only():
    grouped = zrw.group_candidates_by_pr(
        [ZOMBIE_RUN_ORCHESTRA], {"agent/1087-react-dom-peer-major-mismatch": OPEN_PR_1088_MATCHING})
    assert 1088 in grouped
    assert grouped[1088]["runs"] == [ZOMBIE_RUN_ORCHESTRA]


def test_group_candidates_rejects_stale_head_sha():
    # Ветка ушла вперёд — зомби-прогон был на старом head, не блокирует
    # текущий (мутация: снять сравнение head_sha дало бы ложное срабатывание
    # на устаревшем прогоне).
    grouped = zrw.group_candidates_by_pr(
        [ZOMBIE_RUN_ORCHESTRA], {"agent/1087-react-dom-peer-major-mismatch": OPEN_PR_1088_STALE_BRANCH})
    assert grouped == {}


def test_group_candidates_ignores_run_with_no_open_pr():
    grouped = zrw.group_candidates_by_pr([ZOMBIE_RUN_ORCHESTRA], {})
    assert grouped == {}


# ── zombie_run_watch: сценарии end-to-end ───────────────────────────────


def test_zero_queued_runs_reports_clean(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert "queued PR-прогонов старше" in observations[0]
    assert fake.calls == [f"repos/{REPO}/actions/runs?event=pull_request&status=queued&per_page=100"]


def test_non_list_runs_response_is_loud_not_silent_ok(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": "bad"},
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="не ожидаемой формой"):
        zrw.zombie_run_watch(REPO, NOW)


def test_fresh_queued_run_no_action_no_job_lookup(monkeypatch):
    fresh = {**HEALTHY_RUN, "created_at": "2026-09-14T05:55:00Z"}  # 5 мин назад
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [fresh]},
        "actions/runs/34811586423/jobs": AssertionError("не должен вызываться — прогон свежий"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("нет" in o for o in observations)


def test_stale_but_not_zombie_run_no_action(monkeypatch):
    """Прогон старше порога, но job'ы всё же есть (просто медленный старт) —
    НЕ зомби, действие не требуется, PR не трогается."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        f"issues/1088/comments": [],
        "actions/runs/34748469966/jobs": {"total_count": 3, "jobs": [{}]},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []


def test_confirmed_zombie_on_open_pr_triggers_reopen(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)
    patch_calls = [c for c in fake.calls if "-X PATCH" in c and "pulls/1088" in c]
    assert any("state=closed" in c for c in patch_calls)
    assert any("state=open" in c for c in patch_calls)
    comment_calls = [c for c in fake.calls if "-X POST" in c and "issues/1088/comments" in c]
    assert len(comment_calls) == 1


def test_zombie_matching_stale_head_is_not_touched(monkeypatch):
    """Мутация-канарейка: без сравнения head_sha в group_candidates_by_pr
    этот тест ловит ложное срабатывание на устаревшем прогоне."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_STALE_BRANCH],
        "actions/runs/34748469966/jobs": AssertionError("не должен вызываться — прогон устарел"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []


def test_permanent_zombie_after_reopen_is_not_reescalated(monkeypatch):
    """Ключевой сценарий докстринга («Рецидив после действия»): переэмиссия
    уже была, старый run-ID (тот же, что и до неё) остаётся queued/0-jobs
    НАВСЕГДА — новый пульс НЕ должен ни повторно переоткрывать, ни
    эскалировать, ни даже читать число job'ов (короткое замыкание по
    `runs_created_after`)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker, "user": {"login": "x"}},
        ],
        "actions/runs/34748469966/jobs": AssertionError("не должен вызываться — старый permanent-зомби"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("известный старый зомби-прогон" in o for o in observations)


def test_genuine_recurrence_after_reopen_escalates_once(monkeypatch):
    """Новый прогон (созданный ПОСЛЕ переэмиссии) тоже зомби — реальный
    рецидив, эскалация (не повторный close/reopen)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    new_zombie_run = {
        **ZOMBIE_RUN_ORCHESTRA, "id": 999999, "created_at": "2026-09-13T10:00:00Z",
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [new_zombie_run]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker, "user": {"login": "x"}},
        ],
        "actions/runs/999999/jobs": ZOMBIE_JOBS_EMPTY,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "-X POST": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(zrw, "escalate", lambda *a: "Telegram: доставлен; след: оставлен")
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("рецидив после переэмиссии" in a for a in actions)


def test_escalation_not_repeated_once_already_marked(monkeypatch):
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    escalation_marker = "[zombie-run-watch: эскалация PR#1088@9a75c414]"
    new_zombie_run = {
        **ZOMBIE_RUN_ORCHESTRA, "id": 999999, "created_at": "2026-09-13T10:00:00Z",
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [new_zombie_run]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker, "user": {"login": "x"}},
        ],
        "actions/runs/999999/jobs": ZOMBIE_JOBS_EMPTY,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            {"created_at": "2026-09-13T10:05:00Z", "body": escalation_marker, "user": {"login": "x"}},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(
        zrw, "escalate", lambda *a: pytest.fail("уже эскалировано — повтор не нужен"))
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("рецидив уже эскалирован" in o for o in observations)


def test_candidate_cap_truncates_and_warns(monkeypatch):
    many = [
        {**HEALTHY_RUN, "id": 100 + i, "created_at": "2026-09-13T00:00:00Z",
         "head_branch": f"branch-{i}"}
        for i in range(25)
    ]
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": many},
        "pulls?state=open": [],
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("потолок цены API" in o for o in observations)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
