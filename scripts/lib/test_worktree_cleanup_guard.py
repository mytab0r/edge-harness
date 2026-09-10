#!/usr/bin/env python3
"""Гвардия worktree-cleanup (#891): безопасность удаления мёртвых деревьев.

Раньше гвардия проверяла ТЕКСТ исходника (наличие имён функций
`check_dirty`/`check_unpushed_commits` в файле) — переименование функции
красило её ложно-зелёной, а вырезание тела проверки при сохранённом имени
проходило бы молча. Здесь — только ПОВЕДЕНИЕ на настоящем временном
git-репозитории (tempfile + `git init` + реальный `git worktree add`), без
чтения исходника.

Живой замер (#891) вскрыл, что сигнал "ветка есть на origin" не работает в
ЭТОМ репозитории: оркестратор сливает PR, но не удаляет ветку — 127 из 128
реальных деревьев показывали "ветка ещё на origin", включая PR, смердженные
месяцами ранее. Поэтому здесь тестируется актуальный сигнал — статус PR
(`get_pr_status`, `gh pr list --head <ветка>` — точное совпадение имени
ветки, не `--search "head:<префикс>"`, который матчит подстрокой и на живом
прогоне вернул 30 посторонних PR на запрос по одной ветке).

Сценарии (все — обязательные условия неудаления, AGENTS.md «Fail loud»):
  1. Дерево с незакоммиченным файлом — снятие ОТКАЗАНО (и с --force тоже).
  2. Дерево с локальным коммитом, которого нет на origin — ОТКАЗАНО.
  3. Дерево, у которого PR ещё open — ОТКАЗАНО.
  4. Дерево младше retention — ОТКАЗАНО.
  5. Чистое дерево, PR которого merged/closed, старше retention — СНЯТО,
     и физически исчезает с диска.
  6. Не удалось определить статус PR (gh недоступен/PR не найден) —
     ОТКАЗАНО (fail loud, не молчаливое разрешение).

get_pr_status монкипатчится на уровне класса для сценариев 1/2/4/5 (сетевой
`gh`-вызов не имеет отношения к тому, что эти сценарии проверяют, и не может
быть детерминирован без реального GitHub-репозитория); сценарии 3 и 6 тоже
монкипатчат/используют реальный вызов сознательно — см. комментарии внутри.

Retention/«текущее время» инъецируются параметром (`retention_hours`,
`now_ts`) — тесты не зависят от системных часов и скорости выполнения
(класс «тесты-бомбы», AGENTS.md).

Доказательство мутацией (ручной прогон, не часть автоматического набора —
дословный вывод живёт в отчёте PR #893): закомментировать тело проверки
`check_dirty` внутри `can_remove_worktree` (оставив имя функции нетронутым)
красит `test_dirty_worktree_is_refused_even_with_force` в RED; вернуть тело —
GREEN.

Запуск: python -m pytest scripts/lib/test_worktree_cleanup_guard.py -q
"""

import subprocess
import sys

import pytest

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "git" / "worktree-cleanup.py"

_wc_spec = importlib.util.spec_from_file_location("worktree_cleanup", _SCRIPT_PATH)
worktree_cleanup = importlib.util.module_from_spec(_wc_spec)
_wc_spec.loader.exec_module(worktree_cleanup)  # type: ignore[union-attr]

WorktreeAnalyzer = worktree_cleanup.WorktreeAnalyzer

RETENTION_HOURS = 1.0


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stdout}\n{result.stderr}"
        )
    return result


