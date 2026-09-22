#!/usr/bin/env python3
"""Гвардия: список деплойных workflow инварианта 17 не расходится с деревом (#1421).

Чем оплачено. #1041 написал инвариант «прод молча устарел» под ОДИН деплойный
workflow. Второй, `deploy-worker.yml`, краснел девять суток (2026-09-13 …
2026-09-21) — прод-морда не обновлялась, и заметил это человек, открывший
Actions руками (#1419). #1419 дописал второй файл в список: починил СЛУЧАЙ.
Класс — в том, что список рукописный: третий деплойный workflow станет
невидим ровно тем же способом.

Тесты поведенческие: на диске создаётся НАСТОЯЩИЙ каталог workflow с
НАСТОЯЩИМ YAML, и гвардия читает его тем же кодом, что читает
`.github/workflows` в CI. Структурная проверка («в исходнике есть имя
функции») здесь не годится по прямому правилу AGENTS.md: она зеленеет на
вырезанном теле.

Запуск: python -m pytest scripts/lib/test_deploy_workflow_registry_guard.py -q
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "deploy_workflow_registry_guard",
    Path(__file__).with_name("deploy_workflow_registry_guard.py"))
guard = importlib.util.module_from_spec(_spec)
sys.modules["deploy_workflow_registry_guard"] = guard
_spec.loader.exec_module(guard)


# ── Прод-формы: куски РЕАЛЬНЫХ файлов дерева, не пересказ ───────────────────

# .github/workflows/deploy-worker.yml:188 — дословно.
REAL_DEPLOY_STEP = """name: deploy worker
on:
  push:
    branches: [main]
    paths: ['cf-worker/**']
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy
        run: npx wrangler deploy
"""

# Тот же текст, но деплой ТОЛЬКО в комментарии — файл ничего не деплоит.
ONLY_TALKS_ABOUT_DEPLOY = """name: worker ci
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Tests
        run: |
          # Раньше здесь стоял `npx wrangler deploy`, теперь деплой отдельным
          # workflow (deploy-worker.yml) — см. #1419.
          npm test
