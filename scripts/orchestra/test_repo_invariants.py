#!/usr/bin/env python3
"""Тесты инвариантов состояния репозитория (scripts/orchestra/repo_invariants.py, #244).

Кормятся прод-формой: тела issue/PR ниже — реальный текст, снятый живым
`gh api` по этому репозиторию 2026-09-03 (см. PR #244) — не пересказ. Пять
инвариантов, каждый доказан мутацией (снять фикс — тест краснеет), плюс
гвардия холостого хода: на здоровом снимке ни один инвариант не срабатывает
и build_report не делает ни одного мутирующего вызова.

Запуск: python -m pytest scripts/orchestra/test_repo_invariants.py -q
"""

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "repo_invariants.py"
spec = importlib.util.spec_from_file_location("repo_invariants", SCRIPT)
ri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ri)  # type: ignore[union-attr]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def task_issue(number, title="", issue_body="", assignees=()):
    # Параметр НЕ называется body= — та же гвардия класса #124
    # (scripts/orchestra/, scripts/lib/, scripts/review/: grep ',\s*body='
    # ловит keyword-вызов gh()) текстово матчит и сигнатуру функции с
    # дефолтом body="" — ложное срабатывание, не связанное с gh() вовсе
    # (тот же приём уже применён в test_scheduler.py::pull → pr_body).
    return {
        "number": number,
        "title": title,
        "body": issue_body,
        "assignees": [{"login": a} for a in assignees],
        "labels": [{"name": "task"}],
    }


def merged_pr(number, ref, merged_at):
    return {"number": number, "head": {"ref": ref}, "merged_at": merged_at}


def open_pr(number, pr_body="", labels=()):
    return {"number": number, "body": pr_body, "labels": [{"name": n} for n in labels],
            "head": {"sha": f"sha{number}"}}


def health_snapshot_contents_response(rows: list[dict]) -> dict:
    """Прод-форма ответа `GET /repos/{repo}/contents/{path}` (GitHub Contents
    API — base64 в поле `content`), какую реально отдаёт эндпоинт, что читает
    `repo_invariants.fetch_pipeline_health_history` (инвариант 12, #882)."""
    import base64
    import json
    text = "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n"
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def worktree_snapshot_contents_response(rows: list[dict]) -> dict:
    """Та же прод-форма Contents API для журнала уборки рабочих деревьев
    (инвариант 24, #1250) — читает `fetch_worktree_cleanup_records`; форма
    ответа одинаковая, отдельная фикстура только чтобы докстринг теста называл
    свой канал, а не соседний."""
    return health_snapshot_contents_response(rows)


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 1: задача открыта без исполнителя, PR уже слит
# ══════════════════════════════════════════════════════════════════════════


REPO = "mytab0r/edge-harness"


def test_reopened_after_merge_flags_free_task_with_merged_pr(monkeypatch):
    # #394: задача PR резолвится ТОЛЬКО по имени ветки, тело не читается —
    # оба PR названы agent/18-*, тела нет вовсе (штатный PR может быть слит
    # без единого номера в теле).
    fake = FakeGh({"issues/18/comments": []})  # приёмка ещё не выносила вердикт
    patch_gh(monkeypatch, fake)
    tasks = [task_issue(18, "AI-ревьюер диффа", assignees=())]
    pulls = [merged_pr(137, "agent/18-first-pass", "2026-08-31T17:46:11Z"),
             merged_pr(138, "agent/18-second-pass", "2026-09-02T21:31:47Z")]
    violations = ri.check_reopened_after_merge(REPO, tasks, pulls)
    assert len(violations) == 1
    assert violations[0]["issue"] == 18
    assert violations[0]["prs"] == [137, 138]
    assert violations[0]["merged_at"] == "2026-09-02T21:31:47Z"  # самый свежий


def test_reopened_after_merge_silent_when_assigned():
    # тот же слитый PR, но задача СЕЙЧАС занята исполнителем — норма
    # (пост-мерж проверка ещё не сделана, это не бросили). Без FakeGh-маршрута
    # нарочно: маркер приёмки не должен даже спрашиваться (short-circuit по
    # unassigned раньше).
    tasks = [task_issue(18, assignees=("mytab0r",))]
    pulls = [merged_pr(137, "agent/18-first-pass", "2026-08-31T17:46:11Z")]
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []


def test_reopened_after_merge_silent_without_merged_pr():
    tasks = [task_issue(18, assignees=())]
    pulls = [merged_pr(999, "agent/77-other-task", "2026-08-31T17:46:11Z")]  # чужая ветка
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []


def test_reopened_after_merge_mutation_guard(monkeypatch):
    # Мутация: если бы проверка не сверялась с unassigned (снят фильтр по
    # исполнителю), КАЖДАЯ задача со слитым PR стала бы «нарушением» — на
    # живом репозитории это стандартный кратковременный путь после мержа,
    # а не баг. Тест доказывает, что фильтр обязателен.
    tasks = [task_issue(18, assignees=("mytab0r",))]
    pulls = [merged_pr(137, "agent/18-first-pass", "2026-08-31T17:46:11Z")]
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []
    fake = FakeGh({"issues/18/comments": []})
    patch_gh(monkeypatch, fake)
    tasks_unassigned = [task_issue(18, assignees=())]
    assert len(ri.check_reopened_after_merge(REPO, tasks_unassigned, pulls)) == 1


def test_reopened_after_merge_excludes_watchdog_issue():
    """#467: WATCHDOG_ISSUE (#120) — постоянный канал эскалации, не задача
    из пула; accept_merged_tasks её тоже явно пропускает (см. её докстринг) —
    эта проверка обязана делать то же самое, а не находить #120 в списке
    нарушителей своего же канала (живой случай)."""
    watchdog = ri.WATCHDOG_ISSUE
    tasks = [task_issue(watchdog, "Предохранитель конвейера", assignees=())]
    pulls = [merged_pr(126, f"agent/{watchdog}-pause", "2026-08-31T12:01:11Z")]
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []


