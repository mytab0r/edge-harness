#!/usr/bin/env python3
"""Механическое сведение конфликтов класса «аддитивная вставка» (issue #1032,
дополнение к mechanical_rebase.py, issue #762).

Природа класса (разбор пяти реальных PR — 241/395/408/542/567 — на живой
очереди `conflict`, 2026-09-12): это НЕ конкуренция за логику, а ОБЕ стороны
НЕЗАВИСИМО дописывают новый элемент в одну и ту же точку общего реестра —
main добавила один раздел (например `REOPEN_ESCALATION_THRESHOLD`), PR
добавляет свой независимый (`TASK_REPLACEMENT_MARKER`) В ТОЙ ЖЕ точке файла.
`git rebase` сам эту форму не сводит (нет общей истории для 3-way merge на
одной строке вставки), хотя семантически оба блока совместимы — им просто
нужно оказаться РЯДОМ, не друг ВМЕСТО друга.

Осторожно (задание, «union на коде опасен»): слепая конкатенация обеих
сторон конфликта — не универсальный фикс, это ПОРЧА, если хотя бы одна
сторона правит СУЩЕСТВУЮЩИЙ код (тогда конкатенация оставляет ОБЕ версии
живыми — например, одна и та же функция объявлена дважды с разной
сигнатурой, и Python молча оставляет только ПОСЛЕДНЮЮ, требования первой
стороны теряются без единого предупреждения). Живой пример этого КЛАССА
опасности, найденный при разборе PR #629 (scheduler.py): обе стороны
конфликта заканчиваются одной и той же незавершённой строкой `def
append_session_notes(...):` с РАЗНОЙ сигнатурой — это правка ОДНОЙ и той же
функции, не добавление двух независимых. Второй живой пример — PR #883
(repo_invariants.py): обе стороны определяют `ESCALATING_INVARIANTS = (...)`
с РАЗНЫМ содержимым — конкатенация оставила бы работать только вторую
(последнее присваивание побеждает), молча потеряв числа первой.

Критерий безопасности (единственный ходовой признак «это ДВА независимых
добавления, не правка одного»), проверенный на всех перечисленных живых
случаях:

1. Обе стороны конфликта — это ПОЛНЫЕ, самостоятельно валидные фрагменты
   верхнего уровня (для .py — `ast.parse` каждой стороны ПО ОТДЕЛЬНОСТИ, без
   какого-либо общего контекста, обязан пройти без исключения; первая
   непустая строка каждой стороны обязана начинаться БЕЗ отступа). Правка
   ВНУТРИ существующего блока (функции/класса) почти всегда оставляет один
   бок фрагмента НЕЗАВЕРШЁННЫМ синтаксически (см. пример #629 выше — `def
   …:` без тела) или ОТСТУПЛЕННЫМ — оба признака ловятся этим правилом
   механически, без понимания семантики диффа.
2. Множества имён, которые каждая сторона ОПРЕДЕЛЯЕТ на верхнем уровне
   (функции/классы/присваивания), НЕ пересекаются. Это ловит второй класс
   опасности (пример #883 выше) — тот, где обе стороны СИНТАКСИЧЕСКИ
   валидны сами по себе, но правят одно и то же ИМЯ: конкатенация в этом
   случае оставила бы только последнее присваивание/определение живым.

Для Markdown-таблиц (единственный второй поддерживаемый тип — `docs/agents/
LABELS.md`, класс «обе стороны дописывают РАЗНУЮ строку одной таблицы»,
разобран на PR #241/#567/#607) критерий симметричен на уровне СТРОК:
каждая сторона — только строки таблицы (`| ключ | ... |`), ключи (первая
ячейка) каждой стороны уникальны сами по себе, и множества ключей ДВУХ
сторон не пересекаются — иначе (тот же ключ на обеих сторонах, живой случай
#567 — обе стороны несут строку `ci-failure` с РАЗНЫМ содержимым остальных
ячеек) это правка ОДНОЙ существующей строки, не добавление новой.

Любое сомнение (файл не .py/.md, конфликтующая сторона пуста, `ast.parse`
падает, имена/ключи пересекаются, хотя бы ОДИН из нескольких unmerged-файлов
PR не проходит) — ПОЛНЫЙ отказ, ни один файл PR не трогается (частичного
разрешения нет: либо решаем ВСЮ пачку unmerged-путей одного PR, либо не
трогаем НИЧЕГО — оставшийся конфликт уходит агентскому пути как раньше).

Верификация ПЕРЕД тем, как вызывающий код (mechanical_rebase.py) продолжит
рёбейз (`git rebase --continue`) — обязательное требование задания:
1. `ast.parse` ПОЛНОГО (не только хунка) итогового текста каждого .py файла.
2. pytest соседнего test_<name>.py (или самого файла, если это уже test_*.py)
   для каждого затронутого .py файла — единственный источник объективного
   «работает» после сшивки; провал теста = такой же отказ, как провал
   ast.parse (см. try_resolve).

Тесты: python -m pytest scripts/orchestra/test_additive_conflict_merge.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import re
import subprocess
import sys

SUPPORTED_SUFFIXES = (".py", ".md")

_TABLE_ROW_RE = re.compile(r"^\s*\|")


def parse_conflict_hunks(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, ours, theirs) для каждого конфликта в файле — start/end —
    0-based индексы строк (`text.split("\\n")`), ВКЛЮЧАЯ маркерные строки
    `<<<<<<<`/`>>>>>>>`, чтобы вызывающий код мог заменить ИМЕННО этот
    диапазон. Только ОБЫЧНЫЙ 2-way вид маркеров (`<<<<<<< / ======= /
    >>>>>>>`), не diff3 (`|||||||` не настроен нигде в этом репозитории —
    `git config merge.conflictstyle` по умолчанию не diff3)."""
    lines = text.split("\n")
    hunks: list[tuple[int, int, str, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<< "):
            start = i
            i += 1
            ours_lines: list[str] = []
            while i < len(lines) and lines[i] != "=======":
                ours_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError(f"конфликт начат на строке {start + 1}, но '=======' не найден")
            i += 1  # пропускаем строку =======
            theirs_lines: list[str] = []
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                theirs_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError(f"конфликт начат на строке {start + 1}, но '>>>>>>>' не найден")
            end = i
            hunks.append((start, end, "\n".join(ours_lines), "\n".join(theirs_lines)))
        i += 1
    return hunks


def _top_level_names(tree: ast.Module) -> set[str]:
    """Имена, которые фрагмент ОПРЕДЕЛЯЕТ на верхнем уровне — функции,
    классы, обычные и аннотированные присваивания. Пересечение этого
    множества между сторонами — признак правки ОДНОГО и того же имени
    (класс #883: `ESCALATING_INVARIANTS` определён на обеих сторонах с
    разным значением — конкатенация оставила бы только последнее)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _python_hunk_unsafe_reason(ours: str, theirs: str) -> str | None:
    """None — сторона признана безопасной аддитивной вставкой; иначе —
    человекочитаемая причина отказа (AGENTS.md, «алерт не гадает» — тот же
    принцип для отказа механизма: не молчать, называть, что именно не
    совпало)."""
    for label, side in (("ours", ours), ("theirs", theirs)):
        first_line = next((ln for ln in side.split("\n") if ln.strip()), "")
        if first_line and first_line[0] in (" ", "\t"):
            return (f"сторона {label} начинается с отступа ({first_line.strip()!r}) — "
                     "похоже на правку ВНУТРИ существующего блока, не добавление на "
                     "уровне модуля")
    try:
        ours_tree = ast.parse(ours)
        theirs_tree = ast.parse(theirs)
    except SyntaxError as error:
        return f"хотя бы одна сторона не разбирается как самостоятельный Python-фрагмент: {error}"
    collision = _top_level_names(ours_tree) & _top_level_names(theirs_tree)
    if collision:
        return f"обе стороны определяют одно и то же имя верхнего уровня: {sorted(collision)}"
    return None


def _row_keys(text: str) -> list[str] | None:
    """Ключи (первая ячейка) каждой строки текста, если ВСЕ непустые строки
    похожи на строку markdown-таблицы (`| ключ | ... |`) — иначе `None`
    (не наш класс, отказ на уровне вызывающей функции)."""
    keys: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        if not _TABLE_ROW_RE.match(line) or "|" not in line[1:]:
            return None
        cell = line.split("|")[1].strip()
        if not cell:
            return None
        keys.append(cell)
    return keys


def _markdown_hunk_unsafe_reason(ours: str, theirs: str) -> str | None:
    ours_keys = _row_keys(ours)
    theirs_keys = _row_keys(theirs)
    if ours_keys is None or theirs_keys is None:
        return "хотя бы одна сторона несёт строку не в форме `| ключ | ... |` — не табличная вставка"
    if not ours_keys or not theirs_keys:
        return "хотя бы одна сторона не содержит ни одной строки таблицы"
    if len(ours_keys) != len(set(ours_keys)) or len(theirs_keys) != len(set(theirs_keys)):
        return "повторяющийся ключ строки внутри одной стороны — подозрительная форма, отказ"
    collision = set(ours_keys) & set(theirs_keys)
    if collision:
        return f"обе стороны несут строку с одним и тем же ключом (правка одной записи): {sorted(collision)}"
    return None


def _hunk_unsafe_reason(ours: str, theirs: str, suffix: str) -> str | None:
    if not ours.strip() or not theirs.strip():
        return "одна из сторон конфликта пуста — не класс «две независимые вставки»"
    if suffix == ".py":
        return _python_hunk_unsafe_reason(ours, theirs)
    if suffix == ".md":
        return _markdown_hunk_unsafe_reason(ours, theirs)
    return f"тип файла {suffix or '(без расширения)'} не поддержан (только .py/.md)"


def resolve_file_text(text: str, suffix: str) -> tuple[str | None, str]:
    """(итоговый_текст, "") при успехе или (None, причина) при отказе — ни
    один конфликт файла не остаётся частично решённым: первый же небезопасный
    хунк отменяет решение ВСЕГО файла."""
    hunks = parse_conflict_hunks(text)
    if not hunks:
        return None, "маркеры конфликта не найдены (уже разрешено или другой формат)"
    lines = text.split("\n")
    out: list[str] = []
    cursor = 0
    for start, end, ours, theirs in hunks:
        reason = _hunk_unsafe_reason(ours, theirs, suffix)
        if reason is not None:
            return None, f"строки {start + 1}-{end + 1}: {reason}"
        out.extend(lines[cursor:start])
        if ours:
            out.extend(ours.split("\n"))
        if theirs:
            out.extend(theirs.split("\n"))
        cursor = end + 1
    out.extend(lines[cursor:])
    return "\n".join(out), ""


def _test_targets_for(repo_dir, resolved_py_paths: list) -> list[str]:
    """Относительные (к repo_dir) пути к pytest-целям для затронутых .py
    файлов — сосед `test_<имя>.py`, либо сам файл, если это уже test_*.py.
    Файл без соседнего теста просто не добавляет цель (не наш случай —
    ast.parse полного файла уже отдельная гарантия, см. try_resolve)."""
    targets: list[str] = []
    seen: set[str] = set()
    for path in resolved_py_paths:
        if path.name.startswith("test_"):
            candidate = path
        else:
            candidate = path.parent / f"test_{path.name}"
            if not candidate.exists():
                continue
        rel = str(candidate.relative_to(repo_dir))
        if rel not in seen:
            seen.add(rel)
            targets.append(rel)
    return targets


def try_resolve(repo_dir, unmerged_paths: list[str]) -> list[str] | None:
    """Пытается механически свести ВСЕ unmerged_paths как аддитивные
    вставки. Возвращает список путей (относительно repo_dir), которые
    записаны на диск и готовы к `git add`, либо `None` — если хотя бы один
    файл/хунк не прошёл проверку безопасности, `ast.parse` полного файла или
    pytest затронутых тестов (частичного применения нет: либо решаем всю
    пачку, либо не трогаем НИЧЕГО — файлы на диске в этом случае НЕ
    изменяются вовсе, вызывающий код (mechanical_rebase.attempt_rebase)
    делает `git rebase --abort`, который и так вернёт чистое дерево)."""
    plans: dict = {}
    for rel in unmerged_paths:
        path = repo_dir / rel
        suffix = path.suffix
        if suffix not in SUPPORTED_SUFFIXES:
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None  # файл отсутствует (rename/delete-конфликт) — не наш класс
        resolved, _reason = resolve_file_text(text, suffix)
        if resolved is None:
            return None
        plans[path] = resolved

    # Полная верификация синтаксиса .py ДО единой записи на диск (задание:
    # «проверяться ДО пуша, и при любом сомнении — отказ, а не молчаливая
    # порча»).
    for path, resolved in plans.items():
        if path.suffix == ".py":
            try:
                ast.parse(resolved)
            except SyntaxError:
                return None

    for path, resolved in plans.items():
        path.write_text(resolved, encoding="utf-8")

    resolved_py_paths = [path for path in plans if path.suffix == ".py"]
    test_targets = _test_targets_for(repo_dir, resolved_py_paths)
    if test_targets:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *test_targets, "-q"],
            cwd=repo_dir, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            # Верификация провалилась — НЕ откатываем файлы руками: вызывающий
            # код (attempt_rebase) в любом случае делает `git rebase --abort`
            # на отказе, который восстанавливает рабочее дерево целиком —
            # второй, независимой точки отката здесь не заводим.
            return None

    return [str(path.relative_to(repo_dir)) for path in plans]
