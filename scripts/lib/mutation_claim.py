#!/usr/bin/env python3
"""Заявления агента о собственной работе — машинная проверка (#968).

Дыра, которую это закрывает: код проходит второй гейт (живое AI-ревью), а
СЛОВА автора о своей работе не проверяет никто. Три живых случая:

  1. PR #893: заявлено «доказательство мутацией», а гвардия
     (`scripts/lib/test_worktree_cleanup_guard.py`, коммит 508e6899, ДО
     переделки — см. `fixtures_pr893_worktree_cleanup.py.txt` рядом,
     дословная копия) проверяла НАЛИЧИЕ ИМЁН функций (`check_dirty`,
     `check_unpushed_commits`) в исходнике: вырезание тела проверки при
     сохранённом имени функции красит тест ложно-зелёным.
  2. Задача #905/PR #906: «выводы перенесены в репозиторий» — фактически
     три из шести остались в комментариях/PR, где следующий агент их не
     прочитает.
  3. PR #964: «живая регрессия» заявлена и заведена задачей — на чистой
     копии main тест зелёный, регрессии не было.

Что проверяемо машиной (см. AGENTS.md «Доказывай доказыватель» — ненулевой
exit ≠ гвардия покраснела):

  - «Мутация доказана» (главное, единственное полностью механическое
    заявление) — `parse_mutation_claims` + `run_mutation_proof` ниже.
    Автор обязан объявить, ЧТО снять (unified diff, git-apply-совместимый)
    и КАКОЙ тест обязан покраснеть (`python -m pytest <target> -q`,
    единственная разрешённая форма — список argv без shell=True, чтобы
    текст PR не превращался в произвольную команду, см. `TEST_CMD_RE`).
    Механизм: тест зелёный ДО мутации → патч применяется → тест обязан
    покраснеть → патч откатывается → тест снова зелёный. Если тест НЕ
    краснеет под мутацией — заявление ложно (ровно случай #893).
  - «Класс закрыт» (частично проверяемо, если автор называет признак —
    `git grep`-паттерн и ожидаемое число совпадений) — `parse_class_closed_
    claims` + `run_class_closed_check`.
  - «Тесты зелёные, N passed» — НЕ дублируется здесь: это уже проверяет
    обязательный job `test` (repo-ci.yml), прогоняющий pytest каждого
    изменённого файла отдельным шагом/гвардией каталога — вторая копия той
    же проверки была бы рецидивом «одно место правды».
  - Остальное («прогнал на реальной истории», «проверил живьём» и т.п.) —
    НЕ проверяемо машиной без второго дорогого источника (эквивалент живого
    воспроизведения инцидента). Единственное дешёвое, что можно потребовать
    — явная пометка в теле PR (`UNVERIFIED_HEADING`), чтобы читатель не
    принял прозу за доказательство. `find_unverifiable_mentions` +
    `check_unverified_disclosure` реализуют это.

Формат объявления — по образцу уже существующего в репозитории машиночитаемого
блока «## Варианты владельца» (docs/agents/PROTOCOL.md): заголовок `## `
ровно этими словами на своей строке, блок кончается на следующем `## `
заголовке или конце тела. Дёшево для автора (пишется руками в тело PR, тот
же PR, где уже есть чеклист ревью — не отдельный файл, не отдельный API-вызов),
однозначно для машины (фиксированный заголовок + фиксированные метки полей,
не проза).

Запуск: python -m pytest scripts/lib/test_mutation_claim.py -q
"""
from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

