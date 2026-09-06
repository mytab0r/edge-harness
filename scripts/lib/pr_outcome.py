#!/usr/bin/env python3
"""Признак успеха прогона воркера по ветке задачи (#413) — одно место правды,
доступное и bash-скрипту (task.sh), и pytest напрямую.

Живой случай (прогон worker.yml 34002439672, 2026-09-06): DSH открыл PR #402
на ветку задачи И САМ ЖЕ его слил (полный автономный цикл до
`review:ok`+`ai:ok`+merge). `gh pr list --head $BRANCH --state open` не
находит слитый PR — воркер печатал «dsh завершился с кодом 0 без открытого
PR» и падал, хотя работа сделана и слита. Признак успеха — «PR по ветке
СУЩЕСТВУЕТ» (открыт ИЛИ слит), не «открыт»: слитый PR — успех БОЛЕЕ полный,
чем открытый, а не отсутствие PR.

Не терять настоящий провал (fail loud): открытый PR без диффа (`additions
+ deletions == 0`, DSH создал пустую ветку/PR, но не поработал) и закрытый
без слияния — по-прежнему провал, различаются только текстом причины, не
кодом исхода.

Правило выбора при нескольких PR на одну ветку (STATUS_ORDER ниже) —
MERGED побеждает всегда: слияние — необратимый факт работы, сделанной по
этой ветке, даже если тем же вызовом `gh pr list` пришёл более свежий OPEN
PR с тем же именем ветки (переиспользование имени, гонка присвоения
номеров) — этого на практике не бывает (одна ветка = один PR у GitHub), но
предпочтение MERGED делает выбор детерминированным при любом порядке ответа
API, не полагаясь на сортировку.

Запуск: python -m pytest scripts/lib/test_pr_outcome.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Порядок предпочтения статуса при выборе среди нескольких PR одной ветки.
STATUS_ORDER = {"merged": 0, "open": 1, "empty": 2, "absent": 3}


def _is_empty(pull: dict[str, Any]) -> bool:
    return int(pull.get("additions") or 0) + int(pull.get("deletions") or 0) == 0


def classify(pull: dict[str, Any]) -> str:
    """Статус ОДНОГО PR: merged (успех) | open (успех, есть дифф) |
    empty (открыт, но без диффа — не успех) | absent (закрыт без слияния —
    не успех, ветка сути не несёт)."""
    state = pull.get("state")
    if state == "MERGED":
        return "merged"
    if state == "OPEN":
        return "empty" if _is_empty(pull) else "open"
    return "absent"  # CLOSED без слияния — работу выбросили, не считается


def pick_pr_outcome(prs: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """Лучший исход среди всех PR ветки задачи. Пустой список — ("absent", None)."""
    if not prs:
        return "absent", None
    best = min(prs, key=lambda pull: STATUS_ORDER[classify(pull)])
    return classify(best), best


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — «сломано», не «пусто» (fail loud)
        print(f"pr_outcome.py: не смог прочитать/разобрать {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("использование: pr_outcome.py <prs.json>", file=sys.stderr)
        return 2
    prs = _load_json(Path(argv[0]))
    status, pull = pick_pr_outcome(prs)
    url = (pull or {}).get("url", "")
    print(f"{status}\t{url}")
    return 0 if status in ("merged", "open") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
