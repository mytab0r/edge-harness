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
import re
from datetime import datetime, timezone
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

    Запасной маршрут для комментариев #120 (инвариант 13, #899): здоровый
    дефолт — пустой список (маркеров серии конвейера нет). Без него КАЖДЫЙ
    существующий тест build_report был бы обязан завести собственный
    маршрут issues/120/comments, хотя фантомная пауза конвейера — не их
    предмет; тест, которому нужны конкретные маркеры, переопределяет этот
    маршрут явно (уже так делают тесты #196/#220 выше)."""
    _DEFAULT_ROUTES = {
        "actions/workflows/ai-review.yml/runs": {"workflow_runs": []},
        "contents/docs/research/data/pipeline-health.jsonl": RuntimeError(
            "gh api repos/o/r/contents/...: HTTP 404: Not Found"),
        "issues/120/comments": [],
    }

    def __init__(self, routes):
        self.routes = {**self._DEFAULT_ROUTES, **routes}
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
    """Единая точка патча — оба модуля читают gh() по имени (repo_invariants
    реэкспортирует pulse_guard.gh как свой атрибут `ri.gh`, но
    post_issue_comment/escalate внутри pulse_guard.py вызывают СВОЙ
    module-level `gh`, а не `ri.gh`). Патчить только `ri.gh` недостаточно —
    так один прогон реально ушёл в живой issue #120 (инцидент этой задачи,
    #244: очищено вручную, gh api -X DELETE .../comments/5527288512).
    Патчим оба имени — тот же приём, что test_scheduler.py::patch_gh."""
    monkeypatch.setattr(ri, "gh", fake)
    monkeypatch.setattr(ri.pulse_guard, "gh", fake)


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
    violations = ri.check_wasted_ai_review_runs(REPO, [pull])
    assert len(violations) == 1
    assert violations[0]["pr"] == 333
    assert violations[0]["wasted_runs"][0]["run_id"] == 34193569472


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
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == []


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
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == []


def test_wasted_ai_review_silent_without_final_verdict_label():
    # Нет ai:ok/ai:changes-requested — сравнивать не с чем, инвариант не
    # обязан идти в сеть вовсе (нет маршрута в FakeGh — упадёт сам, если
    # код полезет в comments без нужды).
    fake = FakeGh({})
    pull = open_pr(333, labels=["review:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == []
    assert fake.calls == []


def test_wasted_ai_review_silent_when_ai_failed_not_final(monkeypatch):
    # ai:failed — не финальный вердикт (газ #196, автоповтор), инвариант не
    # трогает такой PR вовсе, даже если отпечаток совпал бы.
    fake = FakeGh({})
    pull = open_pr(333, labels=["review:ok", "ai:failed"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == []
    assert fake.calls == []


def test_wasted_ai_review_silent_without_verdict_comment(monkeypatch):
    fake = FakeGh({"issues/333/comments": []})
    patch_gh(monkeypatch, fake)
    pull = open_pr(333, labels=["ai:ok"])
    assert ri.check_wasted_ai_review_runs(REPO, [pull]) == []


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
    violations = ri.check_recurring_worker_failure(REPO)
    assert len(violations) == 1
    assert violations[0]["streak"] == 3
    assert violations[0]["error_text"] == f"##[error]{SESSION_LACKS_ID_ERROR}"
    assert violations[0]["since"] == "2026-09-08T08:45:00Z"
    assert violations[0]["until"] == "2026-09-09T00:00:00Z"


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
    assert ri.check_recurring_worker_failure(REPO) == []


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
    assert ri.check_recurring_worker_failure(REPO) == []


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
    assert ri.check_recurring_worker_failure(REPO) == []
    assert not any("actions/runs/3/jobs" in call or "actions/runs/2/jobs" in call for call in fake.calls)


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
    return {"created_at": when, "body": ri.pulse_guard.PAUSE_MARKER}


def probe_marker_comment(when: str, attempt: int) -> dict:
    return {"created_at": when,
            "body": ri.pulse_guard.probe_alert_text(
                attempt, ri.pulse_guard.probe_backoff_minutes(attempt), None, "err")}


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
    violations = ri.check_conveyor_gate_phantom_pause(REPO)
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
    assert ri.check_conveyor_gate_phantom_pause(REPO) == []


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
    assert ri.check_conveyor_gate_phantom_pause(REPO) == []


def test_phantom_pause_silent_when_head_run_still_in_progress(monkeypatch):
    """Здоровое состояние (issue #899, п.2): голова списка ещё выполняется
    (conclusion=None) — законное объяснение неопределённости, не фантом."""
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
    assert ri.check_conveyor_gate_phantom_pause(REPO) == []


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
    assert ri.check_conveyor_gate_phantom_pause(REPO) == []


def test_phantom_pause_best_effort_on_network_failure(monkeypatch):
    """Сеть недоступна — best-effort [] (тот же принцип, что у 10/12): отказ
    инфраструктуры не должен ронять весь build_report ради инварианта, у
    которого и так нет действия жёстче наблюдения."""
    fake = FakeGh({
        f"workflows/{ri.RECURRING_FAILURE_WORKFLOW}/runs": RuntimeError("gh api: 502"),
    })
    patch_gh(monkeypatch, fake)
    assert ri.check_conveyor_gate_phantom_pause(REPO) == []


def test_phantom_pause_not_in_ci_gating():
    """Долг на живом репозитории ещё не измерен (тот же порядок, что у
    1/5/9/10/12) — наблюдательный, не гейтящий."""
    assert 13 not in ri.CI_GATING


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
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(ri, "OPENSPEC_CHANGES", Path("/nonexistent-openspec-changes"))
    now = utc(2026, 9, 10, 12, 0)
    lines, findings = ri.build_report("mytab0r/edge-harness", now)
    assert len(findings[13]) == 1
    assert any("🚨" in line and "[13]" in line and "0 реальных" in line for line in lines)
