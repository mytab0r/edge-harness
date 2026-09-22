#!/usr/bin/env python3
"""Замер тредов различает исходы данными, а не догадкой (#1463).

Стенд поднимает НАСТОЯЩИЙ `http.server` и зовёт НАСТОЯЩИЙ `call` — правило
AGENTS.md «Заглушка внешнего инструмента — это пересказ, и она ломается на
исправном коде» (2026-09-19, #1373/PR #1380). Подменив `call` заглушкой, мы
проверяли бы собственное представление о том, как ведёт себя urllib на 400 —
а весь смысл шага 3 замера в том, что Bot API отвечает ошибкой С ТЕЛОМ, и
именно тело несёт ответ. Заглушка это тело просто вернула бы; настоящий
сервер заставляет код действительно достать его из HTTPError.

Недоступность сети воспроизводится закрытым портом, а не подставным
исключением — по тому же правилу.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "thread_probe", Path(__file__).resolve().parent / "thread_probe.py")
thread_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(thread_probe)


class _FakeBotApi(BaseHTTPRequestHandler):
    """Настоящий HTTP-сервер, отвечающий формами РЕАЛЬНОГО Bot API.

    Тела ответов — форма, которую отдаёт Telegram (`{"ok": false,
    "error_code": 400, "description": "..."}` с кодом 400 в статусе), а не
    наш пересказ: правило «Тест кормит прод-форму данных»."""

    responses: dict[str, tuple[int, dict]] = {}
    seen: list[tuple[str, dict]] = []

    def do_POST(self):  # noqa: N802 — имя задано BaseHTTPRequestHandler
        method = self.path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append((method, payload))
        status, body = type(self).responses.get(
            method, (200, {"ok": True, "result": {"message_id": 1}}))
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass  # тишина в выводе теста


@pytest.fixture
def bot_api(monkeypatch):
    """Поднимает сервер, направляет на него API_ROOT, отдаёт ручку настройки."""
    _FakeBotApi.responses = {}
    _FakeBotApi.seen = []
    server = HTTPServer(("127.0.0.1", 0), _FakeBotApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(thread_probe, "API_ROOT", f"http://127.0.0.1:{server.server_port}")
    try:
        yield _FakeBotApi
    finally:
        server.shutdown()
        server.server_close()


def _run(silent=True):
    out = (lambda *_a, **_k: None) if silent else print
    return thread_probe.run("test-token", "42", thread_probe.Redactor([]), out=out)


def test_all_methods_working_means_works(bot_api):
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 777, "name": "тема"}})

    verdict = _run()

    assert verdict["outcome"] == thread_probe.OUTCOME_WORKS
    assert verdict["topic_id"] == 777


def test_create_refused_means_themes_absent(bot_api):
    """Форма ответа — настоящая: 400 со статусом, а не 200 с ok=false."""
    bot_api.responses["createForumTopic"] = (
        400, {"ok": False, "error_code": 400,
              "description": "Bad Request: the chat is not a forum"})

    verdict = _run()

    assert verdict["outcome"] == thread_probe.OUTCOME_THEMES_ABSENT
    assert "not a forum" in verdict["why"]


def test_send_into_topic_refused_is_its_own_outcome(bot_api):
    """Ровно случай tdlib/telegram-bot-api#847: тема есть, доставки нет.
    Слить его с «тем нет» значило бы гадать там, где данные различают."""
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 12}})
    calls = {"n": 0}
    real_call = thread_probe.call

    def counting(token, method, payload):
        if method == "sendMessage":
            calls["n"] += 1
            if calls["n"] == 2:  # второй sendMessage — тот, что в тему
                bot_api.responses["sendMessage"] = (
                    400, {"ok": False, "error_code": 400,
                          "description": "Bad Request: message thread not found"})
        return real_call(token, method, payload)

    verdict = thread_probe.run("t", "42", thread_probe.Redactor([]),
                               caller=counting, out=lambda *_a, **_k: None)

    assert verdict["outcome"] == thread_probe.OUTCOME_SEND_BROKEN
    assert "message thread not found" in verdict["why"]


def test_control_failure_is_not_blamed_on_topics(bot_api):
    """Без контрольного шага «всё отказало» не отличалось бы от «не тот
    токен» — и вывод был бы про темы там, где на самом деле про доступ."""
    bot_api.responses["sendMessage"] = (
        401, {"ok": False, "error_code": 401, "description": "Unauthorized"})

    verdict = _run()

    assert verdict["outcome"] == thread_probe.OUTCOME_NO_ACCESS
    assert "createForumTopic" not in [method for method, _ in bot_api.seen]


def test_probe_cleans_up_the_topic_it_created(bot_api):
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 5}})

    _run()

    deletes = [payload for method, payload in bot_api.seen if method == "deleteForumTopic"]
    assert deletes == [{"chat_id": "42", "message_thread_id": 5}]


def test_cleanup_runs_even_when_sending_into_the_topic_fails(bot_api):
    """Уборка в finally: иначе неудачный замер оставлял бы мусор в чате
    владельца ровно в тот момент, когда что-то и так пошло не так."""
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 9}})
    calls = {"n": 0}
    real_call = thread_probe.call

    def counting(token, method, payload):
        if method == "sendMessage":
            calls["n"] += 1
            if calls["n"] == 2:
                bot_api.responses["sendMessage"] = (
                    400, {"ok": False, "error_code": 400, "description": "нет темы"})
        return real_call(token, method, payload)

    thread_probe.run("t", "42", thread_probe.Redactor([]),
                     caller=counting, out=lambda *_a, **_k: None)

    assert [m for m, _ in bot_api.seen].count("deleteForumTopic") == 1


def test_network_outage_is_transport_not_a_telegram_refusal(monkeypatch):
    """Недоступность — закрытый порт, а не подставное исключение."""
    probe_socket = socket.socket()
    probe_socket.bind(("127.0.0.1", 0))
    closed_port = probe_socket.getsockname()[1]
    probe_socket.close()
    monkeypatch.setattr(thread_probe, "API_ROOT", f"http://127.0.0.1:{closed_port}")

    result = thread_probe.call("t", "getMe", {})

    assert result["ok"] is False
    assert result["transport"] is True


def test_redactor_removes_secret_values_from_output():
    redact = thread_probe.Redactor(["1234567890:AA-секрет", "987654321"])

    assert redact("чат 987654321 токен 1234567890:AA-секрет") == "чат <секрет> токен <секрет>"


def test_redactor_ignores_values_too_short_to_redact_safely():
    """Значение из двух символов вырезало бы половину осмысленного текста —
    лог стал бы нечитаем ровно там, где его читают."""
    assert thread_probe.Redactor(["ok"])("всё ok") == "всё ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
