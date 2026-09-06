"""Тесты register_webhook.py (issue #490). Telegram/морда мокаются на уровне
urllib.request.urlopen — тело и статус ответа собираются в форме, которую
реально отдают API (HTTPError для 4xx/401, обычный ответ для 200), а не в
виде пересказа."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))

import register_webhook as rw  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, body: dict | None):
        self.status = status
        self._body = json.dumps(body).encode() if body is not None else b""

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status: int, body: dict) -> urllib.error.HTTPError:
    payload = json.dumps(body).encode()
    error = urllib.error.HTTPError(
        url="https://example.invalid", code=status, msg="err", hdrs=None, fp=io.BytesIO(payload)
    )
    return error


# ── probe_route ──────────────────────────────────────────────────────────────


def test_probe_route_ready_on_need_source_msg_id():
    with patch("urllib.request.urlopen", side_effect=_http_error(400, {"error": "need_source_msg_id"})):
        assert rw.probe_route("https://harness.example", "s3cr3t") == "ready"


def test_probe_route_not_ready_on_401_missing_pr486():
    with patch("urllib.request.urlopen", side_effect=_http_error(401, {"error": "unauthorized"})):
        assert rw.probe_route("https://harness.example", "s3cr3t") == "not_ready"


def test_probe_route_not_ready_on_401_wrong_secret():
    # Тот же код, что «маршрут ещё не задеплоен» — намеренно неразличимо (см. докстрок).
    with patch("urllib.request.urlopen", side_effect=_http_error(401, {"error": "unauthorized"})):
        assert rw.probe_route("https://harness.example", "wrong") == "not_ready"


def test_probe_route_unexpected_status():
    with patch("urllib.request.urlopen", side_effect=_http_error(500, {"error": "internal"})):
        assert rw.probe_route("https://harness.example", "s3cr3t") == "unexpected:500"


def test_probe_route_sends_secret_header_and_empty_object():
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["header"] = request.get_header("X-telegram-bot-api-secret-token")
        captured["data"] = request.data
        raise _http_error(400, {"error": "need_source_msg_id"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rw.probe_route("https://harness.example", "the-secret")
    assert captured["header"] == "the-secret"
    assert captured["data"] == b"{}"


def test_request_overrides_default_urllib_user_agent():
    """Регрессия issue #524: дефолтный `Python-urllib/…` от urllib.request
    ловит Cloudflare error 1010 (Browser Integrity Check) перед воркером на
    `*.workers.dev` раньше, чем запрос доходит до кода — засвидетельствовано
    живым прогоном из GitHub Actions (curl тем же секретом — 400, дефолтный
    urllib — 403, urllib с любым не-дефолтным User-Agent — снова 400).
    Убери переопределение из `_request()` — тест обязан покраснеть."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["user_agent"] = request.get_header("User-agent")
        raise _http_error(400, {"error": "need_source_msg_id"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rw.probe_route("https://harness.example", "the-secret")
    assert captured["user_agent"] is not None
    assert not captured["user_agent"].startswith("Python-urllib")


# ── set_webhook / get_webhook_info ──────────────────────────────────────────


def test_set_webhook_ok():
    with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"ok": True, "result": True})):
        rw.set_webhook("bot-token", "https://harness.example", "s3cr3t")  # не должно бросить


def test_set_webhook_raises_on_not_ok():
    with patch(
        "urllib.request.urlopen",
        return_value=FakeResponse(200, {"ok": False, "description": "Bad Request: bad url"}),
    ):
        try:
            rw.set_webhook("bot-token", "https://harness.example", "s3cr3t")
        except RuntimeError as error:
            assert "Bad Request" in str(error)
        else:
            raise AssertionError("ожидалось RuntimeError")


def test_set_webhook_error_message_never_contains_token_or_secret():
    with patch(
        "urllib.request.urlopen",
        return_value=FakeResponse(200, {"ok": False, "description": "boom"}),
    ):
        try:
            rw.set_webhook("very-secret-bot-token", "https://harness.example", "very-secret-webhook-token")
        except RuntimeError as error:
            text = str(error)
            assert "very-secret-bot-token" not in text
            assert "very-secret-webhook-token" not in text


def test_get_webhook_info_returns_result():
    result = {"url": "https://harness.example/api/messages/ingest", "pending_update_count": 0}
    with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"ok": True, "result": result})):
        assert rw.get_webhook_info("bot-token") == result


def test_get_webhook_info_raises_when_not_ok():
    with patch("urllib.request.urlopen", return_value=FakeResponse(200, {"ok": False})):
        try:
            rw.get_webhook_info("bot-token")
        except RuntimeError:
            pass
        else:
            raise AssertionError("ожидалось RuntimeError")


# ── already_registered / verify ──────────────────────────────────────────────


def test_already_registered_true_when_url_matches_and_no_error():
    info = {"url": "https://harness.example/api/messages/ingest", "last_error_message": ""}
    assert rw.already_registered(info, "https://harness.example/api/messages/ingest") is True


def test_already_registered_false_when_url_differs():
    info = {"url": "https://old.example/api/messages/ingest"}
    assert rw.already_registered(info, "https://harness.example/api/messages/ingest") is False


