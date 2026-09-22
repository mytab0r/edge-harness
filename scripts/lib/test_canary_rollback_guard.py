#!/usr/bin/env python3
"""Гвардия гвардии: откат прода не запускается, не спросив о причине (#1426).

Тесты поведенческие — на диске создаётся НАСТОЯЩИЙ каталог workflow с
НАСТОЯЩИМ YAML, и проверка читает его тем же кодом, что читает
`.github/workflows` в CI.

Запуск: python -m pytest scripts/lib/test_canary_rollback_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "canary_rollback_guard", Path(__file__).with_name("canary_rollback_guard.py"))
guard = importlib.util.module_from_spec(_spec)
sys.modules["canary_rollback_guard"] = guard
_spec.loader.exec_module(guard)


UNGUARDED = """name: deploy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - id: deploy
        run: npx wrangler deploy
      - name: Автооткат
        id: auto_rollback
        if: failure() && steps.deploy.outcome == 'success'
        run: npx wrangler rollback --yes
"""

GUARDED = """name: deploy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - id: deploy
        run: npx wrangler deploy
      - name: Канарейка
        id: canary_ui
        run: |
          echo "verdict=deploy" >> "$GITHUB_OUTPUT"
          node scripts/canary-ui.mjs
      - name: Автооткат
        id: auto_rollback
        if: failure() && steps.deploy.outcome == 'success' && steps.canary_ui.outputs.verdict != 'backend-down'
        run: npx wrangler rollback --yes
"""

# Копипаст из GUARDED с переименованным id канарейки: `if:` ссылается на
# canary_ui, а шаг называется canary — подстрочная проверка зелёная, а в
# Actions output несуществующего шага пуст и откат безусловен (находка
# AI-ревью PR #1441).
GUARDED_WITH_RENAMED_CANARY_ID = """name: deploy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - id: deploy
        run: npx wrangler deploy
      - name: Канарейка
        id: canary
        run: |
          echo "verdict=deploy" >> "$GITHUB_OUTPUT"
          node scripts/canary-ui.mjs
      - name: Автооткат
        id: auto_rollback
        if: failure() && steps.deploy.outcome == 'success' && steps.canary_ui.outputs.verdict != 'backend-down'
        run: npx wrangler rollback --yes
"""

# Шаг-издатель существует, но вердикт не пишет: output пуст, откат безусловен.
VERDICT_PRODUCER_WITHOUT_VERDICT_WRITE = """name: deploy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - id: deploy
        run: npx wrangler deploy
      - name: Канарейка
        id: canary_ui
        run: node scripts/canary-ui.mjs
      - name: Автооткат
        id: auto_rollback
        if: failure() && steps.deploy.outcome == 'success' && steps.canary_ui.outputs.verdict != 'backend-down'
        run: npx wrangler rollback --yes
"""

ONLY_TALKS_ABOUT_ROLLBACK = """name: deploy
on: [push]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - name: Деплой
        run: |
          # Если что-то пойдёт не так, прод чинится вручную:
          # npx wrangler rollback --yes
          npx wrangler deploy
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for name, text in files.items():
        (workflows / name).write_text(text, encoding="utf-8")
    return workflows


def test_rollback_without_a_verdict_reddens(tmp_path, monkeypatch):
    """Сердце задачи: шаг откатывает прод, не спросив о причине красноты —
    ровно то состояние, в котором был откачен корректный деплой (прогон
    35639422589)."""
    workflows = _tree(tmp_path, {"deploy-thing.yml": UNGUARDED})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {})
    problems = guard.check_rollback_guarded(workflows)
    assert len(problems) == 1, problems
    assert "deploy-thing.yml" in problems[0]
    # Тормоз обязан назвать газ (AGENTS.md).
    assert "outputs.verdict" in problems[0]
    assert "UNGUARDED_ROLLBACK_WORKFLOWS" in problems[0]


def test_rollback_with_a_verdict_is_silent(tmp_path, monkeypatch):
    workflows = _tree(tmp_path, {"deploy-thing.yml": GUARDED})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {})
    assert guard.check_rollback_guarded(workflows) == []


