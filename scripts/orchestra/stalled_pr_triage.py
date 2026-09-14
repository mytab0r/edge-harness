#!/usr/bin/env python3
"""Триаж застрявшего PR (ADR 0021/0024) — расчёт величин, не ручной подсчёт.

Контекст (issue #1218): ADR 0021 задаёт шесть измеримых величин для решения
«довести / пересоздать / закрыть», но был написан на выборке из 19 PR, все в
конфликте с main. Прогон метода на реальной очереди 2026-09-14 (38 открытых
PR) руками вскрыл четыре дефекта:

  1. Таблица молчала по PR с малым конфликтом, но висящим `ai:changes-
     requested` — доминирующий случай очереди (35 из 38 несут этот вердикт).
     Правило здесь (`decide`): вердикт на доработку меняет НЕ решение
     сводить/нет, а следующий шаг ПОСЛЕ сведения («довести и доработать
     находки», не «висеть без решения»). Пересоздание остаётся отдельным
     исходом там, где сведение и так дорогое (величины 1–3 велики).
  2. Величина 4 (задача открыта?) на срезе 2026-09-14 — 36 из 36 открыта;
     величина 6 (вердикт) — 35 из 38 changes-requested. Обе оставлены в
     методе (структурно способны различать — закрытая задача возможна, её
     просто не было на этом срезе), но `decide` не строит на них жёсткий
     порог там, где они не различают.
  3. Величина 3 ADR (строк дрейфа main в файлах PR / строк PR) — считает по
     СТРОКАМ, инфлируется несвязанным дописыванием в конец файла (живой
     случай — `test_scheduler.py`). Здесь она заменена на пересечение
     изменённых Python-функций/классов (AST-регионы по merge-base) между
     PR-диффом и main-диффом в общих файлах — `functional_overlap`. Для
     не-Python файлов региона нет (AST не универсален) — оверлап по ним не
     считается, это честно помечено, не подделывается нулём.
  4. Метода не было вовсе: PR добавляет номер `# Инвариант N`
     (`scripts/orchestra/repo_invariants.py`), который main НЕЗАВИСИМО
     ТОЖЕ добавил с общего merge-base — коллизия, найденная после сведения,
     тихо портит реестр. `declaration_collisions` здесь закрывает этот
     класс для repo_invariants.py (узкий, читает только этот файл, не
     трогает сам файл и не дублирует #904/PR #1201 — тот чинит ДРУГОЕ:
     назначение свободного номера внутри файла, не коллизию между PR и
     main). `docs/decisions`/`docs/research` тем же классом уже закрыты
     `scripts/lib/decision_numbering.py` (#1078) — этот модуль их не
     переизобретает, `cmd_queue` ниже переиспользует
     `check_decision_doc_number_collisions` напрямую.

Величина 5 («есть ли уже эквивалент в main») сюда НЕ включена намеренно —
ADR прав: это чтение и суждение, не число (см. его раздел «Что машине
отдать нельзя»). `decide()` принимает её как необязательный, явно
опциональный вход (`replacement_found: bool | None`), не пытается
вычислить сама.

Импорт соседних модулей — importlib по файлу (паттерн claim_task/
review_labels/upstream_drift: скрипты этого репозитория запускаются как
файлы, не как пакет).

CLI:
  python stalled_pr_triage.py measure <PR>       — величины 1–3,7 для
                                                    одного PR (git, без gh).
  python stalled_pr_triage.py queue               — величины 1,2,3,4,6,7 +
                                                    решение для ВСЕХ открытых
                                                    PR репозитория (gh + git).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import json
import os
import re
import subprocess
import sys

_TR_SPEC = importlib.util.spec_from_file_location(
    "task_ref", Path(__file__).resolve().parents[1] / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_TR_SPEC)
_TR_SPEC.loader.exec_module(task_ref)  # type: ignore[union-attr]

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

_DN_SPEC = importlib.util.spec_from_file_location(
    "decision_numbering", Path(__file__).resolve().parents[1] / "lib" / "decision_numbering.py")
decision_numbering = importlib.util.module_from_spec(_DN_SPEC)
_DN_SPEC.loader.exec_module(decision_numbering)  # type: ignore[union-attr]


# ── Носитель величины 7 для repo_invariants.py (узкий, см. шапку модуля) ────

INVARIANT_FILE = "scripts/orchestra/repo_invariants.py"
_INVARIANT_NUMBER_RE = re.compile(r"Инвариант (\d+)")


def declared_invariant_numbers(source: str) -> set[int]:
    """Номера `# Инвариант N`, упомянутые в исходнике. Не различает
    «объявление» от «упоминание в прозе комментария» — тем же способом,
    каким `decision_numbering` не различает файл от ссылки на файл: цена
    ложного совпадения (лишний, безвредный элемент множества) на порядок
    дешевле цены пропуска настоящей коллизии."""
    if not source:
        return set()
    return {int(n) for n in _INVARIANT_NUMBER_RE.findall(source)}


def declaration_collisions(added_by_pr: set[int], added_by_main: set[int]) -> set[int]:
    """Номера, которые PR и main НЕЗАВИСИМО добавили с общего merge-base —
    коллизия, не подтягивание уже существующего числа. Пересечение множеств
    «добавлено ИМЕННО этой стороной», не полных множеств текущих номеров:
    PR, который просто НАСЛЕДУЕТ уже существующий у main номер (не трогал
    его файл), не должен засчитываться коллизией — тот же принцип, что
    `decision_numbering.added_files_under_root` (не «несёт», а «добавил»)."""
    return added_by_pr & added_by_main


# ── Величина 3: функциональное пересечение (AST-регионы) ────────────────────

def python_def_ranges(source: str) -> dict[str, tuple[int, int]]:
    """{"имя:строка_начала": (start, end)} для def/class верхнего и вложенного
    уровня. Ключ несёт номер строки объявления — два метода с одинаковым
    именем в разных классах (`__init__` дюжину раз) не должны схлопываться в
    один регион. Синтаксическая ошибка (PR мог оставить файл битым на
    промежуточном коммите) — пустой словарь, не исключение: величина 3
    просто не считается для этого файла, это не диагностируется отдельно."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    ranges: dict[str, tuple[int, int]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            ranges[f"{node.name}:{node.lineno}"] = (node.lineno, end)
    return ranges


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_added_line_numbers(unified_diff: str) -> set[int]:
    """Номера строк, задетых на стороне `+` унифицированного диффа
    (`git diff --unified=0`), из hunk-заголовков `@@ -a,b +c,d @@`. `d`
    отсутствует ИЛИ равен 0 — чистое удаление, регион всё равно считается
    задетым ЛИНИЕЙ ПОСЛЕ точки удаления (c), иначе строка "функция целиком
    вырезана" никогда бы не пересеклась ни с чем."""
    lines: set[int] = set()
    for line in unified_diff.splitlines():
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            lines.add(max(start, 1))
        else:
            lines.update(range(start, start + count))
    return lines