def test_already_registered_false_when_last_error_present():
    info = {
        "url": "https://harness.example/api/messages/ingest",
        "last_error_message": "wrong response from the webhook: 401 Unauthorized",
    }
    assert rw.already_registered(info, "https://harness.example/api/messages/ingest") is False


def test_verify_passes_on_clean_info():
    info = {"url": "https://harness.example/api/messages/ingest", "pending_update_count": 0}
    rw.verify(info, "https://harness.example/api/messages/ingest")  # не бросает


def test_verify_raises_on_url_mismatch():
    info = {"url": "https://wrong.example/api/messages/ingest", "pending_update_count": 0}
    try:
        rw.verify(info, "https://harness.example/api/messages/ingest")
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидалось RuntimeError")


def test_verify_raises_on_missing_pending_update_count():
    info = {"url": "https://harness.example/api/messages/ingest"}
    try:
        rw.verify(info, "https://harness.example/api/messages/ingest")
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидалось RuntimeError")


def test_verify_raises_on_last_error_message():
    info = {
        "url": "https://harness.example/api/messages/ingest",
        "pending_update_count": 0,
        "last_error_message": "SSL error",
    }
    try:
        rw.verify(info, "https://harness.example/api/messages/ingest")
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидалось RuntimeError")


# ── main(): сквозные сценарии ────────────────────────────────────────────────


def _env(**overrides):
    base = {
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_WEBHOOK_SECRET": "the-secret",
        "HARNESS_URL": "https://harness.example",
    }
    base.update(overrides)
    return base


def test_main_missing_env_fails_loud_on_manual_dispatch(capsys):
    with patch.dict(os.environ, {"FAIL_ON_NOT_READY": "true"}, clear=True):
        assert rw.main() == 1
    assert "::error::" in capsys.readouterr().err


def test_main_missing_env_warns_and_exits_zero_on_auto_trigger(capsys):
    with patch.dict(os.environ, {"FAIL_ON_NOT_READY": "false"}, clear=True):
        assert rw.main() == 0
    assert "::warning::" in capsys.readouterr().err


def test_main_route_not_ready_fails_on_manual_dispatch(capsys):
    env = _env(FAIL_ON_NOT_READY="true")
    with patch.dict(os.environ, env, clear=True), patch(
        "urllib.request.urlopen", side_effect=_http_error(401, {"error": "unauthorized"})
    ):
        assert rw.main() == 1
    assert "PR #486" in capsys.readouterr().err


def test_main_route_not_ready_warns_on_auto_trigger(capsys):
    env = _env(FAIL_ON_NOT_READY="false")
    with patch.dict(os.environ, env, clear=True), patch(
        "urllib.request.urlopen", side_effect=_http_error(401, {"error": "unauthorized"})
    ):
        assert rw.main() == 0
    assert "::warning::" in capsys.readouterr().err


def test_main_already_registered_short_circuits(capsys):
    env = _env()
    responses = [
        _http_error(400, {"error": "need_source_msg_id"}),  # probe_route
        FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "url": "https://harness.example/api/messages/ingest",
                    "pending_update_count": 0,
                },
            },
        ),  # getWebhookInfo — уже как надо
    ]

    def fake_urlopen(request, timeout=None):
        next_item = responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert rw.main() == 0
    assert not responses, "setWebhook не должен вызываться повторно (идемпотентность)"


def test_main_full_registration_flow(capsys):
    env = _env()
    responses = [
        _http_error(400, {"error": "need_source_msg_id"}),  # probe_route
        FakeResponse(200, {"ok": True, "result": {"url": "", "pending_update_count": 0}}),  # getWebhookInfo (до)
        FakeResponse(200, {"ok": True, "result": True}),  # setWebhook
        FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "url": "https://harness.example/api/messages/ingest",
                    "pending_update_count": 3,
                },
            },
        ),  # getWebhookInfo (после)
    ]

    def fake_urlopen(request, timeout=None):
        next_item = responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert rw.main() == 0
    out = capsys.readouterr().out
    assert "зарегистрирован" in out
    assert not responses


def test_main_never_prints_bot_token_or_secret_anywhere(capsys):
    # Литералы короче 20 символов нарочно (scripts/review/check_pr.py::
    # SECRET_PATTERNS матчит `TOKEN=`/`SECRET=` с кавычкой на 20+ символов —
    # тестовая фикстура не должна сама выглядеть как утечка секрета).
    env = _env(
        TELEGRAM_BOT_TOKEN="leak-guard-tok-01",
        TELEGRAM_WEBHOOK_SECRET="leak-guard-sec-01",
    )
    responses = [
        _http_error(400, {"error": "need_source_msg_id"}),
        FakeResponse(200, {"ok": True, "result": {"url": "", "pending_update_count": 0}}),
        FakeResponse(200, {"ok": True, "result": True}),
        FakeResponse(
            200,
            {
                "ok": True,
                "result": {
                    "url": "https://harness.example/api/messages/ingest",
                    "pending_update_count": 0,
                },
            },
        ),
    ]

    def fake_urlopen(request, timeout=None):
        next_item = responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item

    with patch.dict(os.environ, env, clear=True), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        rw.main()
    captured = capsys.readouterr()
    assert "leak-guard-tok-01" not in captured.out
    assert "leak-guard-tok-01" not in captured.err
    assert "leak-guard-sec-01" not in captured.out
    assert "leak-guard-sec-01" not in captured.err