def test_reopened_after_merge_excludes_task_already_verdicted_partial(monkeypatch):
    """#467, живой случай PR #455/задача #454: приёмка уже вынесла терминальный
    вердикт «требует проверки человеком» именно по этому слитому PR и сама
    сняла исполнителя как часть штатного пути — не тихий пробел, инвариант 1
    не должен пересчитывать эту задачу нарушителем снова и снова."""
    fake = FakeGh({
        "issues/454/comments": [
            {"body": f"{ri.scheduler.ACCEPTANCE_PARTIAL_MARKER} PR #455 …",
             "created_at": "2026-09-06T05:45:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    tasks = [task_issue(454, "ранний отказ по квоте", assignees=())]
    pulls = [merged_pr(455, "agent/454-gh-quota-early-exit", "2026-09-06T05:43:14Z")]
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []


def test_reopened_after_merge_excludes_task_already_verdicted_fail(monkeypatch):
    """Тот же класс, второй терминальный маркер (#467, живой случай PR #437/
    задача #432): «доработка» — штатный путь назад в пул для НОВОГО PR, не
    нарушение инварианта 1."""
    fake = FakeGh({
        "issues/432/comments": [
            {"body": f"{ri.scheduler.ACCEPTANCE_FAIL_MARKER} PR #437 — улика показала…",
             "created_at": "2026-09-06T04:08:27Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    tasks = [task_issue(432, "review:large тоже гейт 1", assignees=())]
    pulls = [merged_pr(437, "agent/432-gate1-decided", "2026-09-06T04:05:48Z")]
    assert ri.check_reopened_after_merge(REPO, tasks, pulls) == []


def test_reopened_after_merge_still_flags_task_never_verdicted(monkeypatch):
    """Контроль: пустые комментарии (приёмка ещё не смотрела на эту пару
    задача/PR) — инвариант 1 обязан сработать как раньше, различение не
    глотает настоящий, ещё никем не замеченный пробел."""
    fake = FakeGh({"issues/21/comments": []})
    patch_gh(monkeypatch, fake)
    tasks = [task_issue(21, "dev:docker", assignees=())]
    pulls = [merged_pr(177, "agent/21-dev-docker", "2026-09-02T17:01:28Z")]
    assert len(ri.check_reopened_after_merge(REPO, tasks, pulls)) == 1


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 2 — выведен из состава (см. блок-комментарий в
# repo_invariants.py на месте бывшего check_free_task_count_mismatch):
# #247 закрыл класс substring-scan, который он ловил, сравнивать стало
# не с чем. Тестов для отсутствующей функции нет.
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 3: review:ok без ai:* дольше порога
# ══════════════════════════════════════════════════════════════════════════


class FakeGh:
    """Тот же маршрутизатор, что test_scheduler.py::FakeGh — подстрока пути
    → прод-форма ответа; фиксирует вызовы для гвардии холостого хода.

    Запасной маршрут для занятости ai-review.yml (review_labels.other_active_
    ai_review_runs, #779 блокирующая 3: retry_budget_fact теперь спрашивает
    её же для held_back_by_run) — тот же приём, что test_scheduler.py::
    FakeGh._DEFAULT_ROUTES: без него КАЖДЫЙ существующий тест
    check_stuck_review_gate/check_ai_failed_budget_exhausted был бы обязан
    завести собственную строку «нет активных прогонов», хотя сама занятость
    ai-review.yml не их предмет.

    Запасной маршрут для истории снимков здоровья конвейера (инвариант 12,
    #882): реалистичный дефолт — 404 (ветки data/pipeline-health на живом
    репозитории на момент этого фикса ещё не существует вовсе, ровно предмет
    инцидента). Тест, которому нужен ЗДОРОВЫЙ снимок, переопределяет этот
    маршрут явно (см. test_pipeline_health_snapshot_stale_* ниже) — без этого
    дефолта КАЖДЫЙ существующий тест build_report был бы обязан завести
    собственный маршрут contents/pipeline-health.jsonl, хотя история снимков
    не их предмет.

    Запасной маршрут для журнала уборки рабочих деревьев (инвариант 24,
    #1250) — тот же приём и та же причина: 404 (data/worktree-cleanup пуста
    до первого прод-прогона уборщика после слияния). Тест, которому нужны
    записи, переопределяет маршрут явно (test_worktree_cleanup_records_*)."""
    _DEFAULT_ROUTES = {
        "actions/workflows/ai-review.yml/runs": {"workflow_runs": []},
        "contents/docs/research/data/pipeline-health.jsonl": RuntimeError(
            "gh api repos/o/r/contents/...: HTTP 404: Not Found"),
        "contents/docs/research/data/worktree-cleanup.jsonl": RuntimeError(
            "gh api repos/o/r/contents/...: HTTP 404: Not Found"),
        "issues/120/comments": [],
        # Запасной маршрут для инварианта 17 (морда, #1041): здоровый
        # дефолт — последний прогон deploy-dsh-edge.yml зелёный и уже стоит на
        # main_sha "deadbeef", совпадающем с commits/main — decide_frontend_
        # deploy_stale молчит без единого доп. вызова compare/. Без этого
        # дефолта КАЖДЫЙ существующий тест build_report был бы обязан завести
        # собственные маршруты workflows/deploy-dsh-edge.yml/runs и
        # commits/main, хотя свежесть морды — не их предмет.
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            {"conclusion": "success", "head_sha": "deadbeef",
             "created_at": "2026-09-01T00:00:00Z", "html_url": "https://example/runs/1"},
        ]},
        # Тот же здоровый дефолт для ВТОРОГО деплоя морды (#1419, обобщение
        # #1041): инвариант 17 теперь опрашивает оба workflow, и тест, которому
        # deploy-worker.yml безразличен, не обязан знать о его существовании.
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            {"conclusion": "success", "head_sha": "deadbeef",
             "created_at": "2026-09-01T00:00:00Z", "html_url": "https://example/runs/2"},
        ]},
        "commits/main": {"sha": "deadbeef"},
    }

    def __init__(self, routes):
        # Пользовательские маршруты перебираются ПЕРВЫМИ: дефолт — «запасной
        # маршрут» по докстрингу класса, а не приоритетный. Прежний порядок
        # ({**defaults, **routes}) работал, пока дефолтные фрагменты не
        # пересекались ПОДСТРОКОЙ с пользовательскими: одинаковый ключ
        # переопределялся значением routes, но частичное пересечение
        # («workflows/deploy-worker.yml/runs» дефолта против
        # «…runs?head_sha=…» пользователя) отдавало ответ дефолта, минуя
        # подставленный (обобщение #1419 добавило второй дефолтный
        # deploy-маршрут и вскрыло это на тестах merge-reactions).
        self.routes = dict(routes)
        for fragment, result in self._DEFAULT_ROUTES.items():
            self.routes.setdefault(fragment, result)
        self.calls = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE"))]


def patch_gh(monkeypatch, fake):
    """Единая точка патча — ТРИ модуля читают gh() по имени (repo_invariants
    реэкспортирует pulse_guard.gh как свой атрибут `ri.gh`, но
    post_issue_comment/escalate внутри pulse_guard.py вызывают СВОЙ
    module-level `gh`, а не `ri.gh`; ai_changes_labeled_at внутри
    scheduler.py — третий, самостоятельный биндинг `scheduler.gh`).
    Патчить только `ri.gh`/`ri.pulse_guard.gh` недостаточно — так один
    прогон реально ушёл в живой issue #120 (инцидент этой задачи, #244:
    очищено вручную, gh api -X DELETE .../comments/5527288512), а находка
    ревью PR #1260 поймала тот же класс на `scheduler.gh`: тесты
    check_ai_rework_never_dispatched читали живой GitHub вместо фикстур.
    Патчим все три имени — тот же приём, что test_scheduler.py::patch_gh."""
    monkeypatch.setattr(ri, "gh", fake)
    monkeypatch.setattr(ri.pulse_guard, "gh", fake)
    monkeypatch.setattr(ri.scheduler, "gh", fake)


def gate1_status(when: str):
    """Прод-форма commit status `harness/review` на текущем head PR (#345) —
    якорь после находки ревью #424 (замена таймлайн-события 'labeled',
    замороженного идемпотентностью #203)."""
    return [{"context": ri.review_labels.STATUS_REVIEW, "created_at": when}]


def timeline_with_review_ok(when: str):
    return [{"event": "labeled", "label": {"name": "review:ok"}, "created_at": when}]


def timeline_with_review_large(when: str):
    """Прод-форма таймлайна крупного PR (#432): verdict_for ставит РОВНО одну
    из двух меток гейта 1 — "labeled: review:ok" в таком таймлайне не
    наступает никогда."""
    return [{"event": "labeled", "label": {"name": "review:large"}, "created_at": when}]


def retry_marker_comment(when: str, attempt: int):
    return {
        "created_at": when,
        "body": f"🤖 {ri.AI_REVIEW_RETRY_MARKER} попытка {attempt}/{ri.AI_REVIEW_MAX_ATTEMPTS}",
    }


def test_stuck_review_gate_flags_after_threshold(monkeypatch):
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({
        "commits/sha246/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/246/timeline": timeline_with_review_ok("2026-09-01T10:00:00Z"),
        "issues/246/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)  # заведомо больше порога 120 мин
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 246
    assert violations[0]["attempts_total"] == 0
    assert violations[0]["verdict_ever"] is None


def test_stuck_review_gate_silent_within_threshold(monkeypatch):
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({"commits/sha246/statuses": gate1_status("2026-09-03T14:07:04Z")})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 13)  # 6 минут — живой случай PR #246 на 2026-09-03
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []


def test_stuck_review_gate_silent_when_verdict_present(monkeypatch):
    pull = open_pr(163, labels=["review:ok", "ai:changes-requested"])
    fake = FakeGh({})  # статус даже не должен запрашиваться
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []
    assert fake.calls == []


def test_stuck_review_gate_flags_review_large_without_any_ai_verdict(monkeypatch):
    # #432: review:large — тоже «гейт 1 отработал» (review_labels.gate1_decided).
    # До фикса эта проверка требовала ровно review:ok, и PR с review:large без
    # единой ai:*-метки был невидим инварианту тем же классом, каким
    # scheduler.trigger_ai_review был невидим PR #412. Якорь — commit status
    # `harness/review` (#345), единый для review:ok/review:large — различение
    # по метке этому якорю не нужно вовсе (#424).
    pull = open_pr(432, labels=["review:large"])
    fake = FakeGh({
        "commits/sha432/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/432/timeline": timeline_with_review_large("2026-09-01T10:00:00Z"),
        "issues/432/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)  # заведомо больше порога 120 мин
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 432


# ── ai:failed — газ #196 исчерпал бюджет, но не эскалировал (находка ревью
# #439, класс #431): раньше check_stuck_review_gate пропускала ЛЮБОЙ PR с
# ai:*-меткой, включая ai:failed, и это состояние было невидимо инварианту.


def test_stuck_review_gate_flags_ai_failed_when_budget_exhausted_not_escalated(monkeypatch):
    pull = open_pr(163, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha163/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/163/timeline": timeline_with_review_ok("2026-09-01T10:00:00Z"),
        "issues/163/comments": [
            retry_marker_comment("2026-09-01T10:05:00Z", 1),
            retry_marker_comment("2026-09-01T10:10:00Z", 2),
            retry_marker_comment("2026-09-01T10:15:00Z", 3),
        ],
        "issues/120/comments": [],  # эскалации исчерпания ещё нет
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)  # заведомо больше порога 120 мин
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 163
    assert violations[0]["reason"] == "ai_failed_budget_exhausted_not_escalated"
    assert violations[0]["attempts_in_epoch"] == 3


def test_stuck_review_gate_silent_ai_failed_budget_not_exhausted_yet(monkeypatch):
    # Бюджет ещё не исчерпан в этой эпохе (2/3) — у #196 остаётся попытка,
    # инвариант не должен опережать газ.
    pull = open_pr(164, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha164/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/164/comments": [
            retry_marker_comment("2026-09-01T10:05:00Z", 1),
            retry_marker_comment("2026-09-01T10:10:00Z", 2),
        ],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []


def test_stuck_review_gate_silent_ai_failed_already_escalated(monkeypatch):
    # #196 сам эскалировал исчерпание в #120 в этой же эпохе — инвариант не
    # дублирует сигнал.
    pull = open_pr(165, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha165/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/165/comments": [
            retry_marker_comment("2026-09-01T10:05:00Z", 1),
            retry_marker_comment("2026-09-01T10:10:00Z", 2),
            retry_marker_comment("2026-09-01T10:15:00Z", 3),
        ],
        "issues/120/comments": [
            {"created_at": "2026-09-01T10:20:00Z",
             "body": f"🚨 edge-harness: {ri.pulse_guard.AI_REVIEW_EXHAUSTED_MARKER} #165\n..."},
        ],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []


def test_stuck_review_gate_silent_ai_failed_already_escalated_via_quota_marker(monkeypatch):
    # Находка ревью PR #439: газ #196 мог исчерпать бюджет ТРЕМЯ провалами, из
    # которых последний — quota_exhausted (ветка trigger_ai_review стоит
    # раньше счётчика попыток и эскалирует AI_REVIEW_QUOTA_MARKER, не
    # AI_REVIEW_EXHAUSTED_MARKER). До фикса инвариант знал только про
    # EXHAUSTED_MARKER и бил ложное «не эскалировано», хотя человек уже
    # оповещён тем же каналом #120.
    pull = open_pr(167, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha167/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/167/comments": [
            retry_marker_comment("2026-09-01T10:05:00Z", 1),
            retry_marker_comment("2026-09-01T10:10:00Z", 2),
            retry_marker_comment("2026-09-01T10:15:00Z", 3),
        ],
        "issues/120/comments": [
            {"created_at": "2026-09-01T10:20:00Z",
             "body": f"🚨 edge-harness: {ri.pulse_guard.AI_REVIEW_QUOTA_MARKER} #167\n..."},
        ],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []


def test_stuck_review_gate_ai_failed_mutation_guard(monkeypatch):
    # Мутация: убрать вызов check_ai_failed_budget_exhausted из ветки
    # ai:failed (вернуть "labels & ai_labels: continue" безусловно) — этот
    # тест обязан покраснеть, реальная проверка, не > 0 без содержания.
    pull = open_pr(166, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha166/statuses": gate1_status("2026-09-01T10:00:00Z"),
        "issues/166/timeline": timeline_with_review_ok("2026-09-01T10:00:00Z"),
        "issues/166/comments": [
            retry_marker_comment("2026-09-01T10:05:00Z", 1),
            retry_marker_comment("2026-09-01T10:10:00Z", 2),
            retry_marker_comment("2026-09-01T10:15:00Z", 3),
        ],
        "issues/120/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 0)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["reason"] == "ai_failed_budget_exhausted_not_escalated"


def test_stuck_review_gate_silent_when_neither_gate1_label_present(monkeypatch):
    # Без review:ok И без review:large гейт 1 ещё не отработал вовсе — этот
    # инвариант обязан молчать (не путать «гейт молчит» с «гейт застрял»).
    pull = open_pr(500, labels=[])
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", utc(2026, 9, 3, 14, 0), [pull]) == []
    assert fake.calls == []


def test_stuck_review_gate_mutation_guard(monkeypatch):
    # Мутация: тот же PR/таймлайн, порог опущен ниже возраста — обязан
    # появиться как нарушение (реальная мутация значения, не проверка > 0,
    # находка AI-ревью PR #249: старый вариант не краснел на снятии фикса).
    pull = open_pr(246, labels=["review:ok"])
    fake = FakeGh({
        "commits/sha246/statuses": gate1_status("2026-09-03T14:07:04Z"),
        "issues/246/timeline": timeline_with_review_ok("2026-09-03T14:07:04Z"),
        "issues/246/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 3, 14, 13)  # 6 минут — в пределах порога 120 (см. silent_within_threshold)
    assert ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull]) == []
    monkeypatch.setattr(ri, "UNHEALTHY_PR_AFTER_MINUTES", 1)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1


# ── #472: факт, не гипотеза — прод-форма живых PR #387/#329/#327 ────────────
#
# Живой алерт 2026-09-06T09:00:06Z (issue #120): «3 PR ... Авто-повтор #196
# либо исчерпал попытки, либо не сработал — нужен человек». Снято `gh api`
# по этому репозиторию тем же днём (issues/{n}/timeline, issues/{n}/comments)
# — не пересказ, реальные метки и реальные тексты маркеров-автоповторов.
# Единственное отличие фикстур от сырого ответа: комментарии без маркера
# `AI_REVIEW_RETRY_MARKER` выброшены — код под тестом (retry_budget_fact)
# смотрит только на маркер, отбрасывая остальные тем же фильтром сам.
ALERT_TIME = utc(2026, 9, 6, 9, 0, 6)


def test_stuck_gate_fact_pr387_never_had_a_verdict(monkeypatch):
    """#387: единственная эпоха (review:ok с 03:13:09), 3 маркера автоповтора
    ВСЕ в этой же эпохе — бюджет исчерпан в ТЕКУЩЕЙ эпохе, а ai:*-вердикта не
    было ни разу за всю жизнь PR (не «был и протух», как у #329/#327ниже)."""
    pull = open_pr(387, labels=["review:ok", "review:large", "review:large-ok"])
    fake = FakeGh({
        "commits/sha387/statuses": gate1_status("2026-09-06T03:13:09Z"),
        "issues/387/timeline": [
            {"event": "labeled", "label": {"name": "review:large"}, "created_at": "2026-09-05T23:02:59Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:13:09Z"},
        ],
        "issues/387/comments": [
            retry_marker_comment("2026-09-06T03:45:03Z", 1),
            retry_marker_comment("2026-09-06T03:46:33Z", 2),
            retry_marker_comment("2026-09-06T03:48:40Z", 3),
        ],
    })
    patch_gh(monkeypatch, fake)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", ALERT_TIME, [pull])
    assert len(violations) == 1
    item = violations[0]
    assert item["age_minutes"] == pytest.approx(346.9, abs=0.1)
    assert item["attempts_total"] == 3
    assert item["attempts_in_epoch"] == 3  # все три — в той же (единственной) эпохе
    assert item["verdict_ever"] is None  # НИ РАЗУ, а не «был и устарел»
    line = ri.stuck_gate_fact_line(item)
    assert "исчерпан в этой же эпохе (3/3)" in line
    assert "не было НИ РАЗУ" in line


def test_stuck_gate_fact_pr329_budget_carried_over_from_old_epoch(monkeypatch):
    """#329: на момент алерта текущая эпоха (review:ok с 03:48:22) не получила
    НИ ОДНОГО автоповтора, но глобальный счётчик (issue_marker_times без
    since — та же метрика, что видит scheduler.trigger_ai_review) уже
    показывает 3/3, потому что все три маркера принадлежат СТАРОЙ эпохе
    (якорь 03:05:20), которая своё уже получила вердикт (ai:ok, 03:46:52).
    С #431 это больше не бага: газ считает бюджет по attempts_in_epoch, у
    текущей эпохи свежие 0/3 — inline-текст называет это «должен сработать
    сам», не «исчерпан»."""
    pull = open_pr(329, labels=["review:ok"])
    fake = FakeGh({
        "commits/sha329/statuses": gate1_status("2026-09-06T03:48:22Z"),
        "issues/329/timeline": [
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:05:20Z"},
            {"event": "labeled", "label": {"name": "ai:failed"}, "created_at": "2026-09-06T03:13:07Z"},
            {"event": "unlabeled", "label": {"name": "ai:failed"}, "created_at": "2026-09-06T03:46:51Z"},
            {"event": "labeled", "label": {"name": "ai:ok"}, "created_at": "2026-09-06T03:46:52Z"},
            {"event": "unlabeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:48:00Z"},
            {"event": "unlabeled", "label": {"name": "ai:ok"}, "created_at": "2026-09-06T03:48:01Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:48:02Z"},
            {"event": "unlabeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:48:22Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:48:22Z"},
        ],
        "issues/329/comments": [
            retry_marker_comment("2026-09-06T03:37:16Z", 1),
            retry_marker_comment("2026-09-06T03:38:48Z", 2),
            retry_marker_comment("2026-09-06T03:40:21Z", 3),
        ],
    })
    patch_gh(monkeypatch, fake)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", ALERT_TIME, [pull])
    assert len(violations) == 1
    item = violations[0]
    assert item["labeled_at"] == "2026-09-06T03:48:22+00:00"  # эпоха началась ПОСЛЕ вердикта
    assert item["age_minutes"] == pytest.approx(311.7, abs=0.1)
    assert item["attempts_total"] == 3
    assert item["attempts_in_epoch"] == 0  # ни одного автоповтора в ТЕКУЩЕЙ эпохе
    assert item["verdict_ever"] == {"label": "ai:ok", "at": "2026-09-06T03:46:52+00:00"}
    line = ri.stuck_gate_fact_line(item)
    assert "попытки только в прошлых эпохах (3/3" in line
    assert "в текущей бюджет есть (0/3)" in line
    assert "вердикт был — ai:ok" in line
    assert "до текущей эпохи" in line  # вердикт (03:46:52) раньше anchor (03:48:22)


def test_stuck_gate_fact_pr327_budget_carried_over_and_not_four(monkeypatch):
    """#327: ровно ТА ЖЕ картина, что #329 — глобальный счётчик 3/3 из СТАРОЙ,
    уже решённой эпохи (якорь 03:09:50, вердикт ai:changes-requested в
    04:10:03), 0 автоповторов в текущей эпохе (якорь 05:46:16). Живой алерт
    ошибочно предполагал «четыре автоповтора при лимите три» — маркеров
    ровно три, не четыре (мутация ниже это и доказывает)."""
    pull = open_pr(327, labels=["review:ok", "review:large", "review:large-ok"])
    fake = FakeGh({
        "commits/sha327/statuses": gate1_status("2026-09-06T05:46:16Z"),
        "issues/327/timeline": [
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T03:09:50Z"},
            {"event": "labeled", "label": {"name": "ai:changes-requested"}, "created_at": "2026-09-06T04:10:03Z"},
            {"event": "unlabeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T05:23:35Z"},
            {"event": "unlabeled", "label": {"name": "ai:changes-requested"}, "created_at": "2026-09-06T05:23:36Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T05:23:36Z"},
            {"event": "labeled", "label": {"name": "ai:changes-requested"}, "created_at": "2026-09-06T05:33:52Z"},
            {"event": "unlabeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T05:46:15Z"},
            {"event": "unlabeled", "label": {"name": "ai:changes-requested"}, "created_at": "2026-09-06T05:46:16Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T05:46:16Z"},
        ],
        "issues/327/comments": [
            retry_marker_comment("2026-09-06T03:42:09Z", 1),
            retry_marker_comment("2026-09-06T03:43:30Z", 2),
            retry_marker_comment("2026-09-06T03:45:11Z", 3),
        ],
    })
    patch_gh(monkeypatch, fake)
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", ALERT_TIME, [pull])
    assert len(violations) == 1
    item = violations[0]
    assert item["age_minutes"] == pytest.approx(193.8, abs=0.1)
    assert item["attempts_total"] == 3  # не 4 — живая проверка алерта была неточна
    assert item["attempts_in_epoch"] == 0
    assert item["verdict_ever"] == {"label": "ai:changes-requested", "at": "2026-09-06T05:33:52+00:00"}
    line = ri.stuck_gate_fact_line(item)
    assert "3/3" in line and "в текущей бюджет есть (0/3)" in line


def test_stuck_gate_fact_line_mutation_guard_epoch_vs_global():
    """Мутация: без различения attempts_in_epoch от attempts_total текст не
    отличил бы «исчерпан старой эпохой» (#329/#327) от «исчерпан в этой же»
    (#387) — оба читались бы одинаково «исчерпан», и находка issue #472
    (перенос бюджета между эпохами) стала бы снова невидимой."""
    same_epoch = {
        "pr": 1, "age_minutes": 200.0, "labeled_at": "2026-09-06T00:00:00+00:00",
        "attempts_total": 3, "attempts_in_epoch": 3, "attempts_limit": 3,
        "last_attempt_at": "2026-09-06T01:00:00+00:00", "verdict_ever": None,
    }
    carried_over = dict(same_epoch, attempts_in_epoch=0)
    line_same = ri.stuck_gate_fact_line(same_epoch)
    line_carried = ri.stuck_gate_fact_line(carried_over)
    assert line_same != line_carried
    assert "исчерпан в этой же эпохе" in line_same
    assert "попытки только в прошлых эпохах" in line_carried
    assert "должен сработать сам" in line_carried


def test_stuck_gate_fact_line_budget_not_exhausted():
    item = {
        "pr": 2, "age_minutes": 150.0, "labeled_at": "2026-09-06T00:00:00+00:00",
        "attempts_total": 1, "attempts_in_epoch": 1, "attempts_limit": 3,
        "last_attempt_at": "2026-09-06T01:00:00+00:00", "verdict_ever": None,
    }
    line = ri.stuck_gate_fact_line(item)
    assert "не исчерпан (1/3)" in line
    assert "ближайшем тике" in line


# ── #779, блокирующая 3: третье состояние — бюджет есть, но летящий прогон
# придерживает автоповтор (класс #472 — алерт не гадает и не утверждает
# неверное) ──────────────────────────────────────────────────────────────

def test_stuck_gate_fact_line_budget_held_back_by_active_run():
    # Мутация: убери ветку held_back_by_run из stuck_gate_fact_line — этот
    # тест обязан покраснеть (текст вернётся к «должен сработать на ближайшем
    # тике», ложному в этом состоянии после #779).
    item = {
        "pr": 3, "age_minutes": 150.0, "labeled_at": "2026-09-06T00:00:00+00:00",
        "attempts_total": 0, "attempts_in_epoch": 0, "attempts_limit": 3,
        "last_attempt_at": None, "verdict_ever": None, "held_back_by_run": 34278765696,
    }
    line = ri.stuck_gate_fact_line(item)
    assert "есть (0/3)" in line
    assert "34278765696" in line
    assert "придержан летящим прогоном" in line
    assert "ближайшем тике" not in line  # не путать с безусловным «сработает сам»


def test_stuck_gate_fact_line_budget_carried_over_and_held_back_by_active_run():
    # Сестринская ветка (#472/#329/#327: total>=limit, in_epoch<limit —
    # попытки только в прошлых эпохах, текущей эпохе есть свежий бюджет) с
    # летящим прогоном одновременно. До фикса held_back_by_run проверялся
    # ТОЛЬКО в ветке total<limit — здесь текст молча возвращался к «должен
    # сработать сам», хотя тик увидит занятость и сделает continue, не трогая
    # бюджет: та же ложь класса #472, только во второй ветке. Мутация: убери
    # проверку held_back_by_run из этой ветки — тест обязан покраснеть.
    item = {
        "pr": 4, "age_minutes": 200.0, "labeled_at": "2026-09-06T00:00:00+00:00",
        "attempts_total": 3, "attempts_in_epoch": 0, "attempts_limit": 3,
        "last_attempt_at": "2026-09-06T01:00:00+00:00", "verdict_ever": None,
        "held_back_by_run": 34278765696,
    }
    line = ri.stuck_gate_fact_line(item)
    assert "попытки только в прошлых эпохах (3/3" in line
    assert "в текущей бюджет есть (0/3)" in line
    assert "придержан летящим прогоном run 34278765696" in line
    assert "должен сработать сам" not in line  # тот же класс #472 во второй ветке


def test_retry_budget_fact_reports_held_back_run(monkeypatch):
    # Прод-форма: retry_budget_fact спрашивает ТУ ЖЕ занятость, что и
    # scheduler.trigger_ai_review перед диспатчем (review_labels.
    # other_active_ai_review_runs) — третьей копии предиката не заводим.
    fake = FakeGh({
        "issues/711/comments": [],
        "actions/workflows/ai-review.yml/runs": {"workflow_runs": [
            {"id": 34278765696, "display_title": "ai-review PR #711", "status": "in_progress"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    anchor = utc(2026, 9, 8, 20, 30, 0)
    fact = ri.retry_budget_fact("mytab0r/edge-harness", 711, anchor)
    assert fact["held_back_by_run"] == 34278765696


def test_retry_budget_fact_no_active_run_reports_none(monkeypatch):
    # _DEFAULT_ROUTES отдаёт пустой список активных прогонов.
    fake = FakeGh({"issues/712/comments": []})
    patch_gh(monkeypatch, fake)
    anchor = utc(2026, 9, 8, 20, 30, 0)
    fact = ri.retry_budget_fact("mytab0r/edge-harness", 712, anchor)
    assert fact["held_back_by_run"] is None


def test_stuck_review_gate_reports_held_back_run_via_stuck_gate_fact_line(monkeypatch):
    # Сквозной сценарий (не только unit на stuck_gate_fact_line): PR без
    # ai:*-метки, гейт 1 отработал дольше порога, бюджет автоповтора ещё не
    # исчерпан (0/3), но ai-review.yml для этого PR прямо сейчас летит —
    # инвариант 3 обязан назвать ИМЕННО это, а не соврать «должен сработать
    # на ближайшем тике» (класс #472).
    pull = open_pr(711, labels=["review:ok"])
    fake = FakeGh({
        "commits/sha711/statuses": gate1_status("2026-09-08T20:30:00Z"),
        "issues/711/timeline": timeline_with_review_ok("2026-09-08T20:30:00Z"),
        "issues/711/comments": [],
        "actions/workflows/ai-review.yml/runs": {"workflow_runs": [
            {"id": 34278765696, "display_title": "ai-review PR #711", "status": "in_progress"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 8, 23, 0, 0)  # намного больше порога 120 мин
    violations = ri.check_stuck_review_gate("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["held_back_by_run"] == 34278765696
    line = ri.stuck_gate_fact_line(violations[0])
    assert "придержан летящим прогоном run 34278765696" in line


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 4: openspec/changes полностью отмечен и не заархивирован
# ══════════════════════════════════════════════════════════════════════════


def write_tasks_md(tmp_path, name, content):
    change_dir = tmp_path / name
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "tasks.md").write_text(content, encoding="utf-8")
    return change_dir


def test_unarchived_complete_flags_fully_checked(tmp_path):
    write_tasks_md(tmp_path, "walking-skeleton", "- [x] один\n- [x] два\n")
    violations = ri.check_unarchived_complete_changes(tmp_path)
    assert violations == [{"change": "walking-skeleton", "checked": 2}]


def test_unarchived_complete_silent_when_box_unchecked(tmp_path):
    write_tasks_md(tmp_path, "ai-review-gate", "- [x] один\n- [ ] два — в пуле\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_silent_when_no_checkboxes(tmp_path):
    write_tasks_md(tmp_path, "dsh-pulse-self-update", "просто текст без чекбоксов\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_ignores_archive_dir(tmp_path):
    write_tasks_md(tmp_path / "archive", "already-done", "- [x] всё\n")
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_complete_mutation_guard(tmp_path):
    write_tasks_md(tmp_path, "walking-skeleton", "- [x] один\n- [x] два\n")
    assert len(ri.check_unarchived_complete_changes(tmp_path)) == 1
    # Реальная мутация: каталог, буквально названный "archive", с полностью
    # отмеченным tasks.md прямо внутри него (не под ним) — фильтр
    # `entry.name == "archive"` обязан его исключить. Снять фильтр (удалить
    # условие `or entry.name == "archive"` в repo_invariants.py) — тест ниже
    # покраснеет: без фильтра "archive" стал бы обычной записью с checked=1.
    write_tasks_md(tmp_path, "archive", "- [x] один\n")
    violations = ri.check_unarchived_complete_changes(tmp_path)
    assert all(v["change"] != "archive" for v in violations)


# ── Инвариант 4, второй путь: proposal.md + задача completed + нет PR ──────
#
# Прод-форма: реальный текст proposal.md двух каталогов этого репозитория
# (openspec/changes/reopen-rejected, openspec/changes/task-rework-loop, сняты
# 2026-09-07) и реальная форма ответа `gh api repos/.../issues/369`.

REOPEN_REJECTED_PROPOSAL = """# reopen-rejected: закрытая задача не переоткрывается никогда (#369)

Задача: #369. Дельта-спека:
[specs/journal-tasks-hands/spec.md](specs/journal-tasks-hands/spec.md).
Развилка вариантов и почему выбран текущий — [design.md](design.md).

## Зачем

Решение владельца.
"""

# Реальная проза task-rework-loop: собственной задачи ещё нет («issue в пуле
# пока не заведена этим change»), а первый #N по всему файлу (#200, PR,
# слитый до этого change) — чужой номер. declared_change_task обязан вернуть
# None здесь, а не 200 (иначе второй путь закрыл бы change по состоянию
# ЧУЖОЙ, уже решённой задачи — живая находка при замере на этом репозитории,
# 2026-09-07: без абзацного якоря второй путь ошибочно считал бы завершённым
# dsh-edge-plugin-system и подобные — не по этому фикстюру, но по тому же
# классу «первый #N в файле — не то же самое, что декларация»).
TASK_REWORK_LOOP_PROPOSAL = """# task-rework-loop: конечный цикл реворка — бюджет, needs-spec, а не беклог

Задача владельца: сформулирована в чате 2026-09-03, issue в пуле пока не
заведена этим change — заводится как часть tasks.md (п.0).

## Класс проблемы

scripts/orchestra/scheduler.py::unhealthy_pulls (слито PR #200) снимает
исполнителя с задачи.
"""

ISSUE_369_COMPLETED = {"state": "closed", "state_reason": "completed"}

# Реальный шаблон тела PR этого репозитория (буквальный пункт чек-листа,
# скопирован из PR #261, `gh api repos/mytab0r/edge-harness/pulls/261`,
# 2026-09-07) — «Дифф ограничен `openspec/changes/<id>/`» — тот случай, когда
# открытый PR ссылается на путь change буквально.
OPEN_PR_REFERENCING_PATH = (
    "## Чек-лист\n\n- [x] Дифф ограничен `openspec/changes/reopen-rejected/`"
)
# Реальное тело другого открытого PR этого репозитория (#618, 2026-09-07),
# упоминает "#477"/пулс-гвардию, но не путь reopen-rejected — контекст
# соседней, не связанной задачи.
OPEN_PR_UNRELATED = (
    "Задача: #616.\n\n## Дефект\n\n"
    "`scripts/orchestra/pulse_guard.py::failure_watch` (#477) заводил задачи "
    "`task+ci-failure` БЕЗ какого-либо суточного потолка."
)


def test_declared_change_task_reads_declaration_paragraph():
    assert ri.declared_change_task(REOPEN_REJECTED_PROPOSAL) == 369


def test_declared_change_task_ignores_prose_before_declaration():
    assert ri.declared_change_task(TASK_REWORK_LOOP_PROPOSAL) is None


# Мутационная гвардия (ревью PR #663, некритичное замечание «якорь `^Задач`
# слишком широкий — матчит и «Задача владельца:»»). Абзац ниже — та же чужая
# декларация, что открывает реальный task-rework-loop/proposal.md, но с
# добавленным чужим #999 в том же абзаце: со старым якорём `^Задач` (без
# требования двоеточия сразу после слова) этот абзац матчился бы как
# декларация и declared_change_task вернул бы 999 — чужой номер задачи,
# который второй путь check_unarchived_complete_changes закрыл бы по чужому
# состоянию. Снять `(?:а|и):` из _DECLARED_TASK_PARA_RE (вернуть `^Задач`) —
# тест ниже покраснеет.
OWNER_PROSE_WITH_STRAY_ISSUE_NUMBER = """# task-rework-loop: конечный цикл реворка

Задача владельца: сформулирована в чате 2026-09-03, ссылается на #999 из
истории обсуждения — issue в пуле пока не заведена этим change.

## Класс проблемы

Прочая проза.
"""


def test_declared_change_task_ignores_owner_prose_even_with_stray_issue_number():
    assert ri.declared_change_task(OWNER_PROSE_WITH_STRAY_ISSUE_NUMBER) is None


# ── fetch_task_states: 404 честно пропускается, любая другая ошибка — нет ──
#
# Некритичное замечание ревью PR #663: RuntimeError от gh() был неотличим от
# 404 — сетевой сбой/квота молча превращали бы «нарушений нет» в ложно-зелёный
# инвариант 4 ровно тогда, когда кто-то проверяет условие возврата в
# CI_GATING. Прод-форма сообщения — реальный вывод `gh api` на несуществующий
# issue этого репозитория (см. текст ниже, воспроизведён живым вызовом).
REAL_GH_404_STDERR = (
    'repos/mytab0r/edge-harness/issues/999999999: {"message":"Not Found",'
    '"documentation_url":"https://docs.github.com/rest/issues/issues#get-an-issue",'
    '"status":"404"} (HTTP 404)'
)


def test_fetch_task_states_skips_missing_issue_on_404(monkeypatch):
    def fake(*args):
        raise RuntimeError(REAL_GH_404_STDERR)

    monkeypatch.setattr(ri, "gh", fake)
    assert ri.fetch_task_states(REPO, {999999999}) == {}


def test_fetch_task_states_raises_on_non_404_error(monkeypatch):
    """Мутация: снять проверку `"HTTP 404" not in str(exc)` (вернуть голое
    `except RuntimeError: continue`, как было до фикса #663) — этот тест
    покраснеет, потому что сетевой сбой перестанет отличаться от 404 и
    молча даст пустой словарь вместо честного падения."""

    def fake(*args):
        raise RuntimeError("repos/mytab0r/edge-harness/issues/369: "
                            "API rate limit exceeded for installation")

    monkeypatch.setattr(ri, "gh", fake)
    with pytest.raises(RuntimeError, match="rate limit"):
        ri.fetch_task_states(REPO, {369})


def write_proposal(tmp_path, name, content):
    change_dir = tmp_path / name
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "proposal.md").write_text(content, encoding="utf-8")
    return change_dir


def test_unarchived_second_path_flags_closed_completed_no_open_pr(tmp_path):
    write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    violations = ri.check_unarchived_complete_changes(
        tmp_path, task_states={369: ISSUE_369_COMPLETED}, open_pull_texts=[OPEN_PR_UNRELATED]
    )
    assert violations == [{"change": "reopen-rejected", "task": 369, "reason": "task-closed"}]


def test_unarchived_second_path_silent_when_no_data_passed(tmp_path):
    # Обратная совместимость: старые вызовы (только changes_dir) не должны
    # начать находить новые нарушения молча — их тут не с чем сравнить,
    # второй путь просто не запускается без обоих аргументов.
    write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    assert ri.check_unarchived_complete_changes(tmp_path) == []


def test_unarchived_second_path_silent_when_task_not_completed(tmp_path):
    write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    still_open = {369: {"state": "open", "state_reason": None}}
    assert ri.check_unarchived_complete_changes(
        tmp_path, task_states=still_open, open_pull_texts=[]
    ) == []
    not_planned = {369: {"state": "closed", "state_reason": "not_planned"}}
    assert ri.check_unarchived_complete_changes(
        tmp_path, task_states=not_planned, open_pull_texts=[]
    ) == []


def test_unarchived_second_path_silent_when_open_pr_references_path(tmp_path):
    write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    violations = ri.check_unarchived_complete_changes(
        tmp_path,
        task_states={369: ISSUE_369_COMPLETED},
        open_pull_texts=[OPEN_PR_UNRELATED, OPEN_PR_REFERENCING_PATH],
    )
    assert violations == []


def test_unarchived_second_path_yields_to_tasks_md_even_if_unchecked(tmp_path):
    # Живой случай, найденный замером на этом репозитории 2026-09-07:
    # openspec/changes/dsh-edge-plugin-system несёт tasks.md с 7
    # неотмеченными пунктами из 44 — реальная незавершённая работа, хотя
    # эпик-issue (#78) давно закрыт completed. Второй путь обязан молчать
    # везде, где tasks.md вообще существует, — не только там, где он
    # полностью отмечен. Доказано мутацией (2026-09-07): убрать `continue`
    # сразу после блока tasks.md в repo_invariants.py — этот тест краснеет
    # (путь (b) начинает молча перебивать реальный незакрытый чеклист).
    change_dir = write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    (change_dir / "tasks.md").write_text("- [x] один\n- [ ] два — ещё в работе\n", encoding="utf-8")
    violations = ri.check_unarchived_complete_changes(
        tmp_path, task_states={369: ISSUE_369_COMPLETED}, open_pull_texts=[]
    )
    assert violations == []


def test_unarchived_second_path_mutation_guard(tmp_path):
    """Докажи мутацией (AGENTS.md, «Починил случай — закрой класс»): убери
    условие `state.get("state_reason") != "completed"` в
    repo_invariants.py::check_unarchived_complete_changes (например, замени
    на `not state`) — этот тест краснеет, потому что задача с
    state_reason="not_planned" (интеграции, брошенные не как решённые)
    начинает ложно закрывать change. Проверено вручную 2026-09-07: со снятым
    условием test_unarchived_second_path_silent_when_task_not_completed
    падает (violations непусты вместо []), с условием — проходит."""
    write_proposal(tmp_path, "reopen-rejected", REOPEN_REJECTED_PROPOSAL)
    not_planned = {369: {"state": "closed", "state_reason": "not_planned"}}
    assert ri.check_unarchived_complete_changes(
        tmp_path, task_states=not_planned, open_pull_texts=[]
    ) == []


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 5: пересекающаяся улика file:line у двух открытых задач
# ══════════════════════════════════════════════════════════════════════════

# Реальные тела issue #202/#213 (обрезаны до релевантных фрагментов), сняты
# gh api 2026-09-03. Живой класс: contract_check.py разобран трижды под
# разными номерами.
ISSUE_202_BODY = """## Факты

- `scripts/orchestra/contract_check.py:129-142` — конфликтом считается любой
  другой открытый PR, в теле которого встречается `#{issue_number}`.
"""

ISSUE_213_BODY = """## Где именно (file:line)

- `scripts/orchestra/contract_check.py:103-110` — извлечение issue_number
  ПРОВЕРЯЕМОГО PR.
- `scripts/orchestra/contract_check.py:139-152` — извлечение issue_number
  у ЧУЖИХ PR при поиске конфликта.
"""

# Реальное тело issue #204/#217 (обрезано) — оба трогают check_pr.py, но по
# РАЗНЫМ поводам и БЕЗ пересекающихся строк — не должны склеиваться.
ISSUE_204_BODY = """## Факты

- `scripts/review/check_pr.py:114-125` — при диффе больше порога ставится
  `review:large`, слияние требует ещё и `review:large-ok`.
"""

ISSUE_217_BODY = """## Что нужно

Проверка в детерминированном гейте (`scripts/review/check_pr.py` — там уже
считается размер диффа и ставятся вердикт-метки), падающая громко.
"""


def test_extract_locators_line_form():
    locs = ri.extract_locators(ISSUE_202_BODY)
    assert ("scripts/orchestra/contract_check.py", 129, 142) in locs


def test_duplicate_evidence_catches_202_213_overlap():
    tasks = [
        task_issue(202, "Контракт не различает эпик и лист", ISSUE_202_BODY),
        task_issue(213, "contract: асимметричное распознавание", ISSUE_213_BODY),
    ]
    violations = ri.check_duplicate_evidence(tasks)
    assert len(violations) == 1
    assert violations[0]["issues"] == [202, 213]
    assert "contract_check.py" in violations[0]["shared_location"]


def test_duplicate_evidence_does_not_glue_204_217_different_defects():
    # оба трогают check_pr.py, но #217 не называет строку вовсе — пересечения
    # диапазонов нет, склейки быть не должно (класс #204/#217 назван прямо
    # в задаче #244 как "не путать")
    tasks = [
        task_issue(204, "review:large-ok не ставит автоматика", ISSUE_204_BODY),
        task_issue(217, "PR может молча откатить main", ISSUE_217_BODY),
    ]
    assert ri.check_duplicate_evidence(tasks) == []


def test_duplicate_evidence_ignores_markdown_files():
    # честный потолок: .md исключены (см. docstring extract_locators) — общее
    # правило AGENTS.md, процитированное двумя НЕсвязанными задачами, не
    # должно склеивать их
    body_a = "Смотри правило `AGENTS.md:77` про тормоз без газа."
    body_b = "То же правило `AGENTS.md:77` касается и этого случая."
    tasks = [task_issue(1, "A", body_a), task_issue(2, "B", body_b)]
    assert ri.check_duplicate_evidence(tasks) == []


def test_duplicate_evidence_mutation_guard():
    tasks = [
        task_issue(202, "A", ISSUE_202_BODY),
        task_issue(213, "B", ISSUE_213_BODY),
    ]
    assert len(ri.check_duplicate_evidence(tasks)) == 1
    # мутация: убрать сравнение файлов (сравнивать только диапазоны) слило бы
    # СОВЕРШЕННО разные файлы с совпадающими номерами строк — тест ниже
    # доказывает, что _locators_overlap требует совпадения файла
    assert ri._locators_overlap(("a.py", 10, 20), ("b.py", 10, 20)) is False
    assert ri._locators_overlap(("a.py", 10, 20), ("a.py", 15, 25)) is True


# build_report() с #710 по умолчанию вызывает fetch_open_task_issues_with_body
# (task_deps.fetch_pool через GraphQL) для инварианта 9 (ИСПРАВЛЕНО ревью PR
# #711, major: комментарий раньше ошибочно называл «8» — коллизия с уже
# существующим инвариантом 8, check_wasted_ai_review_runs, #740). Отключается
# параметром check_declared_deps=False (замечание 1 ревью PR #711, main()
# передаёт его на периодическом пульсе `--orchestra`) — FakeGh-фикстуры
# build_report-тестов ниже, вызывающих build_report БЕЗ этого параметра
# (то есть с фетчем инварианта 9 включённым по умолчанию), нуждаются в
# маршруте "graphql", даже если сам инвариант 9 их не интересует (иначе
# FakeGh падает AssertionError «нет маршрута»). Пустой пул — валидный ответ
# (открытых task-issues нет).
def graphql_pool_page(nodes=()):
    return {"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": list(nodes),
    }}}}


def graphql_issue_node(number, issue_body="", blocked_by=(), blocking=()):
    # Параметр НЕ называется body= — та же гвардия класса #124
    # (grep ',\s*body=' в repo-ci.yml матчит и сигнатуру функции с дефолтом
    # body="", не только вызов gh()) уже задокументирована у task_issue()
    # выше в этом файле; здесь тот же приём.
    return {
        "number": number,
        "title": "",
        "body": issue_body,
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "blockedBy": {"totalCount": len(blocked_by),
                      "nodes": [{"number": n, "state": "OPEN"} for n in blocked_by]},
        "blocking": {"totalCount": len(blocking),
                     "nodes": [{"number": n, "state": "OPEN"} for n in blocking]},
    }


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 6: защита main-ветки не откатилась молча (#341)
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма — сырой `gh api repos/mytab0r/edge-harness/branches/main/protection`
# на момент задачи #341 (2026-09-06), не пересказ.
HEALTHY_PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": ["test", "contract"]},
    "enforce_admins": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}


def test_branch_protection_healthy_snapshot_no_violations():
    assert ri.check_branch_protection_drift(HEALTHY_PROTECTION) == []


def test_branch_protection_flags_enforce_admins_disabled():
    # Живой класс задачи #341: enforce_admins стоял в false, admin-токен
    # сливал мимо всех обязательных проверок (HTTP 405 при попытке
    # воспроизвести проверку задним числом подтвердил пропуск).
    broken = {**HEALTHY_PROTECTION, "enforce_admins": {"enabled": False}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "enforce_admins"
    assert "проверок" in violations[0]["consequence"]


def test_branch_protection_flags_missing_context():
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": True, "contexts": ["test"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.contexts"
    assert violations[0]["actual"] == ["test"]
    assert violations[0]["expected"] == sorted(ri.EXPECTED_STATUS_CHECK_CONTEXTS)


def test_branch_protection_flags_extra_context_too():
    # Не только пропажа контекста — лишний неожиданный контекст тоже дрейф
    # (кто-то включил обязательную проверку, для которой не подтверждён
    # живой прогон, и вся очередь PR рискует зависнуть в «Expected»).
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": True,
                                          "contexts": ["test", "contract", "harness/review"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.contexts"


def test_branch_protection_flags_strict_disabled():
    broken = {**HEALTHY_PROTECTION,
              "required_status_checks": {"strict": False, "contexts": ["test", "contract"]}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "required_status_checks.strict"


def test_branch_protection_flags_force_pushes_enabled():
    broken = {**HEALTHY_PROTECTION, "allow_force_pushes": {"enabled": True}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "allow_force_pushes"


def test_branch_protection_flags_deletions_enabled():
    broken = {**HEALTHY_PROTECTION, "allow_deletions": {"enabled": True}}
    violations = ri.check_branch_protection_drift(broken)
    assert len(violations) == 1
    assert violations[0]["setting"] == "allow_deletions"


def test_branch_protection_missing_keys_treated_as_drift():
    # Ответ GitHub без ключа вовсе (сеть отдала урезанный объект, старый
    # формат) — трактуем как расхождение, не как «всё ок»: при сомнении гейт
    # не ослабляется (AGENTS.md).
    violations = ri.check_branch_protection_drift({})
    settings = {v["setting"] for v in violations}
    assert settings == {
        "enforce_admins", "required_status_checks.strict",
        "required_status_checks.contexts", "allow_force_pushes", "allow_deletions",
    }


def test_branch_protection_all_violations_name_a_consequence():
    # AGENTS.md: «Инвариант обязан называть, ЧТО сломается при расхождении».
    violations = ri.check_branch_protection_drift({})
    assert all(v["consequence"] for v in violations)


def test_branch_protection_opt_in_disabled_by_default(monkeypatch):
    # build_report() по умолчанию НЕ дёргает fetch_branch_protection вовсе:
    # GITHUB_TOKEN не может получить право administration ни при какой
    # правке permissions в workflow — вызов оттуда всегда 403. Гвардия ловит
    # регресс «кто-то включил инвариант 6 в стандартный отчёт по умолчанию»
    # мутацией — если бы default стал True, fetch полетел бы в FakeGh без
    # маршрута и тест упал бы AssertionError из самого FakeGh.
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert 6 not in findings
    assert any("⏭️" in line and "[6]" in line for line in lines)


def test_branch_protection_opt_in_enabled_reads_and_reports(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "branches/main/protection": HEALTHY_PROTECTION,
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now, check_branch_protection=True)
    assert findings[6] == []
    assert any("💚" in line and "[6]" in line for line in lines)


def test_branch_protection_not_in_ci_gating():
    # Не может быть в CI_GATING по конструкции: включение обязательной
    # проверки для инварианта, которому GITHUB_TOKEN не может дать ответ,
    # красило бы main на КАЖДОМ прогоне — хуже отсутствия проверки.
    assert 6 not in ri.CI_GATING


def test_declared_deps_opt_out_skips_expensive_graphql_fetch(monkeypatch):
    # Замечание 1 ревью PR #711: на периодическом пульсе (`--orchestra`,
    # main() передаёт check_declared_deps=False) дорогой пагинированный
    # GraphQL-фетч тел не должен вызываться вовсе — FakeGh без маршрута
    # "graphql" здесь и есть доказательство: был бы вызван — упал бы
    # AssertionError самого FakeGh.
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now, check_declared_deps=False)
    assert findings[9] == []
    assert any("⏭️" in line and "[9]" in line for line in lines)


def test_declared_deps_fetch_error_isolated_does_not_abort_whole_report(monkeypatch):
    # Замечание 2 ревью PR #711: TaskDepsError (пул-аномалия, >20 рёбер на
    # issue, task_deps.py:165-180) из фетча инварианта 9 не должна ронять
    # ВЕСЬ build_report — инвариант 7 (гейтящий) обязан по-прежнему
    # посчитаться, а не пропасть вместе с исключением.
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": ri.task_deps.TaskDepsError(
            "issue #999: blockedBy усечён (20 из 41)"),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 6, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[9] == []
    assert 7 in findings  # инвариант 7 всё равно посчитан, не съеден исключением
    assert any("🚨" in line and "[9]" in line and "фетч пула упал" in line for line in lines)


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 7: двусмысленная формула принадлежности плагина (#219)
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма: реальные строки из репозитория ДО правки #219
# (openspec/changes/dsh-in-job/), на которых класс и случился, — не пересказ.


def write_md(tmp_path, rel, content):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_ambiguous_artifact_phrase_flags_prod_forms(tmp_path):
    write_md(tmp_path, "openspec/changes/dsh-in-job/tasks.md",
             "- [ ] Подключение плагинов владельца (combo-router из"
             " `vars.PLUGINS_SUITE_URL`)\n")
    write_md(tmp_path, "openspec/changes/dsh-in-job/design.md",
             "**Провайдер — плагины владельца, не новый код.**\n")
    violations = ri.check_ambiguous_artifact_phrase(tmp_path)
    assert {v["file"] for v in violations} == {
        "openspec/changes/dsh-in-job/tasks.md",
        "openspec/changes/dsh-in-job/design.md",
    }
    by_file = {v["file"]: v for v in violations}
    assert by_file["openspec/changes/dsh-in-job/design.md"]["line"] == 1
    assert "плагины владельца" in by_file["openspec/changes/dsh-in-job/design.md"]["match"]


def test_ambiguous_artifact_phrase_silent_on_unambiguous_wording(tmp_path):
    # Обе разрешённые замены и порядок слов «владелец подключает свой плагин» —
    # инвариант молчит: гвардится именно двусмысленная формула, не упоминание
    # владельца вообще.
    write_md(tmp_path, "docs/x.md",
             "артефакт владельца по адресу X\n"
             "наш плагин (пишем мы, не апстрим)\n"
             "владелец подключает свой плагин\n")
    assert ri.check_ambiguous_artifact_phrase(tmp_path) == []


def test_ambiguous_artifact_phrase_ignores_non_document_dirs(tmp_path):
    write_md(tmp_path, ".git/notes.md", "плагины владельца\n")
    write_md(tmp_path, "node_modules/pkg/README.md", "плагины владельца\n")
    assert ri.check_ambiguous_artifact_phrase(tmp_path) == []


def test_ambiguous_artifact_phrase_mutation_guard(tmp_path):
    path = write_md(tmp_path, "docs/x.md", "документ без формулы\n")
    assert ri.check_ambiguous_artifact_phrase(tmp_path) == []
    # Мутация: вернуть прод-форму (строка proposal.md до #219, с заглавной
    # буквы — регистр не должен спасать) — инвариант обязан покраснеть.
    path.write_text("Плагины владельца устанавливаются job'ом.\n", encoding="utf-8")
    violations = ri.check_ambiguous_artifact_phrase(tmp_path)
    assert len(violations) == 1
    assert violations[0]["line"] == 1


def test_ambiguous_artifact_phrase_in_ci_gating_with_gas():
    # Инвариант включён в обязательную проверку сразу при создании (#219):
    # на момент включения ноль нарушений — фраза вычищена тем же PR. Газ
    # объявлен в GATING_RELEASE_CONDITION — гвардия main() падает громко,
    # если газ отберут, не назвав замену (AGENTS.md, «Тормоз без газа»).
    assert 7 in ri.CI_GATING
    assert 7 in ri.GATING_RELEASE_CONDITION


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 8 (#740): дорогой прогон ai-review после вердикта при
# неизменном отпечатке диффа — живой случай PR #333, 2026-09-08
# ══════════════════════════════════════════════════════════════════════════
#
# ai_run(...) — прод-форма записи `workflow_runs[]` эндпоинта
# `actions/workflows/ai-review.yml/runs` (display_title/created_at/
# conclusion — те же поля, что уже читает review_labels.
# other_active_ai_review_runs).

def ai_verdict_comment(pr: int, diff_fp: str, created_at: str, verdict: str = "approve"):
    bot = {"login": "github-actions[bot]", "type": "Bot"}
    return {
        "user": bot, "created_at": created_at,
        "body": f"pr: {pr}\nhead: deadbeef\nreviewer: {verdict}\ndiff: {diff_fp}\n\nOK\n",
    }


def ai_run(pr: int, created_at: str, run_id: int = 1, conclusion: str = "success"):
    return {
        "id": run_id,
        "display_title": ri.review_labels.ai_review_run_name(pr),
        "created_at": created_at,
        "conclusion": conclusion,
    }


def pr_files(sha: str = "aaa111"):
    return [{"filename": "f.py", "status": "modified", "sha": sha,
              "patch": "@@ -1 +1 @@\n-a\n+b\n"}]


def test_wasted_ai_review_flags_run_after_verdict_with_unchanged_fingerprint(monkeypatch):
    # Живой случай PR #333: вердикт 06:08:55Z, дифф не менялся, но прогон
    # стартовал 06:11:04Z — should_run_ai_review обязан был отдать go=false.
    files = pr_files()
    fp = ri.review_labels.diff_fingerprint(files)
    fake = FakeGh({
        "issues/333/comments": [ai_verdict_comment(333, fp, "2026-09-08T06:08:55Z")],
        "pulls/333/files": files,
        "actions/workflows/ai-review.yml/runs": {
            "workflow_runs": [ai_run(333, "2026-09-08T06:11:04Z", run_id=34193569472)],
        },
    })
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["review:ok", "ai:ok"])
    result = ri.check_wasted_ai_review_runs(REPO, [pull])
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert len(result.violations) == 1
    assert result.violations[0]["pr"] == 333
    assert result.violations[0]["wasted_runs"][0]["run_id"] == 34193569472


def test_wasted_ai_review_silent_when_no_run_after_verdict(monkeypatch):
    # Тот же неизменный отпечаток, но прогонов после вердикта не было —
    # ровно то, что должно быть в норме (should_run_ai_review сработал).
    files = pr_files()
    fp = ri.review_labels.diff_fingerprint(files)
    fake = FakeGh({
        "issues/333/comments": [ai_verdict_comment(333, fp, "2026-09-08T06:08:55Z")],
        "pulls/333/files": files,
        "actions/workflows/ai-review.yml/runs": {
            "workflow_runs": [ai_run(333, "2026-09-08T06:00:00Z", run_id=1)],  # ДО вердикта
        },
    })
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["review:ok", "ai:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == ri.check_result.ok()


def test_wasted_ai_review_silent_when_fingerprint_actually_changed(monkeypatch):
    # Отпечаток разошёлся — новый прогон после вердикта ОБОСНОВАН реальной
    # правкой, не растрата. Инвариант не должен путать «прогон случился» с
    # «прогон был не нужен».
    stored_fp = "old-fingerprint-does-not-match"
    fake = FakeGh({
        "issues/333/comments": [ai_verdict_comment(333, stored_fp, "2026-09-08T06:08:55Z")],
        "pulls/333/files": pr_files(),  # реальный текущий отпечаток другой
        "actions/workflows/ai-review.yml/runs": {
            "workflow_runs": [ai_run(333, "2026-09-08T06:11:04Z", run_id=2)],
        },
    })
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["review:ok", "ai:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == ri.check_result.ok()


def test_wasted_ai_review_silent_without_final_verdict_label():
    # Нет ai:ok/ai:changes-requested — сравнивать не с чем, инвариант не
    # обязан идти в сеть вовсе (нет маршрута в FakeGh — упадёт сам, если
    # код полезет в comments без нужды).
    fake = FakeGh({})
    pull = open_pr(333, labels=["review:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == ri.check_result.ok()
    assert fake.calls == []


def test_wasted_ai_review_silent_when_ai_failed_not_final(monkeypatch):
    # ai:failed — не финальный вердикт (газ #196, автоповтор), инвариант не
    # трогает такой PR вовсе, даже если отпечаток совпал бы.
    fake = FakeGh({})
    pull = open_pr(333, labels=["review:ok", "ai:failed"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == ri.check_result.ok()
    assert fake.calls == []


def test_wasted_ai_review_silent_without_verdict_comment(monkeypatch):
    fake = FakeGh({"issues/333/comments": []})
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["ai:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == ri.check_result.ok()


def test_wasted_ai_review_mutation_guard_missing_run_after_filter(monkeypatch):
    # Докажи мутацией: убрать фильтр «прогон СТАРТОВАЛ ПОСЛЕ вердикта» из
    # ai_review_runs_after (взять все прогоны PR, не только поздние) — тест
    # test_wasted_ai_review_silent_when_no_run_after_verdict выше обязан
    # покраснеть. Здесь — прямая проверка самой функции-фильтра на
    # прод-форме полей ("id"/"display_title"/"created_at"/"conclusion").
    runs_response = {"workflow_runs": [
        ai_run(333, "2026-09-08T06:00:00Z", run_id=1),  # до since — не считается
        ai_run(333, "2026-09-08T07:00:00Z", run_id=2),  # после since — считается
        ai_run(999, "2026-09-08T07:00:00Z", run_id=3),  # чужой PR — не считается
    ]}
    fake = FakeGh({"actions/workflows/ai-review.yml/runs": runs_response})
    found = ri.ai_review_runs_after(REPO, 333, "2026-09-08T06:30:00Z", fake)
    assert [r["run_id"] for r in found] == [2]


def test_ai_review_runs_after_raises_on_unexpected_form():
    # Находка issue #1096/#1109 (F7): раньше форма не по контракту (не dict,
    # без ключа workflow_runs, значение под ключом не список) молча читалась
    # как "прогонов нет" — теперь RuntimeError, вызывающая сторона
    # (check_wasted_ai_review_runs) решает третий исход, не эта функция.
    fake = FakeGh({"actions/workflows/ai-review.yml/runs": {"message": "rate limited"}})
    with pytest.raises(RuntimeError):
        ri.ai_review_runs_after(REPO, 333, "2026-09-08T06:30:00Z", fake)


def test_runs_of_rejects_key_present_but_not_list():
    # Ключ ПРИСУТСТВУЕТ, но значение не список — `{}`-дефолт или
    # `"workflow_runs" in dict`-проверка пропустили бы это с ложным
    # «форма подтверждена» (доводка ai-review PR #1110). runs_of — единая
    # точка разбора формы (F3 в границах repo_invariants.py, PR #1140):
    # обе ошибки формы живут в одном месте.
    with pytest.raises(RuntimeError):
        ri.runs_of({"workflow_runs": None}, "worker.yml")
    with pytest.raises(RuntimeError):
        ri.runs_of({"workflow_runs": {"count": 3}}, "worker.yml")
    with pytest.raises(RuntimeError):
        ri.runs_of({"message": "secondary rate limit"}, "worker.yml")
    assert ri.runs_of({"workflow_runs": []}, "worker.yml") == []


def test_wasted_ai_review_unknown_reason_names_pr_step_and_denominator(monkeypatch):
    # Чеклист ai-review PR #1140: знаменатель причины — только PR с финальным
    # ai-вердиктом (не все открытые), а сам факт — «какой PR, какой шаг,
    # какая ошибка» (текст RuntimeError его уже называет), не перечень всех
    # трёх шагов разом (AGENTS.md «Алерт не гадает»: данные для факта есть —
    # пойманное исключение).
    files = pr_files()
    fp = ri.review_labels.diff_fingerprint(files)
    fake = FakeGh({
        "issues/333/comments": [ai_verdict_comment(333, fp, "2026-09-08T06:08:55Z")],
        "pulls/333/files": files,
        "actions/workflows/ai-review.yml/runs": RuntimeError("gh api: HTTP 502: Bad Gateway"),
    })
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["review:ok", "ai:ok"])
    plain = open_pr(334, labels=["review:ok"])  # без ai-вердикта — не в знаменателе
    result = ri.check_wasted_ai_review_runs(REPO, [pull, plain])
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "1 из 1" in result.reason
    assert "#333" in result.reason
    assert "история прогонов" in result.reason
    assert "gh api: HTTP 502: Bad Gateway" in result.reason


def test_wasted_ai_review_unknown_when_runs_history_unavailable(monkeypatch):
    # Третий исход (issue #1096/#1109, F7): комментарий-вердикт и файлы PR
    # прочитаны, но история прогонов ai-review.yml недоступна — раньше это
    # молча читалось как "растраты нет" (ai_review_runs_after съедала форму
    # ответа), теперь check_wasted_ai_review_runs обязан вернуть unknown(),
    # не ok(), раз хоть один проверяемый PR не досмотрен до конца.
    files = pr_files()
    fp = ri.review_labels.diff_fingerprint(files)
    fake = FakeGh({
        "issues/333/comments": [ai_verdict_comment(333, fp, "2026-09-08T06:08:55Z")],
        "pulls/333/files": files,
        "actions/workflows/ai-review.yml/runs": RuntimeError("gh api: HTTP 502: Bad Gateway"),
    })
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["review:ok", "ai:ok"])
    result = ri.check_wasted_ai_review_runs(REPO, [pull])
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert result.reason


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 9: тело задачи и граф blockedBy расходятся (#710)
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма: реальный live-случай замера 2026-09-08 — #679 отвечает
# «БЛОКИРУЕТСЯ: ничем», нативный blockedBy содержит #605 (ребро проставлено
# вручную мимо текста). Не пересказ — воспроизведено дословно.
ISSUE_679_BODY = (
    "Инцидент 2026-09-06 — класс «квота пробита, конвейер не заметил», а "
    "правило репозитория требует, чтобы инцидент оставил инвариант, а не "
    "только фикс; лок-аут троттлинга выше показывает, что слепота сторожа "
    "невидима (прогоны зелёные). Критерий готовности: в "
    "scripts/orchestra/repo_invariants.py появляется проверка «последний "
    "подтверждённый замер сторожа квот свежее N×CHECK_INTERVAL_MINUTES» с "
    "эскалацией в общий канал; опирается на след реального замера, который "
    "появится с починкой троттлинга.\nБЛОКИРУЕТСЯ: ничем"
)


def test_declared_deps_mismatch_flags_stale_edge_prod_case_679():
    issues = [{"number": 679, "body": ISSUE_679_BODY, "blocked_by_open": [605]},
              {"number": 605, "body": "", "blocked_by_open": []}]
    violations = ri.check_declared_deps_mismatch(issues)
    assert violations == [{"issue": 679, "kind": "stale", "number": 605}]


def test_declared_deps_mismatch_flags_missing_edge():
    issues = [
        {"number": 500, "body": "## Чем блокируется\n#55\n", "blocked_by_open": []},
        {"number": 55, "body": "## Чем блокируется\nничем\n", "blocked_by_open": []},
    ]
    violations = ri.check_declared_deps_mismatch(issues)
    assert violations == [{"issue": 500, "kind": "missing", "number": 55}]


def test_declared_deps_mismatch_silent_when_graph_matches_declaration():
    issues = [
        {"number": 500, "body": "## Чем блокируется\n#55\n", "blocked_by_open": [55]},
        {"number": 55, "body": "## Чем блокируется\nничем\n", "blocked_by_open": []},
    ]
    assert ri.check_declared_deps_mismatch(issues) == []


def test_declared_deps_mismatch_silent_when_no_field_at_all():
    # Issue без поля вовсе (declared_blocked_by → None) — не о чем судить,
    # ручное ребро мимо старого issue без шаблона не нарушение.
    issues = [{"number": 500, "body": "Обычное тело без формы.", "blocked_by_open": [55]}]
    assert ri.check_declared_deps_mismatch(issues) == []


def test_declared_deps_mismatch_covers_reverse_blocking_field_target():
    # #500 объявляет «Что блокирует: #56», у #56 своего поля нет вовсе, но
    # граф её НЕ подтверждает — нарушение приписывается #56 (цели чужого
    # объявления), не только #500.
    issues = [
        {"number": 500, "body": "## Что блокирует\n#56\n", "blocked_by_open": []},
        {"number": 56, "body": "Обычное тело без формы.", "blocked_by_open": []},
    ]
    violations = ri.check_declared_deps_mismatch(issues)
    assert violations == [{"issue": 56, "kind": "missing", "number": 500}]


def test_declared_deps_mismatch_silent_when_native_edge_targets_non_pool_issue():
    # Находка ревью PR #711 (живой прогон ревьюера на #700→#800): текст
    # называет открытый #800, граф несёт #800 — согласовано. #800 просто НЕ
    # в пуле (нет метки task, task_deps.py фильтрует его только по state,
    # не по метке) — тот же класс, что declared_deps.py прямо называет
    # НЕ рассинхроном (ссылка на не-task issue, см. модульный докстринг).
    # Без симметричного фильтра native по open_numbers это ребро попадало бы
    # в «stale» навечно — GATING_RELEASE_CONDITION[9] («0 нарушений») было
    # бы физически недостижимо для любого пула, где такое ребро есть.
    issues = [{"number": 700, "body": "## Чем блокируется\n#800\n", "blocked_by_open": [800]}]
    assert ri.check_declared_deps_mismatch(issues) == []


def test_declared_deps_mismatch_not_in_ci_gating_but_has_release_condition():
    # Живой долг на день внедрения (#679) — гейтить нельзя (см. докстринг
    # check_declared_deps_mismatch и комментарий у CI_GATING), но газ назван
    # заранее (не повторяем #666 — «возврат держится на памяти»).
    assert 9 not in ri.CI_GATING
    assert 9 in ri.GATING_RELEASE_CONDITION


def test_declared_deps_mismatch_flags_bare_number_not_silently_nichem():
    # #711 (блокирующая): живой прод-случай #757 «### Что блокирует\n\n642»
    # без `#` — до фикса читался как [] («ничем»), инвариант ЛОЖНО кричал
    # «642: ребро есть, а поле не называет» (kind=stale), хотя поле называет
    # ровно этот номер голым видом. После фикса — согласовано, 0 нарушений.
    issues = [
        {"number": 757, "body": "### Что блокирует\n\n642", "blocked_by_open": []},
        {"number": 642, "body": "", "blocked_by_open": [757]},
    ]
    assert ri.check_declared_deps_mismatch(issues) == []


def test_declared_deps_mismatch_reports_unrecognized_kind_not_silent_nichem():
    # Поле заполнено, но текст не разбирается ни на один номер — третий вид
    # нарушения, видимый, а не молча «поле согласовано / ничем».
    issues = [
        {"number": 500, "body": "### Чем блокируется\n\nне уверен\n", "blocked_by_open": []},
    ]
    violations = ri.check_declared_deps_mismatch(issues)
    assert violations == [{"issue": 500, "kind": "unrecognized"}]


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 10 (#794): N подряд прогонов worker.yml с ОДНОЙ и той же причиной
# ══════════════════════════════════════════════════════════════════════════
#
# Прод-форма причины — дословно та, что даёт РЕАЛЬНЫЙ пинованный пакет
# @deepseek-ai/dsh-session@0.1.2-rc.1 (проверено живым вызовом adoptSessionEvent
# при разборе #794): "session event at seq 13 lacks an identified message".
# worker.yml резюмирует сессию harness-<N> при повторном ходе по той же
# задаче — холодная загрузка испорченной записи бросает эту ошибку и валит
# job, вживую девять прогонов подряд на harness-257 (2026-09-08T08:45Z —
# 2026-09-09T02:08Z).

def worker_run(run_id, created_at, updated_at=None, conclusion="failure"):
    return {
        "id": run_id,
        "conclusion": conclusion,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
    }


def worker_jobs_payload(job_id, job_name="Прогон"):
    return {"jobs": [{"id": job_id, "name": job_name, "conclusion": "failure", "steps": []}]}


def fake_log_subprocess(logs_by_job_id):
    """Мок pulse_guard.subprocess.run для last_error_log_line — она читает
    лог job'а НАПРЯМУЮ subprocess.run(gh api ...), в обход gh() (см. её
    докстринг), поэтому FakeGh/patch_gh её не перехватывают: нужен отдельный
    маршрутизатор по job_id, вычитанному из URL."""
    def run(args, **kwargs):
        joined = " ".join(args)
        match = re.search(r"actions/jobs/(\d+)/logs", joined)
        job_id = int(match.group(1)) if match else None
        line = logs_by_job_id.get(job_id)
        stdout = f"2026-09-08T08:45:00.0000000Z ##[error]{line}\n" if line else ""
        return SimpleNamespace(returncode=0, stdout=stdout)
    return run


SESSION_LACKS_ID_ERROR = "session event at seq 13 lacks an identified message"


def test_recurring_worker_failure_flags_streak_with_same_cause(monkeypatch):
    # Живой случай #794: три прогона подряд (порог
    # RECURRING_FAILURE_STREAK_THRESHOLD == pulse_guard.WORKER_FAILURE_PAUSE_AFTER),
    # самый свежий первым — одна и та же причина у всех.
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(3, "2026-09-09T00:00:00Z"),
            worker_run(2, "2026-09-08T18:00:00Z"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/3/jobs": worker_jobs_payload(103),
        "actions/runs/2/jobs": worker_jobs_payload(102),
        "actions/runs/1/jobs": worker_jobs_payload(101),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        103: SESSION_LACKS_ID_ERROR, 102: SESSION_LACKS_ID_ERROR, 101: SESSION_LACKS_ID_ERROR,
    })))
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert len(result.violations) == 1
    assert result.violations[0]["streak"] == 3
    assert result.violations[0]["error_text"] == f"##[error]{SESSION_LACKS_ID_ERROR}"
    assert result.violations[0]["since"] == "2026-09-08T08:45:00Z"
    assert result.violations[0]["until"] == "2026-09-09T00:00:00Z"


def test_recurring_worker_failure_silent_below_threshold(monkeypatch):
    # Два прогона подряд — ниже порога, инвариант молчит (это разница между
    # "уже пора паузу" и "серия только начинается", тот же порог, что и у
    # предохранителя диспатча).
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(2, "2026-09-08T18:00:00Z"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/2/jobs": worker_jobs_payload(102),
        "actions/runs/1/jobs": worker_jobs_payload(101),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        102: SESSION_LACKS_ID_ERROR, 101: SESSION_LACKS_ID_ERROR,
    })))
    assert ri.check_recurring_worker_failure(REPO) == ri.check_result.ok()


def test_recurring_worker_failure_silent_when_cause_changes_mid_streak(monkeypatch):
    # Три красных прогона подряд, но причина СМЕНИЛАСЬ на третьем (по времени)
    # — это не одна и та же серия, инвариант не обязан путать «часто красный»
    # с «застрял на одном и том же».
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(3, "2026-09-09T00:00:00Z"),
            worker_run(2, "2026-09-08T18:00:00Z"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/3/jobs": worker_jobs_payload(103),
        "actions/runs/2/jobs": worker_jobs_payload(102),
        "actions/runs/1/jobs": worker_jobs_payload(101),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        103: SESSION_LACKS_ID_ERROR,
        102: SESSION_LACKS_ID_ERROR,
        101: "No such file or directory",  # другая, старая причина — обрывает серию
    })))
    assert ri.check_recurring_worker_failure(REPO) == ri.check_result.ok()


def test_recurring_worker_failure_silent_when_latest_run_is_green(monkeypatch):
    # Дешёвый путь холостого хода: самый свежий прогон — success, серия
    # обрывается СРАЗУ, ни один job/лог не запрашивается (см. докстринг —
    # это и есть цена инварианта на здоровом репозитории).
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(4, "2026-09-09T03:00:00Z", conclusion="success"),
            worker_run(3, "2026-09-09T00:00:00Z"),
            worker_run(2, "2026-09-08T18:00:00Z"),
        ]},
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_recurring_worker_failure(REPO) == ri.check_result.ok()
    assert not any("actions/runs/3/jobs" in call or "actions/runs/2/jobs" in call for call in fake.calls)


def test_recurring_worker_failure_unknown_when_runs_history_unavailable(monkeypatch):
    # Третий исход (issue #1096/#1109): история прогонов worker.yml
    # недоступна целиком (транспорт/квота) — раньше молча читалась как
    # "серии нет" (`except RuntimeError: return []`), теперь unknown().
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": RuntimeError(
            "gh api: HTTP 503: Service Unavailable"),
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert result.reason


def test_recurring_worker_failure_unknown_on_malformed_run_history_shape(monkeypatch):
    # Находка ai-review PR #1140 (находка 2, F3): докстринг обещал unknown()
    # на форму ответа не по контракту, а код шёл через pulse_guard.recent_runs,
    # который молча схлопывает такой ответ (вторичный рейт-лимит — dict без
    # ключа workflow_runs, живой класс #120A) в [] — инвариант отвечал тем же
    # 💚, что и здоровое состояние. Теперь форма разбирает runs_of() — ответ
    # не по контракту даёт unknown(), а не ok().
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"message": "secondary rate limit"},
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "неожиданной" in result.reason
    # Ключ есть, но значение не список — та же слепота, та же граница.
    fake2 = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": None},
    })
    patch_gh(monkeypatch, fake2)
    result2 = ri.check_recurring_worker_failure(REPO)
    assert result2.status == ri.check_result.STATUS_UNKNOWN
    assert "не список" in result2.reason


def test_recurring_worker_failure_unknown_when_jobs_lookup_fails_mid_scan(monkeypatch):
    # Находка ai-review PR #1140 (находка 1): список прогонов получен, но
    # job'ы упавшего прогона недоступны (failing_jobs кидает RuntimeError —
    # по запросу на прогон, типичный способ поймать вторичную квоту посреди
    # скана при живом списке). Раньше `except RuntimeError: break` с пустым
    # streak_runs возвращал ok() — то же 💚 при «классифицировать не удалось
    # вовсе». Теперь — unknown() с фактом: какой прогон, какая ошибка.
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(2, "2026-09-08T18:00:00Z"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/2/jobs": RuntimeError("gh api: HTTP 403: secondary rate limit"),
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "не удалось досмотреть" in result.reason
    assert "прогона 2" in result.reason
    assert "gh api: HTTP 403: secondary rate limit" in result.reason
    # Сколько прогонов осталось неклассифицированным — тоже факт, не гипотеза.
    assert "0 из 2" in result.reason


def test_recurring_worker_failure_violation_survives_jobs_lookup_failure_deeper(monkeypatch):
    # Дополнение к находке 1: накопленная серия ≥ порога ПОБЕЖДАЕТ
    # неопределённость по недосмотренному хвосту — доказанное нарушение
    # не прячется за чужим «не знаю» (тот же принцип, что у инвариантов 8/14).
    # Здесь серия из трёх классифицирована, четвёртый (старейший) — обрыв
    # транспортом: всё равно violation().
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(4, "2026-09-09T06:00:00Z"),
            worker_run(3, "2026-09-09T00:00:00Z"),
            worker_run(2, "2026-09-08T18:00:00Z"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/4/jobs": worker_jobs_payload(104),
        "actions/runs/3/jobs": worker_jobs_payload(103),
        "actions/runs/2/jobs": worker_jobs_payload(102),
        "actions/runs/1/jobs": RuntimeError("gh api: HTTP 502: Bad Gateway"),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        104: SESSION_LACKS_ID_ERROR, 103: SESSION_LACKS_ID_ERROR, 102: SESSION_LACKS_ID_ERROR,
    })))
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert result.violations[0]["streak"] == 3


def test_recurring_worker_failure_jobs_lookup_failure_after_success_is_still_ok(monkeypatch):
    # Обрыв скана по ДАННЫМ (success обрывает серию безусловно) остаётся
    # честным ok(): недосмотренного хвоста за success нет по определению
    # серии — это факт о прогоне, не транспортная деградация; RuntimeError
    # на job'ах СТАРШЕ success вообще не должен быть запрошен.
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(2, "2026-09-09T00:00:00Z", conclusion="success"),
            worker_run(1, "2026-09-08T08:45:00Z"),
        ]},
        "actions/runs/1/jobs": RuntimeError("gh api: HTTP 403: secondary rate limit"),
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_recurring_worker_failure(REPO) == ri.check_result.ok()
    assert not any("actions/runs/1/jobs" in call for call in fake.calls)


# ── Живая серия 2026-09-13 (issue найденный владельцем): голова списка ──────
# ещё выполняется — прогон-снимок реального `gh api
# repos/mytab0r/edge-harness/actions/workflows/worker.yml/runs`. IDs, времена
# и текст ошибок — дословно с живого репозитория (сверено `gh api
# repos/mytab0r/edge-harness/actions/jobs/<id>/logs`), не пересказ:
#
#   34746091297  in_progress (conclusion=None) — воркер ещё выполняется
#   34739313568  failure — цепочка исчерпана целиком таймаутами (ДРУГОЙ,
#                отличный от следующих трёх, отпечаток: в этом прогоне
#                Ollama-2 упёрся в таймаут, а не в max_tokens)
#   34735752165  failure — Ollama-2 rc=1, max_tokens (131072) exceeds ...
#                (65536) for model nemotron-3-ultra — класс НЕ переключаемый
#   34732869856  failure — тот же класс max_tokens (другой ref-UUID)
#   34730173870  failure — тот же класс max_tokens (другой ref-UUID)
#   34728868781  success
#
# Диагностика владельца («четыре подряд одного класса») не подтвердилась
# буквально: реальный fingerprint (workflow+job+нормализованная строка)
# отличает «цепочка исчерпана таймаутами» от «Ollama-2 max_tokens» — это
# ДЕЙСТВИТЕЛЬНО разные причины, а не шум нормализации (ref-UUID и цифры
# схлопываются, но сам текст разный). Настоящая серия одной причины — три
# прогона (34735752165/34732869856/34730173870), и она была НЕВИДИМА
# инварианту, пока голова списка была in_progress (см. тест ниже,
# использующий более раннюю живую точку среза — до завершения 34739313568).

OLLAMA2_MAX_TOKENS_ERROR = (
    "цепочка провайдеров: Ollama-2 — rc=1, класс НЕ переключаемый (stderr: "
    "dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum "
    "output tokens (65536) for model nemotron-3-ultra (ref: {ref}) ), "
    "дальше по цепочке не иду (следующие провайдеры не тронуты)"
)
CHAIN_EXHAUSTED_TIMEOUT_ERROR = (
    "цепочка провайдеров исчерпана целиком (GLM, ZAI, OpenRouter-2, "
    "Ollama-2, Ollama-3, Ollama-1, NVIDIA-NIM-1, OpenRouter-1, NVIDIA-NIM-2)"
)


def test_recurring_worker_failure_pending_head_does_not_hide_streak_behind_it(monkeypatch):
    # Живая точка среза (примерно 2026-09-13T06:00Z, до того как 34739313568
    # завершился): голова списка — ещё идущий прогон 34739313568, а сразу за
    # ним три ЗАВЕРШЁННЫХ прогона одной и той же причины (max_tokens).
    # Старый код обрывал скан на первом же None и возвращал [] — серия ниже
    # была НЕВИДИМА. Это и есть мутация, которую полагается доказать: снять
    # правку (заменить `continue` на `break` для conclusion is None) красит
    # этот тест.
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(34739313568, "2026-09-13T05:02:32Z", "2026-09-13T06:00:00Z", conclusion=None),
            worker_run(34735752165, "2026-09-13T03:32:44Z", "2026-09-13T04:26:35Z"),
            worker_run(34732869856, "2026-09-13T02:22:03Z", "2026-09-13T03:16:04Z"),
            worker_run(34730173870, "2026-09-13T01:17:46Z", "2026-09-13T02:11:55Z"),
            worker_run(34728868781, "2026-09-13T00:47:21Z", "2026-09-13T01:06:21Z", conclusion="success"),
        ]},
        "actions/runs/34735752165/jobs": worker_jobs_payload(103666759504),
        "actions/runs/34732869856/jobs": worker_jobs_payload(103658813654),
        "actions/runs/34730173870/jobs": worker_jobs_payload(103651384804),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        103666759504: OLLAMA2_MAX_TOKENS_ERROR.format(ref="f5feae9d-e13e-4d1a-90f0-c315e4389e21"),
        103658813654: OLLAMA2_MAX_TOKENS_ERROR.format(ref="e13e4d1a-90f0-c315-e438-9e21f5feae9d"),
        103651384804: OLLAMA2_MAX_TOKENS_ERROR.format(ref="bea56f62-51af-47b6-b57d-9b03d8871690"),
    })))
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert len(result.violations) == 1
    assert result.violations[0]["streak"] == 3
    assert result.violations[0]["since"] == "2026-09-13T01:17:46Z"
    assert result.violations[0]["until"] == "2026-09-13T04:26:35Z"
    assert result.violations[0]["latest_run_url"] == f"https://github.com/{REPO}/actions/runs/34735752165"
    assert result.violations[0]["pending_seen"] is True


def test_recurring_worker_failure_does_not_bridge_across_different_cause(monkeypatch):
    # Текущая (2026-09-13T07:47Z) живая точка среза: голова — ещё идущий
    # 34746091297, за ним завершённый провал 34739313568 с ДРУГИМ отпечатком
    # (см. блок-комментарий выше), а уже за ним — настоящая серия max_tokens.
    # Пропуск None не обязан «дотягиваться» через несовпадающий отпечаток:
    # серия обрывается на первом же расхождении причины, как и раньше —
    # инвариант молчит (это НЕ серия одной причины длиной 4, а 1 + разрыв + 3).
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(34746091297, "2026-09-13T07:47:31Z", "2026-09-13T07:47:35Z", conclusion=None),
            worker_run(34739313568, "2026-09-13T05:02:32Z", "2026-09-13T07:45:23Z"),
            worker_run(34735752165, "2026-09-13T03:32:44Z", "2026-09-13T04:26:35Z"),
            worker_run(34732869856, "2026-09-13T02:22:03Z", "2026-09-13T03:16:04Z"),
            worker_run(34730173870, "2026-09-13T01:17:46Z", "2026-09-13T02:11:55Z"),
            worker_run(34728868781, "2026-09-13T00:47:21Z", "2026-09-13T01:06:21Z", conclusion="success"),
        ]},
        "actions/runs/34739313568/jobs": worker_jobs_payload(103676181082),
        "actions/runs/34735752165/jobs": worker_jobs_payload(103666759504),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        103676181082: CHAIN_EXHAUSTED_TIMEOUT_ERROR,
        103666759504: OLLAMA2_MAX_TOKENS_ERROR.format(ref="f5feae9d-e13e-4d1a-90f0-c315e4389e21"),
    })))
    assert ri.check_recurring_worker_failure(REPO) == ri.check_result.ok()
    # Дотягиваться до третьего прогона незачем — расхождение отпечатка уже
    # обнаружено на втором; проверяем, что скан честно останавливается, а не
    # молча досматривает весь список без дела.
    assert not any("actions/runs/34732869856/jobs" in call for call in fake.calls)