# Модуль несёт @dataclass с отложенными аннотациями (`from __future__ import
# annotations`) — dataclasses на Python 3.11 разрешает их через
# `sys.modules[cls.__module__]`. При обычном импорте (`import mutation_claim`)
# интерпретатор регистрирует модуль в sys.modules САМ, до выполнения тела —
# ничего дополнительно не требуется. Не так — только при загрузке ПО ФАЙЛУ
# через `importlib.util.module_from_spec` (в scripts/lib нет `__init__.py`,
# это принятый в репозитории способ импорта между скриптами): загружающая
# сторона обязана зарегистрировать модуль в `sys.modules[name] = module` ДО
# `exec_module` — тот же приём, что уже применяет
# `scripts/lib/guard_step_translator.py::_load_sibling` (см. её докстринг).

REPO_ROOT = Path(__file__).resolve().parents[2]

MUTATION_HEADING = "## Доказательство мутацией"
CLASS_CLOSED_HEADING = "## Класс закрыт"
UNVERIFIED_HEADING = "## Непроверено машиной"

# Единственная разрешённая форма поля «Тест:» — argv-список без shell=True.
# Ограничение осознанное (не просто удобство): тело PR — текст стороннего
# автора, попадающий в CI. Разрешать здесь произвольную shell-команду значило
# бы дать PR-body-тексту исполнение с правами job'а `test` (GH_TOKEN уровня
# шага). Сужение до "python -m pytest <target> -q" не расширяет радиус
# относительно уже существующего в репозитории соглашения (любой
# scripts/ci/guards/*.sh уже может содержать произвольный bash — см.
# scripts/ci/run_guards.sh) — это тот же периметр (файлы этого же PR
# проходят ревью тем же путём), просто без shell-инъекции через `Тест:`.
TEST_CMD_RE = re.compile(
    r"^python -m pytest ([\w./:\-]+) -q$"
)

# Известные формулировки заявлений, которые машина проверить не может без
# второго дорогого источника (эквивалент живого воспроизведения инцидента).
# Список — не полный охват прозы (недостижимо), а конкретные фразы из живых
# случаев #968/#893/#905/#964 — расширяется по мере находок, не заранее.
UNVERIFIABLE_PHRASES = [
    "прогнал на реальной истории",
    "прогнал на истории",
    "прогнал против истории",
    "проверил живьём",
    "проверено вручную",
    "проверено руками",
    "проверил руками",
    "визуально проверил",
    "вручную убедился",
    "убедился вручную",
]

_HEADING_LINE_RE = re.compile(r"^##\s+\S")
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


class MutationClaimFormatError(ValueError):
    """Секция с нужным заголовком есть, но не несёт обязательных полей —
    отличается от «секции нет вовсе» (см. докстринг модуля): контракт
    нарушен явно, а не молча пропущен."""


def _sections(body: str, heading: str) -> list[str]:
    """Тела ВСЕХ секций `body`, чей заголовок построчно равен `heading`
    (после strip). Секция кончается на следующей `## `-строке или на конце
    текста — тот же формат, что уже описан в docs/agents/PROTOCOL.md для
    «## Варианты владельца» (узкий нарочно: произвольная проза с тем же
    заголовком-подстрокой где-то в другом контексте не матчится, потому что
    заголовок должен занимать строку целиком)."""
    lines = (body or "").splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == heading:
            j = i + 1
            block: list[str] = []
            while j < n and not _HEADING_LINE_RE.match(lines[j].strip()):
                block.append(lines[j])
                j += 1
            out.append("\n".join(block).strip("\n"))
            i = j
        else:
            i += 1
    return out


@dataclass
class MutationClaim:
    test_cmd: str          # дословная команда, напр. "python -m pytest scripts/lib/test_x.py::test_y -q"
    test_target: str       # разобранный argv-таргет (после "pytest ")
    patch_text: str        # unified diff, git-apply-совместимый


