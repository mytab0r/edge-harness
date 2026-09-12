#!/usr/bin/env python3
"""Канарейка «мимо двери нельзя» (#611): проверяет не текст, а СПОСОБ.

Повод: обе существующие двери — `scripts/gh/issue-create` (гвардия дублей,
#526/#566) и `scripts/git/pr-create` (контракт PR, #496) — проверяют
аргументы НА ВХОДЕ, но ни разу не проверялось, кто ещё в репозитории делает
ту же GitHub-мутацию МИМО двери. Обход существовал и сработал дважды:
наблюдатель падений заводил задачи напрямую (пять дублей #578/#580/#589/
#592/#598 за час, класс закрыт отдельно), PR #586 был открыт голым
`gh pr create`. Частичная гвардия (`repo-ci.yml`, класс #179/#526) уже
грепает `repos/{repo}/issues"` в трёх директориях — разовая, только для
issue-двери, без реестра, без газа для легитимных исключений.

Единственное место правды на «какая операция кем охраняется» — DOORS ниже.
Добавление третьей двери — одна правка DOORS + одна-две строки в CHECKS,
не правка в трёх местах.

Что считается обходом (per DOORS-door):
  - CLI-форма: `gh issue create` / `gh pr create` в КОМАНДНОЙ позиции (начало
    строки, после `;`/`&`/`|`/`$(`/`exec `) — не в комментарии и не внутри
    произвольного текста (упоминание после произвольного слова/бэктика —
    не команда, а проза; проверено на реальных докстрингах этого репозитория,
    см. test_door_guard.py).
  - REST-форма: строковый литерал, оканчивающийся РОВНО на `.../issues` или
    `.../pulls` (коллекция, не под-ресурс вроде `/issues/{n}/labels` —
    у него после «issues» идёт `/`, не кавычка, поэтому мимо) непосредственно
    перед кавычкой/бэктиком, при этом где-то в окне ±5 строк встречается
    маркер метода POST. Различие «создать» от «прочитать/пометить/
    прокомментировать» — по этому же признаку: список/чтение почти
    всегда несёт query-string (`?state=open`) или уходит на под-ресурс, что
    ломает якорь «сразу кавычка после issues/pulls».

Что НЕ ловит (честная граница, не приукрашено):
  - Динамически собранный URL/путь (конкатенация переменных до литерала,
    `endpoint = base + "/issues"`) — статический разбор строк текста этого
    не видит.
  - Вызов из чужой зависимости (node_modules, установленный пакет) — вне
    охвата по конструкции (см. EXCLUDED_DIR_PARTS).
  - GitHub Action из чужого репозитория (`uses: peter-evans/create-issue@…`)
    без `run:` — эта гвардия читает только `run:`-шаги/исходники этого
    репозитория; сам `uses:` не парсится (на дату написания в репозитории нет
    ни одного такого action — см. test_no_third_party_issue_pr_actions).
  - Блочные комментарии (`/* … */`) и многострочные docstring-строки Python
    (не начинающиеся с `#`/`//`) — стрипается только ЦЕЛАЯ строка-комментарий,
    как и в exec_bit_guard.py/test_pr_create_guard.py. Мнимая находка отсюда
    исключена не общим парсером, а тем, что реальные мутации в этом
    репозитории живут в командной позиции (см. докстринг выше), а не в
    середине строки текста.
  - POST-маркер ищется по подстроке "POST" (заглавными) в окне ±5 строк —
    JS-форма `method: 'POST'` часто на СЛЕДУЮЩЕЙ после URL строке, разбор
    в одну строку это бы пропустил.

Запуск:
  python scripts/lib/door_guard.py          # печать отчёта, exit 1 при нарушении
  python -m pytest scripts/lib/test_door_guard.py -q
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
import sys
from dataclasses import dataclass

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── Реестр дверей: одно место правды ─────────────────────────────────────────
# door_files — сама дверь ЗАКОНОМЕРНО зовёт GitHub напрямую (первое честное
# исключение: она и есть механизм, не обход механизма).
# Комментарии — ОТДЕЛЬНЫМИ строками, не хвостом после запятой: сама эта
# гвардия сканирует и свой собственный файл (test_door_guard.py::
# test_build_report_on_real_repo_is_clean), а её CLI-детектор не различает
# хвостовой комментарий от кода (см. «Что НЕ ловит» выше) — хвостовое
# упоминание вызова здесь красило бы гвардию собственной прозой.
DOORS = {
    "issue-create": {
        "description": "создание НОВОЙ issue пула",
        "door_files": (
            # scripts/gh/issue-create — терминал/агент, обёртка вызывает gh
            "scripts/gh/issue-create",
            # scripts/lib/pool_issue.py — программный код (create_pool_issue)
            "scripts/lib/pool_issue.py",
        ),
    },
    "pr-create": {
        "description": "открытие НОВОГО pull request",
        "door_files": (
            # scripts/git/pr-create — обёртка вызывает gh
            "scripts/git/pr-create",
        ),
    },
}

# Директории, которые вообще сканируются (охват задачи #611: .github/workflows,
# scripts/**, cf-worker/**, dsh-edge/**, plugins-src/**).
SCAN_ROOTS = ("scripts", ".github/workflows", "cf-worker", "dsh-edge", "plugins-src")
SCAN_EXTENSIONS = (".py", ".sh", ".yml", ".yaml", ".js", ".ts", ".mjs")
EXCLUDED_DIR_PARTS = {"node_modules", "__pycache__", ".git", "wrangler-dist"}

# Тестовые файлы — фикстуры/моки чужих вызовов (строки вида
# `args[2] == "repos/o/r/issues"` в test_scheduler.py), не настоящие
# вызовы; этот же критерий уже используют fixtures рядом (test_pr_create_guard.py
# не сканирует их вовсе, полагаясь на то, что мок не бьёт по сети).
_TEST_FILE_RE = re.compile(r"^test_.*\.py$")
_TEST_SUFFIX_RE = re.compile(r"\.(test|spec)\.(sh|js|ts|mjs)$")


def _is_test_path(rel: str, name: str) -> bool:
    if _TEST_FILE_RE.match(name):
        return True
    if _TEST_SUFFIX_RE.search(name):
        return True
    parts = rel.split("/")
    return "test" in parts[:-1]


def _is_excluded(rel: str) -> bool:
    return any(part in EXCLUDED_DIR_PARTS for part in rel.split("/"))


# ── Детекторы: CLI-форма и REST-форма, по одному regex на дверь ─────────────
# Командная позиция: начало строки, либо сразу после `;`/`&`/`|`/`$(`/`exec `.
# НЕ включает бэктик — упоминание в обратных кавычках (докстринг/markdown,
# `` `gh issue create` ``) в этом репозитории всегда прозаическое, не вызов
# (проверено сплошным прочёсом при разработке — см. test_door_guard.py).
_COMMAND_POS = r"(?:^|[;&|]|\$\(|\bexec\s)\s*"
CLI_PATTERNS = {
    "issue-create": re.compile(_COMMAND_POS + r"gh\s+issue\s+create\b"),
    "pr-create": re.compile(_COMMAND_POS + r"gh\s+pr\s+create\b"),
}
# Коллекция ровно `.../issues` или `.../pulls` перед кавычкой/бэктиком — не
# под-ресурс (`/issues/{n}/labels`, после "issues" там `/`, не кавычка) и не
# query-string чтения (`issues?state=open`, после "issues" там `?`).
REST_PATTERNS = {
    "issue-create": re.compile(r"repos/[^\"'\s`]*?/issues[\"'`]"),
    "pr-create": re.compile(r"repos/[^\"'\s`]*?/pulls[\"'`]"),
}
CHECKS = (
    ("issue-create", "cli", CLI_PATTERNS["issue-create"]),
    ("issue-create", "rest", REST_PATTERNS["issue-create"]),
    ("pr-create", "cli", CLI_PATTERNS["pr-create"]),
    ("pr-create", "rest", REST_PATTERNS["pr-create"]),
)

POST_WINDOW = 5
GAS_WINDOW = 2
_GAS_RE = re.compile(r"door-exception:\s*(\S.*)")


def _comment_marker(suffix: str) -> str:
    return "//" if suffix in (".js", ".ts", ".mjs") else "#"


def _post_marker_nearby(lines: list[str], idx: int, window: int = POST_WINDOW) -> bool:
    lo = max(0, idx - window)
    hi = min(len(lines), idx + window + 1)
    return any("POST" in lines[j] for j in range(lo, hi))


def _gas_reason(lines: list[str], idx: int, window: int = GAS_WINDOW) -> str | None:
    lo = max(0, idx - window)
    for j in range(lo, idx + 1):
        match = _GAS_RE.search(lines[j])
        if match:
            return match.group(1).strip()
    return None


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    door: str
    code: str

    def message(self) -> str:
        door = DOORS[self.door]
        entries = ", ".join(door["door_files"])
        return (
            f"{self.file}:{self.line}: обход двери «{self.door}» "
            f"({door['description']}) — {self.code!r}. Единственный "
            f"документированный вход: {entries}. Легитимное исключение — "
            f"комментарий `door-exception: <причина>` на этой строке или "
            f"одной из {GAS_WINDOW} строк выше."
        )


def iter_scan_files(repo_root: Path = REPO_ROOT):
    for root_name in SCAN_ROOTS:
        root = repo_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in SCAN_EXTENSIONS:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if _is_excluded(rel):
                continue
            if _is_test_path(rel, path.name):
                continue
            yield path, rel


def scan_lines(rel: str, lines: list[str], suffix: str) -> list[Violation]:
    """Чистая функция — без IO, доказывается мутацией на литеральных фрагментах
    (test_door_guard.py), тем же приёмом, что exec_bit_guard.py::check_exec_bit."""
    marker = _comment_marker(suffix)
    violations: list[Violation] = []
    for door_name, kind, pattern in CHECKS:
        if rel in DOORS[door_name]["door_files"]:
            continue
        for i, line in enumerate(lines):
            if line.lstrip().startswith(marker):
                continue
            if not pattern.search(line):
                continue
            if kind == "rest" and not _post_marker_nearby(lines, i):
                continue
            if _gas_reason(lines, i) is not None:
                continue
            violations.append(Violation(file=rel, line=i + 1, door=door_name, code=line.strip()))
    return violations


def scan_repo(repo_root: Path = REPO_ROOT) -> list[Violation]:
    violations: list[Violation] = []
    for path, rel in iter_scan_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        violations.extend(scan_lines(rel, text.splitlines(), path.suffix))
    return violations


def main() -> int:
    violations = scan_repo()
    if not violations:
        print("door-guard: обходов дверей GitHub-мутаций не найдено")
        return 0
    for violation in violations:
        print(f"::error::{violation.message()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
