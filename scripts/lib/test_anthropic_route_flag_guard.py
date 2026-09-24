#!/usr/bin/env python3
"""Гвардия: запись без Anthropic-маршрута не тратит попытку прогона (#1524).

Проверяется НАСТОЯЩАЯ `dsh_load_provider_chain_from_manifest` на настоящих
манифестах во временных файлах — не текст `dsh-ci.sh`. Структурная проверка
(«в исходнике есть `anthropic_route`») осталась бы зелёной и при вырезанном
теле фильтра, и при потерянном сообщении о пропуске — а сообщение тут
обязательно: молча ужавшаяся цепочка неотличима от цепочки, которую так и
задумали (AGENTS.md, fail loud).

Запуск: python -m pytest scripts/lib/test_anthropic_route_flag_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import subprocess

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
DSH_CI = REPO / "scripts" / "lib" / "dsh-ci.sh"
LIVE_MANIFEST = REPO / "config" / "provider-usage.json"


def load_chain(manifest: Path) -> tuple[list[str], str]:
    """Прогнать настоящую функцию загрузки и вернуть (имена записей, вывод)."""
    result = subprocess.run(
        ["bash", "-c",
         f'source "{DSH_CI}" >/dev/null 2>&1; '
         f'DSH_PROVIDER_USAGE_MANIFEST="{manifest}" '
         'dsh_load_provider_chain_from_manifest ai-review; '
         'printf "CHAIN:%s\\n" "$DSH_PROVIDER_CHAIN"'],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    output = result.stdout + result.stderr
    line = next((l for l in result.stdout.splitlines() if l.startswith("CHAIN:")), "")
    payload = line[len("CHAIN:"):]
    names = [e["name"] for e in json.loads(payload)] if payload.strip() else []
    return names, output


def write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest = tmp_path / "usage.json"
    manifest.write_text(json.dumps({"usage": {"ai-review": "c"}, "chains": {"c": entries}}),
                        encoding="utf-8")
    return manifest


def entry(name: str, **extra) -> dict:
    return {"name": name, "base_url": "https://example.invalid/v1",
            "model": "m", "secret_env": "K", **extra}


def test_flagged_entry_is_skipped_and_named(tmp_path):
    """Запись с `anthropic_route: false` выбрасывается, и её имя звучит.

    Имя обязательно: цепочка, ужавшаяся молча, читается как «так и задумано»,
    и следующий агент будет искать пропавшего провайдера в логах прогона, а не
    в манифесте."""
    manifest = write_manifest(tmp_path, [
        entry("ЖИВАЯ"), entry("МЁРТВАЯ", anthropic_route=False)])

    names, output = load_chain(manifest)

    assert names == ["ЖИВАЯ"], "мёртвая запись осталась бы тратить попытку прогона"
    assert "МЁРТВАЯ" in output, "пропуск обязан называть, КОГО пропустили"
    assert "anthropic_route" in output, (
        "сообщение обязано называть газ — как вернуть запись в строй "
        "(AGENTS.md, «тормоз без газа не принимается»)")


def test_entry_without_the_flag_stays(tmp_path):
    """Отсутствие флага — это «маршрут не проверяли», а не «мёртв».

    Иначе одна опечатка в имени поля вычистила бы всю цепочку."""
    names, _ = load_chain(write_manifest(tmp_path, [entry("A"), entry("B")]))

    assert names == ["A", "B"]


@pytest.mark.parametrize("value", [True, "false", 0, None])
def test_only_literal_false_disables_an_entry(value, tmp_path):
    """Выключает ровно `false` булевым, а не «что-то похожее».

    Строка `"false"`, ноль и `null` — разные вещи, и трактовать их как отказ
    значило бы выбросить запись по опечатке в манифесте."""
    names, _ = load_chain(write_manifest(tmp_path, [entry("A", anthropic_route=value)]))

    assert names == ["A"]


def test_chain_of_only_dead_entries_fails_loudly(tmp_path):
    """Все записи мертвы — это отказ с текстом, а не пустая цепочка молча.

    Пустая цепочка дальше по коду выглядит как «провайдеров не назначили», и
    лечится совсем иначе, чем «все назначенные без маршрута»."""
    names, output = load_chain(write_manifest(tmp_path, [
        entry("A", anthropic_route=False), entry("B", anthropic_route=False)]))

    assert names == []
    assert "пуста" in output, "молчаливая пустая цепочка — silent-wrong"


def test_live_manifest_keeps_at_least_one_entry():
    """Боевой манифест после всех флагов не должен оказаться пустым.

    Проверяется он сам, а не выдуманный: флаг ставится по замеру, и однажды
    замер может выключить последнюю живую запись — тогда конвейер встанет, и
    узнать об этом надо здесь, а не из красного прогона ai-review."""
    names, output = load_chain(LIVE_MANIFEST)

    assert names, f"боевая цепочка после фильтра пуста:\n{output}"