def parse_mutation_claims(body: str) -> list[MutationClaim]:
    """Секции `MUTATION_HEADING` тела PR → список заявлений. Пустой список —
    заголовка в теле нет вовсе (author claim отсутствует — это НЕ ошибка
    формата, вызывающий код сам решает, обязателен ли блок для этого PR).
    `MutationClaimFormatError` — заголовок есть, но полей не хватает или
    команда не разрешённой формы (контракт нарушен, а не «не заявлено»)."""
    claims: list[MutationClaim] = []
    for idx, section in enumerate(_sections(body, MUTATION_HEADING), start=1):
        test_match = re.search(r"^\s*Тест:\s*`([^`]+)`\s*$", section, re.MULTILINE)
        if not test_match:
            raise MutationClaimFormatError(
                f"{MUTATION_HEADING} (блок {idx}): не найдена строка "
                "«Тест: `python -m pytest <путь>::<test_id> -q`» — команда "
                "обязана быть в обратных кавычках на отдельной строке"
            )
        test_cmd = test_match.group(1).strip()
        cmd_match = TEST_CMD_RE.match(test_cmd)
        if not cmd_match:
            raise MutationClaimFormatError(
                f"{MUTATION_HEADING} (блок {idx}): команда «{test_cmd}» не "
                "разрешённой формы — единственная форма: "
                "«python -m pytest <путь>[::<test_id>] -q» (без shell-операторов)"
            )
        fence_match = _FENCE_RE.search(section)
        if not fence_match or not fence_match.group(1).strip():
            raise MutationClaimFormatError(
                f"{MUTATION_HEADING} (блок {idx}): не найден непустой блок "
                "```diff ... ``` с unified diff, снимающим заявленный фикс"
            )
        claims.append(MutationClaim(
            test_cmd=test_cmd,
            test_target=cmd_match.group(1),
            patch_text=fence_match.group(1),
        ))
    return claims


@dataclass
class ClassClosedClaim:
    pattern: str
    expected_count: int


def parse_class_closed_claims(body: str) -> list[ClassClosedClaim]:
    """Секции `CLASS_CLOSED_HEADING` → список заявлений «класс закрыт
    везде», выраженных как grep-паттерн + ожидаемое число совпадений в
    репозитории (обычно 0 — «нигде не осталось старой формы»)."""
    claims: list[ClassClosedClaim] = []
    for idx, section in enumerate(_sections(body, CLASS_CLOSED_HEADING), start=1):
        pattern_match = re.search(r"^\s*Grep:\s*`([^`]+)`\s*$", section, re.MULTILINE)
        count_match = re.search(r"^\s*Ожидается совпадений:\s*(\d+)\s*$", section, re.MULTILINE)
        if not pattern_match or not count_match:
            raise MutationClaimFormatError(
                f"{CLASS_CLOSED_HEADING} (блок {idx}): нужны обе строки — "
                "«Grep: `<ERE-паттерн>`» и «Ожидается совпадений: <N>»"
            )
        claims.append(ClassClosedClaim(
            pattern=pattern_match.group(1).strip(),
            expected_count=int(count_match.group(1)),
        ))
    return claims


