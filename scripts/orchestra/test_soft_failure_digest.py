#!/usr/bin/env python3
"""Тесты дайджеста мягких отказов (scripts/orchestra/soft_failure_digest.py,
#1121).

Фикстуры — прод-форма: `REAL_NO_ADAPTER_ANNOTATION` дословный снимок живой
аннотации `gh api repos/mytab0r/edge-harness/check-runs/103709785923/
annotations` (2026-09-13, прогон worker.yml 34751913956, ДО фикса #1097);
`REAL_MULTI_SUITE_CHECK_RUNS` — дословный (усечённый по полям, которые
никто не читает) снимок `gh api repos/mytab0r/edge-harness/commits/
f34571b3.../check-runs` того же прогона: 12 check-runs на одном sha, из
которых прогону принадлежат только 2 (check_suite_id 94121634096) — живое
доказательство, зачем нужен фильтр по check_suite_id (без него аннотации
ЧУЖИХ прогонов на том же коммите утекли бы в группы этого прогона).
`REAL_ROLLBACK_STEP_HISTORY` — форма `gh api repos/mytab0r/edge-harness/
actions/runs/{id}/jobs` по 4 последним прогонам deploy-dsh-edge.yml
(2026-09-13): шаг «Автооткат прода при красной канарейке/смоуке» —
skipped/skipped/skipped/success — ровно тот паттерн «условного» шага,
который канал B обязан поймать.

Запуск: python -m pytest scripts/orchestra/test_soft_failure_digest.py -q
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg

SCRIPT = _DIR / "soft_failure_digest.py"
spec = importlib.util.spec_from_file_location("soft_failure_digest", SCRIPT)
sfd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sfd)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(sfd, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class FakeGh:
    """Маршрутизация по подстроке — тот же приём, что
    test_dependabot_alert_watch.py::FakeGh (образец в этом же каталоге).
    Порядок routes важен: более специфичные фрагменты — раньше общих."""

    def __init__(self, routes: list[tuple[str, object]]):
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes:
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


REPO = "mytab0r/edge-harness"
NOW = utc(2026, 9, 13, 12, 0)

# Дословный снимок (2026-09-13, worker.yml, прогон 34751913956, ДО фикса
# #1097) — check-runs/103709785923/annotations. Оставлена вторая (notice)
# аннотация того же ответа — реальный ответ несёт обе.
REAL_NO_ADAPTER_ANNOTATION = {
    "path": ".github",
    "start_line": 178,
    "annotation_level": "warning",
    "title": "",
    "message": (
        "быстрый провайдер Claude (anthropic-oauth-pool) отказал (rc=1), "
        "причина: dsh: NO_ADAPTER: no adapter registered for provider "
        '"anthropic-pool" [anthropic-pool] provider setup failed: TypeError: '
        "Cannot read properties of undefined (reading 'update')     at "
        "ensureProvider (fi — пробую цепочку "
        "vars.DSH_PROVIDER_CHAIN/манифеста использования (#838)"
    ),
    "raw_details": "",
}

REAL_NOTICE_ANNOTATION = {
    "path": ".github",
    "start_line": 126,
    "annotation_level": "notice",
    "title": "",
    "message": (
        "плагин-сьют не сконфигурирован: vars.PLUGINS_SUITE_URL не задан "
        "— используется дефолтная конфигурация переключателя по "
        "vars.DEEPSEEK_* (#215)"
    ),
    "raw_details": "",
}

# Требование задачи: fixture обязана нести дословный UNKNOWN_MODEL —
# тот же реальный шаблон сообщения (task.sh), что и NO_ADAPTER выше, со
# сменённой причиной — форма подтверждена живым замером NO_ADAPTER, вторая
# причина того же источника не наблюдалась в выборке живьём, текст
# конструируется по ТОЙ ЖЕ подтверждённой форме (см. докстринг модуля,
# «Не подтверждено» — честно об этом).
REAL_UNKNOWN_MODEL_ANNOTATION = {
    "path": ".github",
    "start_line": 178,
    "annotation_level": "warning",
    "title": "",
    "message": (
        "быстрый провайдер Claude (anthropic-oauth-pool) отказал (rc=1), "
        'причина: dsh: UNKNOWN_MODEL: pi-ai provider "anthropic-pool" has '
        'no configured model "claude-sonnet-4-5" — пробую цепочку '
        "vars.DSH_PROVIDER_CHAIN/манифеста использования (#838)"
    ),
    "raw_details": "",
}

# Дословный (усечён по нечитаемым полям) снимок check-runs по sha прогона
# 34751913956 — 12 check-runs на одном sha, ИЗ НИХ прогону принадлежат
# только 2 (check_suite_id 94121634096: "orchestra" и "task").
REAL_MULTI_SUITE_CHECK_RUNS = {
    "total_count": 12,
    "check_runs": [
        {"id": 103712889426, "name": "verdict", "check_suite": {"id": 94122912677}, "output": {"annotations_count": 0}},
        {"id": 103712271012, "name": "contract", "check_suite": {"id": 94123944658}, "output": {"annotations_count": 0}},
        {"id": 103712270464, "name": "orchestra", "check_suite": {"id": 94123944658}, "output": {"annotations_count": 1}},
        {"id": 103711807952, "name": "verdict", "check_suite": {"id": 94122349415}, "output": {"annotations_count": 0}},
        {"id": 103711546703, "name": "verdict", "check_suite": {"id": 94121888754}, "output": {"annotations_count": 0}},
        {"id": 103711147000, "name": "review", "check_suite": {"id": 94122912677}, "output": {"annotations_count": 0}},
        {"id": 103710540409, "name": "review", "check_suite": {"id": 94122349415}, "output": {"annotations_count": 0}},
        {"id": 103710054304, "name": "review", "check_suite": {"id": 94121888754}, "output": {"annotations_count": 0}},
        # Эта пара — check_suite_id 94121634096 — ПРИНАДЛЕЖИТ прогону 34751913956.
        {"id": 103709785923, "name": "task", "check_suite": {"id": 94121634096}, "output": {"annotations_count": 2}},
        {"id": 103709627027, "name": "analyze", "check_suite": {"id": 94121483761}, "output": {"annotations_count": 0}},
        {"id": 103709624686, "name": "archive-fixup", "check_suite": {"id": 94121480964}, "output": {"annotations_count": 0}},
        {"id": 103709624223, "name": "test", "check_suite": {"id": 94121480964}, "output": {"annotations_count": 44}},
    ],
}


def make_run(run_id: int, head_sha: str, check_suite_id: int, event: str,
             conclusion: str, updated_at: str) -> dict:
    return {
        "id": run_id, "head_sha": head_sha, "check_suite_id": check_suite_id,
        "event": event, "conclusion": conclusion,
        "created_at": updated_at, "updated_at": updated_at,
        "html_url": f"https://github.com/mytab0r/edge-harness/actions/runs/{run_id}",
    }


# ── Чистые функции ────────────────────────────────────────────────────────


def test_normalize_text_strips_hex_and_digits_keeps_signature():
    normalized = sfd.normalize_text(REAL_NO_ADAPTER_ANNOTATION["message"])
    assert "no_adapter: no adapter registered for provider" in normalized
    assert "838" not in normalized  # число заменено на N


def test_normalize_text_empty_is_empty():
    assert sfd.normalize_text("") == ""
    assert sfd.normalize_text(None) == ""


def test_group_fingerprint_stable_and_distinguishes_inputs():
    a = sfd.group_fingerprint("note", "worker.yml", "task", "no_adapter")
    b = sfd.group_fingerprint("note", "worker.yml", "task", "no_adapter")
    c = sfd.group_fingerprint("note", "worker.yml", "task", "unknown_model")
    assert a == b
    assert a != c


def test_relevant_run_excludes_orchestra_pull_request_noise():
    pr_run = {"event": "pull_request"}
    dispatch_run = {"event": "workflow_dispatch"}
    assert sfd.relevant_run("orchestra.yml", pr_run) is False
    assert sfd.relevant_run("orchestra.yml", dispatch_run) is True
    # Тот же фильтр НЕ применяется к другим workflow — worker.yml не несёт
    # job'а contract, событие pull_request у него нерелевантно самому классу шума.
    assert sfd.relevant_run("worker.yml", pr_run) is True


def test_add_annotation_record_aggregates_repeats_across_runs():
    groups: dict = {}
    for i in range(3):
        sfd.add_annotation_record(
            groups, "worker.yml", "task", "warning",
            REAL_NO_ADAPTER_ANNOTATION["message"],
            utc(2026, 9, 13, 10 + i, 0), f"https://example/{i}")
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert group["count"] == 3
    assert group["first_seen"] == utc(2026, 9, 13, 10, 0)
    assert group["last_seen"] == utc(2026, 9, 13, 12, 0)
    assert len(group["run_urls"]) == 3


def test_add_annotation_record_distinguishes_no_adapter_from_unknown_model():
    groups: dict = {}
    sfd.add_annotation_record(groups, "worker.yml", "task", "warning",
                               REAL_NO_ADAPTER_ANNOTATION["message"], NOW, "u1")
    sfd.add_annotation_record(groups, "worker.yml", "task", "warning",
                               REAL_UNKNOWN_MODEL_ANNOTATION["message"], NOW, "u2")
    assert len(groups) == 2


def test_step_display_name_uses_name_or_falls_back_to_run_uses():
    assert sfd.step_display_name({"name": "Собрать"}) == "Собрать"
    assert sfd.step_display_name({"uses": "actions/checkout@v7"}) == "Run actions/checkout@v7"
    assert sfd.step_display_name({}) == "?"


def test_load_conditional_steps_only_collects_steps_with_explicit_if(tmp_path):
    workflow_text = """
