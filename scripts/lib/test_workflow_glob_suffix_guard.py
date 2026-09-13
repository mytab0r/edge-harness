#!/usr/bin/env python3
"""Тесты гвардии `workflow_glob_suffix_guard.py`.

Фикстуры — не пересказ, а прод-форма (правило репозитория «тест кормит
прод-форму данных, а не пересказ», AGENTS.md):

  - `_EXEC_BIT_GUARD_SNAPSHOT` — дословный снимок функции
    `iter_workflow_run_steps` из `scripts/lib/exec_bit_guard.py`, каким он был
    ДО фикса этой же задачи (issue #635; снят живым прогоном гвардии на
    непочиненном `origin/main` — `.glob("*.yml")` красил гвардию). Заморожен
    как константа, чтобы регрессия ловилась независимо от того, что случится
    с реальным файлом дальше.
  - Позитивные контроли читают РЕАЛЬНЫЕ файлы репозитория с диска
    (`collect_labels.py`, `test_infra_gh_inventory.py` — образцы починки того
    же класса) и РЕАЛЬНЫЕ уже исправленные файлы (`exec_bit_guard.py`,
    `orphan_test_guard.py`, `test_pr_body_label_release_reachable.py`) —
    никаких синтетических копий, живой код репозитория на момент прогона.

Ключевая улика (докстринг `workflow_glob_suffix_guard.py`, дословный вывод
прогона на непочиненном `origin/main`, сохранён как факт в PR): гвардия
называла РОВНО эти три места и не называла `collect_labels.py`/
`test_infra_gh_inventory.py`:

    ::error::scripts/lib/exec_bit_guard.py:120 [iter_workflow_run_steps]: .glob("*.yml") ...
    ::error::scripts/lib/orphan_test_guard.py:161 [iter_workflow_run_steps]: .glob("*.yml") ...
    ::error::scripts/lib/test_pr_body_label_release_reachable.py:169 [test_no_new_body_gated_label_release_without_edited]: .glob("*.yml") ...
"""

import ast
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("workflow_glob_suffix_guard.py")
spec = importlib.util.spec_from_file_location("workflow_glob_suffix_guard", SCRIPT)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)  # type: ignore[union-attr]

REPO_ROOT = guard.REPO_ROOT

# Дословный снимок scripts/lib/exec_bit_guard.py::iter_workflow_run_steps ДО
# фикса этой задачи — реальный код, реально живший в main, замороженный как
# регрессионная фикстура (тот же приём, каким test_orphan_test_guard.py
# фиксирует исторические фрагменты).
_EXEC_BIT_GUARD_SNAPSHOT = '''
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def iter_workflow_run_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[tuple[str, str, str]]:
    steps = []
    for path in sorted(workflows_dir.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run_text = step.get("run")
                if isinstance(run_text, str):
                    steps.append((path.name, step.get("name", "(без имени)"), run_text))
    return steps
'''

# Дословный снимок module-level использования из
# scripts/lib/test_pr_body_label_release_reachable.py ДО фикса — цикл вне
# функции, не внутри def (проверяет ветку «scope = <module>»).
_MODULE_LEVEL_SNAPSHOT = '''
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
'''


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_flags_frozen_exec_bit_guard_snapshot():
    """Прод-форма (шаг 2а поручения): дословный код гвардии со сканом
    каталога — не двухстрочная заглушка — обязан краснеть."""
    violations = guard.find_violations(_EXEC_BIT_GUARD_SNAPSHOT, "exec_bit_guard.py")
    assert len(violations) == 1
    assert violations[0]["pattern"] == "*.yml"
    assert violations[0]["scope"] == "iter_workflow_run_steps"
    assert violations[0]["method"] == "glob"


def test_flags_frozen_module_level_snapshot():
    """Тот же класс вне функции (scope = <module>) — ветка, которую
    exec_bit_guard-снимок не покрывает (там вызов внутри def)."""
    violations = guard.find_violations(_MODULE_LEVEL_SNAPSHOT, "test_pr_body_label_release_reachable.py")
    assert len(violations) == 1
    assert violations[0]["scope"] == "<module>"


