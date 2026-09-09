#!/usr/bin/env python3
"""Гвардия класса «Windows-кодировка валит скрипт репозитория» (issue #723).

Три независимых, но родственных отказа, найденных на живом ru-RU Windows —
одна и та же причина (дефолт CPython на Windows — ANSI-кодовая страница
локали, не UTF-8) бьёт в трёх разных местах ввода-вывода:

1. **Encode (stdout)**: `print(text)` в stdout (не `file=sys.stderr` — там
   CPython всегда `backslashreplace`, не падает) — если stdout перехвачен
   (piped/redirected, обычный случай для CI и для любого агента-инструмента),
   Python берёт ANSI-кодовую страницу локали (`cp1251` на этой машине) с
   `strict`-обработчиком: печать эмодзи (`🔒`, `⚠️`, …) падает
   `UnicodeEncodeError`. Фикс — `scripts/lib/console_utf8.py::
   ensure_utf8_stdio()`, подключается ДОСЛОВНЫМ блоком (см. тест 1) в каждой
   точке входа.
2. **Decode (subprocess)**: `subprocess.run(["gh", "api", ...], text=True)`
   БЕЗ явного `encoding="utf-8"` — тот же дефолт (ANSI-кодовая страница
   локали), падает на UTF-8 JSON-ответе GitHub с не-ASCII байтом
   (заголовок/тело issue на русском). Живой трейс (issue #723): `task-branch`
   → `python3 scripts/lib/epic_guard.py` → `scheduler.gh` → `pulse_guard.gh`
   — `UnicodeDecodeError` в фоновом `_readerthread` (не пробрасывается в
   вызывающий поток!), после чего `result.stdout is None`, и
   `result.stdout.strip()` падает `AttributeError` — то есть звучит как
   «сеть сломана», а на деле кодировка. Фикс — явный `encoding="utf-8"`
   в каждом таком вызове.
3. **Файловый ввод-вывод**: `Path.read_text()`/`Path.write_text()`/`open()`
   без явного `encoding=` — та же ANSI-кодовая страница, тот же класс отказа
   (доработка ревью PR #726, блокирующая находка 2): агент на Windows не
   может прогнать собственный набор тестов репозитория, если тест пишет
   кириллицу во временный файл дефолтной кодировкой, а читает её другой тест
   или код продукта, ожидающий UTF-8 (живой пример на момент правки —
   `test_orphan_test_guard.py` писал маркер `ORPHAN-TEST-OK` cp1251-байтами,
   читатель ждал UTF-8).

Тест 1, 2 и 3 — статические гвардии по исходнику (регрессия невозможна для
ВСЕГО класса, не только для файлов, тронутых сейчас): любой НОВЫЙ скрипт со
своей точкой входа, своим `subprocess.run(text=True)` или своим файловым
вводом-выводом обязан подключить `console_utf8`/указать `encoding=`, иначе
тест красный сразу.

Тест 1 требует ДОСЛОВНОГО присутствия канонического блока
(`console_utf8.BOOTSTRAP_BLOCK_SAME_DIR`/`BOOTSTRAP_BLOCK_PARENT_LIB`), не
подстроки "console_utf8" где угодно — иначе докстринг/комментарий,
НАЗЫВАЮЩИЙ модуль-фикс, но не подключающий его, проходит гвардию (доказано
мутацией при ревью PR #726). Точка входа — не только «есть `if __name__ ==
"__main__":`», но и «вызывается как скрипт» (см. `_invoked_script_paths` —
механический grep по `.github`/`scripts`/`.githooks`/`docs`, тот же приём,
каким ревью PR #726 доказало охват): скрипт, у которого только докстринг
называет способ запуска (`провайдер_secrets_import.py` до того, как обзавёлся
`__main__`, — ровно эта форма), всё равно обязан подключить bootstrap.

Тест 4 — живая мутация: реальный `pulse_guard.gh()` с реалистичной
кириллической полезной нагрузкой (байт `0x98` — вторая половина валидной
UTF-8 последовательности буквы «И», ровно то, что уронило прод). Убери
`encoding="utf-8"` из `pulse_guard.py::gh` — тест 4 покраснеет НА ЭТОЙ
машине (легаси-кодировка локали не может декодировать байт 0x98); на CI-
раннере с UTF-8-локалью по умолчанию (`ubuntu-latest`) эта же мутация не
провоцирует ошибку декодирования вообще (decode внутренне и так падёт на
UTF-8) — тест честно пропускает себя в этом случае (см. `pytest.skip` внутри),
не выдавая зелёный CI за доказательство того, что ловит именно эту мутацию:
на CI класс decode-двери реально держит только статический тест 2 (находка
ревью PR #726, неблокирующая Н1).

Запуск: python -m pytest scripts/lib/test_console_utf8_guard.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import locale
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

_SUBPROCESS_RUN_CALL_RE = re.compile(r"subprocess\.run\((?:[^()]|\([^()]*\))*\)", re.DOTALL)

# Тот же приём, что уже применяет остальной модуль (importlib.util вместо
# добавления scripts/lib в sys.path — не заводим вторую точку записи того же
# импорта): единственное место правды на КАНОНИЧЕСКИЙ текст bootstrap-блока —
# console_utf8.py само, не копия строк здесь.
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8_guard_canon", SCRIPTS_DIR / "lib" / "console_utf8.py")
console_utf8 = importlib.util.module_from_spec(_console_utf8_spec)
_console_utf8_spec.loader.exec_module(console_utf8)  # type: ignore[union-attr]


# ── Точки входа: критерий "вызывается как скрипт", не только __main__ ───────

# Собирает мехнически то же самое, что вручную прогонял ревью PR #726:
# `grep -rhoE "(python3?|python -m|py) +scripts/[^ ]+\.py" .github scripts
# .githooks docs`. Ловит и скрипты БЕЗ `if __name__ == "__main__":`, которые
# тем не менее документированы/вызваны как самостоятельный процесс где-то в
# дереве (workflow, docs, docstring другого скрипта) — ровно форма, в которой
# класс уже прорвался (см. докстринг модуля).
_INVOKED_SCRIPT_RE = re.compile(r"(?:python3?|python -m|py) +(scripts/[^\s\"'`]+\.py)")
_INVOKED_SEARCH_DIRS = (".github", "scripts", ".githooks", "docs")


def _invoked_script_paths() -> set[str]:
    paths: set[str] = set()
    for rel_dir in _INVOKED_SEARCH_DIRS:
        base = REPO_ROOT / rel_dir
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or "__pycache__" in p.parts or ".git" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _INVOKED_SCRIPT_RE.finditer(text):
                paths.add(m.group(1))
    return paths


def _entry_point_scripts() -> list[Path]:
    """Все самостоятельные точки входа, кроме тестов: `if __name__ ==
    "__main__":` ИЛИ файл встречается как аргумент `python`/`python3`/`py` в
    `.github`/`scripts`/`.githooks`/`docs` (см. _invoked_script_paths) — union
    двух признаков, не замена: файлы, у которых есть только __main__ (не
    упомянутые нигде текстом буквально — например, вызываются через
    `subprocess.run([sys.executable, str(path)])` с динамическим путём),
    по-прежнему считаются точками входа."""
    invoked = _invoked_script_paths()
    result = []
    for p in sorted(SCRIPTS_DIR.rglob("*.py")):
        if p.name.startswith("test_") or p.name == "console_utf8.py":
            continue
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        text = p.read_text(encoding="utf-8", errors="replace")
        if "__main__" in text or rel in invoked:
            result.append(p)
    return result


def _expected_bootstrap_block(path: Path) -> str:
    """Форма пути зависит ТОЛЬКО от расположения файла относительно
    console_utf8.py (scripts/lib/console_utf8.py), не от произвольного
    выбора автора — гвардия проверяет ИМЕННО ту форму, что верна для этого
    расположения, а не "любую из двух" (доказанная ревью PR #726 дыра: блок
    с чужой глубиной пути импортирует несуществующий файл молча не падая на
    этом тесте, если проверять только "одна из двух форм присутствует")."""
    if path.parent == SCRIPTS_DIR / "lib":
        return console_utf8.BOOTSTRAP_BLOCK_SAME_DIR
    return console_utf8.BOOTSTRAP_BLOCK_PARENT_LIB


def _subprocess_run_calls_missing_encoding(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    missing = []
    for m in _SUBPROCESS_RUN_CALL_RE.finditer(src):
        call = m.group(0)
        has_text_true = "text=True" in call or "text = True" in call
        if has_text_true and "encoding=" not in call:
            missing.append(call.strip().splitlines()[0][:80])
    return missing


def _file_io_calls_missing_encoding(path: Path) -> list[str]:
    """Третья дверь класса (файловый ввод-вывод): `Path.read_text()`/
    `Path.write_text()`/`open()` в текстовом режиме без `encoding=`. Разбор
    по AST (не regex) — те же вызовы легко живут внутри многострочных
    f-строк/dict-литералов, где парный-скобочный regex (см.
    _SUBPROCESS_RUN_CALL_RE) даёт кучу ложных срабатываний (проверено при
    правке: наивный regex по этому файлу находил 24 "нарушения", реальных
    было 7)."""
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    def has_encoding_kw(call: ast.Call) -> bool:
        return any(kw.arg == "encoding" for kw in call.keywords)

    def is_binary_mode(call: ast.Call) -> bool:
        if len(call.args) < 2 or not isinstance(call.args[1], ast.Constant):
            return False
        mode = call.args[1].value
        return isinstance(mode, str) and "b" in mode

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("read_text", "write_text"):
            if not has_encoding_kw(node):
                offenders.append(f"{node.func.attr}() @ строка {node.lineno}")
        elif isinstance(node.func, ast.Name) and node.func.id == "open":
            if not has_encoding_kw(node) and not is_binary_mode(node):
                offenders.append(f"open() @ строка {node.lineno}")
    return offenders


# ── Тест 1: каждая точка входа подключает ДОСЛОВНЫЙ bootstrap-блок ──────────


def test_every_entry_point_bootstraps_console_utf8():
    missing = []
    for p in _entry_point_scripts():
        text = p.read_text(encoding="utf-8", errors="replace")
        if _expected_bootstrap_block(p) not in text:
            missing.append(str(p.relative_to(REPO_ROOT)))
    assert not missing, (
        "Точки входа без ДОСЛОВНОГО bootstrap-блока console_utf8.py "
        "(encode-класс, issue #723; см. console_utf8.BOOTSTRAP_BLOCK_SAME_DIR/"
        "_PARENT_LIB — форму пути даёт _expected_bootstrap_block по расположению "
        "файла): " + ", ".join(missing)
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


# ── Тест 3: ни один Path.read_text/write_text/open() не забыл encoding= ─────


def test_no_file_io_without_encoding():
    offenders: dict[str, list[str]] = {}
    for p in SCRIPTS_DIR.rglob("*.py"):
        if p.name in ("console_utf8.py", "test_console_utf8_guard.py"):
            continue
        found = _file_io_calls_missing_encoding(p)
        if found:
            offenders[str(p.relative_to(REPO_ROOT))] = found
    assert not offenders, (
        "Path.read_text()/write_text()/open() без encoding=\"utf-8\" (файловая "
        "дверь того же класса, issue #723, доработка ревью PR #726): "
        f"{offenders}"
    )


# ── Тест 4: живая мутация — pulse_guard.gh() с реалистичным байтом 0x98 ─────


def test_pulse_guard_gh_survives_cyrillic_byte_that_breaks_cp1251(monkeypatch):
    # "И" = U+0418 -> UTF-8 0xD0 0x98: реалистичный, ПРАВИЛЬНО сформированный
    # UTF-8 (не изолированный мусорный байт — иначе упал бы и с encoding=
    # "utf-8", доказывая не то). Именно такой байт (0x98) уронил прод
    # (issue #723): GitHub отвечает валидным UTF-8 всегда, ANSI-кодовая
    # страница локали (cp1251) — нет.
    title = "задача: Иванов проверил приёмку"
    payload = ('{"login": "%s"}' % title).encode("utf-8")
    assert 0x98 in payload, "тест должен реально бить по проблемному байту"

    # Находка ревью PR #726, неблокирующая Н1: эта мутация (снять
    # encoding="utf-8" из pulse_guard.gh) красит ЭТОТ тест, только если
    # дефолтная кодировка процесса (locale.getpreferredencoding) не может
    # декодировать байт 0x98 — на Windows-локали разработчика (cp1251) не
    # может, на ubuntu-latest (обычно UTF-8 по умолчанию) — может, и тогда
    # регрессия молча ускользает мимо ЭТОЙ проверки (decode-дверь на таком
    # раннере держит только статический тест 2). Пробуем декодировать РОВНО
    # тем способом, каким это сделал бы subprocess.run(text=True, encoding=
    # None) — если получится, честно пропускаем себя с названной причиной,
    # а не выдаём проходящий тест за доказательство несуществующего здесь
    # покрытия.
    ambient_encoding = locale.getpreferredencoding(False)
    try:
        payload.decode(ambient_encoding)
    except UnicodeDecodeError:
        pass
    else:
        pytest.skip(
            f"дефолтная кодировка процесса ({ambient_encoding}) и так декодирует "
            "байт 0x98 без явного encoding=\"utf-8\" — на этом раннере мутация "
            "«снять encoding= из pulse_guard.gh» не покрасит этот тест; decode-"
            "дверь здесь держит только статический тест 2 (Н1, ревью PR #726)"
        )

    spec = importlib.util.spec_from_file_location(
        "pulse_guard_console_utf8_check", SCRIPTS_DIR / "orchestra" / "pulse_guard.py")
    pulse_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        return real_run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(%r)" % payload],
            **kwargs,
        )

    monkeypatch.setattr(pulse_guard.subprocess, "run", fake_run)
    result = pulse_guard.gh("user")
    assert result == {"login": title}
