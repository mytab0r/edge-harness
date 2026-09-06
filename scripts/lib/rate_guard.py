#!/usr/bin/env python3
"""Ранний отказ по квоте GitHub API вместо 403 посреди дорогого job'а (#454).

Повод (живые случаи 2026-09-06): PR #428 — оба обязательных гейта (`test`,
`contract`) упали с `API rate limit exceeded for installation` посреди
чужого скрипта; PR #453 — прогон `ai-review` упал на
`gh api repos/.../pulls/453`. `GITHUB_TOKEN` даёт 1000 запросов/час НА
РЕПОЗИТОРИЙ, общих на ВСЕ workflow одновременно
(docs/research/21-github-actions.md, «API-лимиты: настоящий потолок»).
`orchestra` тратит 150-250 REST-вызовов за один прогон при непустой очереди
(issue #443, замер по коду) — это и есть основание порога: 300 (250 + ~20%
запас) отсекает прогон РАНЬШЕ, чем он рискнёт упасть 403 посреди своей же
работы, и оставляет не меньше 700 запросов/час другим потребителям того же
часа (contract на каждый открытый PR, ai-review, повторные
workflow_dispatch) — все они делят ОДИН и тот же бюджет, порог общий и
консервативный, откалиброванный по самому дорогому потребителю.

Использование (шаг workflow, ДО дорогого пути; последующие шаги гасятся
условием `if:` на его output):

    - id: quota
      env:
        GH_TOKEN: ${{ github.token }}
      run: python scripts/lib/rate_guard.py --job orchestra
    - if: steps.quota.outputs.skip != 'true'
      run: ...дорогой путь...

Граница (AGENTS.md, «fail loud, не silent-wrong»): отказ по квоте
(`skip=true`, код возврата 0, аннотация `::warning::`) и отказ по ЛЮБОЙ
ДРУГОЙ причине (сеть, права токена, битый ответ — код возврата 1, аннотация
`::error::`) обязаны различаться и кодом, и префиксом — чинятся они
по-разному, «квоты нет» не должно выглядеть как «сеть легла» и наоборот.

`gh api rate_limit` подтверждено НЕ тратит собственную квоту (замер
2026-09-06: три подряд вызова этой же командой не увеличили
`resources.core.used`, задокументировано и в GitHub REST API rate limits) —
эту проверку можно звать перед КАЖДЫМ дорогим job'ом без риска ускорить то,
от чего она защищает.

Запуск: python scripts/lib/rate_guard.py --job <имя>
Тесты:  python -m pytest scripts/lib/test_rate_guard.py -q
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# Один порог на все три точки вызова (orchestra, ai-review::review,
# repo-ci::test-инварианты, #454) — они делят один и тот же бюджет
# GITHUB_TOKEN (1000/час/репозиторий), поэтому один консервативный порог,
# откалиброванный по самому дорогому потребителю (orchestra, 150-250
# запросов/прогон), безопасен и для более дешёвых потребителей: они выйдут
# раньше, чем реально исчерпают себя, а не позже.
DEFAULT_THRESHOLD = 300


class QuotaCheckFailed(RuntimeError):
    """Настоящий сбой проверки (не «квоты мало», а «не смог узнать») — сеть,
    права токена, битый ответ. Отдельный класс исключения — вызывающий код
    не может перепутать эту причину с обычным skip (см. докстринг модуля,
    «Граница»)."""


def fetch_core(env: dict | None = None) -> dict:
    """`.resources.core` живого `gh api rate_limit` — единственный сетевой
    вызов этого скрипта. Не тратит собственную квоту (см. докстринг)."""
    result = subprocess.run(
        ["gh", "api", "rate_limit"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, **(env or {}), "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise QuotaCheckFailed(
            result.stderr.strip() or f"gh api rate_limit завершился с кодом {result.returncode}")
    try:
        body = json.loads(result.stdout)
        return body["resources"]["core"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise QuotaCheckFailed(f"ответ rate_limit не разобран: {error}")


def should_skip(core: dict, threshold: int) -> bool:
    """True — квоты меньше порога, дорогой путь пропускаем. Строго `<`:
    остаток РОВНО на пороге ещё считается достаточным («порог» — это «мало»,
    не «впритык к порогу тоже мало»)."""
    return core["remaining"] < threshold


def reset_human(core: dict) -> str:
    return datetime.fromtimestamp(core["reset"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def write_output(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as file:
        for key, value in pairs.items():
            file.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, help="имя job'а/шага — только для сообщения")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    try:
        core = fetch_core()
    except QuotaCheckFailed as error:
        # Настоящий сбой — не квота: другой код возврата, другой префикс
        # (см. докстринг модуля, «Граница»). Дорогой путь после этого не
        # должен молча продолжиться — вызывающий workflow обязан покраснеть
        # именно здесь, а не тонуть в первом же 403 внутри самого пути.
        print(f"::error::rate_guard: не смог прочитать квоту GitHub API для job'а «{args.job}»: {error}")
        return 1

    remaining, limit = core["remaining"], core["limit"]
    if should_skip(core, args.threshold):
        reset_at = reset_human(core)
        write_output({
            "skip": "true",
            "reason": f"квота {remaining}/{limit} ниже порога {args.threshold}",
            "reset": reset_at,
        })
        print(
            f"::warning::rate_guard: квота GitHub API почти исчерпана "
            f"({remaining}/{limit}, порог {args.threshold}) для job'а «{args.job}» — "
            f"дорогой путь пропущен, сброс в {reset_at}"
        )
        return 0

    write_output({"skip": "false"})
    print(f"rate_guard: квота ок ({remaining}/{limit}) для job'а «{args.job}»")
    return 0


if __name__ == "__main__":
    sys.exit(main())
