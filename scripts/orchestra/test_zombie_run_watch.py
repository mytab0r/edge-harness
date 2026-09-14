#!/usr/bin/env python3
"""Тесты сторожа зомби-прогонов PR-чеков
(scripts/orchestra/zombie_run_watch.py, issue #1106).

Фикстуры — прод-форма, не пересказ:

- `ZOMBIE_RUN_ORCHESTRA` — дословно урезанный снимок `gh api
  repos/mytab0r/edge-harness/actions/runs/34748469966` (2026-09-14): один
  из восьми зомби-прогонов PR #1088 (`status=queued`, `created_at`
  2026-09-13T08:46:40Z), поля сокращены до тех, что реально читает модуль.
- `ZOMBIE_JOBS_EMPTY` — дословный `gh api
  .../actions/runs/34748469966/jobs` (`{"total_count": 0, "jobs": []}`).
- `HEALTHY_RUN`/`HEALTHY_JOBS` — дословный снимок живого успешного прогона
  34811586423 (2026-09-14, repo-ci): первый job создан через 1 секунду
  после `created_at` run'а — база для обоснования ZOMBIE_AGE_MINUTES=15 в
  докстринге модуля.
- `OPEN_PR_1209` — дословно урезанный `gh api
  repos/mytab0r/edge-harness/pulls/1209` (2026-09-14).
- Маркеры в фикстурах-комментариях авторства `github-actions[bot]` — это
  ПРОД-форма: workflow пишет под `github.token`, а чтение маркеров в модуле
  фильтрует автора `trusted_login=EVENT_ACTOR_LOGIN` (находка ревью PR
  #1212, #1027) — комментарий чужого автора маркер-дедуп не создаёт и
  проверяется отдельным тестом ниже.

Запуск: python -m pytest scripts/orchestra/test_zombie_run_watch.py -q
"""

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # zombie_run_watch.py делает `from pulse_guard import …`

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg

