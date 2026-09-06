#!/usr/bin/env python3
"""Тесты канарейки осиротевших тестов (scripts/lib/orphan_test_guard.py, #583).

Три слоя, каждый доказан отдельно:
  1. Чистая логика (`statement_tokens`, `_resolve`, `build_coverage`) — на
     фикстурах, без файловой системы/сети.
  2. Живой снимок текущего репозитория (`test_repo_has_no_orphan_tests`) —
     это и есть сама канарейка: любой новый тестовый файл без единого шага
     CI красит именно этот тест. Мутация, которой это доказано: создай
     временный `scripts/lib/test__scratch_orphan.py` без соответствующего
     шага ни в одном workflow — тест ниже краснеет с точным путём файла;
     удали файл — тест снова зелёный (см. также ручной прогон
     `python scripts/lib/orphan_test_guard.py` в описании PR).
  3. Самопроверка (`test_own_test_file_is_named_in_repo_ci`) — канарейка не
     доверяет только своей же generic-логике для себя самой: явно проверяет,
     что `scripts/lib/test_orphan_test_guard.py` назван строкой в
     .github/workflows/repo-ci.yml, независимо от build_report().

Запуск: python -m pytest scripts/lib/test_orphan_test_guard.py -q
"""

import importlib.util
import textwrap
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("orphan_test_guard", _DIR / "orphan_test_guard.py")
otg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otg)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Чистая логика: statement_tokens ────────────────────────────────────────

def test_statement_tokens_splits_pytest_and_pip_install_on_separate_lines():
    run_text = textwrap.dedent("""\
        pip install --quiet pytest pyyaml
        python -m pytest scripts/lib/test_foo.py -q
    """)
    statements = otg.statement_tokens(run_text)
    assert ["pip", "install", "--quiet", "pytest", "pyyaml"] in statements
    assert ["python", "-m", "pytest", "scripts/lib/test_foo.py", "-q"] in statements


def test_statement_tokens_skips_leading_assignments():
    statements = otg.statement_tokens("HANDS_TOKEN=dev-token bash scripts/foo.sh")
    assert statements == [["bash", "scripts/foo.sh"]]


def test_statement_tokens_ignores_heredoc_body():
    run_text = textwrap.dedent("""\
        python - <<'PY'
        import pytest
        pytest.main(["scripts/lib/test_should_not_count.py"])
        PY
    """)
    statements = otg.statement_tokens(run_text)
    # Тело heredoc не читается как отдельные shell-statement'ы: первым словом
    # первого statement остаётся сам вызов интерпретатора, не "import"/"pytest.main".
    assert statements[0][:2] == ["python", "-"]
    joined = [" ".join(s) for s in statements]
    assert not any("test_should_not_count.py" in s for s in joined if s != " ".join(statements[0]))


def test_statement_tokens_strips_comments():
    statements = otg.statement_tokens("bash scripts/foo.sh # не запускать test_bar.py тут")
    assert statements == [["bash", "scripts/foo.sh"]]


# ── Чистая логика: _resolve / _is_dir_like ─────────────────────────────────

def test_resolve_joins_cwd_and_relative_arg():
    assert otg._resolve("cf-worker", "test/foo.spec.ts") == "cf-worker/test/foo.spec.ts"


def test_resolve_with_no_cwd_is_repo_relative():
    assert otg._resolve(None, "scripts/lib/test_foo.py") == "scripts/lib/test_foo.py"


def test_resolve_returns_none_for_dynamic_github_actions_expression():
    # Динамический working-directory (${{ github.workspace }}/deploy и т.п.) —
    # не резолвится в путь репозитория, безопасная сторона ошибки: такой шаг
    # просто не учитывается в покрытии, ложноположительных «покрыт» не даёт.
    assert otg._resolve("${{ github.workspace }}/deploy", "foo.py") is None


def test_is_dir_like_true_for_trailing_slash_and_extensionless():
    assert otg._is_dir_like("scripts/lib/")
    assert otg._is_dir_like("scripts/lib")
    assert not otg._is_dir_like("scripts/lib/test_foo.py")


# ── Чистая логика: build_coverage ───────────────────────────────────────────

def _step(run_text: str, cwd: str | None = None) -> dict:
    return {"workflow": "fixture.yml", "job": "test", "step": "fixture", "cwd": cwd, "run": run_text}


def test_build_coverage_exact_pytest_file():
    files = ["scripts/lib/test_a.py", "scripts/lib/test_b.py"]
    steps = [_step("python -m pytest scripts/lib/test_a.py -q")]
    assert otg.build_coverage(steps, files) == {"scripts/lib/test_a.py"}


def test_build_coverage_pytest_directory_covers_every_file_under_it():
    files = ["scripts/lib/test_a.py", "scripts/lib/test_b.py", "scripts/orchestra/test_c.py"]
    steps = [_step("pytest scripts/lib/")]
    assert otg.build_coverage(steps, files) == {"scripts/lib/test_a.py", "scripts/lib/test_b.py"}


