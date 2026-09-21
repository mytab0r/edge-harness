#!/usr/bin/env python3
"""Гвардия «харнес пишет только то, что апстримный формат принимает» (#1404).

Класс, оплаченный проданным инцидентом. `scripts/dsh-hands-streamer/lib/core.js`
дописывал в `data` события поля `truncated`/`original_size`, объявляя усечение.
Коллизии проверялись со СЛОВАРЁМ событий (`SessionEventMap`) — и не были
найдены; с ВАЛИДАТОРОМ формата никто не сверялся. А схема v0 у `data`
закрытая: `assertReleasedEventPayload`
(`@deepseek-ai/dsh-session-format-v0-to-v1`) знает для `tool/result` ровно
`{turn, step, message}` плюс опциональные `{error, meta}`, для `tool/call` —
`{turn, step, callId, name, arguments}` БЕЗ опциональных, для
`assistant/message` — `{turn, step, message}` плюс `{usage, interrupted}`.
Любой лишний член → `data has unexpected member` → НЕ мигрирует вся сессия
целиком.

Цена измерена, а не предположена: первый же прогон механизма #1379 (деплой
35571833914, 2026-09-21T07:13Z) показал 165 карантинных сессий прода, из них
**127 с причиной `unexpected member "truncated"`** — 77% карантина породили
две строки этого файла.

Гвардия ПОВЕДЕНЧЕСКАЯ и кормится прод-формой: берётся НАСТОЯЩИЙ
`projectEventData` из нашего кода, его выход отдаётся НАСТОЯЩЕМУ апстримному
валидатору того самого пакета, который отказывал в проде. Ни пересказа схемы,
ни своей копии правил — чужой формат проверяет его собственный код
(AGENTS.md: «Тест кормит прод-форму данных, а не пересказ»).

Запуск: python -m pytest scripts/lib/test_streamer_upstream_format.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
CORE = ROOT / "scripts" / "dsh-hands-streamer" / "lib" / "core.js"

# Пин — тот же приём, что у остальных внешних зависимостей репозитория: версия
# названа, а не «последняя». Апстрим сменит схему — гвардия покраснеет на
# ОБНОВЛЕНИИ пина, осознанно, а не однажды ночью сама по себе.
VALIDATOR_PKG = "@deepseek-ai/dsh-session-format-v0-to-v1"
VALIDATOR_VERSION = "0.1.3-alpha.2"
CACHE = ROOT / "node_modules"  # .gitignore уже несёт node_modules/

needs_node = pytest.mark.skipif(shutil.which("node") is None or shutil.which("npm") is None,
                                reason="нужны настоящие node и npm")


@pytest.fixture(scope="session")
def validator():
    """Ставит пакет апстрима, если его нет. Провал установки — ПАДЕНИЕ, не
    пропуск: «не смогли проверить» и «проверили, всё хорошо» лечатся
    по-разному, и тихий skip здесь вернул бы ровно тот класс, который эта
    гвардия и держит."""
    target = CACHE / VALIDATOR_PKG.replace("/", "/")
    if not (CACHE / "@deepseek-ai" / "dsh-session-format-v0-to-v1" / "package.json").exists():
        result = subprocess.run(
            ["npm", "install", "--no-save", "--no-package-lock", "--no-audit", "--no-fund", "--silent",
             f"{VALIDATOR_PKG}@{VALIDATOR_VERSION}"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, (
            f"не удалось поставить {VALIDATOR_PKG}@{VALIDATOR_VERSION} — гвардия НЕ выполнена "
            f"(это не «всё хорошо»): {result.stderr[-2000:]}")
    assert (CACHE / "@deepseek-ai" / "dsh-session-format-v0-to-v1" / "package.json").exists(), (
        "пакет апстрима не появился после установки")
    return str(target)


def validate(event_type: str, data: dict) -> tuple[bool, str]:
    """Прогоняет `data` через `projectEventData` НАШЕГО кода, а результат —
    через апстримный `assertReleasedEventPayload`. Возвращает (принято, текст
    отказа)."""
    script = f"""
