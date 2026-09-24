#!/usr/bin/env python3
"""Транзитивный пин dsh проверяется по диску, а не по намерению (#1467).

Класс одной фразой: **пины держат два верхних пакета, а их зависимости
резолвятся каждый прогон заново — публикация в ЧУЖОМ реестре меняет то, что
стоит у нас, без единого коммита.**

Живой инцидент 2026-09-22: `dsh` начал падать с «user patch-layer watching
requires the Cordis HMR service» ДО обращения к провайдеру. Конвейер слияний
встал: ни один PR не мог получить `ai:ok`, восемь провайдеров цепочки
«отказывали» одинаково, а итог советовал повтор, который не мог помочь
никогда.

Первый диагноз назвал виновником `@deepseek-ai/cordis 4.0.4` по совпадению
дат публикации и ОПРОВЕРГНУТ (#1481): пин закрепил 4.0.3, падение осталось
тем же. Настоящая причина — несовместимость `dsh-app-boot@0.1.1-rc.2` с
любой опубликованной версией `cordis-plugin-hmr`. Сам класс, ради которого
эта гвардия написана, от опровержения не исчез: транзитивная зависимость
по-прежнему может уехать от чужой публикации без коммита у нас, и проверять
это надо по диску.

`--before` — просьба к npm. Доказательство — версия на диске, и его даёт
`dsh_verify_resolved_deps`. Здесь проверяется сама проверка.

Почему pytest, а не bash-файл рядом с bash-кодом. Так вышло дешевле всего
по числу мест: гейт заявления о мутации в теле PR (ADR 0026) знает ровно
форму `python -m pytest`, а гвардия осиротевших тестов требует, чтобы КАЖДЫЙ
файл теста запускался workflow напрямую. Два bash-файла (стенд + мост)
удовлетворить обе можно было только дублирующим запуском; один pytest-файл,
запускающий НАСТОЯЩИЙ bash через subprocess, удовлетворяет обе честно.

Проверка остаётся поведенческой: настоящий каталог на диске, настоящий
`node` внутри проверяемой функции, настоящий `source` прод-файла. Разбор
исходника вместо запуска зеленел бы на вырезанном теле (AGENTS.md,
«Поведенческий тест находит то, чего структурный не видит»).

Запуск: python -m pytest scripts/lib/test_dsh_transitive_pin_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DSH_CI = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"

# Дословная выдержка из живого прогона ai-review 35752743682 (#1467) — прод-форма,
# а не наш пересказ того, как это могло бы выглядеть.
CRASH_STDERR_HEAD = (
    "file:///opt/hostedtoolcache/node/24.20.0/x64/lib/node_modules/@deepseek-ai/dsh/"
    "node_modules/@deepseek-ai/dsh-app-boot/lib/index.js:764"
)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")


def _npm_root(tmp_path, cordis_version: str | None) -> Path:
    """Настоящий каталог с настоящим package.json — его читает настоящий node."""
    root = tmp_path / "npmroot"
    (root / "@deepseek-ai" / "cordis").mkdir(parents=True)
    if cordis_version is not None:
        (root / "@deepseek-ai" / "cordis" / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/cordis", "version": cordis_version}),
            encoding="utf-8")
    return root


def _run_check(npm_root: Path) -> subprocess.CompletedProcess:
    """Зовёт НАСТОЯЩУЮ dsh_verify_resolved_deps, подменив только `npm root -g`.

    Имя переменной в заглушке — не `root`: у bash динамическая область
    видимости, и `local root` внутри проверяемой функции перекрыл бы
    переменную стенда (заглушка вернула бы пустую строку, а стенд «нашёл» бы
    несуществующий дефект). Поймано исполнением на первом же прогоне."""
    script = (
        f'source "{DSH_CI}"\n'
        'npm() { [ "${1:-}" = "root" ] && { printf \'%s\\n\' "$STUB_NPM_ROOT"; return 0; }; '
        'command npm "$@"; }\n'
        "dsh_verify_resolved_deps\n"
    )
    return subprocess.run(
        ["bash", "-c", script], cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "STUB_NPM_ROOT": str(npm_root),
             "HOME": str(npm_root)},
        capture_output=True, text=True, encoding="utf-8")


def _pinned(name: str) -> str:
    match = re.search(rf'^{name}="([^"]+)"$', DSH_CI.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{name} не объявлен в dsh-ci.sh — проверять нечего"
    return match.group(1)


@needs_bash
def test_expected_version_passes(tmp_path):
    """Версия совпала с пином — проверка молчит и выходит нулём."""
    result = _run_check(_npm_root(tmp_path, _pinned("DSH_EXPECTED_CORDIS")))

    assert result.returncode == 0, result.stderr
    assert _pinned("DSH_EXPECTED_CORDIS") in result.stdout


@needs_bash
def test_drifted_version_is_a_loud_refusal(tmp_path):
    """Ровно инцидент #1467: транзитивная зависимость уехала. Сообщение обязано
    нести ОБА числа — что стоит и что ожидалось, — иначе читателю нечем
    решать.

    «Уехавшая» версия СЧИТАЕТСЯ от ожидаемой, а не пишется числом (#1481):
    прежняя редакция держала литерал `4.0.4`, и когда пин поехал на 4.0.4
    штатно, тест покраснел не потому, что гвардия сломалась, а потому, что
    его собственная константа совпала с ожидаемой. Один источник правды —
    `DSH_EXPECTED_CORDIS` в `dsh-ci.sh`, отсюда только читается."""
    expected = _pinned("DSH_EXPECTED_CORDIS")
    major, minor, patch = (int(part) for part in expected.split(".")[:3])
    drifted = f"{major}.{minor}.{patch + 1}"
    assert drifted != expected  # защита от опечатки в самой арифметике

    result = _run_check(_npm_root(tmp_path, drifted))

    assert result.returncode != 0, "уехавшая зависимость прошла молча — это и есть #1467"
    assert drifted in result.stderr
    assert expected in result.stderr
    assert "#1467" in result.stderr, "сообщение не даёт адрес разбора"


@needs_bash
def test_missing_manifest_is_its_own_message(tmp_path):
    """«Проверить нечем» и «проверили, не то» лечатся по-разному, поэтому и
    сообщения разные (AGENTS.md, «Fail loud, не silent-wrong»)."""
    result = _run_check(_npm_root(tmp_path, None))

    assert result.returncode != 0, "отсутствие манифеста прошло как успех"
    assert "не найден манифест" in result.stderr
    assert "вместо" not in result.stderr, "перепутано с расхождением версий"


def test_pin_and_expected_version_are_declared_together():
    """Разъехавшись, дата пина и ожидаемая версия дают проверку, формально
    зелёную и ничего не держащую."""
    before = _pinned("DSH_RESOLVE_BEFORE")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T", before), f"не похоже на дату: {before}"
    assert re.match(r"^\d+\.\d+\.\d+", _pinned("DSH_EXPECTED_CORDIS"))


def test_both_npm_commands_ask_for_the_pin():
    """Иначе проверка на диске вечно ловила бы дрейф вместо того, чтобы его не
    допускать."""
    source = DSH_CI.read_text(encoding="utf-8")
    assert 'npm pack --before="$DSH_RESOLVE_BEFORE"' in source
    assert 'npm install -g --before="$DSH_RESOLVE_BEFORE"' in source


def test_class_closed_pattern_survives_git_grep():
    """Паттерн блока «Класс закрыт» не должен начинаться с дефисов: `git grep`
    разбирает ведущее `--before` как свою опцию `--before-context` и падает
    («expects a non-negative integer value»). Поймано красным прогоном PR
    #1468, а не догадкой."""
    result = subprocess.run(
        ["git", "grep", "-cE", 'before="\\$DSH_RESOLVE_BEFORE"'],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")

    assert result.returncode == 0, f"git grep не принял паттерн: {result.stderr}"
    assert result.stdout.strip(), "паттерн ничего не находит — блок не исполнится"


def test_incident_evidence_is_recorded_next_to_the_pin():
    """Пин без причины — число, которое следующий агент подвинет не думая.
    Рядом с ним обязан стоять адрес разбора и дословный признак инцидента."""
    source = DSH_CI.read_text(encoding="utf-8")
    assert "#1467" in source
    assert "cordis" in source
    assert CRASH_STDERR_HEAD.split("/")[-1] in source or "Cordis HMR" in source


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
