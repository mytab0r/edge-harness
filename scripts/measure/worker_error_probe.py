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


def status_counts() -> None:
    """Размеры таблиц с прода — числа, без которых «кто жрёт квоту» не считается.

    `/api/status` отдаёт счётчики задач и сообщений по статусам. Это ЕДИНСТВЕННЫЙ
    доступный отсюда способ узнать, сколько строк реально лежит в таблицах: сам
    SQL наружу не торчит, а гадать по коду — то, чем уже потрачены часы (#1503).
    Токен — HANDS_TOKEN, тот же, которым ходит job «рук».
    """
    token = os.environ.get("HANDS_TOKEN", "")
    if not token:
        print("HANDS_TOKEN не задан — счётчики таблиц не прочитаны; это «возможности нет», "
              "а не «таблицы пусты»")
        return
    # User-Agent обязателен (#1508). Дефолтный `Python-urllib/3.x` Cloudflare
    # режет на краю: зонд получал 403 ДО воркера и печатал «не прочитано» —
    # то есть отсутствие числа выглядело как свойство морды, хотя было
    # свойством нашего заголовка. Проверено живым вызовом 2026-09-24: тот же
    # URL с браузерным UA отвечает 401 JSON («нужна авторизация»), то есть
    # доходит до воркера. Это не обход защиты, а честное представление
    # собственного клиента.
    req = urllib.request.Request(
        "https://edge-harness.mytab0r.workers.dev/api/status",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "edge-harness-worker-error-probe (scripts/measure/worker_error_probe.py)",
        })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        # Код возврата — часть факта: 401 значит «токен не тот», 403 —
        # «до воркера не дошло», 500 — «воркер упал». Три разные починки,
        # и печатать их одним «не прочитан» — то самое гадание, которое
        # AGENTS.md запрещает алертам.
        print(f"::warning::/api/status не прочитан: HTTP {error.code} — "
              f"счётчики таблиц не получены; это «возможности нет», а не «таблицы пусты»")
        return
    except (OSError, ValueError) as error:
        print(f"::warning::/api/status не прочитан: {error}")
        return
    print("── размеры таблиц (прод, /api/status)")
    print(json.dumps({
        "tasks": body.get("tasks"),
        "messages": body.get("messages"),
        "last_event_id": body.get("last_event_id"),
        "retention": body.get("retention"),
    }, ensure_ascii=False))
    print()


def main() -> int:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account:
        print("::error::CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — "
              "зонд не может спросить логи; это «возможности нет», а не «ошибок нет»",
              file=sys.stderr)
        return 1

    status_counts()

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - 6 * 60 * 60 * 1000  # шесть часов: 1101 держится с прошлых суток
    url = f"{API}/accounts/{account}/workers/observability/telemetry/query"

    # Формы пробуются по возрастанию специфичности. Первая, что вернёт 200,
    # и есть верная; при всех неудачах дословный вывод назовёт, чего не хватает.
    # Форма установлена итерациями 1-3 (ответы API в логах прогонов):
    # queryId — любая строка, он заводит запрос на лету; лист фильтра обязан
    # нести `type`; datasets обязателен. Контроль telemetry/keys дал 200, то
    # есть токен и права исправны.
    #
    # ГЛАВНОЕ, что показала итерация 3: `POST /api/heartbeat` отвечает 200,
    # outcome "ok". Значит воркер НЕ мёртв целиком, и «падает на каждом
    # маршруте» было неверно. Поэтому здесь спрашиваем ровно события с
    # ошибкой — они и назовут, что именно падает.
    leaf = lambda key, op, value: {
        "key": key, "operation": op, "value": value, "type": "string",
    }
    def q(filters):
        return {
            "queryId": "probe", "timeframe": {"from": since_ms, "to": now_ms},
            "limit": 20, "view": "events",
            "parameters": {"datasets": ["cloudflare-workers"], "filters": filters},
        }
    attempts = [
        ("события с ошибкой", q([
            leaf("$metadata.service", "eq", WORKER),
            leaf("$metadata.error", "exists", ""),
        ]), url),
        ("уровень error", q([
            leaf("$metadata.service", "eq", WORKER),
            leaf("$metadata.level", "eq", "error"),
        ]), url),
        ("исход не ok", q([
            leaf("$metadata.service", "eq", WORKER),
            leaf("$workers.outcome", "neq", "ok"),
        ]), url),
    ]

    ok = False
    for name, payload, endpoint in attempts:
        status, text = _post(token, endpoint, payload)
        print(f"── форма «{name}»: HTTP {status}")
        try:
            payload_json = json.loads(text)
        except ValueError:
            print(text[:BODY_PREVIEW]); print(); continue
        events = (((payload_json.get("result") or {}).get("events") or {}).get("events") or [])
        if not events:
            print(json.dumps(payload_json, ensure_ascii=False)[:BODY_PREVIEW])
        for event in events:
            meta = event.get("$metadata", {})
            workers = event.get("$workers", {})
            # Печатаются ТОЛЬКО поля разбора: сообщение, ошибка, что вызвало,
            # исход. Тело запроса и заголовки не печатаются вовсе — там куски
            # запросов владельца, а репозиторий публичный (AGENTS.md).
            print(json.dumps({
                "ts": event.get("timestamp"),
                "level": meta.get("level"),
                "trigger": meta.get("trigger"),
                "message": str(meta.get("message"))[:300],
                "error": str(meta.get("error"))[:900],
                "outcome": workers.get("outcome"),
                "cpuMs": workers.get("cpuTimeMs"),
            }, ensure_ascii=False))
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