"""

NOT_A_DEPLOY = """name: lint
on: [pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: npm run lint
"""


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    for name, text in files.items():
        (workflows / name).write_text(text, encoding="utf-8")
    return workflows


# ── Распознавание деплоя: по шагам файла, не по имени ───────────────────────

def test_real_wrangler_step_is_recognized_as_deploy(tmp_path):
    """Прод-форма шага из deploy-worker.yml. Распознать её — минимум, без
    которого весь остальной механизм бессмыслен."""
    workflows = _tree(tmp_path, {"deploy-worker.yml": REAL_DEPLOY_STEP})
    assert guard.deploy_workflows(workflows) == {"deploy-worker.yml"}


def test_deploy_only_in_a_comment_is_not_a_deploy(tmp_path):
    """Рассказ о деплое — не деплой. Иначе список исключений начнёт
    наполняться записями «это не деплой, это комментарий»: гвардия чинила бы
    собственный дефект чужими руками."""
    workflows = _tree(tmp_path, {"worker-ci.yml": ONLY_TALKS_ABOUT_DEPLOY})
    assert guard.deploy_workflows(workflows) == set()


def test_wrangler_action_counts_as_deploy(tmp_path):
    """Вторая дверь в тот же прод: официальный action вместо npx. Не знать про
    неё значит завести ровно тот класс, который закрывает этот файл."""
    workflows = _tree(tmp_path, {"deploy-pages.yml": """name: pages
on: [push]
jobs:
  go:
    runs-on: ubuntu-latest
    steps:
      - uses: cloudflare/wrangler-action@v3
        with:
          command: deploy
"""})
    assert guard.deploy_workflows(workflows) == {"deploy-pages.yml"}


def test_versions_upload_counts_as_deploy(tmp_path):
    """`wrangler versions upload` — те же ворота в прод, другой глагол."""
    workflows = _tree(tmp_path, {"deploy-canary.yml": """name: canary
on: [push]
jobs:
  go:
    runs-on: ubuntu-latest
    steps:
      - run: npx wrangler versions upload
"""})
    assert guard.deploy_workflows(workflows) == {"deploy-canary.yml"}


def test_name_does_not_make_a_workflow_a_deploy(tmp_path):
    """Признак машинный, не по имени файла: `deploy-`-префикс без шага
    wrangler деплоем не считается, иначе гвардия судила бы по названию."""
    workflows = _tree(tmp_path, {"deploy-docs.yml": NOT_A_DEPLOY})
    assert guard.deploy_workflows(workflows) == set()


# ── Сверка списков с реальностью ────────────────────────────────────────────

def test_new_deploy_workflow_outside_both_lists_reddens(tmp_path, monkeypatch):
    """Сердце задачи: завтра появился третий деплойный workflow, автор не
    тронул списки — CI краснеет СЕГОДНЯ, а не через девять суток по красной
    вкладке Actions."""
    workflows = _tree(tmp_path, {
        "deploy-worker.yml": REAL_DEPLOY_STEP,
        "deploy-newthing.yml": REAL_DEPLOY_STEP,
    })
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS",
                        ("deploy-worker.yml",))
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS", {})
    problems = guard.check_registry_completeness(workflows)
    assert len(problems) == 1, problems
    assert "deploy-newthing.yml" in problems[0]
    # Тормоз обязан назвать газ (AGENTS.md).
    assert "FRONTEND_DEPLOY_WORKFLOWS" in problems[0]
    assert "UNWATCHED_DEPLOY_WORKFLOWS" in problems[0]


def test_deliberate_exclusion_with_a_reason_is_silent(tmp_path, monkeypatch):
    """Газ работает: осознанное исключение с причиной закрывает вопрос."""
    workflows = _tree(tmp_path, {
        "deploy-worker.yml": REAL_DEPLOY_STEP,
        "deploy-preview.yml": REAL_DEPLOY_STEP,
    })
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS",
                        ("deploy-worker.yml",))
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS",
                        {"deploy-preview.yml": "превью PR, прод не трогает"})
    assert guard.check_registry_completeness(workflows) == []


def test_exclusion_without_a_reason_reddens(tmp_path, monkeypatch):
    """Исключение без причины — тормоз без газа: следующий читатель не
    узнает, можно ли его снимать."""
    workflows = _tree(tmp_path, {"deploy-preview.yml": REAL_DEPLOY_STEP})
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS", ())
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS",
                        {"deploy-preview.yml": "   "})
    problems = guard.check_registry_completeness(workflows)
    assert any("без причины" in p for p in problems), problems


def test_dead_entry_reddens(tmp_path, monkeypatch):
    """Обратная сторона: workflow переименовали, список остался. Ссылка в
    никуда — тот же разрыв между списком и реальностью, только с другого
    конца."""
    workflows = _tree(tmp_path, {"deploy-worker.yml": REAL_DEPLOY_STEP})
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS",
                        ("deploy-worker.yml", "deploy-gone.yml"))
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS", {})
    problems = guard.check_registry_completeness(workflows)
    assert any("deploy-gone.yml" in p and "мёртвая запись" in p for p in problems), problems


def test_listed_workflow_that_stopped_deploying_reddens(tmp_path, monkeypatch):
    """Третий разрыв, и он же — самопроверка признака: файл на месте, но шага
    wrangler в нём нет. Либо деплой оттуда убрали, либо гвардия перестала
    узнавать признак деплоя. Второе опаснее: ложно-зелёная гвардия хуже
    отсутствующей — ровно тот урок, которым оплачен #891/#893."""
    workflows = _tree(tmp_path, {"deploy-worker.yml": NOT_A_DEPLOY})
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS",
                        ("deploy-worker.yml",))
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS", {})
    problems = guard.check_registry_completeness(workflows)
    assert any("ни одного шага с wrangler" in p for p in problems), problems


def test_workflow_in_both_lists_reddens(tmp_path, monkeypatch):
    """«Наблюдаем» и «сознательно не наблюдаем» одновременно — читатель не
    различит, какое из двух правда."""
    workflows = _tree(tmp_path, {"deploy-worker.yml": REAL_DEPLOY_STEP})
    monkeypatch.setattr(guard.repo_invariants, "FRONTEND_DEPLOY_WORKFLOWS",
                        ("deploy-worker.yml",))
    monkeypatch.setattr(guard.repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS",
                        {"deploy-worker.yml": "причина есть, но противоречие"})
    problems = guard.check_registry_completeness(workflows)
    assert any("одновременно" in p for p in problems), problems


# ── Живое дерево ────────────────────────────────────────────────────────────

def test_live_repository_is_consistent():
    """На РЕАЛЬНОМ .github/workflows расхождений нет. Этот тест и есть та
    проверка, которой не существовало: он краснеет в тот день, когда в
    репозиторий приходит новый деплойный workflow мимо списков."""
    assert guard.check_registry_completeness() == []


def test_live_repository_really_has_deploy_workflows():
    """Страховка от ложно-зелёного соседа выше: он зеленеет и на пустом
    множестве (расхождений нет, потому что сравнивать нечего). Дерево обязано
    содержать хотя бы один деплойный workflow, и именно те, что заявлены."""
    found = guard.deploy_workflows()
    assert found, "в дереве не найдено ни одного деплойного workflow — признак уехал"
    assert found == set(guard.repo_invariants.FRONTEND_DEPLOY_WORKFLOWS), found
