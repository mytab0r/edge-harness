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
      изменённых РЕГИОНОВ между PR-диффом и main-диффом в общих файлах
      (`functional_overlap`): МИНИМАЛЬНЫЕ AST def/class-регионы для `.py`
      (регион, СТРОГО содержащий другие регионы — класс с методами, функция
      с вложенной, — фантомная единица: правки двух разных методов одного
      класса давали ложное «пересоздать»; блокирующая находка второго ревью
      PR #1219), line-range fallback (пересечение задетых строк обеих сторон
      в КООРДИНАТАХ MERGE-BASE) для остальных файлов — живой класс прогона
      2026-09-14:
      #395/#613/#920/#1057 конфликтуют в shell/yaml/json/markdown, и
      величина 3 без fallback честно рапортовала им «пересечений нет»,
      ничего не измерив. Строки .py ВНЕ регионов (модульный код) считаются
      тем же line-range правилом (режим `ast+line-range`), не невидимы. Сам
      строковый ratio сохранён как СПРАВОЧНАЯ
      величина: `line_drift_ratio` считается машиной по общим файлам и
      попадает в вывод прогона, в `decide()` не входит.
  4. Метода не было вовсе: PR добавляет номер инварианта
     (`scripts/orchestra/repo_invariants.py`), который main НЕЗАВИСИМО
     ТОЖЕ добавил с общего merge-base — коллизия, найденная после
     сведения, тихо портит реестр. ФОРМАТ объявления (`N. check_имя — …`)
     переиспользуется из `scripts/lib/invariant_numbering.py` (#904, слит
     2026-09-14) — второй копии парсера реестра этот модуль не заводит;
     СРАВНЕНИЕ сторон относительно merge-base остаётся здесь, потому что
     вопрос другой: арбитр #1201 сравнивает два дерева напрямую
     («коллизия ЕСТЬ СЕЙЧАС»), триаж — «обе стороны независимо добавили
     один и тот же номер С ОБЩЕГО ПРЕДКА» (история независимости, не
     снимок). `docs/decisions`/`docs/research` тем же классом закрыты
     `scripts/lib/decision_numbering.py` (#1078) — `cmd_queue` ниже
     переиспользует `check_decision_doc_number_collisions` напрямую.

Величина 5 («есть ли уже эквивалент в main») сюда НЕ включена намеренно —
ADR прав: это чтение и суждение, не число (см. его раздел «Что машине
отдать нельзя»). `decide()` принимает её как необязательный, явно
опциональный вход (`replacement_found: bool | None`), не пытается
вычислить сама.

## Величина 8: PR-дубль уже слитой работы (issue #1236, живой случай PR #1020)

Величина 5 читается человеком («есть ли уже эквивалент в main») — величина 8
не требует чтения вовсе: все файлы, ТРОНУТЫЕ PR (прод-форма `gh api
pulls/{n}/files`, статус `added`/`removed`/`modified`/`renamed` — обход
страниц `review_labels.list_pr_files`, класс пагинации #308/#309), побайтно
(blob-SHA, не diff-текст) равны соответствующим файлам main → PR не меняет
main вовсе, дубль уже слитой работы. Живая улика: PR #1020 закрыт как
мёртвый груз, но величина 3 (AST-пересечение) на нём дала «8 пересечений» —
это было пересечение PR С ЕГО ЖЕ КОПИЕЙ в main (работа уже слита коммитом
`1def95d6`, задача #1021 закрыта), не с независимой правкой main; метод
величины 3 структурно не различает эти два случая. Величина 8 вычисляется
ПЕРВОЙ в `cmd_queue` (см. `already_in_main_check`), ДО вызова `measure_pr`
(который считает дорогую величину 3) — обнаруженный дубль делает величину 3
не только ненужной, но и ВВОДЯЩЕЙ В ЗАБЛУЖДЕНИЕ для такого PR, поэтому она
не считается вовсе (обесценена, не просто пропущена ради экономии). Статус
`removed` — особый случай (requirement 3 issue #1236): PR, удаляющий файл,
который main ЕЩЁ несёт, — настоящая правка, не дубль; файл, уже отсутствующий
и там и там, — согласие сторон, само по себе не решает. Третье состояние
(`scripts/lib/check_result.py`, #1096) — `unknown()`, если хотя бы один blob
не удалось прочитать (сеть/git отказали), а расхождений при этом не нашлось:
«дубль» и «не дубль» здесь одинаково неверны для непроверенного PR.

Импорт соседних модулей — importlib по файлу (паттерн claim_task/
review_labels/upstream_drift: скрипты этого репозитория запускаются как
файлы, не как пакет).

CLI:
  python stalled_pr_triage.py measure <PR>       — величины 1–3,7 для
                                                    одного PR (git, без gh).
  python stalled_pr_triage.py queue               — величины 1,2,3,4,6,7,8 +
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

_IN_SPEC = importlib.util.spec_from_file_location(
    "invariant_numbering", Path(__file__).resolve().parents[1] / "lib" / "invariant_numbering.py")
invariant_numbering = importlib.util.module_from_spec(_IN_SPEC)
_IN_SPEC.loader.exec_module(invariant_numbering)  # type: ignore[union-attr]

# Три состояния проверки (ok/violation/unknown) — одно место правды #1096;
# вторая половина величины 7 различает «коллизий нет» и «проверка не
# состоялась» именно по нему.
_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parents[1] / "lib" / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]


# ── Носитель величины 7 для repo_invariants.py (см. шапку модуля, п. 4) ─────

# Путь и ФОРМАТ объявления — из invariant_numbering (#904), не вторая копия:
# реестр инвариантов в докстринге repo_invariants.py парсится только там.
INVARIANT_FILE = invariant_numbering.TARGET_PATH


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
    """{«имя:строка_начала»: (start, end)} — МИНИМАЛЬНЫЕ def/class-регионы
    верхнего и вложенного уровня. Ключ несёт номер строки начала — два метода
    с одинаковым именем в разных классах (`__init__` дюжину раз) не должны
    схлопываться в один регион. Начало региона — МИНИМУМ по декораторам
    (находка ревью PR #1219): правка только `@декоратора` — правка поведения
    функции, регион, начатый с строки `def`, её бы не заметил.

    Регион, СТРОГО содержащий другой отслеживаемый регион (класс с методами,
    внешняя функция с вложенной), единицей пересечения НЕ является
    (блокирующая находка ревью PR #1219): его диапазон покрывает всё тело,
    и правки двух РАЗНЫХ методов одного класса давали фантомное пересечение
    `{класс}` → ложное «пересоздать» с reason'ом «сведение сотрёт правку»,
    хотя git сводит разные методы чисто; тот же класс для вложенных функций.
    Сигнал не теряется: вложенные регионы несут его сами — PR, заменяющий
    класс целиком, задевает строки и методов внутри. Строки класса ВНЕ
    методов (атрибуты, докстринг класса) отслеживаются в
    `measure_functional_overlap` наравне с прочими внерегиональными строками
    (line-range поверх AST), не регионом класса. Синтаксическая ошибка (PR
    мог оставить файл битым на промежуточном коммите) — пустой словарь, не
    исключение: вызывающий (`measure_functional_overlap`) уводит такой файл
    в line-range fallback, не молчит."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    entries: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            decorators = [d.lineno for d in getattr(node, "decorator_list", [])
                          if hasattr(d, "lineno")]
            start = min([node.lineno, *decorators])
            entries.append((f"{node.name}:{start}", start, end))
    minimal: dict[str, tuple[int, int]] = {}
    for name, start, end in entries:
        contains_other = any(other_start >= start and other_end <= end
                             for _, other_start, other_end in entries
                             if (other_start, other_end) != (start, end))
        if not contains_other:
            minimal[name] = (start, end)
    return minimal


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def parse_base_touched_lines(unified_diff: str) -> set[int]:
    """Номера строк БАЗОВОГО файла (merge-base), задетых одной стороной
    унифицированного диффа (`git diff --unified=0`), из hunk-заголовков
    `@@ -a,b +c,d @@`. Сторона МИНУС, не плюс (находка ревью PR #1219):
    сверять с регионами базового файла можно только в базовых координатах —
    плюс-сторона сдвигается удалениями выше по файлу, и правка функции f
    засчитывалась бы соседней g (ложное «пересечение» → ложное
    «пересоздать»).

    `b` отсутствует — 1 строка (обычная замена). `b == 0` — чистая вставка
    ПОСЛЕ базовой строки a (`-0,0` — вставка в начало файла), регион
    считается задетым строкой a: точка вставки замыкается предыдущей
    строкой; ложную цену на границе регионов (вставка сразу ПОСЛЕ конца
    функции) гасит требование согласия ОБЕИХ сторон (пересечение)."""
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


def count_changed_lines(unified_diff: str) -> int:
    """Число строк `+`/`-` в диффе (единица справочного строкового ratio
    ADR 0021: «насколько большие изменения»). Заголовки файлов
    (`+++`/`---`) содержимым не считаются."""
    return sum(1 for line in unified_diff.splitlines()
               if (line.startswith("+") and not line.startswith("+++"))
               or (line.startswith("-") and not line.startswith("---")))


def intersecting_line_runs(pr_lines: set[int], main_lines: set[int]) -> list[str]:
    """Line-range fallback величины 3 (файлы без AST-регионов): пересечение
    задетых обеими сторонами базовых строк, сгруппированное в непрерывные
    диапазоны `"10-14"` / `"7"`. Единица подсчёта — РЕГИОН, не строка
    (одинаковая с AST-режимом), иначе величина раздувается пропорционально
    длине блока, а не количеству смысловых столкновений."""
    common = sorted(pr_lines & main_lines)
    spans: list[list[int]] = []
    for line in common:
        if spans and line == spans[-1][1] + 1:
            spans[-1][1] = line
        else:
            spans.append([line, line])
    return [f"{start}-{end}" if end > start else str(start) for start, end in spans]


def touched_regions(ranges: dict[str, tuple[int, int]], changed_lines: set[int]) -> set[str]:
    return {
        name for name, (start, end) in ranges.items()
        if any(start <= line <= end for line in changed_lines)
    }


def functional_overlap(pr_regions: set[str], main_regions: set[str]) -> set[str]:
    """Пересечение имён регионов, которые задели ОБЕ стороны (PR и main)
    относительно общего merge-base — величина 3 вместо строкового ratio ADR
    (см. шапку модуля). Непустое пересечение делает величины 1–3 «ВЕЛИКИМИ»
    → исход «пересоздать» по таблице ADR 0024 — то же правило, что для
    git-конфликта: механическое сведение вернёт дефект или сотрёт правку
    одной из сторон. Чтение того, КАКИЕ именно функции столкнулись, — часть
    ИСПОЛНЕНИЯ решения (пересоздание начинается с чтения), не условие его
    отсрочки: одна формулировка во всех трёх местах (докстринг, `decide`,
    reason-строка), находка ревью PR #1219."""
    return pr_regions & main_regions


def region_word(count: int) -> str:
    """«1 регион» / «2 региона» / «5 регионов» — согласование числа
    с существительным в reason-строке, которую читает человек."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} регион"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return f"{count} региона"
    return f"{count} регионов"


def line_drift_ratio(main_drift_lines: int, pr_own_lines: int) -> float | None:
    """Легаси-формула ADR 0021 (строки дрейфа main / строки PR) — СПРАВОЧНАЯ
    величина (issue #1218, дефект 3: инфлируется несвязанными изменениями,
    в `decide()` не входит). Не мёртвый код: считается машиной в
    `measure_functional_overlap` по общим файлам (находка ревью PR #1219:
    «справочная величина», которую машина не вычисляет ни для одного PR, —
    утверждение об артефакте без адреса) и попадает в вывод прогона.
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
    invariant_collision: bool = False,
    decision_doc_collision: bool = False,
    already_in_main: bool = False,
) -> dict:
    """Единая точка решения — читает ВСЕ входы вместе (ADR 0021, «шесть
    величин вместе», не порог на одной). `task_state` — "open"/"closed"/
    "none" (PR без задачи пула, боты)/"unknown" (GraphQL не ответил —
    «не знаю» не превращается в «открыта», находка ревью PR #1219; решение
    при этом не строится на величине 4 вовсе, а в reasons попадает честное
    «не подтверждено»). `ai_verdict` — одна из `review_labels.AI_VERDICTS`
    либо `None` (вердикта ещё нет). `already_in_main` — величина 8 (issue
    #1236, см. шапку модуля): МАШИННО доказанный факт («все тронутые файлы
    побайтно равны main»), в отличие от `replacement_found` (чтение
    человеком/суждением, величина 5) — поэтому проверяется РАНЬШЕ него, а не
    вместо: он не требует ничьего суждения, чтобы быть верным.

    Порядок проверок — от самого дешёвого и самого достоверного решения к
    самому дорогому суждению (велики ли 1–3):

      0. `already_in_main=True` — PR не меняет main вовсе (величина 8,
         машинное доказательство byte-for-byte) — закрыть немедленно, раньше
         даже предупреждений о коллизии номеров (величина 7): нечему
         коллидировать, если сводить нечего.
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
    после мержа даст дубль номера. Половины названы РАЗДЕЛЬНО
    (`invariant_collision` — реестр repo_invariants.py против main,
    `decision_doc_collision` — номера docs/decisions|research, где
    виновником бывает и другой открытый PR, не только main), и reason
    формулируется по сработавшей половине — «алерт не гадает» (находка
    ревью PR #1219: одна строка про «# Инвариант N» описывала только
    инвариантную половину и врала в doc-случаях)."""
    reasons: list[str] = []
    if already_in_main:
        reasons.append(
            "величина 8: все файлы, тронутые PR, побайтно (git blob-SHA) совпадают с main — "
            "PR не меняет main, дубль уже слитой работы (живой случай #1020)")
        return {"action": ACTION_CLOSE, "reasons": reasons}
    if invariant_collision:
        reasons.append(
            "величина 7 (инварианты): PR добавляет запись в реестре repo_invariants.py, "
            "которую main тоже независимо добавил с общего merge-base — сведение молча "
            "даст дубль номера, перенумеровать ПЕРЕД доведением/пересозданием")
    if decision_doc_collision:
        reasons.append(
            "величина 7 (decision/research-номера): номер ADR/research-файла, который "
            "несёт PR, занят другим источником (main или другой открытый PR) — "
            "переиспользован decision_numbering, перенумеровать ПЕРЕД сведением")

    if replacement_found is True:
        reasons.append("величина 5: эквивалент уже в main (дублирование подтверждено чтением)")
        return {"action": ACTION_CLOSE, "reasons": reasons}

    if task_state == "closed" and replacement_found is not True:
        reasons.append("величина 4: задача закрыта, величина 5 замены не находит")
        return {"action": ACTION_CLOSE, "reasons": reasons}

    if task_state == "unknown":
        reasons.append(
            "величина 4: состояние задачи не получено (GraphQL не ответил) — "
            "не подтверждено, решение на величине 4 не строится")

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
            f"величина 3: пересечение регионов PR и main с общего merge-base — "
            f"{region_word(functional_overlap_count)}; механическое сведение вернёт дефект "
            "или сотрёт правку одной из сторон, поэтому пересоздать; какие именно — "
            "читать при исполнении")
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


def blob_sha(ref: str, path: str, cwd: str | None = None) -> str | None:
    """SHA блоба `path` на `ref` (`git rev-parse ref:path`) — носитель
    сравнения величины 8 (issue #1236): байт-в-байт сравнение через
    blob-SHA, не через diff-текст (совпадающий SHA — то же самое дерево
    байт, диффа при этом может не быть вовсе, что и требуется). `None` —
    файла на этом `ref` нет: `git rev-parse` отвечает ненулевым кодом на
    отсутствующий путь — легитимный исход («файла там нет»), не сбой
    инструмента (в отличие от `is_conflicting`, где ненулевой код без
    формы CONFLICT — отказ инструмента; здесь путь либо есть, либо нет,
    третьего по смыслу самого git нет)."""
    result = subprocess.run(
        ["git", "rev-parse", f"{ref}:{path}"], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def changed_files(base_sha: str, ref: str, cwd: str | None = None) -> set[str]:
    """Все файлы, изменённые между merge-base и `ref` (без фильтра по
    расширению: line-range fallback величины 3 работает по НЕ-.py общим
    файлам — находка ревью PR #1219, п. 4 блокирующих)."""
    out = run_git("diff", "--name-only", base_sha, ref, cwd=cwd)
    return {line for line in out.splitlines() if line.strip()}


# Формы CONFLICT-строк `git merge-tree --write-tree`. Решает КОД ВОЗВРАТА
# (находка ревью PR #1219: rc — контракт git, regex — декорация); regex'ы —
# best-effort детализация «какие файлы», не условие вердикта.
_CONFLICT_MERGE_IN_RE = re.compile(r"^CONFLICT \([^)]*\): Merge conflict in (.+)$", re.MULTILINE)
_CONFLICT_PATH_RE = re.compile(
    r"^CONFLICT \((?:modify/delete|rename/delete|rename/add|add/add|delete/modify)\): (.+?) "
    r"(?:deleted|added|modified) in ", re.MULTILINE)


def is_conflicting(main_ref: str, head_ref: str, cwd: str | None = None) -> tuple[bool, list[str]]:
    """`(конфликтует?, [конфликтующие файлы])` — реальный `git merge-tree
    --write-tree`, не GitHub `mergeable` (тот считается GitHub асинхронно и
    может быть `null`/`unknown` — см. review_labels.CONFLICT_CLEAR_STATES;
    здесь считаем сами, синхронно, на живом дереве).

    Код возврата — вердикт (находка ревью PR #1219): `0` — сведение чисто;
    `1` с НЕПУСТЫМ stdout — конфликт (включая формы, чей CONFLICT-текст не
    совпадает с «Merge conflict in», например modify/delete; git печатает
    дерево и CONFLICT-строки именно в stdout). `1` с ПУСТЫМ stdout — отказ
    самого инструмента, не конфликт: замер с несуществующим ref отвечает
    rc=1, но только в stderr (`merge-tree: … - not something we can merge`),
    stdout пуст — так же отвечает старый git без `--write-tree`. Любой
    другой код — отказ ИНСТРУМЕНТА. И то и другое — `GitError`: «не смогли
    проверить» не имеет права выглядеть как «проверили, чисто» (silent-wrong)."""
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", main_ref, head_ref], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode == 0:
        return False, []
    if result.returncode == 1 and result.stdout.strip():
        files = sorted(set(_CONFLICT_MERGE_IN_RE.findall(result.stdout))
                       | set(_CONFLICT_PATH_RE.findall(result.stdout)))
        return True, files
    raise GitError(
        f"git merge-tree --write-tree {main_ref} {head_ref} упал "
        f"(rc={result.returncode}, stdout пуст: {not result.stdout.strip()}): "
        f"{result.stderr.strip()}")


def measure_functional_overlap(main_ref: str, head_ref: str, cwd: str | None = None) -> dict:
    """Величина 3 (см. шапку модуля) для одного PR: пересечение изменённых
    регионов между PR и main относительно общего merge-base, только в
    файлах, которые трогают ОБЕ стороны. AST def/class-регионы для `.py`
    (база парсится, файл существует на базе) — МИНИМАЛЬНЫЕ (см.
    `python_def_ranges`: регион, содержащий другие регионы, фантомил);
    строки .py ВНЕ регионов (модульный код) — тем же line-range правилом,
    режим такого файла `ast+line-range`; line-range fallback
    (`intersecting_line_runs`) — для остальных файлов и для .py, чья база
    не парсится (синт-ошибка) или отсутствует (файл добавлен обеими
    сторонами). Режим каждого файла отдаётся в `overlap_mode_by_file` —
    ноль из-за непосчитанного файла неотличим от нуля «пересечений нет»
    только если режим назван (находка ревью PR #1219). Справочный строковый
    ratio ADR считается по общим файлам (`line_drift_*`)."""
    base_sha = merge_base(main_ref, head_ref, cwd=cwd)
    pr_files = changed_files(base_sha, head_ref, cwd=cwd)
    main_files = changed_files(base_sha, main_ref, cwd=cwd)
    common = sorted(pr_files & main_files)
    overlap_by_file: dict[str, list[str]] = {}
    mode_by_file: dict[str, str] = {}
    total = 0
    main_drift_lines = 0
    pr_own_lines = 0
    for path in common:
        pr_diff = run_git("diff", "--unified=0", base_sha, head_ref, "--", path, cwd=cwd)
        main_diff = run_git("diff", "--unified=0", base_sha, main_ref, "--", path, cwd=cwd)
        pr_touched = parse_base_touched_lines(pr_diff)
        main_touched = parse_base_touched_lines(main_diff)
        main_drift_lines += count_changed_lines(main_diff)
        pr_own_lines += count_changed_lines(pr_diff)

        ranges: dict[str, tuple[int, int]] = {}
        if path.endswith(".py"):
            base_source = show_file(base_sha, path, cwd=cwd)
            if base_source is not None:
                ranges = python_def_ranges(base_source)
        if ranges:
            overlap = touched_regions(ranges, pr_touched) & touched_regions(ranges, main_touched)
            regions = sorted(overlap)
            mode = "ast"
            # Строки ВНЕ минимальных регионов (модульные константы, реестры
            # уровня модуля, атрибуты класса вне методов) считаются тем же
            # line-range правилом — некритичная находка ревью PR #1219:
            # в чистом ast-режиме они были невидимы, и ноль «не считали»
            # был неотличим от нуля «пересечений нет».
            covered = {line for start, end in ranges.values()
                       for line in range(start, end + 1)}
            module_runs = intersecting_line_runs(pr_touched - covered,
                                                 main_touched - covered)
            if module_runs:
                regions = regions + module_runs
                mode = "ast+line-range"
        else:
            mode, regions = "line-range", intersecting_line_runs(pr_touched, main_touched)
        if regions:
            overlap_by_file[path] = regions
            mode_by_file[path] = mode
            total += len(regions)
    return {"merge_base": base_sha, "common_files": common,
            "overlap_mode_by_file": mode_by_file,
            "overlap_count": total, "overlap_detail": overlap_by_file,
            "line_drift_main_lines": main_drift_lines,
            "line_drift_pr_lines": pr_own_lines,
            "line_drift_ratio": line_drift_ratio(main_drift_lines, pr_own_lines)}


def measure_invariant_collision(main_ref: str, head_ref: str, cwd: str | None = None) -> dict:
    """Величина 7 (инвариантная половина): номера реестра, добавленные
    КАЖДОЙ стороной относительно общего merge-base. Формат записи парсится
    `invariant_numbering.parse_registry_entries` (#904) — вторая копия
    парсера не заводится; непустой файл без распознанных записей реестра —
    громкий отказ через `_parse_full_registry_or_die` того же модуля
    (слепота парсера к дрейфу формата не имеет права выглядеть как «номер
    не занят»), а не пустое множество."""
    base_sha = merge_base(main_ref, head_ref, cwd=cwd)

    def registry_numbers(ref: str) -> set[int]:
        """Номера реестра на `ref`; файла нет на этом ref — легитимное
        «ничего» (файл создан позже/удалён стороной)."""
        content = show_file(ref, INVARIANT_FILE, cwd=cwd)
        if content is None:
            return set()
        return {int(number)
                for number in invariant_numbering._parse_full_registry_or_die(ref, content)}

    base_numbers = registry_numbers(base_sha)
    added_by_pr = registry_numbers(head_ref) - base_numbers
    added_by_main = registry_numbers(main_ref) - base_numbers
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
        "velichina3_overlap_modes": overlap["overlap_mode_by_file"],
        "velichina3_detail": overlap["overlap_detail"],
        "velichina3_line_drift_ratio_reference": overlap["line_drift_ratio"],
        "velichina7_invariant_collisions": invariant["collisions"],
    }


# ── Величина 8: PR-дубль уже слитой работы (issue #1236) ────────────────────

def measure_already_in_main(main_ref: str, head_ref: str, files: list[dict],
                            cwd: str | None = None) -> check_result.CheckResult:
    """Величина 8 (см. шапку модуля, живой случай PR #1020): все файлы,
    тронутые PR, побайтно (`blob_sha`) равны main → PR ничего не меняет.
    `files` — прод-форма `gh api pulls/{n}/files` (список словарей с ключами
    `filename`/`status`; статус — `added`/`removed`/`modified`/`renamed`,
    requirement 3 issue #1236 учитывается по нему):

      `removed` — main ЕЩЁ несёт этот путь → PR удаляет то, чего main не
        удалял, это НАСТОЯЩАЯ правка → немедленный `ok()` (доказательство
        «PR не дубль» найдено, дальше можно не читать). Main тоже НЕ несёт
        путь → согласие сторон («уже отсутствует и там, и там») — само по
        себе НЕ довод ни за, ни против, файл пропускается, решают остальные.
      `added`/`modified`/`renamed` (сравнение по НОВОМУ пути, `filename`) —
        blob на `head_ref` сравнивается с blob на `main_ref`. Main НЕ несёт
        такой путь вовсе (файл существует только у PR) — тоже настоящая
        правка → немедленный `ok()`. Blob'ы расходятся — тоже `ok()`.

    Ранний выход на ПЕРВОМ же файле с доказанной настоящей правкой — цена
    величины 8 намеренно минимальна (requirement 1: вычисляется ДО дорогой
    величины 3, обесценивает её), не собирает полный список различий.

    Три исхода (`check_result`, #1096): `ok()` — найдена хотя бы одна
    настоящая правка, PR не дубль; `violation([имена файлов])` — ВСЕ
    тронутые файлы совпали с main, PR — мёртвый груз (live-case #1020);
    `unknown(reason)` — ни одного расхождения не нашлось, но хотя бы один
    blob не читается (`head_ref` недоступен на момент сравнения, аномалия
    прод-формы) — «дубль» и «не дубль» здесь одинаково неверны."""
    if not files:
        return check_result.unknown(
            "величина 8: PR не несёт ни одного файла в pulls/{n}/files — "
            "список пуст, сравнение не имеет смысла")
    unresolved: list[str] = []
    for entry in files:
        path = entry.get("filename")
        status = entry.get("status")
        if status == "removed":
            if blob_sha(main_ref, path, cwd=cwd) is not None:
                return check_result.ok()
            continue
        head_blob = blob_sha(head_ref, path, cwd=cwd)
        if head_blob is None:
            unresolved.append(
                f"{path}: git rev-parse {head_ref}:{path} не читается (status={status})")
            continue
        if blob_sha(main_ref, path, cwd=cwd) != head_blob:
            return check_result.ok()
    if unresolved:
        return check_result.unknown("; ".join(unresolved))
    return check_result.violation(sorted(entry.get("filename") for entry in files))


def already_in_main_check(repo: str, number: int, main_ref: str, head_ref: str,
                          cwd: str | None = None) -> check_result.CheckResult:
    """Обёртка величины 8 для одного PR очереди: список тронутых файлов —
    `review_labels.list_pr_files` (прод-форма `gh api pulls/{n}/files`, обход
    страниц уже встроен туда — класс #308/#309, второй копии обхода здесь
    не заводим), сравнение — `measure_already_in_main` (git-only). Сетевой
    отказ gh — `unknown()`, не падение всего обхода очереди: один кривой PR
    из-за отказа сети на ЭТОМ конкретном запросе не обязан валить `cmd_queue`
    целиком через общий except (requirement 2 issue #1236 — причина обязана
    дойти до строки очереди, не потеряться)."""
    try:
        files = review_labels.list_pr_files(repo, number, gh)
    except RuntimeError as error:
        return check_result.unknown(f"величина 8: gh api pulls/{number}/files отказал: {error}")
    return measure_already_in_main(main_ref, head_ref, files, cwd=cwd)


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


def decision_doc_collision_pr_numbers(
        repo: str, cwd: str | None = None) -> tuple[set[int], str | None]:
    """`(номера PR, замешанных в коллизию docs/decisions|docs/research,
    причина_неизвестности)` — величина 7, вторая половина (см. шапку
    модуля): переиспользует `decision_numbering.
    check_decision_doc_number_collisions` целиком, не второй копией того же
    обхода git+gh (#1078 уже закрыл этот класс для этого носителя). Второй
    элемент кортежа различает два исхода с пустым множеством (находка
    ревью PR #1219: unknown схлопывался в «коллизий нет» в строке очереди):
    `None` — проверка СОСТОЯЛАСЬ и коллизий с PR-источниками нет;
    непустая причина — проверка НЕ СОСТОЯЛАСЬ (`unknown()`: сеть/git
    отказали внутри), «не нашли» не имеет права выглядеть как «проверили».
    `cmd_queue` несёт причину в строке очереди (`velichina7_decision_doc_
    unknown`) рядом с решением, не выдавая её за чистый вердикт."""
    result = decision_numbering.check_decision_doc_number_collisions(repo, cwd=cwd)
    if result.status == check_result.STATUS_UNKNOWN:
        return set(), result.reason
    numbers: set[int] = set()
    if result.status == check_result.STATUS_VIOLATION:
        for found in result.violations:
            for occ in found["occurrences"]:
                for source in occ["sources"]:
                    match = _PR_SOURCE_RE.match(source)
                    if match:
                        numbers.add(int(match.group(1)))
    return numbers, None


def cmd_queue(repo: str, cwd: str | None = None) -> list[dict]:
    """Величины 1,2,3,4,6,7,8 + решение для всех открытых PR репозитория.
    Величина 8 (issue #1236) считается ПЕРВОЙ на каждый PR — обнаруженный
    дубль (`already_in_main_check`) обесценивает дорогую величину 3
    (AST-пересечение) настолько, что она вовсе не вызывается для этого PR
    (см. цикл ниже и шапку модуля).
    Сеть: одна страница `pulls` + один GraphQL-батч задач (gh) + один прогон
    decision_numbering (свой отдельный обход, см. его докстринг) + фетч
    head'а КАЖДОГО PR (`refs/pull/N/head`, находка ревью PR #1219: это
    сетевые запросы, «без сетевой цены на PR» — неправда; правда — «на PR
    нет дорогих gh-вызовов: контрактов/комментариев/состояний, дальше
    только git-объекты»). Refspec с `+`: назначение `origin/pr-N`
    приватное для этого прогона, а force-push в чужой открытый PR отклонил
    бы refspec как non-fast-forward и уронил весь обход очереди. Дальше —
    только локальный git (merge-tree/diff/merge-base) на каждый PR. Строка
    PR, чьё измерение упало (битый head, несводимые с main истории, отказ
    merge-tree), несёт `error` и НЕ несёт `action`: один кривой PR не валит
    весь прогон (находка ревью PR #1219), но и не выглядит решённым.

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
        task_number = task_ref.resolve_pr_task(pull)
        if task_number is None:
            task_state = "none"
        else:
            # «Ответа нет» — НЕ «открыта» (находка ревью PR #1219): issue
            # удалена/несуществует — «не знаю» остаётся «не знаю».
            task_state = states.get(task_number, "unknown")
        row = {"number": number, "title": pull.get("title", ""), "task": task_number,
               "task_state": task_state,
               "ai_verdict": ai_verdict_of(pull.get("labels", []))}
        try:
            head_ref = f"origin/pr-{number}"
            run_git("fetch", "--quiet", "origin",
                    f"+refs/pull/{number}/head:refs/remotes/origin/pr-{number}", cwd=cwd)
            already = already_in_main_check(repo, number, "origin/main", head_ref, cwd=cwd)
            row["velichina8_already_in_main"] = already.status == check_result.STATUS_VIOLATION
            row["velichina8_already_in_main_files"] = (
                already.violations if already.status == check_result.STATUS_VIOLATION else [])
            row["velichina8_already_in_main_unknown"] = (
                already.reason if already.status == check_result.STATUS_UNKNOWN else None)
            if row["velichina8_already_in_main"]:
                # Дорогая величина 3 (AST-пересечение) не только не нужна,
                # но и ОБМАНЫВАЕТ на чистом дубле (живая улика PR #1020: она
                # сравнивала PR с его же копией в main и врала «8
                # пересечений») — не считаем вовсе, см. шапку модуля.
                measured = {}
            else:
                measured = measure_pr("origin/main", head_ref, cwd=cwd)
        except RuntimeError as error:  # GitError — подкласс RuntimeError
            # Один кривой PR (несуществующий head, несводимые с main истории,
            # отказ merge-tree, дрейф формата реестра) не валит весь обход
            # очереди — находка ревью PR #1219; но и не растворяется молча:
            # строка несёт `error`, решения (`action`) у неё НЕТ — «не
            # посчитано» не выдаётся за вердикт (fail loud строкой, не прогоном).
            row["error"] = str(error)
            partial.append(row)
            continue
        partial.append({**row, **measured})

    # Последним — иначе реселлит .git/shallow для всех ссылок, см. докстринг выше.
    doc_collisions, doc_unknown = decision_doc_collision_pr_numbers(repo, cwd=cwd)
    ensure_unshallow(cwd=cwd)

    rows = []
    for row in partial:
        if "error" in row:
            rows.append(row)
            continue
        # Дубль (величина 8) не несёт величин 1,3,7 вовсе (не считали, см.
        # цикл выше) — decide() решает по already_in_main раньше, чем
        # прочитает их, но входы обязаны существовать синтаксически;
        # .get(..., дешёвый нейтральный дефолт) безопасен именно потому,
        # что already_in_main=True делает их недостижимыми внутри decide().
        decision = decide(
            conflicting=row.get("velichina1_conflicting", False),
            functional_overlap_count=row.get("velichina3_functional_overlap", 0),
            task_state=row["task_state"],
            ai_verdict=row["ai_verdict"],
            invariant_collision=bool(row.get("velichina7_invariant_collisions", [])),
            decision_doc_collision=row["number"] in doc_collisions,
            already_in_main=row.get("velichina8_already_in_main", False),
        )
        rows.append({
            **row,
            "velichina7_decision_doc_collision": row["number"] in doc_collisions,
            # None — проверка состоялась; причина — НЕ состоялась («коллизий
            # нет» из несостоявшейся проверки не выдаётся, находка ревью
            # PR #1219).
            "velichina7_decision_doc_unknown": doc_unknown,
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
        run_git("fetch", "--quiet", "origin",
                f"+refs/pull/{number}/head:refs/remotes/origin/pr-{number}")
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
