#!/usr/bin/env python3
"""Гвардия: конвейер не шлёт `thinking: enabled` без `budget_tokens` (#1533).

Проверяется СОБРАННЫЙ профиль, а не текст `dsh-ci.sh`: структурная гвардия
(«строка `reasoningEffort` встречается в исходнике») покрасилась бы зелёным и
на закомментированной строке, и на значении вне набора, и на голом `off`,
который YAML читает булевым — то есть ровно на трёх способах вернуть дефект
(AGENTS.md, «Поведенческий тест находит то, чего структурный не видит»).

Где есть настоящий `dsh`, гвардия идёт дальше и смотрит на живой исходящий
запрос: профиль — это наше намерение, а проверять надо то, что уходит в сеть.

Запуск: python -m pytest scripts/lib/test_thinking_budget_guard.py -q
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
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
DSH_CI = REPO / "scripts" / "lib" / "dsh-ci.sh"

#: Значения, при которых плагин сериализует `thinking: {"type": "enabled"}`.
#: Взяты из его же кода (`resolveThinking`), а не придуманы: `off` — единственное,
#: дающее `disabled`; `undefined` резолвится в `high` (lib/types/config.d.ts:
#: «omitted reasoning effort resolves to `high`»).
EFFORTS_THAT_ENABLE_THINKING = ("low", "high", "max")


def build_profile(tmp_path: Path) -> dict:
    """Собрать профиль НАСТОЯЩИМ `dsh_patch_profile` и прочитать его как YAML.

    Через настоящий bash и настоящую функцию: перепечатать её логику в тесте —
    это пересказ, и он остался бы зелёным, когда разойдётся с оригиналом."""
    env = dict(os.environ)
    env.update(HOME=str(tmp_path), DEEPSEEK_BASE_URL="https://example.invalid/v1",
               DEEPSEEK_MODEL="проверочная-модель", DEEPSEEK_API_KEY="k")
    result = subprocess.run(
        ["bash", "-c",
         f'source "{DSH_CI}" >/dev/null 2>&1; dsh_patch_profile headless'],
        cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"dsh_patch_profile упал: {result.stderr}"
    patches = list(tmp_path.rglob("cordis.patch.yml"))
    assert patches, f"профиль не написан: {result.stdout}\n{result.stderr}"
    return {entry["id"]: entry.get("config", {})
            for entry in yaml.safe_load(patches[0].read_text(encoding="utf-8"))}


def test_built_profile_turns_reasoning_effort_off(tmp_path):
    """Собранный профиль обязан нести `reasoningEffort: "off"` СТРОКОЙ.

    Строкой — не придирка: YAML 1.1 читает голый `off` как булево `False`,
    схема плагина ждёт значение из набора `off/low/high/max`, и неверное
    значение вернуло бы `thinking: enabled` тем же путём, который чинится."""
    config = build_profile(tmp_path)["llm-deepseek"]

    assert "reasoningEffort" in config, (
        "усилие рассуждения не задано — плагин возьмёт умолчание `high` и "
        "пошлёт `thinking: enabled` без `budget_tokens`; провайдер отвергнет "
        "КАЖДЫЙ вызов (#1533, перехват #1525, прогон 36010507733)")
    assert config["reasoningEffort"] == "off", (
        f"получено {config['reasoningEffort']!r} — ожидалась строка 'off'. "
        "Булево False здесь означает голый `off` в YAML без кавычек")


@pytest.mark.parametrize("effort", EFFORTS_THAT_ENABLE_THINKING)
def test_efforts_that_enable_thinking_are_not_what_we_write(effort, tmp_path):
    """Ни одно из значений, включающих рассуждение, в профиль попасть не может.

    Отдельно от проверки выше: та ловит пропажу строки, эта — подмену значения
    на «почти то же». Пока плагин не шлёт `budget_tokens`, любое из них —
    невалидный запрос, а не настройка качества."""
    assert build_profile(tmp_path)["llm-deepseek"]["reasoningEffort"] != effort


def test_plugin_still_omits_budget_tokens_so_the_workaround_is_still_needed():
    """Газ проверяется, а не обещается.

    Комментарий в `dsh-ci.sh` обещает вернуть рассуждение, «когда апстрим
    научится слать `budget_tokens`». Признак этого — появление ключа в
    сериализаторе плагина. Пока его нет, обход обязателен; как только появится,
    этот тест покраснеет и напомнит снять обход, а не оставит его навсегда.

    Плагина нет на диске (repo-ci его не ставит) — тест пропускается честно:
    «не смог посмотреть» не то же самое, что «проверено»."""
    roots = [Path("/opt/node22/lib/node_modules/@deepseek-ai/dsh/node_modules"
                  "/@deepseek-ai/dsh-llm-deepseek/lib/index.js")]
    source = next((p for p in roots if p.exists()), None)
    if source is None:
        pytest.skip("плагин dsh-llm-deepseek не установлен в этом окружении — "
                    "проверить сериализатор нечем")

    text = source.read_text(encoding="utf-8", errors="replace")
    assert "budget_tokens" not in text, (
        "апстрим начал слать `budget_tokens` — обход `reasoningEffort: off` в "
        "dsh_patch_profile больше не нужен, сними его и верни рассуждение "
        "(#1533, там же названа цена обхода)")


def test_live_dsh_sends_thinking_disabled(tmp_path):
    """Сквозная проверка: что уходит в сеть, а не что написано в профиле.

    Поднимается настоящий сервер, `dsh` идёт на него как на провайдера, и
    проверяется ТЕЛО запроса. Профиль — намерение; тело — факт, и между ними
    успел вклиниться ровно один дефект (#1533).

    `dsh` не установлен (repo-ci его не ставит) — пропуск, а не зелёный."""
    if shutil.which("dsh") is None:
        pytest.skip("dsh не установлен в этом окружении — сквозную проверку "
                    "делает прогон dsh-capture.yml (#1525)")

    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(json.loads(
                self.rfile.read(int(self.headers["content-length"]))))
            payload = b'{"type":"message","role":"assistant","content":[]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ)
    env.update(HOME=str(tmp_path),
               DEEPSEEK_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
               DEEPSEEK_MODEL="проверочная-модель",
               DEEPSEEK_API_KEY="local-guard-placeholder-not-a-secret",
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    try:
        subprocess.run(
            ["bash", "-c",
             f'source "{DSH_CI}" >/dev/null 2>&1; dsh_patch_profile headless'],
            cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8")
        subprocess.run(["dsh", "--profile", "headless", "ping"], cwd=REPO, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired:
        pass
    finally:
        server.shutdown()

    assert bodies, "dsh не сделал ни одного запроса — проверять нечего"
    for body in bodies:
        thinking = body.get("thinking")
        valid = (thinking is None
                 or thinking == {"type": "disabled"}
                 or (isinstance(thinking, dict)
                     and thinking.get("type") == "enabled"
                     and isinstance(thinking.get("budget_tokens"), int)))
        assert valid, (
            f"в сеть ушло thinking={thinking!r} — это и есть запрос, который "
            "провайдер отвергает с «expected number, received undefined» на "
            "пути thinking.budget_tokens (#1533). Годны три состояния: поля "
            "нет вовсе, disabled, либо enabled С числовым budget_tokens — "
            "последнее станет возможным, когда апстрим научится его слать")
