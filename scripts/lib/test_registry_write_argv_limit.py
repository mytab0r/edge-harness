#!/usr/bin/env python3
"""Гвардия «тело реестра не уезжает в argv» (issue #1406).

Живой инцидент, а не гипотеза: прогон orchestra
[35585768697](https://github.com/mytab0r/edge-harness/actions/runs/35585768697)
(2026-09-21T09:53Z) упал с `OSError: [Errno 7] Argument list too long: 'gh'`
внутри `review_findings.write_registry` — ПОСЛЕ того, как слил PR #1403.
Цикл слияний (#297) оборвался на первом PR, прогон покраснел, слияния
простояли 2 часа 13 минут при двух зелёных mergeable PR.

Арифметика сошлась с точностью до сотен байт: `findings.json` весил 97 771
байт, его base64 — ≈130 364 символа, а потолок ОДНОГО аргумента в Linux
(`MAX_ARG_STRLEN`, 32 страницы по 4 КБ) — 131 072 байта. `ARG_MAX` (2 МБ
суммарно) ни при чём: ломается лимит одного аргумента, поэтому «аргументов
мало» не спасает. Порог необратим — реестр только растёт, значит каждый
следующий проход падал бы надёжнее предыдущего.

Проверка ПОВЕДЕНЧЕСКАЯ и на уровне ядра, а не на пересказе лимита: argv,
который строит `write_registry`, отдаётся НАСТОЯЩЕМУ `execve` через
`subprocess.run`. Ошибка `E2BIG` возникает в ядре ДО того, как процесс
что-либо выполнит, — поэтому исполняемым берётся безобидный `/bin/true`:
проверяется не поведение `gh`, а то, пролезает ли наша командная строка
через exec вообще. Подменой инструмента это не является — инструмент здесь
не участвует в проверяемом поведении (AGENTS.md, «Заглушка внешнего
инструмента — это пересказ»: пересказом был бы код, СЧИТАЮЩИЙ длину
аргумента вместо запуска).

Запуск: python -m pytest scripts/lib/test_registry_write_argv_limit.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_findings as rf  # noqa: E402

# Реестр заведомо крупнее того, на котором сломался прод: 97 771 байт хватило,
# берём с запасом, чтобы гвардия не зависела от точного значения потолка на
# конкретном ядре.
OVERSIZED_BYTES = 200_000

TRUE_BIN = "/bin/true"

needs_posix_exec = pytest.mark.skipif(
    not os.path.exists(TRUE_BIN),
    reason=f"нужен {TRUE_BIN}: гвардия проверяет ограничение exec, а оно не переносимо")


def oversized_registry() -> dict:
    """Реестр прод-формы, раздутый до нужного размера числом находок, а не
    одной гигантской строкой: так растёт настоящий реестр."""
    registry = rf.empty_registry() if hasattr(rf, "empty_registry") else {"version": 1, "findings": []}
    i = 0
    while len(rf.dump_registry(registry)) < OVERSIZED_BYTES:
        i += 1
        rf.add_finding(registry, f"scripts/lib/file_{i}.py", f"находка номер {i}",
                       "подробности находки, приближённые к реальной длине записи реестра" * 3,
                       1000 + i, "2026-09-21T00:00:00Z")
    return registry


def real_exec_gh(*args: str):
    """gh_func, который РЕАЛЬНО зовёт execve с построенным argv. Возвращает
    то же, что настоящий `pulse_guard.gh` вернул бы на пустом ответе."""
    subprocess.run([TRUE_BIN, "api", *args], capture_output=True, check=False)
    return {"content": {"sha": "newsha"}}


@needs_posix_exec
def test_oversized_registry_write_survives_real_exec():
    """Главная сцена #1406. Прежняя редакция здесь получала
    `OSError: [Errno 7] Argument list too long` от ядра — и роняла весь проход
    оркестратора после первого слияния."""
    registry = oversized_registry()
    assert len(rf.dump_registry(registry)) > 150_000, "стенд обязан быть крупнее прод-инцидента"

    rf.write_registry(real_exec_gh, "o/r", registry, "blobsha", "test: большой реестр")


@needs_posix_exec
def test_no_single_argument_carries_the_body():
    """Прямое требование: тело живёт в ФАЙЛЕ, а не в аргументе. Проверяется
    не длиной (длина — следствие), а тем, что среди аргументов нет ни одного,
    несущего содержимое, и что `--input` указывает на существующий файл с
    нужными полями."""
    seen = {}

    def capture(*args):
        seen["args"] = list(args)
        if "--input" in args:
            path = args[args.index("--input") + 1]
            seen["payload"] = json.loads(Path(path).read_text(encoding="utf-8"))
        return {"content": {"sha": "newsha"}}

    registry = oversized_registry()
    rf.write_registry(capture, "o/r", registry, "blobsha", "test: большой реестр")

    longest = max(len(a) for a in seen["args"])
    assert longest < 4096, f"аргумент длиной {longest} байт снова несёт тело: {seen['args']}"
    assert seen["payload"]["message"] == "test: большой реестр"
    assert seen["payload"]["sha"] == "blobsha"
    assert seen["payload"]["branch"] == rf.REGISTRY_BRANCH
    assert len(seen["payload"]["content"]) > 150_000, "содержимое обязано доехать целиком"


@needs_posix_exec
def test_temporary_body_file_is_removed_even_when_the_call_fails():
    """Временный файл — не утечка: неудачный вызов не оставляет мусор в
    /tmp на каждом проходе оркестратора (а проходы идут круглосуточно)."""
    captured = {}

    def boom(*args):
        captured["path"] = args[args.index("--input") + 1]
        raise RuntimeError("gh api: HTTP 409: conflict")

    with pytest.raises(RuntimeError, match="409"):
        rf.write_registry(boom, "o/r", oversized_registry(), "blobsha", "test")

    assert not Path(captured["path"]).exists(), "временный файл тела остался на диске"


def test_no_call_site_puts_file_content_into_an_argument_value():
    """Класс, а не случай: поведенческий тест выше держит ОДИН вызов, а этот —
    правило для всех. Форма проверки здесь СОЗНАТЕЛЬНО текстовая, и это не
    рецидив класса #891/#893: проверяется не «работает ли функция», а
    «существует ли где-нибудь ещё вызов такой формы» — утверждение о
    множестве мест, которое поведением одного места не проверить. Тот же
    приём и по той же причине применяет scripts/lib/test_pagination_guard.py.

    Содержимое файла в значении аргумента (`-f` с полем content) — единственный
    носитель без верхней границы: тела комментариев и issue ограничены самим
    GitHub 65 536 символами, вдвое ниже потолка одного аргумента, и
    структурно недосягаемы."""
    root = Path(__file__).resolve().parent.parent.parent
    # Паттерн собирается конкатенацией СОЗНАТЕЛЬНО: написанный литералом, он
    # нашёл бы сам себя в этом файле, и проверка «ноль совпадений» стала бы
    # невыполнимой — то же и у заявления «## Класс закрыт» в теле PR, которое
    # гейт ищет по всему дереву.
    pattern = 'f' + '"content='
    found = subprocess.run(["git", "grep", "-nE", pattern],
                           cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert found.returncode in (0, 1), found.stderr
    offenders = [line for line in found.stdout.splitlines() if line.strip()]

    assert offenders == [], (
        "содержимое файла снова уходит значением аргумента — класс #1406 вернулся:\n"
        + "\n".join(offenders))
