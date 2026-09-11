#!/usr/bin/env python3
"""Единое место правды на «какой workflow обязан отреагировать на мерж,
затронувший этот путь» (issue #955, следствие #929). Читают ДВА независимых
потребителя без второй копии реестра:
  - scripts/orchestra/scheduler.py::after_merge — диспетчит явно (react_to_merge)
  - scripts/orchestra/repo_invariants.py — инвариант 15 проверяет, что диспатч
    реально дал прогон (check_merge_reaction_gaps)

Установленный факт (доказан живым `timeline` PR #868/#872/#878, issue #929):
мерж PR через GITHUB_TOKEN НЕ создаёт push-событие (защита GitHub от
рекурсии) — это постоянное свойство платформы, не регрессия. До 2026-09-10
push-прогоны существовали только потому, что часть PR сливал человек
(admin-мерж личным токеном) — как только слияния стали полностью
автономными (оркестратор), дыра обнажилась. Ни один из workflow реестра не
запустится сам собой после автономного слияния — только явным диспатчем
через Actions API.

Реестр — config/merge-reactions.json, формат:
  {"reactions": [{"prefix": "cf-worker/", "workflow": "deploy-worker.yml",
                  "note": "..."}]}
`prefix` == "" — матчит ЛЮБОЙ файл (весь main), тот же приём, что уже
использовал scheduler.py::dispatch_deploy_on_merge для cf-worker/dsh-edge
(здесь обобщён на пустой префикс).

Дедуп по head_sha (замер #929: `deploy-worker.yml` реально продиспатчен
ДВАЖДЫ на один и тот же коммит — push 2026-08-28T16:12:46Z и dispatch
2026-08-28T16:12:50Z, headSha=dbe8c9956d… у обоих) — `has_run_for_sha`
спрашивает Actions API с фильтром `head_sha` ДО диспатча; `react_to_merge`
пропускает workflow, у которого уже есть прогон на этот sha, тем же кодом,
что закрывает и повторный ручной запуск, и повторный проход диспетчера.

`plugin-forge.yml` сознательно НЕ в реестре (см. config/merge-reactions.json
"excluded") — входы `plugin_path`/`task_issue` резолвятся динамически на
каждый затронутый `plugins-src/<id>`, статическая запись prefix→workflow
этого не выражает; отдельная задача — issue #946.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "config" / "merge-reactions.json"


class RegistryError(RuntimeError):
    """Реестр (config/merge-reactions.json) отсутствует или несёт форму,
    не соответствующую контракту — отличается от RuntimeError сетевого
    происхождения (has_run_for_sha/dispatch_workflow), чтобы вызывающая
    сторона не путала «сеть недоступна сейчас» с «файл репозитория сломан»
    (AGENTS.md, «Fail loud, не silent-wrong»)."""


def load_registry(path: Path = REGISTRY_PATH) -> list[dict]:
    """Читает и валидирует список реакций. Каждая запись обязана нести
    `prefix` (str, может быть пустой строкой — «весь main») и `workflow`
    (непустой str, имя файла workflow). Нарушение контракта — RegistryError
    ДО того, как невалидная запись молча превратится в TypeError где-то в
    середине matching_workflows/react_to_merge."""
    if not path.is_file():
        raise RegistryError(f"реестр реакций на мерж не найден: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RegistryError(f"реестр реакций на мерж {path} — невалидный JSON: {error}") from error
    reactions = raw.get("reactions")
    if not isinstance(reactions, list) or not reactions:
        raise RegistryError(f"реестр реакций на мерж {path} — поле 'reactions' пусто или отсутствует")
    for entry in reactions:
        if not isinstance(entry, dict) or "prefix" not in entry or not entry.get("workflow"):
            raise RegistryError(
                f"реестр реакций на мерж {path} — запись без 'prefix'/'workflow': {entry!r}"
            )
    return reactions


def matching_workflows(files: list[dict], registry: list[dict]) -> list[str]:
    """Workflow'ы реестра, чей `prefix` покрывает хотя бы один файл слитого
    PR — порядок реестра, без дублей (один workflow может встретиться в
    списке лишь один раз, даже если совпало несколько записей с одним и тем
    же именем workflow под разными префиксами — избыточная запись реестра,
    не повод диспетчить/проверять дважды за один проход)."""
    filenames = [(f.get("filename") or "") for f in files]
    seen: set[str] = set()
    result: list[str] = []
    for entry in registry:
        workflow = entry["workflow"]
        if workflow in seen:
            continue
        prefix = entry["prefix"]
        if any(name.startswith(prefix) for name in filenames):
            seen.add(workflow)
            result.append(workflow)
    return result


def has_run_for_sha(gh_func, repo: str, workflow: str, head_sha: str) -> bool:
    """Есть ли ХОТЯ БЫ ОДИН прогон `workflow` с этим `head_sha`, любого
    триггера (push/workflow_dispatch/schedule — не важно, каким путём прогон
    появился, важен сам факт наличия). `head_sha` — параметр Actions API
    (GET .../runs?head_sha=...), а не постраничный перебор и ручное
    сравнение: тот же эндпоинт, что уже читает scheduler.deploy_evidence,
    просто с серверным фильтром вместо клиентского."""
    payload = gh_func(f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={head_sha}&per_page=1") or {}
    return bool(payload.get("workflow_runs"))


def dispatch_workflow(gh_func, repo: str, workflow: str, ref: str = "main") -> None:
    """`gh api -X POST .../dispatches` — тот же вызов и та же обёртка gh(),
    которой уже диспетчит worker.yml (scheduler.py) — не отдельный
    `subprocess.run(["gh","workflow","run",...])`, как было у снятого
    dispatch_deploy_on_merge: единая точка вызова мокается тестами так же,
    как GET-и, второго транспортного пути не заводим."""
    gh_func("-X", "POST", f"repos/{repo}/actions/workflows/{workflow}/dispatches", "-f", f"ref={ref}")


def react_to_merge(
    gh_func, repo: str, files: list[dict], head_sha: str,
    registry: list[dict] | None = None, ref: str = "main",
) -> list[str]:
    """Единая функция диспатча на мерж (issue #955): по реестру определяет,
    какие workflow обязаны отреагировать на слитый PR, и для каждого —
    сначала проверяет, нет ли УЖЕ прогона на этот `head_sha` (закрывает
    дублирование #929: push и явный диспатч на один и тот же коммит), и
    только если нет — диспетчит. Возвращает строки-действия для журнала
    после мержа (after_merge), в порядке реестра.

    `head_sha` обязателен (не Optional): без него дедуп невозможен по
    построению — вызывающая сторона обязана взять `sha` из ответа `PUT
    .../pulls/{n}/merge` (класс #929, ревью: раньше pull-словарь ДО мержа не
    нёс актуальный merge_commit_sha).

    Каждый workflow реестра обрабатывается НЕЗАВИСИМО (находка ревью PR
    #956): сетевой сбой на одном workflow (has_run_for_sha/dispatch_workflow
    подняли RuntimeError) не должен обрывать диспатч ОСТАЛЬНЫХ, у которых
    сбоя нет — сбой конкретного workflow превращается в строку-наблюдение
    (⚠️), а цикл продолжается со следующим. Раньше (до этого фикса)
    исключение одного workflow пробрасывалось наружу необработанным,
    вызывающая сторона (after_merge) ловила его ОДНИМ общим `try` вокруг
    всего вызова — и сбой, скажем, `repo-ci.yml` (первый в реестре) молча
    отменял диспатч `deploy-worker.yml`/`deploy-dsh-edge.yml`, у которых
    сети вполне могло хватить."""
    if not head_sha:
        raise RuntimeError(
            "react_to_merge: head_sha пуст — дедуп по коммиту невозможен, "
            "источник (ответ PUT .../merge) не отдал 'sha'"
        )
    registry = registry if registry is not None else load_registry()
    actions: list[str] = []
    for workflow in matching_workflows(files, registry):
        try:
            exists = has_run_for_sha(gh_func, repo, workflow, head_sha)
        except RuntimeError as error:
            actions.append(
                f"⚠️ {workflow}: проверка существующего прогона на {head_sha[:8]} "
                f"не удалась ({error}) — диспатч НЕ сделан (безопасный отказ, "
                "не гадаем, дублировать или нет)"
            )
            continue
        if exists:
            actions.append(
                f"⏭️ {workflow} уже имеет прогон на {head_sha[:8]} — диспатч пропущен (дедуп, #929)"
            )
            continue
        try:
            dispatch_workflow(gh_func, repo, workflow, ref=ref)
        except RuntimeError as error:
            actions.append(f"⚠️ {workflow}: диспатч не удался ({error})")
            continue
        actions.append(f"🚀 {workflow} запущен (push от GITHUB_TOKEN триггеры не создаёт, #929)")
    return actions