SCRIPT = _DIR / "zombie_run_watch.py"
spec = importlib.util.spec_from_file_location("zombie_run_watch", SCRIPT)
zrw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zrw)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    """zombie_run_watch.py импортирует gh/issue_marker_times/escalate ИЗ
    pulse_guard — те, что живут в pulse_guard, резолвят `gh` через
    __globals__ pulse_guard, поэтому обе привязки указывают на один и тот
    же fake (тот же приём, что test_dependabot_alert_watch.py::patch_gh)."""
    monkeypatch.setattr(zrw, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def patch_escalate(monkeypatch, result="Telegram: доставлен; след в #120: оставлен"):
    """Подмена канала эскалации с записью вызовов: возвращает (calls,
    result) — result по умолчанию «оба канала живы», чтобы предикат
    escalation_channel_failed не красил прогон."""
    calls: list[tuple[str, int, str]] = []

    def fake(repo, issue_number, text, options=None):
        calls.append((repo, issue_number, text))
        return result

    monkeypatch.setattr(zrw, "escalate", fake)
    return calls


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


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
                return result(self) if callable(result) else result
        raise AssertionError(f"нет маршрута для: {joined}")


REPO = "mytab0r/edge-harness"
NOW = utc(2026, 9, 14, 6, 0)

# Прод-форма (см. докстринг файла) — один из 8 зомби-прогонов PR #1088.
ZOMBIE_RUN_ORCHESTRA = {
    "id": 34748469966,
    "name": "orchestra",
    "event": "pull_request",
    "status": "queued",
    "conclusion": None,
    "created_at": "2026-09-13T08:46:40Z",
    "head_sha": "9a75c4142a020d8d57f50952acab25b957c086ce",
    "head_branch": "agent/1087-react-dom-peer-major-mismatch",
}
ZOMBIE_JOBS_EMPTY = {"total_count": 0, "jobs": []}

# Достижимость на прод-величинах (issue #1172, доводка PR #1212): все ВОСЕМЬ
# id — дословно те же зомби-прогоны, что называет докстринг модуля,
# перепроверены живым `gh api .../actions/runs/<id>/jobs` за минуты до этого
# коммита — все восемь всё ещё `total_count: 0` (permanent-зомби, как и
# описано). PR #1088 к моменту перепроверки уже MERGED — этот же факт
# воспроизводит "живой замер" докстринга (застрявшие run-объекты остаются
# queued навсегда, даже когда PR давно закрыт); тест ниже реконструирует
# состояние НА МОМЕНТ инцидента (PR ещё открыт, все 8 required-чеков на его
# текущем head) — та форма, для которой сторож и написан.
ZOMBIE_RUN_IDS_LIVE_INCIDENT = [
    34748469966, 34748469981, 34748469982, 34748469975,
    34748469992, 34748470003, 34748470010, 34748470011,
]
ZOMBIE_RUNS_ALL_EIGHT = [
    {**ZOMBIE_RUN_ORCHESTRA, "id": run_id, "name": f"check-{i}"}
    for i, run_id in enumerate(ZOMBIE_RUN_IDS_LIVE_INCIDENT)
]

HEALTHY_RUN = {
    "id": 34811586423,
    "name": "repo-ci",
    "event": "pull_request",
    "status": "queued",
    "conclusion": None,
    "created_at": "2026-09-14T05:58:57Z",
    "head_sha": "b3655b9e041799db49427bea9ad08fadab368d78",
    "head_branch": "agent/1149-rebase-patch-series-0-14",
}
HEALTHY_JOBS = {
    "total_count": 2,
    "jobs": [
        {"name": "test", "created_at": "2026-09-14T05:58:58Z"},
        {"name": "archive-fixup", "created_at": "2026-09-14T05:58:58Z"},
    ],
}

OPEN_PR_1209 = {
    "number": 1209,
    "state": "open",
    "head": {
        "ref": "agent/1149-rebase-patch-series-0-14",
        "sha": "b3655b9e041799db49427bea9ad08fadab368d78",
    },
}
OPEN_PR_1088_STALE_BRANCH = {
    "number": 1088,
    "state": "open",
    "head": {
        "ref": "agent/1087-react-dom-peer-major-mismatch",
        # Ветка PR#1088 ушла вперёд с момента зомби-прогона — head другой.
        "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    },
}
OPEN_PR_1088_MATCHING = {
    "number": 1088,
    "state": "open",
    "head": {
        "ref": "agent/1087-react-dom-peer-major-mismatch",
        "sha": "9a75c4142a020d8d57f50952acab25b957c086ce",
    },
}


# ── Чистая логика ────────────────────────────────────────────────────────


def test_stale_candidates_prod_form_age_boundary():
    # ZOMBIE_RUN_ORCHESTRA создан 2026-09-13T08:46:40Z, NOW=2026-09-14T06:00Z
    # — больше суток, точно за порогом.
    assert zrw.stale_candidates([ZOMBIE_RUN_ORCHESTRA], NOW) == [ZOMBIE_RUN_ORCHESTRA]


def test_stale_candidates_fresh_run_not_a_candidate():
    fresh = {**ZOMBIE_RUN_ORCHESTRA, "created_at": "2026-09-14T05:50:00Z"}  # 10 мин назад
    assert zrw.stale_candidates([fresh], NOW) == []


def test_stale_candidates_ignores_non_queued():
    completed = {**ZOMBIE_RUN_ORCHESTRA, "status": "completed"}
    assert zrw.stale_candidates([completed], NOW) == []


def test_runs_created_after_excludes_old_permanent_zombie():
    reopen_time = utc(2026, 9, 13, 12, 0)  # позже создания зомби-прогона
    assert zrw.runs_created_after([ZOMBIE_RUN_ORCHESTRA], reopen_time) == []


def test_runs_created_after_includes_genuinely_new_run():
    new_run = {**ZOMBIE_RUN_ORCHESTRA, "id": 999, "created_at": "2026-09-14T00:00:00Z"}
    reopen_time = utc(2026, 9, 13, 12, 0)
    assert zrw.runs_created_after([ZOMBIE_RUN_ORCHESTRA, new_run], reopen_time) == [new_run]


def test_open_prs_by_branch_prod_form():
    mapping = zrw.open_prs_by_branch([OPEN_PR_1209])
    assert mapping["agent/1149-rebase-patch-series-0-14"]["number"] == 1209


def test_group_candidates_matches_current_head_only():
    grouped = zrw.group_candidates_by_pr(
        [ZOMBIE_RUN_ORCHESTRA], {"agent/1087-react-dom-peer-major-mismatch": OPEN_PR_1088_MATCHING})
    assert 1088 in grouped
    assert grouped[1088]["runs"] == [ZOMBIE_RUN_ORCHESTRA]


def test_group_candidates_rejects_stale_head_sha():
    # Ветка ушла вперёд — зомби-прогон был на старом head, не блокирует
    # текущий (мутация: снять сравнение head_sha дало бы ложное срабатывание
    # на устаревшем прогоне).
    grouped = zrw.group_candidates_by_pr(
        [ZOMBIE_RUN_ORCHESTRA], {"agent/1087-react-dom-peer-major-mismatch": OPEN_PR_1088_STALE_BRANCH})
    assert grouped == {}


def test_group_candidates_ignores_run_with_no_open_pr():
    grouped = zrw.group_candidates_by_pr([ZOMBIE_RUN_ORCHESTRA], {})
    assert grouped == {}


# ── zombie_run_watch: сценарии end-to-end ───────────────────────────────


def test_zero_queued_runs_reports_clean(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert "queued PR-прогонов старше" in observations[0]
    assert fake.calls == [f"repos/{REPO}/actions/runs?event=pull_request&status=queued&per_page=100"]


def test_non_list_runs_response_is_loud_not_silent_ok(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": "bad"},
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="не ожидаемой формой"):
        zrw.zombie_run_watch(REPO, NOW)


def test_fresh_queued_run_no_action_no_job_lookup(monkeypatch):
    fresh = {**HEALTHY_RUN, "created_at": "2026-09-14T05:55:00Z"}  # 5 мин назад
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [fresh]},
        "actions/runs/34811586423/jobs": AssertionError("не должен вызываться — прогон свежий"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("нет" in o for o in observations)


def test_stale_but_not_zombie_run_no_action(monkeypatch):
    """Прогон старше порога, но job'ы всё же есть (просто медленный старт) —
    НЕ зомби, действие не требуется, PR не трогается."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        f"issues/1088/comments": [],
        # запасной носитель маркера (#120) читается, когда в PR маркера нет
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": {"total_count": 3, "jobs": [{}]},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []


def test_confirmed_zombie_on_open_pr_triggers_reopen(monkeypatch):
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)
    patch_calls = [c for c in fake.calls if "-X PATCH" in c and "pulls/1088" in c]
    assert any("state=closed" in c for c in patch_calls)
    assert any("state=open" in c for c in patch_calls)
    comment_calls = [c for c in fake.calls if "-X POST" in c and "issues/1088/comments" in c]
    assert len(comment_calls) == 1
    # Порядок действия (находка ревью PR #1212): close→reopen ПЕРВЫМИ,
    # маркер-отчёт — только после успеха. Мутация (вернуть порядок
    # «комментарий → действие») красит этот тест: комментарий с маркером,
    # записанный до действия, при отказе посередине навсегда глушит газ.
    closed_idx = fake.calls.index(next(c for c in fake.calls if "state=closed" in c))
    opened_idx = fake.calls.index(next(c for c in fake.calls if "state=open" in c))
    post_idx = fake.calls.index(comment_calls[0])
    assert closed_idx < post_idx and opened_idx < post_idx


def test_catches_all_eight_live_incident_zombies_grouped_on_one_pr(monkeypatch):
    """Достижимость на прод-величинах (issue #1172): реальный инцидент —
    ВОСЕМЬ required-чеков PR #1088, все `queued`/`total_count: 0`, один и
    тот же head_sha (все 8 id перепроверены живым `gh api` минуты назад —
    см. ZOMBIE_RUN_IDS_LIVE_INCIDENT выше). Сторож обязан сгруппировать все
    8 под одним PR и переэмиттить события ОДИН раз (close→reopen чинит все
    required-чеки сразу — не нужно 8 отдельных действий на 8 run'ов)."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": ZOMBIE_RUNS_ALL_EIGHT},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        **{f"actions/runs/{run_id}/jobs": ZOMBIE_JOBS_EMPTY for run_id in ZOMBIE_RUN_IDS_LIVE_INCIDENT},
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    grouped = zrw.group_candidates_by_pr(
        zrw.stale_candidates(ZOMBIE_RUNS_ALL_EIGHT, NOW),
        {"agent/1087-react-dom-peer-major-mismatch": OPEN_PR_1088_MATCHING},
    )
    assert len(grouped[1088]["runs"]) == 8, "все 8 реальных зомби-прогонов обязаны сгруппироваться под PR #1088"
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)
    patch_calls = [c for c in fake.calls if "-X PATCH" in c and "pulls/1088" in c]
    assert any("state=closed" in c for c in patch_calls)
    assert any("state=open" in c for c in patch_calls)


def test_zombie_matching_stale_head_is_not_touched(monkeypatch):
    """Мутация-канарейка: без сравнения head_sha в group_candidates_by_pr
    этот тест ловит ложное срабатывание на устаревшем прогоне."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_STALE_BRANCH],
        "actions/runs/34748469966/jobs": AssertionError("не должен вызываться — прогон устарел"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []


def test_permanent_zombie_after_reopen_is_not_reescalated(monkeypatch):
    """Ключевой сценарий докстринга («Рецидив после действия»): переэмиссия
    уже была, старый run-ID (тот же, что и до неё) остаётся queued/0-jobs
    НАВСЕГДА — новый пульс НЕ должен ни повторно переоткрывать, ни
    эскалировать, ни даже читать число job'ов (короткое замыкание по
    `runs_created_after`)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker,
             "user": {"login": "github-actions[bot]"}},
        ],
        "actions/runs/34748469966/jobs": AssertionError("не должен вызываться — старый permanent-зомби"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("известный старый зомби-прогон" in o for o in observations)


def test_marker_from_other_author_is_not_dedup(monkeypatch):
    """Мутация-гвардия trusted_login (находка ревью PR #1212, #1027):
    маркер в комментарии ЧУЖОГО автора (цитата, пересказ) дедупом не
    является — сторож обязан выполнить газ, а не молча сослаться на чужой
    текст. Снятие фильтра в модуле красит этот тест: чужой комментарий
    заблокировал бы действие."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z",
             "body": f"Цитата чужого отчёта: {reopened_marker}",
             "user": {"login": "some-human"}},
        ],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)


def test_fallback_dedup_anchor_in_watchdog_issue_blocks_repeat_gas(monkeypatch):
    """Случай (в) докстринга — «газ применён, отчёт в PR не записан»:
    эскалация унесла маркер переэмиссии в #120, следующий пульс, не найдя
    маркера в PR, читает его из #120 и ПОВТОРНОЙ переэмиссии не делает
    (контракт «одна попытка на head_sha» держится на обоих носителях)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            {"created_at": "2026-09-13T09:00:05Z",
             "body": f"[zombie-run-watch: отчёт-не-записан PR#1088@9a75c414] "
                     f"Переэмиссия ВЫПОЛНЕНА, отчёт не записан ({reopened_marker})",
             "user": {"login": "github-actions[bot]"}},
        ],
        "actions/runs/34748469966/jobs": AssertionError("не должен вызываться — дедуп по запасному носителю"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("известный старый зомби-прогон" in o for o in observations)


def test_genuine_recurrence_after_reopen_escalates_once(monkeypatch):
    """Новый прогон (созданный ПОСЛЕ переэмиссии) тоже зомби — реальный
    рецидив, эскалация (не повторный close/reopen)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    new_zombie_run = {
        **ZOMBIE_RUN_ORCHESTRA, "id": 999999, "created_at": "2026-09-13T10:00:00Z",
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [new_zombie_run]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker,
             "user": {"login": "github-actions[bot]"}},
        ],
        "actions/runs/999999/jobs": ZOMBIE_JOBS_EMPTY,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "-X POST": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(zrw, "escalate", lambda *a: "Telegram: доставлен; след: оставлен")
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("рецидив после переэмиссии" in a for a in actions)


def test_escalation_not_repeated_once_already_marked(monkeypatch):
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    escalation_marker = "[zombie-run-watch: эскалация PR#1088@9a75c414]"
    new_zombie_run = {
        **ZOMBIE_RUN_ORCHESTRA, "id": 999999, "created_at": "2026-09-13T10:00:00Z",
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [new_zombie_run]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker,
             "user": {"login": "github-actions[bot]"}},
        ],
        "actions/runs/999999/jobs": ZOMBIE_JOBS_EMPTY,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            {"created_at": "2026-09-13T10:05:00Z", "body": escalation_marker,
             "user": {"login": "github-actions[bot]"}},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(
        zrw, "escalate", lambda *a: pytest.fail("уже эскалировано — повтор не нужен"))
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("рецидив уже эскалирован" in o for o in observations)


def test_close_failure_does_not_write_marker_comment(monkeypatch):
    """Форма (а) находки ревью PR #1212: close не прошёл — состояние PR не
    менялось, газа не было, маркер-комментарий НЕ пишется (иначе следующий
    пульс принял бы «переэмиссия уже была» и навсегда заглушил бы газ,
    потратив попытку впустую). Мутация (комментарий до действия) красит
    этот тест."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "state=closed": RuntimeError("secondary rate limit"),
        "state=open": AssertionError("reopen не должен вызываться — close не прошёл"),
        "-X POST": AssertionError("комментарий-маркер при непрошедшем действии не пишется"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("закрытие не выполнено" in o for o in observations)
    assert not any("-X POST" in c for c in fake.calls)


def test_reopen_failure_after_close_escalates_and_reports_closed_pr(monkeypatch):
    """Форма (б): close прошёл, reopen не прошёл — PR ОСТАЛСЯ ЗАКРЫТЫМ,
    сторож смотрит только открытые PR и сам к нему не вернётся, поэтому
    эскалация немедленно, а не в ⚠️ observations. Комментарий-маркер в PR
    не пишется (переэмиссия не состоялась)."""
    calls = patch_escalate(monkeypatch)
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "state=closed": None,
        "state=open": RuntimeError("422 reopen rejected"),
        "-X POST": AssertionError("маркер-комментарий не пишется — переэмиссия не состоялась"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert len(calls) == 1
    assert "ОСТАЛСЯ ЗАКРЫТЫМ" in calls[0][2]
    assert "1088" in calls[0][2]
    assert any("ЗАКРЫТ без переоткрытия" in a for a in actions)


def test_reopen_failure_with_dead_channels_reddens_pulse(monkeypatch):
    """Форма (б) + отказ самой эскалации (оба канала молчат) — молча
    оставить закрытый PR нельзя: прогон краснеет (fail loud)."""
    patch_escalate(monkeypatch, result="Telegram: НЕ доставлен; след в #120: НЕ оставлен")
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "state=closed": None,
        "state=open": RuntimeError("422 reopen rejected"),
        "-X POST": AssertionError("маркер-комментарий не пишется — переэмиссия не состоялась"),
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="остался ЗАКРЫТЫМ"):
        zrw.zombie_run_watch(REPO, NOW)


def test_report_comment_failure_escalates_with_reopened_marker(monkeypatch):
    """Форма (в): газ ПРИМЕНЁН (close и reopen прошли), отчёт-маркер в PR
    не записан — эскалация в #120 обязана нести САМ маркер переэмиссии
    (он становится запасным носителем дедупа, проверен соседним тестом
    test_fallback_dedup_anchor_in_watchdog_issue_blocks_repeat_gas)."""
    calls = patch_escalate(monkeypatch)
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        # POST (запись отчёта в PR) падает, GET-чтения комментариев идут в свои маршруты
        "-X POST": RuntimeError("502 comment post failed"),
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert len(calls) == 1
    assert zrw.REOPENED_MARKER_PREFIX + "9a75c414]" in calls[0][2]
    assert any("переэмиссия ПРИМЕНЕНА" in a for a in actions)
    patch_calls = [c for c in fake.calls if "-X PATCH" in c and "pulls/1088" in c]
    assert any("state=closed" in c for c in patch_calls)
    assert any("state=open" in c for c in patch_calls)


def test_report_failure_with_dead_channels_reddens_pulse(monkeypatch):
    """Форма (в) + отказ самой эскалации: без записи дедупа следующий пульс
    повторил бы переэмиссию, не зная о этой, — молча нельзя, прогон краснеет."""
    patch_escalate(monkeypatch, result="Telegram: НЕ доставлен; след в #120: НЕ оставлен")
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "-X POST": RuntimeError("502 comment post failed"),
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="переэмиссия применена"):
        zrw.zombie_run_watch(REPO, NOW)


def test_report_failure_with_dedup_carrier_lost_reddens_pulse_even_if_telegram_ok(monkeypatch):
    """Находка ревью PR #1212, круг 3, п.3: форма (в) с Telegram ДОСТАВЛЕН,
    но след в #120 НЕ оставлен (escalation_dedup_carrier_failed=True,
    escalation_channel_failed=False — до фикса эта комбинация проходила
    зелёной, т.к. проверялся только один предикат). Маркер переэмиссии не
    найден НИГДЕ (ни в PR — падение записи и есть форма (в), ни в #120 —
    запасной носитель тоже не записан): следующий пульс повторил бы
    переэмиссию вслепую — молча нельзя, прогон обязан покраснеть."""
    patch_escalate(monkeypatch, result="Telegram: доставлен; след в #120: НЕ оставлен")
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "-X POST": RuntimeError("502 comment post failed"),
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="переэмиссия применена"):
        zrw.zombie_run_watch(REPO, NOW)


def test_recurrence_escalation_dead_channels_reddens_pulse(monkeypatch):
    """Находка ревью PR #1212, круг 3, п.2: рецидив после переэмиссии — если
    сама эскалация не доставлена НИ ОДНИМ каналом, сигнал «автогаз не
    сработал, нужен человек» не дошёл никуда — до фикса результат escalate()
    клался в actions и терялся молча, прогон оставался зелёным."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    new_zombie_run = {
        **ZOMBIE_RUN_ORCHESTRA, "id": 999999, "created_at": "2026-09-13T10:00:00Z",
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [new_zombie_run]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            {"created_at": "2026-09-13T09:00:00Z", "body": reopened_marker,
             "user": {"login": "github-actions[bot]"}},
        ],
        "actions/runs/999999/jobs": ZOMBIE_JOBS_EMPTY,
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
    })
    patch_gh(monkeypatch, fake)
    patch_escalate(monkeypatch, result="Telegram: НЕ доставлен; след в #120: НЕ оставлен")
    with pytest.raises(RuntimeError, match="не доставлена ни одним каналом"):
        zrw.zombie_run_watch(REPO, NOW)


# ── set_pr_state: PAT для close/reopen, github.token для остального ────────


def test_set_pr_state_uses_pat_via_raw_subprocess_when_present(monkeypatch):
    """Находка ревью PR #1212, круг 3, п.1: с `GH_PIPELINE_PAT` в окружении
    close/reopen обязаны идти сырым subprocess с заголовком Authorization
    (события GITHUB_TOKEN не зажигают новые workflow-прогоны) — НЕ через
    `gh()` (тот читает `GH_TOKEN`=`github.token` из окружения)."""
    monkeypatch.setenv("GH_PIPELINE_PAT", "test-pat-value")
    monkeypatch.setattr(zrw, "gh", lambda *a: pytest.fail("gh() не должен вызываться — есть PAT"))
    calls = []

    class FakeCompleted:
        returncode = 0
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return FakeCompleted()

    monkeypatch.setattr(zrw.subprocess, "run", fake_run)
    zrw.set_pr_state(REPO, 1088, "closed")
    assert len(calls) == 1
    joined = " ".join(calls[0])
    assert "Authorization: Bearer test-pat-value" in joined
    assert "state=closed" in joined
    assert f"repos/{REPO}/pulls/1088" in joined


def test_set_pr_state_raises_loud_on_pat_failure(monkeypatch):
    """PAT-путь обязан фейлиться так же громко, как gh(), не глотать stderr."""
    monkeypatch.setenv("GH_PIPELINE_PAT", "test-pat-value")

    class FakeFailed:
        returncode = 1
        stderr = "422 Unprocessable Entity"

    monkeypatch.setattr(zrw.subprocess, "run", lambda *a, **kw: FakeFailed())
    with pytest.raises(RuntimeError, match="422 Unprocessable Entity"):
        zrw.set_pr_state(REPO, 1088, "open")


def test_set_pr_state_falls_back_to_gh_without_pat(monkeypatch):
    """Без `GH_PIPELINE_PAT` в окружении (дев/тест-прогон) — честный fallback
    на `gh()`/`GH_TOKEN`, как у `scheduler.update_branch`."""
    monkeypatch.delenv("GH_PIPELINE_PAT", raising=False)
    calls = []
    monkeypatch.setattr(zrw, "gh", lambda *a: calls.append(a))
    zrw.set_pr_state(REPO, 1088, "closed")
    assert len(calls) == 1
    assert "state=closed" in calls[0]


def test_candidate_cap_truncates_and_warns(monkeypatch):
    many = [
        {**HEALTHY_RUN, "id": 100 + i, "created_at": "2026-09-13T00:00:00Z",
         "head_branch": f"branch-{i}"}
        for i in range(25)
    ]
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": many},
        "pulls?state=open": [],
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("потолок цены API" in o for o in observations)


def test_candidate_cap_keeps_oldest_not_newest(monkeypatch):
    """Мутация-гвардия чеклист-находки ревью PR #1212: `/actions/runs`
    отдаёт от свежих к старым (замер 2026-09-14), поэтому усечение обязано
    брать ХВОСТ списка — старейшие. Open PR соответствует только САМОМУ
    старому прогону из 25 — при усечении с головы (новейшие) он вытеснялся
    бы и газ никогда не дошёл бы до самого критичного зомби."""
    many = [
        {**HEALTHY_RUN, "id": 100 + i,
         # свежие ПЕРВЫМИ (прод-порядок API): i=0 — новейший, i=24 — старейший
         "created_at": f"2026-09-13T00:{59 - i:02d}:00Z",
         "head_branch": f"branch-{i}", "head_sha": f"{i:064d}"}
        for i in range(25)
    ]
    oldest_pr = {
        "number": 1300, "state": "open",
        "head": {"ref": "branch-24", "sha": f"{24:064d}"},
    }
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": many},
        "pulls?state=open": [oldest_pr],
        "issues/1300/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/124/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("потолок цены API" in o for o in observations)
    assert any("переоткрыт" in a for a in actions)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