jobs:
  deploy:
    steps:
      - uses: actions/checkout@v7
      - name: Автооткат прода
        if: failure() && steps.deploy.outcome == 'success'
        run: echo hi
      - name: Собрать
        run: echo build
  other:
    steps:
      - name: Всегда
        run: echo always
"""
    path = tmp_path / "deploy-dsh-edge.yml"
    path.write_text(workflow_text, encoding="utf-8")
    result = sfd.load_conditional_steps(path)
    assert result == {"deploy": {"Автооткат прода"}}


def test_load_conditional_steps_missing_file_returns_empty(tmp_path):
    assert sfd.load_conditional_steps(tmp_path / "nope.yml") == {}


def test_load_conditional_steps_real_deploy_workflow_names_rollback_step():
    """Живая гвардия: реальный `.github/workflows/deploy-dsh-edge.yml`
    обязан по-прежнему нести явный `if:` на шаге автооткота — иначе канал B
    (см. `collect_window`) молча перестаёт видеть finding #1 issue #1121."""
    path = sfd.REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"
    result = sfd.load_conditional_steps(path)
    assert "Автооткат прода при красной канарейке/смоуке" in result.get("deploy", set())


def test_add_step_occurrences_only_for_conditional_steps():
    # "Автооткат" — иногда skipped, иногда success: условный, попадает в группы.
    step_data = {
        ("deploy-dsh-edge.yml", "deploy", "Автооткат прода"): [
            {"conclusion": "skipped", "run_time": utc(2026, 9, 13, 1), "run_url": "u1"},
            {"conclusion": "skipped", "run_time": utc(2026, 9, 13, 2), "run_url": "u2"},
            {"conclusion": "success", "run_time": utc(2026, 9, 13, 3), "run_url": "u3"},
        ],
        # "Собрать зависимости" — успевает ВСЕГДА (не условный), не должен попасть в группы.
        ("deploy-dsh-edge.yml", "deploy", "Собрать зависимости"): [
            {"conclusion": "success", "run_time": utc(2026, 9, 13, 1), "run_url": "u1"},
            {"conclusion": "success", "run_time": utc(2026, 9, 13, 2), "run_url": "u2"},
        ],
    }
    groups: dict = {}
    sfd.add_step_occurrences(groups, step_data)
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert group["kind"] == "step"
    assert group["step"] == "Автооткат прода"
    assert group["level"] == "success"
    assert group["count"] == 1