def run_class_closed_check(repo_root: Path, claim: ClassClosedClaim) -> tuple[bool, list[str]]:
    """`git grep` живого дерева паттерном заявления. Возвращает (совпало ли
    заявленное число, список найденных `path:line`) — печать решает вызывающий
    код (наблюдаемость требует показывать список, не просто числа)."""
    result = subprocess.run(
        ["git", "grep", "-nE", claim.pattern],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
    )
    # git grep: 0 — есть совпадения, 1 — нет совпадений, >=2 — ошибка (плохой
    # паттерн и т.п.) — различать нужно, иначе ошибка паттерна тихо считается
    # «совпадений 0».
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git grep упал (паттерн «{claim.pattern}»): {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return (len(lines) == claim.expected_count, lines)


def _strip_fences(body: str) -> str:
    """Тело PR без содержимого fenced-блоков — фразы внутри патча/диффа не
    должны триггерить поиск «непроверяемых» фраз (там это часть кода/диффа,
    не заявление автора)."""
    return _FENCE_RE.sub("", body or "")


def find_unverifiable_mentions(body: str) -> list[str]:
    """Известные непроверяемые формулировки, встреченные в прозе тела PR
    (вне fenced-блоков), без дублей, в порядке списка UNVERIFIABLE_PHRASES."""
    text = _strip_fences(body).lower()
    return [phrase for phrase in UNVERIFIABLE_PHRASES if phrase in text]


@dataclass
class UnverifiedCheck:
    mentions: list[str]
    disclosed: bool


def check_unverified_disclosure(body: str) -> UnverifiedCheck:
    """`mentions` — непроверяемые фразы, реально встреченные в теле.
    `disclosed` — секция `UNVERIFIED_HEADING` присутствует и непуста.
    Пустой `mentions` → `disclosed` не имеет значения (нечего раскрывать)."""
    mentions = find_unverifiable_mentions(body)
    sections = _sections(body, UNVERIFIED_HEADING)
    disclosed = any(section.strip() for section in sections)
    return UnverifiedCheck(mentions=mentions, disclosed=disclosed)


# ── Мутационное доказательство ──────────────────────────────────────────────

GUARD_CATALOG_PREFIX = "scripts/ci/guards/"


def guard_catalog_paths_changed(changed_paths: list[str]) -> list[str]:
    """Из списка изменённых файлов PR — те, что регистрируют/меняют гвардию
    каталога (`scripts/ci/guards/*.sh`, ТОЛЬКО верхний уровень — то же
    соглашение, что `scripts/ci/run_guards.sh` уже применяет к перебору).
    Именно эти PR несут дорогую мутационную проверку — остальные её не
    видят (цена, см. докстринг pr_mutation_claim_check.py)."""
    out = []
    for path in changed_paths:
        if not path.startswith(GUARD_CATALOG_PREFIX):
            continue
        rest = path[len(GUARD_CATALOG_PREFIX):]
        if "/" in rest:
            continue  # поддиректория — вне соглашения перебора, не гвардия
        if rest.endswith(".sh"):
            out.append(path)
    return out


@dataclass
class PhaseResult:
    name: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def tail(self, n: int = 15) -> str:
        lines = (self.stdout + self.stderr).strip().splitlines()
        return "\n".join(lines[-n:])


@dataclass
class MutationProofOutcome:
    verdict: str  # "proved" | "false_claim" | "setup_error"
    message: str
    phases: list[PhaseResult] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"mutation-claim[{self.verdict}]: {self.message}"]
        for phase in self.phases:
            status = "OK" if phase.passed else f"FAIL(rc={phase.returncode})"
            lines.append(f"  фаза {phase.name}: {status}")
            tail = phase.tail()
            if tail:
                lines.append("    " + tail.replace("\n", "\n    "))
        return "\n".join(lines)


def _run_test(repo_root: Path, target: str) -> PhaseResult:
    # PYTHONDONTWRITEBYTECODE — дочерний pytest не должен оставлять
    # __pycache__/ в дереве, за которым следит вызывающий код через
    # `git status` (иначе чужой мусор читается как «дерево не
    # восстановлено» после мутации). errors="replace" — дочерний процесс сам
    # может писать в локальной кодовой странице (issue #723, тот же класс,
    # что console_utf8.py уже чинит для ЭТОГО процесса) — декодирование не
    # должно падать в читающем потоке subprocess: при рассинхроне кодировки
    # вывод останется читаемым с точечными заменами, а не молчаливым крахом
    # фонового потока.
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q"],
        cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    return PhaseResult("test", result.returncode, result.stdout, result.stderr)


