#!/usr/bin/env python3
"""Шаг workflow, читающий GitHub API, обязан стоять за гейтом квоты (#1437).

Класс, названный одной фразой: **предохранитель по квоте `GITHUB_TOKEN`
прикручен к отдельным дорогим шагам вручную, а не ко всем читателям API —
и незакрытый шаг красит чужой PR причиной, к которой тот не относится.**

Живой случай, прогон 35715554412 (PR #1449, job `test`, 2026-09-22):

    10:24:21  rate_guard: квота ок (5000/5000)        ← гейт #454 отработал
    10:27:02  ##[error]repo_invariants: … HTTP 403    ← и не спас
    10:29:54  ##[error]upstream-namespace-collision: … HTTP 403

Второй отказ пришёл из шага, который про гейт не знал вовсе: гейт стоял в
СЕРЕДИНЕ job'а, ниже трёх шагов, которые сами ходят в API. `test` —
обязательная проверка, и покраснел он на исчерпанной общей квоте, а не на
диффе автора.

Что проверяет эта гвардия: каждый шаг любого workflow, которому передан
`GH_TOKEN: ${{ github.token }}`, либо стоит за условием
`steps.<id>.outputs.skip != 'true'` гейта `rate_guard.py` из своего же job'а,
либо перечислен в `ALLOWED_UNGATED_API_STEPS` с причиной. Реестр —
измеренный долг, а не разрешение: он существует, чтобы долг был виден
числом и не рос молча (тот же приём, что `ALLOWED_CURL_STUB_WITHOUT_OUTFILE`
и соседние реестры репозитория).

Газ (AGENTS.md, «тормоз без газа не принимается»): шаг снимается из реестра
тем же PR, который ставит ему `if: steps.quota.outputs.skip != 'true'` и
добавляет гейт в его job, если гейта там ещё нет. Новая запись в реестре
обязана нести причину — пустая строка не проходит.

Запуск: python scripts/lib/api_quota_gate_guard.py
Тесты:  python -m pytest scripts/lib/test_api_quota_gate_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Признак «шаг ходит в GitHub API нашим общим бюджетом»: ему передан токен
# job'а. Секретный PAT (`secrets.GH_PIPELINE_PAT`) — ДРУГОЙ бюджет (личный
# 5000/час, не общий 1000/час репозитория), и под это правило не попадает:
# смешивать их значило бы гасить шаги из-за чужой квоты.
JOB_TOKEN_VALUE = "${{ github.token }}"

# Признак самого гейта — вызов rate_guard.py в `run` шага.
GATE_SCRIPT = "rate_guard.py"

# Измеренный долг на 2026-09-22: 29 шагов в 17 workflow читают API общим
# бюджетом мимо гейта (замер — эта же гвардия на дереве PR, не оценка на
# глаз). Ключ — «<файл>::<job>::<имя шага>», значение — ПОЧЕМУ запись ещё
# здесь; пустая причина не проходит.
#
# Причин ровно три, и они РАЗНЫЕ по цене закрытия — одна общая формулировка
# смазала бы именно то, что решает порядок разбора:
#   _GATE_EXISTS_STEP_NOT_BEHIND_IT — гейт в workflow есть, шаг мимо него
#       (7 записей, самый дешёвый долг);
#   _SINGLE_STEP_WORKFLOW — гейта в job'е нет вовсе (21 запись);
#   _GUARD_CATALOGUE — гасить целиком неверно по существу (1 запись).
_SINGLE_STEP_WORKFLOW = (
    "workflow-одиночка: гейта квоты в job'е нет вовсе, шаги реагируют на событие "
    "и делают единицы вызовов. Участие именно этих прогонов в исчерпании общего "
    "бюджета НЕ измерено — заводить гейт до замера значит чинить незамеренное "
    "(AGENTS.md). Разбирается отдельной узкой задачей на workflow, со своим замером"
)

_GATE_EXISTS_STEP_NOT_BEHIND_IT = (
    "гейт квоты в этом workflow УЖЕ есть, но конкретно этот шаг за ним не стоит — "
    "узкий и дешёвый долг: нужно одно условие `if` и проверка, что пропуск шага "
    "не ломает смысл прогона (у ai-review/orchestra пропуск меняет вердикт и такт, "
    "поэтому не делается вслепую этим PR)"
)

_GUARD_CATALOGUE = (
    "гасить целиком нельзя: шаг гоняет ВЕСЬ каталог scripts/ci/guards, где "
    "подавляющее большинство гвардий в API не ходит, и пропуск по чужой квоте снял "
    "бы проверки, к квоте отношения не имеющие. GH_TOKEN здесь передан на будущее "
    "(см. комментарий шага в repo-ci.yml). Правильное закрытие — гейт внутри тех "
    "гвардий каталога, которые реально читают API, когда такие появятся"
)

ALLOWED_UNGATED_API_STEPS: dict[str, str] = {
    'ai-review.yml::review::Нужен ли дорогой прогон (сверка отпечатка диффа)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::review::Сбор фактов и промпт (доверенный шаг)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::verdict::Вердикт: комментарий + метка':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'ai-review.yml::verdict::Разбудить оркестратор (#297): вердикт AI готов, не ждать расписания':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'branch-protection-watch.yml::watch::Сверить защиту main против EXPECTED_* и сигналить при ослаблении':
        _SINGLE_STEP_WORKFLOW,
    'checklist-tail-triage.yml::triage::Наследование области хвостом чеклиста ревью':
        _SINGLE_STEP_WORKFLOW,
    'dependabot-alert-watch.yml::watch::Наблюдатель алертов Dependabot':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Канарейка runner-bridge (#390/#945)':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Коллизия имён с апстримом (namespace/label/inject/slot)':
        _SINGLE_STEP_WORKFLOW,
    'deploy-dsh-edge.yml::deploy::Скачать плагины и сверить sha256':
        _SINGLE_STEP_WORKFLOW,
    'deploy-worker.yml::deploy::Эскалация автооткота (#120/Telegram)':
        _SINGLE_STEP_WORKFLOW,
    'dsh-edge-pr-smoke.yml::pr-smoke::Скачать плагины и сверить sha256':
        _SINGLE_STEP_WORKFLOW,
    'inbox-issue.yml::create::Issue из директивы инбокса':
        _SINGLE_STEP_WORKFLOW,
    'merge-health-watch.yml::watch::Регрессия worker_success_rate, привязанная к слиянию':
        _SINGLE_STEP_WORKFLOW,
    'orchestra.yml::contract::Контракт PR ↔ задача':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'orchestra.yml::orchestra::Гвардия waiting:owner — задача ждёт решения владельца (#470)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'orchestra.yml::orchestra::Гвардия протухшей метки blocked (#334)':
        _GATE_EXISTS_STEP_NOT_BEHIND_IT,
    'owner-decision.yml::apply::Комментарий с решением владельца':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::forge::Интеграционный дым (локальная сборка с плагином)':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::forge::Создать/обновить релиз plugins-<id>-v<version>':
        _SINGLE_STEP_WORKFLOW,
    'plugin-forge.yml::prepare::Определить состав форжа (dispatch/push/schedule)':
        _SINGLE_STEP_WORKFLOW,
    'pr-review.yml::review::Детерминированное ревью диффа':
        _SINGLE_STEP_WORKFLOW,
    'pr-review.yml::review::Разбудить оркестратор (#297): вердикт изменился, не ждать расписания':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Гейт: нужен ли реальный замер':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Замер квоты (Cloudflare)':
        _SINGLE_STEP_WORKFLOW,
    'quota-watch.yml::watch::Замер лимита GitHub API':
        _SINGLE_STEP_WORKFLOW,
    'quotas.yml::quotas::Снять срез квот':
        _SINGLE_STEP_WORKFLOW,
    'repo-ci.yml::test::Каталог гвардий scripts/ci/guards — перебор (#749)':
        _GUARD_CATALOGUE,
    'secret-scan.yml::full-history::Эскалация в пул задач при падении шага (issue, без значений секрета)':
        _SINGLE_STEP_WORKFLOW,
}


def _steps_of(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def _uses_job_token(step: dict, job: dict, workflow: dict) -> bool:
    """Токен job'а виден шагу и со своего `env:`, и с уровня job/workflow —
    проверять только шаг значило бы не заметить самый широкий радиус."""
    for scope in (step, job, workflow):
        env = scope.get("env") if isinstance(scope, dict) else None
        if isinstance(env, dict) and str(env.get("GH_TOKEN", "")).strip() == JOB_TOKEN_VALUE:
            return True
    return False


def _gate_ids(job: dict) -> set[str]:
    """id шагов этого job'а, которые и есть гейт квоты."""
    return {
        step["id"] for step in _steps_of(job)
        if step.get("id") and GATE_SCRIPT in str(step.get("run", ""))
    }