def test_recurring_worker_failure_skips_multiple_pending_runs_mid_streak(monkeypatch):
    # Синтетический (не прод-снятый) защитный случай: несколько незавершённых
    # прогонов подряд (или вперемешку) внутри окна — теоретически возможно при
    # ручном re-run старого прогона (created_at не меняется, conclusion снова
    # None). Ни один не обрывает скан, ни один не входит в streak_runs.
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(6, "2026-09-13T09:00:00Z", conclusion=None),
            worker_run(5, "2026-09-13T08:00:00Z", conclusion=None),
            worker_run(4, "2026-09-13T07:00:00Z"),
            worker_run(3, "2026-09-13T06:00:00Z"),
            worker_run(2, "2026-09-13T05:00:00Z"),
        ]},
        "actions/runs/4/jobs": worker_jobs_payload(204),
        "actions/runs/3/jobs": worker_jobs_payload(203),
        "actions/runs/2/jobs": worker_jobs_payload(202),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri.pulse_guard, "subprocess", SimpleNamespace(run=fake_log_subprocess({
        204: SESSION_LACKS_ID_ERROR, 203: SESSION_LACKS_ID_ERROR, 202: SESSION_LACKS_ID_ERROR,
    })))
    result = ri.check_recurring_worker_failure(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert len(result.violations) == 1
    assert result.violations[0]["streak"] == 3
    assert result.violations[0]["pending_seen"] is True


# ══════════════════════════════════════════════════════════════════════════
# Холостой ход: здоровый снимок — 0 нарушений, 0 мутирующих вызовов
# ══════════════════════════════════════════════════════════════════════════


def test_idle_guard_healthy_snapshot_no_violations_no_mutating_calls(tmp_path, monkeypatch):
    """Мутация-доказательство холостого хода: здоровое состояние во ВСЕХ
    инвариантах разом не должно вызвать ни одного -X POST/PUT/DELETE."""
    healthy_tasks = [task_issue(1, "здоровая задача", "нет ссылок", assignees=("someone",))]
    healthy_open_pulls = [open_pr(50, "#1", labels=["review:ok", "ai:ok"])]
    healthy_merged = []  # нет слитых PR вовсе — задача 1 занята, не free

    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": healthy_tasks,
        "pulls?state=closed": [],
        "pulls?state=open": healthy_open_pulls,
        # Инвариант 8 (#740): PR несёт ai:ok — читает последний AI-комментарий
        # PR, чтобы сверить отпечаток. Пустой список — вердикта-комментария
        # ещё нет (например, статус проведён вторым каналом, #345), инвариант
        # молчит по построению (comment is None), не запрашивая ни файлы, ни
        # прогоны workflow.
        "issues/50/comments": [],
        # Инвариант 9 (#710): пул с телами через GraphQL — здоровое поле «ничем»
        # в обе стороны, нативных рёбер нет, расхождения тоже нет.
        "graphql": graphql_pool_page([
            graphql_issue_node(1, issue_body="## Чем блокируется\nничем\n\n## Что блокирует\nничем\n"),
        ]),
        # Инвариант 10 (#794): самый свежий прогон worker.yml зелёный — серия
        # обрывается на первом же прогоне, ни один job/лог не запрашивается.
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {
            "workflow_runs": [{"conclusion": "success", "created_at": "2026-09-03T11:00:00Z",
                                "updated_at": "2026-09-03T11:00:00Z", "html_url": "https://x/1"}],
        },
        # Инвариант 12 (#882): свежий снимок здоровья того же дня — переопределяет
        # реалистичный дефолт FakeGh._DEFAULT_ROUTES (404), чтобы этот тест
        # остался про ДРУГИЕ инварианты, а не про историю снимков.
        "contents/docs/research/data/pipeline-health.jsonl":
            health_snapshot_contents_response([{"date": "2026-09-03", "merge_throughput": 1}]),
        # Инвариант 24 (#1250): свежая запись уборщика с числами, которые не
        # нарушают ни половину критерия готовности (устаревших копий гвардий
        # 0 ≤ открытых PR 1), ни симптом инцидента (removed>0) — тем же приёмом,
        # что снимок здоровья выше, чтобы тест остался про другие инварианты.
        "contents/docs/research/data/worktree-cleanup.jsonl":
            worktree_snapshot_contents_response([{
                "ts": "2026-09-03T11:30:00+00:00", "mode": "apply",
                "total": 5, "removed": 2, "kept": 3,
                "guard_copies": 5, "stale_guard_copies": 0,
                "stuck_old_total": 0, "stuck_old_unpushed": 0,
                "stuck_old_unknown_work": 0, "stuck_old_unknown_pr": 0,
                "retention_hours": 1.0,
            }]),
        # Инвариант 12 (#876): полнотекстовый поиск не находит ни одного
        # комментария с противоречивой фразой — здоровое состояние.
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", tmp_path / "changes-empty")
    # Инвариант 7 сканирует документы репозитория — подменяем корень, чтобы
    # юнит-тест оставался герметичным и не зависел от живого дерева; живое
    # дерево проверяет сам инвариант в repo-ci/orchestra.
    monkeypatch.setattr(ri, "REPO_ROOT", tmp_path)
    write_md(tmp_path, "README.md", "# здоровый снимок\n")

    now = utc(2026, 9, 3, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)

    assert all(not v for v in findings.values()), f"здоровый снимок не должен давать нарушений: {findings}"
    assert fake.mutating_calls() == [], "чтение состояния не должно ничего менять"
    assert all("🚨" not in line for line in lines)

    # --orchestra тоже не должен мутировать на здоровом снимке: без нарушений
    # run_escalations не обязан звать escalate() (никаких POST в WATCHDOG_ISSUE
    # и Telegram).
    escalation_lines = ri.run_escalations("mytab0r/edge-harness", findings)
    assert escalation_lines == []
    assert fake.mutating_calls() == []


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 11: манифест использования LLM-провайдеров (#823)
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма: та же двухуровневая схема {chains, usage}, что реально живёт в
# config/provider-usage.json (openspec/changes/llm-provider-usage-manifest,
# design.md «Схема манифеста») — не пересказ, буквальная форма.


def write_manifest(tmp_path, chains, usage):
    path = tmp_path / "provider-usage.json"
    import json
    path.write_text(json.dumps({"chains": chains, "usage": usage}), encoding="utf-8")
    return path


def test_provider_usage_manifest_healthy_snapshot(tmp_path):
    # Модель/URL — синтетическая фикстура (класс #153: гвардия
    # provider-default.guard.sh ловит стейл-литералы прежнего дефолта в
    # scripts/** буквальным текстом) — тест проверяет ФОРМУ манифеста, не
    # конкретного провайдера, реальные значения не нужны.
    path = write_manifest(
        tmp_path,
        chains={"default-chain": [{"name": "TEST-PROVIDER", "base_url": "https://provider.example/v1",
                                    "model": "test-model-x", "secret_env": "TEST_API_KEY",
                                    "max_output_tokens": 131072}]},
        usage={"ai-review": "default-chain", "worker": "default-chain", "hands": "default-chain"},
    )
    assert ri.check_provider_usage_manifest(path) == []


def test_provider_usage_manifest_missing_consumer_mutation_guard(tmp_path):
    """Мутация (класс #727 -> #797): потребитель без записи в .usage — красный."""
    path = write_manifest(
        tmp_path,
        chains={"default-chain": [{"name": "GLM"}]},
        usage={"ai-review": "default-chain", "worker": "default-chain"},  # hands забыт
    )
    violations = ri.check_provider_usage_manifest(path)
    assert violations == [{"kind": "missing", "consumer": "hands"}]


def test_provider_usage_manifest_dangling_chain_mutation_guard(tmp_path):
    """Мутация: .usage ссылается на цепочку, которой нет в .chains — красный."""
    path = write_manifest(
        tmp_path,
        chains={"default-chain": [{"name": "GLM"}]},
        usage={"ai-review": "default-chain", "worker": "ghost-chain", "hands": "default-chain"},
    )
    violations = ri.check_provider_usage_manifest(path)
    assert violations == [{"kind": "dangling", "consumer": "worker", "chain_name": "ghost-chain"}]


def test_provider_usage_manifest_empty_chain_is_dangling(tmp_path):
    path = write_manifest(
        tmp_path,
        chains={"default-chain": [{"name": "GLM"}], "empty-chain": []},
        usage={"ai-review": "default-chain", "worker": "empty-chain", "hands": "default-chain"},
    )
    violations = ri.check_provider_usage_manifest(path)
    assert violations == [{"kind": "dangling", "consumer": "worker", "chain_name": "empty-chain"}]


def test_provider_usage_manifest_missing_file_is_transitional_not_a_violation(tmp_path):
    """Манифеста нет вовсе — переходный период (design.md «Потребители»), не
    сам по себе провал инварианта; вызывающий (build_report) решает, как это
    показать — здесь проверяется только форма ответа check_*."""
    path = tmp_path / "does-not-exist.json"
    violations = ri.check_provider_usage_manifest(path)
    assert violations == [{"kind": "no_manifest", "path": str(path)}]


def test_provider_usage_manifest_in_ci_gating_with_gas():
    assert 11 in ri.CI_GATING
    assert 11 in ri.GATING_RELEASE_CONDITION


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 12 (#882): механизм снимков здоровья конвейера заявляет успех,
# а снимков за N суток нет
# ══════════════════════════════════════════════════════════════════════════
#
# Живой инцидент: `data/pipeline-health` не существовала вовсе, шаг orchestra.yml
# «Само-аудит здоровья конвейера» рапортовал conclusion=success ~96 раз в сутки.
# `check_pipeline_health_snapshot_stale` — чистая функция, `now` инъекцией (не
# `datetime.now()`, класс «тесты-бомбы» здесь недопустим).


def test_pipeline_health_snapshot_stale_empty_history_is_never_written():
    now = utc(2026, 9, 10, 12, 0)
    violations = ri.check_pipeline_health_snapshot_stale([], now)
    assert violations == [{"last_date": None, "age_days": None}]


def test_pipeline_health_snapshot_stale_fresh_snapshot_is_silent():
    history = [{"date": "2026-09-09", "merge_throughput": 1}]
    now = utc(2026, 9, 10, 12, 0)
    assert ri.check_pipeline_health_snapshot_stale(history, now) == []


def test_pipeline_health_snapshot_stale_boundary_at_threshold_is_silent():
    """Ровно на пороге (age_days == max_age_days) — ещё не нарушение, тот же
    приём «строго после порога», что у остальных age-based инвариантов этого
    файла (UNHEALTHY_PR_AFTER_MINUTES: `age <= порог` — здорово)."""
    history = [{"date": "2026-09-08", "merge_throughput": 1}]
    now = utc(2026, 9, 10, 12, 0)  # age_days == 2 == PIPELINE_HEALTH_STALE_AFTER_DAYS
    assert ri.check_pipeline_health_snapshot_stale(history, now) == []


def test_pipeline_health_snapshot_stale_old_snapshot_flags_with_age():
    history = [{"date": "2026-09-05", "merge_throughput": 1}]
    now = utc(2026, 9, 10, 12, 0)
    violations = ri.check_pipeline_health_snapshot_stale(history, now)
    assert violations == [{"last_date": "2026-09-05", "age_days": 5}]


def test_pipeline_health_snapshot_stale_mutation_guard():
    """Мутация-доказательство: без порога любая история старше 0 дней уже
    нарушение — снимок «вчера» ложно краснел бы каждый день. Тест ловит
    регресс «порог убрали/занулили»."""
    history = [{"date": "2026-09-09", "merge_throughput": 1}]
    now_next_day = utc(2026, 9, 10, 12, 0)  # age_days == 1, в пределах порога 2
    assert ri.check_pipeline_health_snapshot_stale(history, now_next_day) == []


def test_pipeline_health_snapshot_stale_not_in_ci_gating():
    # Наблюдательный по построению (см. докстринг инварианта 12 в
    # repo_invariants.py) — сразу после включения фикса история ещё пуста,
    # гейтить с ходу означало бы красить `test` за собственный переходный
    # период (тот же класс «тормоз без газа», от которого уже отказались
    # для 1/4/5/9).
    assert 12 not in ri.CI_GATING


def test_pipeline_health_snapshot_stale_is_escalating():
    assert 12 in ri.ESCALATING_INVARIANTS


def test_fetch_pipeline_health_history_uses_mockable_transport(monkeypatch):
    """Регрессия-класс (docstring patch_gh выше, живой случай issue #120):
    fetch_pipeline_health_history обязана идти через `gh()` этого модуля
    (мокаемый в тестах и патчащий `ri.gh`/`ri.pulse_guard.gh` разом), а не
    через собственный subprocess.run — иначе тест build_report реально ушёл
    бы в сеть."""
    fake = FakeGh({
        "contents/docs/research/data/pipeline-health.jsonl":
            health_snapshot_contents_response([{"date": "2026-09-01", "merge_throughput": 3}]),
    })
    patch_gh(monkeypatch, fake)
    history = ri.fetch_pipeline_health_history("mytab0r/edge-harness")
    assert history == [{"date": "2026-09-01", "merge_throughput": 3}]


def test_fetch_pipeline_health_history_404_is_empty_history_not_error(monkeypatch):
    fake = FakeGh({})  # только дефолтный маршрут — 404 (см. FakeGh._DEFAULT_ROUTES)
    patch_gh(monkeypatch, fake)
    assert ri.fetch_pipeline_health_history("mytab0r/edge-harness") == []


def test_fetch_pipeline_health_history_other_error_is_loud(monkeypatch):
    fake = FakeGh({
        "contents/docs/research/data/pipeline-health.jsonl": RuntimeError(
            "gh api repos/o/r/contents/...: HTTP 403: rate limit"),
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="403"):
        ri.fetch_pipeline_health_history("mytab0r/edge-harness")


def test_build_report_flags_never_written_snapshot(monkeypatch):
    """Живой инцидент #882 воспроизведён целиком через build_report: история
    снимков здоровья пуста (дефолтный маршрут FakeGh — 404, тот же факт, что
    ветки data/pipeline-health на живом репозитории не существовало)."""
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 10, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[12] == [{"last_date": None, "age_days": None}]
    assert any("🚨" in line and "[12]" in line and "НИ РАЗУ" in line for line in lines)


def test_run_escalations_pipeline_health_dedupes_by_last_date(monkeypatch):
    """Эскалация «раз на состояние» (тот же приём, что у 1/3): тот же
    last_date не эскалируется второй раз подряд, смена last_date — новая
    эскалация (штатный повтор при устаревании ещё на день не должен спамить
    Telegram на каждом 15-минутном пульсе)."""
    calls = []

    def fake_issue_marker_times(repo, issue, marker):
        return [utc(2026, 9, 10, 0, 0)] if "2026-09-05" in marker else []

    def fake_escalate(repo, issue, text):
        calls.append(text)
        return "отправлено"

    monkeypatch.setattr(ri, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(ri, "escalate", fake_escalate)

    already_escalated = {12: [{"last_date": "2026-09-05", "age_days": 5}]}
    assert ri.run_escalations("mytab0r/edge-harness", already_escalated) == []
    assert calls == []

    new_state = {12: [{"last_date": "2026-09-06", "age_days": 4}]}
    lines = ri.run_escalations("mytab0r/edge-harness", new_state)
    assert len(calls) == 1 and "2026-09-06" in calls[0]
    assert any("инвариант 12" in line for line in lines)


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 13: фантомная пауза конвейера (issue #899)
# ══════════════════════════════════════════════════════════════════════════


def pause_marker_comment(when: str) -> dict:
    """Честный маркер серии: `performed_via_github_app` НЕ пуст (#1242) —
    писатель семейства один (`pulse_guard.gh()` под `github.token`, #1074),
    поэтому инвариант 13 с require_job_token=True обязан его видеть."""
    return {"created_at": when, "body": ri.pulse_guard.PAUSE_MARKER,
            "performed_via_github_app": {"id": 15368, "slug": "github-actions"}}


def probe_marker_comment(when: str, attempt: int) -> dict:
    return {"created_at": when,
            "body": ri.pulse_guard.probe_alert_text(
                attempt, ri.pulse_guard.probe_backoff_minutes(attempt), None, "err"),
            "performed_via_github_app": {"id": 15368, "slug": "github-actions"}}


def fake_marker_comment(when: str, body: str) -> dict:
    """Прод-форма ПОДДЕЛЬНОГО маркера: буквальный envelope живого инцидента
    #1242 (issuecomment-5665003751 — та же фикстура, что у test_pulse_guard,
    один источник), тело подставляется под сценарий. `user.login == "mytab0r"`,
    `performed_via_github_app is None` — не токен job'а."""
    comment = json.loads((_DIR / "fixtures_issue120_fake_wip_close_marker.json")
                         .read_text(encoding="utf-8"))
    comment["created_at"] = when
    comment["body"] = body
    return comment


def test_phantom_pause_flags_active_marker_with_zero_real_failures(monkeypatch):
    """Живой класс инцидента 2026-09-10 (issue #899): маркер серии активен
    (новее анкера), самый свежий прогон ЗАВЕРШЁН (conclusion='skipped' —
    не провал и не success, count_consecutive_failures останавливается на
    нём и вернёт 0), а держать паузу нечем."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(5, "2026-09-10T11:55:00Z", conclusion="skipped"),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [pause_marker_comment("2026-09-10T11:45:00Z")],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    result = ri.check_conveyor_gate_phantom_pause(REPO, now)
    assert result.status == ri.check_result.STATUS_VIOLATION
    violations = result.violations
    assert len(violations) == 1
    assert violations[0]["failures"] == 0
    assert violations[0]["threshold"] == ri.pulse_guard.WORKER_FAILURE_PAUSE_AFTER
    assert violations[0]["last_marker_at"] == "2026-09-10T11:45:00+00:00"
    assert violations[0]["latest_run_url"] == f"https://github.com/{REPO}/actions/runs/5"


def test_phantom_pause_silent_when_no_marker(monkeypatch):
    """Здоровое состояние: серии нет вовсе (нет маркеров) — паузе неоткуда
    взяться."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(1, "2026-09-10T11:00:00Z", conclusion="success"),
        ]},
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    assert ri.check_conveyor_gate_phantom_pause(REPO, now) == ri.check_result.ok()


def test_phantom_pause_silent_when_real_failures_meet_threshold(monkeypatch):
    """Здоровое состояние: реальных подряд-провалов ровно порог (или больше)
    — пауза оправдана, не фантомная."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(3, "2026-09-10T11:50:00Z"),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [pause_marker_comment("2026-09-10T11:45:00Z")],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    assert ri.check_conveyor_gate_phantom_pause(REPO, now) == ri.check_result.ok()


def test_phantom_pause_silent_when_head_run_still_in_progress(monkeypatch):
    """Здоровое состояние (issue #899, п.2): голова списка ещё выполняется
    (conclusion=None), но моложе scheduler.WORKER_STALL_MINUTES — законное
    объяснение неопределённости, не фантом (issue #1096: старше порога это
    уже НЕ ok(), см. следующий тест)."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(4, "2026-09-10T11:55:00Z", conclusion=None),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [
            pause_marker_comment("2026-09-10T11:45:00Z"),
            probe_marker_comment("2026-09-10T11:55:00Z", 1),
        ],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)  # +5 мин от создания головы — далеко внутри WORKER_STALL_MINUTES
    assert ri.check_conveyor_gate_phantom_pause(REPO, now) == ri.check_result.ok()


def test_phantom_pause_unknown_when_head_run_stalled_past_threshold(monkeypatch):
    """issue #1096, F1 (живой замер: голова списка не завершена ≈63%
    календарного времени — «прогон ещё идёт» это ОБЫЧНОЕ состояние, а не
    редкий край). Та же фикстура, что у предыдущего теста, но `now` дальше
    scheduler.WORKER_STALL_MINUTES (295 мин) от создания головы — «прогон
    ещё идёт» перестаёт быть правдоподобным объяснением, но подряд-провалы
    после якоря пересчитать всё равно нельзя, пока эта голова висит:
    check_result.unknown(), НЕ check_result.ok() (раньше — тихий [])."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(4, "2026-09-10T11:55:00Z", conclusion=None),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [
            pause_marker_comment("2026-09-10T11:45:00Z"),
            probe_marker_comment("2026-09-10T11:55:00Z", 1),
        ],
    })
    patch_gh(monkeypatch, fake)
    assert ri.scheduler.WORKER_STALL_MINUTES == 295
    now = utc(2026, 9, 10, 17, 0)  # +305 мин от создания головы — за порогом
    result = ri.check_conveyor_gate_phantom_pause(REPO, now)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "305" in result.reason
    assert "WORKER_STALL_MINUTES" in result.reason


def test_phantom_pause_completion_time_anchor_clears_stale_marker(monkeypatch):
    """Мутационное доказательство — тот же сценарий, что чинит conveyor_gate
    (issue #899): успешный прогон стартовал T0, маркер поставлен посреди
    него (T0+80мин), завершился успехом в T0+180мин. Анкер серии строится по
    updated_at успеха (см. pulse_guard.series_anchor) — маркер посреди
    прогона старше анкера и вычёркивается, инвариант молчит."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(9, "2026-09-10T15:49:00Z", updated_at="2026-09-10T18:49:00Z",
                       conclusion="success"),
            worker_run(8, "2026-09-10T14:00:00Z"),
            worker_run(7, "2026-09-10T13:00:00Z"),
            worker_run(6, "2026-09-10T12:00:00Z"),
        ]},
        "issues/120/comments": [probe_marker_comment("2026-09-10T17:09:00Z", 4)],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 19, 0)
    assert ri.check_conveyor_gate_phantom_pause(REPO, now) == ri.check_result.ok()


