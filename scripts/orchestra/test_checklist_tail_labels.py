#!/usr/bin/env python3
"""Тесты наследования области хвостом чеклиста ревью
(scripts/orchestra/checklist_tail_labels.py, сирота A аудита 2026-09-11).

Кормятся прод-формой: заголовок хвоста — реальный результат
`review_checklist.tail_issue_title` (не переписанная строка), номера
PR/задач и их метки — форма, которую реально отдаёт `gh api repos/.../
pulls/{n}` (нужен только `head.ref`) и `gh api repos/.../issues/{n}`
(нужны только `number`/`labels`), проверено живым замером 2026-09-11:
PR #942 → agent-ветка `agent/939-...` → задача #939 несёт `area:process`.

Запуск: python -m pytest scripts/orchestra/test_checklist_tail_labels.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # checklist_tail_labels.py делает `from pulse_guard import …`

PG_SCRIPT = _DIR / "pulse_guard.py"
pg_spec = importlib.util.spec_from_file_location("pulse_guard", PG_SCRIPT)
pg = importlib.util.module_from_spec(pg_spec)
pg_spec.loader.exec_module(pg)  # type: ignore[union-attr]
sys.modules["pulse_guard"] = pg  # checklist_tail_labels.py делает `from pulse_guard import …`

SCRIPT = _DIR / "checklist_tail_labels.py"
spec = importlib.util.spec_from_file_location("checklist_tail_labels", SCRIPT)
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)  # type: ignore[union-attr]

_RC_SPEC = importlib.util.spec_from_file_location(
    "review_checklist", _DIR.parent / "lib" / "review_checklist.py")
review_checklist = importlib.util.module_from_spec(_RC_SPEC)
_RC_SPEC.loader.exec_module(review_checklist)  # type: ignore[union-attr]


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(ctl, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


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


REPO = "mytab0r/edge-harness"


def labels(*names):
    return [{"name": n} for n in names]


def tail_issue(number, pr_number, existing_labels=("task",)):
    return {
        "number": number,
        "title": review_checklist.tail_issue_title(pr_number),
        "labels": labels(*existing_labels),
    }


def pull(head_ref):
    return {"head": {"ref": head_ref}}


# ── Синхронность регулярки с настоящим форматом заголовка ──────────────────

def test_tail_title_re_matches_real_tail_issue_title():
    title = review_checklist.tail_issue_title(942)
    match = ctl.TAIL_TITLE_RE.match(title)
    assert match is not None
    assert int(match.group(1)) == 942


# ── inheritable_labels: чистая функция ──────────────────────────────────────

def test_inheritable_labels_keeps_only_area_prefixed():
    parent_labels = labels("task", "area:process", "stale-unclaimed", "area:orchestra")
    assert ctl.inheritable_labels(parent_labels) == {"area:process", "area:orchestra"}


def test_inheritable_labels_empty_when_parent_has_no_area():
    parent_labels = labels("task", "stale-unclaimed")
    assert ctl.inheritable_labels(parent_labels) == set()


# ── Живой сценарий: PR #942 → задача #939 (area:process) ────────────────────

def test_apply_inherited_labels_copies_area_process_from_parent_task(monkeypatch):
    tail = tail_issue(954, 942)
    fake = FakeGh({
        "pulls/942": pull("agent/939-large-review-size-gate-ai-judgment"),
        "issues/939": {"number": 939, "labels": labels("task", "area:process")},
        "issues/954/labels": None,
    })
    patch_gh(monkeypatch, fake)

    line = ctl.apply_inherited_labels(REPO, tail)

    assert "area:process" in line
    label_calls = [c for c in fake.calls if "issues/954/labels" in c]
    assert len(label_calls) == 1
    assert "labels[]=area:process" in label_calls[0]
    assert f"labels[]={ctl.INHERITED_MARKER_LABEL}" in label_calls[0]


def test_apply_inherited_labels_marks_done_when_parent_has_no_area(monkeypatch):
    tail = tail_issue(928, 617)
    fake = FakeGh({
        "pulls/617": pull("agent/614-deploy-worker-rollback"),
        "issues/614": {"number": 614, "labels": labels("task")},
        "issues/928/labels": None,
    })
    patch_gh(monkeypatch, fake)

    line = ctl.apply_inherited_labels(REPO, tail)

    assert "нет area" in line
    label_calls = [c for c in fake.calls if "issues/928/labels" in c]
    assert len(label_calls) == 1
    assert f"labels[]={ctl.INHERITED_MARKER_LABEL}" in label_calls[0]
    assert ("labels[]=" + "area:") not in label_calls[0]


def test_apply_inherited_labels_skips_already_marked_tail(monkeypatch):
    tail = tail_issue(900, 1, existing_labels=("task", ctl.INHERITED_MARKER_LABEL))
    fake = FakeGh({})  # ни один маршрут не должен понадобиться
    patch_gh(monkeypatch, fake)

    line = ctl.apply_inherited_labels(REPO, tail)

    assert line is None
    assert fake.calls == []


def test_apply_inherited_labels_does_not_duplicate_already_present_label(monkeypatch):
    tail = tail_issue(901, 942, existing_labels=("task", "area:process"))
    fake = FakeGh({
        "pulls/942": pull("agent/939-large-review-size-gate-ai-judgment"),
        "issues/939": {"number": 939, "labels": labels("task", "area:process")},
        "issues/901/labels": None,
    })
    patch_gh(monkeypatch, fake)

    line = ctl.apply_inherited_labels(REPO, tail)

    # Отчёт различает пустой to_inherit («у родителя нет области») от пустого
    # missing («область уже стоит») — находка ревью PR #964: старый текст
    # отвечал «нет area» и в этом случае, утверждая ложный факт о родителе.
    assert "уже стоит" in line and "area:process" in line
    assert "нет area" not in line
    label_calls = [c for c in fake.calls if "issues/901/labels" in c]
    assert len(label_calls) == 1
    assert ("labels[]=" + "area:") not in label_calls[0]  # только маркер, без дубля


def test_apply_inherited_labels_pr_not_agent_branch_marks_without_area(monkeypatch):
    tail = tail_issue(902, 5)
    fake = FakeGh({
        "pulls/5": pull("dependabot/npm_and_yarn/sharp-0.35.4"),
        "issues/902/labels": None,
    })
    patch_gh(monkeypatch, fake)

    line = ctl.apply_inherited_labels(REPO, tail)

    assert "не резолвится" in line
    label_calls = [c for c in fake.calls if "issues/902/labels" in c]
    assert len(label_calls) == 1
    assert f"labels[]={ctl.INHERITED_MARKER_LABEL}" in label_calls[0]


def test_apply_inherited_labels_transient_pr_read_failure_does_not_mark(monkeypatch):
    """Находка живого AI-ревью PR #964 (rework, head bcaf99c4): 502/503 на
    чтении PR раньше схлопывался в тот же `None`, что и «ветка не
    agent-формы» — хвост получал INHERITED_MARKER_LABEL НАВСЕГДА по
    транзиентному сбою. Правильное поведение — RuntimeError долетает до
    checklist_tail_labels() как мягкое наблюдение по этому хвосту, БЕЗ
    маркера, чтобы следующий пульс попробовал снова."""
    tail = tail_issue(903, 942)
    fake = FakeGh({"pulls/942": RuntimeError("gh api pulls/942: HTTP 502")})
    patch_gh(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="502"):
        ctl.apply_inherited_labels(REPO, tail)

    label_calls = [c for c in fake.calls if "issues/903/labels" in c]
    assert label_calls == []  # ни разу не поставлен маркер по сбою чтения


def test_checklist_tail_labels_reports_transient_pr_read_failure_without_marking(monkeypatch):
    tail = tail_issue(903, 942)
    fake = FakeGh({
        "issues?state=open&labels=task": [tail],
        "pulls/942": RuntimeError("gh api pulls/942: HTTP 502"),
    })
    patch_gh(monkeypatch, fake)

    lines = ctl.checklist_tail_labels(REPO)

    assert len(lines) == 1
    assert "#903" in lines[0] and "502" in lines[0]
    label_calls = [c for c in fake.calls if "issues/903/labels" in c]
    assert label_calls == []


# ── Мутация, доказывающая гвардию: без фильтра NON_INHERITABLE_LABELS/
# отбора по префиксу area: сервисные метки родителя (review:ok, blocked,
# auto-detected) утекли бы в хвост как «область» — снять фильтр (заменить
# inheritable_labels на `return names`) и увидеть красный тест ниже. ─────────

def test_inheritable_labels_mutation_guard_rejects_service_labels():
    parent_labels = labels("task", "review:ok", "blocked", "auto-detected", "waiting:owner")
    assert ctl.inheritable_labels(parent_labels) == set()


# ── checklist_tail_labels: агрегатор по нескольким хвостам ──────────────────

def test_checklist_tail_labels_processes_each_open_tail_once(monkeypatch):
    tails = [tail_issue(954, 942), tail_issue(928, 617)]
    fake = FakeGh({
        "issues?state=open&labels=task": tails,
        "pulls/942": pull("agent/939-large-review-size-gate-ai-judgment"),
        "issues/939": {"number": 939, "labels": labels("task", "area:process")},
        "issues/954/labels": None,
        "pulls/617": pull("agent/614-deploy-worker-rollback"),
        "issues/614": {"number": 614, "labels": labels("task")},
        "issues/928/labels": None,
    })
    patch_gh(monkeypatch, fake)

    lines = ctl.checklist_tail_labels(REPO)

    assert len(lines) == 2
    assert any("#954" in line and "area:process" in line for line in lines)
    assert any("#928" in line and "нет area" in line for line in lines)


def test_checklist_tail_labels_propagates_pool_read_failure(monkeypatch):
    """Находка ревью PR #964 (критик, блокер 3): чтение списка хвостов —
    единственный сигнал «жив ли механизм» (право issues read, транспорт) —
    обязано падать наверх, а не превращаться в мягкое наблюдение. main()
    (тест ниже) ловит это и красит прогон ненулевым кодом."""
    fake = FakeGh({"issues?state=open&labels=task": RuntimeError("gh api issues: HTTP 503")})
    patch_gh(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="503"):
        ctl.checklist_tail_labels(REPO)


# ── main(): фактический код возврата (мутация — доказывает блокер 3 закрыт) ──

def test_main_returns_nonzero_when_pool_read_fails(monkeypatch):
    fake = FakeGh({"issues?state=open&labels=task": RuntimeError("gh api issues: HTTP 403")})
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert ctl.main() == 1


def test_main_returns_zero_on_healthy_run(monkeypatch):
    fake = FakeGh({"issues?state=open&labels=task": []})
    patch_gh(monkeypatch, fake)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    assert ctl.main() == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