def _is_gated(step: dict, gate_ids: set[str]) -> bool:
    condition = str(step.get("if", ""))
    return any(f"steps.{gate_id}.outputs.skip" in condition for gate_id in gate_ids)


def ungated_api_steps(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Ключи «<файл>::<job>::<имя шага>» всех шагов, читающих API токеном
    job'а мимо гейта. Сам шаг-гейт в список не попадает: он и есть проверка,
    гасить его собственным выводом — замкнуть гейт на себя."""
    found: list[str] = []
    # Оба суффикса: GitHub Actions грузит workflow и из `*.yml`, и из
    # `*.yaml` (класс #146/#635, гвардия scripts/lib/workflow_glob_suffix_
    # guard.py — она же эту дыру здесь и нашла). Скан по одному суффиксу
    # сделал бы `evil.yaml` с незакрытым чтением API невидимым для гвардии,
    # которая ровно за это и отвечает.
    workflow_files = sorted(
        list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml")))
    for path in workflow_files:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        jobs = workflow.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            gate_ids = _gate_ids(job)
            for step in _steps_of(job):
                if GATE_SCRIPT in str(step.get("run", "")):
                    continue
                if not _uses_job_token(step, job, workflow):
                    continue
                if _is_gated(step, gate_ids):
                    continue
                found.append(f"{path.name}::{job_name}::{step.get('name', '<без имени>')}")
    return found


def check(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Нарушения: незакрытый шаг без записи в реестре, запись без причины,
    мёртвая запись (шага больше нет — реестр не должен переживать
    переименование молча, это тот же класс, что #891/#893)."""
    problems: list[str] = []
    ungated = ungated_api_steps(workflows_dir)

    for key in ungated:
        if key not in ALLOWED_UNGATED_API_STEPS:
            problems.append(
                f"{key}: шаг читает GitHub API токеном job'а мимо гейта квоты. "
                f"Поставь `if: steps.<id гейта>.outputs.skip != 'true'` (и сам гейт "
                f"`{GATE_SCRIPT}` в этот job, если его там нет) либо внеси в "
                f"ALLOWED_UNGATED_API_STEPS с причиной")

    for key, reason in ALLOWED_UNGATED_API_STEPS.items():
        if not str(reason).strip():
            problems.append(f"{key}: запись в ALLOWED_UNGATED_API_STEPS без причины")
        if key not in ungated:
            problems.append(
                f"{key}: мёртвая запись в ALLOWED_UNGATED_API_STEPS — такого незакрытого "
                f"шага в .github/workflows/ больше нет (шаг закрыт гейтом, переименован "
                f"или удалён). Убери запись")
    return problems


def main() -> int:
    problems = check()
    if problems:
        for problem in problems:
            print(f"::error::api-quota-gate: {problem}")
        return 1
    print(
        f"api-quota-gate: все шаги, читающие API токеном job'а, закрыты гейтом квоты "
        f"или перечислены с причиной ({len(ALLOWED_UNGATED_API_STEPS)} записей долга)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
