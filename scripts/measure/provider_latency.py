#!/usr/bin/env python3
"""Замер латентности живым вызовом по каждому провайдеру-кандидату цепочки
ai-review (issue #836).

Повод: гейт ai-review держит вердикт 30+ минут, и очередь PR не сливается.
Гипотеза владельца — первый провайдер `vars.DSH_PROVIDER_CHAIN` (см.
docs/runbooks/switch-llm-provider.md) самый медленный из цепочки, а
остальные могут отвечать быстрее. Порядок цепочки сегодня переставлен по
доступности квоты (см. врез в раннбуке), не по латентности — этот скрипт
снимает число, а не гадает.

Codex НАМЕРЕННО не включён — владельцу нужен отдельный детект квоты/сбросов/
контекста для него, отдельная задача (не эта).

Один и тот же маленький промпт на каждого кандидата, POST на
`<base_url>/chat/completions` (OpenAI-compatible форма, тот же контракт, что
уже использует dsh_patch_profile в scripts/lib/dsh-ci.sh для этих же
эндпоинтов). Таймаут на КАЖДЫЙ вызов — медленный провайдер не вешает весь
прогон, попадает в таблицу как "timeout". Секреты — только именами
переменных окружения (secret_env), значения никогда не печатаются; тело
ответа провайдера тоже никогда не печатается — из него достаются только
числа/статусы (латентность, HTTP-код, счётчик токенов usage).

Запуск (нужны реальные ключи в окружении):
    python scripts/measure/provider_latency.py
Тесты (без сети, фейковый transport, прод-форма ответов):
    python -m pytest scripts/measure/test_provider_latency.py -q
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
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Кандидаты бенчмарка — base_url/model/secret_env взяты БУКВАЛЬНО из
# постановки задачи (issue #836): те же провайдеры, что уже фигурируют в
# scripts/lib/dsh-ci.sh (PLUGINS_SUITE_CANDIDATE_ROUTES) и в
# docs/runbooks/switch-llm-provider.md (vars.DSH_PROVIDER_CHAIN) — не второе
# место правды о ТЕКУЩЕЙ цепочке (той остаётся vars.DSH_PROVIDER_CHAIN), а
# отдельный список «кого мерить», аналогично PLUGINS_SUITE_CANDIDATE_ROUTES.
# Литералы имён/URL/моделей здесь допущены гвардией provider-default.guard.sh
# точечным allowlist по имени этого файла (тот же приём, что уже применён к
# dsh-ci.sh) — это не зашитый ДЕФОЛТ провайдера (класс #153), а явная таблица
# кандидатов для измерения, ключ отбора которых — фактическое наличие
# секрета в окружении, как и у PLUGINS_SUITE_CANDIDATE_ROUTES.
PROVIDER_LATENCY_CANDIDATES = [
    {"name": "NVIDIA-nano", "base_url": "https://integrate.api.nvidia.com/v1", "model": "nvidia/nemotron-nano-3-30b-a3b", "secret_env": "NVIDIA_API_KEY"},
    {"name": "NVIDIA-ultra", "base_url": "https://integrate.api.nvidia.com/v1", "model": "nvidia/nemotron-3-ultra-550b-a55b", "secret_env": "NVIDIA_API_KEY"},
    {"name": "Ollama", "base_url": "https://ollama.com/v1", "model": "qwen3-coder:480b-cloud", "secret_env": "OLLAMA_CLOUD_1_API_KEY"},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet-4.6", "secret_env": "OPENROUTER_1_API_KEY"},
    {"name": "GLM", "base_url": "https://api.z.ai/api/coding/paas/v4", "model": "glm-5.3-flash", "secret_env": "DEEPSEEK_API_KEY"},
]

DEFAULT_PROMPT = "Ответь одним словом: OK"
DEFAULT_TIMEOUT_SECS = 180


def extract_usage_tokens(body: dict) -> str:
    """total_tokens из usage — прод-форма OpenAI-compatible ответа
    ({"usage": {"prompt_tokens":.., "completion_tokens":.., "total_tokens":..}}).
    Провайдер не отдаёт usage вовсе (или отдаёт не словарь) — пусто, не 0:
    "нет данных" и "ноль токенов" разные факты (AGENTS.md, fail loud)."""
    if not isinstance(body, dict):
        return ""
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return ""
    total = usage.get("total_tokens")
    if total is None:
        return ""
    return str(total)


def urllib_transport(url: str, headers: dict, payload: bytes, timeout_secs: float):
    """Транспорт по умолчанию — реальный HTTP POST. Возвращает (http_status,
    response_bytes) на успехе; бросает urllib.error.HTTPError/URLError/
    socket.timeout как есть — measure_provider их классифицирует."""
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
        return resp.status, resp.read()


def measure_provider(entry: dict, prompt: str = DEFAULT_PROMPT,
                      timeout_secs: float = DEFAULT_TIMEOUT_SECS,
                      transport=None, key: str | None = None) -> dict:
    """Один замер одного провайдера. transport — точка подмены для тестов
    (сеть не трогается вовсе в юнит-тестах); key — точка подмены секрета для
    тестов (иначе читается из os.environ[entry['secret_env']]).

    Возврат — строка будущей таблицы: name, model, latency_s (float|None),
    status ("success"|"error"|"timeout"), http (int|""), tokens (str),
    note (человекочитаемая причина, БЕЗ тела ответа и БЕЗ значения ключа)."""
    transport = transport or urllib_transport
    name = entry["name"]
    model = entry["model"]
    if key is None:
        key = os.environ.get(entry["secret_env"], "")
    if not key:
        return {"name": name, "model": model, "latency_s": None, "status": "error",
                "http": "", "tokens": "", "note": f"секрет {entry['secret_env']} не задан в окружении"}

    url = entry["base_url"].rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16,
    }).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    start = time.monotonic()
    try:
        http_status, body_bytes = transport(url, headers, payload, timeout_secs)
    except urllib.error.HTTPError as error:
        elapsed = time.monotonic() - start
        # Тело ответа НЕ читаем/не печатаем (AGENTS.md «Секреты» + постановка
        # задачи: значения ключей/тел ответов не печатать) — только код.
        return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "error",
                "http": error.code, "tokens": "", "note": "HTTP-ошибка (тело не печатается)"}
    except (socket.timeout, TimeoutError):
        elapsed = time.monotonic() - start
        return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "timeout",
                "http": "", "tokens": "", "note": f"нет ответа за {timeout_secs:g}с"}
    except urllib.error.URLError as error:
        elapsed = time.monotonic() - start
        reason = getattr(error, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "timeout",
                    "http": "", "tokens": "", "note": f"нет ответа за {timeout_secs:g}с"}
        return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "error",
                "http": "", "tokens": "", "note": "сетевая ошибка (не HTTP, не таймаут)"}

    elapsed = time.monotonic() - start
    try:
        body = json.loads(body_bytes)
    except (ValueError, TypeError):
        return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "error",
                "http": http_status, "tokens": "", "note": "тело ответа не JSON"}

    return {"name": name, "model": model, "latency_s": round(elapsed, 2), "status": "success",
            "http": http_status, "tokens": extract_usage_tokens(body), "note": ""}


def sort_rows(rows: list[dict]) -> list[dict]:
    """По латентности возрастающе — быстрый провайдер первым. Строка без
    измеренной латентности (секрет не задан вовсе, замер даже не стартовал)
    уходит в конец, а не путается с настоящими нулевыми латентностями."""
    return sorted(rows, key=lambda r: r["latency_s"] if r["latency_s"] is not None else float("inf"))


def format_markdown_table(rows: list[dict]) -> str:
    header = "| Провайдер | Модель | Латентность, с | Статус | HTTP | Tokens | Заметка |"
    sep = "|---|---|---|---|---|---|---|"
    lines = [header, sep]
    for r in rows:
        latency = "-" if r["latency_s"] is None else f"{r['latency_s']:.2f}"
        http = "" if r["http"] == "" else str(r["http"])
        lines.append(f"| {r['name']} | {r['model']} | {latency} | {r['status']} | {http} | {r['tokens']} | {r['note']} |")
    return "\n".join(lines)


def run_benchmark(prompt: str = DEFAULT_PROMPT, timeout_secs: float = DEFAULT_TIMEOUT_SECS) -> list[dict]:
    rows = [measure_provider(entry, prompt=prompt, timeout_secs=timeout_secs)
            for entry in PROVIDER_LATENCY_CANDIDATES]
    return sort_rows(rows)


def main() -> int:
    prompt = os.environ.get("PROVIDER_LATENCY_PROMPT") or DEFAULT_PROMPT
    timeout_secs = float(os.environ.get("PROVIDER_LATENCY_TIMEOUT_SECS") or DEFAULT_TIMEOUT_SECS)

    rows = run_benchmark(prompt=prompt, timeout_secs=timeout_secs)
    table_md = format_markdown_table(rows)

    print(f"Замер латентности LLM-провайдеров ({datetime.now(timezone.utc).isoformat()}), "
          f"промпт={prompt!r}, таймаут={timeout_secs:g}с")
    print(table_md)

    # Провайдер без ответа/с ошибкой — «timeout»/«error» в таблице, не провал
    # всего прогона (постановка задачи #836): бенчмарк обязан вернуться с
    # данными по ВСЕМ провайдерам, даже если часть из них недоступна.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"## Латентность LLM-провайдеров (issue #836)\n\n")
            fh.write(f"Промпт: `{prompt}`. Таймаут на вызов: {timeout_secs:g}с. "
                     f"Снято: {datetime.now(timezone.utc).isoformat()}.\n\n")
            fh.write(table_md + "\n")
    else:
        print("::warning::GITHUB_STEP_SUMMARY не задан — таблица только в логе шага", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
