#!/usr/bin/env python3
"""Авторизация git ставится ДО первого шага, способного писать на data-ветку (#1486).

Класс одной фразой: **шаг, пишущий на data-ветку, идёт в job'е раньше, чем
`gh auth setup-git`, — push отказывает 403, и отказ виден не красным прогоном,
а последствием через час.**

Цена уже оплачена дважды:

  #882  — свежий клон в `$RUNNER_TEMP` не наследует креды `actions/checkout`,
          `gh auth setup-git` в orchestra.yml не вызывался; push падал 403, а
          код трактовал это как «кто-то уже записал» — молчание конвейера
          маскировалось под здоровую дедупликацию гонки;
  #1486 — шаг авторизации стоял перед само-аудитом здоровья, единственным
          тогдашним писателем. С #1465 писателей стало больше: отправители
          сигналов заводят тему категории и сохраняют
          `категория → message_thread_id`. Карта не сохранялась бы, и
          КАЖДЫЙ следующий сигнал заводил бы ещё одну тему той же категории —
          свалка одноразовых тем в чате владельца.

Оба раза починили ОДИН шаг, а порядок для остальных остался неявным. Здесь он
становится проверяемым: список писателей не пишется руками, а ВЫВОДИТСЯ из
исходников по транзитивной зависимости от `data_branch_writer` — новый писатель
попадает под гвардию сам, без правки списка.

Запуск тестов: python -m pytest scripts/lib/test_git_auth_order_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import re
import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#: Модуль, вокруг которого всё и строится: единственное место правды на
#: «append + push на data-ветку» (#882). Зависимость от него — и есть признак
#: писателя; перечислять писателей руками значило бы завести второй список,
#: который разойдётся с кодом.
WRITER_MODULE = "scripts/lib/data_branch_writer.py"

#: Признак шага авторизации. Берётся по КОМАНДЕ, а не по имени шага: имя —
#: проза и меняется, `gh auth setup-git` — факт.
GIT_AUTH_COMMAND_RE = re.compile(r"\bgh\s+auth\s+setup-git\b")

#: Какой скрипт запускает шаг. Та же форма, что у соседней гвардии каталога
#: (`api_quota_gate_guard._GUARD_SCRIPT_MODULE_RE`).
STEP_SCRIPT_RE = re.compile(r"python3?\s+(scripts/[A-Za-z0-9_/.-]+\.py)")


def _module_dependencies(source: str) -> list[str]:
    """Пути модулей, которые файл грузит через `spec_from_file_location`.

    В этом репозитории соседние библиотеки подключаются именно так (относительно
    `__file__`), а не обычным `import` — обычный разбор импортов их не увидит.
    Распознаются литералы вида `... / "lib" / "data_branch_writer.py"` и
    `... / "data_branch_writer.py"`: из них берётся ИМЯ ФАЙЛА, а глубина пути
    значения не имеет."""
    names: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.endswith(".py"):
            names.append(node.value)
    return names


def _python_files() -> dict[str, Path]:
    """Имя файла → путь. Имена скриптов в этом репозитории уникальны; если
    когда-нибудь перестанут быть — гвардия скажет об этом вслух, а не выберет
    молча первый попавшийся."""
    found: dict[str, Path] = {}
    collisions: list[str] = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        if path.name in found:
            collisions.append(path.name)
        found[path.name] = path
    if collisions:
        raise RuntimeError(
            "имена скриптов перестали быть уникальными, разрешение зависимостей "
            f"по имени файла больше не однозначно: {sorted(set(collisions))}")
    return found


def writes_data_branch(entry: Path, files: dict[str, Path] | None = None,
                       seen: set[str] | None = None) -> bool:
    """Транзитивно: сам файл или что-либо, что он грузит, зависит от
    `data_branch_writer`. Транзитивность обязательна — `orchestra.yml`
    запускает `scheduler.py`, который грузит `pulse_guard.py`, который грузит
    `telegram_topics.py`, и только тот пишет на ветку."""
    files = files if files is not None else _python_files()
    seen = seen if seen is not None else set()
    key = entry.name
    if key in seen:
        return False
    seen.add(key)
    if entry.as_posix().endswith(WRITER_MODULE):
        return True
    try:
        source = entry.read_text(encoding="utf-8")
    except OSError:
        return False
    if Path(WRITER_MODULE).name in source:
        return True
    for name in _module_dependencies(source):
        child = files.get(Path(name).name)
        if child is not None and writes_data_branch(child, files, seen):
            return True
    return False


def _steps(job: dict) -> list[dict]:
    steps = job.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _step_scripts(step: dict) -> list[str]:
    run = step.get("run")
    return STEP_SCRIPT_RE.findall(run) if isinstance(run, str) else []


def _is_git_auth(step: dict) -> bool:
    run = step.get("run")
    return bool(isinstance(run, str) and GIT_AUTH_COMMAND_RE.search(run))


#: Job'ы, где писатель достижим транзитивно, но шага `gh auth setup-git` нет,
#: и это ЗАПИСАННЫЙ долг, а не проверенная безопасность. Ключ — «workflow:job»,
#: значение — причина, по одной на запись (тот же приём газа, что у
#: `ALLOWED_CURL_STUB_WITHOUT_OUTFILE`).
#:
#: Честная оговорка, чтобы читатель не принял список за доказательство:
#: достижимость через `pulse_guard` ещё не значит, что шаг реально пишет в
#: своём режиме — гвардия считает ДОСТИЖИМОСТЬ, потому что отличить режимы
#: статически нельзя. Ни в одном из этих job'ов запись на data-ветку не
#: наблюдалась; проверить каждый живым прогоном — отдельная задача, а не
#: условие этого фикса.
ALLOWED_WITHOUT_GIT_AUTH: dict[str, str] = {
    "ai-review.yml:review": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "ai-review.yml:verdict": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "dependabot-alert-watch.yml:watch": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "inbox-issue.yml:create": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "merge-health-watch.yml:watch": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "quota-watch.yml:watch": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "quotas.yml:quotas": "писатель достижим через pulse_guard; записи на data-ветку в этом job'е не наблюдалось",
    "repo-ci.yml:test": "инспектор бежит без --orchestra, эскалации и записи карты тем в этом режиме нет",
}


def problems(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Каждое нарушение — отдельной строкой с адресом: workflow, job, шаг.

    Блокируется ровно тот дефект, который оплачен инцидентами: шаг авторизации
    В JOB'Е ЕСТЬ, но стоит ПОЗЖЕ пишущего. Перестановка шагов местами краснеет.

    Отсутствие шага авторизации вовсе — отдельное состояние: оно тоже означает
    отказ push, но лечится не порядком, а решением про креды в конкретном
    job'е. Такие job'ы живут в `ALLOWED_WITHOUT_GIT_AUTH` с причиной на
    каждый; НЕ записанный туда job краснеет, то есть новый job с писателем
    молча не появится."""
    files = _python_files()
    found: list[str] = []
    for workflow in sorted(workflows_dir.glob("*.yml")):
        try:
            parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            found.append(f"{workflow.name}: не разобран как YAML: {error}")
            continue
        if not isinstance(parsed, dict):
            continue
        jobs = parsed.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = _steps(job)
            # Индекс авторизации берётся ПРЕДПРОХОДОМ, а не по ходу обхода:
            # при обходе «шаг авторизации ещё не встретился» неотличимо от
            # «его нет вовсе», и отказ называл бы не ту причину. Поймано
            # собственным тестом (#1486) — ровно тот дефект, от которого
            # правило «алерт не гадает» и защищает.
            auth_steps = [i for i, st in enumerate(steps) if _is_git_auth(st)]
            auth_at: int | None = auth_steps[0] if auth_steps else None
            for index, step in enumerate(steps):
                if _is_git_auth(step):
                    continue
                for script in _step_scripts(step):
                    path = REPO_ROOT / script
                    if not path.exists() or not writes_data_branch(path, files):
                        continue
                    label = step.get("name") or script
                    if auth_at is None:
                        key = f"{workflow.name}:{job_name}"
                        if key in ALLOWED_WITHOUT_GIT_AUTH:
                            continue
                        found.append(
                            f"{key}: шаг «{label}» пишет на data-ветку ({script}), "
                            "а шага `gh auth setup-git` в job'е нет вовсе — push "
                            "отказывает 403. Либо добавь шаг авторизации, либо впиши "
                            "job в ALLOWED_WITHOUT_GIT_AUTH с причиной")
                    elif auth_at > index:
                        found.append(
                            f"{workflow.name}:{job_name}: шаг «{label}» пишет на "
                            f"data-ветку ({script}) раньше, чем `gh auth setup-git`")
    return found


def main() -> int:
    found = problems()
    for line in found:
        print(f"::error::git-auth-order: {line}", file=sys.stderr)
    if found:
        print("::error::git-auth-order: перенеси `gh auth setup-git` выше первого "
              "пишущего шага job'а (#1486, класс #882)", file=sys.stderr)
        return 1
    print("git-auth-order: во всех job'ах авторизация git стоит раньше писателей data-веток")
    return 0


if __name__ == "__main__":
    sys.exit(main())