def test_groups_over_threshold_filters_and_sorts():
    groups = {
        "a": {"count": 5, "kind": "annotation", "workflow": "w", "job": "j", "step": None,
              "sample": "x", "first_seen": NOW, "last_seen": NOW, "run_urls": [], "level": "warning"},
        "b": {"count": 2, "kind": "annotation", "workflow": "w", "job": "j", "step": None,
              "sample": "y", "first_seen": NOW, "last_seen": NOW, "run_urls": [], "level": "warning"},
        "c": {"count": 10, "kind": "annotation", "workflow": "w", "job": "j", "step": None,
              "sample": "z", "first_seen": NOW, "last_seen": NOW, "run_urls": [], "level": "warning"},
    }
    ranked = sfd.groups_over_threshold(groups, threshold=3)
    assert [g["fp"] for g in ranked] == ["c", "a"]


def test_render_digest_table_empty():
    assert "не найдены" in sfd.render_digest_table({})


def test_render_digest_table_lists_all_groups_not_only_over_threshold():
    groups = {"a": {"count": 1, "kind": "annotation", "workflow": "worker.yml", "job": "task",
                    "step": None, "sample": "редкая штука", "first_seen": NOW, "last_seen": NOW,
                    "run_urls": [], "level": "warning"}}
    table = sfd.render_digest_table(groups)
    assert "worker.yml" in table and "редкая штука" in table


def test_group_class_distinguishes_infra_from_defect():
    # Тот же приём, что #1115 просит для чеков PR — переиспользуем ЕДИНОЕ
    # место правды (pulse_guard.classify_failure_cause), не гадаем заново.
    infra_group = {"sample": "ai_dsh.sh: RATE_LIMIT: Weekly Limit quota_exhausted, retry later"}
    defect_group = {"sample": "быстрый провайдер отказал: NO_ADAPTER"}
    assert sfd.group_class(infra_group) == "инфраструктура"
    assert sfd.group_class(defect_group) == "дефект/наблюдение"


def test_group_class_classifies_installation_rate_limit_since_1177():
    # Находка PR #1136: дословный текст `gh` CLI «API rate limit exceeded
    # for installation» изначально НЕ совпадал ни с одной сигнатурой
    # INFRA_ERROR_SIGNATURES — улика для #1115. PR #1177 добавил эту
    # сигнатуру (см. pulse_guard.py, комментарий у INFRA_ERROR_SIGNATURES,
    # issue #1115) — столбец «класс» на живых прогонах orchestra.yml теперь
    # видит этот случай как инфраструктуру сам, без правки этого модуля.
    live_group = {"sample": (
        "orchestra: gh api repos/mytab0r/edge-harness/pulls?state=open"
        "&per_page=100&page=1: gh: API rate limit exceeded for installation. ...")}
    assert sfd.group_class(live_group) == "инфраструктура"


def test_render_digest_table_shows_class_column():
    groups = {"a": {"count": 5, "kind": "annotation", "workflow": "orchestra.yml", "job": "orchestra",
                    "step": None, "sample": "502 Bad Gateway от GitHub API",
                    "first_seen": NOW, "last_seen": NOW, "run_urls": [], "level": "failure"}}
    table = sfd.render_digest_table(groups)
    assert "класс" in table
    assert "инфраструктура" in table


