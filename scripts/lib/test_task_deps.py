#!/usr/bin/env python3
"""Тесты графа блокировок пула (scripts/lib/task_deps.py, задача #361).

Сеть не нужна: `gh api graphql` подменён на уровне `subprocess.run` (тот же
приём, что `test_claim_task.py`) — семантика прод-ответа GitHub GraphQL
сохранена (форма `{"data": {...}}`, пагинация `pageInfo`/`endCursor`,
ошибки `{"errors": [...]}`).

Живой round-trip (addBlockedBy/removeBlockedBy на реальной паре issue
#350↔#320, 2026-09-06) описан в docstring task_deps.py и proposal.md этого
change — здесь он НЕ повторяется сетевым вызовом (тест не должен зависеть
от сети), проверяется только контракт разбора/сборки запроса.

Запуск: python -m pytest scripts/lib/test_task_deps.py -q
"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("task_deps.py")
spec = importlib.util.spec_from_file_location("task_deps", SCRIPT)
td = importlib.util.module_from_spec(spec)
spec.loader.exec_module(td)  # type: ignore[union-attr]


def out(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def gh_error(status, message="ошибка"):
    return SimpleNamespace(returncode=1, stdout="", stderr=f"gh: HTTP {status}: {message}")


def issue_node(
    number, title="задача", labels=(), assignees=(), blocked_by=(), blocking=(),
    blocked_by_total=None, blocking_total=None,
):
    """`blocked_by_total`/`blocking_total` — по умолчанию совпадают с длиной
    переданного списка (страница полная, находка ревью PR #367 неприменима);
    передай явно бОльшее число, чтобы смоделировать усечённую страницу."""
    blocked_by_nodes = [{"number": n, "state": s} for n, s in blocked_by]
    blocking_nodes = [{"number": n, "state": s} for n, s in blocking]
    return {
        "number": number,
        "title": title,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "assignees": {"nodes": [{"login": a} for a in assignees]},
        "blockedBy": {
            "totalCount": blocked_by_total if blocked_by_total is not None else len(blocked_by_nodes),
            "nodes": blocked_by_nodes,
        },
        "blocking": {
            "totalCount": blocking_total if blocking_total is not None else len(blocking_nodes),
            "nodes": blocking_nodes,
        },
    }


class FakeGraphQL:
    """Роутер по последовательности запросов (одна страница -> следующая)."""

    def __init__(self, pages: list[dict] | None = None, single: dict | None = None):
        self.pages = list(pages or [])
        self.single = single
        self.calls: list[list[str]] = []

    def run(self, args, capture_output=True, text=True, env=None):
        self.calls.append(args)
        if self.single is not None:
            return out({"data": self.single})
        page = self.pages.pop(0)
        return out({"data": page})


def install(monkeypatch, fake):
    monkeypatch.setattr(td, "subprocess", SimpleNamespace(run=fake.run))
    return fake


# ── fetch_pool: пагинация + форма полей ─────────────────────────────────────


def test_fetch_pool_single_page_shapes_fields():
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    issue_node(43, "мета", labels=["area:process"], blocking=[(90, "OPEN"), (91, "CLOSED")]),
                    issue_node(90, "прикладная", assignees=["someone"]),
                ],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        issues = td.fetch_pool("owner/repo")
    assert len(issues) == 2
    meta = issues[0]
    assert meta["number"] == 43
    assert meta["labels"] == [{"name": "area:process"}]
    # blocking_open считает ТОЛЬКО OPEN (91 CLOSED не считается)
    assert meta["blocking_open"] == 1
    assert issues[1]["assignees"] == [{"login": "someone"}]


def test_fetch_pool_raises_loudly_on_truncated_blocking_page():
    """Находка ревью PR #367: `blocking(first: 20)` — усечённая страница
    вложенного коннекшена. Без проверки totalCount тяжёлый блокировщик
    (>20 открытых блокируемых) молча застыл бы на 20, и приоритет — весь
    смысл модуля — тихо соврал бы. Обязан упасть громко, не тихо усечься."""
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    issue_node(43, blocking=[(90, "OPEN")], blocking_total=25),
                ],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        with pytest.raises(td.TaskDepsError, match="blocking усечён"):
            td.fetch_pool("owner/repo")


def test_fetch_pool_raises_loudly_on_truncated_blocked_by_page():
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    issue_node(43, blocked_by=[(90, "OPEN")], blocked_by_total=21),
                ],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        with pytest.raises(td.TaskDepsError, match="blockedBy усечён"):
            td.fetch_pool("owner/repo")


def test_fetch_pool_paginates_until_short_page():
    page1 = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR1"},
                "nodes": [issue_node(1), issue_node(2)],
            }
        }
    }
    page2 = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [issue_node(3)],
            }
        }
    }
    fake = FakeGraphQL(pages=[page1, page2])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        issues = td.fetch_pool("owner/repo")
    assert [i["number"] for i in issues] == [1, 2, 3]
    assert len(fake.calls) == 2
    # второй запрос обязан нести cursor из первой страницы
    assert any("after=CURSOR1" in " ".join(call) for call in fake.calls)


def test_fetch_pool_rejects_label_that_is_not_a_literal():
    with pytest.raises(td.TaskDepsError):
        td.fetch_pool("owner/repo", label="not a label; DROP")


def test_fetch_pool_include_body_adds_field_to_query_and_result():
    node = issue_node(43, "мета")
    node["body"] = "Тело задачи с текстом."
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [node],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        issues = td.fetch_pool("owner/repo", include_body=True)
    assert issues[0]["body"] == "Тело задачи с текстом."
    # запрос реально несёт поле body — не тихая заглушка
    assert any("body" in " ".join(call) for call in fake.calls)


def test_fetch_pool_default_omits_body_key():
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [issue_node(43, "мета")],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        issues = td.fetch_pool("owner/repo")
    assert "body" not in issues[0]


def test_gh_graphql_surfaces_gh_failure_loudly():
    fake = FakeGraphQL()
    fake.run = lambda *a, **kw: gh_error(401, "Bad credentials")
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        with pytest.raises(td.TaskDepsError):
            td.fetch_pool("owner/repo")


def test_gh_graphql_surfaces_graphql_errors_field_loudly():
    fake = FakeGraphQL()
    fake.run = lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"errors": [{"message": "field not found"}]}), stderr="",
    )
    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=fake.run)):
        with pytest.raises(td.TaskDepsError):
            td.fetch_pool("owner/repo")


# ── add_dependency/remove_dependency: резолв id + мутация ───────────────────


def test_add_dependency_resolves_ids_then_mutates():
    def run(args, capture_output=True, text=True, env=None):
        joined = " ".join(args)
        if "addBlockedBy" in joined:
            assert "issueId=ID_43" in joined
            assert "blockingIssueId=ID_90" in joined
            return out({"data": {"addBlockedBy": {"issue": {"number": 43}}}})
        # обе резолюции id (заблокированная и блокирующая) идут одним шаблоном
        if "number=43" in joined:
            return out({"data": {"repository": {"issue": {"id": "ID_43"}}}})
        if "number=90" in joined:
            return out({"data": {"repository": {"issue": {"id": "ID_90"}}}})
        raise AssertionError(f"неожиданный вызов: {joined}")

    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=run)):
        td.add_dependency("owner/repo", blocked=43, blocking=90)


def test_issue_node_id_missing_issue_is_loud():
    def run(args, capture_output=True, text=True, env=None):
        return out({"data": {"repository": {"issue": None}}})

    import unittest.mock as mock
    with mock.patch.object(td, "subprocess", SimpleNamespace(run=run)):
        with pytest.raises(td.TaskDepsError):
            td.issue_node_id("owner/repo", 999999)


# ── wire_dependencies: единый цикл переноса объявленного в граф (#529) ───────


def test_wire_dependencies_links_only_numbers_in_open_pool(monkeypatch):
    calls = []
    monkeypatch.setattr(
        td, "add_dependency",
        lambda repo, blocked, blocking, gh_call=None: calls.append((blocked, blocking)),
    )
    linked = td.wire_dependencies(
        "owner/repo", 500, [55, 999], {55, 60}, log=lambda *a: None,
    )
    assert linked == [55]
    assert calls == [(500, 55)]


def test_wire_dependencies_skips_self_reference_without_network_call():
    linked = td.wire_dependencies("owner/repo", 500, [500], {500}, log=lambda *a: None)
    assert linked == []


def test_wire_dependencies_logs_skip_reason_for_unknown_number(monkeypatch):
    monkeypatch.setattr(td, "add_dependency", lambda *a, **kw: None)
    logged = []
    linked = td.wire_dependencies("owner/repo", 500, [999], {55}, log=logged.append)
    assert linked == []
    assert any("не открытая" in line for line in logged)


def test_wire_dependencies_empty_input_no_calls():
    linked = td.wire_dependencies("owner/repo", 500, [], {55}, log=lambda *a: None)
    assert linked == []


# ── CLI ──────────────────────────────────────────────────────────────────────


def run_cli(args, monkeypatch, run):
    monkeypatch.setattr(td, "subprocess", SimpleNamespace(run=run))
    return td.main(args)


def test_cli_pool_prints_json(monkeypatch, capsys):
    page = {
        "repository": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [issue_node(5, "т")],
            }
        }
    }
    fake = FakeGraphQL(pages=[page])
    rc = run_cli(["pool", "owner/repo"], monkeypatch, fake.run)
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed[0]["number"] == 5


def test_cli_unknown_verb_is_rc2():
    assert td.main(["bogus"]) == 2