def test_phantom_pause_unknown_on_network_failure(monkeypatch):
    """issue #1096, F1: сеть недоступна — раньше тихий [] (то же самое 💚,
    что и доказанное «нарушений нет»); теперь check_result.unknown() с
    названной причиной (AGENTS.md «Алерт не гадает»)."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": RuntimeError("gh api: 502"),
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    result = ri.check_conveyor_gate_phantom_pause(REPO, now)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "gh api: 502" in result.reason


def test_phantom_pause_unknown_on_malformed_run_history_shape(monkeypatch):
    """Находка ai-review PR #1110 (F3, живой класс #120A): ответ на список
    прогонов не той формы (dict вторичного рейт-лимита без ключа
    workflow_runs) раньше молча схлопывался в `[]` внутри `pulse_guard.
    recent_runs` — check_conveyor_gate_phantom_pause видела `not runs` и
    отвечала ok(), неотличимо от «прогонов правда нет». Теперь форма
    проверяется до этого — unknown()."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"message": "secondary rate limit"},
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    result = ri.check_conveyor_gate_phantom_pause(REPO, now)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "неожиданной" in result.reason


def test_phantom_pause_ignores_fake_pause_marker_without_job_token(monkeypatch):
    """#1242, находка ai-review этого PR: инвариант 13 — независимый пересчёт
    решения conveyor_gate, обязан читать маркеры с тем же доверием, что и
    сам гейт (тот с этого же PR требует require_job_token=True). Без фильтра
    поддельный PAUSE (живой envelope инцидента #1242, performed_via_github_app
    =None) при здоровой серии давал violation «фантомная пауза»
    ({'failures': 0, ...}) — отчёт о паузе, которой для гейта (allowed=True)
    не существует: гейт и инвариант расходились по построению. С фильтром —
    здоровое состояние, согласованное с гейтом. Сама подделка при этом не
    остаётся невидимой: её детектирует инвариант 18 (всё семейство
    `[статус конвейера:`). Мутация: снять require_job_token из вызова в
    check_conveyor_gate_phantom_pause — тест краснеет (нарушение вернётся)."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(5, "2026-09-10T11:55:00Z", conclusion="skipped"),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [
            fake_marker_comment("2026-09-10T11:45:00Z", ri.pulse_guard.PAUSE_MARKER)],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    assert ri.check_conveyor_gate_phantom_pause(REPO, now) == ri.check_result.ok(), (
        "поддельный PAUSE (не токен job'а) не должен порождать фантомную "
        "паузу в независимом пересчёте — гейт с теми же данными разрешает "
        "диспатч")


def test_phantom_pause_fake_resume_cannot_cancel_honest_pause(monkeypatch):
    """#1242, симметричный случай: поддельный RESUME (не токен job'а) НЕ
    должен становиться якорем пересчёта и молча обнулять проверку честной
    паузы. Без фильтра resume_at брался с подделки (11:50 новее честного
    PAUSE 11:45), маркеры после якоря пусты — «серии нет», инвариант молчал,
    хотя пауза стоит и реальных провалов нет (ровно фантом). С фильтром —
    нарушение видно. Мутация: снять require_job_token — тест краснеет
    (молчаливое ok() вернётся)."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(5, "2026-09-10T11:55:00Z", conclusion="skipped"),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [
            pause_marker_comment("2026-09-10T11:45:00Z"),
            fake_marker_comment("2026-09-10T11:50:00Z",
                                f"✅ edge-harness: {ri.pulse_guard.RESUME_MARKER} #999]"),
        ],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 10, 12, 0)
    result = ri.check_conveyor_gate_phantom_pause(REPO, now)
    assert result.status == ri.check_result.STATUS_VIOLATION, (
        "поддельный RESUME не должен отменять честную паузу в пересчёте — "
        "фантом (пауза активна, реальных провалов 0) обязан остаться виден")
    assert result.violations[0]["failures"] == 0


