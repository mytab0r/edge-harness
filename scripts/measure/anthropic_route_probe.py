#!/usr/bin/env python3
"""Чем именно провайдер не устраивает запрос `dsh` — спросить у провайдера.

Класс одной фразой: **`dsh` ходит формой Anthropic Messages API, записи нашей
цепочки — OpenAI-совместимые эндпоинты, и почему конкретно провайдер отвергает
запрос, никто не спрашивал.**

Живой замер (#1494, `docs/research/28-provider-chain-truth-table.md`): пять
записей цепочки из восьми отвечают HTTP 200 на прямой вызов
`<base_url>/chat/completions` и те же пять не отвечают через `dsh`. Почему —
неизвестно, потому что тело ответа провайдера в логи не попадает ни у одной из
сторон (`EMPTY_RESPONSE` у цепочки, «тело не печатается» у bench).

Что уже установлено и не переспрашивается (#1502):

* правило URL взято из исходника плагина, не угадано —
  `messagesApiRoot(baseURL)` дописывает `/v1` только если путь им ещё не
  заканчивается, дальше `POST …/messages`;
* у NVIDIA NIM этого маршрута НЕТ — живой 404 без ключа;
* у OpenRouter он ЕСТЬ — 401 без ключа, а с ключом через цепочку приходит
  `INVALID_REQUEST: Invalid Anthropic Messages API request`, то есть претензия
  к ТЕЛУ, а не к маршруту.

Этот скрипт закрывает ровно тот пробел: шлёт минимальный корректный запрос в
форме Anthropic Messages по правилу `messagesApiRoot` и печатает ответ
провайдера — код и тело. Тело здесь печатать НУЖНО (в отличие от
`provider_latency.py`, где оно намеренно скрыто): именно в нём лежит причина,
ради которой скрипт существует. Секреты при этом не печатаются никогда: в
запрос уходит только значение из `secret_env`, в вывод — только имя
переменной, а тело ответа проходит через маскирование известных значений.

Запуск (ключи нужны настоящие, поэтому место запуска — CI):
    python scripts/measure/anthropic_route_probe.py
Тесты (без сети, прод-формы ответов):
    python -m pytest scripts/measure/test_anthropic_route_probe.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROVIDER_USAGE_MANIFEST = REPO_ROOT / "config" / "provider-usage.json"

#: Сколько печатать от тела ответа. Обрезка не косметическая: тело чужое, и
#: репозиторий публичный — но причина отказа лежит в его начале.
BODY_PREVIEW = 900

#: Версия протокола — та же, что реально шлёт dsh 0.1.7-alpha.2 (замер #1502,
#: запись живого исходящего запроса на локальный сервер). Не «последняя из
#: документации»: спрашиваем ровно то, что спрашивает прод.
ANTHROPIC_VERSION = "2023-06-01"


def messages_api_root(base_url: str) -> str:
    """Дословный перенос правила из `@deepseek-ai/dsh-llm-deepseek/lib/index.js`:

        const base = baseURL.replace(/\\/+$/u, "");
        return new URL(base).pathname.endsWith("/v1") ? base : `${base}/v1`;

    Перенос, а не пересказ: именно неверный пересказ этого правила дал в #1502
    таблицу, неверную для трёх строк из четырёх. Если правило в плагине
    изменится, менять надо ЗДЕСЬ и в том же коммите, где поднимается пин."""
    base = base_url.rstrip("/")
    from urllib.parse import urlparse
    path = urlparse(base).path
    return base if path.endswith("/v1") else f"{base}/v1"


def load_chain(consumer: str = "ai-review",
               manifest_path: Path = PROVIDER_USAGE_MANIFEST) -> list[dict]:
    """Боевая цепочка из манифеста — то же единственное место правды, что
    читает `provider_latency.py::build_manifest_candidates` и
    `dsh_load_provider_chain_from_manifest` в `scripts/lib/dsh-ci.sh`.
    Второй таблицы кандидатов здесь не заводится (AGENTS.md, «Одно место
    правды»)."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    chain_name = manifest.get("usage", {}).get(consumer)
    chain = manifest.get("chains", {}).get(chain_name) if chain_name else None
    if not isinstance(chain, list):
        return []
    return [e for e in chain if isinstance(e, dict)
            and {"name", "base_url", "model", "secret_env"} <= e.keys()]


def redact(text: str, secrets: list[str]) -> str:
    """Маскирование ТОЧНЫХ значений секретов в чужом теле ответа.

    Честная граница: производное (base64, подстрока, часть JWT) это не ловит —
    тот же предел, что у маскирования GitHub (AGENTS.md, «Секреты»). Поэтому
    печатается обрезанное начало тела, а не весь ответ."""
    for value in secrets:
        if value and len(value) >= 8:
            text = text.replace(value, "***")
    return text


def probe(entry: dict, timeout: float = 30.0) -> dict:
    """Один провайдер: минимальный корректный Anthropic Messages запрос.

    Возвращает факт, а не вердикт: код и тело. Классификацию делает читатель —
    у скрипта нет данных, чтобы отличить «маршрута нет» от «ключ не тот» лучше,
    чем это сделает сам текст провайдера."""
    secret = os.environ.get(entry["secret_env"], "")
    url = f"{messages_api_root(entry['base_url'])}/messages"
    payload = {
        "model": entry["model"],
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "ping"}],
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": secret,
            "accept": "application/json",
        })
    result = {"name": entry["name"], "url": url, "model": entry["model"],
              "secret_env": entry["secret_env"], "secret_present": bool(secret)}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            result.update(status=response.status, body=body[:BODY_PREVIEW])
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        result.update(status=error.code, body=body[:BODY_PREVIEW])
    except OSError as error:
        # «Возможности нет» отделено от «возможность есть, но отказала»
        # (AGENTS.md, fail loud): сеть — не ответ провайдера.
        result.update(status=0, body=f"сеть недоступна: {error}")
    return result


def format_report(results: list[dict]) -> str:
    # Имя переменной секрета — в таблице, значение — нигде. Читателю отчёта
    # чинить конфигурацию, и «секрета нет» без имени переменной не говорит,
    # ЧТО именно положить (AGENTS.md: утверждение обязано нести адрес).
    lines = ["запись | HTTP | секрет | переменная | URL",
             "---|---|---|---|---"]
    for r in results:
        lines.append(f"{r['name']} | {r['status']} | "
                     f"{'есть' if r['secret_present'] else 'НЕТ'} | "
                     f"`{r['secret_env']}` | `{r['url']}`")
    lines.append("")
    lines.append("Ответы провайдеров дословно (обрезаны, секреты замаскированы):")
    for r in results:
        lines.append("")
        lines.append(f"── {r['name']} (HTTP {r['status']}, модель `{r['model']}`)")
        lines.append(r["body"] or "(пустое тело)")
    return "\n".join(lines)


def main() -> int:
    chain = load_chain()
    if not chain:
        print("::error::боевая цепочка не прочитана из config/provider-usage.json — "
              "замер невозможен; это «возможности нет», а не «провайдеры молчат»",
              file=sys.stderr)
        return 1
    secrets = [os.environ.get(e["secret_env"], "") for e in chain]
    results = []
    for entry in chain:
        r = probe(entry)
        r["body"] = redact(r["body"], secrets)
        results.append(r)
    print(format_report(results))
    answered = [r for r in results if r["status"] == 200]
    print("")
    print(f"Ответили 200 на Anthropic-маршруте: {len(answered)} из {len(results)}")
    if not answered:
        print("Ни одна запись цепочки не обслуживает форму, которой ходит dsh — "
              "это факт замера, а не вердикт о причине: текст каждого отказа выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
