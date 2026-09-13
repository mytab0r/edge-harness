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

# Извлечение вызываемого .py из текста run: — прод-примеры зовут скрипт из
# корня репозитория (без cd/working-directory) либо как `python <путь>`,
# либо как `python3 <путь>` (owner-decision.yml:56 уже так делает).
PY_INVOCATION_RE = re.compile(r'\bpython3?\s+(\S+\.py)\b')

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
    вовсе (ключа `pull_request` нет в блоке `on:` совсем). Явный `types:` —
    как есть; голый `pull_request:` без значения — YAML 1.1 парсит это как
    None (ключ ЕСТЬ, значение null), что не отличить от True на уровне
    `.get()` — оба случая означают дефолт GitHub (opened/synchronize/reopened,
    документировано в GH Actions). Отсутствие ключа проверяем ДО `.get()`,
    иначе многотриггерный workflow вида `on: {pull_request:, push: {...}}`
    (repo-ci.yml, worker-ci.yml, codeql.yml — все с настоящим pull_request)
    читался бы как «pull_request не триггер вовсе», и job'ы этих файлов
    целиком выпадали бы из обхода."""
    if "pull_request" not in on_block:
        return None
    pr_trigger = on_block.get("pull_request")
    if pr_trigger is True or not pr_trigger:
        return list(GH_DEFAULT_PR_TYPES)
    return list(pr_trigger.get("types") or GH_DEFAULT_PR_TYPES)


def _qualifying_jobs(doc: dict, on_block: dict) -> dict:
    """Job'ы, реально достижимые событием pull_request: единственный триггер
    workflow'а — все job'ы; несколько триггеров — все job'ы, КРОМЕ тех, чей
    `if` явно исключает pull_request (`!= 'pull_request'`, см.
    orchestra.yml/job orchestra). Job без `if` вовсе (как `test` в
    repo-ci.yml, где живёт половина гвардий репозитория) запускается на
    каждом триггере workflow'а, включая pull_request, — исключать его из
    обхода только потому что `if` не назван явно, значит не видеть ровно те
    job'ы, где новый такой скрипт вероятнее всего появится."""
    if _pr_types(on_block) is None:
        return {}
    jobs = doc.get("jobs") or {}
    only_trigger = len(on_block) == 1
    result = {}
    for job_id, job in jobs.items():
        condition = job.get("if") or ""
        excludes_pr = "event_name != 'pull_request'" in condition
        if only_trigger or not excludes_pr:
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
    # `.yml` И `.yaml` (GitHub Actions грузит оба, находка AI-ревью #146,
    # класс держит гвардия scripts/lib/workflow_glob_suffix_guard.py) — тот
    # же приём, что scripts/lib/collect_labels.py::_scan_workflow_files.
    offenders = []
    for path in sorted(list(WORKFLOWS_DIR.glob("*.yml")) + list(WORKFLOWS_DIR.glob("*.yaml"))):
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


def test_pr_types_distinguishes_absent_key_from_null_value():
    """Мутационная пара (находка AI-ревью PR #602): до фикса
    `on_block.get("pull_request") is None` не отличал «ключа `pull_request` в
    `on:` нет вовсе» от «ключ есть, значение null» — PyYAML именно так парсит
    голый `pull_request:` без `types:` в многотриггерном `on:`. repo-ci.yml —
    прод-форма обоих случаев разом: `pull_request:` без значения соседствует
    с `push:`. Мутация: вернуть `if pr_trigger is None: return None` без
    проверки `"pull_request" not in on_block` — этот тест красный, и
    `_qualifying_jobs`/`test_no_new_body_gated_label_release_without_edited`
    целиком теряют repo-ci.yml (и по той же причине worker-ci.yml, codeql.yml)."""
    path = WORKFLOWS_DIR / "repo-ci.yml"
    doc = _load(path)
    on_block = _on_block(doc, path)
    assert "pull_request" in on_block, (
        "фикстура ожидает ключ pull_request в on: repo-ci.yml — иначе тест "
        "не воспроизводит разбираемый случай"
    )
    assert on_block.get("pull_request") is None, (
        "фикстура ожидает голый `pull_request:` без types — иначе тест не "
        "воспроизводит случай «ключ есть, значение null»"
    )
    types = _pr_types(on_block)
    assert types == GH_DEFAULT_PR_TYPES, (
        f"repo-ci.yml: pull_request — настоящий триггер (соседствует с push "
        f"в on:), обязан читаться как дефолтные типы {GH_DEFAULT_PR_TYPES}, "
        f"получено {types!r} — «ключ есть с null» перепутан с «ключа нет»"
    )


def test_qualifying_jobs_includes_job_without_if():
    """Мутационная пара (находка AI-ревью PR #602): до фикса job без `if` в
    многотриггерном workflow не засчитывался достижимым (условие требовало
    буквального `event_name == 'pull_request'`). repo-ci.yml::test — прод-
    форма: без `if` вовсе, реально запускается на каждом pull_request, и
    именно в нём живёт половина гвардий репозитория. Мутация: вернуть старое
    условие `only_trigger or "event_name == 'pull_request'" in condition` —
    этот тест красный, `test` пропадает из обхода телозависимых газов метки."""
    path = WORKFLOWS_DIR / "repo-ci.yml"
    doc = _load(path)
    on_block = _on_block(doc, path)
    assert doc["jobs"]["test"].get("if") is None, (
        "фикстура ожидает job test без if — иначе тест не воспроизводит "
        "разбираемый случай"
    )
    jobs = _qualifying_jobs(doc, on_block)
    assert "test" in jobs, (
        "repo-ci.yml::test запускается на pull_request (нет if, исключающего "
        "его через event_name != 'pull_request') — обязан попасть в обход "
        "телозависимых газов метки, а не только job'ы с явным == "
        "'pull_request'"
    )