# ── Сетевые читатели (FakeGh) ─────────────────────────────────────────────


def test_fetch_annotated_check_runs_filters_by_check_suite_id(monkeypatch):
    """Мутационный тест (см. отчёт PR): без фильтра check_suite_id вернулись
    бы ВСЕ check-runs с annotations_count>0 (три: orchestra/task/test —
    последний принадлежит ДРУГОМУ прогону) вместо одной («task»,
    принадлежащей ИМЕННО этому прогону)."""
    fake = FakeGh([("commits/f34571b3", REAL_MULTI_SUITE_CHECK_RUNS)])
    patch_gh(monkeypatch, fake)
    run = make_run(34751913956, "f34571b3-full-sha", 94121634096, "workflow_dispatch",
                    "success", "2026-09-13T10:27:52Z")
    result = sfd.fetch_annotated_check_runs(REPO, run)
    assert [c["name"] for c in result] == ["task"]


def test_fetch_annotations_returns_list(monkeypatch):
    fake = FakeGh([("check-runs/103709785923/annotations",
                     [REAL_NO_ADAPTER_ANNOTATION, REAL_NOTICE_ANNOTATION])])
    patch_gh(monkeypatch, fake)
    annotations = sfd.fetch_annotations(REPO, {"id": 103709785923})
    assert len(annotations) == 2


def test_fetch_annotations_non_list_response_fails_loud(monkeypatch):
    fake = FakeGh([("check-runs/1/annotations", {"message": "not found"})])
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        sfd.fetch_annotations(REPO, {"id": 1})


def test_fetch_runs_full_page_fails_loud(monkeypatch):
    fake = FakeGh([("worker.yml/runs", {"workflow_runs": [{"event": "workflow_dispatch"}] * 100})])
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError):
        sfd.fetch_runs(REPO, "worker.yml", NOW)


def test_collect_window_end_to_end_finds_no_adapter_and_conditional_rollback(monkeypatch):
    """Интеграционный прогон обоих каналов на реалистичных фикстурах:
    3 прогона worker.yml несут одну и ту же аннотацию NO_ADAPTER (канал A),
    4 прогона deploy-dsh-edge.yml несут условный шаг «Автооткат» (канал B,
    3x skipped + 1x success — тот же паттерн, что REAL_ROLLBACK_STEP_HISTORY)."""
    worker_runs = {
        "workflow_runs": [
            make_run(100 + i, f"sha{i}", 900 + i, "workflow_dispatch", "success",
                      f"2026-09-13T0{i}:00:00Z")
            for i in range(3)
        ]
    }
    deploy_runs = {
        "workflow_runs": [
            make_run(200 + i, f"dsha{i}", 800 + i, "workflow_dispatch",
                      "success" if i < 3 else "failure", f"2026-09-13T1{i}:00:00Z")
            for i in range(4)
        ]
    }

    def check_runs_for(job_name: str, suite_id: int, ann_count: int) -> dict:
        return {"check_runs": [{"id": 1, "name": job_name, "check_suite": {"id": suite_id},
                                 "output": {"annotations_count": ann_count}}]}

    def jobs_for(step_conclusion: str) -> dict:
        return {"jobs": [{"name": "deploy", "steps": [
            {"name": "Автооткат прода", "conclusion": step_conclusion},
            {"name": "Собрать", "conclusion": "success"},
        ]}]}

    routes = [
        ("workflows/worker.yml/runs", worker_runs),
        ("workflows/deploy-dsh-edge.yml/runs", deploy_runs),
        ("workflows/hands.yml/runs", {"workflow_runs": []}),
        ("workflows/ai-review.yml/runs", {"workflow_runs": []}),
        ("workflows/orchestra.yml/runs", {"workflow_runs": []}),
    ]
    for i in range(3):
        routes.append((f"commits/sha{i}/check-runs", check_runs_for("task", 900 + i, 1)))
        routes.append((f"actions/runs/{100 + i}/jobs", {"jobs": []}))
    for i in range(4):
        routes.append((f"commits/dsha{i}/check-runs", check_runs_for("deploy", 800 + i, 0)))
        conclusion = "skipped" if i < 3 else "success"
        routes.append((f"actions/runs/{200 + i}/jobs", jobs_for(conclusion)))
    routes.append(("check-runs/1/annotations", [REAL_NO_ADAPTER_ANNOTATION]))

    # load_workflow_layout читает РЕАЛЬНЫЙ файл workflow с диска (0 сетевых
    # вызовов) — в тесте изолируем от содержимого настоящих .github/
    # workflows/*.yml (иначе тест ломается при любой правке реальных
    # workflow-файлов, не относящейся к этому коду): подставляем фиксированный
    # набор условных шагов для тестового workflow.
    def fake_layout(path):
        if "deploy-dsh-edge" in str(path):
            return {"deploy": {"display": "deploy", "conditional": {"Автооткат прода"}}}
        return {}

    monkeypatch.setattr(sfd, "load_workflow_layout", fake_layout)

    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)

    groups, observations, stats = sfd.collect_window(REPO, NOW)
    assert observations == []
    assert stats == {"workflows_total": 5, "workflows_ok": 5,
                     "workflows_read": list(sfd.DIGEST_WORKFLOWS),
                     "partial_reads_failed": {}}
    # Канал A (аннотации): 3 прогона worker.yml несут одну и ту же аннотацию
    # NO_ADAPTER — над порогом 3, попадает в эскалацию.
    ranked = sfd.groups_over_threshold(groups, threshold=3)
    kinds = {(g["workflow"], g["kind"], g.get("step")) for g in ranked}
    assert ("worker.yml", "annotation", None) in kinds
    # Канал B (условные шаги): «Автооткат прода» виден в ГРУППАХ (найден
    # каналом B — skipped+success в одних данных), но с count=1 честно НЕ
    # пересекает порог 3 в этой малой фикстуре — реальный порог проверяется
    # на живом 24-часовом окне (см. отчёт PR), здесь важен сам факт
    # обнаружения канала B, не конкретное число.
    step_group = next(g for g in groups.values() if g["kind"] == "step")
    assert step_group["workflow"] == "deploy-dsh-edge.yml"
    assert step_group["step"] == "Автооткат прода"
    assert step_group["count"] == 1