def touched_regions(ranges: dict[str, tuple[int, int]], changed_lines: set[int]) -> set[str]:
    return {
        name for name, (start, end) in ranges.items()
        if any(start <= line <= end for line in changed_lines)
    }


def functional_overlap(pr_regions: set[str], main_regions: set[str]) -> set[str]:
    """Пересечение имён регионов (def/class), которые задели ОБЕ стороны
    (PR и main) относительно общего merge-base — величина 3 вместо строкового
    ratio ADR (см. шапку модуля). Непустое пересечение — сигнал «читать»
    (ADR прав: смысловое столкновение решает человек), не автоматический
    вердикт «пересоздать»."""
    return pr_regions & main_regions


def line_drift_ratio(main_drift_lines: int, pr_own_lines: int) -> float | None:
    """Легаси-формула ADR 0021 (строки дрейфа main / строки PR в тех же
    файлах) — оставлена СПРАВОЧНОЙ величиной (issue #1218, дефект 3: она
    инфлируется несвязанными изменениями и не входит в decide()).
    `None` — PR не менял строк в общих файлах, делить не на что."""
    if pr_own_lines <= 0:
        return None
    return main_drift_lines / pr_own_lines


# ── decide(): шесть+одна величина → действие (issue #1218, дефект 1) ────────

