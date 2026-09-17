#!/usr/bin/env python3
"""Гвардия канала наблюдаемости уборки рабочих деревьев (#1250, п.4).

Носитель записи прогона (состав, JSONL, транспорт data-ветки) — один модуль
`worktree_snapshot.py`, его читают писатель (worktree-cleanup.py
--publish-snapshot), инвариант 24 (repo_invariants.py) и ЭТОТ тест. Здесь —
поведение на настоящем git-транспорте (bare origin + клон, реальный push по
прецеденту test_data_branch_writer.py) и чистая логика на прод-форме данных.

Запуск: python -m pytest scripts/lib/test_worktree_snapshot.py -q
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

_SPEC = importlib.util.spec_from_file_location(
    "worktree_snapshot", Path(__file__).resolve().parent / "worktree_snapshot.py")
worktree_snapshot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(worktree_snapshot)  # type: ignore[union-attr]

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _record(**over):
    """Прод-форма записи (ключи make_record — контракт писателя и читателя)."""
    base = dict(ts=NOW.isoformat(), mode="apply", total=76, removed=28, kept=48,
                guard_copies=73, stale_guard_copies=13, stuck_old_total=9,
                stuck_old_unpushed=7, stuck_old_unknown_work=1,
                stuck_old_unknown_pr=1, retention_hours=1.0)
    base.update(over)
    return base


def _git(cwd, *args, check=True):
    import subprocess
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                            text=True, encoding="utf-8")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {result.stdout}\n{result.stderr}")
    return result


def _setup_origin_and_repo(tmp_path):
    """bare origin + чекаут с raw-origin, похожим на прод (пути непринципиальны:
    publisher_is_safe сравнивает ключи normalize_owner_repo, а транспорт
    тестируется с инъекцией origin=путь к bare)."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("init\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "HEAD:main")
    return bare, work


# ── Чистая логика ───────────────────────────────────────────────────────────

def test_make_record_counts_stuck_total_from_known_codes():
    row = worktree_snapshot.make_record(
        ts=NOW.isoformat(), mode="apply", total=76, removed=0, kept=76,
        guard_copies=73, stale_guard_copies=41,
        stuck_old_by_code={"unpushed": 35, "unknown_work": 0, "unknown_pr": 2,
                           "какой-то будущий код": 5},
        retention_hours=1.0)
    assert row["stuck_old_total"] == 37, "сумма ТОЛЬКО известных кодов — незнакомый ключ не попадает в запись молча"
    assert "stuck_old_какой-то будущий код" not in row
    assert row["stuck_old_unpushed"] == 35 and row["stuck_old_unknown_pr"] == 2


def test_read_rows_empty_and_corrupt():
    assert worktree_snapshot.read_rows("") == []
    assert worktree_snapshot.read_rows("\n\n") == []
    row = _record()
    text = json.dumps(row, sort_keys=True) + "\n"
    assert worktree_snapshot.read_rows(text) == [row]
    with pytest.raises(ValueError, match="строка 2"):
        worktree_snapshot.read_rows(
            json.dumps(row) + "\n{сломанная json строка\n")


def test_last_record_picks_freshest_ts_not_file_order():
    older = _record(ts="2026-09-13T00:00:00+00:00")
    newer = _record(ts="2026-09-14T00:00:00+00:00")
    assert worktree_snapshot.last_record([older, newer]) is newer
    assert worktree_snapshot.last_record([newer, older]) is newer
    assert worktree_snapshot.last_record([]) is None


def test_is_duplicate_observation_window_and_change():
    fresh_same = _record()
    assert worktree_snapshot.is_duplicate_observation(fresh_same, _record(), now=NOW) is True, \
        "те же числа в окне антишума — писать нечего"
    changed = _record(stale_guard_copies=42)
    assert worktree_snapshot.is_duplicate_observation(fresh_same, changed, now=NOW) is False, \
        "изменившееся число — новая информация, пишем немедленно"
    old = _record(ts=(NOW - timedelta(hours=2)).isoformat())
    assert worktree_snapshot.is_duplicate_observation(old, _record(), now=NOW) is False, \
        "запись старше окна — дублем не считается (свежая строка нужна для свежести канала)"
    assert worktree_snapshot.is_duplicate_observation(None, _record(), now=NOW) is False
    broken = _record(ts="не-время")
    assert worktree_snapshot.is_duplicate_observation(broken, _record(), now=NOW) is False, \
        "нечитаемый ts не маскируется под свежий дубль"