def test_digest_due_true_when_no_marker(monkeypatch):
    # all_issue_comments(max_pages=3) читает метаданные issue #120 ПЕРЕД
    # страницами (см. pulse_guard.all_issue_comments) — единственный маршрут
    # нужен здесь: comments=0 возвращает [] без похода за страницами.
    fake = FakeGh([("repos/mytab0r/edge-harness/issues/120", {"comments": 0})])
    patch_gh(monkeypatch, fake)
    assert sfd.digest_due(REPO, NOW) is True


def test_digest_due_false_when_recent_marker(monkeypatch):
    # Порядок routes важен (FakeGh матчит первый совпавший фрагмент):
    # фрагмент страницы комментариев — ПЕРВЫМ (иначе более короткий
    # "issues/120" совпал бы с ним тоже, как подстрока).
    fake = FakeGh([
        ("issues/120/comments?per_page=100&page=1",
         [{"created_at": "2026-09-13T11:00:00Z",
           "body": f"{sfd.DIGEST_HEARTBEAT_MARKER} 2026-09-13T11:00:00+00:00]"}]),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 1}),
    ])
    patch_gh(monkeypatch, fake)
    assert sfd.digest_due(REPO, NOW, interval_hours=4.0) is False


def test_digest_due_true_when_marker_older_than_interval(monkeypatch):
    fake = FakeGh([
        ("issues/120/comments?per_page=100&page=1",
         [{"created_at": "2026-09-13T06:00:00Z",
           "body": f"{sfd.DIGEST_HEARTBEAT_MARKER} 2026-09-13T06:00:00+00:00]"}]),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 1}),
    ])
    patch_gh(monkeypatch, fake)
    assert sfd.digest_due(REPO, NOW, interval_hours=4.0) is True


def test_tracked_fingerprints_reads_marker_from_open_issue_body():
    issues = [
        {"body": f"текст\n{sfd.SOFT_FAILURE_FINGERPRINT_MARKER}abc123 -->\n"},
        {"body": "issue без отпечатка"},
    ]
    assert sfd.tracked_fingerprints(issues) == {"abc123"}


def test_escalate_groups_skips_already_tracked_fingerprint(monkeypatch):
    group = {"fp": "abc123", "workflow": "worker.yml", "kind": "annotation", "job": "task",
              "step": None, "count": 5, "sample": "x", "first_seen": NOW, "last_seen": NOW,
              "run_urls": [], "level": "warning"}
    fake = FakeGh([
        ("issues?state=open&labels=soft-failure",
         [{"body": f"{sfd.SOFT_FAILURE_FINGERPRINT_MARKER}abc123 -->", "number": 5}]),
    ])
    patch_gh(monkeypatch, fake)
    observations, actions = sfd.escalate_groups(REPO, [group], NOW)
    assert actions == []
    assert any("уже в пуле" in o for o in observations)


def test_escalate_groups_creates_task_for_new_fingerprint(monkeypatch):
    group = {"fp": "newfp01", "workflow": "worker.yml", "kind": "annotation", "job": "task",
              "step": None, "count": 5, "sample": "NO_ADAPTER что-то", "first_seen": NOW,
              "last_seen": NOW, "run_urls": ["https://example/1"], "level": "warning"}
    fake = FakeGh([
        ("issues?state=open&labels=soft-failure", []),
        ("issues?state=all&labels=soft-failure", []),
        ("-X POST repos/mytab0r/edge-harness/issues", {"number": 4242}),
    ])
    patch_gh(monkeypatch, fake)
    observations, actions = sfd.escalate_groups(REPO, [group], NOW)
    assert any("#4242" in a for a in actions)
    # labels обязаны нести task (create_pool_issue отказывает без неё ДО сети) —
    # если бы вызов ушёл без task, FakeGh поймал бы AssertionError на маршруте.
    post_call = next(c for c in fake.calls if "-X POST" in c and "/issues" in c)
    assert "labels[]=task" in post_call
    assert "labels[]=soft-failure" in post_call


