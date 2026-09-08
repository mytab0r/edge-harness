#!/usr/bin/env python3
"""Гвардия класса «Windows-кодировка валит скрипт репозитория» (issue #723).

Два независимых, но родственных отказа, найденных на живом ru-RU Windows:

1. **Encode**: `print(text)` в stdout (не `file=sys.stderr` — там CPython
   всегда `backslashreplace`, не падает) — если stdout перехвачен (piped/
   redirected, обычный случай для CI и для любого агента-инструмента),
   Python берёт ANSI-кодовую страницу локали (`cp1251` на этой машине) с
   `strict`-обработчиком: печать эмодзи (`🔒`, `⚠️`, …) падает
   `UnicodeEncodeError`. Фикс — `scripts/lib/console_utf8.py::
   ensure_utf8_stdio()`, подключается одной строкой в каждой точке входа.
2. **Decode**: `subprocess.run(["gh", "api", ...], text=True)` БЕЗ явного
   `encoding="utf-8"` — тот же дефолт (ANSI-кодовая страница локали),
   падает на UTF-8 JSON-ответе GitHub с не-ASCII байтом (заголовок/тело
   issue на русском). Живой трейс (issue #723): `task-branch` → `python3
   scripts/lib/epic_guard.py` → `scheduler.gh` → `pulse_guard.gh` —
   `UnicodeDecodeError` в фоновом `_readerthread` (не пробрасывается в
   вызывающий поток!), после чего `result.stdout is None`, и
   `result.stdout.strip()` падает `AttributeError` — то есть звучит как
   «сеть сломана», а на деле кодировка. Фикс — явный `encoding="utf-8"`
   в каждом таком вызове.

Тест 1 и 2 — статические гвардии по исходнику (регрессия невозможна для
ВСЕГО класса, не только для файлов, тронутых сейчас): любой НОВЫЙ скрипт со
своей точкой входа или своим `subprocess.run(text=True)` обязан подключить
`console_utf8`/указать `encoding=`, иначе тест красный сразу.

Тест 3 — живая мутация: реальный `pulse_guard.gh()` с реалистичной
кириллической полезной нагрузкой (байт `0x98` — вторая половина валидной
UTF-8 последовательности буквы «И», ровно то, что уронило прод). Убери
`encoding="utf-8"` из `pulse_guard.py::gh` — тест 3 покраснеет.

Запуск: python -m pytest scripts/lib/test_console_utf8_guard.py -q
"""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

_SUBPROCESS_RUN_CALL_RE = re.compile(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", re.DOTALL)


def _entry_point_scripts() -> list[Path]:
    """Все самостоятельные точки входа (`if __name__ == "__main__":`),
    кроме тестов — тот же критерий, каким собирался список для issue #723."""
    return sorted(
        p for p in SCRIPTS_DIR.rglob("*.py")
        if not p.name.startswith("test_")
        and p.name != "console_utf8.py"
        and "__main__" in p.read_text(encoding="utf-8", errors="replace")
    )


def _subprocess_run_calls_missing_encoding(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    missing = []
    for m in _SUBPROCESS_RUN_CALL_RE.finditer(src):
        call = m.group(0)
        has_text_true = "text=True" in call or "text = True" in call
        if has_text_true and "encoding=" not in call:
            missing.append(call.strip().splitlines()[0][:80])
    return missing


# ── Тест 1: каждая точка входа подключает console_utf8 ───────────────────────


def test_every_entry_point_bootstraps_console_utf8():
    missing = [
        str(p.relative_to(REPO_ROOT)) for p in _entry_point_scripts()
        if "console_utf8" not in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert not missing, (
        "Точки входа без bootstrap'а console_utf8.py (encode-класс, issue #723): "
        + ", ".join(missing)
    )


# ── Тест 2: ни один subprocess.run(text=True) не забыл encoding= ────────────


def test_no_subprocess_run_text_true_without_encoding():
    offenders: dict[str, list[str]] = {}
    for p in SCRIPTS_DIR.rglob("*.py"):
        if p.name in ("console_utf8.py", "test_console_utf8_guard.py"):
            continue  # сам модуль-фикс и эта гвардия — не мишень своей же проверки
        found = _subprocess_run_calls_missing_encoding(p)
        if found:
            offenders[str(p.relative_to(REPO_ROOT))] = found
    assert not offenders, (
        "subprocess.run(text=True) без encoding=\"utf-8\" (decode-класс, issue #723), "
        "падает UnicodeDecodeError в фоновом потоке на реальном GitHub-ответе "
        f"с кириллицей: {offenders}"
    )


# ── Тест 3: живая мутация — pulse_guard.gh() с реалистичным байтом 0x98 ─────


def test_pulse_guard_gh_survives_cyrillic_byte_that_breaks_cp1251(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "pulse_guard_console_utf8_check", SCRIPTS_DIR / "orchestra" / "pulse_guard.py")
    pulse_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

    # "И" = U+0418 -> UTF-8 0xD0 0x98: реалистичный, ПРАВИЛЬНО сформированный
    # UTF-8 (не изолированный мусорный байт — иначе упал бы и с encoding=
    # "utf-8", доказывая не то). Именно такой байт (0x98) уронил прод
    # (issue #723): GitHub отвечает валидным UTF-8 всегда, ANSI-кодовая
    # страница локали (cp1251) — нет.
    title = "задача: Иванов проверил приёмку"
    payload = ('{"login": "%s"}' % title).encode("utf-8")
    assert 0x98 in payload, "тест должен реально бить по проблемному байту"

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        return real_run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(%r)" % payload],
            **kwargs,
        )

    monkeypatch.setattr(pulse_guard.subprocess, "run", fake_run)
    result = pulse_guard.gh("user")
    assert result == {"login": title}