def test_rollback_if_referencing_missing_producer_step_reddens(tmp_path, monkeypatch):
    """Блокирующая находка AI-ревью PR #1441: `if:` спрашивает вердикт у шага
    с чужим id — подстрочная проверка зелёная, а в Actions output
    несуществующего шага пуст, `'' != 'backend-down'` истинно, откат
    безусловен. Гвардия обязана это ловить и называть чужой id."""
    workflows = _tree(tmp_path, {"deploy-thing.yml": GUARDED_WITH_RENAMED_CANARY_ID})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {})
    problems = guard.check_rollback_guarded(workflows)
    assert problems, "копипаст с чужим id прошёл гвардию молча"
    assert any("canary_ui" in p and "нет" in p for p in problems), problems


def test_producer_step_without_verdict_write_reddens(tmp_path, monkeypatch):
    """Шаг-издатель есть, но `verdict=` в GITHUB_OUTPUT не пишет — вердикт
    не издаётся, output пуст, откат безусловен. То же молчаливое красное
    состояние, прибитое поведенчески."""
    workflows = _tree(tmp_path, {"deploy-thing.yml": VERDICT_PRODUCER_WITHOUT_VERDICT_WRITE})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {})
    problems = guard.check_rollback_guarded(workflows)
    assert problems, "шаг без записи вердикта прошёл гвардию молча"
    assert any("verdict=" in p and "canary_ui" in p for p in problems), problems


def test_rollback_only_in_a_comment_is_not_a_rollback(tmp_path, monkeypatch):
    """Рассказ об откате — не откат. В `deploy-worker.yml` про `wrangler
    rollback` написано в пяти комментариях; считать их шагами значило бы
    требовать вердикт от шагов, которые ничего не откатывают."""
    workflows = _tree(tmp_path, {"deploy-thing.yml": ONLY_TALKS_ABOUT_ROLLBACK})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {})
    assert guard.rollback_steps(workflows) == []
    assert guard.check_rollback_guarded(workflows) == []


def test_deliberate_exclusion_with_a_reason_is_silent(tmp_path, monkeypatch):
    workflows = _tree(tmp_path, {"deploy-thing.yml": UNGUARDED})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS",
                        {"deploy-thing.yml": "чужая канарейка, задача #1440"})
    assert guard.check_rollback_guarded(workflows) == []


def test_exclusion_without_a_reason_reddens(tmp_path, monkeypatch):
    workflows = _tree(tmp_path, {"deploy-thing.yml": UNGUARDED})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS", {"deploy-thing.yml": "  "})
    problems = guard.check_rollback_guarded(workflows)
    assert any("без причины" in p for p in problems), problems


def test_exclusion_for_a_workflow_without_rollback_reddens(tmp_path, monkeypatch):
    """Самопроверка признака: запись есть, шага нет. Либо откат убрали, либо
    гвардия перестала его узнавать — второе опаснее (#891/#893)."""
    workflows = _tree(tmp_path, {"deploy-thing.yml": ONLY_TALKS_ABOUT_ROLLBACK})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS",
                        {"deploy-thing.yml": "причина есть"})
    problems = guard.check_rollback_guarded(workflows)
    assert any("шага с wrangler rollback в нём нет" in p for p in problems), problems


def test_dead_exclusion_entry_reddens(tmp_path, monkeypatch):
    workflows = _tree(tmp_path, {"deploy-thing.yml": GUARDED})
    monkeypatch.setattr(guard, "UNGUARDED_ROLLBACK_WORKFLOWS",
                        {"deploy-gone.yml": "причина есть"})
    problems = guard.check_rollback_guarded(workflows)
    assert any("мёртвая запись" in p for p in problems), problems


# ── Живое дерево ────────────────────────────────────────────────────────────

def test_live_repository_is_consistent():
    """На РЕАЛЬНОМ .github/workflows нарушений нет. Краснеет в тот день,
    когда в репозиторий приходит новый откат без вердикта."""
    assert guard.check_rollback_guarded() == []


def test_live_repository_really_has_rollback_steps():
    """Страховка от ложно-зелёного соседа: он зеленеет и на пустом множестве.
    Откаты в дереве есть, и ровно один из них уже спрашивает вердикт."""
    steps = guard.rollback_steps()
    assert steps, "в дереве не найдено ни одного шага с wrangler rollback — признак уехал"
    guarded = [w for w, _, cond, _ in steps if guard.VERDICT_OUTPUT in cond]
    assert "deploy-worker.yml" in guarded, steps


def test_the_only_exclusion_names_its_follow_up_task():
    """Исключение — тормоз, и его газ обязан быть адресуемым: причина без
    номера задачи не отличается от «потом разберёмся»."""
    for workflow, reason in guard.UNGUARDED_ROLLBACK_WORKFLOWS.items():
        assert "#" in reason, (workflow, reason)