def test_escalate_groups_cap_exhausted_skips_and_escalates_once(monkeypatch):
    groups = [
        {"fp": f"fp{i}", "workflow": "worker.yml", "kind": "annotation", "job": "task",
         "step": None, "count": 5, "sample": "x", "first_seen": NOW, "last_seen": NOW,
         "run_urls": [], "level": "warning"}
        for i in range(1)
    ]
    fake = FakeGh([
        ("-X POST repos/mytab0r/edge-harness/issues/120/comments", {"id": 1}),
        ("issues?state=open&labels=soft-failure", []),
        ("issues?state=all&labels=soft-failure",
         [{"created_at": "2026-09-13T01:00:00Z", "number": n} for n in range(sfd.SOFT_FAILURE_DAILY_CAP)]),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 0}),
    ])
    patch_gh(monkeypatch, fake)
    observations, actions = sfd.escalate_groups(REPO, groups, NOW)
    assert actions == []
    assert any("отсечён потолком" in o for o in observations)
    assert any("-X POST" in c and "issues/120/comments" in c for c in fake.calls)


# ── Частичный провал внутри "прочитанного" workflow (находка ревью PR
# #1136, пятый круг, блокер 1) ────────────────────────────────────────────


def test_collect_window_counts_partial_reads_failed_per_workflow(monkeypatch):
    """workflow целиком прочитан (список прогонов есть), но check-runs ОДНОГО
    из прогонов падает — это partial, не workflows_ok=0 и не полностью
    прочитанное окно молча."""
    routes = [
        ("workflows/worker.yml/runs", {"workflow_runs": [
            {"id": 100, "updated_at": "2026-09-13T10:00:00Z", "head_sha": "sha0", "html_url": "u0"},
        ]}),
        ("workflows/hands.yml/runs", {"workflow_runs": []}),
        ("workflows/ai-review.yml/runs", {"workflow_runs": []}),
        ("workflows/orchestra.yml/runs", {"workflow_runs": []}),
        ("workflows/deploy-dsh-edge.yml/runs", {"workflow_runs": []}),
        ("commits/sha0/check-runs", RuntimeError("rate limit")),
    ]
    monkeypatch.setattr(sfd, "load_workflow_layout", lambda path: {})
    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)

    groups, observations, stats = sfd.collect_window(REPO, NOW)
    assert stats["workflows_ok"] == 5
    assert "worker.yml" in stats["workflows_read"]
    assert stats["partial_reads_failed"] == {"worker.yml": 1}
    assert any("check-runs" in o and "не прочитаны" in o for o in observations)


def test_soft_failure_digest_heartbeat_names_partial_failures(monkeypatch):
    """Находка ревью PR #1136 (пятый круг, блокер 1): partial_reads_failed
    обязан попасть в heartbeat (единственное гарантированно читаемое место),
    не только в наблюдения step summary."""
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", lambda: {"remaining": 950, "limit": 1000})
    monkeypatch.setattr(sfd, "load_workflow_layout", lambda path: {})
    posted: list[str] = []

    def fake_post(repo, issue, body):
        posted.append(body)
        return {"id": 1}

    routes = [
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 0}),
        ("workflows/worker.yml/runs", {"workflow_runs": [
            {"id": 100, "updated_at": "2026-09-13T10:00:00Z", "head_sha": "sha0", "html_url": "u0"},
        ]}),
        ("workflows/hands.yml/runs", {"workflow_runs": []}),
        ("workflows/ai-review.yml/runs", {"workflow_runs": []}),
        ("workflows/orchestra.yml/runs", {"workflow_runs": []}),
        ("workflows/deploy-dsh-edge.yml/runs", {"workflow_runs": []}),
        ("commits/sha0/check-runs", RuntimeError("rate limit")),
    ]
    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sfd, "post_issue_comment", fake_post)

    observations, actions, ok = sfd.soft_failure_digest(REPO, NOW)
    assert ok is True
    assert len(posted) == 1
    assert "worker.yml" in posted[0] and "частично" in posted[0]


# ── Дайджест давно не сканировал успешно (находка ревью PR #1136, пятый
# круг, блокер 2) ──────────────────────────────────────────────────────────


def test_escalate_stale_scan_silent_when_no_history(monkeypatch):
    """Ни одного heartbeat'а в истории вовсе — либо только что заведён, либо
    очень долгая деградация вне окна max_pages=3; неотличимо этим чтением —
    не гадаем, молчим (см. докстринг escalate_stale_scan)."""
    fake = FakeGh([("repos/mytab0r/edge-harness/issues/120", {"comments": 0})])
    patch_gh(monkeypatch, fake)
    observations = sfd.escalate_stale_scan(REPO, NOW)
    assert observations == []
    assert not any("-X POST" in c for c in fake.calls)


