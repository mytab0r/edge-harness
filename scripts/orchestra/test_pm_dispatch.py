#!/usr/bin/env python3
"""Тесты механического потолка закрытий pm-прогона (scripts/orchestra/
pm_dispatch.py, issue #869, Требование B).

Мутация (tasks.md B, критерий приёмки): убрать сравнение `closures >
PM_MAX_CLOSURES_PER_RUN` (заменить на заведомо ложное) — красит
test_check_closures_flags_when_over_budget.

Запуск: python -m pytest scripts/orchestra/test_pm_dispatch.py -q
"""

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "pm_dispatch.py"
spec = importlib.util.spec_from_file_location("pm_dispatch", SCRIPT)
pd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pd)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"
ACTOR = "mytab0r"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def closed_event(actor, created_at):
    return {"event": "closed", "actor": {"login": actor}, "created_at": created_at}


class FakeGh:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(pd, "gh", fake)


# ── Чистая функция: count_closures_in_window ─────────────────────────────────


def test_count_closures_counts_only_matching_actor_within_window():
    events = [
        closed_event(ACTOR, "2026-09-09T10:00:00Z"),
        closed_event(ACTOR, "2026-09-09T10:05:00Z"),
        closed_event("someone-else", "2026-09-09T10:06:00Z"),  # чужой актёр — не в счёт
        closed_event(ACTOR, "2026-09-09T09:00:00Z"),  # до окна — не в счёт
        {"event": "labeled", "actor": {"login": ACTOR}, "created_at": "2026-09-09T10:07:00Z"},  # не closed
    ]
    count = pd.count_closures_in_window(events, ACTOR, since=utc(2026, 9, 9, 9, 30))
    assert count == 2


def test_count_closures_respects_open_and_closed_window_bounds():
    events = [closed_event(ACTOR, "2026-09-09T10:00:00Z"), closed_event(ACTOR, "2026-09-09T11:00:00Z")]
    # Закрытая верхняя граница — событие РОВНО в until не считается (until эксклюзивен).
    assert pd.count_closures_in_window(
        events, ACTOR, since=utc(2026, 9, 9, 9, 0), until=utc(2026, 9, 9, 11, 0)) == 1
    # Открытый конец — оба события в счёт.
    assert pd.count_closures_in_window(events, ACTOR, since=utc(2026, 9, 9, 9, 0)) == 2


# ── Механический потолок: факт из событий, не декларация модели ─────────────


def test_check_closures_passes_within_budget(monkeypatch):
    events = [closed_event(ACTOR, "2026-09-09T10:00:00Z") for _ in range(pd.PM_MAX_CLOSURES_PER_RUN)]
    fake = FakeGh({"issues/events?per_page=100&page=1": events})
    patch_gh(monkeypatch, fake)
    rc = pd.cmd_check_closures([REPO, ACTOR, "2026-09-09T09:00:00Z"])
    assert rc == 0


def test_check_closures_flags_when_over_budget(monkeypatch):
    """Мутация (tasks.md B, критерий приёмки): замени `closures >
    PM_MAX_CLOSURES_PER_RUN` на `False` в cmd_check_closures — этот тест
    покраснеет (rc останется 0 при превышении)."""
    events = [
        closed_event(ACTOR, "2026-09-09T10:00:00Z")
        for _ in range(pd.PM_MAX_CLOSURES_PER_RUN + 1)
    ]
    fake = FakeGh({"issues/events?per_page=100&page=1": events})
    patch_gh(monkeypatch, fake)
    rc = pd.cmd_check_closures([REPO, ACTOR, "2026-09-09T09:00:00Z"])
    assert rc == 1


def test_check_closures_ignores_other_actors_and_pre_window_events(monkeypatch):
    events = (
        [closed_event(ACTOR, "2026-09-09T08:00:00Z") for _ in range(pd.PM_MAX_CLOSURES_PER_RUN + 5)]
        + [closed_event("someone-else", "2026-09-09T10:00:00Z") for _ in range(pd.PM_MAX_CLOSURES_PER_RUN + 5)]
    )
    fake = FakeGh({"issues/events?per_page=100&page=1": events})
    patch_gh(monkeypatch, fake)
    rc = pd.cmd_check_closures([REPO, ACTOR, "2026-09-09T09:00:00Z"])
    assert rc == 0  # оба множества вне счёта: одно до окна, другое — чужой актёр


def test_cmd_check_closures_rejects_wrong_argument_count():
    assert pd.cmd_check_closures([]) == 2
    assert pd.cmd_check_closures([REPO]) == 2


def test_fetch_issue_events_paginates_until_short_page(monkeypatch):
    page1 = [closed_event(ACTOR, "2026-09-09T10:00:00Z") for _ in range(100)]
    page2 = [closed_event(ACTOR, "2026-09-09T11:00:00Z") for _ in range(3)]
    fake = FakeGh({
        "issues/events?per_page=100&page=1": page1,
        "issues/events?per_page=100&page=2": page2,
    })
    patch_gh(monkeypatch, fake)
    events = pd.fetch_issue_events(REPO)
    assert len(events) == 103


def test_main_dispatches_check_closures_subcommand():
    assert pd.main([]) == 2
    assert pd.main(["bogus"]) == 2
