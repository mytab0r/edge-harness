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
from datetime import datetime, timezone
from pathlib import Path

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
    → прод-форма ответа; фиксирует вызовы для гвардии холостого хода."""

    def __init__(self, routes):
        self.routes = routes
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
    Перенос бюджета между эпохами — класс #431/PR #439 (не слит)."""
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
    assert "исчерпан СТАРОЙ эпохой (3/3, в текущей — 0/3)" in line
    assert "перенос бюджета между эпохами" in line
    assert "вердикт был — ai:ok" in line


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
    assert "3/3, в текущей — 0/3" in line


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
    assert "СТАРОЙ эпохой" not in line_same
    assert "СТАРОЙ эпохой" in line_carried


def test_stuck_gate_fact_line_budget_not_exhausted():
    item = {
        "pr": 2, "age_minutes": 150.0, "labeled_at": "2026-09-06T00:00:00+00:00",
        "attempts_total": 1, "attempts_in_epoch": 1, "attempts_limit": 3,
        "last_attempt_at": "2026-09-06T01:00:00+00:00", "verdict_ever": None,
    }
    line = ri.stuck_gate_fact_line(item)
    assert "не исчерпан (1/3)" in line
    assert "ближайшем тике" in line


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