def test_snapshot_still_parses_as_valid_python():
    """Фикстура обязана оставаться исполнимым Python — иначе find_violations
    молча тестировал бы не то, что заявлено (SyntaxError сигнализирует явно,
    но лучше поймать до этого)."""
    ast.parse(_EXEC_BIT_GUARD_SNAPSHOT)
    ast.parse(_MODULE_LEVEL_SNAPSHOT)


def test_real_collect_labels_is_clean():
    """Позитивный контроль на РЕАЛЬНОМ файле репозитория (не копии): union
    двух glob-вызовов (scripts/lib/collect_labels.py:112) не должен краситься."""
    text = _read("scripts/lib/collect_labels.py")
    assert "WORKFLOWS_DIR.glob" in text, "фикстура ожидает живой сайт скана — иначе тест ничего не проверяет"
    assert guard.find_violations(text, "collect_labels.py") == []


def test_real_test_infra_gh_inventory_is_clean():
    """Позитивный контроль: комбинированный паттерн '*.y*ml'
    (scripts/lib/test_infra_gh_inventory.py:41) не должен краситься."""
    text = _read("scripts/lib/test_infra_gh_inventory.py")
    assert '"*.y*ml"' in text, "фикстура ожидает живой сайт скана — иначе тест ничего не проверяет"
    assert guard.find_violations(text, "test_infra_gh_inventory.py") == []


def test_real_exec_bit_guard_is_fixed():
    """Регрессия на реальный файл ПОСЛЕ фикса этой задачи — красный, если
    кто-то откатит починку exec_bit_guard.py к одиночному '*.yml'."""
    text = _read("scripts/lib/exec_bit_guard.py")
    assert guard.find_violations(text, "exec_bit_guard.py") == []


def test_real_orphan_test_guard_is_fixed():
    text = _read("scripts/lib/orphan_test_guard.py")
    assert guard.find_violations(text, "orphan_test_guard.py") == []


def test_real_test_pr_body_label_release_reachable_is_fixed():
    text = _read("scripts/lib/test_pr_body_label_release_reachable.py")
    assert guard.find_violations(text, "test_pr_body_label_release_reachable.py") == []


def test_unrelated_glob_on_workflow_dir_is_not_flagged():
    """`.glob()` на каталог workflow с паттерном, не относящимся к yml/yaml
    (например поиск README), — не наш класс, не должен краситься."""
    source = '''
from pathlib import Path
REPO_ROOT = Path(".")
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

def find_readmes():
    return list(WORKFLOWS_DIR.glob("*.md"))
'''
    assert guard.find_violations(source, "<string>") == []


def test_unrelated_glob_elsewhere_is_not_flagged():
    """`.glob("*.yml")` на каталог, НЕ являющийся `.github/workflows`
    (например `scripts/` для python-файлов) — не наш класс: имя переменной
    не резолвится в workflow_dir_names."""
    source = '''
from pathlib import Path
REPO_ROOT = Path(".")
SCRIPTS_DIR = REPO_ROOT / "scripts"

def find_py():
    return list(SCRIPTS_DIR.glob("*.py"))
'''
    assert guard.find_violations(source, "<string>") == []


def test_exemption_with_reason_silences_violation():
    text = _EXEC_BIT_GUARD_SNAPSHOT + "\n# WORKFLOW-DIR-GLOB-OK: тестовая фикстура, реальный файл починен отдельно\n"
    assert guard.find_violations(text, "<string>") != []  # находки сами по себе не меняются
    assert guard.read_exemption(text) == "тестовая фикстура, реальный файл починен отдельно"


def test_exemption_without_reason_is_not_gas():
    """Пустая причина — не газ (требование поручения): тормоз без названного
    условия возврата не принимается (AGENTS.md «Тормоз без газа»)."""
    text = _EXEC_BIT_GUARD_SNAPSHOT + "\n# WORKFLOW-DIR-GLOB-OK:\n"
    assert guard.read_exemption(text) is None

    text_blank = _EXEC_BIT_GUARD_SNAPSHOT + "\n# WORKFLOW-DIR-GLOB-OK:    \n"
    assert guard.read_exemption(text_blank) is None