def test_escalate_stale_scan_no_escalation_when_marker_fresh(monkeypatch):
    fake = FakeGh([
        ("issues/120/comments?per_page=100&page=1",
         [{"created_at": "2026-09-13T11:00:00Z",
           "body": f"{sfd.DIGEST_HEARTBEAT_MARKER} 2026-09-13T11:00:00+00:00]"}]),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 1}),
    ])
    patch_gh(monkeypatch, fake)
    observations = sfd.escalate_stale_scan(REPO, NOW)
    assert observations == []
    assert not any("-X POST" in c for c in fake.calls)


def test_escalate_stale_scan_escalates_once_past_threshold(monkeypatch):
    stale_time = (NOW - timedelta(hours=sfd.STALE_SCAN_THRESHOLD_HOURS + 1)).isoformat()
    fake = FakeGh([
        (f"comments?per_page=100&page=1",
         [{"created_at": stale_time,
           "body": f"{sfd.DIGEST_HEARTBEAT_MARKER} {stale_time}]"}]),
        ("-X POST repos/mytab0r/edge-harness/issues/120/comments", {"id": 1}),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 1}),
    ])
    patch_gh(monkeypatch, fake)
    observations = sfd.escalate_stale_scan(REPO, NOW)
    assert any("эскалировано" in o for o in observations)
    assert any("-X POST" in c and "issues/120/comments" in c for c in fake.calls)


def test_escalate_stale_scan_does_not_double_escalate_same_day(monkeypatch):
    stale_time = (NOW - timedelta(hours=sfd.STALE_SCAN_THRESHOLD_HOURS + 1)).isoformat()
    own_marker = f"{sfd.STALE_SCAN_MARKER} {NOW.date().isoformat()}]"
    fake = FakeGh([
        (f"comments?per_page=100&page=1",
         [{"created_at": stale_time,
           "body": f"{sfd.DIGEST_HEARTBEAT_MARKER} {stale_time}]"},
          {"created_at": NOW.isoformat(), "body": own_marker}]),
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 2}),
    ])
    patch_gh(monkeypatch, fake)
    observations = sfd.escalate_stale_scan(REPO, NOW)
    assert observations == []
    assert not any("-X POST" in c for c in fake.calls)


# ── Квота (находка ревью PR #1136, блокер 2) ─────────────────────────────


def test_quota_sufficient_true_above_threshold(monkeypatch):
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", lambda: {"remaining": 950, "limit": 1000})
    ok, text = sfd.quota_sufficient()
    assert ok is True
    assert "950/1000" in text


def test_quota_sufficient_false_below_threshold(monkeypatch):
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", lambda: {"remaining": 500, "limit": 1000})
    ok, text = sfd.quota_sufficient()
    assert ok is False
    assert "500/1000" in text


def test_quota_sufficient_false_when_read_fails(monkeypatch):
    def boom():
        raise sfd.rate_guard.QuotaCheckFailed("сеть легла")
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", boom)
    ok, text = sfd.quota_sufficient()
    assert ok is False
    assert "не прочитана" in text


# ── Полный провал скана ≠ «группы не найдены» (находка ревью PR #1136, блокер 3) ──


def test_soft_failure_digest_skips_when_quota_low_without_touching_workflows(monkeypatch):
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", lambda: {"remaining": 100, "limit": 1000})
    fake = FakeGh([("repos/mytab0r/edge-harness/issues/120", {"comments": 0})])
    patch_gh(monkeypatch, fake)
    observations, actions, ok = sfd.soft_failure_digest(REPO, NOW)
    assert ok is True  # квоты мало — не провал, штатный перенос
    assert actions == []
    assert any("квота" in o.lower() for o in observations)
    assert not any("actions/workflows" in c for c in fake.calls)  # до collect_window не дошли


def test_soft_failure_digest_total_read_failure_is_not_empty_groups(monkeypatch):
    monkeypatch.setattr(sfd.rate_guard, "fetch_core", lambda: {"remaining": 950, "limit": 1000})
    fake = FakeGh([
        ("repos/mytab0r/edge-harness/issues/120", {"comments": 0}),
        ("actions/workflows", RuntimeError("сеть легла")),  # матчит ЛЮБОЙ .../runs? — все 5 workflow
    ])
    patch_gh(monkeypatch, fake)
    observations, actions, ok = sfd.soft_failure_digest(REPO, NOW)
    assert ok is False
    assert actions == []
    assert any("скан НЕ удался" in o for o in observations)
    # Не подменяем провал ложным «пусто» — render_digest_table на пустой
    # таблице печатает именно эту строку, её не должно быть в выводе провала.
    assert not any(o == "Группы не найдены (окно пусто или без аннотаций/условных шагов)."
                   for o in observations)
    assert not any("-X POST" in c and "issues/120/comments" in c for c in fake.calls)  # heartbeat не писан


# ── Чеклист ревью PR #1136: сопоставление job'ов, пропуск пустого канала B ──