ACTION_PROCEED = "довести"
ACTION_PROCEED_AND_ADDRESS = "довести и доработать находки"
ACTION_RECREATE = "пересоздать"
ACTION_CLOSE = "закрыть"

AI_REWORK_VERDICTS = (review_labels.AI_CHANGES, review_labels.AI_FAILED)


def decide(
    *,
    conflicting: bool,
    functional_overlap_count: int,
    task_state: str,
    ai_verdict: str | None,
    replacement_found: bool | None = None,
    declaration_collision: bool = False,
) -> dict:
    """Единая точка решения — читает ВСЕ входы вместе (ADR 0021, «шесть
    величин вместе», не порог на одной). `task_state` — "open"/"closed"/
    "none" (PR без задачи пула, боты). `ai_verdict` — одна из
    `review_labels.AI_VERDICTS` либо `None` (вердикта ещё нет).

    Порядок проверок — от самого дешёвого решения (замена/закрытая задача)
    к самому дорогому суждению (велики ли 1–3):

      1. `replacement_found=True` — работа уже сделана в main, закрыть
         независимо от остального (ADR: величина 5 перекрывает всё).
      2. Задача закрыта И замены нет — автор потерял интерес, закрыть.
      3. Иначе решение по 1–3:
         - «малы» (не конфликтует И функциональных пересечений нет) →
           довести; если висит вердикт на доработку — тем же действием, но
           доработка находок называется явным следующим шагом (дефект 1:
           вердикт больше не блокирует само решение сводить).
         - «велики» (конфликтует ИЛИ есть функциональное пересечение) →
           пересоздать; вердикт на доработку в этом случае — довод В ПОЛЬЗУ
           пересоздания (цена «свести + доработать + повторное ревью» выше),
           называется отдельной причиной, не меняет действие.

    Коллизия объявления (величина 7) — не самостоятельная ветка действия
    (сама по себе не решает довести/пересоздать/закрыть), а ОБЯЗАТЕЛЬНОЕ
    предупреждение поверх любого исхода: сведение, которое пройдёт молча,
    после мержа даст дубль номера — читатель обязан увидеть это независимо
    от того, что решили величины 1–6."""
    reasons: list[str] = []
    if declaration_collision:
        reasons.append(
            "величина 7: PR добавляет номер объявления (# Инвариант N), который main "
            "тоже независимо добавил с общего merge-base — сведение молча даст дубль, "
            "снять коллизию ПЕРЕД доведением/пересозданием")

    if replacement_found is True:
        reasons.append("величина 5: эквивалент уже в main (дублирование подтверждено чтением)")
        return {"action": ACTION_CLOSE, "reasons": reasons}

    if task_state == "closed" and replacement_found is not True:
        reasons.append("величина 4: задача закрыта, величина 5 замены не находит")
        return {"action": ACTION_CLOSE, "reasons": reasons}

    small = (not conflicting) and functional_overlap_count == 0
    rework_pending = ai_verdict in AI_REWORK_VERDICTS

    if small:
        reasons.append("величины 1–3 малы: не конфликтует, функциональных пересечений с main нет")
        if rework_pending:
            reasons.append(
                f"величина 6: висит {ai_verdict} — не блокирует сведение, "
                "это следующий шаг после него")
            return {"action": ACTION_PROCEED_AND_ADDRESS, "reasons": reasons}
        return {"action": ACTION_PROCEED, "reasons": reasons}

    if conflicting:
        reasons.append("величина 1: PR конфликтует с main")
    if functional_overlap_count > 0:
        reasons.append(
            f"величина 3: {functional_overlap_count} функций/классов задеты и PR, и main "
            "с общего merge-base — смысловое пересечение, прочитать перед решением")
    if rework_pending:
        reasons.append(
            f"величина 6: висит {ai_verdict} — сведение + доработка + повторное ревью "
            "дороже пересоздания")
    return {"action": ACTION_RECREATE, "reasons": reasons}


# ── IO: git (реальный, без сети) ─────────────────────────────────────────────

