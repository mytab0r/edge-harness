#!/usr/bin/env python3
"""Транслятор рукописного шага регистрации гвардии в файл каталога
`scripts/ci/guards/` (issue #897, продолжение #749/#771/#762/#764).

Зачем: PR #771 перевёл регистрацию CI-гвардий из рукописных шагов
`.github/workflows/repo-ci.yml` в каталог `scripts/ci/guards/*.sh` —
`scripts/lib/ci_guard_registration_guard.py` красит PR, добавляющий НОВЫЙ
рукописный шаг вне `ALLOWLIST`. Живой замер issue #897 (ручная миграция PR
#890/#895): механический ребейз (`scripts/orchestra/mechanical_rebase.py`)
сводит такие PR текстуально БЕЗ конфликта — `git rebase` применяет патч
контекстно и про новую схему каталога не знает, поэтому рукописный шаг
остаётся на месте. `ci_guard_registration_guard` после ребейза красит CI
этого же PR: устранённый источник git-конфликтов породил очередь ручной
работы (замер на дату issue #897: 14 открытых PR, 20 таких шагов).

Что делает `translate_repo_ci(repo_root)`:
  1. Считает `added = guard_step_names(repo_ci) - allowlist` — ТО ЖЕ
     множество, которое `ci_guard_registration_guard.py` уже красит как
     находку класса #749 (одно место правды для «что считается новым
     рукописным шагом гвардии», вторую копию критерия не заводим).
  2. Для КАЖДОГО имени этого множества разбирает шаг: находит его как
     запись `jobs.test.steps` (`yaml.safe_load`, для проверки полей) И как
     диапазон строк исходного текста (для точного удаления без порчи
     форматирования соседних шагов). Поддерживаются только ключи
     `name`/`run`/`working-directory` — любой другой ключ (`env`/`if`/`id`/
     `uses`/`with`/…) останавливает перенос ВСЕГО набора этого вызова с
     точным сообщением, какой ключ и в каком шаге не разобран
     (`UnsupportedStepError` — «не пропускать молча», AGENTS.md «Fail
     loud»). `run:` с выражением GitHub Actions `${{ … }}` тоже не
     разбирается (находка ревью PR #902): выражение вычисляет Actions при
     прогоне workflow, в файле каталога оно осталось бы дословным текстом,
     который shell не вычислит. Перенос атомарный по вызову: либо
     переносятся ВСЕ найденные шаги, либо (при первом же неразборе) НИ
     ОДИН файл не меняется — проще и безопаснее частичного отката на
     середине записи.
  3. Имя файла каталога определяется по файлу, который РЕАЛЬНО исполняет
     `run:` (тот же разбор `_extract_run_targets`, что уже доказал себя в
     `ci_guard_registration_guard.py` для сверки каталог↔рукописный шаг —
     одно место правды и для «это гвардия», и для «как её назвать»):
     первый по алфавиту исполняемый путь (детерминированный выбор при
     нескольких целях), базовое имя без `test_`/`.test`/`.smoke`/`.guard`/
     расширения, `_` → `-`, суффикс `-guard.sh` (не дублируется, если база
     уже кончается на `guard`). Ни одного целевого файла не нашлось
     (инлайн-проверка вида `grep`/`echo` без вызова pytest/node --test/
     guard-файла) — `UnsupportedStepError`: транслятор не умеет придумывать
     имя из русскоязычного текста шага без участия модели (условие задачи
     — «детерминированно, без модели»), это ЧЕСТНАЯ граница метода, не
     недосмотр (живой пример — шаг #241 «Тесты DO журнала»: `npm ci`/`npx
     vitest run`, ни один из паттернов `_extract_run_targets` их не ловит).
     Цель при этом ЛЕЖИТ в самом каталоге `scripts/ci/guards/` (`run:`
     вызывает уже существующий файл каталога — класс обхода (б) из #771,
     «Проверка окружения» → `bash scripts/ci/guards/ci-guard-registration.sh`;
     живые стемы каталога не всегда кончаются на `-guard`) — тоже громкий
     `UnsupportedStepError`, а не перенос (находка ревью PR #902, третий
     круг): гвардия УЖЕ зарегистрирована файлом каталога, переносить
     нечего; молча созданная обёртка `<имя>-guard.sh` с телом `bash
     scripts/ci/guards/<файл>.sh` исполняла бы гвардию ДВАЖДЫ, и мутация-
     критерий #749 («удали файл каталога → должно покраснеть») молча не
     срабатывала бы — ровно тот класс, который `check_catalog_handwritten_
     overlap` не видит (цель обёртки — путь каталога, пересечения с
     рукописными шагами нет), а исходный шаг из repo-ci.yml уже удалён.
     CONTENT-форма того же класса (четвёртый круг ревью PR #902): цель
     шага не лежит в каталоге и производный стем не совпадает, но её УЖЕ
     исполняет существующий файл каталога (`run: python
     scripts/lib/ci_guard_registration_guard.py` против каталога с
     `ci-guard-registration.sh`) — сверка целей шага с `_catalog_targets`
     (один замер на вызов, одно место правды с гвардией #771) даёт тот же
     громкий отказ: иначе перенос заводил бы ВТОРОЙ файл для той же
     гвардии, и после удаления рукописного шага ни одна сверка этого не
     видела бы (`check_catalog_handwritten_overlap` — пересечения нет,
     `_catalog_targets` со своим setdefault затеняет дубль).
  4. Имя файла уже занято (существующий файл каталога ИЛИ другой шаг того
     же вызова) — `UnsupportedStepError` (коллизия, перенос вручную).
  5. Пишет `scripts/ci/guards/<имя>.sh` (шебанг, `set -euo pipefail`,
     опциональный `cd` при `working-directory:`, дословное тело `run:`,
     исходный комментарий шага дословно, если был) — БЕЗ бита исполнения:
     весь каталог `scripts/ci/guards/*.sh` несёт режим `100644` (проверено
     `git ls-tree` и на дату issue #897, и на дату ревью PR #902; каталог
     растёт, фиксирован режим, а не число файлов — вторую копию замера
     рядом не заводим, некритичное замечание ревью PR #902),
     `scripts/ci/run_guards.sh` зовёт
     каждый файл ЧЕРЕЗ интерпретатор (`bash "$script"`), не напрямую —
     бит исполнения этому механизму не нужен (не тот класс, что #510/#516:
     там падал ПРЯМОЙ вызов `run: scripts/foo.sh` без интерпретатора).
  6. Удаляет комментарий и сам шаг из текста `repo-ci.yml` (ALLOWLIST не
     трогает — новые шаги в неё никогда не попадали) и схлопывает пустую
     строку, задвоившуюся РОВНО на стыке удаления. Схлопывание идёт только
     в окрестности удалённых диапазонов, не по всему файлу (находка ревью
     PR #902, второй круг): глобальный проход перекрашивал бы чужие части
     файла — пустые строки внутри чужого `run: |` (heredoc с двумя пустыми
     строками) — легальное содержимое другого job'а, терять их молча нельзя.
  7. `_verify_removal` — самопроверка ПЕРЕД записью на диск (находка ревью
     PR #902): пустая строка внутри блока `run: |` (обычный стиль этого
     репозитория) обрывает диапазон удаления раньше конца шага, и хвост
     `run:`-блока молча приклеивается к `run:` СОСЕДНЕГО шага — итог
     синтаксически ВАЛИДНЫЙ YAML (многострочный скаляр), `yaml.safe_load`
     эту порчу не ловит. Проверка перепарсивает `new_text` и сверяет
     структурно: (а) ни одно перенесённое имя не осталось в файле — заодно
     закрывает дублирующееся имя шага (переносится только ПЕРВОЕ текстовое
     вхождение, второе осталось бы видно этой проверкой); (б) ВЕСЬ
     разобранный документ после удаления равен «старый документ минус
     перенесённые шаги» (deep equality, находка ревью PR #902, второй круг)
     — не только job `test`: пустая строка внутри чужого `run: |` — часть
     скаляра, deep equality ловит её потерю в ЛЮБОМ job'е, а не только там,
     куда смотрит структурная сверка шагов. Расхождение любого рода —
     `UnsupportedStepError` до единой
     записи файла, тот же принцип атомарности, что и остальной модуль.

Возвращает `TranslationResult(migrated=[...], changed_paths=[...])` —
`changed_paths` включает и переписанный `repo-ci.yml`, и все новые файлы
каталога, ровно то, что вызывающая сторона обязана закоммитить.

Тесты: python -m pytest scripts/lib/test_guard_step_translator.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import copy
import re
import shlex
import sys
from dataclasses import dataclass, field

import yaml

_HERE = Path(__file__).resolve().parent


def _load_sibling(name: str, filename: str):
    """Загрузка соседнего модуля scripts/lib по пути файла (в scripts/lib нет
    __init__.py, тот же приём, что ci_guard_registration_guard.py уже
    применяет для orphan_test_guard.py — второй копии импорта не заводим).
    Регистрация в sys.modules ДО exec_module: модули с `@dataclass` на
    отложенных аннотациях (`from __future__ import annotations`) на Python
    3.11 разрешают их через sys.modules[cls.__module__] — без регистрации
    сам импорт падает `AttributeError`, до кода вызывающей стороны."""
    spec = importlib.util.spec_from_file_location(name, _HERE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


_crg = _load_sibling("ci_guard_registration_guard", "ci_guard_registration_guard.py")


class UnsupportedStepError(RuntimeError):
    """Шаг гвардии в repo-ci.yml не поддаётся автопереносу в каталог — текст
    исключения называет ИМЕННО что не разобрано (AGENTS.md, «Алерт не
    гадает» + «Fail loud»), не просто факт отказа."""


@dataclass
class Migration:
    step_name: str
    guard_path: Path


@dataclass
class TranslationResult:
    migrated: list[Migration] = field(default_factory=list)
    changed_paths: list[Path] = field(default_factory=list)


@dataclass
class _StepPlan:
    name: str
    slug: str
    run_text: str
    working_directory: str | None
    comment_lines: list[str]
    line_range: tuple[int, int]
    raw_step: dict


_STEP_START_RE = re.compile(r"^(\s+)- name:(.*)$")

_TARGET_SUFFIXES = (".smoke.sh", ".guard.sh", ".test.sh", ".test.mjs", ".py", ".sh", ".mjs")


def _slug_from_target(target: str) -> str:
    base = Path(target).name
    for suffix in _TARGET_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.startswith("test_"):
        base = base[len("test_"):]
    base = base.replace("_", "-")
    if not base.endswith("guard"):
        base = f"{base}-guard"
    return base


def _find_step_dict(steps: list[dict], name: str) -> dict | None:
    for step in steps:
        if isinstance(step, dict) and step.get("name") == name:
            return step
    return None


def _find_step_line_range(lines: list[str], name: str) -> tuple[int, int, int] | None:
    """Возвращает (comment_start, step_start, step_end) — `comment_start`..
    `step_end` включительно подлежит удалению целиком (комментарий + сам
    шаг), `step_start` отдельно нужен для отсечения комментарной части при
    рендере файла каталога. `None`, если шаг с именем `name` не начинается
    СТРОГО со строки `- name: …` (см. докстринг модуля, п.2 — нестандартный
    порядок ключей не поддержан)."""
    for i, line in enumerate(lines):
        m = _STEP_START_RE.match(line)
        if not m:
            continue
        rest = m.group(2)
        try:
            decoded = yaml.safe_load(f"x:{rest}")
        except yaml.YAMLError:
            continue
        decoded_name = decoded.get("x") if isinstance(decoded, dict) else None
        if decoded_name != name:
            continue
        indent = len(m.group(1))
        end = i
        for j in range(i + 1, len(lines)):
            line_j = lines[j]
            if line_j.strip() == "":
                break
            cur_indent = len(line_j) - len(line_j.lstrip(" "))
            if cur_indent <= indent:
                break
            end = j
        start = i
        for k in range(i - 1, -1, -1):
            line_k = lines[k]
            stripped = line_k.strip()
            cur_indent = len(line_k) - len(line_k.lstrip(" "))
            if stripped.startswith("#") and cur_indent == indent:
                start = k
            else:
                break
        return start, i, end
    return None


def _comment_lines_from(lines: list[str], start: int, step_start: int) -> list[str]:
    """Комментарий шага дословно, только со снятым отступом (строки уже
    прошли проверку `stripped.startswith('#')` при поиске диапазона —
    `lstrip(' ')` восстанавливает ИМЕННО исходный текст комментария)."""
    return [lines[k].lstrip(" ") for k in range(start, step_start)]


def _remove_ranges(lines: list[str], ranges: list[tuple[int, int]]) -> list[str]:
    """Удаляет диапазоны строк (границы включительно) и схлопывает пустую
    строку, задвоившуюся РОВНО на стыке удаления (пустая строка
    непосредственно ПЕРЕД диапазоном при пустой непосредственно ПОСЛЕ).
    Схлопывание — только в окрестности удалённых диапазонов (находка ревью
    PR #902, второй круг): глобальный проход «убрать все задвоенные пустые
    строки по всему файлу» перекрашивал бы чужие части repo-ci.yml — пустые
    строки внутри чужого `run: |` (например, heredoc с двумя пустыми
    строками подряд в другом job'е) — легальное содержимое, которое ничья
    структурная сверка шагов job `test` не защищает; после этой правки
    содержимое файла ВНЕ окрестности удалённых диапазонов сохраняется байт
    в байт, а потерю где-либо ещё громко ловит `_verify_removal`."""
    to_skip: set[int] = set()
    for start, end in ranges:
        for i in range(start, end + 1):
            if i in to_skip:
                raise UnsupportedStepError(
                    "перекрывающиеся диапазоны переноса в repo-ci.yml — вероятно, "
                    "дублирующееся имя шага; перенеси конфликтующие шаги вручную"
                )
            to_skip.add(i)
    drop: set[int] = set()
    for start, end in sorted(ranges):
        before = start - 1
        while before >= 0 and before in to_skip:
            before -= 1
        if before < 0 or lines[before].strip() != "":
            continue
        after = end + 1
        while after < len(lines) and after in to_skip:
            after += 1
        if after < len(lines) and lines[after].strip() == "":
            drop.add(before)
    return [
        line for i, line in enumerate(lines) if i not in to_skip and i not in drop
    ]


def _step_signature(step: object) -> tuple:
    """Представление шага для структурного сравнения «до»/«после» —
    `sorted(items)` даёт устойчивый (не зависящий от порядка ключей в YAML)
    хеш содержимого, не только имени."""
    if not isinstance(step, dict):
        return ("<non-dict>", repr(step))
    return tuple(sorted((key, step.get(key)) for key in step))


def _expected_doc_after_removal(doc: dict, plans: list["_StepPlan"]) -> dict:
    """Ожидаемый разобранный документ ПОСЛЕ переноса: точная глубокая копия
    исходного, из `jobs.test.steps` которой удалено ровно по одному
    вхождению каждого перенесённого шага. Сравнивается с перепарсенным
    новым текстом в `_verify_removal` — deep equality ловит ЛЮБОЕ
    структурное расхождение в любом job'е (пустая строка внутри чужого
    `run: |` — часть скаляра, её потеря меняет строку и видна сравнению),
    не только шаги job `test` (находка ревью PR #902, второй круг)."""
    expected = copy.deepcopy(doc)
    test = (expected.get("jobs") or {}).get("test")
    if not isinstance(test, dict) or not isinstance(test.get("steps"), list):
        raise UnsupportedStepError(
            "самопроверка переноса: job `test` без списка steps в исходном "
            "repo-ci.yml — перенос отменён, перенеси шаги вручную"
        )
    steps = test["steps"]
    for plan in plans:
        signature = _step_signature(plan.raw_step)
        for index, step in enumerate(steps):
            if _step_signature(step) == signature:
                del steps[index]
                break
        else:
            raise UnsupportedStepError(
                f"самопроверка переноса: шаг {plan.name!r} не найден в "
                "исходном документе при построении ожидаемого состояния — "
                "перенос отменён, перенеси шаги вручную"
            )
    if not steps:
        # Перенесён ПОСЛЕДНИЙ шаг job `test` (синтетические деревья, живому
        # repo-ci.yml не бывает: там шагов десятки). Пустая блочная
        # последовательность в YAML-тексте — `steps:` без элементов —
        # парсится как None, а не как []; приводим ожидание к тому, что
        # РЕАЛЬНО выдаст разбор нового текста, иначе deep equality ниже
        # дал бы ложный отказ на честном переносе.
        test["steps"] = None
    return expected


def _verify_removal(new_text: str, doc: dict, plans: list["_StepPlan"]) -> None:
    """Самопроверка ПЕРЕД записью файлов (issue #897, находка ревью PR #902):
    `_find_step_line_range` режет диапазон удаления «до первой пустой строки
    или отступа ≤ шага» — пустая строка ВНУТРИ блока `run: |` (обычный стиль
    этого репозитория: 54 таких строк в самом repo-ci.yml на дату находки)
    обрывает диапазон раньше конца шага. Хвост `run:`-блока остаётся в
    файле и молча приклеивается к `run:` СОСЕДНЕГО шага, из-за чего тот
    начинает исполнять чужое тело — YAML при этом остаётся синтаксически
    ВАЛИДНЫМ (многострочный скаляр), `yaml.safe_load` эту порчу не ловит,
    воспроизведено мутацией живым текстом (см. test_guard_step_translator.py,
    `test_translate_repo_ci_raises_when_blank_line_inside_run_corrupts_neighbor`).
    Проверяем структурно, а не только «файл парсится»:
      1. ни одно из перенесённых имён шага не осталось в новом тексте —
         закрывает и порчу, и дублирующееся имя шага (`added` — множество,
         `_find_step_dict`/`_find_step_line_range` берут только ПЕРВОЕ
         текстовое вхождение — второй одноимённый шаг остаётся нетронутым и
         был бы виден как «имя всё ещё присутствует»; deep equality проверки
         (2) дубликат НЕ видит: в ожидаемом документе тоже остаётся ровно
         одно вхождение);
      2. ВЕСЬ новый документ равен «старый документ минус перенесённые
         шаги» (deep equality по `_expected_doc_after_removal`) — ловит
         любое изменение ЗА пределами перенесённых шагов, в любом job'е:
         и утечку содержимого между соседними шагами (проверка (1) её не
         видит — имя соседа не совпадает с перенесённым), и потерю пустых
         строк в чужом `run: |` другого job'а (находка ревью PR #902,
         второй круг: глобальное схлопывание пустых строк портило heredoc
         чужого шага мимо всякой сверки по job `test`).
    Расхождение любого рода — `UnsupportedStepError` ДО единой записи на
    диск: перенос не может опубликовать порченный repo-ci.yml (то же
    «атомарно по вызову», что и остальной модуль)."""
    try:
        new_doc = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise UnsupportedStepError(
            "самопроверка переноса: repo-ci.yml после удаления перенесённых "
            f"шагов не разбирается как YAML ({exc}) — перенос отменён, "
            "перенеси шаги вручную"
        ) from exc
    new_steps = ((new_doc.get("jobs") or {}).get("test") or {}).get("steps") or []

    migrated_names = {p.name for p in plans}
    remaining_names = {s.get("name") for s in new_steps if isinstance(s, dict)}
    leftover_names = sorted(migrated_names & remaining_names)
    if leftover_names:
        raise UnsupportedStepError(
            f"самопроверка переноса: имя шага {leftover_names} всё ещё "
            "присутствует в repo-ci.yml после удаления (дублирующееся имя "
            "шага или порча удаления) — перенос отменён, перенеси вручную"
        )

    if new_doc != _expected_doc_after_removal(doc, plans):
        raise UnsupportedStepError(
            "самопроверка переноса: repo-ci.yml после удаления не равен "
            "структурно «старый документ минус перенесённые шаги» — "
            "изменилось что-то за пределами перенесённых шагов (частая "
            "причина — пустая строка внутри run: блока перенесённого шага, "
            "порча соседнего шага или соседнего job) — перенос отменён, "
            "перенеси вручную"
        )


def _render_guard_file(plan: _StepPlan) -> str:
    out = [
        "#!/usr/bin/env bash",
        "# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных",
        "# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный",
        f"# шаг «{plan.name}» перенесён сюда автоматически, при механическом",
        "# ребейзе, без содержательной правки run: — исходный комментарий шага",
        "# (если был) приведён ниже дословно.",
    ]
    if plan.comment_lines:
        out.append("#")
        out.extend(plan.comment_lines)
    out.append("set -euo pipefail")
    if plan.working_directory:
        out.append(f"cd {shlex.quote(plan.working_directory)}")
    out.extend(plan.run_text.splitlines())
    return "\n".join(out) + "\n"


def translate_repo_ci(
    repo_root: Path,
    *,
    repo_ci: Path | None = None,
    catalog_dir: Path | None = None,
    allowlist: frozenset[str] | None = None,
) -> TranslationResult:
    """Атомарный перенос ВСЕХ новых рукописных шагов job `test` (не входящих
    в `allowlist`) в `catalog_dir`. Ничего не найдено — `TranslationResult`
    с пустыми списками, файлы не трогаются. Хотя бы один шаг не разобран —
    `UnsupportedStepError`, НИ ОДИН файл (включая уже успешно разобранные
    в этом же вызове) не меняется (см. докстринг модуля, п.2)."""
    repo_ci = repo_ci or (repo_root / ".github" / "workflows" / "repo-ci.yml")
    catalog_dir = catalog_dir or (repo_root / "scripts" / "ci" / "guards")
    allowlist = _crg.ALLOWLIST if allowlist is None else allowlist

    found = _crg.guard_step_names(repo_ci)
    added = sorted(found - allowlist)
    if not added:
        return TranslationResult()

    text = repo_ci.read_text(encoding="utf-8")
    lines = text.split("\n")
    doc = yaml.safe_load(text) or {}
    steps = ((doc.get("jobs") or {}).get("test") or {}).get("steps") or []

    reserved_slugs = {p.stem for p in catalog_dir.glob("*.sh")} if catalog_dir.is_dir() else set()
    catalog_targets: dict[str, str] | None = None
    plans: list[_StepPlan] = []

    for name in added:
        if name.startswith("(шаг без name:"):
            raise UnsupportedStepError(
                f"шаг без `name:` в job `test` ({name}) — транслятор ищет шаги "
                "по точному имени; добавь `name:` шагу и перезапусти, либо "
                "перенеси вручную в scripts/ci/guards/<имя>.sh"
            )
        step = _find_step_dict(steps, name)
        if step is None:
            raise UnsupportedStepError(
                f"шаг {name!r} не найден как запись job `test` при разборе "
                "YAML — перенеси вручную в scripts/ci/guards/<имя>.sh"
            )
        extra_keys = sorted(set(step) - {"name", "run", "working-directory"})
        if extra_keys:
            raise UnsupportedStepError(
                f"шаг {name!r} несёт неподдерживаемый ключ(и) {extra_keys} — "
                "транслятор переносит только name/run/working-directory, "
                "перенеси вручную в scripts/ci/guards/<имя>.sh"
            )
        run_text = step.get("run")
        if not isinstance(run_text, str):
            raise UnsupportedStepError(
                f"шаг {name!r}: `run:` не строка — перенеси вручную в "
                "scripts/ci/guards/<имя>.sh"
            )
        if "${{" in run_text:
            # Некритичное замечание ревью PR #902, поднятое до громкого
            # отказа: выражение `${{ … }}` вычисляет GitHub Actions при
            # прогоне workflow, в файле каталога оно осталось бы дословным
            # текстом (shell отдаёт «bad substitution») — причём исходный
            # шаг к тому моменту уже удалён из repo-ci.yml, чинить было бы
            # негде. Тот же принцип, что отказ на `env:`/`if:`: не молча
            # переносить форму, которую этот контекст не может исполнить.
            raise UnsupportedStepError(
                f"шаг {name!r}: run: содержит выражение GitHub Actions "
                "`${{ … }}` — его вычисляет Actions, а не shell, в файле "
                "каталога оно осталось бы дословным текстом; перенеси "
                "вручную: заведи scripts/ci/guards/<имя>.sh, заменив "
                "выражение env-переменной (env: шага → export в теле "
                "скрипта) или литералом, и удали рукописный шаг из "
                ".github/workflows/repo-ci.yml"
            )
        targets = _crg._extract_run_targets(run_text)
        if not targets:
            raise UnsupportedStepError(
                f"шаг {name!r}: run: не содержит распознаваемого исполняемого "
                "файла (pytest/node --test/bash|sh тестового или guard-файла) "
                "— транслятор не может детерминированно определить имя файла "
                "каталога без участия модели, перенеси вручную: заведи "
                "scripts/ci/guards/<имя>.sh с тем же телом run: и удали "
                "рукописный шаг из .github/workflows/repo-ci.yml"
            )
        catalog_invocations = sorted(
            t for t in targets if _crg._is_guard_catalog_invocation(t)
        )
        if catalog_invocations:
            # Находка ревью PR #902 (третий круг): run: вызывает УЖЕ
            # СУЩЕСТВУЮЩИЙ файл каталога (класс обхода (б) из #771 —
            # «Проверка окружения» → `bash scripts/ci/guards/<x>.sh`; стем
            # живого файла каталога не всегда кончается на `-guard`
            # (`ci-guard-registration.sh`), поэтому проверка коллизии ниже
            # этот класс молча пропускала). Перенос здесь завёл бы обёртку
            # `<имя>-guard.sh` с телом `bash scripts/ci/guards/<файл>.sh`:
            # гвардия исполнялась бы ДВАЖДЫ, `check_catalog_handwritten_
            # overlap` этого не видит (цель обёртки — путь каталога,
            # пересечения с рукописными шагами нет), а исходный шаг к тому
            # моменту уже удалён. Критерий «цель лежит в каталоге» — тот же
            # `_is_guard_catalog_invocation`, что красит такой шаг как
            # регистрацию (одно место правды, вторую копию не заводим).
            raise UnsupportedStepError(
                f"шаг {name!r}: run: вызывает файл(ы) каталога "
                f"{catalog_invocations} — гвардия уже зарегистрирована в "
                "scripts/ci/guards/, переносить нечего, а обёртка вокруг "
                "файла каталога исполняла бы гвардию дважды; просто удали "
                "рукописный шаг целиком из .github/workflows/repo-ci.yml"
            )
        if catalog_targets is None:
            # Однократный замер каталога на вызов translate_repo_ci (не на
            # каждый шаг): отображение «исполняемый файл → имя файла
            # каталога, его исполняющего», тот же `_catalog_targets`, что
            # сверяет каталог↔рукописные шаги в гвардии #771 (одно место
            # правды для «что уже исполняется каталогом»).
            catalog_targets = _crg._catalog_targets(catalog_dir)
        content_overlaps = sorted(
            (t, catalog_targets[t]) for t in sorted(targets) if t in catalog_targets
        )
        if content_overlaps:
            # Находка ревью PR #902 (четвёртый круг): CONTENT-форма того же
            # класса «гвардия уже зарегистрирована» — цель шага не лежит в
            # scripts/ci/guards/ и производный стем не совпадает ни с одним
            # файлом каталога, но эту цель УЖЕ исполняет существующий файл
            # каталога (живой случай: `run: python
            # scripts/lib/ci_guard_registration_guard.py` против каталога с
            # `ci-guard-registration.sh`). Перенос заводил бы ВТОРОЙ файл,
            # исполняющий ту же гвардию: рукописных шагов после удаления не
            # остаётся, `check_catalog_handwritten_overlap` не находит
            # пересечения, а `_catalog_targets` со своим setdefault даже
            # затеняет дубль — silent-wrong в тяжёлом состоянии объекта.
            raise UnsupportedStepError(
                f"шаг {name!r}: цель(и) {content_overlaps} уже исполняются "
                "существующим файлом каталога — гвардия уже зарегистрирована "
                "в scripts/ci/guards/, второй файл для той же гвардии "
                "заводить нельзя (исполнялась бы дважды); просто удали "
                "рукописный шаг целиком из .github/workflows/repo-ci.yml, "
                "каталог не трогай"
            )
        slug = _slug_from_target(sorted(targets)[0])
        if slug in reserved_slugs:
            raise UnsupportedStepError(
                f"шаг {name!r}: производное имя scripts/ci/guards/{slug}.sh "
                "уже занято — перенеси вручную под другим именем"
            )
        reserved_slugs.add(slug)
        located = _find_step_line_range(lines, name)
        if located is None:
            raise UnsupportedStepError(
                f"шаг {name!r}: не найдена начальная строка «- name: ...» в "
                "тексте repo-ci.yml (нестандартный порядок ключей шага?) — "
                "перенеси вручную в scripts/ci/guards/<имя>.sh"
            )
        start, step_start, end = located
        comment_lines = _comment_lines_from(lines, start, step_start)
        plans.append(_StepPlan(
            name=name,
            slug=slug,
            run_text=run_text,
            working_directory=step.get("working-directory"),
            comment_lines=comment_lines,
            line_range=(start, end),
            raw_step=step,
        ))

    delete_ranges = sorted((p.line_range for p in plans), key=lambda r: r[0])
    kept_lines = _remove_ranges(lines, delete_ranges)
    new_text = "\n".join(kept_lines)
    _verify_removal(new_text, doc, plans)

    catalog_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[Migration] = []
    for plan in plans:
        guard_path = catalog_dir / f"{plan.slug}.sh"
        guard_path.write_text(_render_guard_file(plan), encoding="utf-8", newline="\n")
        migrated.append(Migration(step_name=plan.name, guard_path=guard_path))

    repo_ci.write_text(new_text, encoding="utf-8", newline="\n")

    return TranslationResult(migrated=migrated, changed_paths=[repo_ci] + [m.guard_path for m in migrated])
