#!/usr/bin/env python3
"""Живой замер потолка output-токенов ОДНОЙ записи провайдер/модель (#1289).

Повод: реестр `scripts/lib/confirmed-provider-models.json` требует, чтобы
`confirmed_max_output_tokens` был подтверждён ТЕМ ЖЕ способом, что уже принят
для соседних записей — либо документированной OpenAPI-схемой (не всегда
доступна публично, живая проверка #1289 показала: `build.nvidia.com/<id>/
api/openapi.json` отдаёт SPA-шелл, а не JSON, для Nemotron-3 Ultra/Super),
либо живой ошибкой `INVALID_REQUEST` при заведомо завышенном `max_tokens`
(тот же приём, каким #1062 подтвердил потолок Ollama Cloud, см. её evidence в
confirmed-provider-models.json — дословный текст "...exceeds model's maximum
output tokens (N)..."). Этот скрипт — переиспользуемый носитель ВТОРОГО способа: один
живой POST на `<base_url>/chat/completions` с намеренно завышенным
`max_tokens`, разбор числа из текста ошибки (НЕ печатает тело ответа целиком
— только извлечённое число, тот же принцип, что provider_latency.py и
provider_model_discovery.py уже применяют к секретам/телам ответов).

Запуск (нужен реальный ключ в окружении):
    python scripts/measure/probe_max_output_tokens.py <base_url> <model> <secret_env> [max_tokens]
Тесты (без сети, фейковый transport, прод-форма ответов):
    python -m pytest scripts/measure/test_probe_max_output_tokens.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request

# Заведомо больше любого реального потолка вывода известных провайдеров этого
# репозитория (крупнейший подтверждённый на 2026-09-15 — 235929, OpenRouter
# top_provider.max_completion_tokens) — цель не сгенерировать столько токенов
# (запрос обязан отвергнуть его ДО генерации валидацией параметров), а
# вынудить провайдера назвать реальный потолок в тексте отказа.
DEFAULT_PROBE_MAX_TOKENS = 5_000_000
DEFAULT_TIMEOUT_SECS = 30

# Разбирает ТОЛЬКО число из уже известных прод-форм текста отказа (#1062,
# живая цитата Ollama Cloud: "max_tokens (131072) exceeds model's maximum
# output tokens (65536)") — не печатает и не возвращает остальной текст тела,
# даже при неизвестном формате (безопасный по умолчанию отказ распознавания,
# не жадный regex по всему телу).
_LIMIT_RE = re.compile(r"maximum output tokens \((\d+)\)")


def urllib_transport(url: str, headers: dict, payload: bytes, timeout_secs: float):
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
        return resp.status, resp.read()


def probe_max_output_tokens(base_url: str, model: str, key: str,
                             probe_max_tokens: int = DEFAULT_PROBE_MAX_TOKENS,
                             timeout_secs: float = DEFAULT_TIMEOUT_SECS,
                             transport=None) -> dict:
    """Один POST с завышенным max_tokens. Возврат: {"outcome", "http",
    "confirmed_limit" (int|None), "note"} — "outcome" один из:
    "rejected_with_limit" (провайдер назвал число — используй его),
    "rejected_unknown_form" (отказ, но текст не совпал с известной формой),
    "accepted" (200 — потолок НЕ подтверждён этим вызовом, странно для
    заведомо завышенного значения, честно помечается), "error"/"timeout"."""
    transport = transport or urllib_transport
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "OK"}],
        "max_tokens": probe_max_tokens,
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        http_status, body_bytes = transport(url, headers, payload, timeout_secs)
    except urllib.error.HTTPError as error:
        body_text = ""
        try:
            body_text = error.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        m = _LIMIT_RE.search(body_text)
        if m:
            return {"outcome": "rejected_with_limit", "http": error.code,
                    "confirmed_limit": int(m.group(1)), "note": ""}
        return {"outcome": "rejected_unknown_form", "http": error.code,
                "confirmed_limit": None,
                "note": "отказ, но текст ошибки не совпал с известной формой «maximum output tokens (N)» — тело не печатается"}
    except (socket.timeout, TimeoutError):
        return {"outcome": "timeout", "http": "", "confirmed_limit": None,
                "note": f"нет ответа за {timeout_secs:g}с"}
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"outcome": "timeout", "http": "", "confirmed_limit": None,
                    "note": f"нет ответа за {timeout_secs:g}с"}
        return {"outcome": "error", "http": "", "confirmed_limit": None,
                "note": "сетевая ошибка (не HTTP, не таймаут)"}
    # 200 с заведомо завышенным max_tokens — провайдер либо не валидирует
    # параметр строго (потолок этим вызовом НЕ подтверждён), либо реально
    # начал генерацию (дороже, чем ожидалось) — оба варианта честно
    # помечаются как "accepted", не выдаются за подтверждение.
    return {"outcome": "accepted", "http": http_status, "confirmed_limit": None,
            "note": f"провайдер принял max_tokens={probe_max_tokens} без отказа — потолок НЕ подтверждён этим вызовом"}


def main() -> int:
    if len(sys.argv) < 4:
        print("использование: probe_max_output_tokens.py <base_url> <model> <secret_env> [probe_max_tokens]", file=sys.stderr)
        return 2
    base_url, model, secret_env = sys.argv[1], sys.argv[2], sys.argv[3]
    probe_max_tokens = int(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_PROBE_MAX_TOKENS
    key = os.environ.get(secret_env, "")
    if not key:
        print(f"::error::секрет {secret_env} не задан в окружении", file=sys.stderr)
        return 1
    result = probe_max_output_tokens(base_url, model, key, probe_max_tokens=probe_max_tokens)
    print(f"probe_max_output_tokens: base_url={base_url} model={model!r} "
          f"probe_max_tokens={probe_max_tokens} -> outcome={result['outcome']} "
          f"http={result['http']} confirmed_limit={result['confirmed_limit']} note={result['note']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