def test_phantom_pause_not_in_ci_gating():
    """Долг на живом репозитории ещё не измерен (тот же порядок, что у
    1/5/9/10/12) — наблюдательный, не гейтящий."""
    assert 13 not in ri.CI_GATING


def test_phantom_pause_not_escalating():
    # issue #1096, ai-review PR #1110, некритичное замечание 2: 13 несёт
    # CheckResult — findings[13] коллапсирует unknown() в [], эскалация по
    # findings не должна на него полагаться, пока нет отдельного канала.
    assert 13 not in ri.ESCALATING_INVARIANTS
    assert 13 in ri.CHECK_RESULT_MIGRATED_INVARIANTS


def test_build_report_wires_invariant_13(monkeypatch):
    """Проводка build_report: фантомная пауза видна строкой [13] с фактом
    (не гипотезой) — числом реальных провалов и порогом."""
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": [
            worker_run(5, "2026-09-10T11:55:00Z", conclusion="skipped"),
            worker_run(2, "2026-09-10T11:35:00Z"),
            worker_run(1, "2026-09-10T11:20:00Z"),
        ]},
        "issues/120/comments": [pause_marker_comment("2026-09-10T11:45:00Z")],
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 10, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert len(findings[13]) == 1
    assert any("🚨" in line and "[13]" in line and "0 реальных" in line for line in lines)
# Инвариант 14: воркер не рапортует успех при пустом провайдере (#876)
# ══════════════════════════════════════════════════════════════════════════


def test_worker_false_success_comment_healthy_snapshot_no_hits(monkeypatch):
    fake = FakeGh({"search/issues": {"items": []}})
    patch_gh(monkeypatch, fake)
    assert ri.check_worker_false_success_comment(REPO) == ri.check_result.ok()


def test_worker_false_success_comment_flags_genuine_regression_after_fix(monkeypatch):
    # Настоящий регресс: комментарий с точным маркером ПОЗЖЕ даты приземления
    # фикса #876 (WORKER_FALSE_SUCCESS_FIX_LANDED_AT) — единственный случай,
    # когда фраза структурно не должна была родиться заново. Ревизия #1184:
    # литерал — актуальная прод-форма («провайдер: )», пустой WORKER_CHAIN_
    # PROVIDER после интерполяции task.sh:814), не устаревшее «?».
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 900, "html_url": "https://github.com/mytab0r/edge-harness/issues/900",
             "title": "какая-то задача"},
        ]},
        "issues/900/comments": [
            {"created_at": "2026-10-01T00:00:00Z",
             "body": "🤖 Автономный воркер справился (провайдер: ). PR открыт: .../pull/999"},
        ],
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert result.violations == [{
        "issue": 900,
        "url": "https://github.com/mytab0r/edge-harness/issues/900",
        "title": "какая-то задача",
    }]


def test_worker_false_success_comment_historical_incident_not_flagged(monkeypatch):
    # Находка ai-review PR #880 (второй раунд): дословный ИСТОРИЧЕСКИЙ
    # комментарий самого инцидента (issue #140, 2026-09-10T18:53:32Z — живой
    # случай, ради которого #876 и написан) остаётся в теле issue навсегда.
    # Без отсечки по дате инвариант был бы красным с первого пульса после
    # мержа — первое появление ДО фикса не регресс, а его причина. Ревизия
    # #1184: с текущим маркером («провайдер: )», без «?» — см.
    # WORKER_FALSE_SUCCESS_MARKER) этот исторический текст (тогда ещё с «?»,
    # старый фолбэк `${WORKER_CHAIN_PROVIDER:-?}` был жив) не совпадает и по
    # буквальной подстроке — дата-гейт здесь избыточная, но не лишняя защита:
    # тест остаётся регрессом на случай, если маркер когда-нибудь снова
    # сблизится с историческим текстом.
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 140, "html_url": "https://github.com/mytab0r/edge-harness/issues/140",
             "title": "какая-то задача"},
        ]},
        "issues/140/comments": [
            {"created_at": "2026-09-10T18:53:32Z",
             "body": "🤖 Автономный воркер справился (провайдер: ?). PR открыт: .../pull/395"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_worker_false_success_comment(REPO) == ri.check_result.ok()


def test_worker_false_success_comment_search_false_positive_not_reported(monkeypatch):
    # Находка ai-review PR #880: GitHub Search отбрасывает пунктуацию —
    # фразовый запрос на «справился (провайдер: )» (ревизия #1184: актуальный
    # маркер, было устаревшее «?») вырождается в поиск
    # голых слов «справился»+«провайдер», которые соседствуют в КАЖДОМ
    # ЗДОРОВОМ успехе воркера («справился (провайдер: GLM)»). Search вернул
    # бы такую задачу кандидатом, но локальная сверка (буквальная подстрока
    # в реально скачанном теле) обязана её ОТКЛОНИТЬ — иначе гвардия красит
    # каждый настоящий успех, противореча собственному докстрингу.
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 200, "html_url": "https://github.com/mytab0r/edge-harness/issues/200",
             "title": "здоровая задача"},
        ]},
        "issues/200/comments": [
            {"created_at": "2026-09-10T12:00:00Z",
             "body": "🤖 Автономный воркер справился (провайдер: GLM). PR открыт: .../pull/500"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_worker_false_success_comment(REPO) == ri.check_result.ok()


def test_worker_false_success_comment_query_uses_the_exact_contradiction_marker(monkeypatch):
    # Мутация: если запрос когда-нибудь начнёт искать другую фразу (например
    # обобщённое «справился» без «провайдер: )») — это либо ложные
    # срабатывания на КАЖДЫЙ настоящий успех, либо тихая потеря сигнала.
    # Литерал ЗАШИТ здесь буквально (не через ri.WORKER_FALSE_SUCCESS_MARKER):
    # если бы тест сверял константу саму с собой, мутация значения константы
    # прошла бы мимо теста — проверяем дословный прод-текст шаблона task.sh
    # (ревизия #1184: актуальная форма с пустым провайдером, не устаревшее «?»).
    fake = FakeGh({"search/issues": {"items": []}})
    patch_gh(monkeypatch, fake)
    ri.check_worker_false_success_comment(REPO)
    assert any("справился (провайдер: )" in call for call in fake.calls), (
        f"запрос обязан нести точный маркер противоречия: {fake.calls}"
    )


def test_worker_false_success_marker_matches_task_sh_template():
    # Ai-review PR #1189: ничто механически не связывало WORKER_FALSE_SUCCESS_MARKER
    # со строкой шаблона в scripts/worker/task.sh — следующий реворд текста
    # успеха молча вернул бы инвариант 14 в класс #1172 (зелёный навсегда
    # независимо от регресса). Читаем РЕАЛЬНЫЙ task.sh, извлекаем строку
    # «🤖 Автономный воркер справился…», интерполируем пустой
    # WORKER_CHAIN_PROVIDER и сверяем результат с ri.WORKER_FALSE_SUCCESS_MARKER.
    task_sh = (_DIR.parent / "worker" / "task.sh").read_text(encoding="utf-8")
    match = re.search(
        r"^🤖 Автономный воркер справился \(провайдер: (\$\{WORKER_CHAIN_PROVIDER\})\)\.",
        task_sh, re.MULTILINE,
    )
    assert match, "task.sh обязан нести дословный шаблон успеха с ${WORKER_CHAIN_PROVIDER}"
    rendered_empty_provider = match.group(0)[:-1].replace(match.group(1), "")
    assert ri.WORKER_FALSE_SUCCESS_MARKER in rendered_empty_provider, (
        f"константа {ri.WORKER_FALSE_SUCCESS_MARKER!r} разошлась с прод-шаблоном "
        f"task.sh при пустом WORKER_CHAIN_PROVIDER: {rendered_empty_provider!r}"
    )


def test_worker_false_success_comment_ignores_marker_in_code_spans(monkeypatch):
    # PR #1189 (находка А): локальная сверка не должна считать вхождения
    # маркера внутри markdown code spans (`` `...` `` / ```` ```...``` ````) —
    # прод-комментарий воркера фразу в бэктики не заворачивает, а обсуждение
    # PR/задачи вполне может её процитировать. Цитата в бэктиках — не нарушение.
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 999, "html_url": "https://github.com/mytab0r/edge-harness/issues/999",
             "title": "обсуждение с цитатой"},
        ]},
        "issues/999/comments": [
            {"created_at": "2026-10-01T00:00:00Z",
             "body": "Инвариант ищет `справился (провайдер: )` — это новая форма после #1184"},
            {"created_at": "2026-10-01T00:00:00Z",
             "body": "```\n🤖 Автономный воркер справился (провайдер: ). PR открыт: ...\n```"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_worker_false_success_comment(REPO) == ri.check_result.ok()


def test_worker_false_success_comment_still_flags_bare_marker(monkeypatch):
    # Голое вхождение (не в бэктиках) ПОЗЖЕ даты отсечки — это нарушение.
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 998, "html_url": "https://github.com/mytab0r/edge-harness/issues/998",
             "title": "настоящий регресс"},
        ]},
        "issues/998/comments": [
            {"created_at": "2026-10-01T00:00:00Z",
             "body": "Обсуждение: новый маркер — справился (провайдер: ) — появился в логе"},
        ],
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert result.violations == [{
        "issue": 998,
        "url": "https://github.com/mytab0r/edge-harness/issues/998",
        "title": "настоящий регресс",
    }]


def test_worker_false_success_comment_unknown_on_network_failure(monkeypatch):
    # issue #1096, F6: Search недоступен целиком — раньше тихий [] (то же
    # 💚, что у доказанного «нарушений нет»); теперь check_result.unknown()
    # с названной причиной.
    fake = FakeGh({"search/issues": RuntimeError("gh api: rate limited")})
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "rate limited" in result.reason


def test_worker_false_success_comment_unknown_on_malformed_search_response(monkeypatch):
    """Находка ai-review PR #1110 (F3): ответ Search без ключа `items`
    (dict вторичного рейт-лимита) раньше молча читался как `{"items": []}`
    (`(result or {}).get("items") or []`) — «Search правда ничего не нашёл»
    и «ответ неожиданной формы» были неразличимы, оба давали ok(). Теперь
    форма проверяется до чтения items — unknown()."""
    fake = FakeGh({"search/issues": {"message": "secondary rate limit"}})
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "неожиданной" in result.reason


def test_worker_false_success_comment_unknown_when_a_candidate_sync_fails(monkeypatch):
    # issue #1096, F6, второй путь: Search нашёл кандидата, но локальная
    # сверка ЭТОГО issue упала (сеть/квота) — раньше молчаливый `continue`
    # читался как «этот кандидат чист», и при отсутствии других находок
    # весь инвариант отдавал 💚. Теперь — check_result.unknown(): «чисто»
    # здесь недоказанное утверждение, не факт.
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 777, "html_url": "https://github.com/mytab0r/edge-harness/issues/777",
             "title": "не сверенный кандидат"},
        ]},
        "issues/777/comments": RuntimeError("gh api: 502"),
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_UNKNOWN
    assert "1" in result.reason and "777" not in result.reason  # число, не конкретный issue


def test_worker_false_success_comment_confirmed_violation_beats_unchecked_sibling(monkeypatch):
    # issue #1096, F6: подтверждённое нарушение у ОДНОГО кандидата не должно
    # прятаться за тем, что СОСЕДНИЙ кандидат не удалось сверить — реальная
    # находка важнее чужой неопределённости (иначе это была бы потеря сигнала).
    fake = FakeGh({
        "search/issues": {"items": [
            {"number": 900, "html_url": "https://github.com/mytab0r/edge-harness/issues/900",
             "title": "настоящий регресс"},
            {"number": 777, "html_url": "https://github.com/mytab0r/edge-harness/issues/777",
             "title": "не сверенный кандидат"},
        ]},
        "issues/900/comments": [
            {"created_at": "2026-10-01T00:00:00Z",
             "body": "🤖 Автономный воркер справился (провайдер: ). PR открыт: .../pull/999"},
        ],
        "issues/777/comments": RuntimeError("gh api: 502"),
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_worker_false_success_comment(REPO)
    assert result.status == ri.check_result.STATUS_VIOLATION
    assert result.violations == [{
        "issue": 900,
        "url": "https://github.com/mytab0r/edge-harness/issues/900",
        "title": "настоящий регресс",
    }]


def test_worker_false_success_comment_not_in_ci_gating():
    # Наблюдательный: доступность стороннего Search API не должна красить
    # обязательную проверку `test` (см. докстринг check_worker_false_success_comment).
    assert 14 not in ri.CI_GATING


def test_worker_false_success_comment_not_escalating():
    # issue #1096, ai-review PR #1110, некритичное замечание 2: то же, что у
    # инварианта 13 — CheckResult не должен гейтить/эскалировать по findings.
    assert 14 not in ri.ESCALATING_INVARIANTS
    assert 14 in ri.CHECK_RESULT_MIGRATED_INVARIANTS


def test_assert_check_result_invariants_not_gated_or_escalated_passes_on_real_constants():
    # Регресс-доказательство на ЖИВЫХ константах модуля — не только на
    # синтетике ниже: сегодняшние CI_GATING/ESCALATING_INVARIANTS обязаны
    # проходить эту проверку молча.
    ri.assert_check_result_invariants_not_gated_or_escalated(ri.CI_GATING, ri.ESCALATING_INVARIANTS)


def test_assert_check_result_invariants_raises_if_migrated_invariant_added_to_ci_gating():
    with pytest.raises(RuntimeError, match="13"):
        ri.assert_check_result_invariants_not_gated_or_escalated(frozenset({7, 11, 13}), ())


def test_assert_check_result_invariants_raises_if_migrated_invariant_added_to_escalating():
    with pytest.raises(RuntimeError, match="14"):
        ri.assert_check_result_invariants_not_gated_or_escalated(frozenset(), (1, 3, 14))


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 15: мерж без реакции push-триггера (#955, следствие #929)
# ══════════════════════════════════════════════════════════════════════════

MERGE_REACTION_REGISTRY = [
    {"prefix": "", "workflow": "repo-ci.yml"},
    {"prefix": "cf-worker/", "workflow": "deploy-worker.yml"},
    {"prefix": "dsh-edge/", "workflow": "deploy-dsh-edge.yml"},
]


def merged_pull_with_sha(number, merged_at, sha):
    return {"number": number, "merged_at": merged_at, "merge_commit_sha": sha}


def test_merge_reaction_gaps_flags_missing_run_within_window(monkeypatch):
    """Живой класс #929: слияние тронуло cf-worker/, но deploy-worker.yml не
    имеет ни одного прогона на его head_sha спустя 10 минут (>GRACE, <WINDOW)
    — диспатч либо не сработал, либо промахнулся мимо этого коммита."""
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(900, "2026-09-10T11:50:00Z", "dbe8c9956d")]
    fake = FakeGh({
        "pulls/900/files": [{"filename": "cf-worker/src/config.ts"}],
        "workflows/repo-ci.yml/runs?head_sha=dbe8c9956d": {"workflow_runs": [{"id": 1}]},
        "workflows/deploy-worker.yml/runs?head_sha=dbe8c9956d": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    results = ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY)
    assert len(results) == 1
    assert results[0] == {
        "pr": 900, "sha": "dbe8c9956d", "workflow": "deploy-worker.yml",
        "merged_at": "2026-09-10T11:50:00Z", "age_minutes": 10.0, "status": "missing",
    }


def test_merge_reaction_gaps_silent_when_run_exists(monkeypatch):
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(901, "2026-09-10T11:50:00Z", "abc123")]
    fake = FakeGh({
        "pulls/901/files": [{"filename": "cf-worker/src/config.ts"}],
        "workflows/repo-ci.yml/runs?head_sha=abc123": {"workflow_runs": [{"id": 1}]},
        "workflows/deploy-worker.yml/runs?head_sha=abc123": {"workflow_runs": [{"id": 2}]},
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY) == []


def test_merge_reaction_gaps_ignores_merge_within_grace_period(monkeypatch):
    # 2 минуты < MERGE_REACTION_GRACE_MINUTES (5) — диспатчу ещё не дали
    # время появиться в Actions API, судить рано.
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(902, "2026-09-10T11:58:00Z", "sha902")]
    fake = FakeGh({})  # ни одного вызова gh() не ожидается вовсе
    patch_gh(monkeypatch, fake)
    assert ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY) == []
    assert fake.calls == []


def test_merge_reaction_gaps_ignores_merge_older_than_window(monkeypatch):
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(903, "2026-09-10T07:00:00Z", "sha903")]  # 300 мин назад
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    assert ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY) == []
    assert fake.calls == []


def test_merge_reaction_gaps_ignores_path_with_no_matching_registry_entry(monkeypatch):
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(904, "2026-09-10T11:50:00Z", "sha904")]
    fake = FakeGh({
        "pulls/904/files": [{"filename": "docs/README.md"}],
        "workflows/repo-ci.yml/runs?head_sha=sha904": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    # Префикс "" реестра всё равно матчит ЛЮБОЙ путь (repo-ci.yml) — пустой
    # прогон на этом sha ДОЛЖЕН считаться нарушением; проверяем отдельно, что
    # cf-worker/dsh-edge записи НЕ добавили лишних нарушений для пути вне их
    # префикса (единственное нарушение — repo-ci.yml).
    results = ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY)
    assert [(r["workflow"], r["status"]) for r in results] == [("repo-ci.yml", "missing")]


def test_merge_reaction_gaps_skips_pull_without_merge_commit_sha(monkeypatch):
    # merge_commit_sha отсутствует (пул мог не успеть заполнить поле) — не
    # с чем сверять head_sha, кандидат честно пропускается, не гадаем.
    now = utc(2026, 9, 10, 12, 0)
    merged = [{"number": 905, "merged_at": "2026-09-10T11:50:00Z"}]
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    assert ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY) == []
    assert fake.calls == []


def test_merge_reaction_gaps_best_effort_on_files_fetch_failure(monkeypatch):
    """Найдено ревью PR #956: сбой сети на ОДНОМ кандидате раньше глотался
    голым `continue` — 906 просто исчезал из результата, и build_report
    молча читал это как «906 здоров». Теперь 906 присутствует в результате
    со `status: "unchecked"` (видимо, не молча) — 907 (без сбоя) разбирается
    как обычно и даёт `status: "missing"`."""
    now = utc(2026, 9, 10, 12, 0)
    merged = [
        merged_pull_with_sha(906, "2026-09-10T11:50:00Z", "sha906"),
        merged_pull_with_sha(907, "2026-09-10T11:50:00Z", "sha907"),
    ]
    fake = FakeGh({
        "pulls/906/files": RuntimeError("gh api: rate limited"),
        "pulls/907/files": [{"filename": "cf-worker/x"}],
        "workflows/repo-ci.yml/runs?head_sha=sha907": {"workflow_runs": [{"id": 1}]},
        "workflows/deploy-worker.yml/runs?head_sha=sha907": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    results = ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY)
    by_pr = {r["pr"]: r["status"] for r in results}
    assert by_pr[906] == "unchecked"
    assert by_pr[907] == "missing"
    unchecked_906 = next(r for r in results if r["pr"] == 906)
    assert unchecked_906["workflow"] is None  # сбой на уровне ФЕТЧА ФАЙЛОВ — до резолва workflow
    assert "rate limited" in unchecked_906["error"]


def test_merge_reaction_gaps_unchecked_on_run_lookup_failure(monkeypatch):
    """Тот же класс, что фетч файлов выше, но сбой на втором сетевом вызове
    (has_run_for_sha) — тоже 'unchecked', не силентная пропажа."""
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(920, "2026-09-10T11:50:00Z", "sha920")]
    fake = FakeGh({
        "pulls/920/files": [{"filename": "cf-worker/x"}],
        "workflows/repo-ci.yml/runs?head_sha=sha920": RuntimeError("gh api: rate limited"),
        "workflows/deploy-worker.yml/runs?head_sha=sha920": {"workflow_runs": [{"id": 1}]},
    })
    patch_gh(monkeypatch, fake)
    results = ri.check_merge_reaction_gaps(REPO, now, merged, registry=MERGE_REACTION_REGISTRY)
    by_workflow = {r["workflow"]: r["status"] for r in results}
    assert by_workflow["repo-ci.yml"] == "unchecked"
    assert "deploy-worker.yml" not in by_workflow  # у него был реальный прогон — здоров, не в списке


def test_merge_reaction_gaps_uses_real_registry_by_default(monkeypatch):
    """Без явного `registry=` читает config/merge-reactions.json — реальный
    файл репозитория, не тестовую подмену (иначе мутация файла реестра не
    ловится этим тестом)."""
    now = utc(2026, 9, 10, 12, 0)
    merged = [merged_pull_with_sha(908, "2026-09-10T11:50:00Z", "sha908")]
    fake = FakeGh({
        "pulls/908/files": [{"filename": "cf-worker/src/config.ts"}],
        "runs?head_sha=sha908": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    violations = ri.check_merge_reaction_gaps(REPO, now, merged)
    workflows = {v["workflow"] for v in violations}
    assert "repo-ci.yml" in workflows and "deploy-worker.yml" in workflows


def test_merge_reaction_gaps_not_in_ci_gating():
    # Наблюдательный (тот же порядок, что у 1/5/9/10/12/13/14): замер долга
    # на живом репозитории на момент внедрения ещё не сделан.
    assert 15 not in ri.CI_GATING


def test_build_report_wires_invariant_15(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [merged_pull_with_sha(909, "2026-09-10T11:50:00Z", "sha909")],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "pulls/909/files": [{"filename": "cf-worker/src/config.ts"}],
        "runs?head_sha=sha909": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 10, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert len(findings[15]) >= 1
    assert any("🚨" in line and "[15]" in line for line in lines)
    assert any("sha909"[:8] in line for line in lines)  # короткий sha в отчёте


def test_build_report_never_claims_healthy_on_unchecked_only(monkeypatch):
    """Находка ревью PR #956: сбой сети на ЕДИНСТВЕННОМ кандидате не должен
    рендериться как 💚 «нарушений нет» — build_report обязан явно сказать
    «не удалось проверить», не молчать о невозможности проверки."""
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [merged_pull_with_sha(911, "2026-09-10T11:50:00Z", "sha911")],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "pulls/911/files": RuntimeError("gh api: rate limited"),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 10, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[15] == []  # нет ПОДТВЕРЖДЁННЫХ нарушений — но это не то же самое, что здоровье
    assert not any("💚" in line and "[15]" in line for line in lines)
    assert any("⚠️" in line and "[15]" in line and "не удалось проверить" in line for line in lines)


def test_run_escalations_wires_invariant_15_with_fact_not_guess(monkeypatch):
    calls = []

    def fake_issue_marker_times(repo, issue_number, marker):
        return []

    def fake_escalate(repo, issue_number, text):
        calls.append(text)
        return "отправлено"

    monkeypatch.setattr(ri, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(ri, "escalate", fake_escalate)

    findings = {15: [{"pr": 910, "sha": "deadbeefcafe", "workflow": "deploy-worker.yml",
                       "merged_at": "2026-09-10T11:50:00Z", "age_minutes": 15.0}]}
    lines = ri.run_escalations("mytab0r/edge-harness", findings)
    assert len(calls) == 1
    text = calls[0]
    # Алерт называет ФАКТ (workflow, sha, время) — не гипотезу (AGENTS.md,
    # «Алерт не гадает»).
    assert "deploy-worker.yml" in text and "deadbeef" in text and "15.0" in text
    assert any("инвариант 15" in line for line in lines)


def test_escalating_invariants_includes_15():
    assert 15 in ri.ESCALATING_INVARIANTS


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 16 (дефект A, watchdog-issue #120, 2026-09-11): WIP-гейт объявил
# ложное «0 PR ждут доработки» — независимый пересчёт по открытым PR
# расходится с заявленным count и переворачивает решение допуска диспатча
# ══════════════════════════════════════════════════════════════════════════
#
# Тела маркеров ниже — БУКВАЛЬНО тот же шаблон, что публикует
# scheduler.wip_gate (WIP_GATE_CLOSE_MARKER/WIP_GATE_OPEN_MARKER, не
# пересказ формата) — прод-форма проверена по исходнику scheduler.py.

def _close_marker_body(count: int, limit: int) -> str:
    return (f"✅ {ri.scheduler.WIP_GATE_CLOSE_MARKER}\n"
            f"Открытых PR, ждущих доработки: {count} < {limit} — WIP-лимит снят, "
            "новые задачи снова диспетчируются.")


def _open_marker_body(count: int, limit: int) -> str:
    return (f"⏸️ {ri.scheduler.WIP_GATE_OPEN_MARKER}\n"
            f"Открытых PR, ждущих доработки: {count} ≥ лимита {limit}. Новые задачи не "
            "диспетчируются, пока очередь не поредеет.")


def test_check_wip_gate_false_zero_flags_live_incident_2026_09_11():
    """Живой случай: маркер CLOSE заявил 0 (в 10:25:27Z), реальных открытых
    PR с REWORK_LABELS на тот же момент — 27 (WIP_LIMIT=12) — решения
    расходятся: 0 < 12 (пропускает новые задачи), 27 >= 12 (должен держать)."""
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 10, 25, 27)
    markers = [(marker_at, _close_marker_body(0, limit))]
    pulls = [open_pr(900 + n, labels=["conflict"]) for n in range(27)]
    violations = ri.check_wip_gate_false_zero(marker_at, markers, pulls)
    assert len(violations) == 1
    assert violations[0] == {
        "marker_at": marker_at.isoformat(),
        "claimed_count": 0,
        "actual_count": 27,
        "limit": limit,
        "claimed_closes_gate": False,
        "actual_closes_gate": True,
        "literal_false_zero": True,
    }


def test_check_wip_gate_false_zero_silent_when_consistent():
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 10, 2, 44)
    markers = [(marker_at, _open_marker_body(25, limit))]
    pulls = [open_pr(900 + n, labels=["conflict"]) for n in range(25)]
    assert ri.check_wip_gate_false_zero(marker_at, markers, pulls) == []


def test_check_wip_gate_false_zero_silent_when_both_agree_gate_open():
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 8, 0)
    markers = [(marker_at, _close_marker_body(3, limit))]
    pulls = [open_pr(1, labels=["conflict"])]
    assert ri.check_wip_gate_false_zero(marker_at, markers, pulls) == []


def test_check_wip_gate_false_zero_flags_literal_zero_below_limit():
    """Буква ТЗ #948 п.4 (находка ревью PR #950, третий проход): claimed=0,
    actual=1..11 (лимит не перейдён ни там, ни там, решение допуска
    формально совпадает — гейт остаётся открыт) раньше молчало, хотя маркер
    буквально соврал «доработки нет» при живом PR в доработке. Мутация:
    убери ветку `literal_false_zero` (верни `if claimed_closes_gate ==
    actual_closes_gate: return []` без второго условия) — тест покраснеет."""
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 8, 0)
    markers = [(marker_at, _close_marker_body(0, limit))]
    pulls = [open_pr(1, labels=["conflict"])]  # 1 PR в доработке, лимит не перейдён
    violations = ri.check_wip_gate_false_zero(marker_at, markers, pulls)
    assert violations == [{
        "marker_at": marker_at.isoformat(),
        "claimed_count": 0,
        "actual_count": 1,
        "limit": limit,
        "claimed_closes_gate": False,
        "actual_closes_gate": False,
        "literal_false_zero": True,
    }]


def test_check_wip_gate_false_zero_silent_when_claimed_zero_and_actual_zero():
    """Честный ноль (claimed=0, actual=0) — не находка: маркер не соврал."""
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 8, 0)
    markers = [(marker_at, _close_marker_body(0, limit))]
    assert ri.check_wip_gate_false_zero(marker_at, markers, []) == []


