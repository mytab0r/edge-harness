#!/usr/bin/env python3
"""Тесты scripts/lib/verify_transcript.py (#131).

Фикстуры сконструированы по вокабуляру ContentBlockMap/AssistantProvenance
(@deepseek-ai/dsh-llm 0.1.1-rc.2, types.d.ts/message.d.ts) — то же прод-форма
данных, что кормит настоящий ingest (dsh-edge/patches/0004-harness-ingest.patch).

Мутация (доказано вручную): убрать content-блок tool-call из «хорошего»
ассистентского сообщения в test_good_batch_has_no_issues — тест красится
issue из check_tool_correlation; убрать message.source.provider — issue из
check_provider_model. Оба инварианта проверены раздельно и вместе.

Запуск: python -m pytest scripts/lib/test_verify_transcript.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("verify_transcript.py")
spec = importlib.util.spec_from_file_location("verify_transcript", SCRIPT)
verify_transcript = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_transcript)


def assistant_message(turn, step, content, provider="deepseek-official", model="glm-5.3-flash"):
    return {
        "type": "assistant/message",
        "data": {
            "turn": turn,
            "step": step,
            "message": {
                "id": f"a{turn}-{step}",
                "role": "assistant",
                "content": content,
                "source": {"kind": "model", "provider": provider, "model": model},
            },
        },
    }


def tool_call(turn, step, call_id, name="bash", arguments='{"command":"echo hi"}'):
    return {
        "type": "tool/call",
        "data": {"turn": turn, "step": step, "callId": call_id, "name": name, "arguments": arguments},
    }


def tool_result(turn, step, call_id, text="hi"):
    return {
        "type": "tool/result",
        "data": {
            "turn": turn,
            "step": step,
            "message": {
                "id": f"t-{call_id}",
                "role": "user",
                "content": [{"type": "tool-result", "toolCallId": call_id, "isError": False,
                             "content": [{"type": "text", "text": text}]}],
                "source": {"kind": "tool", "callId": call_id},
            },
        },
    }


def test_good_batch_has_no_issues():
    # Ассистентское сообщение НЕСЁТ content-блок tool-call — та форма, которую
    # ожидает нативный рендер тула (@deepseek-ai/dsh-client-ui-tool).
    events = [
        {"type": "turn/start", "data": {"turn": 1}},
        assistant_message(1, 1, [
            {"type": "reasoning", "text": "думаю"},
            {"type": "tool-call", "id": "c1", "name": "bash", "arguments": '{"command":"echo hi"}'},
        ]),
        tool_call(1, 1, "c1"),
        tool_result(1, 1, "c1"),
        {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]
    assert verify_transcript.find_issues(events) == []


def test_tool_call_without_content_block_is_flagged():
    # Форма dsh-edge/ingest-integration/check.mjs (батч 1): assistant/message
    # без tool-call блока, tool/call отдельным журнальным событием — ровно то,
    # что research/12 отметило «не подтверждено» и владелец увидел как
    # «unavailable» в деталях тула.
    events = [
        assistant_message(1, 1, [{"type": "reasoning", "text": "думаю"}, {"type": "text", "text": "делаю"}]),
        tool_call(1, 1, "c1"),
        tool_result(1, 1, "c1"),
    ]
    issues = verify_transcript.check_tool_correlation(events)
    assert len(issues) == 1
    assert "c1" in issues[0]
    assert "unavailable" in issues[0]
    assert verify_transcript.check_provider_model(events) == []


def test_empty_provider_or_model_is_flagged():
    events = [assistant_message(1, 1, [{"type": "text", "text": "hi"}], provider="", model="runner-model")]
    issues = verify_transcript.check_provider_model(events)
    assert len(issues) == 1
    assert "provider" in issues[0]


def test_missing_model_source_kind_is_flagged():
    events = [{
        "type": "assistant/message",
        "data": {"turn": 1, "step": 1, "message": {"id": "a1", "role": "assistant",
                                                     "content": [], "source": {"kind": "tool", "callId": "x"}}},
    }]
    issues = verify_transcript.check_provider_model(events)
    assert len(issues) == 1
    assert "kind=" in issues[0]


def test_non_dict_and_unknown_events_are_ignored():
    events = ["not-a-dict", {"type": "turn/start", "data": {"turn": 1}}, {"type": "session/title", "data": {}}]
    assert verify_transcript.find_issues(events) == []


def test_multiple_assistant_messages_share_correlation_pool():
    # tool/call в шаге 2 находит свой content-блок в другом assistant/message
    # того же прогона — корреляция ищется по всему батчу, не по одному шагу.
    events = [
        assistant_message(1, 1, [{"type": "text", "text": "первый ход"}]),
        assistant_message(1, 2, [{"type": "tool-call", "id": "c2", "name": "read", "arguments": "{}"}]),
        tool_call(1, 2, "c2", name="read"),
        tool_result(1, 2, "c2"),
    ]
    assert verify_transcript.find_issues(events) == []


def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text("[]", encoding="utf-8")
    assert verify_transcript.main(["verify_transcript.py", str(good)]) == 0

    bad = tmp_path / "bad.json"
    bad.write_text(
        '[{"type": "tool/call", "data": {"turn": 1, "step": 1, "callId": "c1", "name": "bash", "arguments": "{}"}}]',
        encoding="utf-8",
    )
    assert verify_transcript.main(["verify_transcript.py", str(bad)]) == 1
    captured = capsys.readouterr()
    assert "unavailable" in captured.err

    not_json = tmp_path / "not-json.txt"
    not_json.write_text("not json", encoding="utf-8")
    assert verify_transcript.main(["verify_transcript.py", str(not_json)]) == 2

    not_list = tmp_path / "not-list.json"
    not_list.write_text('{"events": []}', encoding="utf-8")
    assert verify_transcript.main(["verify_transcript.py", str(not_list)]) == 2
