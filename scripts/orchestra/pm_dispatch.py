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
Код возврата: 0 — в пределах PM_MAX_CLOSURES_PER_RUN и границы pm.md
(исполнитель/`waiting:owner`, design.md §2.3) не нарушены; 1 — превышение
или нарушенная граница (печатает ::error:: с фактическими числами/номерами,
не полагается на самоотчёт модели); 2 — проверка НЕ СОСТОЯЛАСЬ (события не
прочитаны или окно усечено — громкая гвардия усечения в cmd_check_closures,
иначе счётчик занижал бы молча).

Честная граница (находка ревью PR #870, некритичная): счётчик мерит ВСЕ
`closed`-события `actor_login` за окно, не только те, что сделал именно
этот pm-прогон — тем же PAT-логином в это же 70-минутное окно может
закрыть PR/задачу пульс orchestra (мерж, реап stale), и это зачтётся
pm-прогону как ложный красный. Направление ошибки — fail-loud (лишняя
тревога, не пропуск реального превышения), поэтому осознанно не решается
здесь сопоставлением номеров issue с беклогом конкретного прогона —
предмет отдельного уточнения, если ложные срабатывания станут заметны на
практике.
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


def fetch_issue_events(repo: str, per_page: int = 100, max_pages: int = 10) -> tuple[list[dict], bool]:
    """Постранично, ОТ НОВЫХ К СТАРЫМ (репозиторий-эндпоинт
    `GET /repos/{repo}/issues/events` отдаёт события newest-first — замер
    2026-09-10 по api.github.com, находка ревью PR #870: page 1 —
    created_at 19:31:23Z, page 2 — 19:30:22Z; листать надо С page 1, не с
    последней). Возвращает (events, truncated): `truncated=True` — лимит
    max_pages страниц исчерпан, последняя прочитанная страница полная, и за
    ней МОГУТ быть ещё события; достаточно последних max_pages страниц для
    окна одного прогона pm.yml, не всей истории репозитория. Полноту окна
    проверяет cmd_check_closures (громкая гвардия усечения): truncated при
    непроверенной полноте — то же «шаг молча зелёный», что и неверный счёт."""
    events: list[dict] = []
    page = 1
    truncated = False
    while page <= max_pages:
        batch = gh(f"repos/{repo}/issues/events?per_page={per_page}&page={page}") or []
        if not batch:
            return events, truncated
        events.extend(batch)
        if len(batch) < per_page:
            return events, truncated
        page += 1
    return events, True


def _closed_in_window(
    event: dict, actor_login: str, since: datetime, until: datetime | None,
) -> bool:
    """Единое правило попадания события в подсчёт (одно место правды для
    count_closures_in_window и find_boundary_violations): `closed`, тот же
    актёр, created_at в [since, until); `until=None` — открытый конец."""
    if event.get("event") != "closed":
        return False
    actor = (event.get("actor") or {}).get("login")
    if actor != actor_login:
        return False
    created_at = event.get("created_at")
    if not created_at:
        return False
    when = parse_time(created_at)
    if when < since:
        return False
    return not (until is not None and when >= until)


def count_closures_in_window(
    events: list[dict], actor_login: str, since: datetime, until: datetime | None = None,
) -> int:
    """Чистая функция (issue #869): сколько событий `closed`, атрибутированных
    `actor_login`, попадают в [since, until). `until=None` — открытый конец
    (до текущего момента вызова, обычно момент запуска этой проверки)."""
    return sum(
        1 for event in events if _closed_in_window(event, actor_login, since, until)
    )


# Метка «ждёт решения владельца» — граница pm.md («не трогает waiting:owner»),
# та же, что waiting_owner_guard.py снимает по решению владельца (#471).
PM_FORBIDDEN_OWNER_LABEL = "waiting:owner"


def find_boundary_violations(
    events: list[dict], actor_login: str, since: datetime, until: datetime | None = None,
) -> list[dict]:
    """Чистая функция (находка ревью PR #870, некритичное замечание 8):
    закрытые `actor_login`'ом в окне [since, until) items, нарушающие границы
    pm.md — у закрытого есть исполнитель или метка `waiting:owner`. Тот же
    класс «факт по событиям, не декларация модели», что потолок закрытий:
    механически гарантировать можно только посчитанное.

    Известная граница (fail-loud, названа честно): `issue` внутри события
    несёт СОСТОЯНИЕ НА МОМЕНТ ЧТЕНИЯ, не на момент закрытия — исполнитель,
    добавленный после закрытия, попадёт в флаг (лишняя тревога, не пропуск);
    приёмка/мержи закрываются под `github-actions[bot]` (замер 2026-09-11 по
    issues/events: closed-события задач после приёмки и слитых PR), а не под
    актёра pm (`GH_PIPELINE_PAT`, `github.repository_owner`) — классов не
    пересекаются; закрытия воркером под тем же логином возможны — номера
    называются поимённо, разбор окна вручную отличает."""
    violations: list[dict] = []
    for event in events:
        if not _closed_in_window(event, actor_login, since, until):
            continue
        issue = event.get("issue") or {}
        number = issue.get("number")
        if number is None:
            continue
        reasons: list[str] = []
        assignees = [a.get("login", "") for a in issue.get("assignees") or []]
        if assignees:
            reasons.append("есть исполнитель: " + ", ".join(login for login in assignees if login))
        labels = {label.get("name") for label in issue.get("labels") or []}
        if PM_FORBIDDEN_OWNER_LABEL in labels:
            reasons.append(f"метка `{PM_FORBIDDEN_OWNER_LABEL}`")
        if reasons:
            violations.append({
                "number": number,
                "is_pr": bool(issue.get("pull_request")),
                "reasons": reasons,
            })
    return violations


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
        events, truncated = fetch_issue_events(repo)
    except RuntimeError as error:
        print(f"::error::pm_dispatch: не смог прочитать issues/events: {error}")
        return 2
    # Громкая гвардия усечения (находка ревью PR #870, некритичное замечание 5):
    # события приходят newest-first; если лимит страниц исчерпан, а самый
    # старый из ПРОЧИТАННЫХ всё ещё новее `since`, за срезом МОГУТ быть
    # события окна — счётчик занижен, и зелёный шаг здесь был бы ложью
    # (единственный fail-silent ход этой проверки). Код 2 — «проверка не
    # состоялась», не «нарушение»: лечится перезапуском/увеличением max_pages,
    # не разбором поведения pm.
    oldest = min(
        (parse_time(event["created_at"]) for event in events if event.get("created_at")),
        default=None,
    )
    if truncated and oldest is not None and oldest > since:
        print(
            "::error::pm_dispatch: окно усечено — прочитаны не все события issues/events "
            f"(самый старый прочитанный {oldest.isoformat(timespec='seconds')} новее "
            f"начала окна {since.isoformat(timespec='seconds')}); счёт закрытий и границы "
            "ненадёжны, проверка не состоялась"
        )
        return 2
    closures = count_closures_in_window(events, actor, since, until)
    violations = find_boundary_violations(events, actor, since, until)
    failed = False
    if closures > PM_MAX_CLOSURES_PER_RUN:
        print(
            f"::error::pm-прогон закрыл {closures} issue/PR — превышен потолок "
            f"PM_MAX_CLOSURES_PER_RUN ({PM_MAX_CLOSURES_PER_RUN}); факт по событиям "
            f"issues/events, не по декларации модели (design.md §2.3)"
        )
        failed = True
    for violation in violations:
        kind = "PR" if violation["is_pr"] else "issue"
        print(
            f"::error::pm-прогон закрыл {kind} #{violation['number']} — нарушена граница "
            f"pm.md (design.md §2.3): {'; '.join(violation['reasons'])}. Разбор окна "
            "вручную отличает pm-закрытие от чужого под тем же логином (docstring "
            "find_boundary_violations)"
        )
        failed = True
    if failed:
        return 1
    print(f"pm_dispatch: {closures} закрытий за прогон (потолок {PM_MAX_CLOSURES_PER_RUN}), "
          f"границы исполнителя/`{PM_FORBIDDEN_OWNER_LABEL}` не нарушены — в пределах")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "check-closures":
        return cmd_check_closures(argv[1:])
    print("использование: pm_dispatch.py check-closures <repo> <actor> <since_iso> [<until_iso>]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
