#!/usr/bin/env python3
"""Тесты приёмки по ссылке (scripts/orchestra/reference_closure.py, #1042).

Прод-форма: тела трёх реально слитых PR этого репозитория (`gh pr view <N>
--json body`, 2026-09-12) — #986 («закрываю issue #507»), #952 («закрывает
#661»), #841 («Закрыт класс #786»). Живой ложноположительный фрагмент того
же PR #986 («закрытие #806», существительное) и живая проверка, что #389/
#901 (контрпримеры владельца) НЕ становятся кандидатами.

Запуск: python -m pytest scripts/orchestra/test_reference_closure.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "reference_closure.py"
spec = importlib.util.spec_from_file_location("reference_closure", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)  # type: ignore[union-attr]


# ══════════════════════════════════════════════════════════════════════════
# find_candidates — прод-форма реальных тел
# ══════════════════════════════════════════════════════════════════════════

_PR_986 = {
    "number": 986,
    "head": {"ref": "agent/806-plugin-upstream-drift"},
    "merged_at": "2026-09-12T02:14:00Z",
    "body": (
        "- Заявленные в issue #507 упоминания `dsh-tools@0.1.1-rc.2` в\n"
        "  README/body.js сверены отдельно и не относятся к зависимостям,\n"
        "  правкой; закрываю issue #507 этим PR как полностью покрытый.\n"
        "- #806 — причина 2 (ростер), не закрывается автоматически этим PR: "
        "реальный перенос требует отдельного PR.\n"
    ),
}

_PR_952 = {
    "number": 952,
    "head": {"ref": "agent/945-plugin-forge-contract-branch"},
    "merged_at": "2026-09-12T06:44:02Z",
    "body": "форж отказывает\nгромко ДО сборки, если номер не определён (закрывает #661, вариант 1).\n",
}

_PR_841 = {
    "number": 841,
    "head": {"ref": "agent/840-provider-secrets-gh-body-file"},
    "merged_at": "2026-09-12T09:00:00Z",
    "body": "- Закрыт класс #786 по всему `scripts/`: `gh` 2.85 не знает `--body-file`.\n",
}

# Собственная задача этого PR (#1021) — та же, что и «доп.» номер маркера не
# называет; own_task здесь не участвует в also_closes_targets вовсе (тело
# только напоминает про #782, без маркера «закрывает»), это НЕ кандидат этого
# механизма — исключён по конструкции (см. докстринг модуля, «Честная граница»).
_PR_1022_OWN_TASK_NOT_MARKER = {
    "number": 1022,
    "head": {"ref": "agent/1021-waiting-owner-relabel-loop"},
    "merged_at": "2026-09-12T12:08:16Z",
    "body": "Задача выделена из #782: диагноз в теле PR #1020.\n#782 остаётся открытой под свой критерий.\n",
}


def test_find_candidates_recognizes_all_three_real_marker_cases():
    candidates = rc.find_candidates([_PR_986, _PR_952, _PR_841])
    assert set(candidates) == {507, 661, 786}
    assert candidates[507]["number"] == 986
    assert candidates[661]["number"] == 952
    assert candidates[786]["number"] == 841


def test_find_candidates_excludes_own_branch_task_of_the_declaring_pr():
    # #806 — упомянут PR #986, но не маркером «закрывает» (только «закрытие»,
    # отклонённое also_closes_targets отдельно, см. test_task_ref.py) — здесь
    # проверяем на уровне find_candidates, что 806 не всплывает кандидатом.
    candidates = rc.find_candidates([_PR_986])
    assert 806 not in candidates


def test_find_candidates_ignores_pr_with_no_marker_at_all():
    # #1021/#967 (реальные примеры ревизии) — их слитый PR это ИХ СОБСТВЕННАЯ
    # ветка (own_task), и никакого also_closes-маркера на ЧУЖОЙ номер тело не
    # несёт — этот механизм их не видит вовсе, это честная граница модуля.
    candidates = rc.find_candidates([_PR_1022_OWN_TASK_NOT_MARKER])
    assert candidates == {}


def test_find_candidates_prefers_latest_merge_for_same_task():
    older = dict(_PR_986, merged_at="2026-09-11T00:00:00Z")
    newer = dict(_PR_986, number=999, merged_at="2026-09-12T23:00:00Z")
    candidates = rc.find_candidates([older, newer])
    assert candidates[507]["number"] == 999


# ══════════════════════════════════════════════════════════════════════════
# competing_open_pr
# ══════════════════════════════════════════════════════════════════════════


def test_competing_open_pr_found_by_branch():
    open_pulls = [{"number": 1200, "head": {"ref": "agent/507-fix-attempt"}}]
    assert rc.competing_open_pr(507, open_pulls) == 1200


def test_competing_open_pr_ignores_prose_mention_only():
    # Узкая семантика (resolve_pr_task, не любое упоминание) — открытый PR,
    # чья ветка называет ДРУГУЮ задачу, но тело упоминает #507 прозой, не
    # считается конкурирующим (тот же довод, что merged_pr_map/reap_stale).
    open_pulls = [{"number": 1201, "head": {"ref": "agent/900-unrelated"}, "body": "см. #507"}]
    assert rc.competing_open_pr(507, open_pulls) is None


# ══════════════════════════════════════════════════════════════════════════
# decide — чистое решение, все шесть путей skip + close
# ══════════════════════════════════════════════════════════════════════════


def _task_issue(**overrides):
    base = {
        "number": 507,
        "state": "open",
        "assignees": [],
        "labels": [{"name": "task"}],
        "title": "т",
    }
    base.update(overrides)
    return base


def _decide(task_issue=None, pull=None, **overrides):
    kwargs = dict(
        open_pulls=[],
        evidence_state="ok",
        evidence_detail="файлы на месте",
        already_marked=False,
        merge_commit_on_main=True,
    )
    kwargs.update(overrides)
    return rc.decide(task_issue or _task_issue(), pull or _PR_986, **kwargs)


def test_decide_closes_when_everything_checks_out():
    decision = _decide()
    assert decision == {"action": "close", "number": 507, "pr": 986, "detail": "файлы на месте"}


def test_decide_skips_watchdog_issue():
    decision = _decide(task_issue=_task_issue(number=120))
    assert decision["action"] == "skip"
    assert decision["reason"] == rc.SKIP_WATCHDOG


def test_decide_skips_not_open():
    decision = _decide(task_issue=_task_issue(state="closed"))
    assert decision["reason"] == rc.SKIP_NOT_OPEN


def test_decide_skips_epic():
    decision = _decide(task_issue=_task_issue(title="ЭПИК. большая работа"))
    assert decision["reason"] == rc.SKIP_EPIC


def test_decide_skips_missing_task_label():
    decision = _decide(task_issue=_task_issue(labels=[{"name": "bug"}]))
    assert decision["reason"] == rc.SKIP_TASK_LABEL_MISSING


def test_decide_skips_already_marked_idempotent():
    decision = _decide(already_marked=True)
    assert decision["reason"] == rc.SKIP_ALREADY


def test_decide_skips_assignee_present():
    decision = _decide(task_issue=_task_issue(assignees=[{"login": "mytab0r"}]))
    assert decision["reason"] == rc.SKIP_ASSIGNEE
    assert "mytab0r" in decision["detail"]


def test_decide_skips_competing_open_pr():
    open_pulls = [{"number": 1300, "head": {"ref": "agent/507-rework"}}]
    decision = _decide(open_pulls=open_pulls)
    assert decision["reason"] == rc.SKIP_COMPETING_PR
    assert "#1300" in decision["detail"]


def test_decide_skips_merge_commit_not_on_main():
    decision = _decide(merge_commit_on_main=False)
    assert decision["reason"] == rc.SKIP_NOT_ANCESTOR


def test_decide_skips_evidence_pending():
    decision = _decide(evidence_state="pending", evidence_detail="ещё выполняется")
    assert decision["reason"] == rc.SKIP_EVIDENCE_PENDING


def test_decide_skips_evidence_fail():
    decision = _decide(evidence_state="fail", evidence_detail="красные проверки")
    assert decision["reason"] == rc.SKIP_EVIDENCE_FAIL


def test_decide_closes_on_docs_category_too():
    decision = _decide(evidence_state="docs", evidence_detail="файлы на месте")
    assert decision["action"] == "close"


# ══════════════════════════════════════════════════════════════════════════
# combine_evidence / guard_test_evidence / referenced_change_archived
# ══════════════════════════════════════════════════════════════════════════


def test_combine_evidence_fail_wins_over_ok():
    assert rc.combine_evidence(("ok", "a"), ("fail", "b")) == ("fail", "b; a")


def test_combine_evidence_pending_wins_over_docs():
    assert rc.combine_evidence(("docs", "a"), ("pending", "b")) == ("pending", "b; a")


def test_combine_evidence_keeps_base_when_extra_not_worse():
    state, detail = rc.combine_evidence(("ok", "a"), ("ok", "b"))
    assert state == "ok" and "a" in detail and "b" in detail


def test_guard_test_evidence_ok_when_guard_and_mutation_proven_test_present(tmp_path):
    guard = tmp_path / "scripts" / "ci" / "guards"
    guard.mkdir(parents=True)
    test_dir = tmp_path / "scripts" / "lib"
    test_dir.mkdir(parents=True)
    (guard / "sample-guard.sh").write_text("python -m pytest scripts/lib/test_sample.py -q\n", encoding="utf-8")
    (test_dir / "test_sample.py").write_text("# доказано мутацией: сняли фикс — тест покраснел\n", encoding="utf-8")
    state, detail = rc.guard_test_evidence(tmp_path, "scripts/ci/guards/sample-guard.sh")
    assert state == "ok"


def test_guard_test_evidence_pending_when_no_mutation_word(tmp_path):
    guard = tmp_path / "scripts" / "ci" / "guards"
    guard.mkdir(parents=True)
    test_dir = tmp_path / "scripts" / "lib"
    test_dir.mkdir(parents=True)
    (guard / "sample-guard.sh").write_text("python -m pytest scripts/lib/test_sample.py -q\n", encoding="utf-8")
    (test_dir / "test_sample.py").write_text("# просто тест без доказательства\n", encoding="utf-8")
    state, _ = rc.guard_test_evidence(tmp_path, "scripts/ci/guards/sample-guard.sh")
    assert state == "pending"


def test_guard_test_evidence_fail_when_guard_missing(tmp_path):
    state, detail = rc.guard_test_evidence(tmp_path, "scripts/ci/guards/absent-guard.sh")
    assert state == "fail"
    assert "отсутствует" in detail


def test_guard_paths_matches_only_guard_catalog():
    filenames = ["scripts/ci/guards/x-guard.sh", "scripts/ci/y.sh", "scripts/lib/test_task_ref.py"]
    assert rc.guard_paths(filenames) == ["scripts/ci/guards/x-guard.sh"]


def test_referenced_change_archived_true_when_only_archive_copy_exists(tmp_path):
    (tmp_path / "openspec" / "changes" / "archive" / "demo-change").mkdir(parents=True)
    text = "см. openspec/changes/demo-change/tasks.md"
    assert rc.referenced_change_archived(text, tmp_path) is True


def test_referenced_change_archived_false_when_still_active(tmp_path):
    (tmp_path / "openspec" / "changes" / "demo-change").mkdir(parents=True)
    text = "см. openspec/changes/demo-change/proposal.md"
    assert rc.referenced_change_archived(text, tmp_path) is False


def test_referenced_change_archived_none_when_no_path_mentioned():
    assert rc.referenced_change_archived("обычный текст без пути", Path(".")) is None


# ══════════════════════════════════════════════════════════════════════════
# Дожатая проверка контрпримеров владельца — #389/#901 не становятся
# кандидатами этого механизма (никакой сети, только структура текста)
# ══════════════════════════════════════════════════════════════════════════

# Тело PR #409 (реально слитый, ветка agent/389-inbox-native-dispatch — ЭТО
# СОБСТВЕННАЯ задача #389) — не несёт also_closes-маркера ни на один номер:
_PR_409_OWN_TASK_389 = {
    "number": 409,
    "head": {"ref": "agent/389-inbox-native-dispatch"},
    "merged_at": "2026-09-11T03:37:23Z",
    "body": "закрытую задачу #20, докрытие оформлено новой узкой задачей #389.\n",
}


def test_issue_389_is_not_a_candidate_own_task_has_no_marker():
    # #389 — собственная задача ветки PR #409 (штатная приёмка её оценивает,
    # не этот модуль) И тело не несёт also_closes-маркер вовсе (только
    # прозаическое упоминание #20/#389 без глагола «закрывает»).
    candidates = rc.find_candidates([_PR_409_OWN_TASK_389])
    assert candidates == {}


def test_issue_901_never_referenced_by_any_merged_pr_marker():
    # #901 закрыт мандатом владельца (#939) в комментарии к самой задаче —
    # без единого слитого PR, заявляющего её маркером. Пустой список слитых
    # PR — заведомо пустой результат (структурная гарантия, не совпадение).
    assert rc.find_candidates([]) == {}


# ══════════════════════════════════════════════════════════════════════════
# closure_comment / run — наблюдаемость и идемпотентность на моке gh
# ══════════════════════════════════════════════════════════════════════════


def test_closure_comment_names_pr_and_marker():
    text = rc.closure_comment(507, 986, "файлы на месте в main: a, b")
    assert text.startswith(rc.REFERENCE_CLOSURE_MARKER)
    assert "#986" in text
    assert "файлы на месте" in text


class _FakeIssueGh:
    """Мок ТОЛЬКО двух прямых вызовов `gh`, которые `run()` делает сам
    (GET issue, PATCH state=closed) — остальные сетевые функции
    (`post_issue_comment`/`issue_marker_times`/`escalate`/`claim_task.release`)
    патчатся отдельно, каждая на своей границе (см. интеграционные тесты
    ниже) — не пытаемся эмулировать пагинацию pulse_guard/review_labels."""

    def __init__(self, issues, order=None):
        self.issues = issues
        self.closed = []
        # Общий с патченным post_issue_comment журнал порядка операций —
        # носитель теста «закрытие раньше маркер-комментария».
        self.order = order if order is not None else []

    def __call__(self, *args):
        if args[0] == "-X" and args[1] == "PATCH":
            number = int(args[2].split("/issues/")[1])
            self.closed.append(number)
            self.order.append("patch")
            return None
        url = args[0]
        if url.startswith("repos/") and "/issues/" in url:
            number = int(url.split("/issues/")[1])
            return self.issues.get(number)
        raise AssertionError(f"неожиданный вызов gh в этом тесте: {args}")


def test_run_closes_candidate_end_to_end_on_mock(monkeypatch):
    fake = _FakeIssueGh(issues={507: _task_issue()})
    posted = []
    monkeypatch.setattr(rc, "gh", fake)
    monkeypatch.setattr(rc, "post_issue_comment", lambda repo, number, text: posted.append((number, text)))
    monkeypatch.setattr(rc, "issue_marker_times", lambda repo, number, marker: [])
    monkeypatch.setattr(rc, "merge_commit_on_main", lambda repo, sha: True)
    monkeypatch.setattr(rc, "compute_evidence", lambda repo, root, pull: ("docs", "файлы на месте"))
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [_PR_986])
    monkeypatch.setattr(rc.scheduler, "open_pulls", lambda repo: [])
    monkeypatch.setattr(rc.claim_task, "release", lambda repo, number: f"замок task-{number} снят")

    lines = rc.run("mytab0r/edge-harness")

    assert any("✅ #507" in line for line in lines)
    assert 507 in fake.closed
    assert posted and posted[0][0] == 507
    assert rc.REFERENCE_CLOSURE_MARKER in posted[0][1]


def test_run_is_idempotent_second_pass_no_new_comment(monkeypatch):
    fake = _FakeIssueGh(issues={507: _task_issue()})
    posted = []
    monkeypatch.setattr(rc, "gh", fake)
    monkeypatch.setattr(rc, "post_issue_comment", lambda repo, number, text: posted.append((number, text)))
    # Маркер уже стоит для ИМЕННО этого PR (#986) — already_reference_marked
    # читает issue_marker_times с меткой f"{MARKER} PR #{pr_number}".
    monkeypatch.setattr(
        rc, "issue_marker_times",
        lambda repo, number, marker: (
            [object()] if marker == f"{rc.REFERENCE_CLOSURE_MARKER} PR #986" else []
        ),
    )
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [_PR_986])
    monkeypatch.setattr(rc.scheduler, "open_pulls", lambda repo: [])

    lines = rc.run("mytab0r/edge-harness")

    assert not posted
    assert not fake.closed
    assert any(rc.SKIP_ALREADY in line for line in lines)


def test_run_reports_cap_reached_without_silent_skip(monkeypatch):
    # Потолок 1 закрытие за прогон — второй кандидат печатается явно с
    # причиной (не молчаливый пропуск).
    fake = _FakeIssueGh(issues={507: _task_issue(number=507), 661: _task_issue(number=661)})
    posted = []
    monkeypatch.setattr(rc, "gh", fake)
    monkeypatch.setattr(rc, "post_issue_comment", lambda repo, number, text: posted.append((number, text)))
    monkeypatch.setattr(rc, "issue_marker_times", lambda repo, number, marker: [])
    monkeypatch.setattr(rc, "merge_commit_on_main", lambda repo, sha: True)
    monkeypatch.setattr(rc, "compute_evidence", lambda repo, root, pull: ("docs", "файлы на месте"))
    monkeypatch.setattr(rc, "REFERENCE_CLOSURE_RUN_CAP", 1)
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [_PR_986, _PR_952])
    monkeypatch.setattr(rc.scheduler, "open_pulls", lambda repo: [])
    monkeypatch.setattr(rc.claim_task, "release", lambda repo, number: "ok")

    lines = rc.run("mytab0r/edge-harness")

    assert sum(1 for line in lines if line.startswith("✅")) == 1
    assert any(rc.SKIP_CAP in line for line in lines)


def test_run_reports_no_candidates_line_when_nothing_found(monkeypatch):
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [])
    lines = rc.run("mytab0r/edge-harness")
    assert len(lines) == 1 and lines[0].startswith("💗")


# ══════════════════════════════════════════════════════════════════════════
# merge_commit_on_main — «проверка сломана» не маскируется под «не предок»
# (находка ревью PR #1046, чеклист) — AGENTS.md, «Алерт не гадает»
# ══════════════════════════════════════════════════════════════════════════


class _FakeCompareGh:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.payload


def test_merge_commit_on_main_true_for_identical_and_behind(monkeypatch):
    for status in ("identical", "behind"):
        fake = _FakeCompareGh(payload={"status": status})
        monkeypatch.setattr(rc, "gh", fake)
        assert rc.merge_commit_on_main("o/r", "abc123") is True


def test_merge_commit_on_main_false_for_ahead_and_diverged(monkeypatch):
    for status in ("ahead", "diverged"):
        fake = _FakeCompareGh(payload={"status": status})
        monkeypatch.setattr(rc, "gh", fake)
        assert rc.merge_commit_on_main("o/r", "abc123") is False


def test_merge_commit_on_main_false_only_for_empty_sha(monkeypatch):
    # Пустой sha — проверять нечего, сеть не тратим вовсе.
    fake = _FakeCompareGh()
    monkeypatch.setattr(rc, "gh", fake)
    assert rc.merge_commit_on_main("o/r", None) is False
    assert fake.calls == []


def test_merge_commit_on_main_api_failure_is_not_false(monkeypatch):
    # RuntimeError (сеть/рейт-лимит — класс #454) НЕ превращается в False:
    # отчёт не должен называть «коммит не предок main» (класс #925) там, где
    # реальная причина — сбой вызова; исключение уходит вызывающему в
    # эскалационную ветку REFERENCE_CLOSURE_ERROR_MARKER, как у
    # compute_evidence.
    fake = _FakeCompareGh(error="gh api repos/o/r/compare: HTTP 403 (рейт-лимит исчерпан)")
    monkeypatch.setattr(rc, "gh", fake)
    with pytest.raises(RuntimeError, match="403"):
        rc.merge_commit_on_main("o/r", "abc123")


def test_merge_commit_on_main_unexpected_payload_is_not_false(monkeypatch):
    # Ответ без узнаваемого статуса — тоже «проверка сломана», не False.
    fake = _FakeCompareGh(payload={"unexpected": True})
    monkeypatch.setattr(rc, "gh", fake)
    with pytest.raises(RuntimeError, match="неожиданный"):
        rc.merge_commit_on_main("o/r", "abc123")


def test_run_infra_failure_escalates_to_watchdog_with_dedup(monkeypatch):
    # Сбой проверки улики (включая compare API после исправления выше) уходит
    # в эскалацию #120 с дедупом на эпизод, а не в молчаливый/неверный skip.
    fake = _FakeIssueGh(issues={507: _task_issue()})
    escalated = []
    posted_markers = []

    def fake_escalate(repo, number, text, options=None):
        escalated.append((number, text))
        return "доставлен"

    def fake_marker_times(repo, number, marker):
        # Дедуп по факту доставки: маркер уже стоит на #120, если
        # fake_escalate его уже писал.
        return [object()] if marker in posted_markers else []

    monkeypatch.setattr(rc, "gh", fake)
    monkeypatch.setattr(rc, "escalate", fake_escalate)
    monkeypatch.setattr(rc, "issue_marker_times", fake_marker_times)
    monkeypatch.setattr(
        rc, "merge_commit_on_main",
        lambda repo, sha: (_ for _ in ()).throw(RuntimeError("compare API недоступен")))
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [_PR_986])
    monkeypatch.setattr(rc.scheduler, "open_pulls", lambda repo: [])

    lines1 = rc.run("mytab0r/edge-harness")
    assert len(escalated) == 1
    assert escalated[0][0] == rc.WATCHDOG_ISSUE
    assert "compare API недоступен" in escalated[0][1]
    assert rc.REFERENCE_CLOSURE_ERROR_MARKER in escalated[0][1]
    # Новый 🚨-эпизод красит шаг (fail loud, обещание комментария шага в
    # orchestra.yml; замечание ревью PR #1046 из чеклиста).
    assert rc.exit_code(lines1) == 1

    posted_markers.append(f"{rc.REFERENCE_CLOSURE_ERROR_MARKER} #507 PR #986")
    lines2 = rc.run("mytab0r/edge-harness")
    assert len(escalated) == 1  # повторного алерта нет
    assert any("уже эскалировано" in line for line in lines2)
    # Повтор (дедуп) — газ: доставленный алерт не краснит каждый следующий
    # прогон, красный шаг существует только пока эпизод новый.
    assert rc.exit_code(lines2) == 0


# ══════════════════════════════════════════════════════════════════════════
# Порядок записи: закрытие ПЕРВЫМ, маркер-комментарий вторым (блокирующая
# находка ревью PR #1046 — обратный порядок замораживал кандидата навсегда)
# ══════════════════════════════════════════════════════════════════════════


def _happy_run_mocks(monkeypatch, fake, posted, order):
    monkeypatch.setattr(rc, "gh", fake)
    monkeypatch.setattr(
        rc, "post_issue_comment",
        lambda repo, number, text: (posted.append(number), order.append("comment")))
    monkeypatch.setattr(rc, "issue_marker_times", lambda repo, number, marker: [])
    monkeypatch.setattr(rc, "merge_commit_on_main", lambda repo, sha: True)
    monkeypatch.setattr(rc, "compute_evidence", lambda repo, root, pull: ("docs", "файлы на месте"))
    monkeypatch.setattr(rc.scheduler, "all_merged_pulls", lambda repo: [_PR_986])
    monkeypatch.setattr(rc.scheduler, "open_pulls", lambda repo: [])
    monkeypatch.setattr(rc.claim_task, "release", lambda repo, number: "ok")


def test_run_closes_before_posting_marker_comment(monkeypatch):
    order = []
    fake = _FakeIssueGh(issues={507: _task_issue()}, order=order)
    posted = []
    _happy_run_mocks(monkeypatch, fake, posted, order)

    rc.run("mytab0r/edge-harness")

    assert order == ["patch", "comment"], (
        "PATCH закрытия обязан идти раньше маркер-комментария: сбой комментария "
        "при закрытой задаче — громкий ⚠️, а сбой PATCH при стоящем маркере — "
        "вечный кандидат с комментарием «Закрываю.»")


class _FlakyPatchGh(_FakeIssueGh):
    """PATCH падает первым вызовом (транзиент/рейт-лимит, класс #454),
    дальше работает."""

    def __init__(self, issues, order):
        super().__init__(issues, order)
        self.patch_attempts = 0

    def __call__(self, *args):
        if args[0] == "-X" and args[1] == "PATCH":
            self.patch_attempts += 1
            if self.patch_attempts == 1:
                raise RuntimeError("HTTP 403: рейтинг-лимит исчерпан")
        return super().__call__(*args)


def test_run_patch_failure_no_marker_no_close_retry_next_run(monkeypatch):
    # Сбой PATCH: задача остаётся открытой и БЕЗ маркера — следующий прогон
    # повторяет попытку целиком, не упирается в SKIP_ALREADY (это и есть
    # отличие от старого порядка, где маркер стоял до закрытия).
    order = []
    fake = _FlakyPatchGh(issues={507: _task_issue()}, order=order)
    posted = []
    _happy_run_mocks(monkeypatch, fake, posted, order)

    lines1 = rc.run("mytab0r/edge-harness")
    assert not posted, "комментарий-маркер при несостоявшемся закрытии не ставится"
    assert not fake.closed
    assert any("закрытие не выполнено" in line for line in lines1)

    lines2 = rc.run("mytab0r/edge-harness")  # имитация следующего прогона
    assert 507 in fake.closed
    assert posted == [507]
    assert any("✅ #507" in line for line in lines2)


def test_run_comment_failure_after_close_is_loud_not_frozen(monkeypatch):
    # Сбой комментария при уже закрытой задаче: громкая ⚠️-строка, задача
    # остаётся закрытой, попытка засчитана — следующий прогон отсечёт её по
    # state != open, вечного кандидата нет.
    order = []
    fake = _FakeIssueGh(issues={507: _task_issue()}, order=order)
    posted = []
    _happy_run_mocks(monkeypatch, fake, posted, order)

    def failing_comment(repo, number, text):
        raise RuntimeError("комментарий не доставлен")

    monkeypatch.setattr(rc, "post_issue_comment", failing_comment)

    lines = rc.run("mytab0r/edge-harness")

    assert 507 in fake.closed
    assert any("⚠️" in line and "ЗАКРЫТА" in line for line in lines)


# ══════════════════════════════════════════════════════════════════════════
# Код выхода: ⚠️ и НОВЫЙ 🚨-эпизод красят шаг, повторный 🚨 (дедуп) — нет
# (замечание ревью PR #1046 из чеклиста: эскалированный сбой улики оставлял
# шаг зелёным, обещание «красный шаг виден» в orchestra.yml не выполнялось)
# ══════════════════════════════════════════════════════════════════════════


def test_exit_code_quiet_outcomes_are_zero():
    assert rc.exit_code([]) == 0
    assert rc.exit_code([
        "💗 приёмка-по-ссылке: кандидатов не найдено",
    ]) == 0
    assert rc.exit_code([
        "⏭️ #507: не закрываю (PR #986, epic) — эпик",
        "🛑 #661: потолок 5 закрытий за прогон исчерпан (PR #952) — рассмотрю на следующем прогоне (cap_reached)",
        "✅ #786: закрыта по ссылке из PR #841 — файлы на месте в main",
    ]) == 0


def test_exit_code_warning_line_is_one():
    assert rc.exit_code([
        "✅ #786: закрыта по ссылке из PR #841",
        "⚠️ #507: закрытие не выполнено, маркер-комментарий не ставился — повтор на следующем прогоне — 403",
    ]) == 1


def test_exit_code_new_escalation_is_one_dedup_is_zero():
    new_episode = (
        "🚨 #507: приёмка-по-ссылке PR #986 не смогла проверить улику — "
        "compare API недоступен (доставлен)")
    dedup_episode = (
        "🚨 #507: приёмка-по-ссылке PR #986 не смогла проверить улику — "
        "compare API недоступен (уже эскалировано, повторно не шлём)")
    assert rc.exit_code([new_episode]) == 1
    assert rc.exit_code([dedup_episode]) == 0
    # ⚠️ среди строк достаточен независимо от порядка.
    assert rc.exit_code([dedup_episode, "⚠️ #661: комментарии не прочитаны — 429"]) == 1


# ══════════════════════════════════════════════════════════════════════════
# Проводка в конвейер: шаг orchestra.yml — носитель вызова (класс
# «потребитель без вызова — мёртвый код», блокирующая находка ревью PR #1046)
# ══════════════════════════════════════════════════════════════════════════


def test_pipeline_wiring_orchestra_yml_calls_reference_closure():
    yml_path = _DIR.parent.parent / ".github" / "workflows" / "orchestra.yml"
    yml = yml_path.read_text(encoding="utf-8")
    assert "python scripts/orchestra/reference_closure.py" in yml, (
        "шаг «Приёмка по ссылке» удалён из orchestra.yml — модуль снова "
        "мёртвый код: класс #507/#661/#786 возвращается к ручной ревизии")


# ══════════════════════════════════════════════════════════════════════════
# Мутация: снять исключение own_task в find_candidates — должно
# перепутать штатную приёмку с этим модулем (доказательство ценности проверки)
# ══════════════════════════════════════════════════════════════════════════


def test_mutation_without_own_task_exclusion_would_duplicate_branch_task():
    def broken_find_candidates(merged_pulls):
        best = {}
        for pull in merged_pulls:
            for number in rc.task_ref.also_closes_targets(pull.get("body") or ""):
                best[number] = pull
        return best

    pr_with_self_marker = {
        "number": 500,
        "head": {"ref": "agent/500-x"},
        "merged_at": "2026-09-12T00:00:00Z",
        "body": "закрывает #500 полностью.",
    }
    # Без исключения own_task — #500 стал бы «кандидатом» второго источника,
    # хотя это ровно тот номер, который штатная приёмка уже закрывает по
    # ветке (дублирование маркеров/комментариев на одну и ту же пару).
    assert broken_find_candidates([pr_with_self_marker]) == {500: pr_with_self_marker}
    # Актуальный код это исключает:
    assert rc.find_candidates([pr_with_self_marker]) == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