def test_normalize_owner_repo_and_publisher_safety():
    normalize = worktree_snapshot.normalize_owner_repo
    assert normalize("https://github.com/MyTab0r/edge-harness.git") == "mytab0r/edge-harness"
    assert normalize("git@github.com:mytab0r/edge-harness.git") == "mytab0r/edge-harness"
    assert normalize("https://github.com/mytab0r/edge-harness") == "mytab0r/edge-harness"
    assert normalize("/tmp/sandbox/origin.git") == "/tmp/sandbox/origin", \
        "локальный путь — сопоставим с тем же путём, не с github-ключом"
    is_safe = worktree_snapshot.publisher_is_safe
    assert is_safe("https://github.com/mytab0r/edge-harness.git", "mytab0r/edge-harness")
    assert not is_safe("/tmp/sandbox/origin.git", "mytab0r/edge-harness"), \
        "песочница с унаследованным GITHUB_REPOSITORY не должна публиковать"
    assert not is_safe("https://github.com/someone/else.git", "mytab0r/edge-harness")


def test_count_stale_guard_copies_reads_real_files(tmp_path):
    base = tmp_path / ".claude" / "worktrees"
    for name, fix in (("with-fix", True), ("without-fix", False)):
        guard = base / name / "scripts" / "orchestra" / "pulse_guard.py"
        guard.parent.mkdir(parents=True, exist_ok=True)
        guard.write_text(
            "prod_writes_allowed = True\n" if fix else "raise SystemExit(2)\n",
            encoding="utf-8")
    (base / "no-guard").mkdir(parents=True)
    total, stale = worktree_snapshot.count_stale_guard_copies(base)
    assert (total, stale) == (2, 1)
    assert worktree_snapshot.count_stale_guard_copies(tmp_path / "missing") == (0, 0)


# ── Транспорт: реальный bare origin, реальный push ─────────────────────────

def test_publish_appends_row_and_second_run_dedup(tmp_path, capsys):
    bare, work = _setup_origin_and_repo(tmp_path)
    record = _record()

    assert worktree_snapshot.publish_snapshot_record(
        str(work), record, "some/target", origin=str(bare),
        is_duplicate=lambda text: False, log=print) is False, \
        "origin чекаута (локальный путь) не совпадает с целевым — песочница не публикует"

    ok = worktree_snapshot.publish_snapshot_record(
        str(work), record, str(bare), origin=str(bare),
        is_duplicate=lambda text: False, log=print)
    assert ok is True
    content = _git(bare, "show",
                   f"refs/heads/{worktree_snapshot.DATA_BRANCH}:"
                   f"{worktree_snapshot.SNAPSHOT_PATH}").stdout
    assert worktree_snapshot.read_rows(content) == [record]

    # Второй прогон с теми же числами: антишум по факту с сервера — не пишет
    # вторую строку (is_duplicate-инъекция здесь решает как прод-дефолт, но
    # контракт «False = не писать, причина напечатана» — писателя).
    def server_truth(current_text):
        rows = worktree_snapshot.read_rows(current_text) if current_text else []
        return worktree_snapshot.is_duplicate_observation(
            worktree_snapshot.last_record(rows), record, now=NOW)

    again = worktree_snapshot.publish_snapshot_record(
        str(work), record, str(bare), origin=str(bare),
        is_duplicate=server_truth, log=print)
    assert again is False
    content = _git(bare, "show",
                   f"refs/heads/{worktree_snapshot.DATA_BRANCH}:"
                   f"{worktree_snapshot.SNAPSHOT_PATH}").stdout
    assert len(worktree_snapshot.read_rows(content)) == 1, "дубль не дописан"


def test_publish_missing_measure_base_is_skip_not_zero_row(tmp_path, capsys):
    """`.claude/worktrees` отсутствует — запись с НУЛЯМИ не публикуется:
    синтетический ноль с чекаута без деревьев маскировал бы живое состояние
    других чекаутов в окне антишума (тот же silent-wrong, против которого
    весь канал)."""
    bare, work = _setup_origin_and_repo(tmp_path)
    ok = worktree_snapshot.publish_snapshot_record(
        str(work), _record(total=0, removed=0, kept=0, guard_copies=0,
                           stale_guard_copies=0, stuck_old_total=0),
        str(bare), origin=str(bare),
        measure_base=work / ".claude" / "worktrees",
        is_duplicate=lambda text: False, log=print)
    assert ok is False
    assert ".claude/worktrees" in capsys.readouterr().out
    assert _git(bare, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{worktree_snapshot.DATA_BRANCH}", check=False).returncode != 0


def test_publish_transport_failure_is_loud_not_fatal(tmp_path, capsys):
    """Сеть/права отвалились — публикация False с ПЕЧАТНОЙ причиной (пуш в
    несуществующий remote-путь), уборка не ломается: это телеметрия, смерть
    канала ловит инвариант 24 по свежести записей."""
    bare, work = _setup_origin_and_repo(tmp_path)
    ok = worktree_snapshot.publish_snapshot_record(
        str(work), _record(), str(bare),
        origin=str(tmp_path / "nonexistent-origin.git"),  # клон невозможен
        is_duplicate=lambda text: False, log=print)
    assert ok is False
    out = capsys.readouterr().out
    assert "ПРЕДУПРЕЖДЕНИЕ" in out and "инвариант 24" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
