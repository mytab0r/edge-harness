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
    assert len(ranges) == 3  # class A, A.f, top-level f
    assert "f:2" in ranges and "f:5" in ranges


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