def test_check_wip_gate_false_zero_ignores_stale_marker_outside_window():
    """Маркер старше WIP_GATE_FALSE_ZERO_WINDOW_MINUTES не сравнивается с
    текущим снимком — естественный дрейф числа открытых PR между пульсами не
    должен читаться как расхождение (AGENTS.md «алерт не гадает»)."""
    limit = ri.scheduler.WIP_LIMIT
    marker_at = utc(2026, 9, 11, 9, 0)
    now = utc(2026, 9, 11, 12, 0)  # 3 часа спустя — далеко за окном (30 мин)
    markers = [(marker_at, _close_marker_body(0, limit))]
    pulls = [open_pr(900 + n, labels=["conflict"]) for n in range(27)]
    assert ri.check_wip_gate_false_zero(now, markers, pulls) == []


def test_check_wip_gate_false_zero_silent_when_no_markers():
    assert ri.check_wip_gate_false_zero(utc(2026, 9, 11, 12, 0), [], []) == []


def test_check_wip_gate_false_zero_uses_latest_marker_not_oldest():
    limit = ri.scheduler.WIP_LIMIT
    older = utc(2026, 9, 11, 10, 0)
    newer = utc(2026, 9, 11, 10, 20)
    markers = [
        (older, _open_marker_body(25, limit)),   # эпизод открыт
        (newer, _close_marker_body(0, limit)),   # тот же эпизод только что закрыт
    ]
    pulls = [open_pr(900 + n, labels=["conflict"]) for n in range(27)]
    violations = ri.check_wip_gate_false_zero(newer, markers, pulls)
    assert len(violations) == 1
    assert violations[0]["claimed_count"] == 0  # берётся САМЫЙ свежий маркер, не старый


def test_fetch_wip_gate_markers_reads_both_marker_kinds(monkeypatch):
    limit = ri.scheduler.WIP_LIMIT
    fake = FakeGh({
        "issues/120/comments": [
            {"created_at": "2026-09-11T10:02:44Z", "body": _open_marker_body(25, limit),
             "user": {"login": ri.pulse_guard.EVENT_ACTOR_LOGIN}},
            {"created_at": "2026-09-11T10:25:27Z", "body": _close_marker_body(0, limit),
             "user": {"login": ri.pulse_guard.EVENT_ACTOR_LOGIN}},
        ],
    })
    patch_gh(monkeypatch, fake)
    markers = ri.fetch_wip_gate_markers("mytab0r/edge-harness")
    assert len(markers) == 2
    assert {body for _, body in markers} == {
        _open_marker_body(25, limit), _close_marker_body(0, limit),
    }


def test_fetch_wip_gate_markers_includes_comment_not_from_ci_actor(monkeypatch):
    """Доводка #1027 находкой #1074 (живой случай watchdog-issue #120,
    2026-09-12/13): комментарий `[статус конвейера: WIP-лимит снят] ...
    0 < 12` дословно взят с живого репозитория (`gh api
    repos/mytab0r/edge-harness/issues/120/comments`, id 5650994043,
    2026-09-13T03:53:03Z) — его REST-форма несёт `user.login == "mytab0r"`,
    `user.type == "User"` (личный PAT, прогон `scheduler.py` вне GitHub
    Actions), а не `github-actions[bot]`/`Bot`, как у честного маркера
    orchestra.yml рядом (id 5649763434, «22 ≥ 12»).

    #1027 подключил `trusted_login=pulse_guard.EVENT_ACTOR_LOGIN` именно
    сюда — и тем самым сделал инвариант 16 СЛЕПЫМ к 26 таким маркерам подряд
    (репозиторий #1074): фильтр защищает РЕШЕНИЕ `scheduler.wip_gate` (см.
    test_scheduler.py::test_wip_gate_ignores_close_marker_not_posted_by_ci_
    actor — тот фильтр остаётся), но применённый здесь же он не даёт
    НАБЛЮДАТЕЛЬНОМУ инварианту увидеть чужеродную запись вовсе, до всякого
    сравнения claimed/actual. fetch_wip_gate_markers обязан вернуть ОБА
    маркера — фильтрация по автору здесь не нужна и не тестовому предмету.

    Мутация: верни `trusted_login=pulse_guard.EVENT_ACTOR_LOGIN` в
    `fetch_wip_gate_markers` — этот тест покраснеет (markers будет содержать
    1 маркер, не 2 — самозванец отфильтрован молча)."""
    limit = ri.scheduler.WIP_LIMIT
    fake = FakeGh({
        "issues/120/comments": [
            {"created_at": "2026-09-13T00:47:39Z", "body": _open_marker_body(22, limit),
             "user": {"login": ri.pulse_guard.EVENT_ACTOR_LOGIN, "type": "Bot"}},
            {"created_at": "2026-09-13T03:53:03Z",
             "body": "✅ [статус конвейера: WIP-лимит снят]\n"
                     "Открытых PR, ждущих доработки: 0 < 12 — WIP-лимит снят, новые задачи "
                     "снова диспетчируются.",
             "user": {"login": "mytab0r", "type": "User"}},
        ],
    })
    patch_gh(monkeypatch, fake)
    markers = ri.fetch_wip_gate_markers("mytab0r/edge-harness")
    assert len(markers) == 2
    assert {body for _, body in markers} == {
        _open_marker_body(22, limit),
        "✅ [статус конвейера: WIP-лимит снят]\n"
        "Открытых PR, ждущих доработки: 0 < 12 — WIP-лимит снят, новые задачи "
        "снова диспетчируются.",
    }


def test_build_report_flags_wip_gate_false_zero_from_impostor_marker(monkeypatch):
    """Сквозная проверка — воспроизводит инцидент #1074 целиком через
    build_report: тело ложного маркера дословно взято с живого репозитория
    (issue #120, комментарий id 5650994043, 2026-09-13T03:53:03Z, автор
    `mytab0r`/`User`, не `github-actions[bot]`). До этой правки инвариант 16
    молчал бы (fetch_wip_gate_markers отфильтровывал такой комментарий по
    trusted_login ДО сравнения) — теперь он обязан найти расхождение
    claimed=0 / actual>0 независимо от автора маркера."""
    limit = ri.scheduler.WIP_LIMIT
    now = utc(2026, 9, 13, 3, 55)  # 2 минуты после ложного маркера — внутри окна
    pulls = [open_pr(1000 + n, labels=["conflict"]) for n in range(23)]  # прод-число той ночи
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": pulls,
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "issues/120/comments": [
            {"created_at": "2026-09-13T00:47:39Z", "body": _open_marker_body(22, limit),
             "user": {"login": ri.pulse_guard.EVENT_ACTOR_LOGIN, "type": "Bot"}},
            {"created_at": "2026-09-13T03:53:03Z",
             "body": "✅ [статус конвейера: WIP-лимит снят]\n"
                     "Открытых PR, ждущих доработки: 0 < 12 — WIP-лимит снят, новые задачи "
                     "снова диспетчируются.",
             "user": {"login": "mytab0r", "type": "User"}},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[16] == [{
        "marker_at": "2026-09-13T03:53:03+00:00",
        "claimed_count": 0,
        "actual_count": 23,
        "limit": limit,
        "claimed_closes_gate": False,
        "actual_closes_gate": True,
        "literal_false_zero": True,
    }]
    assert any("🚨" in line and "[16]" in line and "23" in line for line in lines)


def test_build_report_flags_wip_gate_false_zero_live_incident(monkeypatch):
    """Сквозная проверка через build_report (не только unit на чистой
    функции) — воспроизводит #120 2026-09-11 целиком: маркер CLOSE(0) в
    комментариях #120, 27 реально открытых PR ждут доработки."""
    limit = ri.scheduler.WIP_LIMIT
    now = utc(2026, 9, 11, 10, 26)
    # `conflict`, не `ai:changes-requested` (тоже входит в REWORK_LABELS,
    # scheduler.REWORK_LABELS): последний — ФИНАЛЬНЫЙ вердикт ai-гейта, и
    # завёл бы сюда ещё и дорогой обход инварианта 8 (check_wasted_ai_review_
    # runs — latest_ai_comment на каждый PR), не предмет этого теста.
    pulls = [open_pr(900 + n, labels=["conflict"]) for n in range(27)]
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": pulls,
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "issues/120/comments": [
            {"created_at": "2026-09-11T10:25:27Z", "body": _close_marker_body(0, limit),
             "user": {"login": ri.pulse_guard.EVENT_ACTOR_LOGIN}},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[16] == [{
        "marker_at": "2026-09-11T10:25:27+00:00",
        "claimed_count": 0,
        "actual_count": 27,
        "limit": limit,
        "claimed_closes_gate": False,
        "actual_closes_gate": True,
        "literal_false_zero": True,
    }]
    assert any("🚨" in line and "[16]" in line and "27" in line for line in lines)


def test_wip_gate_false_zero_is_escalating_not_gating():
    # Наблюдательный по построению (тот же принцип, что 8/10/12/14): нарушение
    # зависит от истории маркеров #120, гейтить им PR означало бы красить
    # чужой PR за чужое искажение снимка.
    assert 16 not in ri.CI_GATING
    assert 16 in ri.ESCALATING_INVARIANTS


# ══════════════════════════════════════════════════════════════════════════
# Дедуп эскалации инварианта 16: «состояние + окно», не «состояние + момент»
# (issue #1261, живой случай 2026-09-13/14: 36 отдельных эскалаций за ~30
# часов при неизменном claimed=0 — старый ключ нёс сырой marker_at/
# actual_count, дрейфующие почти на каждый такт)
# ══════════════════════════════════════════════════════════════════════════

def _wip_gate_finding(marker_at: datetime, claimed: int, actual: int, limit: int) -> dict:
    """Находка инварианта 16 в ПРОД-ФОРМЕ (AGENTS.md, «Тест кормит прод-форму
    данных, а не пересказ»): не рукописный словарь с булевой математикой,
    пересчитанной на тестовой стороне, а результат самого
    check_wip_gate_false_zero — тело маркера той же формы, какую публикует
    scheduler.wip_gate («Открытых PR, ждущих доработки: …»), и синтетические
    открытые PR, из которых scheduler.pr_needs_rework насчитывает ровно
    `actual`. Смена булевой математики в проде здесь краснеть НЕ молчит:
    помощник отдаёт то, что реально вернул прод-код, а пустой список
    (согласованное состояние) валит тест громким assert, не тихой находкой."""
    body = (f"{ri.scheduler.WIP_GATE_CLOSE_MARKER}\n"
            f"Открытых PR, ждущих доработки: {claimed} < {limit} — WIP-лимит снят")
    rework_pull = {
        "draft": False,
        "user": {"login": "mytab0r"},
        "labels": [{"name": label} for label in sorted(ri.scheduler.REWORK_LABELS)],
    }
    finding = ri.check_wip_gate_false_zero(
        marker_at, [(marker_at, body)], [dict(rework_pull) for _ in range(actual)])
    assert len(finding) == 1, f"ожидаемая находка не построилась: {claimed=}, {actual=}, {limit=}"
    return finding[0]


def _wire_escalation_recorder(monkeypatch):
    """Тот же приём, что test_run_escalations_pipeline_health_dedupes_by_last_date
    (инвариант 12), но с НАКАПЛИВАЮЩИМСЯ множеством уже отправленных маркеров
    (issue_marker_times читает СОСТОЯНИЕ #120, которое растёт с каждым
    escalate()) — нужно для сценария «несколько тактов подряд»."""
    already_escalated_markers: set[str] = set()
    calls: list[str] = []

    def fake_issue_marker_times(repo, issue, marker):
        return [utc(2026, 9, 1)] if marker in already_escalated_markers else []

    def fake_escalate(repo, issue, text):
        calls.append(text)
        already_escalated_markers.add(text.splitlines()[0])  # маркер — первая строка
        return "отправлено"

    monkeypatch.setattr(ri, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(ri, "escalate", fake_escalate)
    return calls


# MUTATION-PROOF
# ref: 66d12179c62af09e44b2e4d5ddb0755914ae1e95
# paths: scripts/orchestra/repo_invariants.py
# run: python -X utf8 -m pytest scripts/orchestra/test_repo_invariants.py::test_run_escalations_wip_gate_dedupes_repeated_ticks_of_same_state -q
# expect: 1 failed
def test_run_escalations_wip_gate_dedupes_repeated_ticks_of_same_state(monkeypatch):
    """Живой случай 2026-09-13/14: то же состояние (claimed=0, actual растёт
    естественным дрейфом 28→30), маркер republish'ится другим временем — один
    такт диспатча (orchestra.yml, ~15 мин) позже НЕ должен дать вторую
    эскалацию, пока окно ESCALATION_REMINDER_WINDOW_MINUTES не истекло.

    Мутация: верни старый ключ (`f"{item['marker_at']}:{item['claimed_count']}:
    {item['actual_count']}"`) в run_escalations — этот тест покраснеет (calls
    будет содержать 2 элемента, не 1). Доказано исполнением (issue #1261):
    вывод red/green зафиксирован в теле PR. MUTATION-PROOF выше даёт CI
    возможность повторить это исполнение самостоятельно (scripts/lib/
    mutation_recipe_guard.py, ADR 0023) — `ref` указывает на состояние ДО
    этого фикса (текущий origin/main на момент задачи #1261)."""
    calls = _wire_escalation_recorder(monkeypatch)
    limit = ri.scheduler.WIP_LIMIT
    first_tick = utc(2026, 9, 13, 10, 6, 48)
    second_tick = first_tick + timedelta(minutes=9)  # тот же 15-минутный такт диспатча

    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(first_tick, 0, 28, limit)]})
    assert len(calls) == 1

    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(second_tick, 0, 30, limit)]})
    assert len(calls) == 1, "то же состояние внутри окна напоминания — не новая эскалация"


def test_run_escalations_wip_gate_state_change_escalates_immediately(monkeypatch):
    """Смена КАЧЕСТВЕННОГО состояния (здесь: literal_false_zero перестаёт
    выполняться, потому что actual тоже упал в ноль) обязана эскалировать
    сразу, даже внутри того же окна времени — это не тот же инцидент."""
    calls = _wire_escalation_recorder(monkeypatch)
    limit = ri.scheduler.WIP_LIMIT
    moment = utc(2026, 9, 13, 10, 6, 48)

    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(moment, 0, 28, limit)]})
    assert len(calls) == 1

    # 5 минут спустя, тот же 6-часовой бакет, но actual_closes_gate теперь
    # False (было True) — переворот решения допуска другого типа.
    later = moment + timedelta(minutes=5)
    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(later, 0, 2, limit)]})
    assert len(calls) == 2, "смена типа расхождения — новая эскалация, даже в том же окне"


def test_run_escalations_wip_gate_recurrence_after_pause_escalates_again(monkeypatch):
    """Возврат ТОГО ЖЕ состояния после паузы, длиннее окна напоминания,
    обязан снова эскалировать — иначе рецидив, наступивший днём позже,
    остался бы немым (ровно риск, названный в задаче #1261: просто выкинуть
    marker_at схлопнул бы такой рецидив навсегда). Здесь пауза дольше
    ESCALATION_REMINDER_WINDOW_MINUTES, числа буквально совпадают с первым
    наблюдением — единственное отличие от «того же такта» выше — время."""
    calls = _wire_escalation_recorder(monkeypatch)
    limit = ri.scheduler.WIP_LIMIT
    first = utc(2026, 9, 13, 10, 6, 48)
    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(first, 0, 28, limit)]})
    assert len(calls) == 1

    # Инцидент «разрешился» между тактами (findings.get(16) пуст — здоровые
    # тики не вызывают escalate_if_new вовсе, ничего дополнительно мокать не
    # нужно), затем то же состояние возвращается почти через сутки.
    recurred = first + timedelta(hours=20)
    ri.run_escalations("mytab0r/edge-harness", {16: [_wip_gate_finding(recurred, 0, 28, limit)]})
    assert len(calls) == 2, "рецидив после долгой паузы обязан эскалировать снова"


