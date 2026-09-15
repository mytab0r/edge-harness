#!/usr/bin/env python3
"""Тесты probe_max_output_tokens.py — фейковый transport, без сети (#1289)."""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import urllib.error

import pytest

_spec = importlib.util.spec_from_file_location(
    "probe_max_output_tokens", Path(__file__).with_name("probe_max_output_tokens.py"))
pmt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pmt)  # type: ignore[union-attr]


def _http_error(code: int, body: bytes):
    def transport(url, headers, payload, timeout_secs):
        raise urllib.error.HTTPError(url, code, "err", {}, __import__("io").BytesIO(body))
    return transport


def test_rejected_with_limit_prod_form():
    # #1062, дословная прод-форма Ollama Cloud (прогон worker.yml 34730173870).
    body = b'{"error":{"message":"max_tokens (5000000) exceeds model\'s maximum output tokens (65536) for model nemotron-3-ultra","type":"invalid_request_error"}}'
    result = pmt.probe_max_output_tokens(
        "https://example.test/v1", "some-model", "k",
        transport=_http_error(400, body))
    assert result["outcome"] == "rejected_with_limit"
    assert result["confirmed_limit"] == 65536
    assert result["http"] == 400


def test_rejected_unknown_form_does_not_guess():
    body = b'{"error":"something else entirely"}'
    result = pmt.probe_max_output_tokens(
        "https://example.test/v1", "some-model", "k",
        transport=_http_error(400, body))
    assert result["outcome"] == "rejected_unknown_form"
    assert result["confirmed_limit"] is None


def test_accepted_is_not_treated_as_confirmation():
    def transport(url, headers, payload, timeout_secs):
        return 200, b'{"choices":[{"message":{"content":"ok"}}]}'
    result = pmt.probe_max_output_tokens(
        "https://example.test/v1", "some-model", "k", transport=transport)
    assert result["outcome"] == "accepted"
    assert result["confirmed_limit"] is None


def test_timeout():
    def transport(url, headers, payload, timeout_secs):
        raise TimeoutError()
    result = pmt.probe_max_output_tokens(
        "https://example.test/v1", "some-model", "k", transport=transport)
    assert result["outcome"] == "timeout"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