import {{ projectEventData }} from {json.dumps(str(CORE))}
import {{ assertReleasedEventPayload }} from {json.dumps(VALIDATOR_PKG)}
const type = {json.dumps(event_type)}
const input = {json.dumps(data, ensure_ascii=False)}
const {{ data: projected }} = projectEventData(type, input)
try {{
  assertReleasedEventPayload({{ type, seq: 7, data: projected }}, 0)
  console.log(JSON.stringify({{ ok: true, message: '' }}))
}} catch (error) {{
  console.log(JSON.stringify({{ ok: false, message: String(error && error.message || error) }}))
}}
"""
    path = ROOT / "node_modules" / ".streamer-upstream-probe.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    return answer["ok"], answer["message"]


def assistant(text: str) -> dict:
    return {"turn": 1, "step": 1, "message": {
        "id": "msg-a", "role": "assistant", "content": [{"type": "text", "text": text}],
        "source": {"kind": "model", "provider": "test", "model": "test"}}}


def tool_call(arguments: str) -> dict:
    return {"turn": 1, "step": 1, "callId": "call-1", "name": "bash", "arguments": arguments}


def tool_result(text: str) -> dict:
    return {"turn": 1, "step": 1, "message": {
        "id": "msg-t", "role": "user",
        "content": [{"type": "tool-result", "toolCallId": "call-1",
                     "content": [{"type": "text", "text": text}]}],
        "source": {"kind": "tool", "callId": "call-1"}}}


@needs_node
@pytest.mark.parametrize("event_type,make,size", [
    ("assistant/message", assistant, 48_000 + 500),
    ("tool/call", tool_call, 16_000 + 500),
    ("tool/result", tool_result, 16_000 + 500),
])
def test_truncated_event_still_passes_the_real_upstream_validator(validator, event_type, make, size):
    """Главная сцена #1404: событие, которое харнес РЕАЛЬНО УСЕК, обязано
    проходить апстримную проверку. Прежняя редакция здесь получала
    `data has unexpected member "truncated"` — и вместе с ней не мигрировала
    вся сессия."""
    ok, message = validate(event_type, make("я" * size))
    assert ok, f"{event_type}: усечённое событие отвергнуто апстримом — {message}"


@needs_node
@pytest.mark.parametrize("event_type,make", [
    ("assistant/message", assistant),
    ("tool/call", tool_call),
    ("tool/result", tool_result),
])
def test_untouched_event_passes_too(validator, event_type, make):
    """Контроль: короткое событие проходило и раньше. Без него «прошло»
    у предыдущего теста не доказывало бы, что дело в усечении."""
    ok, message = validate(event_type, make("коротко"))
    assert ok, f"{event_type}: неусечённое событие отвергнуто апстримом — {message}"


@needs_node
def test_the_fact_of_truncation_is_not_lost_it_moved_into_the_text(validator):
    """Fail loud остаётся fail loud: усечение по-прежнему ЗАЯВЛЕНО, просто не
    лишним членом `data`, а внутри самого усечённого текста. Мутация «резать
    молча» красит этот тест — иначе лечение класса #1404 свелось бы к тому,
    что журнал перестал врать ЦЕНОЙ молчания."""
    script = f"""
import {{ projectEventData }} from {json.dumps(str(CORE))}
const input = {json.dumps(assistant("я" * 48_500), ensure_ascii=False)}
const {{ data, truncated, originalSize }} = projectEventData('assistant/message', input)
console.log(JSON.stringify({{
  text: data.message.content[0].text.slice(-80),
  truncated, originalSize,
  extraMembers: Object.keys(data).filter((k) => !['turn', 'step', 'message'].includes(k)),
}}))
"""
    path = ROOT / "node_modules" / ".streamer-notice-probe.mjs"
    path.write_text(script, encoding="utf-8")
    result = subprocess.run(["node", str(path)], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout.strip().splitlines()[-1])

    assert "обрезано харнесом" in answer["text"], answer["text"]
    assert "48500" in answer["text"], answer["text"]
    assert answer["truncated"] is True, answer
    assert answer["originalSize"] == 48_500, answer
    assert answer["extraMembers"] == [], (
        f"в data появились лишние члены — ровно класс #1404: {answer['extraMembers']}")
