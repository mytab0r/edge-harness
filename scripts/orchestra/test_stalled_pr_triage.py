#!/usr/bin/env python3
"""Тесты триажа застрявшего PR (scripts/orchestra/stalled_pr_triage.py, #1218).

Два слоя: чистые функции (decide/functional_overlap/declaration_collisions) —
без git; измерение (measure_functional_overlap/is_conflicting/
measure_invariant_collision) — на настоящем временном git-репозитории
(AGENTS.md, «поведенческий тест находит то, чего структурный не видит»):
структурная проверка «функция называется touched_regions» красится ложно-
зелёной, если тело перепутает сторону diff'а (PR/main) или региональные
границы — только реальный `git diff`/`git merge-tree` на реальных коммитах
это ловит.

Запуск: python -m pytest scripts/orchestra/test_stalled_pr_triage.py -q
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "stalled_pr_triage.py"
spec = importlib.util.spec_from_file_location("stalled_pr_triage", SCRIPT)
tri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tri)  # type: ignore[union-attr]


# ── decide(): матрица дефекта 1 (дыра таблицы) и дефекта 2 (константы) ──────

def test_small_conflict_no_verdict_is_proceed():
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="open", ai_verdict=None)
    assert result["action"] == tri.ACTION_PROCEED


def test_small_conflict_with_rework_verdict_still_proceeds_not_silent():
    """Дефект 1: раньше эта комбинация не покрывалась ни одной строкой
    таблицы (58% реальной очереди, 2026-09-14). Вердикт на доработку не
    блокирует решение сводить — он становится следующим шагом."""
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="open", ai_verdict=tri.review_labels.AI_CHANGES)
    assert result["action"] == tri.ACTION_PROCEED_AND_ADDRESS
    assert any("changes-requested" in r for r in result["reasons"])


def test_small_conflict_with_ai_failed_also_proceeds_and_addresses():
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="open", ai_verdict=tri.review_labels.AI_FAILED)
    assert result["action"] == tri.ACTION_PROCEED_AND_ADDRESS


def test_large_conflict_no_verdict_is_recreate():
    result = tri.decide(conflicting=True, functional_overlap_count=0,
                         task_state="open", ai_verdict=None)
    assert result["action"] == tri.ACTION_RECREATE


def test_functional_overlap_alone_forces_recreate_even_when_git_reports_clean():
    """Величина 3 (дефект 3): git может считать PR MERGEABLE, но AST-регионы
    пересекаются с main — «дёшево по git» не значит «дёшево по смыслу»."""
    result = tri.decide(conflicting=False, functional_overlap_count=3,
                         task_state="open", ai_verdict=None)
    assert result["action"] == tri.ACTION_RECREATE
    assert any("функций/классов" in r for r in result["reasons"])


def test_large_conflict_with_rework_verdict_names_extra_cost_reason():
    result = tri.decide(conflicting=True, functional_overlap_count=5,
                         task_state="open", ai_verdict=tri.review_labels.AI_CHANGES)
    assert result["action"] == tri.ACTION_RECREATE
    assert any("дороже пересоздания" in r for r in result["reasons"])


def test_replacement_found_closes_regardless_of_everything_else():
    result = tri.decide(conflicting=True, functional_overlap_count=99,
                         task_state="open", ai_verdict=tri.review_labels.AI_CHANGES,
                         replacement_found=True)
    assert result["action"] == tri.ACTION_CLOSE


def test_closed_task_without_replacement_closes():
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="closed", ai_verdict=None, replacement_found=False)
    assert result["action"] == tri.ACTION_CLOSE


def test_closed_task_is_not_enough_alone_if_replacement_unknown_still_closes_per_adr():
    """ADR 0021: «задача закрыта И величина 5 не находит замены» — величина 5
    unknown (не проверяли) трактуется как «не нашли» здесь: закрытая задача
    без подтверждённой замены остаётся сигналом закрытия, не блокируется
    отсутствием прочтения (иначе величина 4 снова ничего не решает)."""
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="closed", ai_verdict=None, replacement_found=None)
    assert result["action"] == tri.ACTION_CLOSE


def test_open_task_with_no_replacement_info_does_not_close():
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="open", ai_verdict=None, replacement_found=None)
    assert result["action"] == tri.ACTION_PROCEED


def test_task_with_no_pool_task_at_all_is_not_treated_as_closed():
    """PR боты (dependabot) — task_state="none": отсутствие задачи не то же
    самое, что закрытая задача, закрывать по этому признаку нельзя."""
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="none", ai_verdict=None)
    assert result["action"] == tri.ACTION_PROCEED


def test_declaration_collision_is_a_warning_overlaid_on_any_action():
    """Дефект 4: коллизия номера — не отдельная ветка действия, а
    обязательное предупреждение поверх решения, каким бы оно ни было."""
    proceed = tri.decide(conflicting=False, functional_overlap_count=0,
                          task_state="open", ai_verdict=None, declaration_collision=True)
    assert proceed["action"] == tri.ACTION_PROCEED
    assert any("коллизию" in r or "коллизия" in r or "дубль" in r for r in proceed["reasons"])

    recreate = tri.decide(conflicting=True, functional_overlap_count=0,
                           task_state="open", ai_verdict=None, declaration_collision=True)
    assert recreate["action"] == tri.ACTION_RECREATE
    assert any("дубль" in r for r in recreate["reasons"])


# ── declared_invariant_numbers / declaration_collisions (чистые) ───────────

def test_declared_invariant_numbers_extracts_all_mentions():
    source = "# Инвариант 15: X\ndef f():\n    '''Инвариант 15 (#900)'''\n# Инвариант 9\n"
    assert tri.declared_invariant_numbers(source) == {15, 9}


def test_declaration_collisions_only_flags_numbers_added_by_both_sides():
    added_by_pr = {15, 20}
    added_by_main = {15, 17}
    assert tri.declaration_collisions(added_by_pr, added_by_main) == {15}


def test_declaration_collisions_empty_when_disjoint():
    assert tri.declaration_collisions({20}, {17}) == set()


# ── parse_added_line_numbers / python_def_ranges (чистые) ──────────────────

def test_parse_added_line_numbers_reads_plus_side_of_hunk_header():
    diff = "@@ -10,2 +12,4 @@\n+a\n+b\n+c\n+d\n@@ -30 +34,0 @@\n"
    assert tri.parse_added_line_numbers(diff) == {12, 13, 14, 15, 34}


def test_python_def_ranges_keys_by_name_and_start_line_to_avoid_collapsing_same_name():
    source = "class A:\n    def f(self):\n        pass\n\ndef f():\n    pass\n"
    ranges = tri.python_def_ranges(source)
    assert len(ranges) == 3  # class A, A.f, top-level f
    assert "f:2" in ranges and "f:5" in ranges


def test_python_def_ranges_on_syntax_error_returns_empty_not_raises():
    assert tri.python_def_ranges("def f(:\n") == {}


def test_functional_overlap_is_intersection_of_region_names():
    assert tri.functional_overlap({"a:1", "b:5"}, {"b:5", "c:9"}) == {"b:5"}


def test_line_drift_ratio_none_when_pr_touched_nothing_in_common_files():
    assert tri.line_drift_ratio(500, 0) is None


def test_line_drift_ratio_matches_division():
    assert tri.line_drift_ratio(100, 50) == 2.0


# ── Поведенческие тесты на реальном git-репозитории ─────────────────────────

def git(*args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                             encoding="utf-8", check=True)
    return result.stdout


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(cwd: Path):
    git("init", "--quiet", "-b", "main", cwd=cwd)
    git("config", "user.email", "test@example.com", cwd=cwd)
    git("config", "user.name", "test", cwd=cwd)


SCHEDULER_SOURCE = (
    "def dispatch_conflict_rework():\n"
    "    return 1\n"
    "\n\n"
    "def wip_gate():\n"
    "    return 2\n"
    "\n\n"
    "def unrelated_helper():\n"
    "    return 3\n"
)


@pytest.fixture
def repo(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    init_repo(cwd)
    write(cwd / "scripts" / "orchestra" / "scheduler.py", SCHEDULER_SOURCE)
    git("add", "-A", cwd=cwd)
    git("commit", "-m", "base", cwd=cwd)
    return cwd


def test_measure_functional_overlap_is_zero_for_disjoint_edits_like_pr542_and_pr1053(repo):
    """Живой случай issue #1218 (дефект 3): #542/#1053 — main дописывает в
    конец файла (растущий test_scheduler.py), PR правит другую функцию того
    же файла. Строковый ratio ADR инфлировался этим; функциональное
    пересечение обязано остаться нулём."""
    git("checkout", "-b", "pr", cwd=repo)
    source = SCHEDULER_SOURCE.replace(
        "def dispatch_conflict_rework():\n    return 1\n",
        "def dispatch_conflict_rework():\n    return 100  # PR meniaet\n")
    write(repo / "scripts" / "orchestra" / "scheduler.py", source)
    git("commit", "-am", "pr edits dispatch_conflict_rework", cwd=repo)

    git("checkout", "main", cwd=repo)
    grown = SCHEDULER_SOURCE + "\n\ndef unrelated_helper_2():\n    return 4\n" * 20
    write(repo / "scripts" / "orchestra" / "scheduler.py", grown)
    git("commit", "-am", "main appends unrelated helpers at end of file", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 0


def test_measure_functional_overlap_finds_real_semantic_collision_like_pr831(repo):
    """Обратный случай: main правит РОВНО ту функцию, которую PR тоже
    правит — величина 3 обязана увидеть это как пересечение, не ноль."""
    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py",
          SCHEDULER_SOURCE.replace("def wip_gate():\n    return 2\n",
                                    "def wip_gate():\n    return 200  # PR rewrite\n"))
    git("commit", "-am", "pr rewrites wip_gate", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py",
          SCHEDULER_SOURCE.replace("def wip_gate():\n    return 2\n",
                                    "def wip_gate():\n    return 2  # main also touches this\n"))
    git("commit", "-am", "main also touches wip_gate", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 1
    assert "wip_gate" in result["overlap_detail"]["scripts/orchestra/scheduler.py"][0]


def test_is_conflicting_true_on_real_textual_conflict(repo):
    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py", "def dispatch_conflict_rework():\n    return 'PR'\n")
    git("commit", "-am", "pr", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py", "def dispatch_conflict_rework():\n    return 'MAIN'\n")
    git("commit", "-am", "main", cwd=repo)

    conflicting, files = tri.is_conflicting("main", "pr", cwd=str(repo))
    assert conflicting is True
    assert "scripts/orchestra/scheduler.py" in files


# ── ensure_unshallow (issue #1213/#1218: ловушка поверхностного клона) ─────

def test_ensure_unshallow_grows_a_shallow_clone_to_full_history(tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "--quiet", "--bare", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    init_repo(seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    write(seed / "a.txt", "1\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "first", cwd=seed)
    write(seed / "a.txt", "2\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "second", cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--quiet", "--no-local", "--depth", "1", "--branch", "main",
        str(origin), str(clone), cwd=tmp_path)
    assert tri.is_shallow(cwd=str(clone)) is True

    tri.ensure_unshallow(cwd=str(clone))
    assert tri.is_shallow(cwd=str(clone)) is False
    # Полная история реально появилась, не только флаг снялся:
    log = git("log", "--oneline", cwd=clone)
    assert "first" in log and "second" in log


def test_ensure_unshallow_is_a_noop_on_a_full_repository(repo):
    assert tri.is_shallow(cwd=str(repo)) is False
    tri.ensure_unshallow(cwd=str(repo))  # не должно упасть на полном дереве
    assert tri.is_shallow(cwd=str(repo)) is False


def test_is_conflicting_false_on_clean_rebase(repo):
    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "README.md", "pr change\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "pr", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "OTHER.md", "main change\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "main", cwd=repo)

    conflicting, files = tri.is_conflicting("main", "pr", cwd=str(repo))
    assert conflicting is False
    assert files == []


def test_measure_invariant_collision_detects_live_class_from_870_883_944(repo):
    """Живой случай issue #1218 (дефект 4): main и PR независимо добавляют
    один и тот же номер `# Инвариант N` с общего merge-base (870→15/883→18/
    944→17 против main)."""
    write(repo / tri.INVARIANT_FILE, "# Инвариант 14\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base with invariant 14", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / tri.INVARIANT_FILE, "# Инвариант 14\n# Инвариант 15: PR adds drain quiet check\n")
    git("commit", "-am", "pr adds invariant 15", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / tri.INVARIANT_FILE, "# Инвариант 14\n# Инвариант 15: main adds push-trigger check\n")
    git("commit", "-am", "main independently adds invariant 15", cwd=repo)

    result = tri.measure_invariant_collision("main", "pr", cwd=str(repo))
    assert result["collisions"] == [15]


# ── decision_doc_collision_pr_numbers (величина 7, вторая половина) ────────

def test_decision_doc_collision_pr_numbers_extracts_only_pr_sources(monkeypatch):
    """Переиспользует decision_numbering целиком (#1078) — здесь проверяется
    только извлечение номера PR из имени источника `"PR #N"`, не сам обход
    git/gh (тот уже покрыт test_decision_numbering.py)."""
    def fake_check(repo, cwd=None):
        return tri.decision_numbering.check_result.violation([{
            "root": "docs/decisions", "number": "0017",
            "occurrences": [{"filename": "0017-x.md", "sources": ["main", "PR #944"]},
                             {"filename": "0017-y.md", "sources": ["PR #870"]}],
        }])
    monkeypatch.setattr(tri.decision_numbering, "check_decision_doc_number_collisions", fake_check)
    result = tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness")
    assert result == {944, 870}


def test_decision_doc_collision_pr_numbers_empty_when_no_violation(monkeypatch):
    monkeypatch.setattr(tri.decision_numbering, "check_decision_doc_number_collisions",
                         lambda repo, cwd=None: tri.decision_numbering.check_result.ok())
    assert tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness") == set()


def test_decision_doc_collision_pr_numbers_empty_on_unknown_not_raises(monkeypatch):
    """unknown() (сеть/git отказали внутри decision_numbering) — не падение
    и не ложное 'коллизия есть', честное 'не нашли' (см. докстринг)."""
    monkeypatch.setattr(tri.decision_numbering, "check_decision_doc_number_collisions",
                         lambda repo, cwd=None: tri.decision_numbering.check_result.unknown("сеть недоступна"))
    assert tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness") == set()


def test_measure_invariant_collision_empty_when_pr_only_inherits_mains_number(repo):
    write(repo / tri.INVARIANT_FILE, "# Инвариант 14\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "README.md", "pr touches something else\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "pr", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / tri.INVARIANT_FILE, "# Инвариант 14\n# Инвариант 15: main adds\n")
    git("commit", "-am", "main adds invariant 15 alone", cwd=repo)

    result = tri.measure_invariant_collision("main", "pr", cwd=str(repo))
    assert result["collisions"] == []