def test_load_workflow_layout_carries_display_and_conditional(tmp_path):
    workflow_text = """
jobs:
  build:
    name: Собрать образ
    steps:
      - name: Верно
        if: failure()
        run: echo hi
  plain:
    steps:
      - run: echo always
"""
    path = tmp_path / "w.yml"
    path.write_text(workflow_text, encoding="utf-8")
    layout = sfd.load_workflow_layout(path)
    assert layout["build"]["display"] == "Собрать образ"
    assert layout["build"]["conditional"] == {"Верно"}
    assert layout["plain"]["display"] == "plain"
    assert layout["plain"]["conditional"] == set()


def test_match_job_key_exact_display_and_matrix_form():
    layout = {"build": {"display": "Собрать образ", "conditional": {"x"}},
              "deploy": {"display": "deploy", "conditional": {"y"}}}
    assert sfd.match_job_key(layout, "deploy") == "deploy"
    # Матрица: Jobs API показывает «<display> (<значения>)».
    assert sfd.match_job_key(layout, "Собрать образ (ubuntu-latest, 3.12)") == "build"
    assert sfd.match_job_key(layout, "deploy ( Arms )") == "deploy"
    assert sfd.match_job_key(layout, "никто не знает") is None
    # «Собрать» не совпадает с «Собрать образ» без матричной скобки.
    assert sfd.match_job_key(layout, "Собрать") is None


def test_collect_window_unmatched_job_is_loud_not_silent(monkeypatch):
    """Чеклист ревью PR #1136: job из Jobs API, не сопоставленный ни с одним
    job'ом исходника (переименовали в YAML, добавили `name:`), раньше
    пропускался молча — канал B просто «не находил» шаги. Теперь это громкое
    наблюдение (одно на workflow за скан), не тишина."""
    runs = {"workflow_runs": [make_run(300, "usha", 700, "workflow_dispatch", "success",
                                       "2026-09-13T05:00:00Z")]}
    routes = [
        ("workflows/worker.yml/runs", runs),
        ("commits/usha/check-runs", {"check_runs": []}),
        ("actions/runs/300/jobs", {"jobs": [{"name": "Переименованный job", "steps": [
            {"name": "Условный шаг", "conclusion": "success"}]}]}),
    ] + [
        (f"workflows/{w}/runs", {"workflow_runs": []})
        for w in ("hands.yml", "ai-review.yml", "orchestra.yml", "deploy-dsh-edge.yml")
    ]
    monkeypatch.setattr(sfd, "load_workflow_layout",
                        lambda path: ({"old": {"display": "old", "conditional": {"Условный шаг"}}}
                                      if "worker" in str(path) else {}))
    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)

    groups, observations, stats = sfd.collect_window(REPO, NOW)
    assert stats["workflows_ok"] == 5
    assert groups == {}
    warnings = [line for line in observations if "не сопоставлен" in line]
    assert len(warnings) == 1 and "Переименованный job" in warnings[0]


def test_collect_window_skips_fetch_jobs_when_no_conditional_steps(monkeypatch):
    """Чеклист ревью PR #1136: workflow без единого `if:`-шага (worker.yml,
    hands.yml) не должен тратить вызов jobs на КАЖДЫЙ прогон с выброшенным
    результатом — канал B для него отключён, канал A работает."""
    runs = {"workflow_runs": [
        make_run(400 + i, f"wsha{i}", 600 + i, "workflow_dispatch", "success",
                 f"2026-09-13T0{i}:30:00Z")
        for i in range(2)
    ]}
    routes = [
        ("workflows/worker.yml/runs", runs),
        ("commits/wsha0/check-runs", {"check_runs": []}),
        ("commits/wsha1/check-runs", {"check_runs": []}),
        ("actions/runs/400/jobs", {"jobs": [{"name": "task", "steps": []}]}),
        ("actions/runs/401/jobs", {"jobs": [{"name": "task", "steps": []}]}),
    ] + [
        (f"workflows/{w}/runs", {"workflow_runs": []})
        for w in ("hands.yml", "ai-review.yml", "orchestra.yml", "deploy-dsh-edge.yml")
    ]
    monkeypatch.setattr(sfd, "load_workflow_layout", lambda path: {})
    fake = FakeGh(routes)
    patch_gh(monkeypatch, fake)

    groups, observations, stats = sfd.collect_window(REPO, NOW)
    assert stats["workflows_ok"] == 5
    jobs_calls = [c for c in fake.calls if "/jobs" in c]
    assert jobs_calls == [], "для workflow без условных шагов fetch_jobs не зовётся"


def test_mark_heartbeat_lists_unread_workflows(monkeypatch):
    """Чеклист ревью PR #1136: при частичном провале непрочитанные workflow
    названы в САМОМ heartbeat'е, а не только ⚠️-строкой в step summary, —
    heartbeat читает гейт цикла гарантированно, step summary — никто."""
    posted: list[str] = []

    def fake_post(repo, issue, body):
        posted.append(body)
        return {"id": 1}

    monkeypatch.setattr(sfd, "post_issue_comment", fake_post)
    sfd.mark_heartbeat(REPO, NOW, unread=["worker.yml"])
    assert len(posted) == 1
    assert "Непрочитаны" in posted[0] and "worker.yml" in posted[0]
    sfd.mark_heartbeat(REPO, NOW)
    assert "Непрочитаны" not in posted[1]
