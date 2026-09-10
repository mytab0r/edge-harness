#!/usr/bin/env python3
"""Требование B (#869, openspec/changes/drain-health-curator): механический
потолок закрытий за автоматический прогон роли pm.

design.md §2.3 — разница автоматического диспетча pm от ручного вызова: у
ручного вызова разбор результата делает человек (успел заметить нарушение
границы pm.md — «не более 25 закрытий за проход»). У автоматического —
некому. Поэтому job-обвязка вокруг pm-прогона (pm.yml) считает ФАКТ (сколько
issue/PR реально закрыто за окно этого прогона) — не то, что модель сказала
о себе, тот же класс решения, что уже применён к
check_ai_failed_budget_exhausted/AI_REVIEW_MAX_ATTEMPTS (scheduler.py):
бюджет считается по внешнему следу (событиям), не по декларации агента.

Источник факта — `GET /repos/{repo}/issues/events` (REST, единый эндпоинт
для issue И PR-событий — GitHub API трактует PR как issue для целей этого
эндпоинта): событие `closed`, актёр — GitHub-логин, под которым идёт
автоматический прогон (единственный логин у всех агентов репозитория,
AGENTS.md), created_at внутри окна прогона [since, until).

Запуск (шаг workflow, после DSH-прогона pm.yml):
  python scripts/orchestra/pm_dispatch.py check-closures <repo> <actor> \
      <since_iso> [<until_iso>]
Код возврата: 0 — в пределах PM_MAX_CLOSURES_PER_RUN; 1 — превышение
(печатает ::error:: с фактическим числом, не полагается на самоотчёт модели).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys
from datetime import datetime, timezone

_PG_SPEC = importlib.util.spec_from_file_location(
    "pulse_guard", Path(__file__).resolve().parent / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_PG_SPEC)
_PG_SPEC.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

gh = pulse_guard.gh
parse_time = pulse_guard.parse_time

# Тот же явный потолок, что pm.md уже объявляет для СЕБЯ и что
# scheduler.PM_MAX_CLOSURES_PER_RUN уже несёт как одно место правды —
# читается оттуда, не второе число здесь (scheduler.py не импортируется
# целиком ради одной константы — нет циклического импорта: pm_dispatch.py
# сам не импортируется scheduler.py, безопасно ленивым importlib).
_SCH_SPEC = importlib.util.spec_from_file_location(
    "scheduler", Path(__file__).resolve().parent / "scheduler.py")
scheduler = importlib.util.module_from_spec(_SCH_SPEC)
_SCH_SPEC.loader.exec_module(scheduler)  # type: ignore[union-attr]

PM_MAX_CLOSURES_PER_RUN = scheduler.PM_MAX_CLOSURES_PER_RUN


def fetch_issue_events(repo: str, per_page: int = 100, max_pages: int = 10) -> list[dict]:
    """Постранично, от новых к старым (GitHub отдаёт issues/events в порядке
    возрастания id — старые первыми; здесь достаточно последних max_pages
    страниц для окна одного прогона pm.yml, не всей истории репозитория)."""
    events: list[dict] = []
    page = 1
    while page <= max_pages:
        batch = gh(f"repos/{repo}/issues/events?per_page={per_page}&page={page}") or []
        if not batch:
            break
        events.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return events


def count_closures_in_window(
    events: list[dict], actor_login: str, since: datetime, until: datetime | None = None,
) -> int:
    """Чистая функция (issue #869): сколько событий `closed`, атрибутированных
    `actor_login`, попадают в [since, until). `until=None` — открытый конец
    (до текущего момента вызова, обычно момент запуска этой проверки)."""
    count = 0
    for event in events:
        if event.get("event") != "closed":
            continue
        actor = (event.get("actor") or {}).get("login")
        if actor != actor_login:
            continue
        created_at = event.get("created_at")
        if not created_at:
            continue
        when = parse_time(created_at)
        if when < since:
            continue
        if until is not None and when >= until:
            continue
        count += 1
    return count


def cmd_check_closures(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "использование: pm_dispatch.py check-closures <repo> <actor> <since_iso> [<until_iso>]",
            file=sys.stderr,
        )
        return 2
    repo, actor, since_iso = argv[0], argv[1], argv[2]
    until = parse_time(argv[3]) if len(argv) == 4 else None
    try:
        since = parse_time(since_iso)
        events = fetch_issue_events(repo)
    except RuntimeError as error:
        print(f"::error::pm_dispatch: не смог прочитать issues/events: {error}")
        return 2
    closures = count_closures_in_window(events, actor, since, until)
    if closures > PM_MAX_CLOSURES_PER_RUN:
        print(
            f"::error::pm-прогон закрыл {closures} issue/PR — превышен потолок "
            f"PM_MAX_CLOSURES_PER_RUN ({PM_MAX_CLOSURES_PER_RUN}); факт по событиям "
            f"issues/events, не по декларации модели (design.md §2.3)"
        )
        return 1
    print(f"pm_dispatch: {closures} закрытий за прогон (потолок {PM_MAX_CLOSURES_PER_RUN}) — в пределах")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "check-closures":
        return cmd_check_closures(argv[1:])
    print("использование: pm_dispatch.py check-closures <repo> <actor> <since_iso> [<until_iso>]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
