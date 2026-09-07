#!/usr/bin/env python3
"""Прогон набора тестов со сдвинутыми часами — носитель на класс «тест
зависит от настенных часов и покраснеет в будущем» (issue #649).

Механизм сдвига — `scripts/conftest.py::CLOCK_SHIFT_DAYS` (autouse-фикстура
на freezegun, подробности и честная граница покрытия — докстринг того файла).
Этот скрипт — тонкая проводка вокруг pytest на КАЖДЫЙ горизонт: подпроцесс,
классификация исхода, сообщение на языке последствий (не «assertion failed»,
а «упадёт такого-то числа и заблокирует слияния» — AGENTS.md, «Алерт не
гадает»).

Горизонты (HORIZON_DAYS ниже — одно место правды, читает и workflow через
`python -m clock_shift_suite --list-horizons`, и этот файл):
  +1   — быстрая проверка, что сам механизм сдвига жив (санити);
  +8   — обязательный (issue #649): переживает MAX_CAMPAIGN_DAYS=7
         (scripts/measure/dispatch_tail.py) — ровно порог, взорвавшийся
         2026-09-07 (#643);
  +40  — переживает месячные пороги и разовые «через месяц» даты фикстур;
         ESCALATE_AFTER_HOURS=48 (stall_detector.py), STALE_HOURS=24 и
         WAITING_OWNER_REESCALATE_HOURS=24 (scheduler.py/waiting_owner_guard.py)
         на этом горизонте многократно пройдены;
  +400 — переживает годовой рубеж: фикстуры с литералом года (`2026-…`) в
         сравнении с текущим годом падают именно на переходе через Новый год,
         дальше самого дальнего порога кода (7 дней) — с большим запасом.

Коды выхода pytest, которые НЕЛЬЗЯ путать с «тесты собрались и все прошли»
(урок 2026-09-07, AGENTS.md «Алерт не гадает» + задание #649, пункт 3):
  0 — все зелёные;               1 — есть падения (это и есть красный класс);
  2..5 — харнес не смог стартовать (нет интерпретатора, битый импорт, 0
         собранных тестов, usage error) — это ОТДЕЛЬНЫЙ красный исход
         «проверить не удалось», не подтверждение зелёного.

Использование:
  python scripts/measure/clock_shift_suite.py --horizon-days 8 [--paths scripts/]
  python scripts/measure/clock_shift_suite.py --list-horizons
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Одно место правды горизонтов — обоснование числами в докстринге выше.
HORIZON_DAYS: tuple[int, ...] = (1, 8, 40, 400)

_SUMMARY_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)"
)
_FAILED_NODE_RE = re.compile(r"^FAILED\s+(\S+)", re.MULTILINE)
_ERROR_NODE_RE = re.compile(r"^ERROR\s+(\S+)", re.MULTILINE)

# pytest: 0 ok, 1 — тесты собрались и часть упала (наш красный класс).
# Всё остальное — харнес не поднялся (см. докстринг).
_HARNESS_OK_CODES = (0, 1)


def parse_collected_total(stdout: str) -> int:
    """Сумма счётчиков финальной строки-сводки pytest (`N passed, M failed
    in ...`). Отсутствие сводки (крах до отчёта, usage error) даёт 0 —
    неотличимо от «действительно ноль тестов», и это НАМЕРЕННО одна и та же
    красная классификация (see classify_result: harness_broken)."""
    return sum(int(n) for n, _ in _SUMMARY_COUNT_RE.findall(stdout))


def parse_failed_node_ids(stdout: str) -> list[str]:
    """Node id's из блока `short test summary info` (`FAILED path::test`) и
    ошибок коллекции (`ERROR path`) — конкретные имена, не общее число,
    чтобы сообщение называло факт, а не намекало."""
    return sorted(set(_FAILED_NODE_RE.findall(stdout)) | set(_ERROR_NODE_RE.findall(stdout)))


def classify_result(returncode: int, stdout: str) -> dict:
    """Чистая классификация — тестируется без subprocess, на прод-форме
    вывода pytest (реальные строки, снятые прогонами этого же скрипта).

    Возвращает {"outcome": "green"|"red"|"harness_broken", "collected": int,
    "failed_nodes": list[str], "detail": str}."""
    collected = parse_collected_total(stdout)
    failed_nodes = parse_failed_node_ids(stdout)

    if collected == 0:
        return {
            "outcome": "harness_broken",
            "collected": 0,
            "failed_nodes": failed_nodes,
            "detail": (
                f"0 тестов собрано (exit={returncode}) — проверить не удалось, "
                "это НЕ доказательство зелёного прогона"
            ),
        }
    if returncode not in _HARNESS_OK_CODES:
        return {
            "outcome": "harness_broken",
            "collected": collected,
            "failed_nodes": failed_nodes,
            "detail": (
                f"pytest завершился exit={returncode} при {collected} собранных "
                "тестах — харнес не смог довести прогон до отчёта (не путать "
                "с exit=1 «тесты упали»), не доказательство зелёного"
            ),
        }
    if returncode == 1:
        return {
            "outcome": "red",
            "collected": collected,
            "failed_nodes": failed_nodes,
            "detail": f"{len(failed_nodes)} из {collected} тестов красные при сдвинутых часах",
        }
    return {
        "outcome": "green",
        "collected": collected,
        "failed_nodes": failed_nodes,
        "detail": f"{collected} тестов, все зелёные",
    }


def consequence_message(horizon_days: int, result: dict) -> str:
    """Текст на языке последствий (AGENTS.md «Алерт не гадает»): что
    случится и когда, не пересказ внутреннего состояния pytest."""
    if result["outcome"] == "harness_broken":
        return (
            f"::error::горизонт +{horizon_days}д: {result['detail']} — "
            "прогон-носитель класса «тест зависит от настенных часов» (#649) "
            "не смог проверить репозиторий на этом горизонте"
        )
    names = ", ".join(result["failed_nodes"]) or "(имена не извлечены из вывода)"
    return (
        f"::error::горизонт +{horizon_days}д: {names} — "
        f"переживёт {horizon_days} календарных дней и станет обязательным "
        "красным в защите main (класс #643/#649, живой случай 2026-09-07: "
        "test_dispatch_failure_writes_note_not_row держал 33 открытых PR "
        "без единого алерта)"
    )


def run_pytest_with_shift(horizon_days: int, paths: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    env["CLOCK_SHIFT_DAYS"] = str(horizon_days)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *paths, "-q"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )
    output = proc.stdout + "\n" + proc.stderr
    print(output)
    return proc.returncode, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--horizon-days", type=int,
                        help="сдвиг часов в днях (см. HORIZON_DAYS)")
    parser.add_argument("--paths", nargs="+", default=["scripts/"],
                        help="что прогонять pytest'ом (по умолчанию весь scripts/)")
    parser.add_argument("--list-horizons", action="store_true",
                        help="напечатать HORIZON_DAYS через запятую и выйти")
    args = parser.parse_args()

    if args.list_horizons:
        print(",".join(str(d) for d in HORIZON_DAYS))
        return 0

    if args.horizon_days is None:
        parser.error("--horizon-days обязателен (или --list-horizons)")

    returncode, stdout = run_pytest_with_shift(args.horizon_days, args.paths)
    result = classify_result(returncode, stdout)
    print(f"горизонт +{args.horizon_days}д: {result['outcome']} — {result['detail']}")

    if result["outcome"] != "green":
        print(consequence_message(args.horizon_days, result))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
