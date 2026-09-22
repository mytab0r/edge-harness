#!/usr/bin/env python3
"""Тесты гвардии «шаг, читающий API, стоит за гейтом квоты» (#1437).

Поведенческие, не структурные (AGENTS.md, #891/#893): каждый сценарий —
НАСТОЯЩИЙ каталог с настоящими YAML-файлами workflow, который гвардия
разбирает своим обычным путём. Проверка «в исходнике есть имя функции»
покрасилась бы зелёным и при вырезанном теле разбора.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "api_quota_gate_guard", Path(__file__).resolve().parent / "api_quota_gate_guard.py")
guard = importlib.util.module_from_spec(_spec)
sys.modules["api_quota_gate_guard"] = guard
_spec.loader.exec_module(guard)


def _workflow(tmp_path: Path, name: str, text: str) -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory


GATED = """
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Квота GitHub API
        id: quota
        env:
          GH_TOKEN: ${{ github.token }}
        run: python scripts/lib/rate_guard.py --job ci
      - name: Дорогое чтение API
        if: steps.quota.outputs.skip != 'true'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api repos/o/r/issues
"""

UNGATED = """
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Квота GitHub API
        id: quota
        env:
          GH_TOKEN: ${{ github.token }}
        run: python scripts/lib/rate_guard.py --job ci
      - name: Дорогое чтение API
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api repos/o/r/issues
"""


def test_step_behind_the_gate_is_not_a_violation(tmp_path):
    directory = _workflow(tmp_path, "ci.yml", GATED)

    assert guard.ungated_api_steps(directory) == []


def test_step_beside_the_gate_is_found(tmp_path):
    """Живой случай #1437: гейт в job'е ЕСТЬ и отработал, а шаг рядом с ним
    про него не знает — именно так `test` покраснел на PR #1449 при
    корректно пропущенном шаге инвариантов."""
    directory = _workflow(tmp_path, "ci.yml", UNGATED)

    assert guard.ungated_api_steps(directory) == ["ci.yml::test::Дорогое чтение API"]


def test_the_gate_step_itself_is_never_reported(tmp_path):
    """Шаг-гейт сам ходит в API и `if` на себя не несёт. Считать его
    нарушением — замкнуть гвардию на её же предмет: реестр пополнялся бы
    каждым новым гейтом, то есть штрафовал бы ровно за починку."""
    directory = _workflow(tmp_path, "ci.yml", GATED)

    assert not any("Квота GitHub API" in key for key in guard.ungated_api_steps(directory))


def test_job_level_token_is_seen_too(tmp_path):
    """Токен, выданный на уровне job, доходит до КАЖДОГО шага — радиус шире,
    чем у step-level `env`. Гвардия, смотрящая только на шаг, пропустила бы
    самый широкий случай молча."""
    directory = _workflow(tmp_path, "wide.yml", """
name: wide
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Читает API через job-level токен
        run: gh api repos/o/r/pulls
""")

    assert guard.ungated_api_steps(directory) == [
        "wide.yml::build::Читает API через job-level токен"]


def test_personal_pat_is_not_this_guards_business(tmp_path):
    """`secrets.GH_PIPELINE_PAT` — ДРУГОЙ бюджет (личный, не общий 1000/час
    репозитория). Гасить шаг из-за чужой квоты было бы тормозом без причины."""
    directory = _workflow(tmp_path, "pat.yml", """
name: pat
on: [push]
jobs:
  fix:
    runs-on: ubuntu-latest
    steps:
      - name: Пишет под личным PAT
        env:
          GH_TOKEN: ${{ secrets.GH_PIPELINE_PAT }}
        run: gh api repos/o/r/issues
""")

    assert guard.ungated_api_steps(directory) == []


def test_another_jobs_gate_does_not_count(tmp_path):
    """`steps.<id>` виден только внутри своего job'а. Условие, ссылающееся на
    гейт ИЗ ДРУГОГО job'а, в GitHub Actions вычисляется в пустую строку и
    шаг исполняется всегда — то есть «гейт» не гейтит ничего."""
    directory = _workflow(tmp_path, "cross.yml", """
name: cross
on: [push]
jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - name: Квота
        id: quota
        run: python scripts/lib/rate_guard.py --job guard
  work:
    runs-on: ubuntu-latest
    steps:
      - name: Чтение API с чужим условием
        if: steps.quota.outputs.skip != 'true'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api repos/o/r/issues