class GitError(RuntimeError):
    pass


def run_git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} упал: {result.stderr.strip()}")
    return result.stdout


def merge_base(base_ref: str, head_ref: str, cwd: str | None = None) -> str:
    return run_git("merge-base", base_ref, head_ref, cwd=cwd).strip()


def is_shallow(cwd: str | None = None) -> bool:
    return run_git("rev-parse", "--is-shallow-repository", cwd=cwd).strip() == "true"


def ensure_unshallow(cwd: str | None = None) -> None:
    """Ловушка среды (issue #1213/#1218): общий `.git` может быть
    поверхностным клоном (CI checkout БЕЗ `fetch-depth: 0`) — `merge-base`
    между произвольными PR-ветками и main требует полной истории, иначе
    падает пустым stderr (нет общего предка в усечённом графе). Вызывается
    ДО измерений (защита от исходно мелкого дерева) И ПОСЛЕ обращения к
    `decision_numbering` (та сама мелит дерево своим `--depth 1` — см.
    докстринг `cmd_queue`), чтобы не оставить рабочее дерево мельче, чем
    застали. Идемпотентна: полное дерево — no-op."""
    if is_shallow(cwd=cwd):
        run_git("fetch", "--unshallow", "origin", cwd=cwd)


def commits_behind(base_sha: str, main_ref: str, cwd: str | None = None) -> int:
    out = run_git("rev-list", "--count", f"{base_sha}..{main_ref}", cwd=cwd)
    return int(out.strip() or "0")


def show_file(ref: str, path: str, cwd: str | None = None) -> str | None:
    """Содержимое `path` на `ref`; `None` — файла там нет (не ошибка: новый
    файл PR, файл удалён на другой стороне)."""
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout


def changed_python_files(base_sha: str, ref: str, cwd: str | None = None) -> set[str]:
    out = run_git("diff", "--name-only", base_sha, ref, "--", "*.py", cwd=cwd)
    return {line for line in out.splitlines() if line.strip()}


def is_conflicting(main_ref: str, head_ref: str, cwd: str | None = None) -> tuple[bool, list[str]]:
    """`(конфликтует?, [конфликтующие файлы])` — реальный `git merge-tree
    --write-tree`, не GitHub `mergeable` (тот считается GitHub асинхронно и
    может быть `null`/`unknown` — см. review_labels.CONFLICT_CLEAR_STATES;
    здесь считаем сами, синхронно, на живом дереве)."""
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", main_ref, head_ref], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    files = re.findall(r"^CONFLICT \([^)]*\): Merge conflict in (.+)$", result.stdout, re.MULTILINE)
    return bool(files), files


def measure_functional_overlap(main_ref: str, head_ref: str, cwd: str | None = None) -> dict:
    """Величина 3 (см. шапку модуля) для одного PR: пересечение изменённых
    Python def/class-регионов между PR и main относительно общего
    merge-base, только в файлах, которые трогают ОБЕ стороны."""
    base_sha = merge_base(main_ref, head_ref, cwd=cwd)
    pr_files = changed_python_files(base_sha, head_ref, cwd=cwd)
    main_files = changed_python_files(base_sha, main_ref, cwd=cwd)
    common = sorted(pr_files & main_files)
    overlap_by_file: dict[str, list[str]] = {}
    total = 0
    for path in common:
        base_source = show_file(base_sha, path, cwd=cwd)
        if base_source is None:
            continue
        ranges = python_def_ranges(base_source)
        pr_diff = run_git("diff", "--unified=0", base_sha, head_ref, "--", path, cwd=cwd)
        main_diff = run_git("diff", "--unified=0", base_sha, main_ref, "--", path, cwd=cwd)
        pr_regions = touched_regions(ranges, parse_added_line_numbers(pr_diff))
        main_regions = touched_regions(ranges, parse_added_line_numbers(main_diff))
        overlap = functional_overlap(pr_regions, main_regions)
        if overlap:
            overlap_by_file[path] = sorted(overlap)
            total += len(overlap)
    return {"merge_base": base_sha, "common_python_files": common,
            "overlap_count": total, "overlap_detail": overlap_by_file}


