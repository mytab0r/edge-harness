#!/usr/bin/env python3
"""Тесты scripts/lib/pre_push_guard_gate.py (issue #1280).

Юнит-уровень: логика трёх исходов (`run_gate`, включая префлайт окружения
«gh аутентифицирован?» и потолок времени) и режима (`_mode`) на
синтетическом каталоге гвардий (`tmp_path`), без сети и без боевых 82
гвардий. Поведенческий сквозной прогон РЕАЛЬНОГО `.githooks/pre-push` на
живом git push — `scripts/git/test/pre-push-guard-gate.test.sh` (отдельный
носитель, AGENTS.md «Поведенческий тест находит то, чего структурный не
видит»): эти уровни не дублируют, а дополняют друг друга.

Запуск: python -m pytest scripts/lib/test_pre_push_guard_gate.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import shutil
import stat

import pytest

_GATE_SPEC = importlib.util.spec_from_file_location(
    "pre_push_guard_gate", Path(__file__).resolve().parent / "pre_push_guard_gate.py")
gate = importlib.util.module_from_spec(_GATE_SPEC)
_GATE_SPEC.loader.exec_module(gate)  # type: ignore[union-attr]

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
cr = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(cr)  # type: ignore[union-attr]


def _make_catalog(tmp_path, exit_code: int) -> Path:
    """Синтетический repo_root с одной гвардией, выходящей `exit_code`."""
    guards_dir = tmp_path / "scripts" / "ci" / "guards"
    guards_dir.mkdir(parents=True)
    run_guards = tmp_path / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text(
        "#!/usr/bin/env bash\nexit " + str(exit_code) + "\n", encoding="utf-8")
    run_guards.chmod(run_guards.stat().st_mode | stat.S_IEXEC)
    return tmp_path


@pytest.fixture(autouse=True)
def fake_gh_on_path(tmp_path, monkeypatch):
    """Автофикстура: настоящий `gh` в тестовом окружении не гарантирован
    (локальная машина без gh auth покрасила бы префлайт и сломала бы
    unaffected-тесты прогонки каталога) — на PATH ставится fake `gh`,
    завершающийся 0. Тесты самого префлайта переопределяют его явно
    (выход 4 / отсутствие на PATH)."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    return fake_bin


# ══════════════════════════════════════════════════════════════════════════
# run_gate — три исхода
# ══════════════════════════════════════════════════════════════════════════

def test_run_gate_ok_when_run_guards_exits_zero(tmp_path):
    repo_root = _make_catalog(tmp_path, 0)
    result = gate.run_gate(repo_root, "local")
    assert result.status == cr.STATUS_OK


def test_run_gate_violation_when_run_guards_exits_nonzero(tmp_path):
    repo_root = _make_catalog(tmp_path, 1)
    result = gate.run_gate(repo_root, "local")
    assert result.status == cr.STATUS_VIOLATION
    assert result.violations  # непустой — конструктор violation() иначе бы упал


def test_run_gate_unknown_when_run_guards_missing(tmp_path):
    # tmp_path пуст — scripts/ci/run_guards.sh физически не существует.
    result = gate.run_gate(tmp_path, "local")
    assert result.status == cr.STATUS_UNKNOWN
    assert result.reason and "run_guards.sh" in result.reason


def test_run_gate_local_mode_sets_skip_env_for_numbering_guards(tmp_path, monkeypatch):
    """Режим local обязан подставлять GUARD_CATALOG_SKIP на обе гвардии
    ловушки #1228 — run_guards.sh здесь просто печатает полученную
    переменную, доказывая, что она реально дошла до дочернего процесса."""
    monkeypatch.delenv("GUARD_CATALOG_SKIP", raising=False)  # изоляция от ambient-окружения вызывающего
    guards_dir = tmp_path / "scripts" / "ci" / "guards"
    guards_dir.mkdir(parents=True)
    run_guards = tmp_path / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text(
        "#!/usr/bin/env bash\necho \"SKIP=$GUARD_CATALOG_SKIP\"\nexit 0\n",
        encoding="utf-8")
    run_guards.chmod(run_guards.stat().st_mode | stat.S_IEXEC)

    import subprocess
    captured = {}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        # capture_output/text фильтруются наравне с cwd/env: через spy теперь
        # проходит и префлайт (`gh auth status`), который сам их передаёт.
        proc = real_run(cmd, capture_output=True, text=True, **{k: v for k, v in kwargs.items() if k not in ("cwd", "env", "capture_output", "text")}, cwd=kwargs.get("cwd"), env=kwargs.get("env"))
        captured["stdout"] = proc.stdout
        return proc

    monkeypatch.setattr(subprocess, "run", spy)
    gate.run_gate(tmp_path, "local")
    assert "decision-doc-numbering-guard" in captured["stdout"]
    assert "invariant-numbering-guard" in captured["stdout"]