def test_escalation_state_key_windowing_matches_reminder_constant():
    """Мутация: поменяй ESCALATION_REMINDER_WINDOW_MINUTES на 0 (или убери
    деление на него) — этот тест покраснеет, потому что моменты внутри
    объявленного окна перестанут давать одинаковый ключ."""
    limit = ri.scheduler.WIP_LIMIT
    window = ri.ESCALATION_REMINDER_WINDOW_MINUTES
    # Начало окна выровнено по той же арифметике, что escalation_state_key
    # (floor(epoch_minutes / window) * window) — иначе «через window-1 минуту»
    # от ПРОИЗВОЛЬНОГО момента может уже попасть в следующий бакет.
    raw = utc(2026, 9, 13, 10, 0)
    bucket_start_minutes = (int(raw.timestamp() // 60) // window) * window
    bucket_start = datetime.fromtimestamp(bucket_start_minutes * 60, tz=timezone.utc)
    finding_a = _wip_gate_finding(bucket_start + timedelta(minutes=1), 0, 28, limit)
    finding_b = _wip_gate_finding(bucket_start + timedelta(minutes=window - 1), 0, 31, limit)
    finding_c = _wip_gate_finding(bucket_start + timedelta(minutes=window + 1), 0, 31, limit)

    def key_of(item):
        # Сигнатура — из прод-помощника (wip_gate_state_signature), не
        # вторая ручная сборка той же f-строки: смена формы сигнатуры в
        # проде обязана пройти и через этот замер (issue #1261, ревью PR #1263).
        return ri.escalation_state_key(
            datetime.fromisoformat(item["marker_at"]),
            ri.wip_gate_state_signature(item))

    assert key_of(finding_a) == key_of(finding_b), "внутри окна — тот же ключ, несмотря на дрейф actual"
    assert key_of(finding_a) != key_of(finding_c), "за окном — новый ключ (напоминание)"


_INV16_ESCALATION_RE = re.compile(r"\[инвариант 16: (.+?):(\d+):(\d+)\]")
_INV16_LIMIT_RE = re.compile(r"\(лимит (\d+)\)")


def _load_issue120_invariant16_history() -> list[tuple[datetime, int, int, int]]:
    """Прод-форма (issue #1261): комментарии #120 сняты `gh api
    repos/mytab0r/edge-harness/issues/120/comments?since=2026-09-13T00:00:00Z
    --paginate` (2026-09-14), фикстура несёт ВСЕ 37 комментариев, содержащих
    подстроку «инвариант 16» (одна — опровержение постороннего маркера,
    формату эскалации не соответствует и regex её не берёт — прод-форма,
    не подчищенный вручную список из ровно 36 нужных строк)."""
    path = Path(__file__).resolve().parent / "testdata" / "issue120_invariant16_escalations_2026-09-13.json"
    import json
    comments = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for c in comments:
        m = _INV16_ESCALATION_RE.search(c["body"])
        if not m:
            continue
        marker_at = datetime.fromisoformat(m.group(1))
        claimed, actual = int(m.group(2)), int(m.group(3))
        limit_m = _INV16_LIMIT_RE.search(c["body"])
        limit = int(limit_m.group(1)) if limit_m else ri.scheduler.WIP_LIMIT
        rows.append((marker_at, claimed, actual, limit))
    rows.sort(key=lambda r: r[0])
    return rows


def test_issue120_fixture_reproduces_36_escalations_with_old_key():
    """До/после (issue #1261, буквально задаваемое число): старый ключ
    (`marker_at:claimed_count:actual_count`, все три компонента разные
    почти на каждой строке фикстуры) даёт ровно 36 эскалаций на реальной
    сохранённой истории — то же число, что дал живой прогон 2026-09-13/14.
    Этот тест не про новый код — он подтверждает, что фикстура и разбор
    воспроизводят замер из задачи ДО того, как считать число «после»."""
    rows = _load_issue120_invariant16_history()
    assert len(rows) == 36

    seen = set()
    for marker_at, claimed, actual, _limit in rows:
        key = f"{marker_at.isoformat()}:{claimed}:{actual}"
        seen.add(key)
    assert len(seen) == 36


def test_issue120_fixture_new_key_collapses_36_into_units():
    """Число «после» (issue #1261): та же история, дедуп-ключ
    `escalation_state_key` — 36 наблюдений схлопываются в единицы, не в одно
    (см. соседний тест — инцидент длился ~30 часов, окно 6 часов даёт
    периодическое напоминание, а не одну немую запись на весь инцидент).

    Каждая строка фикстуры прогоняется через сам check_wip_gate_false_zero
    (та же прод-форма входа, что и у поведенческих тестов выше), сигнатура —
    из wip_gate_state_signature: прод-код здесь не пересказан формулой, а
    исполнен, и смена булевой математики в проде перенесёт этот замер за
    собой, а не оставит мерить устаревший ключ (ревью PR #1263)."""
    rows = _load_issue120_invariant16_history()
    seen_in_order = []
    for marker_at, claimed, actual, limit in rows:
        key = ri.escalation_state_key(
            marker_at, ri.wip_gate_state_signature(_wip_gate_finding(marker_at, claimed, actual, limit)))
        if key not in seen_in_order:
            seen_in_order.append(key)
    # Единицы, не десятки (было 36) и не единственная запись на весь
    # 30-часовой инцидент (что означало бы: не разрешившийся рецидив никогда
    # не напомнит о себе повторно).
    assert 1 < len(seen_in_order) < 10, seen_in_order


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 18: тот же СИМПТОМ (много эскалаций), другая ПРИЧИНА — не тронут
# (issue #1261: 18 эскалаций за то же время, но множество violator-id
# ПРОВЕРЕННО росло на каждой из них — реальные новые подделки, не пересчёт
# того же состояния; см. докстринг pipeline_status_marker_key)
# ══════════════════════════════════════════════════════════════════════════

def test_issue120_fixture_invariant18_every_escalation_carries_new_violators():
    """Подтверждение исполнением, не на глаз: РОВНО у скольких из 18
    эскалаций счётчик нарушителей строго вырос относительно предыдущей —
    если бы ключ был волатильным БЕЗ изменения состава (тот же класс
    дефекта, что у 16), встретились бы повторы одного и того же счётчика.
    На реальной истории — ни одного повтора: каждая эскалация несёт
    честно новую подделку, поэтому инвариант 18 НЕ подведён под общий
    механизм окна (см. докстринг pipeline_status_marker_key)."""
    path = Path(__file__).resolve().parent / "testdata" / "issue120_invariant18_escalations_2026-09-13.json"
    import json
    comments = json.loads(path.read_text(encoding="utf-8"))
    count_re = re.compile(r"— (\d+) таких комментариев")
    counts = []
    for c in comments:
        m = count_re.search(c["body"])
        if m:
            counts.append(int(m.group(1)))
    assert len(counts) == 18
    assert counts == sorted(counts), "счётчик нарушителей монотонно растёт — прод-факт, не допущение"
    assert len(set(counts)) == len(counts), "ни один счётчик не повторился — каждая эскалация несла новые id"


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 17: живая морда dsh-edge отстаёт от main (#1041)
# ══════════════════════════════════════════════════════════════════════════
#
# Живой случай 2026-09-12/13: deploy-dsh-edge.yml упал на run 34702503576
# (e2e-смоук нашёл 440 находок, «DSH Edge» вкладка), следующий прогон не
# наступил сутки — единственный видимый признак был красной вкладкой Actions.
# Повторный ручной прогон на ТОМ ЖЕ main (run 34742800780, без единого
# коммита между ними) упал ТОЙ ЖЕ причиной — не флейк смоука, а факт, что
# морда с этого коммита не деплоится вовсе; ниже — фикстуры именно этой формы.


def deploy_run(head_sha, conclusion="success", created_at="2026-09-12T12:21:25Z",
               html_url="https://example/runs/1"):
    return {"head_sha": head_sha, "conclusion": conclusion, "created_at": created_at,
            "html_url": html_url}


def test_path_is_watched_matches_glob_suffix_and_exact_name():
    watched = ["dsh-edge/**", ".github/workflows/deploy-dsh-edge.yml"]
    assert ri._path_is_watched("dsh-edge/e2e-smoke/browser-walk.mjs", watched)
    assert ri._path_is_watched(".github/workflows/deploy-dsh-edge.yml", watched)
    assert not ri._path_is_watched("docs/INDEX.md", watched)
    assert not ri._path_is_watched("dsh-edge-unrelated/x.txt", watched)  # префикс без "/"


def test_frontend_deploy_watched_paths_reads_real_workflow():
    # Одно место правды (AGENTS.md): читает РЕАЛЬНЫЙ deploy-dsh-edge.yml,
    # не пересказ списком-литералом — список путей мог измениться в workflow
    # без синхронной правки инварианта, тест ловит именно это расхождение.
    paths = ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml")
    assert "dsh-edge/**" in paths
    assert ".github/workflows/deploy-dsh-edge.yml" in paths


def test_frontend_deploy_watched_paths_reads_real_worker_workflow():
    # Обобщение #1419: второй деплой морды читает СВОИ пути из СВОЕГО файла —
    # не наследует dsh-edge/** чужого деплоя.
    paths = ri._frontend_deploy_watched_paths("deploy-worker.yml")
    assert "cf-worker/**" in paths
    assert ".github/workflows/deploy-worker.yml" in paths
    assert not any(p.startswith("dsh-edge/") for p in paths)


def test_frontend_deploy_watched_paths_fail_loud_when_missing(tmp_path):
    workflow = tmp_path / "deploy-dsh-edge.yml"
    workflow.write_text("on:\n  push:\n    branches: [main]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="on.push.paths"):
        ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", workflow)


def test_frontend_deploy_watched_paths_fail_loud_on_unrecognized_glob_form(tmp_path):
    # Находка ревью PR #1076 (блокирующая 1): узкая звезда (`cf-worker/src/
    # *.cjs`) — форма, которую _path_is_watched не понимает (не точное имя,
    # не "<префикс>/**"). Раньше молча матчилась бы как "не покрыто" —
    # реальный дрейф по такому пути читался бы 💚, хотя деплой его слушает.
    # Живая репродукция: decide_frontend_deploy_stale с таким входом раньше
    # возвращал None (см. коммит до этого фикса).
    workflow = tmp_path / "deploy-dsh-edge.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n    paths:\n"
        "      - dsh-edge/**\n      - cf-worker/src/*.cjs\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=re.escape("cf-worker/src/*.cjs")):
        ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", workflow)


def test_frontend_deploy_watched_paths_accepts_exclusion_glob(tmp_path):
    # "!"-исключения — форма, которую GitHub Actions понимает, но
    # _path_is_watched её вовсе не видит (не в списке паттернов для матча) —
    # не гейтим её отдельно, RuntimeError выше не про неё.
    workflow = tmp_path / "deploy-dsh-edge.yml"
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n    paths:\n"
        "      - dsh-edge/**\n      - '!dsh-edge/**.md'\n",
        encoding="utf-8",
    )
    paths = ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", workflow)
    assert paths == ["dsh-edge/**", "!dsh-edge/**.md"]


def test_frontend_deploy_watched_paths_missing_pyyaml_is_runtime_error_not_import_error(monkeypatch, tmp_path):
    # Находка ревью PR #1076 (второй проход, блокирующая): import yaml убран
    # с уровня модуля именно затем, чтобы ImportError на голом Python (класс
    # #723) или смене образа раннера не убивал ВСЕ 17 инвариантов сразу —
    # радиус отказа обязан остаться внутри check_frontend_deploy_stale
    # (build_report уже отличает RuntimeError "недоступна" от 💚 "здорово").
    workflow = tmp_path / "deploy-dsh-edge.yml"
    workflow.write_text("on:\n  push:\n    paths: ['dsh-edge/**']\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", None)  # следующий `import yaml` -> ImportError
    with pytest.raises(RuntimeError, match="PyYAML недоступна"):
        ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", workflow)


def test_frontend_deploy_watched_paths_missing_workflow_file_is_runtime_error(tmp_path):
    # Находка ревью PR #1076 (третий проход, блокирующая 1): переименованный/
    # удалённый workflow давал FileNotFoundError МИМО except RuntimeError в
    # build_report — падал весь main() инвариантов, шаг orchestra.yml без
    # continue-on-error пропускал все гвардии после него. Радиус отказа
    # обязан остаться внутри инварианта: «недоступна», не крах модуля.
    with pytest.raises(RuntimeError, match="не найден"):
        ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", tmp_path / "deploy-dsh-edge.yml")


def test_frontend_deploy_watched_paths_broken_yaml_is_runtime_error(tmp_path):
    # Тот же радиус: опечатка в YAML workflow на main давала yaml.ScannerError
    # мимо except RuntimeError — тот же крах всего модуля гвардий.
    workflow = tmp_path / "deploy-dsh-edge.yml"
    workflow.write_text("on:\n  push:\n    paths: [dsh-edge/**\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="YAML не разбирается"):
        ri._frontend_deploy_watched_paths("deploy-dsh-edge.yml", workflow)


def test_decide_frontend_deploy_stale_no_completed_runs_is_unconfirmed_not_healthy():
    # Пусто (или всё ещё бежит) — НЕ "здорово" (AGENTS.md: «возможности нет» и
    # «возможность есть, но сломана» — разные сообщения).
    result = ri.decide_frontend_deploy_stale([], "mainsha", None, ["dsh-edge/**"])
    assert result["status"] == "unconfirmed"
    assert result["last_deployed_sha"] is None
    assert result["latest_run_url"] is None


def test_decide_frontend_deploy_stale_no_success_in_scanned_runs():
    runs = [deploy_run("aaa", conclusion="failure", html_url="https://example/runs/9")]
    result = ri.decide_frontend_deploy_stale(runs, "mainsha", None, ["dsh-edge/**"])
    assert result["status"] == "never-succeeded"
    assert result["last_deployed_sha"] is None
    assert result["latest_conclusion"] == "failure"
    assert result["latest_run_url"] == "https://example/runs/9"
    # Упавший прогон стоял на ЧУЖОМ sha — «деплой текущего main упал» фактами
    # не является (AGENTS.md, «Алерт не гадает»).
    assert result["main_deploy_state"] == "not-attempted"
    assert result["main_run_url"] is None


def test_decide_frontend_deploy_stale_silent_when_last_good_is_main():
    runs = [deploy_run("mainsha")]
    assert ri.decide_frontend_deploy_stale(runs, "mainsha", None, ["dsh-edge/**"])["status"] == "healthy"


def test_decide_frontend_deploy_stale_silent_when_drift_outside_watched_paths():
    # main ушёл дальше, но только по README.md — деплою это безразлично
    # (AGENTS.md, «Алерт не гадает»): нельзя тревожить тем, что не триггерит
    # деплой вовсе.
    runs = [deploy_run("oldsha")]
    changed = ["README.md", "docs/INDEX.md"]
    assert ri.decide_frontend_deploy_stale(runs, "newsha", changed, ["dsh-edge/**"])["status"] == "healthy"


def test_decide_frontend_deploy_stale_flags_live_incident_2026_09_12():
    # Прод-форма живого случая: последний зелёный — dddd94c1 (бамп пина
    # #810/#811), main ушёл на ecf646da (#600/#603, browser-walk.mjs) —
    # ровно путь, который deploy-dsh-edge.yml слушает. Прогон на ecf646da
    # БЫЛ и упал → main_deploy_state == "failed" с URL.
    runs = [
        deploy_run("ecf646da", conclusion="failure",
                   created_at="2026-09-12T15:31:50Z", html_url="https://example/runs/34702503576"),
        deploy_run("dddd94c1", conclusion="success",
                   created_at="2026-09-12T12:21:25Z", html_url="https://example/runs/34693446228"),
    ]
    changed = ["dsh-edge/e2e-smoke/browser-walk.mjs", "docs/INDEX.md"]
    result = ri.decide_frontend_deploy_stale(runs, "ecf646da", changed, ["dsh-edge/**"])
    assert result == {
        "status": "stale",
        "last_deployed_sha": "dddd94c1",
        "last_success_at": "2026-09-12T12:21:25Z",
        "last_success_url": "https://example/runs/34693446228",
        "main_sha": "ecf646da",
        "stale_paths": ["dsh-edge/e2e-smoke/browser-walk.mjs"],
        "latest_run_url": "https://example/runs/34702503576",
        "latest_conclusion": "failure",
        "main_deploy_state": "failed",
        "main_run_url": "https://example/runs/34702503576",
    }


def test_decide_frontend_deploy_stale_not_attempted_when_no_run_on_main():
    # Данные различают «деплой main упал» от «деплой на main не запускался»
    # (ревью PR #1076, третий проход, блокирующая 2): последний зелёный позади,
    # main ушёл по watched-пути, но прогона с head_sha == main в окне НЕТ —
    # stale с main_deploy_state "not-attempted", алерт советует ЗАПУСТИТЬ, а не
    # «проверь причину падения» (падения не было).
    runs = [deploy_run("oldsha", html_url="https://example/runs/1")]
    changed = ["dsh-edge/e2e-smoke/browser-walk.mjs"]
    result = ri.decide_frontend_deploy_stale(runs, "newmain", changed, ["dsh-edge/**"])
    assert result["status"] == "stale"
    assert result["main_deploy_state"] == "not-attempted"
    assert result["main_run_url"] is None


def test_decide_frontend_deploy_stale_silent_while_current_main_deploy_in_flight():
    # Находка ревью PR #1076 (блокирующая 2): незавершённый прогон с
    # head_sha == main_sha — деплой ЭТОГО main уже идёт, не "устарела".
    # Третий проход: это состояние БОЛЬШЕ не коллапсирует с «здорово» —
    # отдельный статус "in-flight" (чеклист ревью: непроверенное не должно
    # печататься как 💚 «морда стоит на main»).
    runs = [
        deploy_run("newmain", conclusion=None,
                   created_at="2026-09-13T08:00:00Z", html_url="https://example/runs/99"),
        deploy_run("oldsha", conclusion="success",
                   created_at="2026-09-12T12:21:25Z", html_url="https://example/runs/1"),
    ]
    changed = ["dsh-edge/e2e-smoke/browser-walk.mjs"]
    result = ri.decide_frontend_deploy_stale(runs, "newmain", changed, ["dsh-edge/**"])
    assert result["status"] == "in-flight"
    assert result["latest_run_url"] == "https://example/runs/99"
    assert result["stale_paths"] == []


def test_decide_frontend_deploy_stale_flags_after_inflight_deploy_fails():
    # Симметрично: если тот же прогон, что был "в полёте", ЗАВЕРШИЛСЯ
    # неуспехом — сигнал не потерян, следующий пересчёт (те же runs, но
    # conclusion уже проставлен) обязан снова поднять устарелость.
    runs = [
        deploy_run("newmain", conclusion="failure",
                   created_at="2026-09-13T08:00:00Z", html_url="https://example/runs/99"),
        deploy_run("oldsha", conclusion="success",
                   created_at="2026-09-12T12:21:25Z", html_url="https://example/runs/1"),
    ]
    changed = ["dsh-edge/e2e-smoke/browser-walk.mjs"]
    result = ri.decide_frontend_deploy_stale(runs, "newmain", changed, ["dsh-edge/**"])
    assert result["status"] == "stale"
    assert result["last_deployed_sha"] == "oldsha"
    assert result["main_deploy_state"] == "failed"
    assert result["main_run_url"] == "https://example/runs/99"


def test_check_frontend_deploy_stale_healthy_default(monkeypatch):
    fake = FakeGh({})  # дефолт FakeGh уже здоров: last_good_sha == main_sha
    patch_gh(monkeypatch, fake)
    result = ri.check_frontend_deploy_stale(REPO)
    # Оба деплоя морды проверены (#1419), оба здоровы.
    assert result["deploy-dsh-edge.yml"]["status"] == "healthy"
    assert result["deploy-worker.yml"]["status"] == "healthy"
    assert result["deploy-worker.yml"]["workflow"] == "deploy-worker.yml"
    # Дешёвый путь: last_good_sha уже совпал с main_sha — ни on.push.paths,
    # ни compare/ не читаются.
    assert not any("compare/" in c for c in fake.calls)


def test_check_frontend_deploy_stale_flags_worker_incident_1419(monkeypatch):
    # Живой случай #1419: deploy-dsh-edge.yml здоров, deploy-worker.yml на
    # каждом прогоне красен (устаревший генерат) — дифф должен быть виден
    # ПО КАЖДОМУ деплою отдельно, а не тонуть в общем 💚 другого.
    fake = FakeGh({
        # dsh-edge-деплой зелёный на main — не участник этого сценария.
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("d848f998", conclusion="success"),
        ]},
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("d848f998", conclusion="failure",
                       created_at="2026-09-13T17:51:33Z", html_url="https://example/runs/1419a"),
            deploy_run("53135c4e", conclusion="success",
                       created_at="2026-09-12T00:17:25Z", html_url="https://example/runs/1419b"),
        ]},
        "commits/main": {"sha": "d848f998"},
        "compare/53135c4e...d848f998": {"files": [
            {"filename": "cf-worker/src/index.ts"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_frontend_deploy_stale(REPO)
    assert result["deploy-dsh-edge.yml"]["status"] == "healthy"  # дефолт FakeGh
    worker = result["deploy-worker.yml"]
    assert worker["status"] == "stale"
    assert worker["workflow"] == "deploy-worker.yml"
    assert worker["main_deploy_state"] == "failed"
    assert worker["stale_paths"] == ["cf-worker/src/index.ts"]


def test_check_frontend_deploy_stale_compare_shared_between_workflows(monkeypatch):
    # Оба деплоя стоят на одном последнем зелёном — compare/ делается ОДИН
    # раз (кэш), а не по копии на workflow.
    fake = FakeGh({
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("oldsha", conclusion="success"),
        ]},
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("oldsha", conclusion="success", html_url="https://example/runs/2"),
        ]},
        "commits/main": {"sha": "newmain"},
        "compare/oldsha...newmain": {"files": [{"filename": "docs/INDEX.md"}]},
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_frontend_deploy_stale(REPO)
    # дрейф вне watched у обоих → оба здоровы
    assert result["deploy-dsh-edge.yml"]["status"] == "healthy"
    assert result["deploy-worker.yml"]["status"] == "healthy"
    assert sum(1 for c in fake.calls if "compare/" in c) == 1


def test_check_frontend_deploy_stale_flags_live_incident_via_gh(monkeypatch):
    fake = FakeGh({
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("ecf646da", conclusion="failure",
                       created_at="2026-09-12T15:31:50Z", html_url="https://example/runs/34702503576"),
            deploy_run("dddd94c1", conclusion="success",
                       created_at="2026-09-12T12:21:25Z", html_url="https://example/runs/34693446228"),
        ]},
        # deploy-worker.yml — дефолтный здоровый маршрут FakeGh стоит на
        # deadbeef; здесь main = ecf646da, ставим его зелёный на main, чтобы
        # предмет теста (dsh-edge-инцидент #1041) остался единственным сигналом.
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("ecf646da", conclusion="success", html_url="https://example/runs/2"),
        ]},
        "commits/main": {"sha": "ecf646da"},
        "compare/dddd94c1...ecf646da": {"files": [
            {"filename": "dsh-edge/e2e-smoke/browser-walk.mjs"},
            {"filename": "docs/INDEX.md"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    result = ri.check_frontend_deploy_stale(REPO)
    assert result["deploy-dsh-edge.yml"]["status"] == "stale"
    assert result["deploy-dsh-edge.yml"]["last_deployed_sha"] == "dddd94c1"
    assert result["deploy-dsh-edge.yml"]["stale_paths"] == ["dsh-edge/e2e-smoke/browser-walk.mjs"]
    assert result["deploy-dsh-edge.yml"]["main_deploy_state"] == "failed"
    assert result["deploy-worker.yml"]["status"] == "healthy"


def test_check_frontend_deploy_stale_compare_ceiling_is_runtime_error_not_false_healthy(monkeypatch):
    # Находка ревью PR #1076 (чеклист, класс «страница GitHub API без обхода»,
    # #308/#309): compare/ режет files на жёстком потолке БЕЗ флага в ответе —
    # ровно потолочное число файлов означает «сравнение неполно». Обрезка
    # выкидывает хвост (где dsh-edge/** после docs/) — раньше это читалось как
    # «дрейфа нет» → ложное 💚. Теперь громкое «недоступна».
    many = [{"filename": f"docs/file{i}.md"} for i in range(ri.COMPARE_FILES_CEILING)]
    fake = FakeGh({
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("ecf646da", conclusion="failure"),
            deploy_run("dddd94c1", conclusion="success"),
        ]},
        "commits/main": {"sha": "ecf646da"},
        "compare/dddd94c1...ecf646da": {"files": many},
    })
    patch_gh(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="сравнение обрезано"):
        ri.check_frontend_deploy_stale(REPO)


def test_build_report_flags_frontend_deploy_stale(monkeypatch):
    now = utc(2026, 9, 13, 6, 30)
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        "search/issues": {"items": []},
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("ecf646da", conclusion="failure"),
            deploy_run("dddd94c1", conclusion="success"),
        ]},
        # deploy-worker.yml зелёный на main — его 💚 не обязан существовать,
        # но и мешать чужому тесту не должен.
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("ecf646da", conclusion="success"),
        ]},
        "commits/main": {"sha": "ecf646da"},
        "compare/dddd94c1...ecf646da": {"files": [
            {"filename": "dsh-edge/e2e-smoke/browser-walk.mjs"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report(REPO, now)
    assert findings[17][0]["last_deployed_sha"] == "dddd94c1"
    assert findings[17][0]["workflow"] == "deploy-dsh-edge.yml"
    assert any("🚨" in line and "[17]" in line and "устарела" in line for line in lines)


def test_build_report_flags_deploy_worker_stale_1419(monkeypatch):
    # Живой случай #1419 — инвариант называет красный deploy-worker.yml:
    # dsh-edge-деплой здоров, worker-деплой девять суток красен. До
    # обобщения этот сценарий печатал общий 💚 «морда стоит на main».
    now = utc(2026, 9, 21, 12, 0)
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        "search/issues": {"items": []},
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("d848f998", conclusion="success"),
        ]},
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("d848f998", conclusion="failure"),
            deploy_run("53135c4e", conclusion="success"),
        ]},
        "commits/main": {"sha": "d848f998"},
        "compare/53135c4e...d848f998": {"files": [
            {"filename": "cf-worker/src/index.ts"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report(REPO, now)
    assert len(findings[17]) == 1
    assert findings[17][0]["workflow"] == "deploy-worker.yml"
    assert any("🚨" in line and "[17]" in line and "deploy-worker.yml" in line for line in lines)


def test_build_report_frontend_deploy_stale_reports_unavailable_not_healthy(monkeypatch):
    # Сеть недоступна — build_report обязан отличить «не проверено» от
    # «здорово» (тот же приём, что инварианты 12/15).
    now = utc(2026, 9, 13, 6, 30)
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        "search/issues": {"items": []},
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "workflows/deploy-dsh-edge.yml/runs": RuntimeError(
            "gh api repos/o/r/actions/workflows/deploy-dsh-edge.yml/runs: HTTP 503"),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report(REPO, now)
    assert findings[17] == []
    assert any("🚨" in line and "[17]" in line and "недоступна" in line for line in lines)


def test_frontend_deploy_stale_not_in_ci_gating_is_escalating():
    # Наблюдательный, не гейтящий (тот же класс, что 10/12/15/16): нарушение
    # зависит от последнего успешного прогона деплоя, не от содержимого
    # текущего PR — гейтить им PR означало бы красить чужой PR за чужой
    # упавший деплой.
    assert 17 not in ri.CI_GATING
    assert 17 in ri.ESCALATING_INVARIANTS


def test_run_escalations_posts_frontend_deploy_stale_once(monkeypatch):
    fake = FakeGh({"issues/120/comments": []})
    patch_gh(monkeypatch, fake)
    findings = {17: [{
        "status": "stale",
        "workflow": "deploy-dsh-edge.yml",
        "last_deployed_sha": "dddd94c1", "last_success_at": "2026-09-12T12:21:25Z",
        "last_success_url": "https://example/runs/1", "main_sha": "ecf646da",
        "stale_paths": ["dsh-edge/e2e-smoke/browser-walk.mjs"],
        "latest_run_url": "https://example/runs/9", "latest_conclusion": "failure",
        "main_deploy_state": "failed", "main_run_url": "https://example/runs/9",
    }]}
    lines = ri.run_escalations(REPO, findings)
    assert any("инвариант 17" in line for line in lines)
    posts = fake.mutating_calls()
    assert len(posts) == 1
    # «Алерт не гадает»: факт «деплой main упал» назван С URL, не как гипотеза.
    assert "https://example/runs/9" in posts[0]
    assert "УПАЛ" in posts[0]


def test_run_escalations_each_deploy_workflow_escalates_separately(monkeypatch):
    # Обобщение #1419: больны ОБА деплоя — два поста, каждый называет СВОЙ
    # workflow; ключ дедупа несёт имя, чтобы снятие алерта одного деплоя
    # не заглушило алерт другого.
    fake = FakeGh({"issues/120/comments": []})
    patch_gh(monkeypatch, fake)
    stale = lambda wf: {
        "status": "stale", "workflow": wf,
        "last_deployed_sha": "dddd94c1", "last_success_at": "2026-09-12T12:21:25Z",
        "last_success_url": "https://example/runs/1", "main_sha": "ecf646da",
        "stale_paths": ["docs/x.md"],
        "latest_run_url": "https://example/runs/9", "latest_conclusion": "failure",
        "main_deploy_state": "failed", "main_run_url": "https://example/runs/9",
    }
    ri.run_escalations(REPO, {17: [stale("deploy-dsh-edge.yml"), stale("deploy-worker.yml")]})
    posts = fake.mutating_calls()
    assert len(posts) == 2
    assert "deploy-dsh-edge.yml" in posts[0]
    assert "deploy-worker.yml" in posts[1]


def test_run_escalations_frontend_deploy_stale_not_attempted_advises_dispatch(monkeypatch):
    # Ревью PR #1076 (третий проход, блокирующая 2): stale достижим и БЕЗ
    # упавшего прогона на main (деплой не запускался вовсе). Текст обязан
    # назвать это и советовать запуск, а не «проверь причину падения» —
    # чинить нечего.
    fake = FakeGh({"issues/120/comments": []})
    patch_gh(monkeypatch, fake)
    findings = {17: [{
        "status": "stale",
        "workflow": "deploy-worker.yml",
        "last_deployed_sha": "dddd94c1", "last_success_at": "2026-09-12T12:21:25Z",
        "last_success_url": "https://example/runs/1", "main_sha": "ecf646da",
        "stale_paths": ["cf-worker/src/index.ts"],
        "latest_run_url": None, "latest_conclusion": None,
        "main_deploy_state": "not-attempted", "main_run_url": None,
    }]}
    ri.run_escalations(REPO, findings)
    posts = fake.mutating_calls()
    assert len(posts) == 1
    assert "НЕ ЗАПУСКАЛСЯ" in posts[0]
    assert "workflow_dispatch" in posts[0]
    assert "deploy-worker.yml" in posts[0]
    # Ложный совет «проверь причину падения» (падения не было) отсутствует —
    # фраза «править причину падения нечего» допустима, а вот императива
    # «проверь причину» быть не должно.
    assert "проверь причину падения" not in posts[0]


def test_run_escalations_no_success_runs_states_window_fact(monkeypatch):
    # unconfirmed (завершённых прогонов нет вовсе) печатает факт окна, а не
    # «Последний прогон: None, None».
    fake = FakeGh({"issues/120/comments": []})
    patch_gh(monkeypatch, fake)
    findings = {17: [{
        "status": "unconfirmed",
        "workflow": "deploy-dsh-edge.yml",
        "last_deployed_sha": None, "last_success_at": None, "last_success_url": None,
        "main_sha": "ecf646da", "stale_paths": [],
        "latest_run_url": None, "latest_conclusion": None,
        "main_deploy_state": "not-attempted", "main_run_url": None,
    }]}
    ri.run_escalations(REPO, findings)
    posts = fake.mutating_calls()
    assert len(posts) == 1
    assert "Завершённых прогонов в сканируемом окне нет вовсе" in posts[0]


def test_build_report_in_flight_deploy_is_warning_not_false_healthy(monkeypatch):
    # Ревью PR #1076 (чеклист): незавершённый деплой текущего main раньше
    # печатался общим 💚 «морда стоит на main» — непроверенное выдавалось за
    # проверенное («Проверяй видимый результат, а не шаг»). Теперь отдельная
    # ⚠️-строка, нарушения нет, эскалация молчит.
    now = utc(2026, 9, 13, 6, 30)
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        "search/issues": {"items": []},
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "workflows/deploy-dsh-edge.yml/runs": {"workflow_runs": [
            deploy_run("newmain", conclusion=None,
                       created_at="2026-09-13T06:00:00Z", html_url="https://example/runs/99"),
            deploy_run("oldsha", conclusion="success",
                       created_at="2026-09-12T12:21:25Z", html_url="https://example/runs/1"),
        ]},
        # Второй деплой морды зелёный на main — не участник сценария (#1419).
        "workflows/deploy-worker.yml/runs": {"workflow_runs": [
            deploy_run("newmain", conclusion="success", html_url="https://example/runs/2"),
        ]},
        "commits/main": {"sha": "newmain"},
        "compare/oldsha...newmain": {"files": [
            {"filename": "dsh-edge/e2e-smoke/browser-walk.mjs"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report(REPO, now)
    assert findings[17] == []
    assert any("⚠️" in line and "[17]" in line and "в полёте" in line for line in lines)
    # in-flight НЕ печатается 💚: «деплой ещё идёт» — это «не проверено»,
    # не «свежо» (AGENTS.md, «Проверяй видимый результат, а не шаг»).
    # 💚 ВТОРОГО (здорового) деплоя корректен и делу не мешает.
    assert not any("💚 [17] deploy-dsh-edge.yml" in line for line in lines)


# Инвариант 18 (#1101): маркер статуса конвейера в #120 не от токена job'а
# ══════════════════════════════════════════════════════════════════════════

# Живая прод-форма (снята 2026-09-13, `gh api
# repos/mytab0r/edge-harness/issues/120/comments`) — тот же ложный
# close-маркер, что уже используют тесты инварианта 16 выше (id 5650994043,
# 2026-09-13T03:53:03Z), с добавленным `performed_via_github_app` (в живом
# ответе REST у этого комментария — `null`, у честного маркера рядом —
# непустой объект GitHub App «github-actions»).
_IMPOSTOR_WIP_CLOSE_COMMENT = {
    "id": 5650994043,
    "created_at": "2026-09-13T03:53:03Z",
    "body": "✅ [статус конвейера: WIP-лимит снят]\n"
            "Открытых PR, ждущих доработки: 0 < 12 — WIP-лимит снят, новые задачи "
            "снова диспетчируются.",
    "user": {"login": "mytab0r", "type": "User"},
    "performed_via_github_app": None,
    "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5650994043",
}

# Честный маркер того же семейства, опубликованный job'ом (форма
# `performed_via_github_app` — реальный объект GitHub App «github-actions»,
# id 15368, снят тем же живым запросом) — не нарушение.
_GENUINE_BOT_WIP_OPEN_COMMENT = {
    "id": 5649763434,
    "created_at": "2026-09-13T00:47:39Z",
    "body": "⏸️ [статус конвейера: WIP-лимит закрыл диспатч]\n"
            "Открытых PR, ждущих доработки: 22 ≥ 12 — новые задачи не диспетчируются.",
    "user": {"login": "github-actions[bot]", "type": "Bot"},
    "performed_via_github_app": {"id": 15368, "name": "GitHub Actions",
                                  "slug": "github-actions"},
    "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5649763434",
}


def test_check_pipeline_status_marker_impersonation_flags_live_incident():
    """Живой инцидент #1074/#1077 воспроизведён дословно: смешанные
    комментарии (честный маркер job'а + поддельный маркер личного PAT) —
    находит РОВНО поддельный, не оба и не ни одного."""
    violations = ri.check_pipeline_status_marker_impersonation(
        [_GENUINE_BOT_WIP_OPEN_COMMENT, _IMPOSTOR_WIP_CLOSE_COMMENT])
    assert violations == [{
        "id": 5650994043,
        "created_at": "2026-09-13T03:53:03Z",
        "login": "mytab0r",
        "user_type": "User",
        "url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5650994043",
    }]


def test_check_pipeline_status_marker_impersonation_silent_on_genuine_bot_only():
    assert ri.check_pipeline_status_marker_impersonation(
        [_GENUINE_BOT_WIP_OPEN_COMMENT]) == []


def test_check_pipeline_status_marker_impersonation_flags_foreign_app(monkeypatch):
    """Третий канал публикации — непустое, но ЧУЖОЕ приложение (#1389).

    Почему тест нужен именно здесь, а не только у предиката: инвариант 18 —
    ЕДИНСТВЕННЫЙ потребитель, ради которого сужение делалось, и его покрытие
    было слепым — все существующие случаи здесь про `None` и про
    `github-actions`. Мутация это показала: откат делегирования обратно к
    `performed_via_github_app is not None` оставлял десять тестов инварианта
    ЗЕЛЁНЫМИ (исполнено ai-ревью PR #1390). То есть слепота, ради которой
    задача заведена, могла вернуться молча.

    Фикстура — та же, что у test_pulse_guard (один источник, не вторая
    копия живого ответа): issuecomment-5744398934, `slug: "claude"`,
    2026-09-19T18:35:45Z — ровно тот маркер «0 < 12», чью ложность в ту же
    минуту доказал инвариант 16 независимым пересчётом (21)."""
    foreign = json.loads(
        (_DIR / "fixtures_issue120_foreign_app_wip_close_marker.json")
        .read_text(encoding="utf-8"))
    assert foreign["performed_via_github_app"]["slug"] != "github-actions", (
        "фикстура обязана нести ЧУЖОЕ приложение — иначе тест проверяет не то")

    violations = ri.check_pipeline_status_marker_impersonation(
        [_GENUINE_BOT_WIP_OPEN_COMMENT, foreign])

    assert [v["id"] for v in violations] == [5744398934], (
        "маркер чужого приложения обязан быть нарушением, а честный маркер "
        f"job'а — нет: {violations!r}")


def test_check_pipeline_status_marker_impersonation_ignores_unrelated_user_comment():
    """Комментарий человека БЕЗ маркера семейства «статус конвейера» (обычное
    обсуждение) не должен считаться нарушением, даже если автор — не job:
    инвариант проверяет СЕМЬЮ маркера, а не любой комментарий не от бота."""
    unrelated = {
        "id": 1,
        "created_at": "2026-09-13T04:00:00Z",
        "body": "Проверил вручную — на текущем main пересчёт совпадает с маркером.",
        "user": {"login": "mytab0r", "type": "User"},
        "performed_via_github_app": None,
        "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-1",
    }
    assert ri.check_pipeline_status_marker_impersonation([unrelated]) == []


def test_check_pipeline_status_marker_impersonation_catches_pause_marker_family():
    """Семейство — общий префикс «[статус конвейера: …]», не только
    WIP-гейт: PAUSE_MARKER (pulse_guard) тоже входит, иначе инвариант ловил
    бы только один из шести маркеров этого семейства."""
    impostor_pause = {
        "id": 2,
        "created_at": "2026-09-13T05:00:00Z",
        "body": f"🚨 edge-harness: {ri.pulse_guard.PAUSE_MARKER}\nручной прогон вне CI",
        "user": {"login": "mytab0r", "type": "User"},
        "performed_via_github_app": None,
        "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-2",
    }
    violations = ri.check_pipeline_status_marker_impersonation([impostor_pause])
    assert len(violations) == 1
    assert violations[0]["id"] == 2


def test_check_pipeline_status_marker_impersonation_sorted_by_time():
    later = {**_IMPOSTOR_WIP_CLOSE_COMMENT, "id": 3, "created_at": "2026-09-14T00:00:00Z"}
    violations = ri.check_pipeline_status_marker_impersonation(
        [later, _IMPOSTOR_WIP_CLOSE_COMMENT])
    assert [v["id"] for v in violations] == [5650994043, 3]


def test_check_pipeline_status_marker_impersonation_mutation_guard():
    """Доказательство, что тест реально проверяет ЗАЩИТУ, не пустой список:
    без проверки `performed_via_github_app` (мутация — как если бы условие
    было снято) честный маркер job'а тоже попал бы в находки — этот тест
    ловит именно ту мутацию, дословно применяя её здесь же, без правки
    исходника."""
    def naive_check(comments):
        # Мутация: как check_pipeline_status_marker_impersonation, но БЕЗ
        # фильтра по performed_via_github_app.
        return [c for c in comments
                if ri.PIPELINE_STATUS_MARKER_FAMILY_RE.search(c.get("body") or "")]

    mutated = naive_check([_GENUINE_BOT_WIP_OPEN_COMMENT, _IMPOSTOR_WIP_CLOSE_COMMENT])
    assert len(mutated) == 2  # мутация красит ОБА — включая честный маркер job'а

    real = ri.check_pipeline_status_marker_impersonation(
        [_GENUINE_BOT_WIP_OPEN_COMMENT, _IMPOSTOR_WIP_CLOSE_COMMENT])
    assert len(real) == 1  # настоящая проверка отличает job от личного PAT


def test_build_report_wires_invariant_18(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "issues/120/comments": [_GENUINE_BOT_WIP_OPEN_COMMENT, _IMPOSTOR_WIP_CLOSE_COMMENT],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 13, 8, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[18] == [{
        "id": 5650994043,
        "created_at": "2026-09-13T03:53:03Z",
        "login": "mytab0r",
        "user_type": "User",
        "url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5650994043",
    }]
    assert any("🚨" in line and "[18]" in line and "5650994043" in line for line in lines)


def test_build_report_invariant_18_healthy_when_no_impostor(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "issues/120/comments": [_GENUINE_BOT_WIP_OPEN_COMMENT],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 13, 8, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert findings[18] == []
    assert any("💚" in line and "[18]" in line for line in lines)


def test_check_pipeline_status_marker_impersonation_ignores_prose_quoting_marker():
    """Находка ревью PR #1102 (живые id 5641698169/5651719035, 2026-09-13):
    два реальных комментария #120 упоминают дословный текст маркера СРЕДИ
    ПРОЗЫ (ревизия пула, цитирующая `pulse_guard.py:100-102`; сам разбор
    инцидента #1074, дословно приводящий текст ложных маркеров как улику) —
    оба от `mytab0r`/`User`/`performed_via_github_app: null`, то есть по
    голому совпадению подстроки оба стали бы ложным нарушением. Первая
    строка обоих комментариев маркера НЕ содержит — инвариант обязан
    смотреть только на первую строку и промолчать."""
    revision_excerpt = {
        "id": 5641698169,
        "created_at": "2026-09-11T23:12:59Z",
        "body": (
            "🧹 Ревизия пула (проход PM 2026-09-12): **задачу НЕ закрываю. "
            "Снимаю метки `task` и `stale-unclaimed` — #120 переоформляется "
            "из задачи пула в постоянный служебный канал эскалации.**\n\n"
            "**Почему не закрытие.** Механизм, ради которого задача заводилась, "
            "реализован — `scripts/orchestra/pulse_guard.py:100-102`: "
            "`WATCHDOG_ISSUE = 120`, `PAUSE_MARKER = \"[статус конвейера: "
            "пауза]\"`, плюс `conveyor_gate` (пауза диспатча после серии "
            "красных `worker.yml`)."
        ),
        "user": {"login": "mytab0r", "type": "User"},
        "performed_via_github_app": None,
        "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5641698169",
    }
    refutation_excerpt = {
        "id": 5651719035,
        "created_at": "2026-09-13T06:40:52Z",
        "body": (
            "⚠️ [опровержение] 26 ложных маркеров «WIP-лимит снят» ниже — "
            "недостоверны\n\n"
            "Аудит 2026-09-13 (issue #1074): между **2026-09-12T17:21:47Z** "
            "и **2026-09-13T03:53:03Z**\nв этот канал попали 26 комментариев "
            "`✅ [статус конвейера: WIP-лимит снят] Открытых PR,\nждущих "
            "доработки: 0 < 12` от логина `mytab0r` (`user.type=User`, "
            "`performed_via_github_app=none` — личный PAT вне GitHub Actions)."
        ),
        "user": {"login": "mytab0r", "type": "User"},
        "performed_via_github_app": None,
        "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5651719035",
    }
    assert ri.check_pipeline_status_marker_impersonation(
        [revision_excerpt, refutation_excerpt]) == []


def test_check_pipeline_status_marker_impersonation_mutation_guard_first_line():
    """Мутация: если бы проверка смотрела на ВСЁ тело, а не на первую строку
    (как это и было в первой версии этого инварианта, найдено ревью #1102),
    оба комментария из предыдущего теста стали бы ложными нарушениями."""
    def whole_body_check(comments):
        return [c for c in comments
                if ri.PIPELINE_STATUS_MARKER_FAMILY_RE.search(c.get("body") or "")
                and c.get("performed_via_github_app") is None]

    prose_only = [{
        "id": 1, "created_at": "2026-09-11T23:12:59Z",
        "body": "Ревизия:\n`PAUSE_MARKER = \"[статус конвейера: пауза]\"` — просто цитата, не маркер.",
        "user": {"login": "mytab0r", "type": "User"},
        "performed_via_github_app": None,
        "html_url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-1",
    }]
    assert len(whole_body_check(prose_only)) == 1  # мутация красит прозу
    assert ri.check_pipeline_status_marker_impersonation(prose_only) == []  # фикс молчит


def test_pipeline_status_marker_impersonation_not_in_ci_gating():
    # Наблюдательный НАВСЕГДА (см. блок-комментарий у самой функции): долг по
    # прошлым комментариям #120 структурно необнуляем (правило репозитория
    # запрещает их удалять) — гейтить им PR означало бы красить main вечно.
    assert 18 not in ri.CI_GATING


def test_pipeline_status_marker_impersonation_is_escalating():
    """Находка не живёт только строкой отчёта прогона (блокирующая находка
    ревью PR #1102): инвариант 18 входит в ESCALATING_INVARIANTS, у
    run_escalations есть его ветка. Основание весомее, чем у соседнего 16:
    поддельный PAUSE/RESUME не только сигнализирует, он РЕАЛЬНО двигает
    решение (conveyor_gate читает маркеры #120 без trusted_login,
    поддельный RESUME работает виртуальным success) — владелец обязан
    узнавать о каждой новой подделке из канала (#120 + Telegram), а не
    из лога CI, который никто не читает."""
    assert 18 in ri.ESCALATING_INVARIANTS
    # Структурная привязка номера к ветке эскалации — поведенческим тестом
    # ниже (test_run_escalations_invariant_18_*), здесь только реестр.


def test_run_escalations_invariant_18_dedupes_by_id_set(monkeypatch):
    """Эскалация «раз на состояние» (тот же приём, что у 12/15/16): тот же
    набор id не эскалируется второй раз — вечный долг из 174 комментариев
    даёт ОДНУ эскалацию, не спам каждые 15 минут; новая подделка меняет
    набор — новая эскалация, и её текст несёт факты (счётчик, последний по
    времени, различение по токену), а не гадание, кто писатель."""
    calls = []
    markers_seen = []

    def fake_issue_marker_times(repo, issue, marker):
        markers_seen.append(marker)
        # Точный маркер уже эскалированного состояния — ключ это хэш
        # множества id (ri.pipeline_status_marker_key — то же место правды).
        expected = f"[инвариант 18: {ri.pipeline_status_marker_key([known])}]"
        return [utc(2026, 9, 13, 0, 0)] if marker == expected else []

    def fake_escalate(repo, issue, text):
        calls.append((issue, text))
        return "отправлено"

    monkeypatch.setattr(ri, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(ri, "escalate", fake_escalate)

    known = {
        "id": 5650994043,
        "created_at": "2026-09-13T03:53:03Z",
        "login": "mytab0r",
        "user_type": "User",
        "url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-5650994043",
    }
    # Тот же набор, что уже эскалирован — тишина, конвейер не спамит.
    assert ri.run_escalations("mytab0r/edge-harness", {18: [known]}) == []
    assert calls == []

    # Новая подделка меняет множество — одна новая эскалация с фактами.
    newer = {**known, "id": 5652329765, "created_at": "2026-09-13T08:57:10Z"}
    lines = ri.run_escalations("mytab0r/edge-harness", {18: [known, newer]})
    assert len(calls) == 1
    issue, text = calls[0]
    assert issue == ri.WATCHDOG_ISSUE
    assert "2 таких комментариев" in text  # счётчик, не «либо/либо»
    assert "2026-09-13T08:57:10Z" in text  # последний по времени
    assert "performed_via_github_app" in text  # признак — токен, не логин
    assert "не подтверждено" in text  # алерт не гадает: авторство не установлено
    assert any("📣 инвариант 18 эскалирован" in line for line in lines)


def test_run_escalations_invariant_18_key_stays_compact(monkeypatch):
    """Гвардия класса «вход, растущий со временем» (блокирующая находка
    ревью PR #1102, третий раунд): полный перечень id в дедуп-ключе умирал о
    лимит Bot API 4096 символов (при 174 нарушителях текст уже 2569
    символов, темп писателя ~25/сутки). Ключ — хэш множества: даже при 500
    нарушителях маркер остаётся коротким, а смена состава (новая подделка)
    всё ещё даёт НОВЫЙ ключ."""
    seen_markers = []

    def fake_issue_marker_times(repo, issue, marker):
        seen_markers.append(marker)
        return []

    sent = []

    def fake_escalate(repo, issue, text):
        sent.append((repo, issue, text))
        return "отправлено"

    monkeypatch.setattr(ri, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(ri, "escalate", fake_escalate)

    base = {
        "created_at": "2026-09-13T03:53:03Z",
        "login": "mytab0r",
        "user_type": "User",
        "url": "https://github.com/mytab0r/edge-harness/issues/120#issuecomment-x",
    }
    many = [{**base, "id": 5560000000 + n} for n in range(500)]
    ri.run_escalations("mytab0r/edge-harness", {18: many})
    assert len(seen_markers) == 1
    assert seen_markers[0].startswith("[инвариант 18: ")
    assert len(seen_markers[0]) < 80  # при 174 id старый ключ был ~2000 символов

    many_plus_one = many + [{**base, "id": 9999999999, "created_at": "2026-09-14T00:00:00Z"}]
    ri.run_escalations("mytab0r/edge-harness", {18: many_plus_one})
    assert len(seen_markers) == 2
    assert seen_markers[0] != seen_markers[1]  # новый состав — новая эскалация


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 22 (issue #1253): «доводку не звали ни разу» отдельно от
# «доводка звалась и не помогла» — check_stuck_review_gate (инвариант 3)
# структурно не видит PR с уже вынесенным ai:changes-requested.
# ══════════════════════════════════════════════════════════════════════════


def timeline_with_ai_changes(when: str):
    return [{"event": "labeled", "label": {"name": ri.review_labels.AI_CHANGES}, "created_at": when}]


def ai_rework_marker_comment(when: str):
    return {"created_at": when, "body": f"🤖 {ri.scheduler.AI_REWORK_MARKER} fp:abc123 текст"}


def test_ai_rework_never_dispatched_flags_after_threshold_without_marker(monkeypatch):
    # Живой случай (issue #1253, замер 2026-09-14): PR #261 несёт
    # ai:changes-requested с 2026-09-06T09:13:49Z (~8.3 сут на момент замера)
    # и ни разу не получал маркер авто-доводки — голодание очереди по
    # возрасту (dispatch_ai_review_rework перебирал сырой порядок open_pulls,
    # новые PR первыми, до фикса этим же PR).
    pull = open_pr(261, labels=[ri.review_labels.AI_CHANGES])
    fake = FakeGh({
        "issues/261/timeline": timeline_with_ai_changes("2026-09-06T09:13:49Z"),
        "issues/261/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 33)
    violations = ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 261
    assert violations[0]["age_minutes"] > ri.AI_REWORK_NEVER_DISPATCHED_AFTER_MINUTES


def test_ai_rework_never_dispatched_silent_within_threshold(monkeypatch):
    # Свежепомеченный PR — формирующийся бэклог сразу после дисптача,
    # НЕ находка (тот же класс «тормоз без газа», от которого уже
    # отказались для 1/4/5/9): очередь из N PR разгребается сама за часы.
    pull = open_pr(1254, labels=[ri.review_labels.AI_CHANGES])
    fake = FakeGh({
        "issues/1254/timeline": timeline_with_ai_changes("2026-09-14T15:33:15Z"),
        "issues/1254/comments": [],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 34)
    assert ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull]) == []


def test_ai_rework_never_dispatched_silent_when_marker_present(monkeypatch):
    # «Доводка звалась и не помогла» — ДРУГОЕ состояние: бюджет
    # dispatch_ai_review_rework (AI_REWORK_MAX_ATTEMPTS + эскалация) уже
    # отвечает за него, второй тормоз здесь не заводится.
    #
    # Мутация: убери ветку `if ever_dispatched: continue` в
    # check_ai_rework_never_dispatched — этот тест покраснеет (PR #804,
    # 7.5 суток с меткой, стал бы ложной находкой несмотря на маркер доводки).
    pull = open_pr(804, labels=[ri.review_labels.AI_CHANGES])
    fake = FakeGh({
        "issues/804/timeline": timeline_with_ai_changes("2026-09-09T09:35:51Z"),
        "issues/804/comments": [ai_rework_marker_comment("2026-09-12T13:00:00Z")],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 33)  # ~7.5 суток с простановки метки
    assert ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull]) == []


def test_ai_rework_never_dispatched_flags_recidivism_across_episodes(monkeypatch):
    """Находка написания дельта-спеки этого PR (класс #1172 в новом коде):
    `ever_dispatched` сканировал ВСЮ историю комментариев PR на маркер
    доводки, не сверяясь с `labeled_at` ТЕКУЩЕГО эпизода — PR, которому
    доводку делали в ПРОШЛОМ эпизоде ai:changes-requested, потом метку
    сняли, потом навесили снова и он опять застоялся сверх порога, не
    отмечался НИКОГДА: старый маркер глушил находку ровно на рецидиве, ради
    которого инвариант написан.

    Два эпизода на одном PR: доводка (маркер) в ПЕРВОМ эпизоде
    (2026-08-25 → снята 2026-08-28), тишина во ВТОРОМ эпизоде (навешена
    заново 2026-09-06) сверх порога — инвариант обязан найти, несмотря на
    маркер первого эпизода.

    Мутация: убери условие `pulse_guard.parse_time(comment["created_at"]) >=
    labeled_at` (верни голое `MARKER in body for comment in comments`,
    без сверки с episode) — этот тест покраснеет: старый маркер первого
    эпизода снова заглушит находку второго."""
    pull = open_pr(900, labels=[ri.review_labels.AI_CHANGES])
    timeline = [
        {"event": "labeled", "label": {"name": ri.review_labels.AI_CHANGES},
         "created_at": "2026-08-25T00:00:00Z"},
        {"event": "unlabeled", "label": {"name": ri.review_labels.AI_CHANGES},
         "created_at": "2026-08-28T00:00:00Z"},
        {"event": "labeled", "label": {"name": ri.review_labels.AI_CHANGES},
         "created_at": "2026-09-06T00:00:00Z"},
    ]
    fake = FakeGh({
        "issues/900/timeline": timeline,
        # Маркер лежит МЕЖДУ первым labeled (08-25) и unlabeled (08-28) —
        # принадлежит ПЕРВОМУ эпизоду, строго раньше второго labeled (09-06).
        "issues/900/comments": [ai_rework_marker_comment("2026-08-26T12:00:00Z")],
    })
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 33)  # ~8.6 суток со ВТОРОГО эпизода (09-06)
    violations = ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 900
    assert violations[0]["labeled_at"].startswith("2026-09-06")  # возраст СЧИТАН от нового эпизода


def test_ai_rework_never_dispatched_silent_for_conflict_pr(monkeypatch):
    # Conflict-PR — своя очередь (dispatch_conflict_rework), этот инвариант
    # их не трогает вовсе, даже если ai:changes-requested тоже висит.
    pull = open_pr(560, labels=[ri.review_labels.AI_CHANGES, ri.scheduler.CONFLICT_LABEL])
    fake = FakeGh({})  # маршрут даже не должен запрашиваться
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 33)
    assert ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull]) == []
    assert fake.calls == []


def test_ai_rework_never_dispatched_silent_when_no_ai_changes_label():
    pull = open_pr(1, labels=["review:ok"])
    now = utc(2026, 9, 14, 15, 33)
    assert ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull]) == []


def test_ai_rework_never_dispatched_silent_when_label_event_not_found(monkeypatch):
    # ai_changes_labeled_at вернул None (таймлайн не отдал событие — метка
    # снята и переставлена мимо API, редкий факт) — не гадаем, не находка.
    pull = open_pr(1, labels=[ri.review_labels.AI_CHANGES])
    fake = FakeGh({"issues/1/timeline": []})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 14, 15, 33)
    assert ri.check_ai_rework_never_dispatched("mytab0r/edge-harness", now, [pull]) == []


def test_ai_rework_never_dispatched_not_in_ci_gating_or_escalating():
    # Наблюдательный (docstring build_report/CI_GATING): свежедобавленный
    # инвариант, ноль истории эскалаций — тот же порядок, что у 8/9/10/12/14.
    assert 22 not in ri.CI_GATING
    assert 22 not in ri.ESCALATING_INVARIANTS


def test_build_report_wires_invariant_22(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [{**open_pr(261, labels=[ri.review_labels.AI_CHANGES]),
                               "created_at": "2026-09-06T00:00:00Z"}],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "issues/261/timeline": timeline_with_ai_changes("2026-09-06T09:13:49Z"),
        "issues/261/comments": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 14, 15, 33)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert len(findings[22]) == 1
    assert findings[22][0]["pr"] == 261
    assert any("🚨" in line and "[22]" in line for line in lines)
    assert any("#261" in line for line in lines)
# Инвариант 24 (#1250): наблюдаемость уборки рабочих деревьев
# ══════════════════════════════════════════════════════════════════════════

# Прод-форма записи уборщика — сам состав полей читается из ЕДИНОГО модуля
# канала (worktree_snapshot.make_record), не из копии словаря в тесте.
def wt_record(ts="2026-09-14T11:00:00+00:00", **over):
    base = ri.worktree_snapshot.make_record(
        ts=ts, mode="apply", total=76, removed=28, kept=48,
        guard_copies=73, stale_guard_copies=13, stuck_old_by_code={
            "unpushed": 7, "unknown_work": 1, "unknown_pr": 1},
        retention_hours=1.0)
    base.update(over)
    return base


def test_worktree_cleanup_records_no_records_is_not_healthy():
    """«Записей нет НИ РАЗУ» — нарушение («объект ненаблюдаем»), не 💚:
    инвариант, читающий пустоту как чистоту, повторял бы класс #882."""
    violations = ri.check_worktree_cleanup_records([], 26, utc(2026, 9, 14, 12, 0))
    assert len(violations) == 1 and violations[0]["kind"] == "no-records"


def test_worktree_cleanup_records_stale_channel_names_unobservability():
    old = wt_record(ts="2026-09-01T11:00:00+00:00")
    violations = ri.check_worktree_cleanup_records([old], 26, utc(2026, 9, 14, 12, 0))
    assert len(violations) == 1
    assert violations[0]["kind"] == "stale-channel"
    assert violations[0]["age_days"] == 13.0
    assert violations[0]["threshold_days"] == ri.worktree_snapshot.RECORD_STALE_AFTER_DAYS


def test_worktree_cleanup_records_fresh_total_zero_is_healthy():
    """Свежая запись с total=0 — «деревьев нет»: прогон СОСТОЯЛСЯ и деревьев
    не нашёл. Это отличимое от «ненаблюдаем» здоровое состояние."""
    fresh_zero = wt_record(total=0, removed=0, kept=0, guard_copies=0,
                           stale_guard_copies=0, stuck_old_total=0)
    assert ri.check_worktree_cleanup_records([fresh_zero], 26, utc(2026, 9, 14, 12, 0)) == []


def test_worktree_cleanup_records_stale_guard_copies_over_open_prs():
    """Вторая половина критерия готовности #1250: устаревших копий гвардий
    не больше числа открытых PR. Числа — из замера задачи (41 против 26)."""
    record = wt_record(stale_guard_copies=41)
    violations = ri.check_worktree_cleanup_records([record], 26, utc(2026, 9, 14, 12, 0))
    assert violations == [{
        "kind": "stale-guard-copies", "stale_guard_copies": 41, "open_prs": 26,
        "last_ts": "2026-09-14T11:00:00+00:00",
    }]
    at_boundary = wt_record(stale_guard_copies=26)
    assert ri.check_worktree_cleanup_records([at_boundary], 26, utc(2026, 9, 14, 12, 0)) == []


def test_worktree_cleanup_records_zero_removed_while_stuck():
    """Основной симптом инцидента #1250: removed=0 при запертых деревьях
    старше retention — нарушение; removed>0 или без запертых — нет."""
    incident = wt_record(removed=0, stuck_old_total=45)
    violations = ri.check_worktree_cleanup_records([incident], 26, utc(2026, 9, 14, 12, 0))
    assert violations == [{
        "kind": "zero-removed-stuck", "removed": 0, "total": 76,
        "stuck_old_total": 45, "last_ts": "2026-09-14T11:00:00+00:00",
    }]
    healthy = wt_record(removed=28)
    assert ri.check_worktree_cleanup_records([healthy], 26, utc(2026, 9, 14, 12, 0)) == []
    nothing_stuck = wt_record(removed=0, stuck_old_total=0)
    assert ri.check_worktree_cleanup_records([nothing_stuck], 26, utc(2026, 9, 14, 12, 0)) == []


def test_worktree_cleanup_records_bad_record_is_violation_not_crash():
    broken = {"ts": "не-время", "total": 1}
    violations = ri.check_worktree_cleanup_records([broken], 26, utc(2026, 9, 14, 12, 0))
    assert len(violations) == 1 and violations[0]["kind"] == "bad-record"


def test_fetch_worktree_cleanup_records_404_is_empty(monkeypatch):
    patch_gh(monkeypatch, FakeGh({}))  # дефолтный маршрут — 404, штатное «записей не было»
    assert ri.fetch_worktree_cleanup_records("mytab0r/edge-harness") == []


def test_build_report_wires_invariant_24(monkeypatch):
    """Прод-форма Contents API → находка [24] в отчёте; 404 — отдельная
    🚨 «записей нет НИ РАЗУ», не 💚."""
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        # 26 открытых PR, 41 устаревшая копия — числа из замера задачи #1250.
        "pulls?state=open": [open_pr(n) for n in range(26)],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "contents/docs/research/data/worktree-cleanup.jsonl":
            worktree_snapshot_contents_response([wt_record(stale_guard_copies=41)]),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report("mytab0r/edge-harness", utc(2026, 9, 14, 12, 0))
    assert [v["kind"] for v in findings[24]] == ["stale-guard-copies"]
    assert any("🚨" in line and "[24]" in line and "41" in line for line in lines)

    fake404 = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
    })
    patch_gh(monkeypatch, fake404)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines404, findings404 = ri.build_report("mytab0r/edge-harness", utc(2026, 9, 14, 12, 0))
    assert [v["kind"] for v in findings404[24]] == ["no-records"]
    assert any("🚨" in line and "[24]" in line and "НИ РАЗУ" in line for line in lines404)


def test_build_report_invariant_24_healthy_line_carries_numbers(monkeypatch):
    fake = FakeGh({
        f"issues?state=open&labels={ri.TASK_LABEL}": [],
        "pulls?state=closed": [],
        "pulls?state=open": [],
        "graphql": graphql_pool_page(),
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": {"workflow_runs": []},
        "search/issues": {"items": []},
        "contents/docs/research/data/worktree-cleanup.jsonl":
            worktree_snapshot_contents_response([wt_record(total=0, removed=0, kept=0,
                                                           guard_copies=0,
                                                           stale_guard_copies=0,
                                                           stuck_old_total=0)]),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    lines, findings = ri.build_report("mytab0r/edge-harness", utc(2026, 9, 14, 12, 0))
    assert findings[24] == []
    healthy = [line for line in lines if "[24]" in line and "💚" in line]
    assert healthy and "total=0" in healthy[0], \
        "здоровая строка обязана различать «деревьев нет» от общего «здорово»"


def test_inspector_runs_its_checks_inside_the_read_cache(monkeypatch):
    """Механизм без включения — это выключенный механизм (#1483). Проверяется
    ФАКТ: во время работы проверок кэш включён, а после возврата из main() —
    нет. Вырезанное включение прошло бы текстовый разбор молча.

    `_run_checks` подменён — здесь проверяется обвязка main(), а не сами
    инварианты: их поведение держат 249 соседних тестов."""
    inside = []
    monkeypatch.setattr(ri, "_run_checks",
                        lambda: inside.append(ri.pulse_guard.read_cache_size() == 0
                                              and _cache_is_on()) or 0)

    def _cache_is_on() -> bool:
        # Пустой словарь — кэш включён и ещё ничего не прочитал; None — выключен.
        return ri.pulse_guard._READ_CACHE is not None

    assert ri.main() == 0
    assert inside == [True], "проверки бежали вне кэша — бюджет тратится на повторы"
    assert ri.pulse_guard._READ_CACHE is None, "кэш пережил main() — утечёт в чужой прогон"
