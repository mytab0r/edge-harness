#!/usr/bin/env python3
"""Тесты гвардии механизма регистрации CI-гвардий
(scripts/lib/ci_guard_registration_guard.py, #749).

Мутация, доказывающая класс, который эта гвардия закрывает: дописать шаг
`- name: Гвардия <что-то новое>` рукописно в фикстуру repo-ci.yml без
добавления его в ALLOWLIST — `check_no_undeclared_step` обязан вернуть
непустой список; убрать шаг из фикстуры, оставив имя в ALLOWLIST, —
тоже непустой список (устаревшая запись реестра, тот же приём, что
`test_label_registry.py`/`test_infra_gh_inventory.py`).

Запуск: python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q
"""

import importlib.util
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "ci_guard_registration_guard", _DIR / "ci_guard_registration_guard.py"
)
crg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crg)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_repo_ci(tmp_path: Path, step_names: list[str]) -> Path:
    steps_yaml = "\n".join(f'      - name: "{n}"\n        run: echo hi' for n in step_names)
    content = "jobs:\n  test:\n    steps:\n" + steps_yaml + "\n"
    path = tmp_path / "repo-ci.yml"
    path.write_text(content, encoding="utf-8")
    return path


# ── guard_step_names: только совпадающий префикс ────────────────────────────

def test_guard_step_names_keeps_only_matching_prefixes(tmp_path):
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия Y", "checkout", "Smoke Z", "Юнит-тесты W"])
    assert crg.guard_step_names(path) == {"Тесты X", "Гвардия Y", "Smoke Z", "Юнит-тесты W"}


def test_guard_step_names_case_insensitive_prefix():
    path_dummy = None
    # регэксп сам по себе, без файла: гвардия/ГВАРДИЯ/гвардия — один класс
    assert crg._GUARD_NAME_RE.match("гвардия нижний регистр")
    assert crg._GUARD_NAME_RE.match("ГВАРДИЯ верхний регистр")


def test_guard_step_names_does_not_match_infrastructure_steps(tmp_path):
    # "Каталог гвардий scripts/ci/guards — перебор" — имя перебора каталога
    # намеренно не начинается с Тест/Гвардия/Smoke/Юнит-тест, иначе гвардия
    # путала бы сам перебор с рукописной регистрацией.
    path = _write_repo_ci(tmp_path, ["Каталог гвардий scripts/ci/guards — перебор (#749)", "checkout"])
    assert crg.guard_step_names(path) == set()


# ── check_no_undeclared_step: обе стороны мутации ────────────────────────────

def test_check_flags_new_undeclared_step(tmp_path):
    allowlist = frozenset({"Тесты X"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия новая рукописная"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert any("Гвардия новая рукописная" in p for p in problems)
    assert not any("Тесты X" in p for p in problems)


def test_check_flags_stale_allowlist_entry(tmp_path):
    allowlist = frozenset({"Тесты X", "Тесты давно мигрированной гвардии"})
    path = _write_repo_ci(tmp_path, ["Тесты X"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert any("Тесты давно мигрированной гвардии" in p for p in problems)


def test_check_clean_when_in_sync(tmp_path):
    allowlist = frozenset({"Тесты X", "Гвардия Y"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия Y", "checkout"])
    assert crg.check_no_undeclared_step(path, allowlist) == []


def test_check_reports_both_directions_at_once(tmp_path):
    allowlist = frozenset({"Тесты старая", "Тесты X"})
    path = _write_repo_ci(tmp_path, ["Тесты X", "Гвардия новая"])
    problems = crg.check_no_undeclared_step(path, allowlist)
    assert len(problems) == 2
    joined = " ".join(problems)
    assert "Гвардия новая" in joined
    assert "Тесты старая" in joined


# ── Живой снимок: сама гвардия на реальном repo-ci.yml ──────────────────────

def test_live_repo_ci_matches_frozen_allowlist():
    problems = crg.check_no_undeclared_step()
    assert problems == [], (
        "рукописные шаги гвардий в .github/workflows/repo-ci.yml разошлись с "
        f"ALLOWLIST scripts/lib/ci_guard_registration_guard.py: {problems}"
    )


def test_catalog_perebor_step_itself_is_present_in_repo_ci():
    repo_ci = (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    assert "scripts/ci/run_guards.sh" in repo_ci, (
        "перебор каталога гвардий (#749) не подключён к repo-ci.yml — "
        "каталог scripts/ci/guards/ никогда не исполняется"
    )
