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
     loud»). Перенос атомарный по вызову: либо переносятся ВСЕ найденные
     шаги, либо (при первом же неразборе) НИ ОДИН файл не меняется —
     проще и безопаснее частичного отката на середине записи.
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
  4. Имя файла уже занято (существующий файл каталога ИЛИ другой шаг того
     же вызова) — `UnsupportedStepError` (коллизия, перенос вручную).
  5. Пишет `scripts/ci/guards/<имя>.sh` (шебанг, `set -euo pipefail`,
     опциональный `cd` при `working-directory:`, дословное тело `run:`,
     исходный комментарий шага дословно, если был) — БЕЗ бита исполнения:
     живой снимок каталога на дату issue #897 показывает все 14 файлов
     как режим `100644` (`git ls-tree`), `scripts/ci/run_guards.sh` зовёт
     каждый файл ЧЕРЕЗ интерпретатор (`bash "$script"`), не напрямую —
     бит исполнения этому механизму не нужен (не тот класс, что #510/#516:
     там падал ПРЯМОЙ вызов `run: scripts/foo.sh` без интерпретатора).
  6. Удаляет комментарий и сам шаг из текста `repo-ci.yml` (ALLOWLIST не
     трогает — новые шаги в неё никогда не попадали) и схлопывает
     образовавшиеся задвоенные пустые строки.

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
    to_skip: set[int] = set()
    for start, end in ranges:
        for i in range(start, end + 1):
            if i in to_skip:
                raise UnsupportedStepError(
                    "перекрывающиеся диапазоны переноса в repo-ci.yml — вероятно, "
                    "дублирующееся имя шага; перенеси конфликтующие шаги вручную"
                )
            to_skip.add(i)
    return [line for i, line in enumerate(lines) if i not in to_skip]


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    result: list[str] = []
    prev_blank = False
    for line in lines:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        result.append(line)
        prev_blank = blank
    return result


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
        ))

    delete_ranges = sorted((p.line_range for p in plans), key=lambda r: r[0])
    kept_lines = _collapse_blank_runs(_remove_ranges(lines, delete_ranges))
    new_text = "\n".join(kept_lines)

    catalog_dir.mkdir(parents=True, exist_ok=True)
    migrated: list[Migration] = []
    for plan in plans:
        guard_path = catalog_dir / f"{plan.slug}.sh"
        guard_path.write_text(_render_guard_file(plan), encoding="utf-8", newline="\n")
        migrated.append(Migration(step_name=plan.name, guard_path=guard_path))

    repo_ci.write_text(new_text, encoding="utf-8", newline="\n")

    return TranslationResult(migrated=migrated, changed_paths=[repo_ci] + [m.guard_path for m in migrated])