def measure_invariant_collision(main_ref: str, head_ref: str, cwd: str | None = None) -> dict:
    base_sha = merge_base(main_ref, head_ref, cwd=cwd)
    base_source = show_file(base_sha, INVARIANT_FILE, cwd=cwd) or ""
    pr_source = show_file(head_ref, INVARIANT_FILE, cwd=cwd) or ""
    main_source = show_file(main_ref, INVARIANT_FILE, cwd=cwd) or ""
    added_by_pr = declared_invariant_numbers(pr_source) - declared_invariant_numbers(base_source)
    added_by_main = declared_invariant_numbers(main_source) - declared_invariant_numbers(base_source)
    collisions = declaration_collisions(added_by_pr, added_by_main)
    return {"added_by_pr": sorted(added_by_pr), "added_by_main": sorted(added_by_main),
            "collisions": sorted(collisions)}


def measure_pr(main_ref: str, head_ref: str, cwd: str | None = None) -> dict:
    """Все git-считаемые величины (1,2,3,7) для одного PR — без gh, без
    задачи/вердикта (те приходят из GitHub API, см. `cmd_queue`)."""
    conflicting, conflict_files = is_conflicting(main_ref, head_ref, cwd=cwd)
    base_sha = merge_base(main_ref, head_ref, cwd=cwd)
    overlap = measure_functional_overlap(main_ref, head_ref, cwd=cwd)
    invariant = measure_invariant_collision(main_ref, head_ref, cwd=cwd)
    return {
        "velichina1_conflicting": conflicting,
        "velichina1_conflict_files": conflict_files,
        "velichina2_commits_behind": commits_behind(base_sha, main_ref, cwd=cwd),
        "velichina3_functional_overlap": overlap["overlap_count"],
        "velichina3_detail": overlap["overlap_detail"],
        "velichina7_invariant_collisions": invariant["collisions"],
    }


# ── IO: gh (метаданные) ──────────────────────────────────────────────────────

def gh(*args: str):
    result = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} упал: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def open_pulls(repo: str) -> list[dict]:
    return review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)


def task_states(repo: str, numbers: list[int]) -> dict[int, str]:
    """{номер: "open"|"closed"} через один GraphQL-батч (экономия gh —
    AGENTS.md, «экономь gh»: без этого — по одному REST-запросу на задачу)."""
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    parts = [f"i{n}: issue(number: {n}) {{ number state }}" for n in numbers]
    query = (f'query {{ repository(owner: "{owner}", name: "{name}") '
             f'{{ {" ".join(parts)} }} }}')
    result = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api graphql упал: {result.stderr.strip()}")
    data = json.loads(result.stdout)["data"]["repository"]
    return {n: data[f"i{n}"]["state"].lower() for n in numbers if data.get(f"i{n}")}


def ai_verdict_of(labels) -> str | None:
    names = {label["name"] for label in labels}
    for verdict in review_labels.AI_VERDICTS:
        if verdict in names:
            return verdict
    return None


_PR_SOURCE_RE = re.compile(r"^PR #(\d+)$")


def decision_doc_collision_pr_numbers(repo: str, cwd: str | None = None) -> set[int]:
    """Номера PR, ЗАМЕШАННЫХ в коллизию `docs/decisions`/`docs/research`
    (величина 7, вторая половина — см. шапку модуля): переиспользует
    `decision_numbering.check_decision_doc_number_collisions` целиком, не
    второй копией того же обхода git+gh (#1078 уже закрыл этот класс для
    этого носителя). `unknown()` (сеть/git отказали внутри) — пустое
    множество, не падение: `cmd_queue` не должен топить весь прогон из-за
    того, что эта ДОПОЛНИТЕЛЬНАЯ проверка не досчиталась, но и не должен
    выдавать её результат за «коллизий нет» где-то ещё — здесь честно
    возвращается «не нашли ни одной» (тот же исход, что ok())."""
    result = decision_numbering.check_decision_doc_number_collisions(repo, cwd=cwd)
    if result.status != "violation":
        return set()
    numbers: set[int] = set()
    for found in result.violations:
        for occ in found["occurrences"]:
            for source in occ["sources"]:
                match = _PR_SOURCE_RE.match(source)
                if match:
                    numbers.add(int(match.group(1)))
    return numbers


