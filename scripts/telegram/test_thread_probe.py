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


def _fail_nth_send(bot_api, status, body, nth=2):
    """Подменяет ответ на N-ю отправку, оставляя вызов НАСТОЯЩИМ."""
    calls = {"n": 0}
    real_call = thread_probe.call

    def counting(token, method, payload):
        if method == "sendMessage":
            calls["n"] += 1
            if calls["n"] == nth:
                bot_api.responses["sendMessage"] = (status, body)
        return real_call(token, method, payload)

    return counting


def test_network_outage_on_create_is_not_called_themes_absent(bot_api):
    """Находка ревью PR #1464: флаг transport заводился ради различения
    «не ответил» и «отказал», а решающая функция его не читала — обрыв сети
    печатал «themes_absent», то есть факт о темах, которого замер не видел."""
    def dead_network(token, method, payload):
        if method == "createForumTopic":
            return {"ok": False, "transport": True, "description": "сеть: timed out"}
        return thread_probe.call(token, method, payload)

    verdict = thread_probe.run("t", "42", thread_probe.Redactor([]),
                               caller=dead_network, out=lambda *_a, **_k: None)

    assert verdict["outcome"] == thread_probe.OUTCOME_UNDETERMINED
    assert "не ответил" in verdict["why"]


def test_rate_limit_on_sending_into_topic_is_not_called_send_broken(bot_api):
    """429 на шаге 3 — это про лимит, а не про #847. Прежний код объявлял
    «#847 воспроизвёлся» и уходил владельцу как факт."""
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 3}})
    caller = _fail_nth_send(bot_api, 429,
                            {"ok": False, "error_code": 429,
                             "description": "Too Many Requests: retry after 5"})

    verdict = thread_probe.run("t", "42", thread_probe.Redactor([]),
                               caller=caller, out=lambda *_a, **_k: None)

    assert verdict["outcome"] == thread_probe.OUTCOME_UNDETERMINED
    assert "429" in verdict["why"]


def test_server_error_on_control_is_not_called_no_access(bot_api):
    """503 на контроле — Telegram лежит, а не «токен не тот»."""
    bot_api.responses["sendMessage"] = (
        503, {"ok": False, "error_code": 503, "description": "Service Unavailable"})

    verdict = _run()

    assert verdict["outcome"] == thread_probe.OUTCOME_UNDETERMINED


def test_undetermined_still_cleans_up_the_topic(bot_api):
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 8}})
    caller = _fail_nth_send(bot_api, 429, {"ok": False, "error_code": 429, "description": "лимит"})

    thread_probe.run("t", "42", thread_probe.Redactor([]),
                     caller=caller, out=lambda *_a, **_k: None)

    assert [m for m, _ in bot_api.seen].count("deleteForumTopic") == 1


def test_plain_bad_request_still_reads_as_a_fact_about_topics(bot_api):
    """Обратная сторона: 400 — это ОТВЕТ про запрос, и превращать его в
    «установить не удалось» значило бы потерять единственный измеренный факт."""
    bot_api.responses["createForumTopic"] = (
        400, {"ok": False, "error_code": 400, "description": "Bad Request: the chat is not a forum"})

    assert _run()["outcome"] == thread_probe.OUTCOME_THEMES_ABSENT


def test_only_established_outcomes_keep_the_run_green():
    """rc 0 — только там, где замер что-то установил."""
    assert thread_probe.ESTABLISHED_OUTCOMES == {
        thread_probe.OUTCOME_WORKS,
        thread_probe.OUTCOME_SEND_BROKEN,
        thread_probe.OUTCOME_THEMES_ABSENT,
    }
    assert thread_probe.OUTCOME_UNDETERMINED not in thread_probe.ESTABLISHED_OUTCOMES
    assert thread_probe.OUTCOME_NO_ACCESS not in thread_probe.ESTABLISHED_OUTCOMES


def test_works_verdict_names_a_refused_edit_instead_of_claiming_it(bot_api):
    """Некритичное замечание ревью PR #1464: строка «правка темы принята»
    была константой — она печаталась бы и при отказе editForumTopic."""
    bot_api.responses["createForumTopic"] = (
        200, {"ok": True, "result": {"message_thread_id": 4}})
    bot_api.responses["editForumTopic"] = (
        400, {"ok": False, "error_code": 400, "description": "Bad Request: TOPIC_NOT_MODIFIED"})

    verdict = _run()

    assert verdict["outcome"] == thread_probe.OUTCOME_WORKS
    assert "правка темы ОТКАЗАНА" in verdict["why"]
    assert "правка темы принята" not in verdict["why"]


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