def test_run_gate_full_mode_discards_ambient_skip_env(tmp_path, monkeypatch):
    """Внешний GUARD_CATALOG_SKIP из окружения вызывающего НЕ должен молча
    ужать «полный» каталог (находка ai-ревью PR #1285): full-режим изымает
    переменную из окружения дочернего процесса — иначе
    `GUARD_CATALOG_SKIP="..." git push` проходил бы мимо части гвардий."""
    monkeypatch.setenv("GUARD_CATALOG_SKIP", "decision-doc-numbering-guard")
    guards_dir = tmp_path / "scripts" / "ci" / "guards"
    guards_dir.mkdir(parents=True)
    run_guards = tmp_path / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text(
        "#!/usr/bin/env bash\necho \"SKIP=[$GUARD_CATALOG_SKIP]\"\nexit 0\n",
        encoding="utf-8")
    run_guards.chmod(run_guards.stat().st_mode | stat.S_IEXEC)

    import subprocess
    captured = {}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        proc = real_run(cmd, capture_output=True, text=True, cwd=kwargs.get("cwd"), env=kwargs.get("env"))
        captured["stdout"] = proc.stdout
        return proc

    monkeypatch.setattr(subprocess, "run", spy)
    gate.run_gate(tmp_path, "full")
    assert "SKIP=[]" in captured["stdout"]


# ══════════════════════════════════════════════════════════════════════════
# префлайт окружения — «упала гвардия» ≠ «окружение не даёт прогнать»
# (находка ai-ревью PR #1285)
# ══════════════════════════════════════════════════════════════════════════

def test_run_gate_unknown_when_gh_missing(tmp_path, monkeypatch):
    """gh CLI отсутствует → каталог заранее недостоверен (гвардии, требующие
    gh, упали бы не из-за кода) → третье состояние, каталог НЕ запускается."""
    repo_root = _make_catalog(tmp_path, 0)
    bin_dir = tmp_path / "only-bash"
    bin_dir.mkdir()
    os.symlink(shutil.which("bash"), bin_dir / "bash")
    monkeypatch.setenv("PATH", str(bin_dir))
    result = gate.run_gate(repo_root, "local")
    assert result.status == cr.STATUS_UNKNOWN
    assert "gh" in result.reason


def test_run_gate_unknown_when_gh_not_authenticated(tmp_path, fake_gh_on_path):
    """`gh auth status` с ненулевым кодом (протухший PAT, офлайн, разлогин) —
    тот же класс заранее недостоверного прогона, третье состояние."""
    repo_root = _make_catalog(tmp_path, 0)
    fake_gh = fake_gh_on_path / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env sh\necho 'You are not logged into any GitHub hosts.' >&2\nexit 4\n",
        encoding="utf-8")
    result = gate.run_gate(repo_root, "local")
    assert result.status == cr.STATUS_UNKNOWN
    assert "аутентифицир" in result.reason
    assert "код 4" in result.reason


def test_run_gate_unknown_when_catalog_hangs_past_timeout(tmp_path, monkeypatch):
    """Подвисшая гвардия не подвешивает пуш навсегда (находка ai-ревью
    PR #1285): потолок времени → третье состояние, не красная гвардия."""
    repo_root = _make_catalog(tmp_path, 0)
    run_guards = repo_root / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    monkeypatch.setattr(gate, "GUARD_CATALOG_TIMEOUT_SECONDS", 1)
    result = gate.run_gate(repo_root, "local")
    assert result.status == cr.STATUS_UNKNOWN
    assert "не уложился" in result.reason


def test_run_gate_full_mode_does_not_skip_anything(tmp_path, monkeypatch):
    monkeypatch.delenv("GUARD_CATALOG_SKIP", raising=False)  # изоляция от ambient-окружения вызывающего
    guards_dir = tmp_path / "scripts" / "ci" / "guards"
    guards_dir.mkdir(parents=True)
    run_guards = tmp_path / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text(
        "#!/usr/bin/env bash\necho \"SKIP=[$GUARD_CATALOG_SKIP]\"\nexit 0\n",
        encoding="utf-8")
    run_guards.chmod(run_guards.stat().st_mode | stat.S_IEXEC)

    import subprocess
    captured = {}
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        proc = real_run(cmd, capture_output=True, text=True, cwd=kwargs.get("cwd"), env=kwargs.get("env"))
        captured["stdout"] = proc.stdout
        return proc

    monkeypatch.setattr(subprocess, "run", spy)
    gate.run_gate(tmp_path, "full")
    assert "SKIP=[]" in captured["stdout"]


