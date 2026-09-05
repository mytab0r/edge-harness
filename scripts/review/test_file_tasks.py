#!/usr/bin/env python3
"""Тесты file_tasks.py: заведение задач из AI-ревью (#18) и перенос
заявленной зависимости в нативный граф `blockedBy` при создании (#371,
продолжение #361/task_deps.py).

Сеть не нужна: `gh()` подменяется прямо на уровне модуля (тот же приём,
что `gh_call=` у task_deps.add_dependency — инъекция, не сеть).

Запуск: python -m pytest scripts/review/test_file_tasks.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("file_tasks.py")
spec = importlib.util.spec_from_file_location("file_tasks", SCRIPT)
ft = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ft)  # type: ignore[union-attr]


def fake_gh(calls, id_by_number=None, fail_on=()):
    """Роутер вызовов gh(*args) по подстроке — тот же стиль, что
    test_task_deps.py::FakeGraphQL, но без subprocess: file_tasks.gh
    сам инжектируется как gh_call у task_deps."""
    id_by_number = id_by_number or {}

    def _gh(*args):
        calls.append(args)
        joined = " ".join(args)
        for bad in fail_on:
            if bad in joined:
                raise RuntimeError(f"gh api: отказ на {bad}")
        if "addBlockedBy" in joined:
            return {"data": {"addBlockedBy": {"issue": {"number": 1}}}}
        for number, node_id in id_by_number.items():
            if f"number={number}" in joined:
                return {"data": {"repository": {"issue": {"id": node_id}}}}
        raise AssertionError(f"неожиданный вызов gh: {joined}")

    return _gh


# ── wire_declared_dependency: перенос заявленного в нативный граф ───────────


def test_wire_missing_line_no_calls_no_link(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ft, "gh", fake_gh(calls))
    linked = ft.wire_declared_dependency("owner/repo", 500, "Цель.\nКритерий.", {1, 2})
    assert linked == []
    assert calls == []
    assert "нет строки" in capsys.readouterr().out


def test_wire_nichem_no_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ft, "gh", fake_gh(calls))
    linked = ft.wire_declared_dependency(
        "owner/repo", 500, "Цель.\nБЛОКИРУЕТСЯ: ничем", {1, 2},
    )
    assert linked == []
    assert calls == []


def test_wire_valid_open_number_adds_native_edge(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ft, "gh", fake_gh(calls, id_by_number={500: "ID_NEW", 55: "ID_55"}),
    )
    linked = ft.wire_declared_dependency(
        "owner/repo", 500, "Цель.\nБЛОКИРУЕТСЯ: #55", {55, 60},
    )
    assert linked == [55]
    joined_calls = " ".join(" ".join(c) for c in calls)
    assert "addBlockedBy" in joined_calls
    assert "number=500" in joined_calls
    assert "number=55" in joined_calls


def test_wire_unknown_number_not_linked_no_network_calls(monkeypatch, capsys):
    # #999 не входит в открытый пул с меткой task — ложная связь опаснее
    # отсутствующей, task_deps.add_dependency НЕ вызывается вовсе.
    calls = []
    monkeypatch.setattr(ft, "gh", fake_gh(calls))
    linked = ft.wire_declared_dependency(
        "owner/repo", 500, "Цель.\nБЛОКИРУЕТСЯ: #999", {55, 60},
    )
    assert linked == []
    assert calls == []
    assert "не открытая" in capsys.readouterr().out


def test_wire_mixed_valid_and_unknown_links_only_valid(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ft, "gh", fake_gh(calls, id_by_number={500: "ID_NEW", 55: "ID_55"}),
    )
    linked = ft.wire_declared_dependency(
        "owner/repo", 500, "Цель.\nБЛОКИРУЕТСЯ: #55 #999", {55, 60},
    )
    assert linked == [55]
    joined_calls = " ".join(" ".join(c) for c in calls)
    assert "number=999" not in joined_calls


# ── open_pool_issues/open_task_titles: один источник, не два прохода ────────


def test_open_task_titles_derives_from_open_pool_issues(monkeypatch):
    calls = []

    def _gh(*args):
        calls.append(args)
        return [
            {"number": 1, "title": "Первая"},
            {"number": 2, "title": "Вторая"},
        ]

    monkeypatch.setattr(ft, "gh", _gh)
    titles = ft.open_task_titles("owner/repo")
    assert titles == {"Первая", "Вторая"}
    # один проход пагинации (одна короткая страница = один вызов), не два
    # расходящихся запроса за заголовками и номерами
    assert len(calls) == 1


# ── main(): интеграция — идемпотентность, dry-run, перенос в граф ──────────


def _verdict_comment(body_extra=""):
    return {
        "id": 9,
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "body": (
            "pr: 140\nhead: abc123\nreviewer: rework\n\n"
            "Находка одна.\n\n" + body_extra
        ),
    }


def _main_router(calls, comment, pool, created_number, id_by_number, patched=()):
    def _gh(*args):
        calls.append(args)
        joined = " ".join(args)
        if args and args[0] == "-X":
            method, url = args[1], args[2]
            if method == "POST" and url.endswith("/issues"):
                return {"number": created_number}
            if method == "PATCH" and "/comments/" in url:
                return {}
            raise AssertionError(f"неожиданный мутирующий вызов: {joined}")
        if args and args[0].startswith("repos/") and "/comments" in args[0]:
            return [comment]
        if args and args[0].startswith("repos/") and "issues?state=open&labels=task" in args[0]:
            return pool
        if "addBlockedBy" in joined:
            return {"data": {"addBlockedBy": {"issue": {"number": 1}}}}
        for number, node_id in id_by_number.items():
            if f"number={number}" in joined:
                return {"data": {"repository": {"issue": {"id": node_id}}}}
        raise AssertionError(f"неожиданный вызов gh: {joined}")

    return _gh


def test_main_files_task_and_wires_native_dependency(monkeypatch, capsys):
    task_fence = (
        "````задача\nНовая задача\nЦель.\nБЛОКИРУЕТСЯ: #55\n````\n"
    )
    comment = _verdict_comment(task_fence)
    pool = [{"number": 55, "title": "Существующая открытая"}]
    calls = []
    router = _main_router(
        calls, comment, pool, created_number=500,
        id_by_number={500: "ID_NEW", 55: "ID_55"},
    )
    monkeypatch.setattr(ft, "gh", router)
    monkeypatch.setattr(ft, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr("sys.argv", ["file_tasks.py", "--pr", "140"])
    rc = ft.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "+ #500" in out
    assert "заблокирована #55" in out
    joined_calls = " ".join(" ".join(c) for c in calls)
    assert "addBlockedBy" in joined_calls


def test_main_dry_run_previews_blocked_by_without_creating(monkeypatch, capsys):
    task_fence = "````задача\nНовая задача\nЦель.\nБЛОКИРУЕТСЯ: #55\n````\n"
    comment = _verdict_comment(task_fence)
    pool = [{"number": 55, "title": "Существующая открытая"}]
    calls = []

    def _gh(*args):
        calls.append(args)
        if args and args[0].startswith("repos/") and "/comments" in args[0]:
            return [comment]
        if args and args[0].startswith("repos/") and "issues?state=open&labels=task" in args[0]:
            return pool
        raise AssertionError(f"неожиданный вызов в dry-run: {' '.join(args)}")

    monkeypatch.setattr(ft, "gh", _gh)
    monkeypatch.setattr(ft, "current_repo", lambda: "owner/repo")
    monkeypatch.setattr("sys.argv", ["file_tasks.py", "--pr", "140", "--dry-run"])
    rc = ft.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "[55]" in out
    # dry-run не мутирует ничего — ни POST issues, ни PATCH комментария,
    # ни граф зависимостей
    for call in calls:
        assert not (call and call[0] == "-X")