def test_build_coverage_node_test_exact_file():
    files = ["scripts/dsh-hands-streamer/test/streamer.test.mjs"]
    steps = [_step("node --test scripts/dsh-hands-streamer/test/streamer.test.mjs")]
    assert otg.build_coverage(steps, files) == set(files)


def test_build_coverage_npm_test_covers_vitest_spec_under_working_directory():
    files = ["cf-worker/test/pulse.spec.ts", "cf-worker/test/harness.spec.ts", "scripts/lib/test_a.py"]
    steps = [_step("npm ci", cwd="cf-worker"), _step("npm test", cwd="cf-worker")]
    covered = otg.build_coverage(steps, files)
    assert covered == {"cf-worker/test/pulse.spec.ts", "cf-worker/test/harness.spec.ts"}


def test_build_coverage_bash_direct_and_via_interpreter():
    files = ["scripts/gh/test/issue-create.test.sh", "scripts/git/test/task-branch.test.sh"]
    steps = [
        _step("bash scripts/gh/test/issue-create.test.sh"),
        _step("scripts/git/test/task-branch.test.sh"),
    ]
    assert otg.build_coverage(steps, files) == set(files)


def test_build_coverage_leaves_unmentioned_file_uncovered():
    files = ["scripts/lib/test_orphan.py"]
    steps = [_step("python -m pytest scripts/lib/test_other.py -q")]
    assert otg.build_coverage(steps, files) == set()


# ── Газ (легальные исключения) ──────────────────────────────────────────────

def test_own_marker_literal_is_not_self_exempting(tmp_path):
    # Регресс живой находки (#583): файл, который лишь УПОМИНАЕТ строку
    # маркера как тестовую фикстуру (не начинает ею строку исходника — маркер
    # спрятан внутри вызова write_text(...)), не должен засчитывать сам себя
    # в газ. Ровно эта форма есть в этом же test_orphan_test_guard.py ниже —
    # без анкера на начало строки канарейка на первом же прогоне против самой
    # себя молча объявляла себя «в газе».
    path = tmp_path / "test_looks_like_fixture.py"
    path.write_text('    with_reason.write_text("# ORPHAN-TEST-OK: чужая причина")\n')
    assert otg.read_exemption(path) is None


def test_read_exemption_requires_non_empty_reason(tmp_path):
    with_reason = tmp_path / "test_with_reason.py"
    with_reason.write_text("# ORPHAN-TEST-OK: требует боевого секрета, гоняется вручную\n")
    assert otg.read_exemption(with_reason) == "требует боевого секрета, гоняется вручную"

    without_marker = tmp_path / "test_without_marker.py"
    without_marker.write_text("# обычный комментарий\n")
    assert otg.read_exemption(without_marker) is None


def test_build_report_excludes_exempted_file_from_orphans(tmp_path, monkeypatch):
    exempt = tmp_path / "scripts" / "lib"
    exempt.mkdir(parents=True)
    test_file = exempt / "test_exempt_example.py"
    test_file.write_text("# ORPHAN-TEST-OK: живой пример газа для теста канарейки\n")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text("jobs:\n  test:\n    steps: []\n")

    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows)
    assert report["orphans"] == []
    assert report["exemptions"] == {"scripts/lib/test_exempt_example.py": "живой пример газа для теста канарейки"}


def test_build_report_flags_unwired_file_as_orphan(tmp_path):
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "test_unwired.py").write_text("def test_x():\n    assert True\n")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text("jobs:\n  test:\n    steps: []\n")

    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows)
    assert report["orphans"] == ["scripts/lib/test_unwired.py"]


# ── Живой снимок текущего репозитория — сама канарейка ─────────────────────

def test_repo_has_no_orphan_tests():
    report = otg.build_report()
    assert report["orphans"] == [], (
        "осиротевшие тесты (файл лежит, но не запускается ни одним workflow): "
        f"{report['orphans']} — см. вывод `python scripts/lib/orphan_test_guard.py` "
        "для точной подсказки, куда добавить шаг"
    )


def test_repo_discovers_more_than_a_handful_of_test_files():
    # Страховка от тихо сломанной файловой находки (пустой список files всегда
    # даёт report["orphans"] == [] — зелёным по неверной причине).
    report = otg.build_report()
    assert report["total"] > 30, f"найдено подозрительно мало тестовых файлов: {report['total']}"


# ── Самопроверка: канарейка не может осиротеть сама (#583, требование 5) ───

def test_own_test_file_is_named_in_repo_ci():
    repo_ci = (REPO_ROOT / ".github" / "workflows" / "repo-ci.yml").read_text(encoding="utf-8")
    assert "scripts/lib/test_orphan_test_guard.py" in repo_ci, (
        "канарейка осиротевших тестов сама не подключена к repo-ci.yml — "
        "первая же осиротела бы"
    )
    assert "scripts/lib/orphan_test_guard.py" in repo_ci, (
        "живой снимок канарейки (python scripts/lib/orphan_test_guard.py) "
        "не вызывается в repo-ci.yml"
    )