""")

    assert guard.ungated_api_steps(directory) == [
        "cross.yml::work::Чтение API с чужим условием"]


def test_yaml_suffix_workflows_are_scanned_too(tmp_path):
    """GitHub Actions грузит workflow и из `*.yml`, и из `*.yaml` (класс
    #146/#635). Скан по одному суффиксу оставил бы `evil.yaml` с незакрытым
    чтением API невидимым для гвардии, которая за это и отвечает — дыру нашла
    гвардия репозитория `workflow_glob_suffix_guard`, не автор."""
    directory = _workflow(tmp_path, "evil.yaml", UNGATED)

    assert guard.ungated_api_steps(directory) == ["evil.yaml::test::Дорогое чтение API"]


def test_token_spelling_variants_are_all_recognised(tmp_path):
    """Одно и то же значение GitHub Actions принимает в нескольких
    орфографиях, и сравнение посимвольно видело бы только одну: шаг с
    `${{github.token}}` или с `secrets.GITHUB_TOKEN` проходил бы мимо
    гвардии, а она печатала бы зелёное «все закрыты» (находка ai-review
    PR #1451, без блокировки). Гвардия, держащая класс на одной точной
    строке, — ложно-зелёная по построению."""
    directory = _workflow(tmp_path, "spellings.yml", """
name: spellings
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - name: Без пробелов
        env:
          GH_TOKEN: ${{github.token}}
        run: gh api repos/o/r/issues
  b:
    runs-on: ubuntu-latest
    steps:
      - name: Через secrets
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh api repos/o/r/issues
  c:
    runs-on: ubuntu-latest
    steps:
      - name: Второе имя переменной
        env:
          GITHUB_TOKEN: ${{ github.token }}
        run: gh api repos/o/r/issues
""")

    assert guard.ungated_api_steps(directory) == [
        "spellings.yml::a::Без пробелов",
        "spellings.yml::b::Через secrets",
        "spellings.yml::c::Второе имя переменной",
    ]


# ── check(): реестр долга ────────────────────────────────────────────────────


def test_unregistered_ungated_step_is_a_violation(tmp_path, monkeypatch):
    directory = _workflow(tmp_path, "ci.yml", UNGATED)
    monkeypatch.setattr(guard, "ALLOWED_UNGATED_API_STEPS", {})

    problems = guard.check(directory)

    assert len(problems) == 1
    assert "ci.yml::test::Дорогое чтение API" in problems[0]


def test_registered_step_with_a_reason_passes(tmp_path, monkeypatch):
    directory = _workflow(tmp_path, "ci.yml", UNGATED)
    monkeypatch.setattr(guard, "ALLOWED_UNGATED_API_STEPS",
                        {"ci.yml::test::Дорогое чтение API": "замер цены не сделан, #1437"})

    assert guard.check(directory) == []


def test_registered_step_without_a_reason_is_a_violation(tmp_path, monkeypatch):
    """Запись без причины — это разрешение без основания: следующий читатель
    не отличит «взвесили и решили» от «дописали, чтобы CI позеленел»."""
    directory = _workflow(tmp_path, "ci.yml", UNGATED)
    monkeypatch.setattr(guard, "ALLOWED_UNGATED_API_STEPS",
                        {"ci.yml::test::Дорогое чтение API": "   "})

    problems = guard.check(directory)

    assert any("без причины" in problem for problem in problems)


def test_dead_registry_entry_is_a_violation(tmp_path, monkeypatch):
    """Шаг закрыли гейтом или переименовали, а запись осталась — реестр
    начинает врать про размер долга. Тот же класс, что #891/#893: реестр,
    переживающий переименование молча, ложно-зелёный."""
    directory = _workflow(tmp_path, "ci.yml", GATED)
    monkeypatch.setattr(guard, "ALLOWED_UNGATED_API_STEPS",
                        {"ci.yml::test::Дорогое чтение API": "причина есть"})

    problems = guard.check(directory)

    assert any("мёртвая запись" in problem for problem in problems)


# ── живое дерево репозитория ─────────────────────────────────────────────────


def test_repository_tree_is_clean_against_its_own_registry():
    """На реальных .github/workflows гвардия обязана быть зелёной: либо шаг
    за гейтом, либо в реестре с причиной. Красный здесь — это либо новый
    незакрытый шаг, либо долг, который разобрали и забыли убрать из реестра."""
    assert guard.check() == []


def test_the_repo_ci_test_job_reads_no_api_beside_the_gate():
    """Точечная гвардия на тот самый job, который краснел чужой причиной:
    в `repo-ci.yml::test` незакрытым имеет право остаться РОВНО один шаг —
    перебор каталога гвардий, и он в реестре с отдельной причиной (гасить
    весь каталог из-за квоты неверно по существу)."""
    ungated = [key for key in guard.ungated_api_steps() if key.startswith("repo-ci.yml::test::")]

    assert ungated == ["repo-ci.yml::test::Каталог гвардий scripts/ci/guards — перебор (#749)"], ungated


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
