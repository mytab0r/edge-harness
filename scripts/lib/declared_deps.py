#!/usr/bin/env python3
"""Обнаружение рассинхрона «тело задачи называет блокирующую issue — нативный
граф её не знает» (задача #371, продолжение #361/task_deps.py).

`find_desync` — только детектор для человека/CI, сам по себе НЕ источник
истины и НИЧЕГО не пишет в граф (о записи модулем в граф — `auto_wire`
ниже), эвристика по формулировкам прозы («после #N», «зависит от
#N», «блокируется #N», «заблокирован(а) #N», «блокер — #N», «до закрытия
#N»). Ложные срабатывания здесь ожидаемы (та же формулировка встречается не
только для реальной межзадачной зависимости — «после слияния» про порядок
шагов внутри одной задачи, не про #N) — именно поэтому ЭТА находка не
пишет связь автоматически (в отличие от структурного переноса —
`file_tasks.py::wire_declared_dependency` для строки «БЛОКИРУЕТСЯ: …» и
`auto_wire` ниже для поля формы, переносящих СТРУКТУРНЫЙ ответ, не любое
упоминание в прозе) и не проваливает обязательный CI-гейт —
только видимое предупреждение (`::warning::`), см. `.github/workflows/
repo-ci.yml`. Живой замер при внедрении (2026-09-06): более широкий захват
(окно `\\D{0,20}` без исключения `)`/`.`) давал ложный матч «Поглощает часть
#227 (закрытие задач после слияния) и часть #201» — триггер «после» из
несвязанной скобки цеплял #201 через закрывающую скобку. Окно сужено до
`[^#).\\n]{0,20}` (не пересекает конец скобки/предложения) — тот же прогон
после сужения даёт 0 таких ложных матчей на реальном пуле репозитория.

Ссылка на уже ЗАКРЫТУЮ/несуществующую/не-task issue — НЕ рассинхрон:
приоритет читает только ОТКРЫТЫХ блокирующих (`blocked_by_open`), отсутствие
связи на закрытую задачу не влияет ни на что (design.md task-priority-
blocking-graph, задача #371 report).

Отдельно — `auto_wire`/CLI `wire` (задача #529, продолжение #371): значение
СТРУКТУРНОГО поля формы «Чем блокируется» (`form_field_numbers`) — не
эвристика, ответ схемы issue-формы, поэтому переносится в граф
АВТОМАТИЧЕСКИ, тем же путём, что `file_tasks.py::wire_declared_dependency`
уже делает для строки «БЛОКИРУЕТСЯ:» (общий цикл —
`task_deps.wire_dependencies`). Прозаическая эвристика `_TRIGGER_RE` в
автопереносе НЕ участвует ни в каком виде — только в `find_desync`
(предупреждение, не запись).

Задача #710, продолжение #529: живой замер пула (198 открытых task-issues,
2026-09-08) нашёл, что `auto_wire` реально покрывал 2 из 24 тел с
заполненным полем связи (8%) — `_FORM_FIELD_RE` требовал РОВНО `###`, а живой
рендер поля даёт `##` в 9 из 11 тел (GitHub Issue Forms не гарантирует
уровень заголовка стабильным между полями/датами; тела, заведённые
`scripts/gh/issue-create`/CLI, тоже копируют структуру формы вручную, не
всегда с пустой строкой между заголовком и ответом). Три формы записи,
реально встречающиеся в пуле:

  1. Заголовок «Чем блокируется» (`##`/`###`, с пустой строкой между
     заголовком и ответом или без неё) — `form_field_numbers`/
     `declared_blocked_by`.
  2. Заголовок «Что блокирует» (тот же разброс уровня/пробела) — ОБРАТНОЕ
     направление: если A отвечает «Что блокирует: B», это то же самое, что
     B отвечает «Чем блокируется: A» — `blocking_field_numbers`, переносится
     `auto_wire` с разворотом направления.
  3. Инлайн «БЛОКИРУЕТСЯ: …» (последняя непустая строка тела, контракт
     `ai_review.blocked_by_numbers`) — раньше переносился только ОДИН РАЗ, в
     момент заведения (`file_tasks.py::wire_declared_dependency`); если
     названный номер на тот момент ещё не существовал (ссылка на задачу
     того же батча, ещё не созданную), связь терялась навсегда — ничего не
     досматривало её позже. `auto_wire` теперь пересматривает и эту форму
     на каждом push — `declared_blocked_by`.

Ад-хок заголовки со СВОБОДНОЙ формулировкой («## Блокирует», «##
Блокирующая зависимость», старая конвенция «## Зависимости» — все три
встречены в живом пуле по разу) НЕ становятся новым распознаваемым полем:
это не повторяемая схема (текст каждый раз свой), а прозопарсинг именно
такого — запрещённый класс (docstring `task_deps.py`); они остаются в зоне
`_TRIGGER_RE`/`find_desync` (предупреждение человеку), не автозаписи.

CLI:
    python scripts/lib/declared_deps.py check <owner/repo> [label]
    python scripts/lib/declared_deps.py wire <owner/repo> [label]
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import importlib.util
import json
import re
import sys
from pathlib import Path

_TRIGGER_RE = re.compile(
    r"(?:после|зависит\s+от|блокируется|заблокирован[а-я]*|блокер|до\s+закрытия)"
    r"[^#).\n]{0,20}#(\d+)",
    re.IGNORECASE,
)

# Заголовок обязательного поля формы «Чем блокируется»
# (.github/ISSUE_TEMPLATE/task.yml, white-spot.yml). ИСТОРИЯ (задача #710):
# докстринг раньше утверждал единственный канонический рендер
# `### <label>\n\n<ответ>\n` (design.md task-priority-blocking-graph) — живой
# замер пула 2026-09-08 (198 открытых task-issues) это опроверг: уровень
# заголовка держится и `##`, и `###` (9 против 2 живых тел), а пустая строка
# между заголовком и ответом то есть, то нет (issue, заведённые через
# `scripts/gh/issue-create --body`, копируют структуру формы вручную, без
# гарантии рендера GitHub Issue Forms). Регэксп поэтому НЕ фиксирует ни
# уровень (`#{1,6}`), ни число переводов строки (`\n+`, не `\n\s*\n`) —
# фиксирует только точный текст заголовка (регистр не важен), это по-прежнему
# ответ СХЕМЫ, не прозопарсинг: свободная проза «блокируется» без ЭТОГО
# заголовка полем не считается (см. `_TRIGGER_RE` выше — та же граница).
#
# ИСТОРИЯ (находка ревью PR #711, major): точный текст заголовка — не
# гарантия единственности матча. `.search()` берёт ПЕРВОЕ вхождение, а тот же
# точный текст заголовка встречается и в цитате примера формы — внутри
# fenced-блока (```…```) или HTML-комментария (<!-- … -->), которыми люди
# иногда поясняют, как заполнять поле. Без вырезания этих зон первым
# найденным оказывается пример, а настоящий ответ ниже молча игнорируется.
# Решение — `_strip_quoted_noise` (см. ниже) вырезает обе зоны ДО `.search()`,
# не «искать последнее вхождение вместо первого»: пример может стоять и
# ПОСЛЕ настоящего ответа, порядковая эвристика не закрыла бы класс целиком,
# а вырезание убирает источник шума напрямую (design.md, развилка (а),
# исправлена этим же PR).
_FORM_FIELD_RE = re.compile(
    r"^#{1,6}\s*Чем\s+блокируется\s*\n+([^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

# Заголовок обратного поля «Что блокирует» — тот же шаблон, тот же разброс
# уровня/пробела, ОБРАТНОЕ направление связи (задача #710): «A: Что
# блокирует — B» означает «B заблокирована A», не «A заблокирована B».
_BLOCKING_FIELD_RE = re.compile(
    r"^#{1,6}\s*Что\s+блокирует\s*\n+([^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

# Вырезаются ДО поиска заголовка поля (находка ревью PR #711) — нежадные
# (`.*?`), один блок/комментарий за раз, не «от первого открывающего до
# последнего закрывающего» через всё тело.
_FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Число в значении поля: с `#` (канонический рендер) ИЛИ голым (находка
# ревью PR #711, блокирующая): живой замер пула 2026-09-08 (233 открытых
# task-issue) — из четырёх заполненных ответов поля «Что блокирует» ТРИ были
# голым номером без `#` (#760→434, #759→735, #757→642), читался только один
# (#766→#750). Диапазон цифр 2-5 — граница живой нумерации issue этого
# репозитория (сейчас #7xx), не эвристика с потолка: она отсекает случайный
# одиночный разряд («1», «0») в неструктурированном тексте, оставаясь щедрой
# для любого реального номера ближайших лет. Мутация, воспроизведённая при
# разборе: снятие `#?` возвращает старый баг — `[]` вместо голого номера.
_VALUE_NUMBER_RE = re.compile(r"#?(\d{2,5})")


def _strip_quoted_noise(body: str) -> str:
    """Убирает fenced code-блоки и HTML-комментарии ДО разбора структурного
    поля (см. комментарий у `_FORM_FIELD_RE` выше — находка ревью PR #711)."""
    return _HTML_COMMENT_RE.sub("", _FENCED_BLOCK_RE.sub("", body))


class _UnrecognizedForm:
    """Сентинел «поле заполнено (непустой ответ, не «ничем»), но текст ответа
    не разбирается ни как список номеров, ни как явное «ничем»» — задача
    #710/#711, блокирующая находка ревью. Это ТРЕТИЙ факт, отличный и от
    `None` (секции нет вовсе в теле), и от `[]` (явный валидный ответ
    «ничем») — до этой правки оба факта («ничем» и «непонятно что»)
    схлопывались в `[]`, что запрещено правилом «Fail loud, не silent-wrong»
    (AGENTS.md). Голый номер (без `#`) больше НЕ приводит сюда — читается
    `_VALUE_NUMBER_RE`; сентинел остаётся для текста, где нет вообще ни
    одного числа в распознаваемой форме (на живом пуле 2026-09-08 таких
    заполненных ответов не встретилось — 0 из 4, но поле человекозаполняемое,
    случай возможен впредь)."""

    def __repr__(self) -> str:
        return "UNRECOGNIZED_FORM"


UNRECOGNIZED_FORM = _UnrecognizedForm()


def declared_candidates(body: str) -> set[int]:
    """Номера issue рядом с формулировкой зависимости — эвристика для
    предупреждения, не факт (см. docstring модуля). Структурные поля формы
    («Чем блокируется»/«Что блокирует») сюда НЕ входят с #529/#710: их
    переносит детерминированный `auto_wire`, и предупреждение на них было бы
    шумом о том, что тот же прогон чинит (см. комментарий у `_FORM_FIELD_RE`
    выше)."""
    return {int(n) for n in _TRIGGER_RE.findall(body or "")}


def _field_numbers(
    pattern: re.Pattern[str], body: str
) -> list[int] | None | _UnrecognizedForm:
    """Общий разбор одного структурного поля формы (заголовок + ответ первой
    непустой строкой) — единое место на семантику `None`/`[]`/список/
    `UNRECOGNIZED_FORM`, используется и «Чем блокируется»
    (`form_field_numbers`), и «Что блокирует» (`blocking_field_numbers`):
    разница между ними — только сам `pattern`, не смысл значений.

    `None` — секции нет вовсе в теле (issue не из этого шаблона, или заведена
    до введения обязательного поля) — автопереносу не из чего переносить,
    это НЕ то же самое, что «явно ничем» (см. ниже).
    `[]` — явный ответ «ничем» (или пустое значение) — зависимостей нет,
    ВАЛИДНЫЙ ответ обязательного поля, не повод для предупреждения/шума
    (задача #529, требование «поле обязательное — «ничем» тоже ответ»).
    `UNRECOGNIZED_FORM` — ответ НЕПУСТ и не «ничем», но не содержит ни одного
    числа в распознаваемой форме (см. `_VALUE_NUMBER_RE`/`_UnrecognizedForm`
    выше) — третий факт, не смешивается ни с одним из двух выше (задача
    #710/#711, блокирующая находка ревью: раньше молча становился `[]`).
    Иначе — список номеров issue, названных в ответе (с `#` или голым —
    `_VALUE_NUMBER_RE`)."""
    match = pattern.search(_strip_quoted_noise(body or ""))
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() == "ничем":
        return []
    numbers = [int(n) for n in _VALUE_NUMBER_RE.findall(value)]
    if not numbers:
        return UNRECOGNIZED_FORM
    return numbers


def form_field_numbers(body: str) -> list[int] | None | _UnrecognizedForm:
    """Структурный ответ обязательного поля «Чем блокируется» (issue-формы
    `task.yml`/`white-spot.yml`, обязательное с #387) — не эвристика прозы,
    ответ схемы формы (см. `_FORM_FIELD_RE`, семантика
    `None`/`[]`/список/`UNRECOGNIZED_FORM` — докстринг `_field_numbers`)."""
    return _field_numbers(_FORM_FIELD_RE, body)


def blocking_field_numbers(body: str) -> list[int] | None | _UnrecognizedForm:
    """Структурный ответ поля «Что блокирует» (задача #710) — то же поле
    формы, ОБРАТНОЕ направление связи (см. `_BLOCKING_FIELD_RE`); семантика
    `None`/`[]`/список/`UNRECOGNIZED_FORM` — та же, докстринг
    `_field_numbers`."""
    return _field_numbers(_BLOCKING_FIELD_RE, body)


_ai_review_module = None


def _load_ai_review():
    """Module-level memo (находка ревью PR #711): без кэша каждый вызов заново
    исполняет ai_review.py вместе с пятью подгружаемыми им модулями — замер
    ~1.6 мс на вызов, ~0.3 с на проход по пулу из ~200 задач, а
    `declared_blocked_by` зовёт эту функцию на КАЖДУЮ issue (первый проход
    `auto_wire` и инвариант 9 на каждый push/PR/пульс). Тот же приём, каким
    `_load_task_deps` уже вызывается один раз за прогон в `auto_wire`/`main`."""
    global _ai_review_module
    if _ai_review_module is None:
        spec = importlib.util.spec_from_file_location(
            "ai_review", Path(__file__).resolve().parents[1] / "review" / "ai_review.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _ai_review_module = module
    return _ai_review_module


def declared_blocked_by(body: str) -> list[int] | None | _UnrecognizedForm:
    """Объединяет ОБЕ формы записи направления «эта задача заблокирована
    N» (задача #710): структурное поле «Чем блокируется» (`form_field_numbers`)
    И инлайн-конвенция АИ-ревью «БЛОКИРУЕТСЯ: …» — последняя непустая строка
    тела (`ai_review.blocked_by_numbers`, тот же контракт, что уже проверяет
    `ai_review.parse_tasks`, не вторая копия регэкспа). На практике
    взаимоисключающие (человеческий/шаблонный путь рендерит заголовок,
    АИ-ревью-путь — инлайн-строку), но объединение защищает от случая, когда
    оба присутствуют одновременно.

    `None` — НИ ОДНА из форм не найдена (полю неоткуда взяться — issue не по
    этому контракту); `UNRECOGNIZED_FORM` — заголовочная форма заполнена, но
    не разбирается (см. `_field_numbers`) — распространяется вверх БЕЗ
    объединения с инлайн-частью: молча слить нераспознанный текст с валидным
    инлайн-ответом означало бы потерять сигнал «нужно вмешательство
    человека» (задача #711, fail loud); иначе — объединённый список (может
    быть пустым, если обе формы явно отвечают «ничем»)."""
    heading = form_field_numbers(body)
    if isinstance(heading, _UnrecognizedForm):
        return UNRECOGNIZED_FORM
    inline = _load_ai_review().blocked_by_numbers(body)
    if heading is None and inline is None:
        return None
    return sorted(set(heading or []) | set(inline or []))


def auto_wire(
    repo: str, label: str = "task", gh_call=None, log=print,
) -> list[dict]:
    """Единый механизм автопереноса ВСЕХ структурных форм записи связи в
    нативный `blockedBy` — задача #529 (заголовок «Чем блокируется»),
    продолжение #710 (заголовок «Что блокирует», обратное направление, и
    инлайн-конвенция АИ-ревью «БЛОКИРУЕТСЯ: …» — раньше переносилась только
    один раз, в момент заведения, теперь пересматривается на каждом push).
    Переиспользует `task_deps.wire_dependencies` (тот же цикл, что уже
    применяет `file_tasks.py::wire_declared_dependency`) — не третья копия
    «пропустить номер вне пула, иначе add_dependency».

    Идемпотентно: номера, уже присутствующие в `blocked_by_open` (или уже
    поставленные ЭТИМ ЖЕ прогоном — `linked_so_far` ниже учитывает и то, и
    другое), повторно не линкуются — ни одного лишнего сетевого вызова,
    безопасно гонять на каждый push. Номер вне открытого пула (закрыт/не
    задача/не существует) и ссылка на саму issue отфильтровываются ещё
    здесь, без предупреждения: этот путь — периодический, предупреждение на
    каждом push превратилось бы в постоянный шум, а связь на закрытую «не
    влияет ни на что» (см. docstring модуля; find_desync тот же случай
    молчит). Одноразовый путь заведения (АИ-ревью) получает предупреждение от
    самого `task_deps.wire_dependencies`.

    Возвращает список `{"issue": N, "linked": [...]}` только для issues, где
    реально что-то дописано в граф на ЭТОМ прогоне."""
    task_deps = _load_task_deps()
    gh_call = gh_call or task_deps._default_gh
    issues = task_deps.fetch_pool(repo, label=label, include_body=True, gh_call=gh_call)
    open_numbers = {issue["number"] for issue in issues}
    # Снимок «уже в графе» на старте прогона, обновляется по ходу — вторая
    # заявка на ТУ ЖЕ пару (например, A объявляет «Чем блокируется: B» И
    # отдельно B объявляет «Что блокирует: A» — избыточно, но не запрещено)
    # не должна давать второй сетевой вызов в ОДНОМ прогоне.
    linked_so_far: dict[int, set[int]] = {
        issue["number"]: set(issue.get("blocked_by_open") or []) for issue in issues
    }
    report: dict[int, list[int]] = {}

    def _wire_one(blocked_number: int, blocking_numbers: list[int]) -> None:
        if blocked_number not in open_numbers or not blocking_numbers:
            return
        already = linked_so_far.setdefault(blocked_number, set())
        missing = [n for n in blocking_numbers
                   if n not in already and n in open_numbers and n != blocked_number]
        if not missing:
            return
        linked = task_deps.wire_dependencies(
            repo, blocked_number, missing, open_numbers, gh_call=gh_call, log=log)
        if linked:
            already.update(linked)
            bucket = report.setdefault(blocked_number, [])
            for n in linked:
                if n not in bucket:
                    bucket.append(n)

    for issue in issues:
        declared = declared_blocked_by(issue.get("body") or "")
        if isinstance(declared, _UnrecognizedForm):
            # Fail loud (задача #711): не молчать и не гадать номер — видимое
            # предупреждение вместо тихого "перенести нечего".
            log(
                f"::warning::declared_deps: #{issue['number']} поле «Чем "
                "блокируется»/«БЛОКИРУЕТСЯ:» заполнено, но текст не "
                "разбирается ни как список номеров, ни как «ничем» — "
                "перенос пропущен, поправь формулировку вручную"
            )
            continue
        _wire_one(issue["number"], declared or [])

    # Обратное направление («Что блокирует») — иду ВТОРЫМ проходом, чтобы
    # первый (собственное поле каждой issue) уже отразился в linked_so_far и
    # не порождал дублирующий вызов на ту же пару, объявленную с двух сторон.
    for issue in issues:
        declaring_number = issue["number"]
        blocking = blocking_field_numbers(issue.get("body") or "")
        if isinstance(blocking, _UnrecognizedForm):
            log(
                f"::warning::declared_deps: #{declaring_number} поле «Что "
                "блокирует» заполнено, но текст не разбирается ни как "
                "список номеров, ни как «ничем» — перенос пропущен, "
                "поправь формулировку вручную"
            )
            continue
        for target in blocking or []:
            _wire_one(target, [declaring_number])

    return [{"issue": number, "linked": linked} for number, linked in report.items()]


def find_desync(issues: list[dict]) -> list[dict]:
    """`issues` — форма `task_deps.fetch_pool(..., include_body=True)`:
    ключи `number`, `body`, `blocked_by_open`. Возвращает находки
    `{"issue": N, "declared_blocking": M}` — тело N называет M формулировкой
    зависимости, M сам открыт и есть в пуле, но нативного `blockedBy` на M
    у issue N нет. Пусто — рассинхрона не обнаружено (легитимный, ожидаемый
    исход большую часть времени: тело либо совпадает с графом, либо ссылка
    на что-то, что не является межзадачной зависимостью вовсе)."""
    open_numbers = {issue["number"] for issue in issues}
    findings: list[dict] = []
    for issue in issues:
        number = issue["number"]
        declared = declared_candidates(issue.get("body") or "")
        native = set(issue.get("blocked_by_open") or [])
        missing = {n for n in declared if n in open_numbers and n != number} - native
        for n in sorted(missing):
            findings.append({"issue": number, "declared_blocking": n})
    return findings


def _load_task_deps():
    spec = importlib.util.spec_from_file_location(
        "task_deps", Path(__file__).resolve().with_name("task_deps.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3) and argv[0] == "check":
        task_deps = _load_task_deps()
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        issues = task_deps.fetch_pool(repo, label=label, include_body=True)
        findings = find_desync(issues)
        if not findings:
            print("declared_deps: рассинхрона не найдено")
            return 0
        for finding in findings:
            print(
                f"::warning::declared_deps: #{finding['issue']} называет в теле "
                f"формулировкой зависимости открытую #{finding['declared_blocking']}, "
                f"но нативного blockedBy на неё нет — проверь и при подтверждении "
                f"поставь `python scripts/lib/task_deps.py block {repo} "
                f"{finding['issue']} {finding['declared_blocking']}`"
            )
        return 0
    if len(argv) in (2, 3) and argv[0] == "wire":
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        report = auto_wire(repo, label)
        if not report:
            print("declared_deps: переносить нечего (поле пусто/«ничем»/уже в графе)")
            return 0
        for item in report:
            linked = " ".join(f"#{n}" for n in item["linked"])
            print(f"declared_deps: #{item['issue']} -> {linked} "
                  "(поле «Чем блокируется»/«Что блокирует» или БЛОКИРУЕТСЯ:)")
        return 0
    print(
        "использование: declared_deps.py check <owner/repo> [label] "
        "| wire <owner/repo> [label]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