def test_exemption_marker_anchored_to_line_start_not_substring_anywhere():
    """Находка AI-ревью на orphan_test_guard.py, тот же класс здесь: строка,
    несущая маркер как ЛИТЕРАЛ фикстуры (не начинающаяся с '#'), не должна
    засчитываться газом — иначе собственный тест этой гвардии ложно исключал
    бы сам себя."""
    text = 'assert "# WORKFLOW-DIR-GLOB-OK: reason" in some_string\n'
    assert guard.read_exemption(text) is None


def test_build_report_flags_frozen_snapshot_end_to_end(tmp_path):
    """`build_report` целиком: временное дерево scripts/, воспроизводящее
    прод-форму (снимок + рядом «здоровый» реальный файл), и реальный
    .github/workflows этого репозитория (санитарная проверка — требование
    «сама сканирует свои входы по обоим суффиксам»)."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "offender.py").write_text(_EXEC_BIT_GUARD_SNAPSHOT, encoding="utf-8")
    (scripts_dir / "clean.py").write_text(_read("scripts/lib/collect_labels.py"), encoding="utf-8")

    report = guard.build_report(scripts_dir=scripts_dir, workflows_dir=guard.WORKFLOWS_DIR)
    assert report["scanned"] == 2
    assert len(report["violations"]) == 1
    assert report["violations"][0]["path"] == "scripts/offender.py"


def test_build_report_respects_exemption(tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    exempted = _EXEC_BIT_GUARD_SNAPSHOT + "\n# WORKFLOW-DIR-GLOB-OK: осознанное исключение теста\n"
    (scripts_dir / "offender.py").write_text(exempted, encoding="utf-8")

    report = guard.build_report(scripts_dir=scripts_dir, workflows_dir=guard.WORKFLOWS_DIR)
    assert report["violations"] == []
    assert report["exemptions"] == {"scripts/offender.py": "осознанное исключение теста"}


def test_build_report_fails_loud_on_empty_scripts_dir(tmp_path):
    """Класс «тихий ноль»: пустой каталог кандидатов — не «нарушений нет»,
    а ошибка конфигурации, обязана падать громко."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    try:
        guard.build_report(scripts_dir=empty_dir, workflows_dir=guard.WORKFLOWS_DIR)
        assert False, "build_report обязан падать на пустом каталоге кандидатов, а не молчать"
    except RuntimeError as exc:
        assert "0 python-файлов" in str(exc)


def test_build_report_fails_loud_on_empty_workflows_dir(tmp_path):
    """Тот же класс для второго входа гвардии — каталог workflow."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "clean.py").write_text(_read("scripts/lib/collect_labels.py"), encoding="utf-8")
    empty_workflows = tmp_path / "workflows-empty"
    empty_workflows.mkdir()
    try:
        guard.build_report(scripts_dir=scripts_dir, workflows_dir=empty_workflows)
        assert False, "build_report обязан падать на пустом каталоге workflow, а не молчать"
    except RuntimeError as exc:
        assert "workflow" in str(exc)


def test_workflow_dir_has_files_scans_both_suffixes(tmp_path):
    """Прямая проверка требования «сама сканирует свои входы по обоим
    суффиксам»: каталог, несущий только *.yaml (без единого *.yml), обязан
    засчитываться непустым."""
    only_yaml = tmp_path / "only-yaml"
    only_yaml.mkdir()
    (only_yaml / "one.yaml").write_text("name: x\n", encoding="utf-8")
    assert guard.workflow_dir_has_files(only_yaml) is True

    empty = tmp_path / "empty"
    empty.mkdir()
    assert guard.workflow_dir_has_files(empty) is False


def test_real_repo_scan_is_clean_end_to_end():
    """Полный прогон гвардии по реальному дереву репозитория (тот же путь,
    каким её вызывает repo-ci.yml) — после фикса этой задачи обязан быть
    зелёным."""
    report = guard.build_report()
    assert report["violations"] == [], report["violations"]
