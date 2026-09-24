#!/usr/bin/env python3
"""Стектрейс исключения воркера из Workers Logs — вместо гадания по коду 1101.

Класс одной фразой: **`error code: 1101` — это «воркер бросил исключение», и
сам по себе он не называет НИ ОДНОЙ причины; без стектрейса разбор вырождается
в перебор гипотез.**

Живой случай (#1503, 2026-09-24): морда отдавала 1101 на всех `/api/*` при
нулевой квоте, нулевом `rows_read` и нулевом `storedBytes`. Проверка гипотез по
косвенным признакам съела несколько часов и не дала ответа — ни одна из них не
отличалась от соседней по наблюдаемым данным.

Логи собираются (`cf-worker/wrangler.jsonc`, `observability.enabled: true`), то
есть стектрейс существует и доступен по API тем же токеном, которым ходит
`quotas.py`. Этот скрипт его забирает.

Честная граница: форма запроса к telemetry-API в доке описана неполно. Скрипт
НЕ делает вид, что знает её наверняка — он пробует известные формы по очереди и
при отказе печатает ответ Cloudflare ДОСЛОВНО, потому что именно текст отказа
называет правильную форму (ровно так в этом же PR починился запрос
`durableObjectsStorageGroups`: «filter: not an object» — и стало понятно, чего
не хватает).

Секреты: токен читается из окружения и никуда не печатается. Тела ответов
печатаются обрезанными — там могут быть значения из запросов владельца.

Запуск: CLOUDFLARE_API_TOKEN=… CLOUDFLARE_ACCOUNT_ID=… python scripts/measure/worker_error_probe.py
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
import time
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
WORKER = "edge-harness"
#: Сколько печатать от тела ответа. Обрезка не косметическая: в логах воркера
#: лежат куски запросов владельца, а репозиторий публичный (AGENTS.md, «Секреты»).
BODY_PREVIEW = 4000


def _post(token: str, url: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")
    except OSError as error:
        return 0, f"сеть недоступна: {error}"


def main() -> int:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account:
        print("::error::CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — "
              "зонд не может спросить логи; это «возможности нет», а не «ошибок нет»",
              file=sys.stderr)
        return 1

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - 6 * 60 * 60 * 1000  # шесть часов: 1101 держится с прошлых суток
    url = f"{API}/accounts/{account}/workers/observability/telemetry/query"

    # Формы пробуются по возрастанию специфичности. Первая, что вернёт 200,
    # и есть верная; при всех неудачах дословный вывод назовёт, чего не хватает.
    # Форма взята НЕ из доки, а из ответа самого API: ZodError первой попытки
    # назвал оба варианта фильтра — либо группа
    # (`kind: "group"`, `filterCombination`, `filters`), либо лист, у которого
    # ОБЯЗАТЕЛЕН `type` из {string, number, boolean}. Первая попытка отдельно
    # показала, что произвольный `queryId` не годится: «Query not found» —
    # значит это ссылка на сохранённый запрос, а не свободная метка.
    leaf = lambda key, value: {
        "key": key, "operation": "eq", "value": value, "type": "string",
    }
    base = {"timeframe": {"from": since_ms, "to": now_ms}, "limit": 20}
    attempts = [
        ("лист с type, без queryId", {
            **base,
            "parameters": {"filters": [leaf("$metadata.service", WORKER)]},
        }),
        ("группа and", {
            **base,
            "parameters": {"filters": [{
                "kind": "group", "filterCombination": "and",
                "filters": [leaf("$metadata.service", WORKER)],
            }]},
        }),
        ("без фильтров вовсе", {**base, "parameters": {}}),
    ]

    ok = False
    for name, payload in attempts:
        status, text = _post(token, url, payload)
        print(f"── форма «{name}»: HTTP {status}")
        print(text[:BODY_PREVIEW])
        print()
        if status == 200:
            ok = True
    if not ok:
        print("::error::ни одна форма запроса не вернула 200 — правильную форму называет "
              "текст отказа выше, он напечатан дословно именно для этого", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
