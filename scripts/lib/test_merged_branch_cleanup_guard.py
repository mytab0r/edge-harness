#!/usr/bin/env python3
"""Гвардия отложенной уборки веток `agent/*` после слияния/закрытия PR (#940).

Поведенческие тесты по прод-форме данных `gh pr list --json
headRefName,state,mergedAt,closedAt` (не пересказ формата, а те же ключи и
тот же регистр значений `state`, который реально отдаёт `gh`), плюс один
тест на настоящем git-репозитории (bare "origin" + push --delete) — то, что
`delete_branch` действительно снимает ref, а не текстовая проверка исходника.

Доказательство мутацией (ручной прогон, дословный вывод — в отчёте PR):
  - убрать условие `any(r.get("state") == "OPEN" ...)` (всегда `False`) —
    `test_open_pr_blocks_deletion` красный: ветка с открытым PR попадает в
    список на удаление.
  - заменить `age_hours < retention_hours` на `age_hours < 0` (эффективно
    снять порог) — `test_young_merge_is_kept` красный: свежеслитая ветка
    удаляется немедленно.
  - убрать guard `branch.startswith("agent/")` — `test_non_agent_branch_never_touched`
    красный: чужая ветка становится кандидатом на удаление.
  - в агрегации статуса поменять порядок на «последний в списке выигрывает»
    (убрать приоритет open) — `test_multiple_prs_open_wins` красный, когда
    open — не последняя запись.

Запуск: python -m pytest scripts/lib/test_merged_branch_cleanup_guard.py -q
"""

import importlib.util
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "git" / "merged-branch-cleanup.py"
_mbc_spec = importlib.util.spec_from_file_location("merged_branch_cleanup", _SCRIPT_PATH)
merged_branch_cleanup = importlib.util.module_from_spec(_mbc_spec)
_mbc_spec.loader.exec_module(merged_branch_cleanup)  # type: ignore[union-attr]

branch_deletion_candidates = merged_branch_cleanup.branch_deletion_candidates
delete_branch = merged_branch_cleanup.delete_branch
RETENTION_HOURS = merged_branch_cleanup.RETENTION_HOURS

NOW = datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_old_merged_branch_is_deletable():
    old_merge = NOW - timedelta(hours=RETENTION_HOURS + 1)
    records = [{"headRefName": "agent/1-old", "state": "MERGED", "mergedAt": _iso(old_merge), "closedAt": None}]
    deletable, kept = branch_deletion_candidates(["agent/1-old"], records, NOW)
    assert deletable == ["agent/1-old"]
    assert kept == {}


def test_open_pr_blocks_deletion():
    old_merge = NOW - timedelta(hours=RETENTION_HOURS + 1)
    records = [
        {"headRefName": "agent/2-live", "state": "MERGED", "mergedAt": _iso(old_merge), "closedAt": None},
        {"headRefName": "agent/2-live", "state": "OPEN", "mergedAt": None, "closedAt": None},
    ]
    deletable, kept = branch_deletion_candidates(["agent/2-live"], records, NOW)
    assert deletable == []
    assert "открыт" in kept["agent/2-live"]


def test_young_merge_is_kept():
    fresh_merge = NOW - timedelta(hours=1)
    records = [{"headRefName": "agent/3-fresh", "state": "MERGED", "mergedAt": _iso(fresh_merge), "closedAt": None}]
    deletable, kept = branch_deletion_candidates(["agent/3-fresh"], records, NOW)
    assert deletable == []
    assert "моложе" in kept["agent/3-fresh"]


def test_no_pr_record_is_kept():
    deletable, kept = branch_deletion_candidates(["agent/4-unknown"], [], NOW)
    assert deletable == []
    assert "не знаем статуса" in kept["agent/4-unknown"]


def test_non_agent_branch_never_touched():
    # Даже если бы по какой-то ошибке в списке живых веток оказалась чужая
    # ветка с "подходящей" PR-записью — трогать её нельзя.
    old_merge = NOW - timedelta(hours=RETENTION_HOURS + 1)
    records = [{"headRefName": "main", "state": "MERGED", "mergedAt": _iso(old_merge), "closedAt": None}]
    deletable, kept = branch_deletion_candidates(["main"], records, NOW)
    assert deletable == []
    assert "не agent" in kept["main"]


def test_multiple_prs_open_wins_regardless_of_order():
    old_merge = NOW - timedelta(hours=RETENTION_HOURS + 5)
    records_open_first = [
        {"headRefName": "agent/5-reused", "state": "OPEN", "mergedAt": None, "closedAt": None},
        {"headRefName": "agent/5-reused", "state": "MERGED", "mergedAt": _iso(old_merge), "closedAt": None},
    ]
    records_open_last = list(reversed(records_open_first))

    for records in (records_open_first, records_open_last):
        deletable, kept = branch_deletion_candidates(["agent/5-reused"], records, NOW)
        assert deletable == [], f"open PR должен побеждать независимо от порядка: {records}"


def test_closed_without_merge_is_also_deletable_after_retention():
    old_close = NOW - timedelta(hours=RETENTION_HOURS + 1)
    records = [{"headRefName": "agent/6-abandoned", "state": "CLOSED", "mergedAt": None, "closedAt": _iso(old_close)}]
    deletable, kept = branch_deletion_candidates(["agent/6-abandoned"], records, NOW)
    assert deletable == ["agent/6-abandoned"]


# ── Поведенческий тест на настоящем git-репозитории: delete_branch реально снимает ref ──

def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stdout}\n{result.stderr}")
    return result


def test_delete_branch_removes_ref_on_real_origin(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD:refs/heads/main")
    _git(work, "checkout", "-b", "agent/7-doomed")
    _git(work, "push", "origin", "agent/7-doomed")

    heads, _ = merged_branch_cleanup.run_cmd(["git", "-C", str(work), "ls-remote", "--heads", "origin"])
    assert "agent/7-doomed" in heads

    monkeypatch.chdir(work)
    assert delete_branch("agent/7-doomed") is True

    heads_after, _ = merged_branch_cleanup.run_cmd(["git", "-C", str(work), "ls-remote", "--heads", "origin"])
    assert "agent/7-doomed" not in heads_after

    # Повторное удаление уже отсутствующей ветки — честный отказ, не False positive.
    assert delete_branch("agent/7-doomed") is False
