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

def _step(run_text: str, cwd: str | None = None, workflow: str = "repo-ci.yml", job: str = "test") -> dict:
    return {"workflow": workflow, "job": job, "step": "fixture", "cwd": cwd, "run": run_text}


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
    path.write_text('    with_reason.write_text("# ORPHAN-TEST-OK: чужая причина")\n', encoding="utf-8")
    assert otg.read_exemption(path) is None


def test_read_exemption_requires_non_empty_reason(tmp_path):
    with_reason = tmp_path / "test_with_reason.py"
    with_reason.write_text("# ORPHAN-TEST-OK: требует боевого секрета, гоняется вручную\n", encoding="utf-8")
    assert otg.read_exemption(with_reason) == "требует боевого секрета, гоняется вручную"

    without_marker = tmp_path / "test_without_marker.py"
    without_marker.write_text("# обычный комментарий\n", encoding="utf-8")
    assert otg.read_exemption(without_marker) is None


def test_build_report_excludes_exempted_file_from_orphans(tmp_path, monkeypatch):
    exempt = tmp_path / "scripts" / "lib"
    exempt.mkdir(parents=True)
    test_file = exempt / "test_exempt_example.py"
    test_file.write_text("# ORPHAN-TEST-OK: живой пример газа для теста канарейки\n", encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text("jobs:\n  test:\n    steps: []\n", encoding="utf-8")

    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows)
    assert report["orphans"] == []
    assert report["exemptions"] == {"scripts/lib/test_exempt_example.py": "живой пример газа для теста канарейки"}


def test_build_report_default_catalog_dir_resolves_from_repo_root_not_live_catalog(tmp_path):
    # Ревью PR #771, minor 10: `catalog_dir` по умолчанию раньше был
    # захардкоженной константой GUARD_CATALOG_DIR (живой каталог ЭТОГО
    # репозитория), а не производной от repo_root вызова — build_report на
    # синтетической фикстуре без явного catalog_dir тихо читал бы файлы
    # scripts/ci/guards/ настоящего репозитория. Здесь repo_root — tmp_path,
    # где каталога scripts/ci/guards/ вовсе нет — orphans не должен
    # схлопнуться в 0 по инерции живого каталога.
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "test_default_catalog.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "repo-ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n"
        "      - name: perebor\n"
        f"        run: bash {otg.GUARD_CATALOG_RUNNER}\n",
        encoding="utf-8",
    )
    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows)
    assert report["orphans"] == ["scripts/lib/test_default_catalog.py"]


def test_suggest_mentions_guard_catalog_alternative_for_py_mjs_sh():
    # Ревью PR #771, minor 8: раньше единственная подсказка сироте была
    # «допиши рукописный шаг в repo-ci.yml» — ровно то, за что краснеет
    # гвардия рецидива (#749, ci_guard_registration_guard.py). Обе гвардии
    # одного PR не должны давать взаимоисключающие инструкции.
    assert "scripts/ci/guards/" in otg._suggest("scripts/lib/test_x.py")
    assert "scripts/ci/guards/" in otg._suggest("scripts/lib/test/x.test.mjs")
    assert "scripts/ci/guards/" in otg._suggest("scripts/lib/test/x.test.sh")


def test_build_report_flags_unwired_file_as_orphan(tmp_path):
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "test_unwired.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "fixture.yml").write_text("jobs:\n  test:\n    steps: []\n", encoding="utf-8")

    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows)
    assert report["orphans"] == ["scripts/lib/test_unwired.py"]


# ── Каталог гвардий (#749): покрытие через опосредованный перебор ──────────

def test_catalog_runner_not_wired_when_no_step_calls_it():
    steps = [_step("python -m pytest scripts/lib/test_a.py -q")]
    assert otg._catalog_runner_is_wired(steps) is False


def test_catalog_runner_wired_when_bash_step_calls_it():
    steps = [_step(f"bash {otg.GUARD_CATALOG_RUNNER}")]
    assert otg._catalog_runner_is_wired(steps) is True


def test_catalog_runner_not_wired_when_called_from_wrong_workflow():
    # Ревью PR #771, minor 7 — комбинированная мутация: шаг-перебор убран из
    # required repo-ci.yml и добавлен в НЕОБЯЗАТЕЛЬНЫЙ workflow (например
    # deploy-dsh-edge.yml с continue-on-error) — required-гейт каталог вообще
    # не исполняет, поэтому это НЕ должно засчитываться как «подключено».
    steps = [_step(f"bash {otg.GUARD_CATALOG_RUNNER}", workflow="deploy-dsh-edge.yml", job="deploy")]
    assert otg._catalog_runner_is_wired(steps) is False


def test_catalog_runner_not_wired_when_called_from_wrong_job():
    # Тот же класс минор 7 — тот же файл repo-ci.yml, но другой job (не
    # `test`, required-контекст защиты ветки) тоже не должен засчитываться.
    steps = [_step(f"bash {otg.GUARD_CATALOG_RUNNER}", job="lint")]
    assert otg._catalog_runner_is_wired(steps) is False


def test_iter_guard_catalog_steps_reads_every_sh_file(tmp_path):
    (tmp_path / "a.sh").write_text("python -m pytest scripts/lib/test_a.py -q\n", encoding="utf-8")
    (tmp_path / "b.sh").write_text("node --test scripts/x/test/y.test.mjs\n", encoding="utf-8")
    steps = otg.iter_guard_catalog_steps(tmp_path)
    assert {s["step"] for s in steps} == {"a.sh", "b.sh"}


def test_iter_guard_catalog_steps_empty_dir_returns_empty_list(tmp_path):
    assert otg.iter_guard_catalog_steps(tmp_path / "does-not-exist") == []


def test_build_report_covers_test_invoked_only_inside_catalog_file(tmp_path):
    # Мутация, доказывающая класс #749: тест-файл, чей pytest-вызов лежит
    # ТОЛЬКО внутри scripts/ci/guards/<имя>.sh (не в самом workflow), не
    # считается осиротевшим — потому что каталог реально перебирается
    # шагом workflow (bash scripts/ci/run_guards.sh).
    lib = tmp_path / "scripts" / "lib"
    lib.mkdir(parents=True)
    (lib / "test_via_catalog.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    # Имя файла и job — ИМЕННО `repo-ci.yml`/`test` (ревью PR #771, minor 7):
    # только этот workflow+job считается required-подключением перебора.
    (workflows / "repo-ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n"
        "      - name: perebor\n"
        f"        run: bash {otg.GUARD_CATALOG_RUNNER}\n",
        encoding="utf-8",
    )

    catalog = tmp_path / "scripts" / "ci" / "guards"
    catalog.mkdir(parents=True)
    (catalog / "via-catalog.sh").write_text(
        "python -m pytest scripts/lib/test_via_catalog.py -q\n", encoding="utf-8"
    )

    report = otg.build_report(repo_root=tmp_path, workflows_dir=workflows, catalog_dir=catalog)
    assert report["orphans"] == []

    # Убери перебор из workflow (никто больше не вызывает run_guards.sh) —
    # тот же тест-файл снова осиротевший, а не молча остаётся зелёным по
    # инерции старого прогона: catalog_dir существует, но не подключён.
    (workflows / "repo-ci.yml").write_text(
        "jobs:\n  test:\n    steps: []\n", encoding="utf-8"
    )
    report_unwired = otg.build_report(repo_root=tmp_path, workflows_dir=workflows, catalog_dir=catalog)
    assert report_unwired["orphans"] == ["scripts/lib/test_via_catalog.py"]


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
