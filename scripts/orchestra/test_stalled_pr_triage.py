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
    пересекаются с main — «дёшево по git» не значит «дёшево по смыслу».
    Докстринг, decide() и reason-строка говорят одно (находка ревью PR
    #1219): пересечение → «пересоздать», что читать — часть исполнения."""
    result = tri.decide(conflicting=False, functional_overlap_count=3,
                         task_state="open", ai_verdict=None)
    assert result["action"] == tri.ACTION_RECREATE
    assert any("регион" in r and "пересоздать" in r for r in result["reasons"])


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


def test_unknown_task_state_is_honest_not_silently_open():
    """Находка ревью PR #1219: отсутствующий ответ GraphQL не имеет права
    притворяться «задача открыта» — «не знаю» остаётся «не знаю», решение
    на величине 4 не строится, в reasons попадает честное «не подтверждено»."""
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="unknown", ai_verdict=None)
    assert result["action"] == tri.ACTION_PROCEED
    assert any("не подтверждено" in r for r in result["reasons"])


def test_declaration_collision_is_a_warning_overlaid_on_any_action():
    """Дефект 4: коллизия номера — не отдельная ветка действия, а
    обязательное предупреждение поверх решения, каким бы оно ни было.
    Половины названы раздельно (находка ревью PR #1219: reason обязан
    описывать сработавшую половину, не гадать)."""
    proceed = tri.decide(conflicting=False, functional_overlap_count=0,
                          task_state="open", ai_verdict=None,
                          invariant_collision=True)
    assert proceed["action"] == tri.ACTION_PROCEED
    assert any("инварианты" in r and "дубль" in r for r in proceed["reasons"])

    recreate = tri.decide(conflicting=True, functional_overlap_count=0,
                           task_state="open", ai_verdict=None,
                           decision_doc_collision=True)
    assert recreate["action"] == tri.ACTION_RECREATE
    assert any("decision/research-номера" in r for r in recreate["reasons"])
    # Инвариантная формулировка в doc-случае не появляется — «алерт не гадает».
    assert not any("инварианты" in r for r in recreate["reasons"])


# ── decide(): величина 8 — PR-дубль уже слитой работы (issue #1236) ─────────
#
# MUTATION-PROOF
# ref: 1caf1f7053a2323bf0a05c9e4054c413b4a872b6
# paths: scripts/orchestra/stalled_pr_triage.py
# run: python -m pytest scripts/orchestra/test_stalled_pr_triage.py -k test_already_in_main_closes_regardless_of_everything_else -q --tb=no
# expect: 1 failed
#
# `ref` — main ДО этого PR (issue #1236): на нём `decide()` не принимает
# `already_in_main`, `measure_already_in_main`/`already_in_main_check` не
# существуют вовсе. Гвардия (scripts/lib/mutation_recipe_guard.py, #1194)
# подменяет ТОЛЬКО stalled_pr_triage.py историческим содержимым, тест ниже
# остаётся текущим (зовёт already_in_main=...) — TypeError на неизвестном
# kwarg даёт "1 failed", которого нет в baseline-прогоне текущего дерева
# (там тест зелёный, "failed" не встречается вовсе). `--tb=no` — не
# декорация: без него pytest печатает кириллический докстринг/исходник
# вокруг падения, а Windows-раннер этого рецепта (в отличие от Linux-CI)
# декодирует stdout ДОЧЕРНЕГО pytest в кодировке локали, а не UTF-8 —
# `--tb=no` убирает печать исходника, рецепт остаётся кроссплатформенным
# без `set PYTHONIOENCODING=...` (синтаксис cmd.exe, ломается на Linux-CI).

def test_already_in_main_closes_regardless_of_everything_else():
    """Величина 8 — машинно доказанный факт (git blob-SHA), а не чтение
    человеком (в отличие от replacement_found, величина 5) — проверяется
    РАНЬШЕ него и раньше предупреждений о коллизии номеров: нечему
    коллидировать, если сводить нечего."""
    result = tri.decide(conflicting=True, functional_overlap_count=99,
                         task_state="open", ai_verdict=tri.review_labels.AI_CHANGES,
                         invariant_collision=True, decision_doc_collision=True,
                         already_in_main=True)
    assert result["action"] == tri.ACTION_CLOSE
    assert any("величина 8" in r and "1020" in r for r in result["reasons"])
    # Предупреждения о коллизии номеров (величина 7) не появляются — нечему
    # коллидировать, если PR не меняет main вовсе.
    assert not any("величина 7" in r for r in result["reasons"])


def test_already_in_main_false_does_not_short_circuit():
    result = tri.decide(conflicting=False, functional_overlap_count=0,
                         task_state="open", ai_verdict=None, already_in_main=False)
    assert result["action"] == tri.ACTION_PROCEED
    assert not any("величина 8" in r for r in result["reasons"])