def run_mutation_proof(repo_root: Path, claim: MutationClaim) -> MutationProofOutcome:
    """Единственный механический прогон заявления «мутация доказана»:

      1. baseline — тест ДО мутации обязан быть зелёным (иначе заявление
         непроверяемо: либо тест сломан, либо таргет назван неверно).
      2. `git apply --check` — патч обязан примениться К ТЕКУЩЕМУ дереву
         (иначе setup_error, не false_claim — это не то же самое, что
         «мутация не красит», это «патч не накладывается»).
      3. применить патч, прогнать тест — ОБЯЗАН покраснеть. Если он зелёный
         — заявление ложно (ровно класс #893: снятие тела при сохранённом
         имени функции проходит структурную проверку молча).
      4. откатить патч (`git apply -R`), прогнать тест снова — обязан
         вернуться к зелёному (иначе патч отменяется нечисто, и рабочее
         дерево остаётся мутированным — тоже setup_error, не молчаливый успех).

    Working tree после вызова ВСЕГДА восстанавливается до состояния на входе
    (даже при setup_error) — `git checkout --` по файлам патча в блоке finally
    вызывающего кода не нужен: функция сама гарантирует чистый откат ниже."""
    phases: list[PhaseResult] = []

    baseline = _run_test(repo_root, claim.test_target)
    phases.append(PhaseResult("baseline (до мутации)", baseline.returncode, baseline.stdout, baseline.stderr))
    if not baseline.passed:
        return MutationProofOutcome(
            "setup_error",
            f"тест «{claim.test_target}» уже красный ДО мутации — заявление "
            "непроверяемо (почини тест или таргет команды «Тест:»)",
            phases,
        )

    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=repo_root, input=claim.patch_text, capture_output=True, text=True, encoding="utf-8",
    )
    phases.append(PhaseResult("git apply --check", check.returncode, check.stdout, check.stderr))
    if check.returncode != 0:
        return MutationProofOutcome(
            "setup_error",
            "патч из блока «Доказательство мутацией» не накладывается на "
            "текущее дерево (git apply --check упал) — обнови патч",
            phases,
        )

    apply_res = subprocess.run(
        ["git", "apply", "-"],
        cwd=repo_root, input=claim.patch_text, capture_output=True, text=True, encoding="utf-8",
    )
    phases.append(PhaseResult("git apply", apply_res.returncode, apply_res.stdout, apply_res.stderr))
    if apply_res.returncode != 0:
        return MutationProofOutcome(
            "setup_error",
            "git apply --check прошёл, а сам git apply упал — не должно "
            "случаться, дерево не тронуто",
            phases,
        )

    try:
        mutated = _run_test(repo_root, claim.test_target)
        phases.append(PhaseResult("mutated (после снятия фикса)", mutated.returncode, mutated.stdout, mutated.stderr))
    finally:
        revert = subprocess.run(
            ["git", "apply", "-R", "-"],
            cwd=repo_root, input=claim.patch_text, capture_output=True, text=True, encoding="utf-8",
        )
        phases.append(PhaseResult("git apply -R (возврат)", revert.returncode, revert.stdout, revert.stderr))

    if revert.returncode != 0:
        return MutationProofOutcome(
            "setup_error",
            "откат патча (git apply -R) не прошёл — рабочее дерево могло "
            "остаться мутированным, требуется ручная проверка",
            phases,
        )

    if mutated.passed:
        return MutationProofOutcome(
            "false_claim",
            f"заявление ЛОЖНО: тест «{claim.test_target}» остался ЗЕЛЁНЫМ "
            "после применения патча, снимающего фикс — заявленная мутация "
            "не проверяет поведение (класс #893: имя есть, тела нет)",
            phases,
        )

    restored = _run_test(repo_root, claim.test_target)
    phases.append(PhaseResult("restored (после возврата)", restored.returncode, restored.stdout, restored.stderr))
    if not restored.passed:
        return MutationProofOutcome(
            "setup_error",
            f"после возврата патча тест «{claim.test_target}» остался "
            "красным — откат не восстановил исходное поведение",
            phases,
        )

    return MutationProofOutcome(
        "proved",
        f"доказано: тест «{claim.test_target}» красный под мутацией, "
        "зелёный до и после возврата",
        phases,
    )
