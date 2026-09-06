#!/usr/bin/env python3
"""Тесты гвардии бита исполнения (scripts/lib/exec_bit_guard.py, #510/#516).

Фрагменты run: — verbatim из реальных workflow (ai-review.yml, pr-review.yml,
plugin-forge.yml, deploy-dsh-edge.yml, repo-ci.yml) на момент написания теста,
не пересказ формата. Мутация «сними бит исполнения» проверяется явно —
образцовый случай #510.

Запуск: python -m pytest scripts/lib/test_exec_bit_guard.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("exec_bit_guard.py")
spec = importlib.util.spec_from_file_location("exec_bit_guard", SCRIPT)
ebg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ebg)  # type: ignore[union-attr]


# ── direct_script_invocations: находит прямой вызов ──────────────────────────

def test_finds_bare_invocation_ai_review():
    # verbatim ai-review.yml/pr-review.yml, шаг «Разбудить оркестратор» (#297/#456)
    run_text = 'scripts/gh/wake_orchestra.sh "$GITHUB_REPOSITORY"'
    assert ebg.direct_script_invocations(run_text) == ["scripts/gh/wake_orchestra.sh"]


def test_finds_invocation_inside_command_substitution():
    # verbatim plugin-forge.yml
    run_text = (
        'PR_URL=$(scripts/git/pr-create --base main --head "$BRANCH" '
        '--title "$PR_TITLE" --body "$PR_BODY")\n'
        'echo "pr_url=$PR_URL" >> "$GITHUB_OUTPUT"'
    )
    assert ebg.direct_script_invocations(run_text) == ["scripts/git/pr-create"]


# ── direct_script_invocations: НЕ путает вызов через интерпретатор ───────────

def test_skips_bash_prefixed_invocation():
    # verbatim repo-ci.yml
    run_text = "bash scripts/git/test/task-branch-epic-guard.smoke.sh"
    assert ebg.direct_script_invocations(run_text) == []


def test_skips_env_assignment_then_bash():
    # verbatim deploy-dsh-edge.yml — несколько присваиваний подряд перед bash
    run_text = "PLUGIN_ID=$id STATE=building SOURCE=forge bash scripts/plugins/plugin_status.sh"
    assert ebg.direct_script_invocations(run_text) == []


def test_skips_python_prefixed_invocation():
    run_text = "python scripts/orchestra/repo_invariants.py"
    assert ebg.direct_script_invocations(run_text) == []


def test_skips_heredoc_body():
    # содержимое heredoc — код другого интерпретатора (python), пути внутри
    # него — не shell-команды этого workflow.
    run_text = (
        "python - <<'PY'\n"
        "import glob\n"
        "scripts/not_a_real_command_just_text\n"
        "PY\n"
    )
    assert ebg.direct_script_invocations(run_text) == []


def test_skips_paths_outside_scanned_prefixes():
    run_text = "dsh-edge/verify-ingest-allowlist.mjs"
    assert ebg.direct_script_invocations(run_text) == []


def test_multiple_statements_only_first_word_each():
    run_text = "scripts/gh/wake_orchestra.sh \"$X\" && echo done"
    assert ebg.direct_script_invocations(run_text) == ["scripts/gh/wake_orchestra.sh"]


# ── check_exec_bit: пропускает исполняемые, ловит неисполняемые ─────────────

def test_check_exec_bit_no_violation_when_executable():
    steps = [("ai-review.yml", "Разбудить оркестратор",
              'scripts/gh/wake_orchestra.sh "$GITHUB_REPOSITORY"')]
    modes = {"scripts/gh/wake_orchestra.sh": "100755"}
    assert ebg.check_exec_bit(steps, modes) == []


def test_check_exec_bit_flags_non_executable_mode():
    # мутация: тот же случай, но бит исполнения снят (образцовый случай #510
    # ДО фикса #511 — PR #458 добавил файл с режимом 100644).
    steps = [("ai-review.yml", "Разбудить оркестратор",
              'scripts/gh/wake_orchestra.sh "$GITHUB_REPOSITORY"')]
    modes = {"scripts/gh/wake_orchestra.sh": "100644"}
    violations = ebg.check_exec_bit(steps, modes)
    assert len(violations) == 1
    assert violations[0]["path"] == "scripts/gh/wake_orchestra.sh"
    assert violations[0]["mode"] == "100644"
    assert violations[0]["workflow"] == "ai-review.yml"


def test_check_exec_bit_skips_untracked_path():
    # путь не найден в карте режимов (например $VAR не распознан как путь и
    # случайно совпал с шаблоном, или файл не отслежен git) — не наш класс,
    # ложных срабатываний не заводим.
    steps = [("some.yml", "step", "scripts/does/not/exist.sh")]
    assert ebg.check_exec_bit(steps, {}) == []


def test_check_exec_bit_bash_prefixed_never_flagged_even_if_not_executable():
    # файл без бита исполнения, но вызван через bash — не нарушение (бит не
    # нужен), проверяет что command_tokens правильно исключает такие случаи
    # на уровне direct_script_invocations, а не только по modes.
    steps = [("repo-ci.yml", "step", "bash scripts/plugins/plugin_status.sh")]
    modes = {"scripts/plugins/plugin_status.sh": "100644"}
    assert ebg.check_exec_bit(steps, modes) == []


# ── build_report на реальном дереве репозитория ──────────────────────────────

def test_build_report_on_real_repo_is_clean():
    """Живой прогон на настоящем .github/workflows и git-дереве этого
    репозитория: на момент написания теста нарушений нет (#510 уже
    зафиксирован #511) — это регрессионная страховка, а не фикстура."""
    violations = ebg.build_report()
    assert violations == [], violations
