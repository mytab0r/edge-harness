#!/usr/bin/env python3
"""Гвардия класса «--body-file у gh secret set/gh variable set непортируем» (#786, #840).

Класс: установленная в раннерах и на машинах агентов версия `gh` (2.85.0) не
знает флаг `--body-file` у `gh secret set`/`gh variable set` (`unknown flag`).
Оба подкоманды читают значение из **stdin**, когда `--body`/`--body-file` не
передан вовсе — флаг лишний и вредный: он не отклоняется тихо, он валит сам
вызов, и значение секрета/переменной не записывается, хотя код возврата
ловится и печатается как обычный сбой (не отличить от сетевого).

Живой случай: `scripts/lib/provider_secrets_import.py::set_secret`/
`set_variable` несли `--body-file "-"` — почтено #786, здесь фикс закрывается
ПО ВСЕМУ `scripts/`, не только в одном файле (AGENTS.md, «Починил случай —
закрой класс»): любой НОВЫЙ вызов `gh secret set`/`gh variable set` с
`--body-file` в argv — красный тест, а не находка постфактум на живом прогоне.

Доказательство мутацией: временно верни `--body-file` в любую из функций —
этот тест краснеет (см. docstring класса выше про инвариант).

Область: `.py`-файлы (argv-список `subprocess.run([...])`) и `.sh`-файлы
(вызов `gh secret set ...`/`gh variable set ...` одной строкой) внутри
`scripts/`. Обёртки, вызывающие `--body-file` у ДРУГИХ подкоманд gh
(`gh issue create`, `gh pr create`) — не этот класс, gh отлично знает
`--body-file` там; они не матчатся регулярками ниже, потому что те
привязаны буквально к `secret`/`variable` + `set`.

Запуск: python -m pytest scripts/lib/test_gh_body_file_guard.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Python: argv-список литералом, "gh", "secret"/"variable", "set" подряд,
# с чем угодно (включая перенос строк) до закрывающей "]", если внутри
# встретился --body-file.
PY_ARGV_RE = re.compile(
    r'\[\s*"gh"\s*,\s*"(?:secret|variable)"\s*,\s*"set"[^\]]*\]',
    re.DOTALL,
)

# Shell: gh secret set / gh variable set ... --body-file на одной строке.
SH_CALL_RE = re.compile(
    r'\bgh\s+(?:secret|variable)\s+set\b[^\n]*--body-file',
)

VIOLATION_HINT = (
    "gh secret set/gh variable set не поддерживает --body-file на gh 2.85 "
    "(unknown flag, #786) — значение только через stdin (input=value в "
    "subprocess.run, без --body/--body-file в argv)."
)


def _scan_python(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    violations = []
    for match in PY_ARGV_RE.finditer(text):
        if "--body-file" in match.group(0):
            line_no = text.count("\n", 0, match.start()) + 1
            violations.append(f"{path}:{line_no}")
    return violations


def _scan_shell(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    violations = []
    for match in SH_CALL_RE.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        violations.append(f"{path}:{line_no}")
    return violations


def test_no_body_file_in_gh_secret_or_variable_set_calls():
    violations: list[str] = []
    for path in SCRIPTS_DIR.rglob("*.py"):
        violations.extend(_scan_python(path))
    for path in SCRIPTS_DIR.rglob("*.sh"):
        violations.extend(_scan_shell(path))
    # task-branch, pr-create, issue-create и т.п. — файлы без расширения
    # (bash-скрипты). Сканируем их тоже тем же shell-регексом.
    for path in SCRIPTS_DIR.rglob("*"):
        if path.is_file() and path.suffix == "" and path.name not in {".gitkeep"}:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in SH_CALL_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                violations.append(f"{path}:{line_no}")

    assert not violations, (
        f"{VIOLATION_HINT}\nНайдено: {violations}"
    )


if __name__ == "__main__":
    import sys
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"] + sys.argv[1:]))
