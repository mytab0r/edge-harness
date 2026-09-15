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
- `path`/`workflow_id` прогонов и id workflow — живой `gh api
  /actions/workflows` и живые снимки прогонов (2026-09-14, доводка
  PR #1212, круг 4): сторож классифицирует workflow по версии файла на
  head (Contents API) и занятости его concurrency-группы (Actions API).
- `*_YML_HEAD` — дословные фрагменты реальных `.github/workflows/*.yml`
  (сняты 2026-09-14): quota-watch — workflow-level `concurrency` с
  `cancel-in-progress: false` (легитимное ожидание очереди возможно),
  dsh-edge-pr-smoke — с `cancel-in-progress: true`, orchestra — только
  job-level `concurrency` (классификацией игнорируется).
- Маркеры в фикстурах-комментариях авторства `github-actions[bot]` — это
  ПРОД-форма: workflow пишет под `github.token`, а чтение маркеров в модуле
  фильтрует автора `trusted_login=EVENT_ACTOR_LOGIN` (находка ревью PR
  #1212, #1027) — комментарий чужого автора маркер-дедуп не создаёт и
  проверяется отдельным тестом ниже.

Запуск: python -m pytest scripts/orchestra/test_zombie_run_watch.py -q
"""

import base64
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


@pytest.fixture(autouse=True)
def _outside_actions(monkeypatch):
    """Газ-путь в тестах исполняется через FakeGh (вне боевого гейта):
    на раннере `GITHUB_ACTIONS=true` честный отказ `set_pr_state` без
    `GH_PIPELINE_PAT` (находка ревью PR #1212, круг 5) красил бы ВСЕ
    сценарии газа ещё до действия. Тест самого гейта
    (test_set_pr_state_no_pat_in_prod_fails_before_action) перекрывает
    это своей подменой `prod_writes_allowed` на True."""
    monkeypatch.setattr(zrw, "prod_writes_allowed", lambda: False)


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


def contents_payload(text: str) -> dict:
    """Прод-форма ответа Contents API на файл: `{content: <base64>,
    encoding: "base64"}` (живой ответ `gh api repos/.../contents/...`)."""
    return {
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


def comment(created_at: str, body: str) -> dict:
    return {
        "created_at": created_at,
        "body": body,
        "user": {"login": "github-actions[bot]"},
    }


# Реальные (path, workflow_id) восьми зомби-прогонов #1106 и workflow-файлов
# вообще — живой `gh api repos/mytab0r/edge-harness/actions/workflows`
# (2026-09-14, доводка PR #1212, круг 4).
WORKFLOW_ID = {
    "orchestra.yml": 344622326,
    "pr-review.yml": 344702742,
    "quota-watch.yml": 351882846,
    "dsh-edge-pr-smoke.yml": 351861292,
    "repo-ci.yml": 345011826,
    "secret-scan.yml": 352321814,
    "codeql.yml": 344758060,
    "worker-ci.yml": 344591013,
}

# Дословные фрагменты реальных `.github/workflows/*.yml` (2026-09-14) —
# прод-форма для Contents API-маршрутов фикстур.
QUOTA_WATCH_YML_HEAD = (
    "name: quota-watch\n"
    "\n"
    "concurrency:\n"
    "  group: quota-watch\n"
    "  cancel-in-progress: false\n"
)
DSH_EDGE_PR_SMOKE_YML_HEAD = (
    "name: dsh-edge-pr-smoke\n"
    "\n"
    "concurrency:\n"
    "  group: dsh-edge-pr-smoke-${{ github.event.pull_request.number }}\n"
    "  cancel-in-progress: true\n"
)
ORCHESTRA_YML_HEAD = (
    "name: orchestra\n"
    "\n"
    "jobs:\n"
    "  orchestra:\n"
    "    # Глобальная группа — архитектурный замок из шапки файла: два слияния\n"
    "    # никогда не идут параллельно.\n"
    "    concurrency:\n"
    "      group: orchestra\n"
    "      cancel-in-progress: false\n"
)
REPO_CI_YML_HEAD = "name: repo-ci\n\non:\n  pull_request:\n"

HEAD_SHA_1088 = "9a75c4142a020d8d57f50952acab25b957c086ce"


def contents_routes(**files: str) -> dict:
    """Маршруты Contents API для тестового FakeGh: имя файла workflow ->
    текст его head-версии. Ключ маршрута — часть URL, которую FakeGh
    ищет подстрокой."""
    return {
        f"contents/.github/workflows/{name}": contents_payload(text)
        for name, text in files.items()
    }


# Маршрут Contents API по умолчанию для фикстур на ZOMBIE_RUN_ORCHESTRA:
# head-версия orchestra.yml — только job-level concurrency (гейт «none»),
# газ напрямую, без чтения занятости группы (прод-факт 2026-09-14).
ORCHESTRA_HEAD_ROUTE = contents_routes(**{"orchestra.yml": ORCHESTRA_YML_HEAD})


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
    "head_sha": HEAD_SHA_1088,
    "head_branch": "agent/1087-react-dom-peer-major-mismatch",
    "path": ".github/workflows/orchestra.yml",
    "workflow_id": WORKFLOW_ID["orchestra.yml"],
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
# текущем head) — та форма, для которой сторож и написан. path/workflow_id
# каждого — живой снимок того же прогона (2026-09-14): среди восьми ЕСТЬ
# workflow с concurrency-группами (quota-watch — blocking, dsh-edge-pr-smoke
# — cancelling) — полное исключение сгруппированных лишило бы газ трети
# реального класса (находка ревью PR #1212, круг 4).
ZOMBIE_RUN_IDS_LIVE_INCIDENT = [
    34748469966, 34748469981, 34748469982, 34748469975,
    34748469992, 34748470003, 34748470010, 34748470011,
]
LIVE_INCIDENT_PATHS = [
    "orchestra.yml", "pr-review.yml", "quota-watch.yml", "dsh-edge-pr-smoke.yml",
    "repo-ci.yml", "secret-scan.yml", "codeql.yml", "worker-ci.yml",
]
ZOMBIE_RUNS_ALL_EIGHT = [
    {**ZOMBIE_RUN_ORCHESTRA, "id": run_id, "name": Path(path).stem,
     "path": f".github/workflows/{path}", "workflow_id": WORKFLOW_ID[path]}
    for run_id, path in zip(ZOMBIE_RUN_IDS_LIVE_INCIDENT, LIVE_INCIDENT_PATHS)
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
    "path": ".github/workflows/repo-ci.yml",
    "workflow_id": WORKFLOW_ID["repo-ci.yml"],
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        # head-версии всех восьми workflow (прод-имена файлов живого замера);
        # quota-watch — blocking-группа: маршрут занятости ниже отвечает
        # «свободна» (нет in_progress прогонов) — газ разрешён.
        **contents_routes(**{
            "orchestra.yml": ORCHESTRA_YML_HEAD,
            "pr-review.yml": REPO_CI_YML_HEAD,  # гейт «none», форма неважна
            "quota-watch.yml": QUOTA_WATCH_YML_HEAD,
            "dsh-edge-pr-smoke.yml": DSH_EDGE_PR_SMOKE_YML_HEAD,
            "repo-ci.yml": REPO_CI_YML_HEAD,
            "secret-scan.yml": REPO_CI_YML_HEAD,
            "codeql.yml": REPO_CI_YML_HEAD,
            "worker-ci.yml": REPO_CI_YML_HEAD,
        }),
        f"workflows/{WORKFLOW_ID['quota-watch.yml']}/runs?status=in_progress":
            {"total_count": 0, "workflow_runs": []},
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
    `runs_created_after`). Переэмиссия при этом ПОРОДИЛА новые прогоны
    (id больше старых, любой статус — здесь завершённый) — «не трогаю»
    честно (чеклист-находка ревью PR #1212)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [
            comment("2026-09-13T09:00:00Z", reopened_marker),
        ],
        # prогоны, порождённые переэмиссией: id больше старого 34748469966
        "actions/runs?head_sha=": {"total_count": 2, "workflow_runs": [
            ZOMBIE_RUN_ORCHESTRA, {"id": 34750000000, "status": "completed"},
        ]},
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
        **ORCHESTRA_HEAD_ROUTE,
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
        # запасной носитель подтвердил переэмиссию; она породила новые прогоны
        "actions/runs?head_sha=": {"total_count": 2, "workflow_runs": [
            ZOMBIE_RUN_ORCHESTRA, {"id": 34750000000, "status": "completed"},
        ]},
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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
        **ORCHESTRA_HEAD_ROUTE,
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


def test_set_pr_state_no_pat_in_prod_fails_before_action(monkeypatch):
    """Находка ревью PR #1212 (круг 5): в БОЕВОМ прогоне (prod_writes_allowed)
    пустой `GH_PIPELINE_PAT` — RuntimeError ДО действия, не тихий fallback:
    close→reopen под github.token прошёл бы успешными PATCH'ами, не зажигая
    ни одного прогона; маркер-отчёт записался бы, и дедуп «одна попытка на
    head_sha» запер бы сторож навсегда при зелёных прогонах."""
    monkeypatch.delenv("GH_PIPELINE_PAT", raising=False)
    monkeypatch.setattr(zrw, "prod_writes_allowed", lambda: True)
    monkeypatch.setattr(
        zrw, "gh", lambda *a: pytest.fail("gh() не должен вызываться — честный отказ до действия"))
    with pytest.raises(RuntimeError, match="GH_PIPELINE_PAT отсутствует в боевом прогоне"):
        zrw.set_pr_state(REPO, 1088, "closed")


def test_jobs_budget_bounds_expensive_checks_and_serves_oldest_first(monkeypatch):
    """Потолок цены (находка ревью PR #1212, круг 4): после группировки по
    открытым PR потолок держится на ДОРОГИХ /jobs-вызовах — не больше
    MAX_JOBS_CHECKS_PER_PULSE за пульс; кандидаты проверяются старейшие
    первыми, остаток честно переносится на следующий пульс."""
    many = [
        # прод-порядок API: от свежих к старым; i=0 — старейший
        {**HEALTHY_RUN, "id": 500 + i,
         "created_at": f"2026-09-13T00:{i:02d}:00Z",
         "head_branch": f"branch-{i}", "head_sha": f"{i:064d}"}
        for i in range(25)
    ]
    many.reverse()
    open_prs = [
        {"number": 1300 + i, "state": "open",
         "head": {"ref": f"branch-{i}", "sha": f"{i:064d}"}}
        for i in range(25)
    ]
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": many},
        "pulls?state=open": open_prs,
        **contents_routes(**{"repo-ci.yml": REPO_CI_YML_HEAD}),
        "-X POST": None,
        "-X PATCH": None,
        "issues/": [],  # комментарии всех 25 PR и #120 — без маркеров
        "actions/runs/": ZOMBIE_JOBS_EMPTY,  # все прогоны — зомби
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("бюджет /jobs-проверок за пульс исчерпан" in o for o in observations)
    jobs_calls = [c for c in fake.calls if "/jobs" in c]
    assert len(jobs_calls) == zrw.MAX_JOBS_CHECKS_PER_PULSE
    # газ дошёл ровно до 20 старейших: их /jobs проверены и они переоткрыты
    assert len([a for a in actions if "переоткрыт" in a]) == zrw.MAX_JOBS_CHECKS_PER_PULSE
    checked_ids = sorted(int(c.rsplit("/", 2)[-2]) for c in jobs_calls)
    assert checked_ids == list(range(500, 500 + zrw.MAX_JOBS_CHECKS_PER_PULSE))


def test_permanent_zombies_of_merged_prs_do_not_displace_open_pr_zombie(monkeypatch):
    """Находка ревью PR #1212 (круг 4), мутация-гвардия потолка: усечение
    сырого списка ДО группировки брало старейших — а permanent-зомби
    слитых/закрытых PR остаются queued навсегда (восемь штук #1088 живы в
    проде) и потому всегда старейшие: 24 таких вытесняли единственного
    зомби ОТКРЫТОГО PR, газ до него никогда не доходил, прогон был зелёным
    с отчётом «действие не требуется». Прод-раскладка обратная старому
    тесту: permanent-зомби (чужие ветки, без открытых PR) СТАРЕЕ, зомби
    открытого PR — НОВЕЙШИЙ. Газ обязан дойти до открытого PR, /jobs и
    Contents-чтения по permanent-зомби не тратятся вовсе."""
    permanent = [
        {**HEALTHY_RUN, "id": 600 + i,
         "created_at": f"2026-09-12T0{i % 10}:00:00Z",
         "head_branch": f"dead-branch-{i}", "head_sha": f"{100 + i:064d}"}
        for i in range(24)
    ]
    # прод-порядок API: от свежих к старым — зомби открытого PR первым
    many = [ZOMBIE_RUN_ORCHESTRA] + permanent
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": many},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        **ORCHESTRA_HEAD_ROUTE,
        "issues/1088/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": ZOMBIE_JOBS_EMPTY,
        "600": AssertionError("permanent-зомби не группируются — /jobs не вызывается"),
        "contents/.github/workflows/repo-ci.yml": AssertionError(
            "permanent-зомби отбракованы группировкой — их workflow не читается"),
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)
    jobs_calls = [c for c in fake.calls if "/jobs" in c]
    assert jobs_calls == [f"repos/{REPO}/actions/runs/34748469966/jobs"]


def test_reopen_that_produced_no_runs_escalates(monkeypatch):
    """Чеклист-находка ревью PR #1212: маркер переэмиссии есть, новых
    queued-прогонов нет — но на head НЕ ПОЯВИЛОСЬ НИ ОДНОГО нового прогона
    вообще (id не выше старых): газ прошёл вхолостую, PR стоит, молча
    говорить «не трогаю» нельзя — эскалация со своим дедуп-маркером."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    calls = patch_escalate(monkeypatch)
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [comment("2026-09-13T09:00:00Z", reopened_marker)],
        # на head нет прогонов с id выше старого — переэмиссия вхолостую
        "actions/runs?head_sha=": {"total_count": 1, "workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/34748469966/jobs": AssertionError(
            "старый permanent-зомби не проверяется — решение уже принято"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert len(calls) == 1
    assert zrw.NO_RUNS_MARKER_PREFIX + "PR#1088@9a75c414]" in calls[0][2]
    assert any("ВХОЛОСТУЮ" in a for a in actions)


def test_idle_reopen_escalation_not_repeated_once_marked(monkeypatch):
    """Дедуп холостой переэмиссии: маркер в #120 уже есть — повторной
    эскалации нет («не повторяю»), прогон зелёный."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    idle_marker = zrw.NO_RUNS_MARKER_PREFIX + "PR#1088@9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [comment("2026-09-13T09:00:00Z", reopened_marker)],
        "actions/runs?head_sha=": {"total_count": 1, "workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [
            comment("2026-09-13T09:20:00Z", f"{idle_marker} Переэмиссия ВЫПОЛНЕНА, "
                                           "но ни одного нового прогона..."),
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(
        zrw, "escalate", lambda *a, **kw: pytest.fail("уже эскалировано — повтор не нужен"))
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("уже эскалирована" in o for o in observations)


def test_idle_reopen_escalation_dead_carrier_reddens_pulse(monkeypatch):
    """Холостая переэмиссия + эскалация не записана ни одним каналом —
    маркер-дедуп не появится и следующий пульс повторил бы эскалацию
    каждые 15 минут: красный прогон (тот же класс, что форма (в))."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    patch_escalate(monkeypatch, result="Telegram: НЕ доставлен; след в #120: НЕ оставлен")
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [comment("2026-09-13T09:00:00Z", reopened_marker)],
        "actions/runs?head_sha=": {"total_count": 1, "workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="переэмиссия вхолостую"):
        zrw.zombie_run_watch(REPO, NOW)


def test_head_runs_unreadable_defers_known_zombie_decision(monkeypatch):
    """Прогоны на head не прочитаны — решение «не трогаю» НЕ принимается
    вслепую: ⚠️ и откладывание на следующий пульс (это та ветка, где
    холостая переэмиссия отличима от нормальной)."""
    reopened_marker = "[zombie-run-watch: переэмиссия 9a75c414]"
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [ZOMBIE_RUN_ORCHESTRA]},
        "pulls?state=open": [OPEN_PR_1088_MATCHING],
        "issues/1088/comments": [comment("2026-09-13T09:00:00Z", reopened_marker)],
        "actions/runs?head_sha=": RuntimeError("502 bad gateway"),
        "actions/runs/34748469966/jobs": AssertionError("/jobs не вызывается"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("решение отложено" in o for o in observations)


# ── Различение «зомби #1106» от легитимного ожидания concurrency-группы ────
# (находка ревью PR #1212, круг 4; обоснование — докстринг модуля)


def quota_watch_zombie() -> dict:
    return {**HEALTHY_RUN, "id": 777,
            "created_at": "2026-09-13T08:46:40Z",  # давний — за порогом
            "path": ".github/workflows/quota-watch.yml",
            "workflow_id": WORKFLOW_ID["quota-watch.yml"]}


def test_group_wait_of_blocking_workflow_is_not_gassed(monkeypatch):
    """queued+0 job'ов у workflow с workflow-level cancel-in-progress:
    false (прод-форма quota-watch.yml — PR-триггер и собственный cron в
    общей группе) при ЗАНЯТОЙ группе (есть in_progress прогон того же
    workflow) — легитимное ожидание очереди: газа нет, /jobs не читается,
    наблюдение называет причину. Маркеры дедупа читаются ДО различения
    (дедуп первичен — PR-носитель решает, есть ли что проверять вообще)."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued":
            {"workflow_runs": [quota_watch_zombie()]},
        "pulls?state=open": [OPEN_PR_1209],
        **contents_routes(**{"quota-watch.yml": QUOTA_WATCH_YML_HEAD}),
        f"workflows/{WORKFLOW_ID['quota-watch.yml']}/runs?status=in_progress":
            {"total_count": 1, "workflow_runs": [{"id": 888}]},
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": AssertionError(
            "ожидание группы не зомби — /jobs не вызывается"),
        "-X POST": AssertionError("газа быть не должно"),
        "-X PATCH": AssertionError("газа быть не должно"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("ожидание concurrency-группы" in o for o in observations)


def test_grouped_workflow_zombie_is_gassed_when_group_free(monkeypatch):
    """Тот же blocking-workflow при СВОБОДНОЙ группе (ни одного
    in_progress-прогона): queued+0 job'ов ≥ порога — зомби (прод-факт:
    реальные зомби #1106 34748469982 и 34748469975 — прогоны workflow
    именно с группами), газ обязана пройти."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued":
            {"workflow_runs": [quota_watch_zombie()]},
        "pulls?state=open": [OPEN_PR_1209],
        **contents_routes(**{"quota-watch.yml": QUOTA_WATCH_YML_HEAD}),
        f"workflows/{WORKFLOW_ID['quota-watch.yml']}/runs?status=in_progress":
            {"total_count": 0, "workflow_runs": []},
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)


def test_cancelling_gate_workflow_is_gassed_without_occupancy_check(monkeypatch):
    """cancel-in-progress: true (прод-форма dsh-edge-pr-smoke.yml):
    новый прогон группы отменяет старый — накопительного ожидания очереди
    не бывает, queued+0 job'ов ≥ порога — зомби; занятость группы даже не
    читается (маршрута нет — вызов упал бы громко)."""
    zombie = {**quota_watch_zombie(), "id": 778,
              "path": ".github/workflows/dsh-edge-pr-smoke.yml",
              "workflow_id": WORKFLOW_ID["dsh-edge-pr-smoke.yml"]}
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [zombie]},
        "pulls?state=open": [OPEN_PR_1209],
        **contents_routes(**{"dsh-edge-pr-smoke.yml": DSH_EDGE_PR_SMOKE_YML_HEAD}),
        f"workflows/{WORKFLOW_ID['dsh-edge-pr-smoke.yml']}/runs?status=in_progress":
            AssertionError("cancelling-гейт не читает занятость группы"),
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/778/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)


def test_head_workflow_version_unreadable_defers_gas(monkeypatch):
    """Версия workflow-файла на head не прочитана (Contents API упал) —
    газ откладывается с ⚠️: queued+0 job'ов могло быть легитимным
    ожиданием группы, гадать нельзя; прогон остаётся зелёным, PR
    переходит на следующий пульс."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued":
            {"workflow_runs": [quota_watch_zombie()]},
        "pulls?state=open": [OPEN_PR_1209],
        "contents/.github/workflows/quota-watch.yml": RuntimeError("502 bad gateway"),
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": AssertionError("газ отложен — /jobs не вызывается"),
        "-X POST": AssertionError("газа быть не должно"),
        "-X PATCH": AssertionError("газа быть не должно"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("не прочитан" in o and "отложен" in o for o in observations)


def test_occupancy_unreadable_defers_gas_conservatively(monkeypatch):
    """Занятость группы не прочитана (Actions API упал) — консервативно
    считаем группу занятой: газ отложен с ⚠️, не гадаем."""
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued":
            {"workflow_runs": [quota_watch_zombie()]},
        "pulls?state=open": [OPEN_PR_1209],
        **contents_routes(**{"quota-watch.yml": QUOTA_WATCH_YML_HEAD}),
        f"workflows/{WORKFLOW_ID['quota-watch.yml']}/runs?status=in_progress":
            RuntimeError("403 forbidden"),
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": AssertionError("газ отложен — /jobs не вызывается"),
        "-X POST": AssertionError("газа быть не должно"),
        "-X PATCH": AssertionError("газа быть не должно"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("занятость группы" in o for o in observations)


def test_run_without_path_defers_gas(monkeypatch):
    """Прогон без `path` (форма ответа сломана) — газ отложен с ⚠️:
    принадлежность к workflow с группой не установить."""
    zombie = {**quota_watch_zombie()}
    del zombie["path"]
    del zombie["workflow_id"]
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued": {"workflow_runs": [zombie]},
        "pulls?state=open": [OPEN_PR_1209],
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": AssertionError("газ отложен — /jobs не вызывается"),
        "-X POST": AssertionError("газа быть не должно"),
        "-X PATCH": AssertionError("газа быть не должно"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert actions == []
    assert any("без `path`" in o for o in observations)


def test_mixed_pr_group_wait_deferred_and_zombie_gassed(monkeypatch):
    """Смешанный PR: quota-watch-прогон легитимно ждёт занятую группу,
    repo-ci-прогон — зомби. Газ обязан пойти ТОЛЬКО по отфильтрованному
    списку (газ-допустимым): ожидающий группы прогон не проверяется /jobs
    и не газится, даже если он старейший в PR. Мутация (проверять сырой
    runs_to_check вместо eligible) красит этот тест ложным газом."""
    waiting = quota_watch_zombie()  # старейший, но ждёт занятую группу
    zombie = {**HEALTHY_RUN, "id": 779,
              "created_at": "2026-09-13T09:00:00Z",  # новее ожидающего
              "path": ".github/workflows/repo-ci.yml",
              "workflow_id": WORKFLOW_ID["repo-ci.yml"]}
    fake = FakeGh({
        "actions/runs?event=pull_request&status=queued":
            {"workflow_runs": [waiting, zombie]},
        "pulls?state=open": [OPEN_PR_1209],
        **contents_routes(**{"quota-watch.yml": QUOTA_WATCH_YML_HEAD,
                             "repo-ci.yml": REPO_CI_YML_HEAD}),
        f"workflows/{WORKFLOW_ID['quota-watch.yml']}/runs?status=in_progress":
            {"total_count": 1, "workflow_runs": [{"id": 888}]},
        "issues/1209/comments": [],
        f"issues/{pg.WATCHDOG_ISSUE}/comments": [],
        "actions/runs/777/jobs": AssertionError(
            "ожидающий группы прогон не проверяется /jobs"),
        "actions/runs/779/jobs": ZOMBIE_JOBS_EMPTY,
        "-X POST": None,
        "-X PATCH": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = zrw.zombie_run_watch(REPO, NOW)
    assert any("переоткрыт" in a for a in actions)
    assert any("779" in a for a in actions), "газ должен идти по зомби-прогону repo-ci"
    assert any("ожидание concurrency-группы" in o for o in observations)
    assert not any("/runs/777/jobs" in c for c in fake.calls)


# ── workflow_queue_gate: классификация prod-формы concurrency ──────────────

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_gate_classifications_prod_snippets():
    none_text = "name: x\non: push\njobs:\n  a:\n    runs-on: ubuntu\n"
    assert zrw.workflow_queue_gate(none_text) == zrw.QUEUE_GATE_NONE
    assert zrw.workflow_queue_gate(
        "on: push\nconcurrency:\n  group: q\njobs:\n  a: {}\n") == zrw.QUEUE_GATE_BLOCKING
    assert zrw.workflow_queue_gate(
        "on: push\nconcurrency:\n  group: q\n  cancel-in-progress: false\n"
        "jobs:\n  a: {}\n") == zrw.QUEUE_GATE_BLOCKING
    assert zrw.workflow_queue_gate(
        "on: push\nconcurrency:\n  group: x-${{ github.event.pull_request.number }}\n"
        "  cancel-in-progress: true\njobs:\n  a: {}\n") == zrw.QUEUE_GATE_CANCELLING
    # скалярная форма без cancel-in-progress — дефолт false → blocking
    assert zrw.workflow_queue_gate(
        "on: push\nconcurrency: my-group\njobs:\n  a: {}\n") == zrw.QUEUE_GATE_BLOCKING
    # flow-форма с cancel-in-progress: true
    assert zrw.workflow_queue_gate(
        "on: push\nconcurrency: {group: g, cancel-in-progress: true}\n"
        "jobs:\n  a: {}\n") == zrw.QUEUE_GATE_CANCELLING
    # только job-level concurrency (прод-форма orchestra.yml) — гейт «none»
    assert zrw.workflow_queue_gate(ORCHESTRA_YML_HEAD) == zrw.QUEUE_GATE_NONE
    # комментарий не считается объявлением
    assert zrw.workflow_queue_gate(
        "on: push\n# concurrency:\n#   group: q\njobs:\n  a: {}\n") == zrw.QUEUE_GATE_NONE


def test_gate_real_workflow_files_prod_facts():
    """Прод-факты (2026-09-14): quota-watch — blocking (PR-триггер в общей
    группе с cron'ом), dsh-edge-pr-smoke — cancelling, orchestra/repo-ci —
    none. Читаются НАСТОЯЩИЕ файлы репозитория, не пересказ."""
    workflows = REPO_ROOT / ".github" / "workflows"
    read = lambda name: (workflows / name).read_text(encoding="utf-8")
    assert zrw.workflow_queue_gate(read("quota-watch.yml")) == zrw.QUEUE_GATE_BLOCKING
    assert zrw.workflow_queue_gate(read("dsh-edge-pr-smoke.yml")) == zrw.QUEUE_GATE_CANCELLING
    assert zrw.workflow_queue_gate(read("orchestra.yml")) == zrw.QUEUE_GATE_NONE
    assert zrw.workflow_queue_gate(read("repo-ci.yml")) == zrw.QUEUE_GATE_NONE


def test_gate_agrees_with_pyyaml_on_all_real_workflows():
    """Построчный разбор (runtime сторожа без PyYAML) сверяется с PyYAML
    на всех реальных workflow: дрейф парсера ловится здесь, а не в проде
    первым красным прогоном или ложным газом."""
    yaml = pytest.importorskip("yaml")
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        conc = doc.get("concurrency") if isinstance(doc, dict) else None
        if conc is None:
            expected = zrw.QUEUE_GATE_NONE
        else:
            cip = conc.get("cancel-in-progress") if isinstance(conc, dict) else None
            expected = (zrw.QUEUE_GATE_CANCELLING if str(cip).lower() == "true"
                        else zrw.QUEUE_GATE_BLOCKING)
        actual = zrw.workflow_queue_gate(path.read_text(encoding="utf-8"))
        assert actual == expected, (
            f"{path.name}: построчный разбор {actual!r} != PyYAML {expected!r}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