def cmd_queue(repo: str, cwd: str | None = None) -> list[dict]:
    """Величины 1,2,3,4,6,7 + решение для всех открытых PR репозитория.
    Одна страница `pulls` + один GraphQL-батч задач (gh) + один прогон
    decision_numbering (свой отдельный обход, см. его докстринг), дальше —
    только локальный git (без сетевой цены) на КАЖДЫЙ PR.

    ГРАБЛЯ (issue #1218, тот же класс, что #1213): `decision_numbering.
    fetch_refs` делает `git fetch --depth 1` СВОЕЙ веткой `main` в отдельный
    неймспейс — даже на уже полном (unshallow) дереве это переводит .git/
    shallow в частично-мелкое состояние (shallow — свойство КОММИТА, не
    ссылки: если main и `refs/decision-numbering/main` указывают на один и
    тот же коммит, фетч с `--depth 1` мелит его для ВСЕХ ссылок разом,
    включая уже читаемый `origin/main`). Живой симптом: последующий
    `git merge-base origin/main ...` падает пустым stderr сразу после
    вызова `decision_doc_collision_pr_numbers`. Лечится порядком: этот
    вызов — ПОСЛЕДНИМ, после того как весь git-обход измерений уже
    завершился, а не до него."""
    ensure_unshallow(cwd=cwd)
    pulls = open_pulls(repo)
    task_numbers = sorted({n for n in (task_ref.resolve_pr_task(p) for p in pulls) if n is not None})
    states = task_states(repo, task_numbers)

    partial = []
    for pull in pulls:
        number = pull["number"]
        head_ref = f"origin/pr-{number}"
        run_git("fetch", "--quiet", "origin", f"refs/pull/{number}/head:refs/remotes/origin/pr-{number}", cwd=cwd)
        measured = measure_pr("origin/main", head_ref, cwd=cwd)
        task_number = task_ref.resolve_pr_task(pull)
        task_state = states.get(task_number, "open") if task_number is not None else "none"
        verdict = ai_verdict_of(pull.get("labels", []))
        partial.append({
            "number": number, "title": pull.get("title", ""), "task": task_number,
            "task_state": task_state, "ai_verdict": verdict, **measured,
        })

    # Последним — иначе реселлит .git/shallow для всех ссылок, см. докстринг выше.
    doc_collisions = decision_doc_collision_pr_numbers(repo, cwd=cwd)
    ensure_unshallow(cwd=cwd)

    rows = []
    for row in partial:
        declaration_collision = (bool(row["velichina7_invariant_collisions"])
                                  or row["number"] in doc_collisions)
        decision = decide(
            conflicting=row["velichina1_conflicting"],
            functional_overlap_count=row["velichina3_functional_overlap"],
            task_state=row["task_state"],
            ai_verdict=row["ai_verdict"],
            declaration_collision=declaration_collision,
        )
        rows.append({
            **row,
            "velichina7_decision_doc_collision": row["number"] in doc_collisions,
            **decision,
        })
    return rows


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: stalled_pr_triage.py measure <PR>|queue", file=sys.stderr)
        return 2
    command, *rest = argv
    if command == "measure":
        if len(rest) != 1:
            print("usage: stalled_pr_triage.py measure <PR>", file=sys.stderr)
            return 2
        number = rest[0]
        ensure_unshallow()
        run_git("fetch", "--quiet", "origin", f"refs/pull/{number}/head:refs/remotes/origin/pr-{number}")
        print(json.dumps(measure_pr("origin/main", f"origin/pr-{number}"), indent=2, ensure_ascii=False))
        return 0
    if command == "queue":
        repo = os.environ.get("GITHUB_REPOSITORY")
        if not repo:
            print("GITHUB_REPOSITORY не задан", file=sys.stderr)
            return 2
        rows = cmd_queue(repo)
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    print(f"неизвестная команда: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
