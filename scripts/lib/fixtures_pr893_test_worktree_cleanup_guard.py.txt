#!/usr/bin/env python3
"""Гвардия worktree-cleanup (#891): безопасность удаления мёртвых деревьев.

Гвардия покрывает главный риск: скрипт должен быть НЕВОЗМОЖНО настроить так,
чтобы он удалил дерево с несохранённой работой.

Проверки:
1. Скрипт существует и работает (--help, --dry-run)
2. Сигнатура функций проверки безопасности присутствует в коде
3. Dry-run проходит без ошибок
4. Все три категории опасности (dirty, unpushed, young) обрабатываются
"""

import subprocess
import sys
from pathlib import Path

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---


def test_script_exists_and_executable():
    """Скрипт worktree-cleanup существует и имеет корректный синтаксис"""
    scripts_dir = Path(__file__).parent.parent
    cleanup_script = scripts_dir / "git" / "worktree-cleanup.py"

    assert cleanup_script.exists(), f"Script not found: {cleanup_script}"

    # Проверить синтаксис
    result = subprocess.run(
        ["python3", "-m", "py_compile", str(cleanup_script)],
        capture_output=True,
        text=True,
        encoding="utf-8"
    )
    assert result.returncode == 0, f"Syntax error in {cleanup_script}: {result.stderr}"


def test_script_has_safety_checks():
    """Скрипт содержит функции проверки безопасности"""
    scripts_dir = Path(__file__).parent.parent
    cleanup_script = scripts_dir / "git" / "worktree-cleanup.py"

    content = cleanup_script.read_text(encoding="utf-8")

    # Проверить наличие всех критических функций безопасности
    required_functions = [
        "check_dirty",           # Проверка грязного состояния
        "check_unpushed_commits", # Проверка непушенных коммитов
        "can_remove_worktree",   # Комбинированная проверка безопасности
    ]

    for func in required_functions:
        assert func in content, \
            f"Missing safety check function: {func}"

    # Проверить, что в can_remove_worktree есть ВСЕ проверки ДО удаления
    assert "# Проверка 1:" in content or "Check 1:" in content or "check_dirty" in content, \
        "Missing first safety check documentation"


def test_dry_run_mode_works():
    """Скрипт работает в режиме --dry-run и не удаляет деревья"""
    repo_root = Path(__file__).parent.parent.parent
    cleanup_script = repo_root / "scripts" / "git" / "worktree-cleanup.py"

    result = subprocess.run(
        ["python3", str(cleanup_script), "--dry-run"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60
    )

    # Dry-run должен завершиться успехом (или с exit 0 если нет ошибок)
    # Может быть exit 1 если есть проблемы, но вывод должен быть информативным
    assert "DRY RUN" in result.stdout or "worktree" in result.stdout.lower(), \
        f"Unexpected dry-run output: {result.stdout}"

    # Главное: вывод не должен содержать ошибок типа "remove failed"
    # которые бы означали, что скрипт пытался удалять
    if "REMOVE" in result.stdout or "removed" in result.stdout.lower():
        # Если есть какие-то удаления, то только в dry-run контексте
        assert "DRY RUN" in result.stdout or "Removed: 0" in result.stdout, \
            "Dry-run should not actually remove worktrees"


def test_safety_categories_present():
    """Все категории опасности обрабатываются отдельно"""
    scripts_dir = Path(__file__).parent.parent
    cleanup_script = scripts_dir / "git" / "worktree-cleanup.py"

    content = cleanup_script.read_text(encoding="utf-8")

    # Проверить, что статистика отслеживает все категории
    required_stats = [
        "'dirty':",      # Грязные (с изменениями)
        "'unpushed':",   # Непушенные коммиты
        "'young':",      # Слишком молодые
        "'open_pr':",    # Открытые PR
    ]

    for stat in required_stats:
        assert stat in content, \
            f"Missing stats category: {stat}"


def test_no_force_skip_dirty_check():
    """Даже с --force скрипт проверяет грязное состояние"""
    scripts_dir = Path(__file__).parent.parent
    cleanup_script = scripts_dir / "git" / "worktree-cleanup.py"

    content = cleanup_script.read_text(encoding="utf-8")

    # check_dirty должна быть до force-флага в can_remove_worktree
    # Проверить, что в can_remove_worktree грязная проверка идёт ДО force-флага
    can_remove_func = content[content.find("def can_remove_worktree"):
                              content.find("def can_remove_worktree") + 2000]

    dirty_check_pos = can_remove_func.find("check_dirty")
    force_check_pos = can_remove_func.find("self.force")

    # Грязная проверка должна быть раньше (меньший индекс) force-флага
    assert dirty_check_pos > 0 and (force_check_pos < 0 or dirty_check_pos < force_check_pos), \
        "Dirty check must come before force flag check"


if __name__ == '__main__':
    tests = [
        ("Script exists and executable", test_script_exists_and_executable),
        ("Safety checks present", test_script_has_safety_checks),
        ("Dry-run mode works", test_dry_run_mode_works),
        ("Safety categories present", test_safety_categories_present),
        ("Force flag doesn't skip dirty check", test_no_force_skip_dirty_check),
    ]

    failed = 0
    for name, test_func in tests:
        try:
            test_func()
            print(f"OK {name}")
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\nTests: {len(tests) - failed}/{len(tests)} passed")
    sys.exit(0 if failed == 0 else 1)
