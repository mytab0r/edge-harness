#!/usr/bin/env python3
"""Гвардия правила удаления scripts/git/worktree_gc.py (#565).

Кормится чистой функцией decide() напрямую (тот же подход, что
scripts/lib/test_epic_guard.py хвалит в комментарии task-branch: юнит на
моке, не смоук на настоящем git/gh) — оба «никогда» (живой агент,
незакоммиченные изменения) обязаны перевешивать любые остальные факты,
включая «PR слит» и «неотправленных коммитов нет».

Мутация, которой доказана проверка (проверено вручную при написании):
  - закомментируй `if live: return KEEP, ...` в decide() — тест
    test_live_agent_never_removed_even_if_everything_else_says_remove
    краснеет (дерево живого агента внезапно подлежит удалению).
  - закомментируй `if dirty: return KEEP, ...` — тест
    test_dirty_worktree_never_removed_even_if_everything_else_says_remove
    краснеет тем же образом.
Верни блок — тесты снова зелёные.

Запуск: python -m pytest scripts/git/test_worktree_gc.py -q
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("worktree_gc.py")
_spec = importlib.util.spec_from_file_location("worktree_gc_module", SCRIPT)
worktree_gc = importlib.util.module_from_spec(_spec)
# Регистрация в sys.modules ДО exec_module: @dataclass внутри worktree_gc.py
# резолвит аннотации типов через sys.modules[cls.__module__] — без записи
# здесь падает AttributeError на пустом __dict__ (не относится к логике
# самого модуля, чисто механика importlib для модулей с dataclass).
sys.modules[_spec.name] = worktree_gc
_spec.loader.exec_module(worktree_gc)  # type: ignore[union-attr]

decide = worktree_gc.decide
REMOVE = worktree_gc.REMOVE
KEEP = worktree_gc.KEEP


def test_live_agent_never_removed_even_if_everything_else_says_remove():
    action, reason = decide(dirty=False, live=True, pr_state="MERGED", unsent=False)
    assert action == KEEP
    assert "живой" in reason


def test_dirty_worktree_never_removed_even_if_everything_else_says_remove():
    action, reason = decide(dirty=True, live=False, pr_state="MERGED", unsent=False)
    assert action == KEEP
    assert "незакоммич" in reason


def test_live_agent_beats_dirty_check_order_independence():
    # Оба «никогда» действуют независимо: комбинация live+dirty тоже KEEP,
    # с сообщением про живого агента (проверяется первым).
    action, reason = decide(dirty=True, live=True, pr_state="MERGED", unsent=False)
    assert action == KEEP
    assert "живой" in reason


def test_clean_merged_no_unsent_commits_is_removed():
    action, _reason = decide(dirty=False, live=False, pr_state="MERGED", unsent=False)
    assert action == REMOVE


def test_clean_closed_no_unsent_commits_is_removed():
    action, _reason = decide(dirty=False, live=False, pr_state="CLOSED", unsent=False)
    assert action == REMOVE


def test_open_pr_never_removed():
    action, reason = decide(dirty=False, live=False, pr_state="OPEN", unsent=False)
    assert action == KEEP
    assert "открыт" in reason


def test_no_pr_found_never_removed():
    action, reason = decide(dirty=False, live=False, pr_state=None, unsent=None)
    assert action == KEEP
    assert "не найден" in reason


def test_unknown_unsent_status_never_removed():
    # PR закрыт, ветка на сервере уже удалена — сомнение решается в пользу KEEP.
    action, reason = decide(dirty=False, live=False, pr_state="CLOSED", unsent=None)
    assert action == KEEP
    assert "не могу подтвердить" in reason


def test_unsent_commits_never_removed():
    action, reason = decide(dirty=False, live=False, pr_state="MERGED", unsent=True)
    assert action == KEEP
    assert "неотправленн" in reason
