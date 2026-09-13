#!/usr/bin/env python3
"""Тесты канарейки «мимо двери нельзя» (scripts/lib/door_guard.py, #611).

Фрагменты в test_scan_lines_* — verbatim строки из реальных файлов этого
репозитория на момент написания (pool_issue.py, dispatch_tail.py,
task.sh, upstream_drift.py) — не пересказ формы. Мутационная проверка на
временном файле (test_build_report_flags_synthetic_bypass /
test_build_report_clean_after_removing_bypass) — обязательное требование
задачи: добавить реальный обход → канарейка красит с точным file:line,
убрать → снова зелено.

Запуск: python -m pytest scripts/lib/test_door_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("door_guard", _DIR / "door_guard.py")
dg = importlib.util.module_from_spec(_SPEC)
# dataclass(frozen=True) в door_guard.py резолвит postponed-аннотации через
# sys.modules[cls.__module__] — модуль обязан быть зарегистрирован ДО exec_module,
# иначе AttributeError: 'NoneType' object has no attribute '__dict__'.
sys.modules["door_guard"] = dg
_SPEC.loader.exec_module(dg)  # type: ignore[union-attr]


# ── scan_lines: CLI-форма находит настоящий вызов ────────────────────────────

def test_cli_finds_bare_gh_issue_create_at_line_start():
    violations = dg.scan_lines("scripts/whatever.sh", ['gh issue create --title "x"'], ".sh")
    assert len(violations) == 1
    assert violations[0].door == "issue-create"
    assert violations[0].line == 1


def test_cli_finds_gh_pr_create_after_exec():
    violations = dg.scan_lines("scripts/whatever.sh", ['exec gh pr create "${args[@]}"'], ".sh")
    assert [v.door for v in violations] == ["pr-create"]


def test_cli_finds_gh_issue_create_after_dollar_paren():
    violations = dg.scan_lines("scripts/whatever.py", ['out = subprocess.run("$(gh issue create --title x)")'], ".py")
    # $( — командная позиция форс-открытия сабшелла, тот же класс.
    assert any(v.door == "issue-create" for v in violations)


def test_cli_finds_gh_pr_create_after_semicolon():
    violations = dg.scan_lines("scripts/whatever.sh", ['foo; gh pr create --title x'], ".sh")
    assert [v.door for v in violations] == ["pr-create"]


# ── scan_lines: CLI-форма НЕ путает прозу/докстринг с вызовом ───────────────

def test_cli_skips_whole_line_comment():
    # verbatim scripts/gh/test/issue-create.test.sh:3
    line = "# отклоняется ДО вызова `gh issue create` — реальный `gh` не вызывается вовсе"
    assert dg.scan_lines("scripts/whatever.sh", [line], ".sh") == []


def test_cli_skips_mention_inside_prose_string():
    # verbatim scripts/worker/task.sh:296 (обёртка над gh pr create — не вызов)
    line = ('route_pr_step="Открой PR в main: scripts/git/pr-create ... '
            '(обёртка над gh pr create — тот же интерфейс, issue #496)"')
    assert dg.scan_lines("scripts/worker/task.sh", [line], ".sh") == []


def test_cli_skips_docstring_mention_in_backticks():
    # verbatim scripts/lib/pool_issue.py:17 (docstring, не # комментарий)
    line = "Канал агентских/ручных запросов (`gh issue create` в терминале, вне"
    assert dg.scan_lines("scripts/lib/pool_issue.py", [line], ".py") == []


def test_cli_skips_gh_pr_view_and_list_not_create():
    assert dg.scan_lines("scripts/whatever.sh", ["gh pr view 453", "gh issue list --label task"], ".sh") == []


# ── scan_lines: REST-форма находит настоящий POST на коллекцию ─────────────

def test_rest_finds_raw_post_to_issues_collection():
    # verbatim scripts/lib/pool_issue.py:54 форма (без самого файла-двери)
    line = 'args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}"]'
    violations = dg.scan_lines("scripts/orchestra/some_new_watchdog.py", [line], ".py")
    assert [v.door for v in violations] == ["issue-create"]


def test_rest_finds_raw_post_to_pulls_collection_python():
    # verbatim scripts/measure/dispatch_tail.py:616
    line = 'gh.request("POST", f"/repos/{gh.repo}/pulls", {'
    violations = dg.scan_lines("scripts/measure/dispatch_tail.py", [line], ".py")
    assert [v.door for v in violations] == ["pr-create"]


def test_rest_finds_raw_post_to_issues_collection_js_method_on_next_line():
    # verbatim cf-worker/src/harness.ts:1653-1654 форма — URL и method на РАЗНЫХ строках
    lines = [
        "      const res = await fetch(`${GITHUB.apiBase}/repos/${repo}/issues`, {",
        "        method: 'POST',",
    ]
    violations = dg.scan_lines("cf-worker/src/some_new_caller.ts", lines, ".ts")
    assert [v.door for v in violations] == ["issue-create"]


# ── scan_lines: REST-форма НЕ путает под-ресурс/чтение с созданием ─────────

def test_rest_skips_sub_resource_labels():
    line = 'gh("-X", "POST", f"repos/{repo}/issues/{number}/labels", "-f", f"labels[]={label}")'
    assert dg.scan_lines("scripts/orchestra/scheduler.py", [line], ".py") == []


def test_rest_skips_sub_resource_comments():
    line = 'gh("-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", "body=" + text)'
    assert dg.scan_lines("scripts/orchestra/pulse_guard.py", [line], ".py") == []


def test_rest_skips_read_with_query_string():
    line = 'found=$(gh api "repos/$GITHUB_REPOSITORY/pulls?head=$HEAD_OWNER:$RUN_BRANCH&state=open")'
    assert dg.scan_lines("some_workflow_step.sh", [line], ".sh") == []


def test_rest_skips_endpoint_without_post_marker_in_window():
    # ссылка на коллекцию без POST рядом (например, комментарий про READ) — не наш класс
    lines = ["x = f\"repos/{repo}/issues\"  # список задач, GET"] + ["#noop"] * 6
    assert dg.scan_lines("scripts/whatever.py", lines, ".py") == []


# ── газ: легитимное исключение с причиной пропускается, без причины — нет ──

def test_gas_exception_on_same_line_is_honored():
    line = 'exec gh issue create "${args[@]}"  # door-exception: сама дверь, первое честное исключение'
    assert dg.scan_lines("scripts/some_copy.sh", [line], ".sh") == []


def test_gas_exception_on_line_above_is_honored():
    lines = [
        "# door-exception: кампания #4 управляет одним идемпотентным черновиком PR",
        'gh.request("POST", f"/repos/{gh.repo}/pulls", {',
    ]
    assert dg.scan_lines("scripts/measure/dispatch_tail.py", lines, ".py") == []


def test_gas_exception_too_far_above_is_not_honored():
    lines = ["# door-exception: причина"] + ["pass"] * (dg.GAS_WINDOW + 1) + [
        'gh.request("POST", f"/repos/{gh.repo}/pulls", {'
    ]
    assert len(dg.scan_lines("scripts/measure/dispatch_tail.py", lines, ".py")) == 1


def test_bare_door_exception_marker_without_reason_does_not_suppress():
    # Пустая причина (голое "door-exception:" без текста) — не считается газом,
    # иначе можно было бы заглушить проверку без объяснения.
    line = 'exec gh pr create "${args[@]}"  # door-exception:'
    assert len(dg.scan_lines("scripts/some_copy.sh", [line], ".sh")) == 1


# ── door_files: сама дверь исключена по имени файла, не по хардкоду в regex ─

def test_door_file_itself_is_never_flagged():
    assert dg.scan_lines("scripts/gh/issue-create", ['exec gh issue create "${args[@]}"'], ".sh") == []
    assert dg.scan_lines("scripts/git/pr-create", ['exec gh pr create "${args[@]}"'], ".sh") == []
    assert dg.scan_lines(
        "scripts/lib/pool_issue.py",
        ['args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}"]'],
        ".py",
    ) == []


# ── мутационная проверка обязательна (требование задачи #611) ──────────────

def test_build_report_flags_synthetic_bypass_then_clean_after_removal(tmp_path):
    """Добавь во временный файл прямой вызов создания issue мимо двери →
    канарейка красит с точным file:line. Удали → снова зелено."""
    repo = tmp_path
    (repo / "scripts").mkdir()
    target = repo / "scripts" / "sneaky_watchdog.py"

    target.write_text('gh("-X", "POST", f"repos/{repo}/issues", "-f", f"title={t}")\n', encoding="utf-8")
    violations = dg.scan_repo(repo)
    assert len(violations) == 1
    assert violations[0].file == "scripts/sneaky_watchdog.py"
    assert violations[0].line == 1
    assert violations[0].door == "issue-create"

    target.write_text('gh("-X", "POST", f"repos/{repo}/issues/{n}/comments", "-f", f"body={t}")\n', encoding="utf-8")
    assert dg.scan_repo(repo) == []


def test_build_report_legit_operations_never_flagged(tmp_path):
    """Ложное срабатывание на комментарий/метку/чтение дороже пропуска — не
    должно краснеть ни одно из них."""
    repo = tmp_path
    (repo / "scripts" / "orchestra").mkdir(parents=True)
    target = repo / "scripts" / "orchestra" / "legit.py"
    target.write_text(
        "\n".join([
            'gh("-X", "POST", f"repos/{repo}/issues/{n}/comments", "-f", "body=" + text)',
            'gh("-X", "POST", f"repos/{repo}/issues/{n}/labels", "-f", f"labels[]={label}")',
            'gh("-X", "POST", f"repos/{repo}/issues/{n}/assignees", "-f", f"assignees[]={login}")',
            'info = gh("-X", "GET", f"repos/{repo}/issues/{n}")',
            'gh("-X", "GET", "search/issues", "-f", f"q={query}")',
        ]) + "\n",
        encoding="utf-8",
    )
    assert dg.scan_repo(repo) == []


# ── реальное дерево репозитория: регрессионная страховка ───────────────────

def test_build_report_on_real_repo_is_clean():
    """Живой прогон на настоящем дереве репозитория — после того как это PR
    расставил `door-exception:` на известных легитимных сторонних дверях
    (cf-worker/harness.ts, plugins-src/runner-bridge), нарушений не остаётся."""
    violations = dg.scan_repo()
    assert violations == [], [v.message() for v in violations]
