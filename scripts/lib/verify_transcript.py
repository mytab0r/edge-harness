#!/usr/bin/env python3
"""Проверка рендера транскрипта сессии раннера в морде dsh-edge (#131).

Место правды по форме событий — SessionEventMap (@deepseek-ai/dsh-session
0.1.1-rc.2), закреплено research/12. Владелец наблюдал два дефекта на демо-
сессии («provider/model — заглушка», «детали тула — unavailable»); оба
объясняются формой батча, не сетью, поэтому проверяются здесь структурно, а
не «на глаз» (пост-мерж пункт runner-sessions-in-dsh-morde/tasks.md, #131).

1. Каждое ``assistant/message`` обязано нести непустые
   ``message.source.provider``/``.model`` — панель сообщения морды рендерит
   ровно эти поля (``AssistantProvenance``, `@deepseek-ai/dsh-llm`
   `message.d.ts`). Пустое значение — «провайдер/модель — заглушка».

2. Каждый ``tool/call`` (журнальное событие: ``turn, step, callId, name,
   arguments`` — SessionEventMap) обязан находить парный content-блок
   ``{type:'tool-call', id==callId}`` в каком-нибудь ``assistant/message``
   того же прогона (``ContentBlockMap``, `@deepseek-ai/dsh-llm` `types.d.ts`).
   Нативный рендер тула (`@deepseek-ai/dsh-client-ui-tool`) строит «детали
   тула» ИЗ этого блока — ``tool/call`` сам по себе в производную историю чата
   не входит (research/12, «Форма канонических событий»: tool/call —
   журнальный тип, не поверхностный). Без парного блока владелец видит
   «unavailable».

Чистая логика без сети/файлов — тестируется списком событий напрямую.
Формат входа CLI — JSON-массив событий вида ``{"type": "...", "data": {...}}``,
как отдаёт replay-эндпоинт морды (``GET /api/sessions/:id/events``, SSE
``data: {...}`` построчно) после извлечения JSON из каждой строки.
"""
from __future__ import annotations

import json
import sys


def _assistant_messages(events):
    """Yield (event_data, message) for every assistant/message event."""
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "assistant/message":
            continue
        data = event.get("data") or {}
        message = data.get("message") or {}
        yield data, message


def check_provider_model(events: list) -> list:
    """Issue per assistant/message whose message.source lacks provider/model."""
    issues = []
    for data, message in _assistant_messages(events):
        where = f"turn={data.get('turn')}, step={data.get('step')}"
        source = message.get("source") or {}
        if source.get("kind") != "model":
            issues.append(
                f"assistant/message ({where}) has message.source.kind="
                f"{source.get('kind')!r}, expected 'model' — panel cannot show provider/model"
            )
            continue
        provider = source.get("provider")
        model = source.get("model")
        if not isinstance(provider, str) or not provider.strip():
            issues.append(
                f"assistant/message ({where}) has an empty message.source.provider "
                "— panel will show a stub"
            )
        if not isinstance(model, str) or not model.strip():
            issues.append(
                f"assistant/message ({where}) has an empty message.source.model "
                "— panel will show a stub"
            )
    return issues


def _tool_call_ids_in_content(events) -> set:
    """callId set for every tool-call content block found in any assistant/message."""
    ids = set()
    for _data, message in _assistant_messages(events):
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool-call":
                call_id = block.get("id")
                if call_id is not None:
                    ids.add(call_id)
    return ids


def check_tool_correlation(events: list) -> list:
    """Issue per tool/call without a matching tool-call content block anywhere."""
    issues = []
    known_ids = _tool_call_ids_in_content(events)
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool/call":
            continue
        data = event.get("data") or {}
        call_id = data.get("callId")
        if call_id not in known_ids:
            issues.append(
                f"tool/call {data.get('name')!r} (callId={call_id!r}, turn={data.get('turn')}, "
                f"step={data.get('step')}) has no matching tool-call content block in any "
                "assistant/message — tool details will render 'unavailable' (#131)"
            )
    return issues


def find_issues(events: list) -> list:
    """All issues, provider/model first (cheaper to fix, usually a config bug)."""
    return check_provider_model(events) + check_tool_correlation(events)


def main(argv: list) -> int:
    path = argv[1] if len(argv) > 1 else None
    text = open(path, encoding="utf-8").read() if path else sys.stdin.read()
    try:
        events = json.loads(text)
    except json.JSONDecodeError as error:
        print(f"::error::verify_transcript: вход не JSON ({error})", file=sys.stderr)
        return 2
    if not isinstance(events, list):
        print("::error::verify_transcript ожидает JSON-массив событий", file=sys.stderr)
        return 2
    issues = find_issues(events)
    if issues:
        for issue in issues:
            print(f"::warning::транскрипт-проверка (#131): {issue}", file=sys.stderr)
        return 1
    print(f"транскрипт-проверка (#131): {len(events)} событий, дефектов не найдено")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