def _setup_repo(tmp_path: Path) -> Path:
    """Завести bare-репозиторий 'origin' и рабочий клон с одним коммитом на main."""
    bare = tmp_path / "origin.git"
    work = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("init\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", "HEAD:main")
    _git(work, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return work


def _add_worktree(work: Path, name: str, branch: str, push: bool = True) -> Path:
    """Создать worktree под .claude/worktrees/<name> на новой ветке от origin/main."""
    wt_dir = work / ".claude" / "worktrees" / name
    wt_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(work, "worktree", "add", "-b", branch, str(wt_dir), "origin/main")
    if push:
        _git(work, "push", "origin", branch)
        _git(wt_dir, "branch", f"--set-upstream-to=origin/{branch}", branch)
    return wt_dir


def _delete_remote_branch(work: Path, branch: str) -> None:
    _git(work, "push", "origin", "--delete", branch)
    _git(work, "fetch", "origin", "--prune")


def _mtime_now_ts(path: Path, offset_hours: float) -> float:
    """now_ts, вычисленный ОТНОСИТЕЛЬНО реального mtime дерева, а не от
    time.time() напрямую — инъекция, не системные часы (класс «тесты-бомбы»)."""
    import os

    mtime = os.stat(path).st_mtime
    return mtime + offset_hours * 3600


def _worktree_info(work: Path, branch: str) -> dict:
    """Вернуть {'path', 'branch'} — ровно то, что отдаёт get_worktrees() для этой ветки."""
    for info in WorktreeAnalyzer(str(work)).get_worktrees():
        if info.get("branch") == f"refs/heads/{branch}":
            return info
    raise AssertionError(f"worktree на ветке {branch} не найден в git worktree list")


def _patch_pr_status(monkeypatch, value):
    """PR-статус недетерминирован без реального GitHub-репозитория (сетевой
    gh-вызов) — монкипатчим на уровне класса там, где сценарий проверяет НЕ
    его, а другой гейт (dirty/unpushed/retention)."""
    monkeypatch.setattr(WorktreeAnalyzer, "get_pr_status", lambda self, branch: value)


# ── Сценарий 1: незакоммиченные изменения ──────────────────────────────────

def test_dirty_worktree_is_refused_even_with_force(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")  # PR-гейт заведомо не мешает — изолируем dirty
    work = _setup_repo(tmp_path)
    branch = "agent/1-dirty"
    wt_dir = _add_worktree(work, "1-dirty", branch)
    (wt_dir / "uncommitted.txt").write_text("x", encoding="utf-8")

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)  # заведомо старше retention
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), force=False, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    assert analyzer.check_dirty(str(wt_dir)) is True

    can, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason

    analyzer_force = WorktreeAnalyzer(str(work), force=True, retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can_force, reason_force = analyzer_force.can_remove_worktree(info)
    assert can_force is False, f"--force не обязан снимать грязную проверку: {reason_force}"

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "грязное дерево не должно исчезать с диска"


# ── Сценарий 2: локальный коммит, которого нет на origin ───────────────────

def test_unpushed_commit_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")  # изолируем unpushed-гейт
    work = _setup_repo(tmp_path)
    branch = "agent/2-unpushed"
    wt_dir = _add_worktree(work, "2-unpushed", branch)

    (wt_dir / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(wt_dir, "add", "extra.txt")
    _git(wt_dir, "commit", "-m", "local only, never pushed")

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    assert analyzer.check_unpushed_commits(str(wt_dir)) is True

    can, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "дерево с непушенным коммитом не должно исчезать с диска"


# ── Сценарий 3: PR ещё open ──────────────────────────────────────────────

def test_open_pr_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "open")
    work = _setup_repo(tmp_path)
    branch = "agent/3-open"
    wt_dir = _add_worktree(work, "3-open", branch)
    _delete_remote_branch(work, branch)  # ветка удалена — но PR всё равно open

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert "open" in reason.lower()

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "дерево с открытым PR не должно исчезать с диска"


# ── Сценарий 4: дерево младше retention ────────────────────────────────────

def test_young_worktree_is_refused(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")  # изолируем retention-гейт
    work = _setup_repo(tmp_path)
    branch = "agent/4-young"
    wt_dir = _add_worktree(work, "4-young", branch)

    # Прошла только половина окна retention — заведомо младше, без зависимости
    # от реальной скорости выполнения теста.
    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 0.5)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    age = analyzer.get_worktree_age_hours(str(wt_dir))
    assert age < RETENTION_HOURS

    can, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "молодое дерево не должно исчезать с диска"


# ── Сценарий 5: чистое дерево, PR merged, старше retention ─────────────────

def test_clean_merged_worktree_is_removed_from_disk(tmp_path, monkeypatch):
    _patch_pr_status(monkeypatch, "merged")
    work = _setup_repo(tmp_path)
    branch = "agent/5-merged"
    wt_dir = _add_worktree(work, "5-merged", branch)
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    can, reason = analyzer.can_remove_worktree(info)
    assert can is True, reason

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 1, stats
    assert not wt_dir.exists(), "безопасное дерево обязано физически исчезнуть с диска"

    remaining = _git(work, "worktree", "list", "--porcelain").stdout
    assert str(wt_dir) not in remaining, "git тоже не должен помнить снятое дерево"


# ── Сценарий 6: статус PR не определён — отказ, не молчаливое разрешение ───

def test_unknown_pr_status_is_refused(tmp_path):
    """Без монкипатча: origin — локальный bare-репозиторий, не GitHub, gh
    честно не может определить владельца/репозиторий и возвращает ошибку
    быстро одним пакетным вызовом (не сеть, не таймаут — проверено вручную:
    ~0.8с). get_pr_status обязан вернуть None, а can_remove_worktree —
    отказать, а не молча разрешить снятие (fail loud, не silent-wrong,
    AGENTS.md)."""
    work = _setup_repo(tmp_path)
    branch = "agent/6-unknown"
    wt_dir = _add_worktree(work, "6-unknown", branch)
    _delete_remote_branch(work, branch)

    now_ts = _mtime_now_ts(wt_dir, RETENTION_HOURS * 10)
    info = _worktree_info(work, branch)

    analyzer = WorktreeAnalyzer(str(work), retention_hours=RETENTION_HOURS, now_ts=now_ts)
    assert analyzer.get_pr_status(branch) is None

    can, reason = analyzer.can_remove_worktree(info)
    assert can is False, reason
    assert "determine" in reason.lower()

    stats = analyzer.analyze_and_cleanup()
    assert stats["removed"] == 0
    assert wt_dir.exists(), "при неопределённом статусе PR дерево не должно исчезать"


# ── Здоровье скрипта: синтаксис и CLI --dry-run ────────────────────────────

def test_script_compiles():
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(_SCRIPT_PATH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"Syntax error in {_SCRIPT_PATH}: {result.stderr}"


def test_dry_run_cli_smoke_on_synthetic_repo(tmp_path):
    """CLI-обвязка (--dry-run) не падает и печатает маркер режима.

    main() берёт repo_root от os.getcwd() (текущего каталога вызова), НЕ от
    расположения самого файла скрипта (__file__) — иначе вызов из
    scripts/git/task-branch внутри песочницы теста task-branch.test.sh
    (`cd "$WORK/x-main" && bash task-branch`) запускал бы уборку по
    НАСТОЯЩЕМУ репозиторию разработчика вместо песочницы (найдено при
    подключении вызова, #891). Здесь это и проверяется: cwd=синтетический
    репозиторий без единого worktree под .claude/worktrees — "Total
    worktrees: 0", реальный dev-репозиторий не затронут вообще."""
    work = _setup_repo(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--dry-run"],
        cwd=str(work),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert "DRY RUN" in result.stdout
    assert "Total worktrees: 0" in result.stdout
    assert "Removed: 0" in result.stdout


def test_dry_run_cli_smoke_on_real_repo():
    """То же самое, но cwd — настоящий репозиторий (сотни реальных
    worktree'ов, один пакетный gh pr list, не сеть на каждое дерево, класс
    #891) — доказывает, что боевой путь тоже не падает, не только песочница."""
    repo_root = _SCRIPT_PATH.parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--dry-run"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    assert "DRY RUN" in result.stdout
    assert "Removed:" in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
