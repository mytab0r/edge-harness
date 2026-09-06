#!/usr/bin/env python3
"""Гвардия класса «метку ставит событие A, снимает только событие B, которое
не покрывает способ реального исправления» (#599).

Живой случай (PR #586): `contract_check.py` читает ТЕЛО PR (`pull["body"]`,
класс — запрещённая директива Closes/Fixes/Resolves рядом с #N) и снимает
`contract:failed` при следующем зелёном прогоне (`contract_check.py:249-253`).
Прогон запускает `orchestra.yml` job `contract`, но до фикса #599 его
`pull_request.types` не включал `edited` — правка ТЕЛА PR (единственный
реальный способ убрать директиву без пустого коммита) не запускала прогон,
метка висела вечно. `docs/agents/LABELS.md` называет газ «следующий прогон
контракта» — газ был объявлен, но недостижим для этого конкретного пути
исправления.

Два теста:

  1. `test_orchestra_contract_reacts_to_pr_body_edits` — прямая регрессия на
     сам фикс #599: `orchestra.yml` слушает `pull_request: edited`.
     Мутация: убрать `edited` из `types:` — тест красный.

  2. `test_no_new_body_gated_label_release_without_edited` — гвардия КЛАССА,
     не случая: сканирует `.github/workflows/*.yml`, находит job'ы, реально
     достижимые событием `pull_request` (единственный триггер workflow'а,
     или job с `if: github.event_name == 'pull_request'` при нескольких
     триггерах), и python-скрипты, которые они запускают. Скрипт считается
     «телозависимым газом метки», если И читает `pull["body"]`/
     `pull.get("body")` (сам текст PR — то, что чинится правкой без коммита),
     И удаляет метку GitHub API-вызовом `-X DELETE .../labels/...`. Такой
     скрипт обязан жить в job'е, чей `pull_request.types` включает `edited`
     — иначе газ для этого класса поломок недостижим тем же путём, что #599.

     ALLOWLIST — уже НАЙДЕННЫЙ, но ещё не починенный экземпляр этого же
     класса (issue #601, `pr-review.yml`/`check_pr.py`::revert_ok_gas):
     оставлен видимым здесь с номером задачи, а не тихо потушен — новый
     необъявленный экземпляр по-прежнему красит тест.

Тест кормится прод-формой (yaml.safe_load реальных workflow-файлов и реальных
скриптов scripts/), не пересказом — тот же приём, что test_gate_triggers.py и
test_pagination_guard.py.

Запуск: python -m pytest scripts/lib/test_pr_body_label_release_reachable.py -q
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ORCHESTRA = WORKFLOWS_DIR / "orchestra.yml"

GH_DEFAULT_PR_TYPES = ["opened", "synchronize", "reopened"]

# Признак 1: скрипт читает текст ТЕЛА PR как вход решения (не тело issue —
# разграничение не нужно здесь на уровне регэкспа: скрипты, читающие тело
# ISSUE, живут в job'ах, не гейтящихся на pull_request, и просто не попадают
# в _qualifying_jobs ниже).
BODY_READ_RE = re.compile(r'pull(?:_after_\w+)?(?:\[["\']body["\']\]|\.get\(["\']body["\'])')

# Признак 2: удаление метки через gh api — `"-X", "DELETE", f"…/labels/…"`.
# `\s*` между токенами намеренно матчит перенос строки (как в pagination_guard):
# contract_check.py разносит "-X", "DELETE" и f-строку по двум строкам.
DELETE_LABEL_RE = re.compile(r'"-X",\s*"DELETE",?\s*f?"[^"]*/labels/', re.DOTALL)

# Извлечение вызываемого .py из текста run: — все прод-примеры сегодня зовут
# `python <путь> …` из корня репозитория (без cd/working-directory).
PY_INVOCATION_RE = re.compile(r'\bpython\s+(\S+\.py)\b')

# Уже найденный, ещё не починенный экземпляр этого класса (#601): оставлен
# видимым с номером задачи, чтобы новый необъявленный экземпляр по-прежнему
# красил тест, а не тонул рядом со старым.
ALLOWLIST = {
    ("pr-review.yml", "review"):
        "issue #601 — revert_ok_gas (scripts/review/check_pr.py) читает тело "
        "PR и снимает review:changes-requested, но pr-review.yml не слушает "
        "edited/labeled; чинить нужно с отдельной проверкой лишних платных "
        "прогонов ai-review.yml через workflow_run — та же осторожность, что "
        "у #599, отдельной узкой задачей, не довеском к этому PR.",
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _on_block(doc: dict, path: Path) -> dict:
    """PyYAML (YAML 1.1) парсит голый ключ `on:` как булево True — прод-файлы
    пишут его без кавычек. Тот же приём, что test_gate_triggers.py::_triggers."""
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"{path.name}: нет блока on:"
    return triggers


def _pr_types(on_block: dict) -> list[str] | None:
    """Типы pull_request этого workflow'а, либо None — pull_request не триггер
    вовсе. Явный `types:` — как есть; голый `pull_request:` (без types) —
    дефолт GitHub (opened/synchronize/reopened, документировано в GH Actions)."""
    pr_trigger = on_block.get("pull_request")
    if pr_trigger is None:
        return None
    if pr_trigger is True or not pr_trigger:
        return list(GH_DEFAULT_PR_TYPES)
    return list(pr_trigger.get("types") or GH_DEFAULT_PR_TYPES)


def _qualifying_jobs(doc: dict, on_block: dict) -> dict:
    """Job'ы, реально достижимые событием pull_request: единственный триггер
    workflow'а — все job'ы; несколько триггеров — только job с явным
    `if: github.event_name == 'pull_request'` (символично противоположному
    `!= 'pull_request'`, см. orchestra.yml/scheduler.py)."""
    if _pr_types(on_block) is None:
        return {}
    jobs = doc.get("jobs") or {}
    only_trigger = len(on_block) == 1
    result = {}
    for job_id, job in jobs.items():
        condition = job.get("if") or ""
        if only_trigger or "event_name == 'pull_request'" in condition:
            result[job_id] = job
    return result


def _invoked_scripts(job: dict) -> list[str]:
    scripts = []
    for step in job.get("steps") or []:
        run_text = step.get("run")
        if isinstance(run_text, str):
            scripts.extend(PY_INVOCATION_RE.findall(run_text))
    return scripts


def _is_body_gated_label_release(script_path: Path) -> bool:
    if not script_path.exists():
        return False
    text = script_path.read_text(encoding="utf-8")
    return bool(BODY_READ_RE.search(text)) and bool(DELETE_LABEL_RE.search(text))


def test_orchestra_contract_reacts_to_pr_body_edits():
    doc = _load(ORCHESTRA)
    on_block = _on_block(doc, ORCHESTRA)
    types = _pr_types(on_block)
    assert types is not None, "orchestra.yml: нет триггера pull_request"
    assert "edited" in types, (
        f"orchestra.yml: pull_request.types {types} без edited — job contract "
        "(contract_check.py читает тело PR, снимает contract:failed) не "
        "перезапускается правкой тела PR без нового коммита (#599, живой "
        "случай PR #586: метка поставлена, тело исправлено, ни одного "
        "unlabeled после)"
    )


def test_no_new_body_gated_label_release_without_edited():
    offenders = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = _load(path)
        on_block = _on_block(doc, path)
        types = _pr_types(on_block)
        if types is None:
            continue
        for job_id, job in _qualifying_jobs(doc, on_block).items():
            if "edited" in types:
                continue
            for script in _invoked_scripts(job):
                script_path = REPO_ROOT / script
                if not _is_body_gated_label_release(script_path):
                    continue
                key = (path.name, job_id)
                if key in ALLOWLIST:
                    continue
                offenders.append(
                    f"{path.name}::{job_id} → {script} (types={types}, "
                    "читает тело PR и удаляет метку)"
                )
    assert offenders == [], (
        "Job читает тело PR и снимает метку по решению, но pull_request.types "
        "не включает edited — правка тела без нового коммита не снимет метку "
        f"(класс #599): {offenders}. Либо добавь edited в types (проверь "
        "остальные job'ы того же workflow на лишние платные прогоны, как в "
        "#599), либо, если фикс отложен осознанно, назови номер задачи в "
        "ALLOWLIST этого файла."
    )