# ── declaration_collisions (чистые) ─────────────────────────────────────────

def test_declaration_collisions_only_flags_numbers_added_by_both_sides():
    added_by_pr = {15, 20}
    added_by_main = {15, 17}
    assert tri.declaration_collisions(added_by_pr, added_by_main) == {15}


def test_declaration_collisions_empty_when_disjoint():
    assert tri.declaration_collisions({20}, {17}) == set()


def test_invariant_registry_format_is_single_sourced_to_invariant_numbering():
    """Находка ревью PR #1219 (в связке с #1201, слитым 2026-09-14): формат
    записи реестра инвариантов парсится только invariant_numbering — путь и
    парсер не копируются в триаж."""
    assert tri.INVARIANT_FILE == tri.invariant_numbering.TARGET_PATH


# ── parse_base_touched_lines / python_def_ranges (чистые) ──────────────────

def test_parse_base_touched_lines_reads_minus_side_of_hunk_header():
    """БАЗОВЫЕ координаты (сторона минус), не плюс-стороны (находка ревью
    PR #1219): сверять можно только с регионами того же файла, что в
    заголовке минус-стороны."""
    diff = "@@ -10,2 +12,4 @@\n+a\n+b\n+c\n+d\n@@ -30 +34,0 @@\n"
    assert tri.parse_base_touched_lines(diff) == {10, 11, 30}


def test_parse_base_touched_lines_pure_insertion_touches_base_line():
    """`b == 0` — вставка после базовой строки a: регион замыкается
    строкой a (иначе вставка внутри функции никогда бы не пересеклась), а
    `-0,0` (вставка в начало файла) — строкой 1, не 0."""
    diff = "@@ -15,0 +16,3 @@\n+x\n+y\n+z\n@@ -0,0 +1,2 @@\n+new\n+file\n"
    assert tri.parse_base_touched_lines(diff) == {15, 1}


def test_python_def_ranges_keys_by_name_and_start_line_to_avoid_collapsing_same_name():
    source = "class A:\n    def f(self):\n        pass\n\ndef f():\n    pass\n"
    ranges = tri.python_def_ranges(source)
    # Метод A.f и верхнеуровневая f — два региона с одним именем; класс A
    # регионом не остаётся (содержит метод — см. тест ниже).
    assert "f:2" in ranges and "f:5" in ranges
    assert "A:1" not in ranges


def test_python_def_ranges_drops_region_strictly_containing_other_regions():
    """Блокирующая находка ревью PR #1219: регион класса покрывает всё тело,
    и правки ДВУХ РАЗНЫХ методов одного класса давали пересечение `{класс}`
    → ложное «пересоздать» (git сводит разные методы чисто). Минимальные
    регионы: класс с методами и внешняя функция с вложенной — не единицы
    пересечения; сигнал не теряется — вложенные регионы несут его сами
    (PR, заменяющий класс целиком, задевает строки методов)."""
    source = ("class Orchestrator:\n"
              "    def start(self):\n"
              "        return 1\n"
              "\n"
              "    def stop(self):\n"
              "        return 2\n")
    assert set(tri.python_def_ranges(source)) == {"start:2", "stop:5"}

    nested = ("def outer():\n"
              "    def inner():\n"
              "        return 1\n"
              "    return inner\n")
    assert set(tri.python_def_ranges(nested)) == {"inner:2"}


def test_python_def_ranges_keeps_class_without_tracked_children():
    """Обратная сторона минимизации: класс БЕЗ отслеживаемых детей
    (только атрибуты) — сам минимальный регион; правки его тела обеими
    сторонами обязаны оставаться измеримыми."""
    source = "class Config:\n    enabled = True\n    retries = 3\n"
    assert tri.python_def_ranges(source) == {"Config:1": (1, 3)}


def test_python_def_ranges_region_starts_at_decorator_not_at_def():
    """Находка ревью PR #1219: правка только `@декоратора` — правка
    поведения функции; регион, начатый со строки `def`, её бы не заметил."""
    source = "@staticmethod\n@deprecated\ndef f():\n    pass\n"
    ranges = tri.python_def_ranges(source)
    assert ranges == {"f:1": (1, 4)}


def test_python_def_ranges_on_syntax_error_returns_empty_not_raises():
    assert tri.python_def_ranges("def f(:\n") == {}


def test_region_word_agrees_with_russian_plural():
    assert tri.region_word(1) == "1 регион"
    assert tri.region_word(3) == "3 региона"
    assert tri.region_word(5) == "5 регионов"
    assert tri.region_word(11) == "11 регионов"
    assert tri.region_word(21) == "21 регион"
    assert tri.region_word(14) == "14 регионов"