# ══════════════════════════════════════════════════════════════════════════
# _clean_git_env — находка 2026-09-15: git-хук подставляет GIT_DIR и т.п.
# ══════════════════════════════════════════════════════════════════════════

def test_clean_git_env_strips_all_git_prefixed_vars():
    dirty = {
        "GIT_DIR": "/repo/.git/worktrees/x",
        "GIT_WORK_TREE": "/repo",
        "GIT_INDEX_FILE": "/repo/.git/index",
        "GIT_PREFIX": "",
        "PATH": "/usr/bin",
        "GITHUB_ACTIONS": "true",  # НЕ GIT_-префикс формально не совпадает ('GIT_' не начало 'GITHUB') — проверим отдельно
    }
    cleaned = gate._clean_git_env(dirty)
    assert "GIT_DIR" not in cleaned
    assert "GIT_WORK_TREE" not in cleaned
    assert "GIT_INDEX_FILE" not in cleaned
    assert "GIT_PREFIX" not in cleaned
    assert cleaned["PATH"] == "/usr/bin"


def test_clean_git_env_keeps_github_actions_var():
    # "GITHUB_ACTIONS" не начинается с "GIT_" ('GITHUB' != 'GIT_') — режим
    # full/local не должен ломаться очисткой.
    cleaned = gate._clean_git_env({"GITHUB_ACTIONS": "true"})
    assert cleaned["GITHUB_ACTIONS"] == "true"


def test_run_gate_strips_leaked_git_dir_before_running_catalog(tmp_path, monkeypatch):
    """Живой сценарий 2026-09-15: GIT_DIR унаследован от git-хука — вложенный
    git внутри run_guards.sh обязан НЕ видеть его."""
    guards_dir = tmp_path / "scripts" / "ci" / "guards"
    guards_dir.mkdir(parents=True)
    run_guards = tmp_path / "scripts" / "ci" / "run_guards.sh"
    run_guards.write_text(
        "#!/usr/bin/env bash\n"
        "if [ -n \"${GIT_DIR:-}\" ]; then echo LEAKED; exit 1; fi\n"
        "echo CLEAN\nexit 0\n",
        encoding="utf-8")
    run_guards.chmod(run_guards.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("GIT_DIR", "/some/other/repo/.git/worktrees/leaked")
    result = gate.run_gate(tmp_path, "local")
    assert result.status == cr.STATUS_OK


# ══════════════════════════════════════════════════════════════════════════
# _mode — GITHUB_ACTIONS решает full/local
# ══════════════════════════════════════════════════════════════════════════

def test_mode_is_full_inside_github_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert gate._mode() == "full"


def test_mode_is_local_outside_github_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert gate._mode() == "local"


# ══════════════════════════════════════════════════════════════════════════
# main() — интерпретация исхода в блокировку пуша + аварийный выход
# ══════════════════════════════════════════════════════════════════════════

def test_main_blocks_on_violation(tmp_path, monkeypatch, capsys):
    repo_root = _make_catalog(tmp_path, 1)
    monkeypatch.setenv("GUARD_GATE_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GUARD_GATE_SKIP_ACK", raising=False)
    assert gate.main() != 0
    assert "run_guards.sh завершился с кодом" in capsys.readouterr().err


def test_main_passes_on_ok(tmp_path, monkeypatch):
    repo_root = _make_catalog(tmp_path, 0)
    monkeypatch.setenv("GUARD_GATE_REPO_ROOT", str(repo_root))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GUARD_GATE_SKIP_ACK", raising=False)
    assert gate.main() == 0


def test_main_blocks_on_unknown_not_treated_as_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GUARD_GATE_REPO_ROOT", str(tmp_path))  # пустой каталог
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GUARD_GATE_SKIP_ACK", raising=False)
    assert gate.main() != 0
    assert "НЕ СМОГ запуститься" in capsys.readouterr().err


def test_main_escape_hatch_skips_gate_with_visible_reason(tmp_path, monkeypatch, capsys):
    repo_root = _make_catalog(tmp_path, 1)  # заведомо красный каталог
    monkeypatch.setenv("GUARD_GATE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("GUARD_GATE_SKIP_ACK", "тест: осознанный пропуск")
    assert gate.main() == 0
    assert "тест: осознанный пропуск" in capsys.readouterr().err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
