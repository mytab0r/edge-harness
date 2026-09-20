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

Третий поддерживаемый тип — shell (#1383): `.sh`, а также файл БЕЗ
расширения, чей шебанг называет шелл (в репозитории так живут десятки
исполняемых `scripts/gh/*`, `scripts/git/*`). Критерий симметричен
питоновскому: `bash -n` каждой стороны вместо `ast.parse` плюс непересечение
верхнеуровневых имён (функции и присваивания в НУЛЕВОЙ колонке). Отдельный,
несимметричный рубеж — heredoc: конфликт внутри незакрытого heredoc'а
отвергается всегда, потому что его тело — ДАННЫЕ, и все три проверки на нём
слепы одновременно (находка ai-ревью PR #1392, воспроизведена исполнением).
Честная разница с .py, которую отчёт печатает вслух: у шелла НЕТ третьего
рубежа — прогона соседнего теста, потому что соглашения «сосед-тест» для .sh
в репозитории нет.

Любое сомнение (тип файла не опознан, конфликтующая сторона пуста, `ast.parse`
или `bash -n` падает, имена/ключи пересекаются, хунк внутри heredoc, хотя бы
ОДИН из нескольких unmerged-файлов PR не проходит) — ПОЛНЫЙ отказ, ни один файл PR не попадает в `git add`
(частичного разрешения нет: либо решаем ВСЮ пачку unmerged-путей одного PR,
либо не трогаем НИЧЕГО — оставшийся конфликт уходит агентскому пути как
раньше). Каждая причина отказа печатается в лог job'а одной строкой, включая
хвост вывода pytest, — отказ не имеет права быть неотличимым от «не наш
класс» (находка ai-review PR #1033).

Верификация ПЕРЕД тем, как вызывающий код (mechanical_rebase.py) продолжит
рёбейз (`git rebase --continue`) — обязательное требование задания:
1. `ast.parse` ПОЛНОГО (не только хунка) итогового текста каждого .py файла.
2. Отсутствие ДУБЛИКАТОВ верхнеуровневых имён в итоговом .py (и ключей строк
   таблиц в итоговом .md) — находка ai-review PR #1033, круг 3: перехунковый
   критерий п.«критерий безопасности» видит «обе стороны одного хунка», но
   тот же класс проходит двумя РАЗНЫМИ хунками одного файла (`ours` первого
   хунка и `theirs` второго определяют `WORKER_TIMEOUT` с разными
   значениями — оба присваивания живут, молча побеждает последнее). Проверка
   по итоговому файлу ловит форму «разными хунками» и форму «сторона и
   нетронутая часть файла» той же ценой отказа.
3. pytest соседнего test_<name>.py (или самого файла, если это уже test_*.py)
   для каждого затронутого .py файла — единственный источник объективного
   «работает» после сшивки; провал теста = такой же отказ, как провал
   ast.parse, с напечатанной причиной (см. try_resolve, _refuse).

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
import os
import re
import subprocess
import sys

SUPPORTED_SUFFIXES = (".py", ".md", ".sh")

_TABLE_ROW_RE = re.compile(r"^\s*\|")

# ── Shell (#1383) ────────────────────────────────────────────────────────────
#
# Почему .sh добавлен ВТОРЫМ после .py/.md, а не «заодно»: замер живого
# прогона conflict-mechanical-rebase 35491299405 (2026-09-20T05:17Z) —
# отказы по типу файла распределены как `.sh` ×3, `.yml` ×2, `.json` ×1,
# без расширения ×1. То есть шелл — самый частый отказ, а «файлы без
# расширения», названные в постановке #1383 дешёвым направлением, дают один
# отказ из семи. Порядок выбран по замеру, не по порядку в тексте задачи.
#
# Критерий симметричен питоновскому, а не придуман заново:
#   .py  — `ast.parse` стороны + непересечение верхнеуровневых ИМЁН;
#   .sh  — `bash -n` стороны + непересечение верхнеуровневых ИМЁН
#          (функции и присваивания в нулевой колонке).
# Честная разница, названная вслух: у .py есть третий рубеж — pytest соседнего
# test_<имя>.py. У шелла соглашения «сосед-тест» в этом репозитории нет
# (тесты лежат как scripts/*/test/<имя>.{test,smoke}.sh с разными хвостами),
# поэтому третьего рубежа у .sh НЕТ, и отчёт обязан это печатать, а не
# умалчивать: гарантия для шелла слабее, чем для питона.
_SH_FUNCTION_RE = re.compile(
    r"^(?:function\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*\))?"
    r"|([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\))\s*\{?", re.MULTILINE)
# `declare`/`typeset` могут идти И с флагом, И без него; `alias x=` — тоже
# определение имени (находка ai-ревью PR #1392): неполный список означал бы
# «имена не пересеклись» там, где они пересекаются, то есть ложное «безопасно».
_SH_ASSIGN_RE = re.compile(
    r"^(?:export\s+|readonly\s+|local\s+|alias\s+"
    r"|(?:declare|typeset)\s+(?:-\S+\s+)?)?"
    r"([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)
_SHEBANG_SHELL_RE = re.compile(r"^#!.*\b(?:ba|z|k)?sh\b")

# `<<` или `<<-`, но НЕ `<<<` (herestring — не heredoc) и НЕ строка маркера
# конфликта (`<<<<<<< HEAD`).
#
# Работу делает ЛЕВАЯ граница `(?<!<)`, и это установлено мутацией, а не
# рассуждением. Первая редакция ставила правую, `<<(?!<)`, и этого НЕ хватало:
# в `<<<` символы 1-2 — тоже `<<`, а следом пробел, поэтому регексп находил
# heredoc СО СДВИГОМ НА СИМВОЛ. Последствия были два, оба тихие:
# `grep x <<< HELLO` открывал фантомный heredoc «HELLO», который никогда не
# закроется, а `<<<<<<< HEAD` становился heredoc'ом с ограничителем «HEAD» —
# то есть ЛЮБОЙ второй хунк файла отвергался как «внутри heredoc». Прежние
# тесты этого не видели: они ждали отказа и по другой причине, то есть
# зеленели не на том (класс #891/#893).
#
# Симметричная правая граница при наличии левой — мёртвый код: её снятие не
# меняет поведения ни на одном написании (`<<<`, `<<<WORD`, `a<<<b`,
# `<<<<<<< HEAD`), проверено исполнением. Недоказуемый рубеж здесь не
# оставлен сознательно — он выглядел бы защитой, не будучи ею.
_HEREDOC_START_RE = re.compile(
    r"(?<!<)<<-?\s*(?P<q>['\"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")


def _shell_heredoc_open_before(lines: list, index: int) -> str | None:
    """Имя ограничителя незакрытого heredoc'а, внутри которого оказалась
    строка `index`, либо None (#1383, блокирующая находка ai-ревью PR #1392).

    Зачем: тело heredoc — это ДАННЫЕ, а не код, и все три рубежа шелла на нём
    слепы одновременно. Строка тела стоит в нулевой колонке (значит «не
    отступ»), не определяет верхнеуровневых имён (значит «имена не
    пересекаются»), а `bash -n` принимает произвольную прозу внутри heredoc
    (значит «синтаксис валиден»). Итог: правка ОДНОЙ строки текста двумя
    сторонами выглядела бы как два независимых добавления и сводилась бы
    конкатенацией — в файле оказались бы ОБЕ взаимоисключающие строки.

    Это не теория: `scripts/gh/issue-create` — тот самый файл из замера
    задачи — несёт heredoc'и с markdown-телами issue.

    Несколько heredoc'ов, открытых одной строкой (`cmd <<A <<B`), держатся
    очередью: закрытие A не снимает B."""
    pending: list = []
    for line in lines[:index]:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        for match in _HEREDOC_START_RE.finditer(line):
            pending.append(match.group("delim"))
    return pending[0] if pending else None
_SHEBANG_PYTHON_RE = re.compile(r"^#!.*\bpython[0-9.]*\b")


def _bash_syntax_error(text: str) -> str | None:
    """None — `bash -n` принял текст; иначе первая строка его жалобы.

    Фрагмент, оборванный посередине конструкции (половина функции, середина
    case), `bash -n` не принимает — и это ровно тот отказ, который нужен:
    склеивать половинки в аддитивном слиянии нельзя."""
    result = subprocess.run(["bash", "-n"], input=text, capture_output=True,
                            text=True, encoding="utf-8")
    if result.returncode == 0:
        return None
    return (result.stderr or "").strip().split("\n")[0] or "bash -n отказал без текста"


def _shell_top_level_names(text: str) -> set[str]:
    """Имена, которые текст ОПРЕДЕЛЯЕТ на верхнем уровне: функции и
    присваивания в НУЛЕВОЙ колонке. Отступ значит «внутри чего-то» — такие
    строки в множество не попадают, иначе локальная переменная внутри функции
    сталкивалась бы с чужой глобальной и давала ложный отказ."""
    names: set[str] = set()
    for match in _SH_FUNCTION_RE.finditer(text):
        names.add(match.group(1) or match.group(2))
    for match in _SH_ASSIGN_RE.finditer(text):
        names.add(match.group(1))
    return names


def _shell_hunk_unsafe_reason(ours: str, theirs: str) -> str | None:
    for label, side in (("ours", ours), ("theirs", theirs)):
        first_line = next((ln for ln in side.split("\n") if ln.strip()), "")
        if first_line and first_line[0] in (" ", "\t"):
            return (f"сторона {label} начинается с отступа ({first_line.strip()!r}) — "
                    "похоже на правку ВНУТРИ существующего блока, не добавление на "
                    "верхнем уровне")
        error = _bash_syntax_error(side)
        if error is not None:
            return (f"сторона {label} не разбирается как самостоятельный "
                    f"shell-фрагмент (bash -n): {error}")
    collision = _shell_top_level_names(ours) & _shell_top_level_names(theirs)
    if collision:
        return f"обе стороны определяют одно и то же имя верхнего уровня: {sorted(collision)}"
    return None


def _duplicate_shell_top_level_names(text: str) -> list[str]:
    """Тот же класс, что _duplicate_top_level_names у .py: одно имя,
    определённое дважды РАЗНЫМИ хунками одного файла, перехунковый критерий
    не видит, а слепая конкатенация оставляет живым только последнее."""
    counts: dict[str, int] = {}
    for match in _SH_FUNCTION_RE.finditer(text):
        name = match.group(1) or match.group(2)
        counts[name] = counts.get(name, 0) + 1
    for match in _SH_ASSIGN_RE.finditer(text):
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    return sorted(name for name, n in counts.items() if n > 1)


def language_of(path, text: str) -> str | None:
    """Язык файла для критерия безопасности: расширение, а при его отсутствии —
    шебанг. Файлов без расширения в репозитории десятки (`scripts/gh/*`,
    `scripts/git/*`), и все они исполняемые — отказывать им по одному только
    отсутствию суффикса значит отказывать по орфографии имени, а не по
    существу (постановка #1383). `None` — тип не поддержан."""
    suffix = path.suffix
    if suffix in SUPPORTED_SUFFIXES:
        return suffix
    if suffix:
        return None
    first_line = text.split("\n", 1)[0]
    if _SHEBANG_SHELL_RE.match(first_line):
        return ".sh"
    if _SHEBANG_PYTHON_RE.match(first_line):
        return ".py"
    return None


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
    return set(_top_level_name_list(tree))


def _top_level_name_list(tree: ast.Module) -> list[str]:
    """То же, что _top_level_names, но список с повторами — источник для
    поиска ДУБЛИКАТОВ в итоговом файле (находка ai-review PR #1033, круг 3:
    перехунковый критерий ловит «обе стороны одного хунка», но не «ours
    первого хунка и theirs второго» — итог содержит оба присваивания,
    ast.parse зелёный, молча живёт последнее)."""
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _duplicate_top_level_names(tree: ast.Module) -> list[str]:
    """Имена верхнего уровня, определённые в итоговом файле БОЛЕЕ ОДНОГО
    раза, отсортированно. Python молча оставляет живым последнее
    присваивание/определение — дубликат в сведённом файле значит «требования
    одной из сторон потеряны без предупреждения», независимо от того,
    внесён ли дубликат обеими сторонами одного хунка, разными хунками или
    стороной и нетронутой частью файла. Честная цена: файл, у которого
    дубликат был ЕЩЁ до конфликта, теперь отказывается от аддитивного
    сведения целиком — один такт агентского пути, тот же консервативный
    отказ, что и у всех прочих проверок (находка ai-review PR #1033)."""
    counts: dict[str, int] = {}
    for name in _top_level_name_list(tree):
        counts[name] = counts.get(name, 0) + 1
    return sorted(name for name, n in counts.items() if n > 1)


def _table_row_keys_full_text(text: str) -> list[str]:
    """Ключи (первая ячейка) ВСЕХ строк таблицы итогового текста, с
    повторами. В отличие от _row_keys не требует, чтобы КАЖДАЯ строка была
    табличной (итоговый файл несёт заголовки/прозу вокруг таблиц) —
    дубликаты ключей ищутся только среди табличных строк (находка ai-review
    PR #1033: тот же ключ от ours одного хунка и theirs другого)."""
    keys: list[str] = []
    for line in text.split("\n"):
        if not line.strip() or not _TABLE_ROW_RE.match(line) or "|" not in line[1:]:
            continue
        cell = line.split("|")[1].strip()
        if cell:
            keys.append(cell)
    return keys


def _duplicate_row_keys(text: str) -> list[str]:
    """Ключи строк таблицы, встречающиеся в итоговом тексте более одного
    раза (та же логика «правка одной записи, не добавление новой», что в
    _markdown_hunk_unsafe_reason, но по всему итоговому файлу, а не внутри
    одного хунка). Сюда попадают и легитимные повторы разделителей/шапок
    нескольких таблиц файла — та же консервативная цена «один такт
    агентского пути», осознанно принятая (находка ai-review PR #1033)."""
    counts: dict[str, int] = {}
    for key in _table_row_keys_full_text(text):
        counts[key] = counts.get(key, 0) + 1
    return sorted(key for key, n in counts.items() if n > 1)


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
    if suffix == ".sh":
        return _shell_hunk_unsafe_reason(ours, theirs)
    return (f"тип файла {suffix or '(без расширения)'} не поддержан "
            "(только .py/.md/.sh; файл без расширения опознаётся по шебангу)")


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
        if suffix == ".sh":
            heredoc = _shell_heredoc_open_before(lines, start)
            if heredoc is not None:
                return None, (f"строки {start + 1}-{end + 1}: конфликт внутри "
                              f"незакрытого heredoc (<<{heredoc}) — это ДАННЫЕ, "
                              "а не код: отступа нет, верхнеуровневых имён нет, "
                              "bash -n принимает любую прозу, поэтому все три "
                              "рубежа слепы, и правка одной строки двумя "
                              "сторонами склеилась бы в обе сразу")
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


def _refuse(reason: str) -> None:
    """Печатает причину отказа ОДНОЙ строкой в лог job'а и возвращает None
    (находка ai-review PR #1033, замечание из чеклиста: «отказ верификации
    неотличим от „не наш класс“ и невидим в логе» — рычаг мог бы молча
    мертветь в проде, как те самые 0/12 замера; в т.ч. «No module named
    pytest» — workflow pytest не ставит, и провал запуска pytest обязан быть
    виден именно как провал запуска, не как «не аддитивный конфликт»)."""
    print(f"::warning::additive_conflict_merge: отказываюсь сводить — штатный "
          f"агентский путь продолжит. Причина: {reason}")
    return None


# Токены, которые НЕ уходят в верификационный pytest (находка ai-review
# PR #1033). Здесь впервые В ЭТОМ WORKFLOW исполняется код ИЗ PR (conftest.py,
# test_*.py и всё, что они импортируют) — воркер-агент исполняет код PR
# штатно, речь только про conflict-mechanical-rebase.yml; шаг
# `conflict-mechanical-rebase.yml` несёт `GH_TOKEN: ${{ secrets.GH_PIPELINE_PAT }}`
# — write-токен репозитория. Эскалации привилегий здесь нет (агент-воркер и
# так ходит под тем же PAT), но эпик «Безопасность и токены: изоляция
# секретов от дочерних процессов» требует обратного по умолчанию, а сети
# этим тестам не нужно вовсе. Список — одно место правды: и код, и гвардия
# читают его отсюда.
VERIFICATION_STRIPPED_ENV_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_PIPELINE_PAT")


def _verification_env() -> dict:
    """Окружение для верификационного pytest: копия текущего БЕЗ токенов
    (VERIFICATION_STRIPPED_ENV_VARS). Всё остальное (PATH, PYTHONPATH,
    HOME) сохраняется — без него pytest просто не запустится."""
    env = dict(os.environ)
    for name in VERIFICATION_STRIPPED_ENV_VARS:
        env.pop(name, None)
    return env


def try_resolve(repo_dir, unmerged_paths: list[str]) -> list[str] | None:
    """Пытается механически свести ВСЕ unmerged_paths как аддитивные
    вставки. Возвращает список путей (относительно repo_dir), которые
    записаны на диск и готовы к `git add`, либо `None` — если хотя бы один
    файл/хунк не прошёл проверку безопасности, `ast.parse` полного файла или
    pytest затронутых тестов (частичного применения нет: либо решаем всю
    пачку, либо не трогаем НИЧЕГО). Каждая причина отказа печатается в лог
    одной строкой (`_refuse`), включая хвост вывода pytest.

    Честный контракт по состоянию диска на отказе (находка ai-review
    PR #1033, прежняя формулировка противоречила поведению): отказ ДО записи
    (неподдержанный файл/небезопасный хунк/провал ast.parse) не трогает файлы
    вовсе; отказ ПОСЛЕ записи (провал pytest) оставляет файлы УЖЕ СЛИТЫМИ на
    диске. В обоих случаях вызывающий код (mechanical_rebase.attempt_rebase)
    делает `git rebase --abort`, который и восстанавливает рабочее дерево
    целиком, — второй, независимой точки отката здесь не заводится, но
    читать диск между `try_resolve → None` и `--abort` без учёта этого
    нельзя."""
    plans: dict = {}
    languages: dict = {}
    for rel in unmerged_paths:
        path = repo_dir / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return _refuse(f"{rel}: файл не читается (rename/delete-конфликт?) — не наш класс")
        except UnicodeDecodeError as error:
            # UnicodeDecodeError — наследник ValueError, а не OSError: до
            # находки ai-review PR #1033 (круг 5) он уходил МИМО всего
            # механизма отказа, `process_pull` ловит только RuntimeError, и
            # один нечитаемый файл ронял ВЕСЬ проход job'а с трейсбеком —
            # summary не писался, хвост очереди не обрабатывался, крон падал
            # на том же PR каждые 15 минут. Причина названа отдельной строкой,
            # не слита с «rename/delete» (AGENTS.md, «алерт не гадает»):
            # лечатся эти два случая по-разному.
            return _refuse(f"{rel}: файл не читается как UTF-8 "
                           f"(бинарный или чужая кодировка: {error.reason}, "
                           f"байт {error.start}) — не наш класс")
        # Язык — после чтения: у файла без расширения он живёт в шебанге,
        # то есть В ТЕКСТЕ, а до #1383 решение принималось по одному только
        # имени и роняло целую семью исполняемых скриптов репозитория.
        suffix = language_of(path, text)
        if suffix is None:
            return _refuse(f"{rel}: тип файла {path.suffix or '(без расширения)'} "
                           "не поддержан (только .py/.md/.sh; файл без "
                           "расширения опознаётся по шебангу)")
        languages[path] = suffix
        try:
            resolved, reason = resolve_file_text(text, suffix)
        except ValueError as error:
            # parse_conflict_hunks поднимает ValueError на ОБРЕЗАННЫХ маркерах
            # («<<<<<<<» без «=======»/«>>>>>>>») — та же семья, что выше:
            # штатный отказ, а не падение прохода.
            return _refuse(f"{rel}: маркеры конфликта не разбираются "
                           f"({error}) — не наш класс")
        if resolved is None:
            return _refuse(f"{rel}: {reason}")
        plans[path] = resolved

    # Полная верификация синтаксиса .py ДО единой записи на диск (задание:
    # «проверяться ДО пуша, и при любом сомнении — отказ, а не молчаливая
    # порча»).
    for path, resolved in plans.items():
        if languages[path] == ".py":
            try:
                resolved_tree = ast.parse(resolved)
            except SyntaxError as error:
                return _refuse(f"{path.name}: итоговый файл не разбирается "
                               f"ast.parse'ом целиком: {error}")
            # Дубликаты имён верхнего уровня ищутся по ИТОГОВОМУ файлу, не по
            # хунку (находка ai-review PR #1033, круг 3: тот же класс «обе
            # стороны правят одно имя» проходит двумя РАЗНЫМИ хунками одного
            # файла — перехунковый критерий его не видит, ast.parse зелёный,
            # молча живёт последнее присваивание).
            duplicates = _duplicate_top_level_names(resolved_tree)
            if duplicates:
                return _refuse(f"{path.name}: итоговый файл определяет "
                               f"верхнеуровневое имя более одного раза "
                               f"(слепая конкатенация оставила бы живым только "
                               f"последнее определение): {duplicates}")
        elif languages[path] == ".sh":
            error = _bash_syntax_error(resolved)
            if error is not None:
                return _refuse(f"{path.name}: итоговый файл не принимается "
                               f"bash -n целиком: {error}")
            duplicates = _duplicate_shell_top_level_names(resolved)
            if duplicates:
                return _refuse(f"{path.name}: итоговый файл определяет "
                               f"верхнеуровневое имя более одного раза "
                               f"(слепая конкатенация оставила бы живым только "
                               f"последнее определение): {duplicates}")
        elif languages[path] == ".md":
            # Тот же класс для таблиц (находка ai-review PR #1033): один ключ
            # строки от ours одного хунка и theirs другого — правка одной
            # записи, не два независимых добавления.
            duplicates = _duplicate_row_keys(resolved)
            if duplicates:
                return _refuse(f"{path.name}: итоговый текст содержит строку "
                               f"таблицы с одним и тем же ключом более одного "
                               f"раза (правка одной записи, не добавление "
                               f"новой): {duplicates}")

    for path, resolved in plans.items():
        path.write_text(resolved, encoding="utf-8")

    resolved_py_paths = [path for path in plans if languages[path] == ".py"]
    resolved_sh_paths = [path for path in plans if languages[path] == ".sh"]
    test_targets = _test_targets_for(repo_dir, resolved_py_paths)
    # Факт верификации восстановим из лога, а не домысливается по исходу
    # (находка ai-review PR #1033, круг 5: строка отчёта заявляла
    # «затронутые тесты перепроверены», хотя соседних test_*.py могло не
    # быть вовсе, и тогда не прогонялось НИЧЕГО). Печатаем ровно то, что
    # было: список целей или честное «целей нет».
    if test_targets:
        print(f"::notice::additive_conflict_merge: верификация ДО пуша — "
              f"pytest {' '.join(test_targets)}")
    elif resolved_py_paths:
        print("::notice::additive_conflict_merge: верификация ДО пуша — "
              "только ast.parse итоговых файлов: соседних test_*.py у "
              "затронутых модулей нет, pytest не запускался")
    else:
        # Без .py говорить «только ast.parse» — неправда: ast.parse тут не
        # звался вовсе (находка ai-ревью PR #1392). Отчёт обязан называть то,
        # что было, а не то, что бывает обычно.
        print("::notice::additive_conflict_merge: верификация ДО пуша — "
              "питоновских файлов в пачке нет, ast.parse и pytest не "
              "запускались; что проверялось — строками ниже")
    if resolved_sh_paths:
        # Честная граница, а не умолчание (#1383): у шелла нет соглашения
        # «сосед-тест», поэтому третьего рубежа нет и гарантия слабее, чем
        # у .py. Читатель лога обязан видеть это, а не достраивать сам.
        print("::notice::additive_conflict_merge: shell-файлы "
              f"({', '.join(path.name for path in resolved_sh_paths)}) "
              "проверены bash -n и непересечением верхнеуровневых имён; "
              "тестов у них не запускалось — соглашения «сосед-тест» для .sh "
              "в репозитории нет, гарантия слабее питоновской")
    if test_targets:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *test_targets, "-q"],
            cwd=repo_dir, capture_output=True, text=True, encoding="utf-8",
            env=_verification_env(),
        )
        if result.returncode != 0:
            # Верификация провалилась — НЕ откатываем файлы руками: вызывающий
            # код (attempt_rebase) в любом случае делает `git rebase --abort`
            # на отказе, который восстанавливает рабочее дерево целиком —
            # второй, независимой точки отката здесь не заводим. Но отказ
            # обязан быть видимым и различимым (см. _refuse): печатаем хвост
            # вывода pytest одной строкой — «No module named pytest»,
            # красный тест или падение сбора — это РАЗНЫЕ причины, а не
            # обезличенный «не наш класс».
            tail = " ⏎ ".join(
                line.strip() for line in (result.stdout or "").strip().splitlines()[-5:]
                if line.strip()
            )
            stderr_tail = (result.stderr or "").strip().splitlines()
            tail += (f" ⏎ stderr: {stderr_tail[-1].strip()}" if stderr_tail else "")
            return _refuse(
                f"pytest {' '.join(test_targets)} упал rc={result.returncode} "
                f"(файлы оставлены слитыми на диске, откат — git rebase --abort "
                f"вызывающего): {tail}"
            )

    return [str(path.relative_to(repo_dir)) for path in plans]
