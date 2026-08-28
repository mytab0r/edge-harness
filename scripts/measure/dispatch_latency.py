#!/usr/bin/env python3
"""Замер задержки «repository_dispatch → первый heartbeat» (tasks.md, задача 5).

Ставит N задач через POST /api/tasks (воркер должен быть задеплоен и иметь
GH_DISPATCH_TOKEN), дожидается от каждой первого heartbeat'а — DO записывает
latency_ms в задачу по своим часам, так что часы машины-измерителя на число не
влияют — и сводит распределение.

Использование:
    python scripts/measure/dispatch_latency.py --n 20
    python scripts/measure/dispatch_latency.py --n 20 --out dispatch-latency.csv

Задача, не начатая за --timeout-s (по умолчанию 600), помечается как timeout и в
числовую сводку не попадает, но в CSV пишется — тяжёлый хвост распределения сам
по себе результат (см. docs/research/99-open-questions.md).
"""

import argparse
import csv
import json
import os
import statistics
import sys
import time
import urllib.request

POLL_INTERVAL_S = 1.0

BASE = os.environ["HARNESS_URL"].rstrip("/")
TOKEN = os.environ["HANDS_TOKEN"]
# Cloudflare на workers.dev блокирует UA "Python-urllib/*" (403 от edge, до кода не доходит).
UA = "edge-harness/0.1 (GitHub Actions; +https://github.com/mytab0r/edge-harness)"


def request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="сколько задач поставить")
    parser.add_argument("--timeout-s", type=int, default=600, help="сколько ждать старта одной задачи")
    parser.add_argument("--pause-s", type=float, default=2.0, help="пауза между постановками")
    parser.add_argument("--out", default="dispatch-latency.csv", help="файл CSV с результатами")
    args = parser.parse_args()

    rows = []
    for i in range(args.n):
        created = request("POST", "/api/tasks", {"payload": {"measure_run": i}})
        if not created.get("dispatched"):
            print(
                f"ФАТАЛЬНО: dispatch не выполнен ({created.get('dispatch')}): "
                "воркер должен быть задеплоен с настроенным GH_DISPATCH_TOKEN",
                file=sys.stderr,
            )
            return 3
        task_id = created["task_id"]
        print(f"[{i + 1}/{args.n}] задача {task_id[:8]}… поставлена, жду первый heartbeat…")

        latency = None
        status = "unknown"
        deadline = time.monotonic() + args.timeout_s
        while time.monotonic() < deadline:
            task = request("GET", f"/api/tasks/{task_id}")["task"]
            status = task["status"]
            if task["latency_ms"] is not None:
                latency = task["latency_ms"]
                break
            if status in ("done", "failed"):
                break
            time.sleep(POLL_INTERVAL_S)

        if latency is not None:
            print(f"    старт через {latency} мс (статус: {status})")
        else:
            print(f"    ТАЙМАУТ {args.timeout_s} с: job не стартовал (статус: {status})")
        rows.append({"run": i, "task_id": task_id, "latency_ms": latency, "status": status,
                     "timeout_s": args.timeout_s if latency is None else ""})
        time.sleep(args.pause_s)

    with open(args.out, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["run", "task_id", "latency_ms", "status", "timeout_s"])
        writer.writeheader()
        writer.writerows(rows)

    measured = [row["latency_ms"] for row in rows if row["latency_ms"] is not None]
    timeouts = len(rows) - len(measured)
    print(f"\n== Результаты записаны в {args.out} ==")
    if measured:
        measured_sorted = sorted(measured)
        p90 = measured_sorted[max(0, round(0.9 * len(measured_sorted)) - 1)]
        print(f"замеров: {len(measured)} из {len(rows)} (таймаутов: {timeouts})")
        print(f"min: {min(measured)} мс | медиана: {statistics.median(measured)} мс | "
              f"p90: {p90} мс | max: {max(measured)} мс")
    else:
        print("ни одного успешного старта — записаны только таймауты")
    return 0


if __name__ == "__main__":
    sys.exit(main())