def test_intersecting_line_runs_groups_contiguous_lines_into_ranges():
    """Line-range fallback: единица — РЕГИОН (диапазон), не строка, иначе
    величина раздувается пропорционально длине блока."""
    pr = {10, 11, 12, 13, 40}
    main = {11, 12, 13, 14, 40}
    assert tri.intersecting_line_runs(pr, main) == ["11-13", "40"]


def test_intersecting_line_runs_empty_when_disjoint():
    assert tri.intersecting_line_runs({1, 2}, {50}) == []


def test_count_changed_lines_counts_plus_and_minus_not_headers():
    diff = ("--- a/f.md\n+++ b/f.md\n@@ -1,2 +1,2 @@\n-old\n+new\n"
            "\\ No newline at end of file\n")
    assert tri.count_changed_lines(diff) == 2


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
    assert result["overlap_mode_by_file"]["scripts/orchestra/scheduler.py"] == "ast"


SHIFTED_SOURCE = (
    "def alpha():\n"       # 1-4: тело, которое PR удаляет целиком
    "    return 1\n"
    "    return 2\n"
    "    return 3\n"
    "def gamma():\n"       # 5-6: main правит здесь
    "    return 30\n"
    "def beta():\n"        # 7-8: PR правит здесь
    "    return 2\n"
)


