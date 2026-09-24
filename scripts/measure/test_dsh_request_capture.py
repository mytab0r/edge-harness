#!/usr/bin/env python3
"""Тесты пересылающего рекордера (#1525).

Стенд поднимает НАСТОЯЩИЙ сервер и зовёт рекордер настоящим HTTP-клиентом:
подменять пересылку заглушкой здесь бессмысленно вдвойне — сама пересылка и
есть то, что проверяется (AGENTS.md: «Заглушка внешнего инструмента — это
пересказ»).

Запуск: python -m pytest scripts/measure/test_dsh_request_capture.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import http.server
import json
import threading
import urllib.request

import pytest

_spec = importlib.util.spec_from_file_location(
    "dsh_request_capture", Path(__file__).resolve().parent / "dsh_request_capture.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


#: Формы base_url из боевой цепочки и адрес, на который обязан уйти запрос,
#: пришедший на локальный `/v1/messages`. Правило берётся из зонда #1520 —
#: второй копии здесь нет, и тест ловит как раз расхождение, если она заведётся.
FORWARD_CASES = [
    ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/messages"),
    ("https://api.z.ai/api/coding/paas/v4",
     "https://api.z.ai/api/coding/paas/v4/v1/messages"),
    ("https://ollama.com/v1/", "https://ollama.com/v1/messages"),
]


@pytest.mark.parametrize("base,expected", FORWARD_CASES, ids=[c[0] for c in FORWARD_CASES])
def test_forwarding_uses_the_same_url_rule_as_the_probe(base, expected):
    """Перехват обязан ходить ТЕМ ЖЕ маршрутом, которым ходит замер #1520.

    Разойдись они — отказ провайдера относился бы к другому запросу, и
    сравнивать перехват с таблицей замера стало бы нельзя."""
    assert mod.forward_target("/v1/messages", base) == expected


def test_unexpected_local_path_is_forwarded_as_is_not_silently_dropped():
    """Путь, не начинающийся с `/v1`, пересылается как есть.

    Тихо отбросить его нельзя: `dsh` может пойти на служебный маршрут
    (перечень моделей, например), и рекордер, отвечающий за него сам, показал
    бы читателю разговор, которого не было."""
    assert mod.forward_target("/models", "https://openrouter.ai/api/v1") == (
        "https://openrouter.ai/api/v1/models")


@pytest.fixture
def upstream():
    """Настоящий сервер в роли провайдера: отвечает 400 тем же текстом, которым
    OpenRouter отвечает `dsh`, если в теле есть поле `ломай`, иначе — валидным
    Anthropic-ответом. Так стенд воспроизводит ОБА исхода, а не один."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            seen.append({"path": self.path, "headers": dict(self.headers),
                         "body": json.loads(body)})
            reject = "ломай" in json.loads(body)
            payload = (b'{"type":"error","error":{"type":"invalid_request_error",'
                       b'"message":"Invalid Anthropic Messages API request"}}'
                       if reject else
                       b'{"type":"message","role":"assistant","content":[]}')
            self.send_response(400 if reject else 200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, seen
    server.shutdown()


def _recorder_for(upstream_server, secret="upstream-key-0123456789"):
    mod.Recorder.target_base = f"http://127.0.0.1:{upstream_server.server_port}/v1"
    mod.Recorder.secret = secret
    mod.Recorder.captures = []
    server = http.server.HTTPServer(("127.0.0.1", 0), mod.Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _post(port, payload, headers=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/messages",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"content-type": "application/json",
                 "x-api-key": "stub-key-sent-by-dsh", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def test_rejected_request_and_verbatim_refusal_land_in_one_capture(upstream):
    """То, ради чего рекордер написан: отвергнутый запрос и дословный отказ
    лежат рядом.

    Проверяется на живом отказе, а не на успехе: перехват, поймавший только
    удачные запросы, расследуемого случая не показывает."""
    upstream_server, seen = upstream
    recorder = _recorder_for(upstream_server)
    try:
        status, body = _post(recorder.server_port, {"model": "m", "ломай": True})
    finally:
        recorder.shutdown()

    assert status == 400
    assert "Invalid Anthropic Messages API request" in body, (
        "ответ провайдера обязан доходить до dsh неизменным — иначе перехват "
        "меняет то, что наблюдает")
    (capture,) = mod.Recorder.captures
    assert capture["is_answer"] is False
    assert capture["error_type"] == "invalid_request_error"
    assert json.loads(capture["request_body"])["ломай"] is True
    assert "Invalid Anthropic Messages API request" in capture["response_body"]
    assert seen[0]["path"] == "/v1/messages", (
        "провайдер обязан увидеть тот же путь, что и без рекордера")


def test_recorder_swaps_in_the_real_key_and_never_prints_it(upstream):
    """Ключ, который шлёт `dsh`, — заглушка (локальный адрес секрета не
    требует). Провайдеру обязан уйти настоящий, а в перехват — не уйти никакой:
    репозиторий публичный, и заголовок может нести производное, которое
    маскирование по точному совпадению не ловит."""
    upstream_server, seen = upstream
    recorder = _recorder_for(upstream_server, secret="real-upstream-key-0123456789")
    try:
        _post(recorder.server_port, {"model": "m"})
    finally:
        recorder.shutdown()

    sent = {k.lower(): v for k, v in seen[0]["headers"].items()}
    assert sent["x-api-key"] == "real-upstream-key-0123456789"
    printed = mod.format_capture(mod.Recorder.captures[0], ["real-upstream-key-0123456789"])
    assert "real-upstream-key" not in printed
    assert "x-api-key" not in printed.lower(), (
        "заголовок с ключом не печатается вовсе, а не маскируется")


def test_transport_failure_is_reported_as_such_not_as_a_refusal():
    """Закрытый порт — «возможности нет», а не «провайдер отверг». Разные
    сообщения, потому что лечатся по-разному (AGENTS.md, fail loud).

    «Недоступность» воспроизводится закрытым портом, а не подменой функции."""
    mod.Recorder.target_base = "http://127.0.0.1:1/v1"
    mod.Recorder.secret = "k"
    mod.Recorder.captures = []
    recorder = http.server.HTTPServer(("127.0.0.1", 0), mod.Recorder)
    threading.Thread(target=recorder.serve_forever, daemon=True).start()
    try:
        status, _ = _post(recorder.server_port, {"model": "m"})
    finally:
        recorder.shutdown()

    assert status == 502
    (capture,) = mod.Recorder.captures
    assert capture["status"] == 0
    assert capture["transport_error"], "причина обязана быть названа, а не съедена"
    assert "до провайдера не дошли" in mod.format_capture(capture, [])


def test_hop_by_hop_headers_are_not_forwarded(upstream):
    """`host` и `content-length` от `dsh` относятся к НАШЕМУ серверу. Уйди они
    дальше — запрос попал бы не туда либо разошёлся бы с телом, и перехват
    показал бы поломку, которой в конвейере нет."""
    upstream_server, seen = upstream
    recorder = _recorder_for(upstream_server)
    try:
        _post(recorder.server_port, {"model": "m"},
              headers={"accept-encoding": "gzip"})
    finally:
        recorder.shutdown()

    assert seen[0]["headers"]["Host"] == f"127.0.0.1:{upstream_server.server_port}"
    assert "gzip" not in (seen[0]["headers"].get("Accept-Encoding") or ""), (
        "сжатый ответ пришлось бы распаковывать, чтобы показать дословно — "
        "проще не просить сжатия")
