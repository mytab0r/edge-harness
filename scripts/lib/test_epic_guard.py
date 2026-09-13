#!/usr/bin/env python3
"""Тесты гвардии «PR не заводится на эпик» (#376, scripts/lib/epic_guard.py).

Признак эпика (`is_epic_issue`) не переопределяется здесь — модуль импортирует
`scripts/orchestra/scheduler.py` тем же способом, что `test_scheduler.py`, и
тесты этого файла кормятся тем же прод-снимком #77
(`{"total": 8, "completed": 6, "percent_completed": 75}`), которым уже доказан
`is_epic_issue` в `scripts/orchestra/test_scheduler.py`. Мутация «убрали вызов
`is_epic_issue` из `check()`» краснит `test_check_epic_by_title`/
`test_check_epic_by_open_sub_issues` (обе перестанут детектировать эпик).

Сеть не нужна: `scheduler.gh` и `subprocess.run` подменены monkeypatch'ем.

Запуск: python -m pytest scripts/lib/test_epic_guard.py -q
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("epic_guard.py")
spec = importlib.util.spec_from_file_location("epic_guard", SCRIPT)
eg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eg)  # type: ignore[union-attr]


# ── current_repo ──────────────────────────────────────────────────────────────


def test_current_repo_from_env_no_subprocess_call(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    called = []
    monkeypatch.setattr(eg.subprocess, "run", lambda *a, **k: called.append(a) or SimpleNamespace(returncode=0, stdout=""))
    assert eg.current_repo() == "o/r"
    assert called == []


def test_current_repo_via_gh_repo_view_when_env_absent(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(
        eg.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="mytab0r/edge-harness\n"))
    assert eg.current_repo() == "mytab0r/edge-harness"


def test_current_repo_none_when_gh_fails(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(eg.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert eg.current_repo() is None


# ── check() / is_epic_issue (прод-форма #77) ─────────────────────────────────


EPIC_77_ISSUE = {
    "title": "ЭПИК: dsh-edge как полноценный DSH-хост — плагины через морду, раннер как мастерская",
    "sub_issues_summary": {"total": 8, "completed": 6, "percent_completed": 75},
}

ORDINARY_TASK_ISSUE = {
    "title": "plugin-forge.yml: workflow форжа плагинов в раннере (#77 стройка 3)",
    "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
}


def test_check_epic_by_title(monkeypatch):
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: EPIC_77_ISSUE)
    is_epic, issue = eg.check("o/r", 77)
    assert is_epic is True
    assert issue is EPIC_77_ISSUE


def test_check_epic_by_open_sub_issues_without_epic_title(monkeypatch):
    # #115-подобный случай (комментарий is_epic_issue): заголовок без «ЭПИК»,
    # но есть незакрытые нативные sub-issues — второй сигнал ловит этот класс.
    issue = {"title": "Обычный заголовок без префикса",
             "sub_issues_summary": {"total": 3, "completed": 1}}
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: issue)
    is_epic, _ = eg.check("o/r", 115)
    assert is_epic is True


def test_check_ordinary_task_is_not_epic(monkeypatch):
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: ORDINARY_TASK_ISSUE)
    is_epic, _ = eg.check("o/r", 268)
    assert is_epic is False


# ── open_sub_issues ───────────────────────────────────────────────────────────


def test_open_sub_issues_filters_open_only(monkeypatch):
    payload = {
        "data": {"repository": {"issue": {"subIssues": {"nodes": [
            {"number": 78, "title": "закрыта", "state": "CLOSED"},
            {"number": 86, "title": "открыта 1", "state": "OPEN"},
            {"number": 114, "title": "открыта 2", "state": "OPEN"},
        ]}}}}
    }
    monkeypatch.setattr(
        eg.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    subs = eg.open_sub_issues("o/r", 77)
    assert [s["number"] for s in subs] == [86, 114]


def test_open_sub_issues_best_effort_empty_on_graphql_failure(monkeypatch):
    monkeypatch.setattr(eg.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert eg.open_sub_issues("o/r", 77) == []


# ── main() / CLI ──────────────────────────────────────────────────────────────


def test_main_ok_for_ordinary_task(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: ORDINARY_TASK_ISSUE)
    assert eg.main(["epic_guard.py", "268"]) == eg.EXIT_OK
    assert capsys.readouterr().err == ""


def test_main_refuses_epic_with_gas_and_subissues(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: EPIC_77_ISSUE)
    payload = {"data": {"repository": {"issue": {"subIssues": {"nodes": [
        {"number": 86, "title": "Миграция функций edge-harness в плагин", "state": "OPEN"},
    ]}}}}}
    monkeypatch.setattr(
        eg.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(payload)))
    assert eg.main(["epic_guard.py", "77"]) == eg.EXIT_EPIC
    err = capsys.readouterr().err
    assert "эпик" in err
    assert "заведи узкую задачу" in err
    assert "#86" in err


def test_main_refuses_epic_even_without_subissues_data(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(eg.scheduler, "gh", lambda *a, **k: EPIC_77_ISSUE)
    monkeypatch.setattr(eg.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert eg.main(["epic_guard.py", "77"]) == eg.EXIT_EPIC
    err = capsys.readouterr().err
    assert "заведи узкую задачу" in err


def test_main_error_when_repo_not_found(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(eg.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert eg.main(["epic_guard.py", "268"]) == eg.EXIT_ERROR


def test_main_error_when_gh_api_broken(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def broken(*a, **k):
        raise RuntimeError("gh api repos/o/r/issues/268: HTTP 500")

    monkeypatch.setattr(eg.scheduler, "gh", broken)
    assert eg.main(["epic_guard.py", "268"]) == eg.EXIT_ERROR


def test_main_usage_error_on_bad_argv():
    assert eg.main(["epic_guard.py"]) == eg.EXIT_ERROR
    assert eg.main(["epic_guard.py", "not-a-number"]) == eg.EXIT_ERROR