def test_measure_functional_overlap_uses_base_coordinates_not_shifted_plus_side(repo):
    """Находка ревью PR #1219 (класс «сдвиги дают ложные пересечения»):
    PR удаляет тело alpha (−3 строки ВЫШЕ) и правит beta; в плюс-координатах
    правка beta получает номер 5 — внутри региона gamma, и правка main
    gamma ложно давала бы «пересечение» → ложное «пересоздать». Базовые
    (минус-)координаты обязаны дать ноль."""
    write(repo / "scripts" / "orchestra" / "scheduler.py", SHIFTED_SOURCE)
    git("commit", "-am", "base shifted layout", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py",
          SHIFTED_SOURCE.replace("def alpha():\n    return 1\n    return 2\n    return 3\n",
                                 "def alpha():\n    return 0\n")
          .replace("def beta():\n    return 2\n", "def beta():\n    return 200\n"))
    git("commit", "-am", "pr shrinks alpha and rewrites beta", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "scheduler.py",
          SHIFTED_SOURCE.replace("def gamma():\n    return 30\n", "def gamma():\n    return 31\n"))
    git("commit", "-am", "main rewrites gamma", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 0


def test_measure_functional_overlap_zero_for_two_different_methods_of_same_class(repo):
    """Блокирующая находка ревью PR #1219 (фантомные пересечения классов):
    PR правит первый метод класса, main — второй. Регион класса (покрывал
    всё тело) давал пересечение `{класс}` → ложное «пересоздать» с
    reason'ом «сведение сотрёт правку», хотя git сводит разные методы
    чисто; классовые файлы в очереди есть (scheduler.py, pulse_guard.py).
    Минимальные регионы обязаны дать ноль."""
    pulse_source = ("class Orchestrator:\n"
                    "    def start(self):\n"
                    "        return 1\n"
                    "\n"
                    "    def stop(self):\n"
                    "        return 2\n")
    write(repo / "scripts" / "orchestra" / "pulse_guard.py", pulse_source)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with Orchestrator class", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "pulse_guard.py",
          pulse_source.replace("        return 1\n", "        return 100  # PR\n"))
    git("commit", "-am", "pr edits start method", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "pulse_guard.py",
          pulse_source.replace("        return 2\n", "        return 200  # main\n"))
    git("commit", "-am", "main edits stop method", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 0
    assert result["overlap_detail"] == {}
    # А тот же файл с правкой ОДНОГО метода обеими сторонами по-прежнему
    # ловится (фикс не смазал сигнал реальной коллизии):
    git("checkout", "pr", cwd=repo)
    git("checkout", "-b", "pr2", cwd=repo)
    write(repo / "scripts" / "orchestra" / "pulse_guard.py",
          pulse_source.replace("        return 2\n", "        return 200  # PR2\n"))
    git("commit", "-am", "pr2 edits stop method like main", cwd=repo)
    result = tri.measure_functional_overlap("main", "pr2", cwd=str(repo))
    assert result["overlap_count"] == 1
    assert result["overlap_mode_by_file"]["scripts/orchestra/pulse_guard.py"] == "ast"
    assert "stop:5" in result["overlap_detail"]["scripts/orchestra/pulse_guard.py"]


def test_measure_functional_overlap_ast_mode_covers_module_level_lines(repo):
    """Некритичная находка ревью PR #1219: в режиме ast строки ВНЕ def/class
    (модульные константы, реестры уровня модуля) были невидимы — ноль «не
    считали» был неотличим от нуля «пересечений нет». Модульные строки
    считаются тем же line-range правилом поверх AST-регионов."""
    module_source = ("CHAIN = [\n"        # 1-4: модульный код, вне регионов
                     "    'a',\n"
                     "    'b',\n"
                     "]\n"
                     "\n\n"
                     "def helper():\n"    # 7-8: регион
                     "    return 1\n")
    write(repo / "scripts" / "orchestra" / "mod_levels.py", module_source)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with module const", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "mod_levels.py",
          module_source.replace("    'b',\n", "    'B-PR',\n"))
    git("commit", "-am", "pr edits module const", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "mod_levels.py",
          module_source.replace("    'b',\n", "    'B-MAIN',\n"))
    git("commit", "-am", "main edits same module const", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 1
    assert result["overlap_mode_by_file"]["scripts/orchestra/mod_levels.py"] == "ast+line-range"
    assert result["overlap_detail"]["scripts/orchestra/mod_levels.py"] == ["3"]


def test_measure_functional_overlap_module_line_versus_function_edit_is_zero(repo):
    """Обратный случай того же класса: PR правит модульную строку, main —
    тело функции; пересечения нет, и модульный учёт не выдумывает его."""
    module_source = ("CHAIN = [\n"
                     "    'a',\n"
                     "    'b',\n"
                     "]\n"
                     "\n\n"
                     "def helper():\n"
                     "    return 1\n")
    write(repo / "scripts" / "orchestra" / "mod_levels.py", module_source)
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "scripts" / "orchestra" / "mod_levels.py",
          module_source.replace("    'b',\n", "    'B-PR',\n"))
    git("commit", "-am", "pr edits module const", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "scripts" / "orchestra" / "mod_levels.py",
          module_source.replace("    return 1\n", "    return 2  # main\n"))
    git("commit", "-am", "main edits helper body", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 0
    assert "scripts/orchestra/mod_levels.py" not in result["overlap_mode_by_file"]


def test_measure_functional_overlap_line_range_fallback_for_non_python_files(repo):
    """Блокирующая находка ревью PR #1219 (п. 4 задачи #1218): fallback для
    не-.py — пересечение задетых обеими сторонами базовых строк. Живой
    класс: #1026 — конфликт в config json/markdown, где AST-версия величины
    3 честно рапортовала «код не пересекается», ничего не измерив."""
    write(repo / "docs" / "guide.md", "t1\nt2\nt3\nt4\nt5\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base guide", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "docs" / "guide.md", "t1\nP2\nP3\nt4\nt5\n")
    git("commit", "-am", "pr edits same lines 2-3", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "docs" / "guide.md", "t1\nM2\nM3\nt4\nt5\n")
    git("commit", "-am", "main edits lines 2-3", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 1
    assert result["overlap_mode_by_file"]["docs/guide.md"] == "line-range"
    assert result["overlap_detail"]["docs/guide.md"] == ["2-3"]


def test_measure_functional_overlap_line_range_fallback_disjoint_is_zero(repo):
    """Обратный случай fallback: обе стороны правят ОДИН не-.py файл, но
    РАЗНЫЕ его строки — пересечения нет, и ноль обязан остаться нулём."""
    write(repo / "docs" / "guide.md", "t1\nt2\nt3\nt4\nt5\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base guide", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "docs" / "guide.md", "P1\nt2\nt3\nt4\nt5\n")
    git("commit", "-am", "pr edits first line only", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "docs" / "guide.md", "t1\nt2\nt3\nt4\nM5\n")
    git("commit", "-am", "main edits last line", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["overlap_count"] == 0
    assert "docs/guide.md" not in result["overlap_mode_by_file"]


def test_measure_functional_overlap_carries_reference_line_ratio(repo):
    """Блокирующая находка ревью PR #1219: справочный строковый ratio ADR
    считается машиной и доходит до вывода, не остаётся формулой рядом с
    мёртвой функцией. PR: 1 строка изменена (−1/+1); main: +80 строк
    (дописывание в конец, живой класс #542/#1053) → ratio 40.0."""
    git("checkout", "-b", "pr", cwd=repo)
    source = SCHEDULER_SOURCE.replace(
        "def dispatch_conflict_rework():\n    return 1\n",
        "def dispatch_conflict_rework():\n    return 100\n")
    write(repo / "scripts" / "orchestra" / "scheduler.py", source)
    git("commit", "-am", "pr edits one line", cwd=repo)

    git("checkout", "main", cwd=repo)
    grown = SCHEDULER_SOURCE + "\n\ndef unrelated_helper_2():\n    return 4\n" * 20
    write(repo / "scripts" / "orchestra" / "scheduler.py", grown)
    git("commit", "-am", "main appends helpers", cwd=repo)

    result = tri.measure_functional_overlap("main", "pr", cwd=str(repo))
    assert result["line_drift_main_lines"] == 80
    assert result["line_drift_pr_lines"] == 2
    assert result["line_drift_ratio"] == 40.0
    # Справочная величина обязана ДОХОДИТЬ до вывода measure_pr (мутация:
    # ключ удаляется — тест красный), не жить только внутри measure.
    full = tri.measure_pr("main", "pr", cwd=str(repo))
    assert full["velichina3_line_drift_ratio_reference"] == 40.0


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


def test_is_conflicting_true_on_real_modify_delete_conflict(repo):
    """Блокирующая находка ревью PR #1219: modify/delete-конфликт даёт
    rc=1 и строку «CONFLICT (modify/delete): …», НЕ формы «Merge conflict
    in» — старый код, решавший по regex одной формы, возвращал
    `(False, [])`, то есть «не конфликтует», и PR ушёл бы в «довести»."""
    write(repo / "del_target.txt", "content\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base with del_target", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    git("rm", "--quiet", "del_target.txt", cwd=repo)
    git("commit", "-m", "pr deletes file", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "del_target.txt", "content changed in main\n")
    git("commit", "-am", "main modifies same file", cwd=repo)

    conflicting, files = tri.is_conflicting("main", "pr", cwd=str(repo))
    assert conflicting is True
    assert "del_target.txt" in files


def test_is_conflicting_parses_real_modify_delete_output_form():
    """Прод-форма вывода `git merge-tree --write-tree` (modify/delete):
    парсер обязан извлечь путь и из неё, не только из «Merge conflict in».
    Строка — реальный формат вывода git, не пересказ."""
    stdout = (
        "Auto-merging del_target.txt\n"
        "CONFLICT (modify/delete): del_target.txt deleted in pr and modified in main."
        "  Version main of del_target.txt left in tree.\n"
    )
    files = sorted(set(tri._CONFLICT_MERGE_IN_RE.findall(stdout))
                   | set(tri._CONFLICT_PATH_RE.findall(stdout)))
    assert "del_target.txt" in files


def test_is_conflicting_raises_giterror_when_tool_fails(repo):
    """Блокирующая находка ревью PR #1219: отказ merge-tree (несуществующий
    ref, старый git без --write-tree) — не «чисто», а громкий отказ.
    Сбой инструмента, притворившийся «конфликта нет», уводил бы PR в
    «довести» молча."""
    with pytest.raises(tri.GitError):
        tri.is_conflicting("main", "no-such-ref-anywhere", cwd=str(repo))


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


REGISTRY = (
    "# Реестр инвариантов\n"
    "\n"
    "  14. check_existing — единственная запись базы\n"
)


def test_measure_invariant_collision_detects_live_class_from_870_883_944(repo):
    """Живой случай issue #1218 (дефект 4): main и PR независимо добавляют
    один и тот же номер реестра repo_invariants.py с общего merge-base
    (870→15/883→18/944→17 против main). Формат записей — живой формат
    реестра (`N. check_имя — …`), который парсится invariant_numbering
    (#904), не пересказ."""
    write(repo / tri.INVARIANT_FILE, REGISTRY)
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base with invariant 14", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / tri.INVARIANT_FILE,
          REGISTRY + "  15. check_drain_quiet — PR adds drain quiet check\n")
    git("commit", "-am", "pr adds invariant 15", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / tri.INVARIANT_FILE,
          REGISTRY + "  15. check_push_trigger — main adds push-trigger check\n")
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
    numbers, unknown = tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness")
    assert (numbers, unknown) == ({944, 870}, None)


def test_decision_doc_collision_pr_numbers_empty_when_no_violation(monkeypatch):
    monkeypatch.setattr(tri.decision_numbering, "check_decision_doc_number_collisions",
                         lambda repo, cwd=None: tri.decision_numbering.check_result.ok())
    assert tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness") == (set(), None)


def test_decision_doc_collision_pr_numbers_carries_unknown_reason_not_silent_clean(monkeypatch):
    """unknown() (сеть/git отказали внутри decision_numbering) — не падение,
    но и не «коллизий нет»: причина возвращается ВТОРЫМ элементом, чтобы
    строка очереди различала «проверили, чисто» и «посмотреть не смогли»
    (находка ревью PR #1219: unknown схлопывался в «коллизий нет»)."""
    monkeypatch.setattr(tri.decision_numbering, "check_decision_doc_number_collisions",
                         lambda repo, cwd=None:
                         tri.decision_numbering.check_result.unknown("сеть недоступна"))
    numbers, unknown = tri.decision_doc_collision_pr_numbers("mytab0r/edge-harness")
    assert numbers == set()
    assert unknown == "сеть недоступна"


def test_measure_invariant_collision_empty_when_pr_only_inherits_mains_number(repo):
    write(repo / tri.INVARIANT_FILE, REGISTRY)
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "README.md", "pr touches something else\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "pr", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / tri.INVARIANT_FILE,
          REGISTRY + "  15. check_push_trigger — main adds\n")
    git("commit", "-am", "main adds invariant 15 alone", cwd=repo)

    result = tri.measure_invariant_collision("main", "pr", cwd=str(repo))
    assert result["collisions"] == []


def test_measure_invariant_collision_raises_when_registry_format_drifts(repo):
    """Слепота парсера не имеет права выглядеть как «номер не занят»
    (та же находка ai-review PR #1201 в invariant_numbering): непустой
    файл без распознанных записей реестра — GitError, не пустое множество.
    Файл в живом repo_invariants.py никогда не бывает легитимно пуст от
    записей реестра."""
    write(repo / tri.INVARIANT_FILE, REGISTRY)
    git("add", "-A", cwd=repo)
    git("commit", "-m", "base", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "README.md", "pr touches something else\n")
    git("add", "-A", cwd=repo)
    git("commit", "-m", "pr", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / tri.INVARIANT_FILE,
          "# дрейф формата: запись реестра не распознаётся\n- ~~инварианты~~\n")
    git("commit", "-am", "main rewrites registry in unknown format", cwd=repo)

    # Исключение бросает переиспользуемая machinery invariant_numbering.
    # Ловим RuntimeError (общий родитель GitError): importlib-загрузка по
    # файлу создаёт для каждого модуля СВОЙ объект класса GitError, поэтому
    # точный класс в raises не сослать — контракт «громкий отказ семейства
    # GitError», не тип-объект.
    with pytest.raises(RuntimeError, match="формат"):
        tri.measure_invariant_collision("main", "pr", cwd=str(repo))


# ── measure_already_in_main: величина 8, PR-дубль уже слитой работы ────────
# (issue #1236, живой случай PR #1020 — оба файла PR побайтно совпадали
# с main, а величина 3 (AST) этого не видела и врала «8 пересечений»,
# сравнивая PR с его же копией в main.)

def test_measure_already_in_main_violation_when_all_touched_files_byte_identical(repo):
    """Регрессия живого случая #1020: PR и main независимо пришли к
    одинаковым байтам одного файла — blob-SHA совпадает, величина 8 обязана
    дать violation() (already_in_main), не читая AST вовсе."""
    write(repo / "guard.py", "def check():\n    return 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with guard.py", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 42\n")
    git("commit", "-am", "pr rewrites guard.py", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 42\n")
    git("commit", "-am", "main independently arrives at the same bytes", cwd=repo)

    files = [{"filename": "guard.py", "status": "modified"}]
    result = tri.measure_already_in_main("main", "pr", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_VIOLATION
    assert result.violations == ["guard.py"]


def test_measure_already_in_main_ok_when_main_edited_same_function_differently(repo):
    """Обратный случай (requirement 6, issue #1236): main и PR правили ОДНУ
    функцию, но РАЗНО — файлы расходятся байт в байт, величина 8 обязана
    дать ok() (не дубль), не violation()."""
    write(repo / "guard.py", "def check():\n    return 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with guard.py", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 42\n")
    git("commit", "-am", "pr rewrites guard.py to 42", cwd=repo)

    git("checkout", "main", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 7\n")
    git("commit", "-am", "main rewrites guard.py to 7, differently", cwd=repo)

    files = [{"filename": "guard.py", "status": "modified"}]
    result = tri.measure_already_in_main("main", "pr", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_OK


def test_measure_already_in_main_ok_when_pr_removes_file_main_still_holds(repo):
    """requirement 3 (issue #1236): PR удаляет файл, который main ЕЩЁ несёт —
    настоящая правка, НЕ дубль."""
    write(repo / "old.txt", "content\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with old.txt", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    git("rm", "--quiet", "old.txt", cwd=repo)
    git("commit", "-m", "pr removes old.txt", cwd=repo)

    files = [{"filename": "old.txt", "status": "removed"}]
    result = tri.measure_already_in_main("main", "pr", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_OK


def test_measure_already_in_main_removed_file_already_gone_in_main_counts_as_duplicate(repo):
    """requirement 3, обратная сторона: PR удаляет файл, которого main УЖЕ
    не несёт (main удалил его независимо) — стороны согласны, само по себе
    это не блокирует already_in_main для остальных файлов PR."""
    write(repo / "old.txt", "content\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with old.txt", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    git("rm", "--quiet", "old.txt", cwd=repo)
    git("commit", "-m", "pr removes old.txt", cwd=repo)

    git("checkout", "main", cwd=repo)
    git("rm", "--quiet", "old.txt", cwd=repo)
    git("commit", "-m", "main independently removes old.txt too", cwd=repo)

    files = [{"filename": "old.txt", "status": "removed"}]
    result = tri.measure_already_in_main("main", "pr", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_VIOLATION
    assert result.violations == ["old.txt"]


def test_measure_already_in_main_unknown_when_blob_unreadable(repo):
    """Аномалия прод-формы (PR заявляет путь, которого нет на его же голове) —
    честное unknown(), не тихое «не дубль» и не «дубль»."""
    files = [{"filename": "no/such/path.py", "status": "modified"}]
    result = tri.measure_already_in_main("main", "main", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_UNKNOWN
    assert "no/such/path.py" in result.reason


def test_measure_already_in_main_empty_files_list_is_unknown(repo):
    result = tri.measure_already_in_main("main", "main", [], cwd=str(repo))
    assert result.status == tri.check_result.STATUS_UNKNOWN


def test_measure_already_in_main_renamed_file_compares_new_path(repo):
    """`renamed` сравнивается по НОВОМУ пути (`filename`) — тот же критерий,
    что added/modified."""
    write(repo / "old_name.py", "VALUE = 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base with old_name.py", cwd=repo)

    git("checkout", "-b", "pr", cwd=repo)
    git("mv", "old_name.py", "new_name.py", cwd=repo)
    git("commit", "-m", "pr renames file", cwd=repo)

    git("checkout", "main", cwd=repo)
    git("mv", "old_name.py", "new_name.py", cwd=repo)
    git("commit", "-m", "main independently renames the same way", cwd=repo)

    files = [{"filename": "new_name.py", "status": "renamed", "previous_filename": "old_name.py"}]
    result = tri.measure_already_in_main("main", "pr", files, cwd=str(repo))
    assert result.status == tri.check_result.STATUS_VIOLATION


# ── already_in_main_check: обёртка величины 8 (gh + git) ───────────────────

def test_already_in_main_check_delegates_to_list_pr_files_and_measures(repo, monkeypatch):
    write(repo / "guard.py", "def check():\n    return 1\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "base", cwd=repo)
    git("checkout", "-b", "pr", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 42\n")
    git("commit", "-am", "pr", cwd=repo)
    git("checkout", "main", cwd=repo)
    write(repo / "guard.py", "def check():\n    return 42\n")
    git("commit", "-am", "main matches independently", cwd=repo)

    calls = []

    def fake_list_pr_files(repo_name, number, gh_func):
        calls.append((repo_name, number))
        return [{"filename": "guard.py", "status": "modified"}]

    monkeypatch.setattr(tri.review_labels, "list_pr_files", fake_list_pr_files)
    result = tri.already_in_main_check("mytab0r/edge-harness", 1020, "main", "pr", cwd=str(repo))
    assert result.status == tri.check_result.STATUS_VIOLATION
    assert calls == [("mytab0r/edge-harness", 1020)]


def test_already_in_main_check_transport_failure_is_unknown_not_raise(repo, monkeypatch):
    """Сетевой отказ gh (requirement 2, issue #1236) — unknown(), обход
    очереди на одном PR не падает целым RuntimeError наружу."""
    def fail(repo_name, number, gh_func):
        raise RuntimeError("gh api упал: HTTP 502")

    monkeypatch.setattr(tri.review_labels, "list_pr_files", fail)
    result = tri.already_in_main_check("mytab0r/edge-harness", 501, "main", "main", cwd=str(repo))
    assert result.status == tri.check_result.STATUS_UNKNOWN
    assert "gh api pulls/501/files" in result.reason


# ── cmd_queue: один кривой PR не валит обход очереди (настоящий git) ───────

def test_cmd_queue_continues_past_one_broken_pr(tmp_path, monkeypatch):
    """Некритичная находка ревью PR #1219: отказ измерения ОДНОГО PR (здесь —
    ветка с историей, несводимой с main) не топит весь прогон: строка несёт
    `error` и НЕ несёт `action` («не посчитано» не выглядит вердиктом),
    остальные PR получают честные решения. Поведенчески: настоящий bare-
    репозиторий, настоящий клон, настоящие refs/pull/N/head."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "--quiet", "--bare", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    init_repo(seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    write(seed / "f.txt", "1\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "c0", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    git("checkout", "-qb", "pr501", cwd=seed)
    write(seed / "f.txt", "2\n")
    git("commit", "-am", "pr501", cwd=seed)
    git("push", "-q", "origin", "pr501:refs/pull/501/head", cwd=seed)

    git("checkout", "-q", "main", cwd=seed)
    git("checkout", "-q", "--orphan", "pr502", cwd=seed)
    git("commit", "--allow-empty", "-qm", "orphan root, no common ancestor", cwd=seed)
    git("push", "-q", "origin", "pr502:refs/pull/502/head", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--quiet", "--no-local", str(origin), str(clone), cwd=tmp_path)

    pulls = [
        {"number": 502, "title": "broken", "head": {"ref": "pr502"}, "labels": []},
        {"number": 501, "title": "good", "head": {"ref": "pr501"}, "labels": []},
    ]
    monkeypatch.setattr(tri, "open_pulls", lambda repo: pulls)
    monkeypatch.setattr(tri, "decision_doc_collision_pr_numbers",
                        lambda repo, cwd=None: (set(), None))
    # Величина 8 (issue #1236) теперь звонит gh ДО измерения каждого PR —
    # без мока это был бы настоящий сетевой gh api на реальном репозитории
    # (класс изоляции теста, не относится к дефекту #1219 самой строки):
    # pr501 несёт реальное расхождение с main (f.txt "2\n" против "1\n") —
    # величина 8 честно даёт ok(), сценарий не меняется; pr502 (orphan,
    # несводимая история) получает пустой список — величина 8 уходит в
    # unknown() ДО git-вызовов, сам «кривой PR» сценарий (несводимая история)
    # по-прежнему проверяется дальше, в measure_pr.
    def fake_list_pr_files(repo, number, gh_func):
        if number == 501:
            return [{"filename": "f.txt", "status": "modified"}]
        return []
    monkeypatch.setattr(tri.review_labels, "list_pr_files", fake_list_pr_files)
    rows = tri.cmd_queue("mytab0r/edge-harness", cwd=str(clone))

    by_number = {row["number"]: row for row in rows}
    assert set(by_number) == {501, 502}
    assert "error" in by_number[502]
    assert "action" not in by_number[502]
    good = by_number[501]
    assert good["velichina1_conflicting"] is False
    assert good["action"] == tri.ACTION_PROCEED
    assert good["velichina7_decision_doc_unknown"] is None
    assert good["velichina8_already_in_main"] is False


# ── cmd_queue: величина 8 short-circuit — дубль не считает величину 3 ──────

def test_cmd_queue_closes_duplicate_pr_without_computing_functional_overlap(tmp_path, monkeypatch):
    """Живой случай #1020: PR, чьи тронутые файлы побайтно совпадают с main,
    обязан получить ACTION_CLOSE через величину 8, а величина 3 (AST-
    пересечение) не должна вычисляться вовсе (requirement 1 issue #1236 —
    «вычисляется ДО дорогих величин, обесценивает их»). Второй PR очереди —
    подлинная правка, доказывает, что short-circuit не сломал обычный путь."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "--quiet", "--bare", cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    init_repo(seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    write(seed / "f.txt", "1\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "c0", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # pr601 — дубль: правит f.txt на "2\n", ровно то же, что main получит
    # независимо (ветка от c0, main продвигается позже отдельным коммитом).
    git("checkout", "-qb", "pr601", cwd=seed)
    write(seed / "f.txt", "2\n")
    git("commit", "-am", "pr601 rewrites f.txt", cwd=seed)
    git("push", "-q", "origin", "pr601:refs/pull/601/head", cwd=seed)

    git("checkout", "-q", "main", cwd=seed)
    write(seed / "f.txt", "2\n")
    git("commit", "-am", "main independently arrives at the same bytes", cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)

    # pr602 — подлинная правка: новый файл, которого main не несёт.
    git("checkout", "-qb", "pr602", "main", cwd=seed)
    write(seed / "g.txt", "genuine new content\n")
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "pr602 adds g.txt", cwd=seed)
    git("push", "-q", "origin", "pr602:refs/pull/602/head", cwd=seed)

    clone = tmp_path / "clone"
    git("clone", "--quiet", "--no-local", str(origin), str(clone), cwd=tmp_path)

    pulls = [
        {"number": 601, "title": "duplicate", "head": {"ref": "pr601"}, "labels": []},
        {"number": 602, "title": "genuine", "head": {"ref": "pr602"}, "labels": []},
    ]
    monkeypatch.setattr(tri, "open_pulls", lambda repo: pulls)
    monkeypatch.setattr(tri, "decision_doc_collision_pr_numbers",
                        lambda repo, cwd=None: (set(), None))

    def fake_list_pr_files(repo, number, gh_func):
        if number == 601:
            return [{"filename": "f.txt", "status": "modified"}]
        return [{"filename": "g.txt", "status": "added"}]
    monkeypatch.setattr(tri.review_labels, "list_pr_files", fake_list_pr_files)

    rows = tri.cmd_queue("mytab0r/edge-harness", cwd=str(clone))
    by_number = {row["number"]: row for row in rows}

    dup = by_number[601]
    assert dup["velichina8_already_in_main"] is True
    assert dup["velichina8_already_in_main_files"] == ["f.txt"]
    assert dup["action"] == tri.ACTION_CLOSE
    assert any("величина 8" in r for r in dup["reasons"])
    # Дорогая величина 3 не вычислялась вовсе для дубля (мутация: сними
    # short-circuit в cmd_queue — этот ключ появится, тест покраснеет).
    assert "velichina3_functional_overlap" not in dup

    genuine = by_number[602]
    assert genuine["velichina8_already_in_main"] is False
    assert genuine["action"] == tri.ACTION_PROCEED
    assert "velichina3_functional_overlap" in genuine
