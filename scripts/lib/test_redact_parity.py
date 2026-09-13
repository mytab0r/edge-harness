#!/usr/bin/env python3
"""Гвардия паритета маскирования: dsh-ci.sh::redact ↔ cf-worker/src/redact.ts.

Класс проблемы (ревью PR #173): инбокс морды пишет произвольный фриформ-текст
владельца в ПУБЛИЧНЫЙ issue — новый наружный путь, которому нужен тот же класс
паттернов секретов, что у bash-транспортов. Паттерны живут в двух местах
(bash и TS), и без гвардии они расходятся молча: новая форма секрета,
добавленная в dsh-ci.sh, доехала бы до публичного issue в сыром виде.

Два правила, каждое красит отдельно (мутационно):
  1. Каждая sed-подстановка redact() dsh-ci.sh имеет строку-пару в redact.ts:
     тот же префикс секрета и тот же токен замены <префикс>[REDACTED].
  2. Число подстановок совпадает: форма, добавленная только в TS, тоже красит.

Запуск: python -m pytest scripts/lib/test_redact_parity.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DSH_CI = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
TS_REDACT = REPO_ROOT / "cf-worker" / "src" / "redact.ts"

# sed-подстановка внутри redact(): -e 's/МАТЧ/ЗАМЕНА/g'
_SED_SUB_RE = re.compile(r"-e\s+'s/([^/]+)/([^/']*)/g'")
# Ведущая sed-группа контекста матча: (^|[^...]) — не часть секрета.
_CONTEXT_GROUP_RE = re.compile(r"^\(\^\|\[\^[^\]]*\]\)")
# Литеральный префикс секрета: nvapi-, sk-, ghp_, github_pat_
_PREFIX_RE = re.compile(r"^([a-z][a-z0-9_]*(?:_|-))")


def sed_substitutions() -> list[tuple[str, str]]:
    """(префикс секрета, токен замены) для каждой подстановки redact()."""
    text = DSH_CI.read_text(encoding="utf-8")
    block = text.split("redact()", 1)[1]
    subs: list[tuple[str, str]] = []
    for match, replacement in _SED_SUB_RE.findall(block):
        stripped = _CONTEXT_GROUP_RE.sub("", match)
        prefix = _PREFIX_RE.match(stripped)
        assert prefix, (
            f"не удалось извлечь префикс секрета из sed-матча {match!r} — "
            "гвардия смотрит не туда или форма sed изменилась"
        )
        assert "[REDACTED]" in replacement, f"замена без [REDACTED]: {replacement!r}"
        subs.append((prefix.group(1), replacement))
    assert subs, f"в {DSH_CI} не найдено подстановок redact() — гвардия смотрит не туда"
    return subs


def ts_pattern_lines() -> list[str]:
    """Строки-паттерны массива REDACT_PATTERNS: `[/…/g, "…[REDACTED]"],` —
    комментарии и докстрок не считаются."""
    ts = TS_REDACT.read_text(encoding="utf-8")
    return [
        line for line in ts.splitlines()
        if "[REDACTED]" in line and re.match(r"^\s*\[/.*/g,", line)
    ]


def test_every_sed_secret_prefix_is_masked_in_ts():
    """Мутация (а): форма секрета добавлена в dsh-ci.sh, но не в TS — красный."""
    ts_lines = ts_pattern_lines()
    missing = [
        (prefix, replacement)
        for prefix, replacement in sed_substitutions()
        if not any(prefix in line and replacement.split("\\1")[-1] in line for line in ts_lines)
    ]
    assert not missing, (
        f"формы секретов {missing} есть в dsh-ci.sh::redact, но их нет в {TS_REDACT}: "
        "публичный issue из инбокса получил бы сырой секрет — TS-модуль обязан "
        "маскировать тот же класс паттернов"
    )


def test_same_number_of_substitutions():
    """Мутация (б): форма добавлена только в TS (или удалена из bash) — красный."""
    sed_count = len(sed_substitutions())
    ts_count = len(ts_pattern_lines())
    assert sed_count == ts_count, (
        f"sed-подстановок в dsh-ci.sh::redact: {sed_count}, паттернов в {TS_REDACT}: {ts_count} — "
        "списки маскирования обязаны совпадать один к одному"
    )
