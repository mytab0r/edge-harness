#!/usr/bin/env python3
"""Замер латентности живым вызовом по каждому провайдеру-кандидату (issue
#836, доводка ревью #837).

Повод: гейт ai-review держит вердикт 30+ минут, и очередь PR не сливается.
Гипотеза владельца — первый провайдер живой цепочки самый медленный, а
остальные (включая непроверенную ёмкость в секретах — учётки, ни разу не
вызванные живьём) могут отвечать быстрее. Порядок цепочки сегодня переставлен
по доступности квоты, не по латентности — этот скрипт снимает число, а не
гадает.

Codex НАМЕРЕННО не включён — владельцу нужен отдельный детект квоты/сбросов/
контекста для него, отдельная задача (не эта).

Кандидаты собираются программой (не второй хардкод-таблицей — находка ревью
#837, «Одно место правды», AGENTS.md) из ДВУХ уже существующих источников:

1. `build_manifest_candidates()` — боевая цепочка `config/provider-usage.json`
   (потребитель `ai-review`, #823) — те провайдеры, что УЖЕ пробует гейт.
2. `build_plugin_suite_candidates()` — таблица
   `PLUGINS_SUITE_CANDIDATE_ROUTES` в `scripts/lib/dsh-ci.sh` (#215) —
   непроверенная ёмкость, для которой в окружении уже лежит секрет, но
   цепочка её ни разу не вызывала.

Один и тот же промпт на каждого кандидата (реалистичный дифф на ревью, не
"hi" — гейт ревьюит диффы такого размера, и латентность на них решает
порядок, см. `fixtures/sample_review_diff.patch`), POST на
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
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DSH_CI_SH = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
PROVIDER_USAGE_MANIFEST = REPO_ROOT / "config" / "provider-usage.json"
SAMPLE_DIFF_FIXTURE = Path(__file__).with_name("fixtures") / "sample_review_diff.patch"

# Провайдер, подтверждённо мёртвый на генерацию (не на листинг моделей —
# разные эндпоинты, разные факты, issue #798): два живых замера отдали 404
# на completion-пути, PR #834 (подтверждение id в реестре) закрыт как
# not-planned именно по этой причине. Манифест (#823) сегодня всё ещё несёт
# эту запись первым элементом — дрейф от факта закрытия #798/#834, который
# этот бенчмарк не тиражирует: живой замер мёртвого провайдера — потраченный
# впустую таймаут, не число.
DEAD_CANDIDATE_NAMES = {"NVIDIA-nano"}

# У OpenRouter в PLUGINS_SUITE_CANDIDATE_ROUTES (dsh-ci.sh) стоит платная
# модель (contextWindow 200000, "anthropic/claude-sonnet-4.6") — у владельца
# план OpenRouter только free-tier, платная модель на нём вернёт ошибку
# оплаты, а не латентность. Дублировать вторую хардкод-таблицу под "модель
# для бенчмарка" тоже не годится (класс "второе место правды") — вместо
# этого бенчмарк подставляет НЕПОДТВЕРЖДЁННОГО кандидата на бесплатный тариф
# (суффикс ":free") и живым вызовом решает, годится ли он: "не гадать" здесь
# и означает измерить, а не поверить на слово источнику модели для другого
# случая использования. Результат этого самого прогона — единственное
# подтверждение; если ключ провайдера отсутствует, слот пропускается тем же
# путём, что и любой другой candidate без секрета.
OPENROUTER_ROUTE_ALIAS_PREFIX = "openrouter-"
OPENROUTER_FREE_MODEL_OVERRIDE = "deepseek/deepseek-r1:free"

DEFAULT_TIMEOUT_SECS = 180


_PROMPT_INSTRUCTION = "Прочитай дифф ниже и ответь одним словом: OK.\n\n"


def _load_default_prompt() -> str:
    """Реалистичный промпт — короткая инструкция + реальный дифф на ревью
    (~1.5-2к токенов), не строка из двух слов: гейт ai-review ревьюит диффы
    такого размера, и латентность решает порядок цепочки именно на них
    (постановка ревью #837). Фикстуры нет физически (испорченный checkout) —
    короткий промпт с явной пометкой, не молчаливая деградация до "hi"."""
    try:
        diff_text = SAMPLE_DIFF_FIXTURE.read_text(encoding="utf-8")
    except OSError:
        return ("[фикстура scripts/measure/fixtures/sample_review_diff.patch "
                 "не найдена — короткий промпт вместо реалистичного] Ответь одним словом: OK")
    return _PROMPT_INSTRUCTION + diff_text


DEFAULT_PROMPT = _load_default_prompt()


def build_manifest_candidates(consumer: str = "ai-review",
                               manifest_path: Path = PROVIDER_USAGE_MANIFEST) -> list[dict]:
    """Боевая цепочка потребителя `consumer` из манифеста использования
    (config/provider-usage.json, #823) — единственное место правды для
    ТЕКУЩЕЙ цепочки, та же форма {name, base_url, model, secret_env}, что
    уже парсит dsh_load_provider_chain_from_manifest в scripts/lib/dsh-ci.sh.
    Манифеста нет, JSON битый, потребитель/цепочка не найдены — пустой
    список (вызывающий не падает: остальные источники кандидатов остаются),
    а не второй фоллбэк-дефолт."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chain_name = manifest.get("usage", {}).get(consumer)
    chain = manifest.get("chains", {}).get(chain_name) if chain_name else None
    if not isinstance(chain, list):
        return []
    out = []
    for entry in chain:
        if not isinstance(entry, dict):
            continue
        if not {"name", "base_url", "model", "secret_env"} <= entry.keys():
            continue
        if entry["name"] in DEAD_CANDIDATE_NAMES:
            continue
        out.append({"name": entry["name"], "base_url": entry["base_url"],
                     "model": entry["model"], "secret_env": entry["secret_env"]})
    return out


_ROUTE_ARRAY_RE = re.compile(r'PLUGINS_SUITE_CANDIDATE_ROUTES=\((.*?)\n\)', re.DOTALL)
_ROUTE_LINE_RE = re.compile(r'^"([^"]*)"\s*$')


def build_plugin_suite_candidates(dsh_ci_path: Path = DSH_CI_SH) -> list[dict]:
    """Непроверенная ёмкость — таблица `PLUGINS_SUITE_CANDIDATE_ROUTES` в
    scripts/lib/dsh-ci.sh (#215), распарсенная, а не скопированная вторым
    списком (находка ревью #837): каждая строка формата
    "alias|baseURL|apiKeyEnvVar|model|contextWindow|displayName". Модель
    OpenRouter-алиасов переопределяется на неподтверждённого free-tier
    кандидата (см. OPENROUTER_FREE_MODEL_OVERRIDE выше) — источник несёт
    платную модель для другого случая использования (комбо-роутер), не для
    этого бенчмарка. Файла нет/формат не совпал — пустой список, fail loud
    достаётся вызывающему по количеству кандидатов, не молчаливому [] здесь."""
    try:
        text = dsh_ci_path.read_text(encoding="utf-8")
    except OSError:
        return []
    m = _ROUTE_ARRAY_RE.search(text)
    if not m:
        return []
    out = []
    for raw_line in m.group(1).splitlines():
        line_m = _ROUTE_LINE_RE.match(raw_line.strip())
        if not line_m:
            continue
        parts = line_m.group(1).split("|")
        if len(parts) != 6:
            continue
        alias, base_url, secret_env, model, _context_window, display_name = parts
        if alias.startswith(OPENROUTER_ROUTE_ALIAS_PREFIX):
            model = OPENROUTER_FREE_MODEL_OVERRIDE
        out.append({"name": display_name, "base_url": base_url, "model": model, "secret_env": secret_env})
    return out


def build_candidates() -> list[dict]:
    """Полный список кандидатов бенчмарка — боевая цепочка + непроверенная
    ёмкость, без дублей по (base_url, model, secret_env). Порядок:
    боевая цепочка первой (уже подтверждённая рабочей), дальше — кандидаты
    на расширение, в порядке появления в dsh-ci.sh."""
    seen = set()
    out = []
    for entry in build_manifest_candidates() + build_plugin_suite_candidates():
        key = (entry["base_url"], entry["model"], entry["secret_env"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


PROVIDER_LATENCY_CANDIDATES = build_candidates()


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
    if not PROVIDER_LATENCY_CANDIDATES:
        # Оба источника кандидатов (манифест + suite-таблица) отдали пусто —
        # это не «нечего мерить», это сломанный парсинг одного из них: fail
        # loud вместо тихого зелёного прогона по нулю строк.
        print("::error::PROVIDER_LATENCY_CANDIDATES пуст — build_manifest_candidates()/"
              "build_plugin_suite_candidates() не нашли ни одного кандидата "
              "(config/provider-usage.json или scripts/lib/dsh-ci.sh не читаются/не совпал формат)",
              file=sys.stderr)
        return 1

    prompt = os.environ.get("PROVIDER_LATENCY_PROMPT") or DEFAULT_PROMPT
    timeout_secs = float(os.environ.get("PROVIDER_LATENCY_TIMEOUT_SECS") or DEFAULT_TIMEOUT_SECS)
    # Промпт — реалистичный дифф (~1.5-2к токенов), не короткая строка: в лог
    # уходит только длина и превью первой строки, не всё содержимое (шум,
    # AGENTS.md «Секреты» — тело чужого ответа не печатаем, свой длинный
    # промпт по той же причине печатаем усечённым).
    prompt_preview = prompt.splitlines()[0][:80] if prompt else ""

    rows = run_benchmark(prompt=prompt, timeout_secs=timeout_secs)
    table_md = format_markdown_table(rows)

    print(f"Замер латентности LLM-провайдеров ({datetime.now(timezone.utc).isoformat()}), "
          f"промпт={len(prompt)} симв. ({prompt_preview!r}...), таймаут={timeout_secs:g}с, "
          f"кандидатов={len(PROVIDER_LATENCY_CANDIDATES)}")
    print(table_md)

    # Провайдер без ответа/с ошибкой — «timeout»/«error» в таблице, не провал
    # всего прогона (постановка задачи #836): бенчмарк обязан вернуться с
    # данными по ВСЕМ провайдерам, даже если часть из них недоступна.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Латентность LLM-провайдеров (issue #836)\n\n")
            fh.write(f"Промпт: {len(prompt)} символов, начало `{prompt_preview}...` "
                     f"(полный текст — scripts/measure/fixtures/sample_review_diff.patch). "
                     f"Таймаут на вызов: {timeout_secs:g}с. "
                     f"Снято: {datetime.now(timezone.utc).isoformat()}.\n\n")
            fh.write(table_md + "\n")
    else:
        print("::warning::GITHUB_STEP_SUMMARY не задан — таблица только в логе шага", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
