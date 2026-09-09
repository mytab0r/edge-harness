#!/usr/bin/env python3
"""Discovery живых model id провайдеров-кандидатов (#848, доводка #836/#798).

Повод: `PLUGINS_SUITE_CANDIDATE_ROUTES` (scripts/lib/dsh-ci.sh, #215) несёт
model id, взятые буквально из примера combo-router
(dsh-combo-router/examples/mytab0r.settings.yml), НЕ сверенные с реальным
каталогом моделей владельца. Живой замер #836 (run 34406807344) показал
404/410 на трёх из четырёх новых провайдеров именно поэтому — рунбук
(docs/runbooks/switch-llm-provider.md, «Узнать точный id модели») требует
сверки буква-в-букву с `/v1/models`, но раньше это делалось руками (либо не
делалось вовсе). Этот скрипт — переиспользуемый механизм: список кандидатов
для дискавери берётся из ОДНОГО источника (dsh-ci.sh, тот же парсер, что уже
использует scripts/measure/provider_latency.py), не второй хардкод-таблицей.

Что делает:
1. Группирует записи `PLUGINS_SUITE_CANDIDATE_ROUTES` по семейству провайдера
   (alias без хвостового "-N") — NVIDIA-NIM, Ollama Cloud, OpenRouter.
2. Для каждого аккаунта семейства запрашивает листинг моделей (`GET
   <base_url>/models` — OpenAI-совместимый путь; для Ollama Cloud
   дополнительно пробует нативный `<root>/api/tags`, если `/models` не
   ответил — оба пути документированы противоречиво, рунбук прямо просит
   сверяться, не гадать один раз и забыть).
3. Ранжирует найденные id эвристикой «сильная модель для кода» (см.
   CODING_RANK_KEYWORDS ниже) — для OpenRouter вдобавок фильтрует ТОЛЬКО
   бесплатный тариф (`pricing.prompt == "0" and pricing.completion == "0"`).
4. Верифицирует топ-кандидатов ЖИВЫМ вызовом `chat/completions` (переиспользует
   `provider_latency.measure_provider` — тот же контракт, что уже применяет
   бенчмарк латентности, не второй клиент): пробует несколько кандидатов по
   рангу, пока один не ответит 200 (free-tier часто rate-limited — этого и
   ждём здесь), либо кандидаты не кончатся.

Значения ключей и тела ответов НИКОГДА не печатаются — только id моделей
(публичная информация каталога) и числа/статусы (HTTP-код, число моделей в
листинге). Скрипт НИЧЕГО не пишет в dsh-ci.sh/провайдерные файлы сам —
рекомендация выводится в таблицу (лог + GITHUB_STEP_SUMMARY), применение
живого id — отдельный, осознанный коммит с датой подтверждения (тот же
принцип, что confirmed-provider-models.json, #737).

Запуск (нужны реальные ключи в окружении):
    python scripts/measure/provider_model_discovery.py
Тесты (без сети, фейковый транспорт, прод-форма ответов):
    python -m pytest scripts/measure/test_provider_model_discovery.py -q
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
from datetime import datetime, timezone

_pl_spec = importlib.util.spec_from_file_location(
    "provider_latency", Path(__file__).with_name("provider_latency.py"))
pl = importlib.util.module_from_spec(_pl_spec)
_pl_spec.loader.exec_module(pl)  # type: ignore[union-attr]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DSH_CI_SH = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"

# Ранжирование «сильная модель для кода» по имени id — эвристика для выбора
# СРЕДИ УЖЕ ПОЛУЧЕННОГО ЖИВОГО каталога `/v1/models`, не предположение о том,
# какая модель развёрнута прямо сейчас (не второй дефолт класса #153: список
# используется только внутри discover_route() ниже, ничего не патчит и не
# подставляется как provider chain сам по себе). Аллоулист гвардии —
# scripts/lib/test/provider-default.guard.sh, блок "provider_model_discovery.py".
CODING_RANK_KEYWORDS = (
    "deepseek-r1",
    "deepseek-v3",
    "deepseek-chat",
    "deepseek-coder",
    "qwen3-coder",
    "qwen3-235b",
    "qwen3-32b",
    "qwen2.5-coder",
    "llama-3.3-70b",
    "llama-3.1-405b",
    "nemotron-ultra",
    "nemotron-super",
    "kimi-k2",
    "gpt-oss-120b",
    "mixtral-8x22b",
    "codestral",
)

VERIFY_MAX_ATTEMPTS = 5
VERIFY_TIMEOUT_SECS = 60
VERIFY_PROMPT = "Ответь одним словом: OK."

# Семейства, для которых discovery вообще имеет смысл (#848: NVIDIA-NIM,
# Ollama Cloud, OpenRouter — ровно три провайдера из живого замера #836 с
# ошибочным id). GLM/Z.AI и базовый env-provider сюда не входят — их id уже
# подтверждён отдельным путём (confirmed-provider-models.json, #737) либо не
# входит в эту постановку.
FAMILY_LIST_STRATEGIES = {
    "nvidia-nim": ["openai_models"],
    "ollama-cloud": ["openai_models", "ollama_tags"],
    "openrouter": ["openrouter_free"],
}

_FAMILY_RE = re.compile(r'^(.*)-\d+$')


def family_of(alias: str) -> str:
    m = _FAMILY_RE.match(alias)
    return m.group(1) if m else alias


def build_list_url(base_url: str, strategy: str) -> str:
    base = base_url.rstrip("/")
    if strategy in ("openai_models", "openrouter_free"):
        return base + "/models"
    if strategy == "ollama_tags":
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        return root.rstrip("/") + "/api/tags"
    raise ValueError(f"неизвестная стратегия листинга: {strategy}")


def _urllib_get(url: str, headers: dict, timeout_secs: float):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
        return resp.status, resp.read()


def fetch_listing(url: str, key: str, transport=None, timeout_secs: float = 30) -> dict:
    """GET листинга моделей. Возврат: {"ok": bool, "http": int|"", "parsed":
    <json>|None, "note": str}. Ключ может быть пустым (OpenRouter публично
    отдаёт листинг без авторизации) — заголовок Authorization в этом случае
    просто не добавляется."""
    transport = transport or _urllib_get
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        status, body = transport(url, headers, timeout_secs)
    except urllib.error.HTTPError as error:
        return {"ok": False, "http": error.code, "parsed": None,
                "note": "HTTP-ошибка при листинге (тело не печатается)"}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "http": "", "parsed": None, "note": f"таймаут листинга за {timeout_secs:g}с"}
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"ok": False, "http": "", "parsed": None, "note": f"таймаут листинга за {timeout_secs:g}с"}
        return {"ok": False, "http": "", "parsed": None, "note": "сетевая ошибка листинга (не HTTP, не таймаут)"}
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return {"ok": False, "http": status, "parsed": None, "note": "тело листинга не JSON"}
    return {"ok": True, "http": status, "parsed": parsed, "note": ""}


def extract_openai_style_ids(parsed) -> list[str]:
    """`{"data": [{"id": "..."}]}` — форма OpenAI-совместимого `/v1/models`,
    её же отдают NVIDIA NIM и (предположительно, сверяется этим же вызовом)
    Ollama Cloud OpenAI-путь."""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        return []
    return [d["id"] for d in data if isinstance(d, dict) and isinstance(d.get("id"), str)]


def extract_ollama_tags_ids(parsed) -> list[str]:
    """`{"models": [{"name": "..."}]}` — нативный листинг Ollama (`/api/tags`),
    другая форма от OpenAI-совместимого `/v1/models`."""
    models = parsed.get("models") if isinstance(parsed, dict) else None
    if not isinstance(models, list):
        return []
    return [m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]


def extract_openrouter_free(parsed) -> tuple[list[str], dict]:
    """Та же форма `{"data": [...]}`, что и OpenAI-style, но с `pricing` и
    `context_length` на каждой записи — фильтруем строго бесплатный тариф
    (постановка #848: `pricing.prompt=="0" and pricing.completion=="0"`).
    Возврат: (список free id, {id: context_length})."""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    ids: list[str] = []
    context_by_id: dict[str, int] = {}
    if not isinstance(data, list):
        return ids, context_by_id
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        pricing = entry.get("pricing")
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        if str(pricing.get("prompt")) == "0" and str(pricing.get("completion")) == "0":
            ids.append(model_id)
            context_length = entry.get("context_length")
            if isinstance(context_length, (int, float)):
                context_by_id[model_id] = int(context_length)
    return ids, context_by_id


def rank_all(ids: list[str], context_by_id: dict | None = None) -> list[str]:
    """Все id, отсортированные по приоритету CODING_RANK_KEYWORDS (первое
    совпадение — раньше), внутри одного ключа — по убыванию context_length;
    хвост (без совпадений) — тоже по убыванию context_length."""
    context_by_id = context_by_id or {}
    used: set[str] = set()
    ranked: list[str] = []
    for keyword in CODING_RANK_KEYWORDS:
        tier = sorted(
            (i for i in ids if i not in used and keyword in i.lower()),
            key=lambda i: context_by_id.get(i, 0), reverse=True,
        )
        ranked.extend(tier)
        used.update(tier)
    rest = sorted((i for i in ids if i not in used), key=lambda i: context_by_id.get(i, 0), reverse=True)
    ranked.extend(rest)
    return ranked


def pick_best(ids: list[str], context_by_id: dict | None = None) -> tuple[str | None, str]:
    ranked = rank_all(ids, context_by_id)
    if not ranked:
        return None, "листинг пуст"
    top = ranked[0]
    low = top.lower()
    for keyword in CODING_RANK_KEYWORDS:
        if keyword in low:
            return top, f"совпадение по ключу «{keyword}»"
    if context_by_id:
        return top, "ни один приоритетный ключ не совпал — взята модель с наибольшим контекстным окном"
    return top, "ни один приоритетный ключ не совпал — взята первая модель каталога"


def discover_listing(route: dict, key: str, transport=None) -> dict:
    """Пробует стратегии листинга ПО ПОРЯДКУ для семейства route["family"],
    останавливается на первой, отдавшей непустой список id. Возврат несёт
    strategy/url/ids/context_by_id/note — используется и для рекомендации, и
    для отчёта (сколько моделей увидел этот конкретный аккаунт)."""
    strategies = FAMILY_LIST_STRATEGIES.get(route["family"], [])
    notes = []
    for strategy in strategies:
        url = build_list_url(route["base_url"], strategy)
        resp = fetch_listing(url, key, transport=transport)
        if not resp["ok"]:
            notes.append(f"{strategy} ({url}): {resp['note']} (http={resp['http']})")
            continue
        if strategy == "openai_models":
            ids, context_by_id = extract_openai_style_ids(resp["parsed"]), {}
        elif strategy == "ollama_tags":
            ids, context_by_id = extract_ollama_tags_ids(resp["parsed"]), {}
        elif strategy == "openrouter_free":
            ids, context_by_id = extract_openrouter_free(resp["parsed"])
        else:
            ids, context_by_id = [], {}
        if ids:
            return {"strategy": strategy, "url": url, "ids": ids,
                     "context_by_id": context_by_id, "note": ""}
        notes.append(f"{strategy} ({url}): листинг пуст")
    return {"strategy": None, "url": None, "ids": [], "context_by_id": {},
             "note": "; ".join(notes) if notes else "нет стратегий листинга для этого семейства"}


def verify_candidates(route: dict, ranked_ids: list[str], key: str,
                       max_attempts: int = VERIFY_MAX_ATTEMPTS, measure=None) -> dict:
    """Пробует до max_attempts кандидатов по рангу живым `chat/completions`
    (переиспользует provider_latency.measure_provider — тот же контракт, что
    уже применяет бенчмарк латентности, #836). Останавливается на первом
    успехе (http 200) — free-tier часто rate-limited, поэтому пробуем
    несколько, а не один."""
    measure = measure or pl.measure_provider
    attempts = []
    for candidate in ranked_ids[:max_attempts]:
        entry = {"name": route["display_name"], "base_url": route["base_url"],
                  "model": candidate, "secret_env": route["secret_env"]}
        row = measure(entry, prompt=VERIFY_PROMPT, timeout_secs=VERIFY_TIMEOUT_SECS, key=key)
        attempts.append({"candidate": candidate, "status": row["status"], "http": row["http"], "note": row["note"]})
        if row["status"] == "success":
            return {"verified_model": candidate, "verified": True, "attempts": attempts}
    return {"verified_model": None, "verified": False, "attempts": attempts}


def discover_route(route: dict, transport=None, measure=None) -> dict:
    """Полный цикл для ОДНОГО аккаунта (одной записи PLUGINS_SUITE_CANDIDATE_ROUTES):
    листинг → ранжирование → верификация живым вызовом. Секрет не задан —
    честный отказ без сетевого вызова листинга платно (OpenRouter — исключение,
    его листинг публичный, ключ нужен только для верификации)."""
    key = os.environ.get(route["secret_env"], "")
    result = {"alias": route["alias"], "display_name": route["display_name"],
               "family": route["family"], "secret_present": bool(key)}
    if not key and route["family"] != "openrouter":
        result.update({"strategy": None, "listing_count": 0, "recommended": None,
                        "verified_model": None, "verified": False, "attempts": [],
                        "note": f"секрет {route['secret_env']} не задан — листинг и верификация пропущены"})
        return result

    # Дошли сюда с пустым key ТОЛЬКО когда family == "openrouter" (листинг
    # публичный) — иначе выше уже вернули отказ без сетевого вызова.
    listing = discover_listing(route, key, transport=transport)
    result["strategy"] = listing["strategy"]
    result["listing_count"] = len(listing["ids"])
    if not listing["ids"]:
        result.update({"recommended": None, "verified_model": None, "verified": False,
                        "attempts": [], "note": listing["note"]})
        return result

    ranked = rank_all(listing["ids"], listing["context_by_id"])
    recommended, pick_note = pick_best(listing["ids"], listing["context_by_id"])
    result["recommended"] = recommended
    result["pick_note"] = pick_note

    if not key:
        result.update({"verified_model": None, "verified": False, "attempts": [],
                        "note": f"листинг публичный (без ключа), верификация пропущена — "
                                f"секрет {route['secret_env']} не задан"})
        return result

    verify = verify_candidates(route, ranked, key, measure=measure)
    result.update(verify)
    result["note"] = "" if verify["verified"] else "ни один из опробованных кандидатов не ответил 200"
    return result


def load_routes_by_family(dsh_ci_path: Path = DSH_CI_SH) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for route in pl.parse_plugin_suite_routes(dsh_ci_path):
        fam = family_of(route["alias"])
        if fam not in FAMILY_LIST_STRATEGIES:
            continue
        route = dict(route)
        route["family"] = fam
        grouped.setdefault(fam, []).append(route)
    return grouped


def format_markdown_report(grouped_results: dict[str, list[dict]]) -> str:
    lines = ["| Семья | Аккаунт | Секрет | Листинг | Кол-во | Рекомендация | Верифицировано | HTTP | Заметка |",
              "|---|---|---|---|---|---|---|---|---|"]
    for family, rows in grouped_results.items():
        for row in rows:
            secret = "есть" if row["secret_present"] else "нет"
            strategy = row.get("strategy") or "-"
            count = row.get("listing_count", 0)
            recommended = row.get("recommended") or "-"
            verified_model = row.get("verified_model")
            verified = "да" if row.get("verified") else "нет"
            last_http = row["attempts"][-1]["http"] if row.get("attempts") else ""
            note = row.get("note", "")
            display = verified_model or recommended or "-"
            lines.append(f"| {family} | {row['display_name']} | {secret} | {strategy} | {count} "
                         f"| {display} | {verified} | {last_http} | {note} |")
    return "\n".join(lines)


def main() -> int:
    # DSH_CI_SH передаётся явно (не через дефолт параметра) — дефолт связан
    # один раз при определении функции, monkeypatch модульной переменной в
    # тестах его не видит; явная передача читает актуальное значение на
    # каждый вызов (тот же класс, что уже решён в provider_latency.py через
    # прямое обращение к SAMPLE_DIFF_FIXTURE в теле main()).
    grouped_routes = load_routes_by_family(DSH_CI_SH)
    if not grouped_routes:
        print("::error::discovery не нашёл ни одной записи семейств "
              f"{sorted(FAMILY_LIST_STRATEGIES)} в PLUGINS_SUITE_CANDIDATE_ROUTES "
              f"({DSH_CI_SH}) — формат таблицы изменился?", file=sys.stderr)
        return 1

    grouped_results: dict[str, list[dict]] = {}
    for family, routes in grouped_routes.items():
        grouped_results[family] = [discover_route(route) for route in routes]

    table_md = format_markdown_report(grouped_results)
    print(f"Discovery model id ({datetime.now(timezone.utc).isoformat()}), "
          f"семей: {len(grouped_results)}, аккаунтов: {sum(len(v) for v in grouped_results.values())}")
    print(table_md)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Discovery живых model id провайдеров (issue #848)\n\n")
            fh.write("Скрипт ничего не пишет в dsh-ci.sh сам — применение id "
                     "верифицированного `Верифицировано=да` вручную, с датированным "
                     "комментарием (docs/runbooks/switch-llm-provider.md).\n\n")
            fh.write(f"Снято: {datetime.now(timezone.utc).isoformat()}.\n\n")
            fh.write(table_md + "\n")
    else:
        print("::warning::GITHUB_STEP_SUMMARY не задан — таблица только в логе шага", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
