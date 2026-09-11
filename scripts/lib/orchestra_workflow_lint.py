#!/usr/bin/env python3
"""Гвардия класса «continue-on-error шаг без id остаётся невидимым» (#887).

`scripts/orchestra/best_effort_outcome_guard.py` читает `${{ toJSON(steps) }}`
в конце job `orchestra`, чтобы поймать РЕАЛЬНЫЙ провал `continue-on-error`
шага (Jobs API после завершения прогона отдаёт только замаскированный
`conclusion`, см. докстринг того скрипта). GitHub Actions кладёт в контекст
`steps` только шаги с явным `id:` — шаг без `id` физически не адресуем, и
`toJSON(steps)` его не покажет ни при каком провале. Значит: любой
`continue-on-error: true` шаг job `orchestra` БЕЗ `id:` — снова невидимый
провал тем же способом, каким уже был живой инцидент 2026-09-10 (прогон
34506949025, шаг «Гвардия протухшей метки blocked»).

`continue_on_error_steps_without_id` — чистая функция без IO: принимает уже
распарсенный YAML-документ workflow (`yaml.safe_load`), возвращает имена
шагов job `orchestra`, у которых `continue-on-error: true`, но нет `id`.
Пустой список — здоровое состояние.

`summary_step_violations` — вторая половина того же класса: сам читатель
реальных outcome (`best_effort_outcome_guard.py`) обязан быть ПРОВЕДЁН в
job — после последнего `continue-on-error` шага стоит шаг, чей `run` зовёт
его, с `if: always()` и `env STEPS_JSON: ${{ toJSON(steps) }}`. Без этой
проверки снятие проводки (удалили шаг, переименовали env, перетащили в
другой job) неотличимо от «провалов нет» — гвардия рантайма отвечает на это
fail loud'ом (нет STEPS_JSON/пустой снимок = exit 1), но лучше ловить то же
нарушение по исходнику в CI, до живого прогона. Шаг-свод не должен нести
`continue-on-error` вовсе: с ним он сам становится «последним
continue-on-error шагом», кандидаты после него исчезают, и линт краснеет
«шаг-свод не найден» — замаскированный провал читателя снова был бы невидим
(его собственный outcome в его же снимок не попадает).

`job_missing` — гвардия обязана отличать «нарушений нет» от «объекта
гвардии нет»: переименованный/удалённый job `orchestra` раньше читался как
здоровое состояние (пустой steps → []), при этом отсутствие ФАЙЛА падало
громко — несимметрично (находка ревью PR #888).

Запуск:
  python scripts/lib/orchestra_workflow_lint.py         # печать отчёта, exit 1 при нарушении
  python -m pytest scripts/lib/test_orchestra_workflow_lint.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRA_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "orchestra.yml"

# Имя job, чей `steps` реально читает best_effort_outcome_guard.py — узкий
# охват НАРОЧНО (см. докстринг модуля): только у этого job есть механизм,
# которому нужен id каждого continue-on-error шага. Другие workflow
# (ai-review.yml/pr-review.yml/plugin-forge.yml/deploy-dsh-edge.yml)
# используют continue-on-error для другого класса задач (best-effort
# уведомления не про этот же механизм) — расширять охват по факту нового
# outcome-guard'а, не заранее.
WATCHED_JOB = "orchestra"

# Проводка читателя реальных outcome (класс #887): шаг-свод обязан звать
# именно этот скрипт и получать снимок контекста `steps` именно этой формой —
# любое другое значение env (переименовали, поменяли выражение) делает снимок
# пустым/чужим, а рантайм-гвардия отвечает на это exit 1.
SUMMARY_STEP_RUN_SUBSTRING = "best_effort_outcome_guard.py"
SUMMARY_STEP_ENV_VAR = "STEPS_JSON"
SUMMARY_STEP_ENV_VALUE = "${{ toJSON(steps) }}"


def continue_on_error_steps_without_id(doc: dict) -> list[str]:
    """doc — распарсенный YAML всего workflow-файла. `on:` в YAML 1.1
    иногда сворачивается в булев ключ `True` парсером — эта функция его не
    трогает, ей нужен только `jobs.<WATCHED_JOB>.steps`."""
    jobs = (doc or {}).get("jobs") or {}
    job = jobs.get(WATCHED_JOB) or {}
    steps = job.get("steps") or []
    violations = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if step.get("continue-on-error") is True and not step.get("id"):
            violations.append(step.get("name") or f"шаг #{index}")
    return violations


def _canonical_expr(value: object) -> str:
    """Форма GitHub-выражения без пробелов: `${{ toJSON(steps) }}` и
    `${{toJSON(steps)}}` — одно и то же выражение, гвардия не должна
    краснеть от форматирования, но обязана — от другого выражения."""
    return re.sub(r"\s+", "", str(value or ""))


def job_missing(doc: dict) -> bool:
    """True — job `orchestra` исчез из workflow (удалён или переименован):
    тогда проверять нечего, и молчаливое «здоров» здесь — та же ложь, что и
    пустой снимок в рантайме."""
    return WATCHED_JOB not in ((doc or {}).get("jobs") or {})


def summary_step_violations(doc: dict) -> list[str]:
    """Проводка читателя реальных outcome по исходнику workflow (класс #887):
    после ПОСЛЕДНЕГО `continue-on-error` шага job `orchestra` обязан стоять
    шаг, чей `run` зовёт `best_effort_outcome_guard.py`, с `if: always()`
    (отработать даже при провале предыдущих) и
    `env STEPS_JSON: ${{ toJSON(steps) }}` (снимок исходов до завершения
    job). Позиция «после последнего» существенна: контекст `steps` шага
    содержит только УЖЕ завершившиеся шаги — стоящий раньше шаг-свод часть
    исходов не увидит. Если шаг-свод сам несёт `continue-on-error`, он
    становится последним таким шагом и кандидаты после него исчезают —
    нарушение поймано тем же сообщением (см. докстринг модуля)."""
    job = ((doc or {}).get("jobs") or {}).get(WATCHED_JOB) or {}
    steps = job.get("steps") or []
    last_coe = None
    for index, step in enumerate(steps):
        if isinstance(step, dict) and step.get("continue-on-error") is True:
            last_coe = index
    if last_coe is None:
        return []  # нет continue-on-error шагов — нет и читателя, которого обязаны проводить
    after = [s for s in steps[last_coe + 1:] if isinstance(s, dict)]
    candidates = [
        step for step in after
        if SUMMARY_STEP_RUN_SUBSTRING in str(step.get("run") or "")
    ]
    if not candidates:
        return [
            f"после последнего continue-on-error шага (#{last_coe}) нет шага, "
            f"заводящего {SUMMARY_STEP_RUN_SUBSTRING} — реальные outcome "
            "continue-on-error шагов никем не читаются, их провал снова "
            "невидим (#887)"
        ]
    violations = []
    expected = _canonical_expr(SUMMARY_STEP_ENV_VALUE)
    for step in candidates:
        name = step.get("name") or "шаг-свод"
        if "always()" not in str(step.get("if") or ""):
            violations.append(
                f"«{name}»: нет if: always() — при провале предыдущего шага "
                "свод не отработает и реальный исход останется непрочитанным (#887)"
            )
        env = step.get("env") or {}
        if _canonical_expr(env.get(SUMMARY_STEP_ENV_VAR)) != expected:
            violations.append(
                f"«{name}»: env {SUMMARY_STEP_ENV_VAR} != "
                f"{SUMMARY_STEP_ENV_VALUE} — снимок исходов не доехал до "
                "гвардии, она ответит fail loud'ом уже в живом прогоне (#887)"
            )
    return violations


def main() -> int:
    if not ORCHESTRA_WORKFLOW_PATH.is_file():
        print(f"::error::orchestra_workflow_lint: файл не найден: {ORCHESTRA_WORKFLOW_PATH}")
        return 1
    doc = yaml.safe_load(ORCHESTRA_WORKFLOW_PATH.read_text(encoding="utf-8"))
    if job_missing(doc):
        print(
            f"::error::orchestra_workflow_lint: job '{WATCHED_JOB}' не найден "
            f"в {ORCHESTRA_WORKFLOW_PATH.name} — объект гвардии исчез, "
            "«здоров» здесь было бы молчаливой ложью (#887)"
        )
        return 1
    violations = continue_on_error_steps_without_id(doc) + summary_step_violations(doc)
    if not violations:
        print(
            f"💚 все continue-on-error шаги job '{WATCHED_JOB}' имеют id, "
            f"читатель {SUMMARY_STEP_RUN_SUBSTRING} проведён (после последнего "
            "из них, if: always(), STEPS_JSON: toJSON(steps))"
        )
        return 0
    for message in violations:
        print(f"::error::orchestra.yml: {message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
