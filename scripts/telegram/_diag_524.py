#!/usr/bin/env python3
"""ВРЕМЕННЫЙ диагностический скрипт (issue #524) — снять вместе с шагом в
telegram-webhook.yml, который его вызывает, перед мержем PR.

Проверяет, снимается ли 403 у urllib.request сменой ТОЛЬКО заголовка
User-Agent (без переезда на другой HTTP-клиент) — отличает «дело в заголовке»
от «дело в TLS-профиле интерпретатора» (curl тем же секретом с тем же job
получает 400, см. предыдущий шаг того же workflow).
"""

import os
import urllib.error
import urllib.request

url = os.environ["HARNESS_URL"] + "/api/messages/ingest"
secret = os.environ["TELEGRAM_WEBHOOK_SECRET"]

for label, extra_headers in (
    ("default urllib UA", {}),
    ("curl-like UA", {"User-Agent": "curl/8.9.1"}),
    ("browser-like UA", {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}),
):
    headers = {
        **extra_headers,
        "X-Telegram-Bot-Api-Secret-Token": secret,
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, method="POST", headers=headers, data=b"{}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"{label}: HTTP {resp.status} {resp.read()[:200]!r}")
    except urllib.error.HTTPError as error:
        print(f"{label}: HTTP {error.code} {error.read()[:200]!r}")
