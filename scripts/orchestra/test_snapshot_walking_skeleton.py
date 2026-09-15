#!/usr/bin/env python3
"""Ходячий скелет снимка состояния задача/PR (#1287, orchestrator-core-v2,
openspec/changes/orchestrator-core-v2/, tasks.md Этап 0).

build_observed_pr/build_observed_task кормятся ПРОД-ФОРМОЙ, не пересказом:
fixtures_pr1290_observed.json/fixtures_issue1287_observed.json — реальные
ответы `gh api repos/mytab0r/edge-harness/pulls/1290` и
`.../issues/1287`, снятые 2026-09-15 (та же пара задача/PR, через которую
идёт этот ходячий скелет).

Запуск: python -m pytest scripts/orchestra/test_snapshot_walking_skeleton.py -q
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # scheduler.py делает `from pulse_guard import …`

SCRIPT = _DIR / "scheduler.py"
spec = importlib.util.spec_from_file_location("scheduler", SCRIPT)
sch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sch)  # type: ignore[union-attr]

PR_1290 = json.loads((_DIR / "fixtures_pr1290_observed.json").read_text(encoding="utf-8"))
ISSUE_1287 = json.loads((_DIR / "fixtures_issue1287_observed.json").read_text(encoding="utf-8"))
REPO = "mytab0r/edge-harness"


# ── build_observed_pr на реальной фикстуре PR #1290 ─────────────────────────


def test_build_observed_pr_1290_is_awaiting_gate1_not_gate2():
    """Живой факт репозитория на момент снятия фикстуры: PR #1290 несёт
    ТОЛЬКО review:large (без review:large-ok) — design.md §2.2 прямо относит
    это к awaiting_gate1 (не awaiting_gate2), хотя review_labels.gate1_decided
    для review:large уже true (#432, ровно та граница, ради которой заведён
    gate1_open отдельно от gate1_decided)."""
    assert [label["name"] for label in PR_1290["labels"]] == ["review:large"]
    observed = sch.build_observed_pr(REPO, PR_1290)
    assert observed.stage == sch.PR_STAGE_AWAITING_GATE1
    assert observed.repo == REPO
    assert observed.number == 1290


def test_build_observed_pr_1290_conflict_flag_from_real_mergeable_state():
    # Фикстура несёт mergeable_state="blocked" (снято живым запросом) — в
    # CONFLICT_CLEAR_STATES, то есть НЕ конфликт (review_labels.py).
    assert PR_1290["mergeable_state"] == "blocked"
    observed = sch.build_observed_pr(REPO, PR_1290)
    assert observed.flags["conflict"] is False


def test_build_observed_pr_1290_needs_rework_false_no_changes_requested_label():
    observed = sch.build_observed_pr(REPO, PR_1290)
    assert observed.flags["needs_rework"] is False


def test_build_observed_pr_1290_does_not_write_uncomputed_flags():
    # Этап 0 честно НЕ вычисляет checks_red/contract_ok/stale_ready/
    # gate2_error — их не должно быть в flags вовсе (design.md §1.2: не
    # вычислено — отсутствующий ключ, не фальшивое значение).
    observed = sch.build_observed_pr(REPO, PR_1290)
    assert "checks_red" not in observed.flags
    assert "contract_ok" not in observed.flags
    assert "stale_ready" not in observed.flags
    assert "gate2_error" not in observed.flags


def test_build_observed_pr_stage_ready_when_both_gates_open():
    pull = copy.deepcopy(PR_1290)
    pull["labels"] = [{"name": "review:ok"}, {"name": "ai:ok"}]
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_READY


def test_build_observed_pr_stage_awaiting_gate2_when_gate1_open_gate2_not():
    pull = copy.deepcopy(PR_1290)
    pull["labels"] = [{"name": "review:ok"}]
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_AWAITING_GATE2


def test_build_observed_pr_stage_draft():
    pull = copy.deepcopy(PR_1290)
    pull["draft"] = True
    pull["labels"] = []
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_DRAFT


def test_build_observed_pr_stage_merged():
    pull = copy.deepcopy(PR_1290)
    pull["merged"] = True
    pull["merged_at"] = "2026-09-15T00:00:00Z"
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_MERGED
    assert observed.stage in sch.PR_STAGE_TERMINAL


def test_build_observed_pr_stage_closed_without_merge():
    pull = copy.deepcopy(PR_1290)
    pull["state"] = "closed"
    pull["merged"] = False
    pull["merged_at"] = None
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_CLOSED
    assert observed.stage in sch.PR_STAGE_TERMINAL


def test_build_observed_pr_stage_no_task_for_branch_without_task_number():
    pull = copy.deepcopy(PR_1290)
    pull["head"] = {"ref": "dependabot/npm_and_yarn/foo-1.2.3"}
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.stage == sch.PR_STAGE_NO_TASK


def test_build_observed_pr_conflict_none_when_mergeable_state_unknown():
    # design.md §2.2: "не знаю" (None/unknown) НЕ снимает флаг — отдельное
    # значение, не False.
    pull = copy.deepcopy(PR_1290)
    pull["mergeable_state"] = "unknown"
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.flags["conflict"] is None


def test_build_observed_pr_conflict_true_on_dirty():
    pull = copy.deepcopy(PR_1290)
    pull["mergeable_state"] = "dirty"
    observed = sch.build_observed_pr(REPO, pull)
    assert observed.flags["conflict"] is True


# ── build_observed_task на реальной фикстуре issue #1287 ───────────────────


def test_build_observed_task_1287_real_fixture_shape():
    assert ISSUE_1287["number"] == 1287
    assert ISSUE_1287["state"] == "open"


def test_build_observed_task_1287_has_pr_when_pull_1290_open_and_linked():
    # Живая форма (#1287 branch agent/1287-orchestrator-core-v2, PR #1290
    # головой на неё) — task_ref.resolve_pr_task(PR_1290) обязана вернуть 1287.
    observed = sch.build_observed_task(REPO, ISSUE_1287, pulls=[PR_1290])
    assert observed.stage == sch.TASK_STAGE_HAS_PR


def test_build_observed_task_pool_when_no_assignee_no_pr():
    issue = copy.deepcopy(ISSUE_1287)
    issue["assignees"] = []
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.stage == sch.TASK_STAGE_POOL


def test_build_observed_task_leased_when_assignee_without_pr():
    issue = copy.deepcopy(ISSUE_1287)
    issue["assignees"] = [{"login": "mytab0r"}]
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.stage == sch.TASK_STAGE_LEASED


def test_build_observed_task_done_on_closed_completed():
    issue = copy.deepcopy(ISSUE_1287)
    issue["state"] = "closed"
    issue["state_reason"] = "completed"
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.stage == sch.TASK_STAGE_DONE
    assert observed.stage in sch.TASK_STAGE_TERMINAL


def test_build_observed_task_abandoned_on_closed_not_planned():
    issue = copy.deepcopy(ISSUE_1287)
    issue["state"] = "closed"
    issue["state_reason"] = "not_planned"
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.stage == sch.TASK_STAGE_ABANDONED
    assert observed.stage in sch.TASK_STAGE_TERMINAL


def test_build_observed_task_reopened_invalid():
    issue = copy.deepcopy(ISSUE_1287)
    issue["state"] = "open"
    issue["state_reason"] = "reopened"
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.stage == sch.TASK_STAGE_REOPENED_INVALID
    assert observed.stage not in sch.TASK_STAGE_TERMINAL


def test_build_observed_task_flags_blocked_and_waiting_owner():
    issue = copy.deepcopy(ISSUE_1287)
    issue["labels"] = [{"name": "task"}, {"name": "blocked"}, {"name": "waiting:owner"}]
    observed = sch.build_observed_task(REPO, issue, pulls=[])
    assert observed.flags == {"blocked": True, "waiting_owner": True}


# ── post_entity_snapshot: честный отказ без HANDS_TOKEN/HARNESS_URL ─────────


def test_post_entity_snapshot_honest_failure_without_config(monkeypatch):
    monkeypatch.setattr(sch, "HANDS_TOKEN", "")
    monkeypatch.setattr(sch, "HARNESS_URL", "")
    observed = sch.build_observed_pr(REPO, PR_1290)
    ok, detail = sch.post_entity_snapshot("pr", observed)
    assert ok is False
    assert "HANDS_TOKEN" in detail and "HARNESS_URL" in detail


def test_post_entity_snapshot_dry_run_outside_ci_even_with_config(monkeypatch):
    """Тот же гейт, что update_branch/_morde_rpc (RAW_WRITE_CENSUS,
    test_raw_write_transport_census_stays_gated): HANDS_TOKEN/HARNESS_URL
    заданы, но вне GitHub Actions — DRY-RUN, urlopen не вызывается вовсе
    (класс #950/#951 — сырая прод-запись мимо prod_writes_allowed)."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.setattr(sch, "HANDS_TOKEN", "test-hands-token")
    monkeypatch.setattr(sch, "HARNESS_URL", "https://harness.example")
    calls = []
    monkeypatch.setattr(sch.urllib.request, "urlopen", lambda *a, **kw: calls.append((a, kw)))

    observed = sch.build_observed_pr(REPO, PR_1290)
    ok, detail = sch.post_entity_snapshot("pr", observed)

    assert ok is False
    assert "DRY-RUN" in detail
    assert calls == []


def test_post_entity_snapshot_posts_real_json_body_to_pr_snapshot_route(monkeypatch):
    """Прод-форма запроса: POST {HARNESS_URL}/api/pr-snapshot, Bearer
    HANDS_TOKEN, тело — то, что реально отдаёт GET того же маршрута (см.
    cf-worker/test/snapshot.spec.ts, тот же контракт с другой стороны)."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setattr(sch, "HANDS_TOKEN", "test-hands-token")
    monkeypatch.setattr(sch, "HARNESS_URL", "https://harness.example")

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"written": True, "repo": REPO, "number": 1290, "stage": "awaiting_gate1"}).encode()

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode())
        return _FakeResponse()

    monkeypatch.setattr(sch.urllib.request, "urlopen", fake_urlopen)

    observed = sch.build_observed_pr(REPO, PR_1290)
    ok, detail = sch.post_entity_snapshot("pr", observed)

    assert ok is True
    assert captured["url"] == "https://harness.example/api/pr-snapshot"
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bearer test-hands-token"
    assert captured["body"] == {
        "repo": REPO,
        "number": 1290,
        "stage": sch.PR_STAGE_AWAITING_GATE1,
        "flags": {"conflict": False, "needs_rework": False},
    }
    assert "written=True" in detail


# ── write_walking_skeleton_snapshot: best-effort, не роняет main() ─────────


def test_write_walking_skeleton_snapshot_survives_gh_failure(monkeypatch):
    """MUTATION-PROOF-класс без git ref (новый код, нет исторического «до»):
    убери try/except в write_walking_skeleton_snapshot — этот тест
    воспроизводит именно тот сценарий (gh() бросает), который сломал
    test_main_labels_old_unclaimed_task_end_to_end/
    test_main_makes_zero_mutating_calls_on_fully_empty_queue до фикса
    (найдено этим же прогоном при разработке): исключение обязано быть
    поймано ВНУТРИ write_walking_skeleton_snapshot, наружу выходит текст
    предупреждения, не exception."""

    def failing_gh(*args, **kwargs):
        raise RuntimeError("нет маршрута для: " + " ".join(str(a) for a in args))

    monkeypatch.setattr(sch, "gh", failing_gh)
    lines = sch.write_walking_skeleton_snapshot(REPO, pulls=[])
    assert len(lines) == 2
    assert all("не удался" in line for line in lines)


def test_write_walking_skeleton_snapshot_lines_not_appended_to_main_report(monkeypatch):
    """Регресс-гвардия живой находки (тот же прогон разработки этого PR):
    main() обязан ПЕЧАТАТЬ строки ходячего скелета (stdout), не добавлять их
    в `lines` — тот же приём/обоснование, что announce_write_mode(). Раньше
    (до фикса) `lines += write_walking_skeleton_snapshot(...)` ломал
    stall_detector.detect_and_act (⚠️-строка ходячего скелета просачивалась
    в текст авто-комментария issue #120, ломая census-тесты FakeGh, найдено
    живым прогоном test_scheduler.py при разработке этого PR)."""
    import inspect

    source = inspect.getsource(sch.main)
    assert "lines += write_walking_skeleton_snapshot" not in source
    assert "print(skeleton_line)" in source
