#!/usr/bin/env python3
"""Гвардия класса «встроенный GITHUB_TOKEN не порождает проверок» (issue
#218, следствие #929/#955): любой workflow с `on.push` по `main`, не
зарегистрированный в config/merge-reactions.json (ни как реакция, ни как
осознанное исключение), обязан краснить CI — иначе четвёртый экземпляр
класса появится молча, тем же путём, что #929 нашёл живьём (репо-ci.yml,
codeql.yml, worker-ci.yml без явного диспатча на мерже месяцами).

Устройство:
  1. `push_main_workflows` — скан `.github/workflows/*.yml`: у каждого файла
     смотрим `on.push`, распознавая готчу PyYAML (YAML 1.1: незакавыченный
     ключ `on:` парсится как булево `True`, не строка `'on'` — доказано
     живым прогоном `yaml.safe_load` на repo-ci.yml, issue #955). `on:` сам
     может быть словарём (`{push: {...}}`), списком коротких имён событий
     (`[push, pull_request]`) или одиночной строкой (`push`) — все три формы
     валидны для GitHub Actions и распознаются `_extract_push_trigger`
     единообразно (находка ревью PR #956: раньше только dict-форма
     проверялась, `on: [push]`/`on: push` молча проходили гвардию мимо, хотя
     оба безусловно триггерят push по ВСЕМ веткам, включая main). Покрывает
     main — либо `push:` вовсе без `branches` (матчит все ветки, включая
     list-/строчную форму, где фильтрации по branches в принципе нет), либо
     `branches` явно содержит `'main'`.
  2. `registered_workflows` — объединение `reactions[].workflow` (реестр,
     `merge_reactions.load_registry`) и `excluded[].workflow` (осознанные
     исключения с указанной причиной — например `plugin-forge.yml`, issue
     #946: входы резолвятся динамически, статическая запись реестра это не
     выражает).
  3. `check_registry_completeness` — разница `push_main_workflows -
     registered_workflows` красит гвардию; `registered_workflows -
     существующие файлы .github/workflows/` красит её же (мёртвая запись
     реестра — переименовали/удалили workflow, реестр не обновили,
     `AGENTS.md`, «одно место правды» не терпит зависшую ссылку).

Запуск:
  python scripts/lib/merge_reactions_registry_guard.py
  python -m pytest scripts/lib/test_merge_reactions_registry_guard.py -q
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
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
REGISTRY_PATH = REPO_ROOT / "config" / "merge-reactions.json"

_MR_SPEC = importlib.util.spec_from_file_location(
    "merge_reactions", Path(__file__).resolve().parent / "merge_reactions.py")
merge_reactions = importlib.util.module_from_spec(_MR_SPEC)
_MR_SPEC.loader.exec_module(merge_reactions)  # type: ignore[union-attr]


def _push_covers_main(push_value) -> bool:
    """`push_value` — значение ключа `push` внутри `on:` workflow'а (только
    когда `on:` — словарь, dict-форма). `None` (голый `push:` без вложенного
    отображения) и словарь без ключа `branches` матчат ЛЮБУЮ ветку, включая
    `main` — только явный список `branches`, из которого `main` исключён, не
    покрывает main."""
    if push_value is None:
        return True
    if isinstance(push_value, dict):
        branches = push_value.get("branches")
        if branches is None:
            return True
        return "main" in branches
    # Другая форма (строка/список внутри push:) на 2026-09-11 в дереве не
    # встречается — консервативно считаем покрывающей main, чтобы не
    # молчать о незнакомой форме (fail loud, не тихий пропуск).
    return True


def _extract_push_trigger(on_value) -> tuple[bool, object]:
    """(есть_ли_push_триггер, значение_push_для_фильтра_веток) — единообразно
    для ВСЕХ трёх валидных форм `on:` GitHub Actions (находка ревью PR #956,
    живая мутация критика: `zz-fake-push.yml` с `on: [push]` или `on: push`
    давал `exit=0` — гвардия молчала ровно там, где должна была закраснеть):

      а) словарь `{push: {...}}` / `{push: null}` — есть ключ `push`, его
         значение идёт в `_push_covers_main` как обычно (там же живёт логика
         `branches`);
      б) список `[push, pull_request]` (короткая форма перечисления событий
         без доп. конфигурации КАЖДОГО) — `push` есть, если строка `"push"`
         входит в список; список НЕ умеет фильтровать по `branches` вовсе —
         триггер безусловно покрывает все ветки, включая main (возвращаем
         `None` — тот же сентинел, что «голый push: без отображения» в
         `_push_covers_main`);
      в) строка `"push"` (единственное событие без доп. конфигурации) — та
         же безусловная форма, что и (б).

    Незнакомая форма (не dict/list/str/None) — не молчим (fail loud,
    AGENTS.md): считаем, что push ЕСТЬ и покрывает main, а не тихо
    пропускаем файл мимо проверки."""
    if on_value is None:
        return False, None
    if isinstance(on_value, dict):
        if "push" not in on_value:
            return False, None
        return True, on_value["push"]
    if isinstance(on_value, list):
        return ("push" in on_value), None
    if isinstance(on_value, str):
        return (on_value == "push"), None
    return True, None


def push_main_workflows(workflows_dir: Path = WORKFLOWS_DIR) -> set[str]:
    """Имена файлов (`repo-ci.yml`, не путь) всех workflow с `on.push`,
    покрывающим `main` — по ЛЮБОЙ из трёх форм `on:` (dict/list/строка,
    см. `_extract_push_trigger`)."""
    result: set[str] = set()
    for path in sorted(workflows_dir.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # YAML 1.1: незакавыченный ключ `on:` — булево True, не строка 'on'
        # (проверено живым yaml.safe_load, issue #955) — берём ЛЮБОЙ ключ.
        on_value = doc.get("on", doc.get(True))
        has_push, push_value = _extract_push_trigger(on_value)
        if not has_push:
            continue
        if _push_covers_main(push_value):
            result.add(path.name)
    return result


def registered_workflows(registry_path: Path = REGISTRY_PATH) -> tuple[set[str], set[str]]:
    """(зарегистрированные_как_реакция, исключённые_осознанно) — раздельно,
    чтобы отчёт называл, каким путём workflow учтён (реакция или explicit
    excluded), не просто «учтён»."""
    reactions = {entry["workflow"] for entry in merge_reactions.load_registry(registry_path)}
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    excluded = {entry["workflow"] for entry in raw.get("excluded", []) if entry.get("workflow")}
    return reactions, excluded


def check_registry_completeness(
    workflows_dir: Path = WORKFLOWS_DIR, registry_path: Path = REGISTRY_PATH,
) -> list[str]:
    """Двусторонняя сверка (тот же приём, что уже доказал себя в
    ci_guard_registration_guard.check_no_undeclared_step):
      - новый push-по-main workflow, не учтённый ни реестром, ни excluded —
        класс #929/#218 возвращается молча;
      - запись реестра/excluded, ссылающаяся на workflow, которого больше
        нет в .github/workflows/ — мёртвая ссылка, реестр разошёлся с
        реальностью."""
    present = {path.name for path in workflows_dir.glob("*.y*ml")}
    push_main = push_main_workflows(workflows_dir)
    reactions, excluded = registered_workflows(registry_path)
    accounted = reactions | excluded

    problems: list[str] = []
    for name in sorted(push_main - accounted):
        problems.append(
            f"{name} несёт on.push по main, но не зарегистрирован ни в "
            f"config/merge-reactions.json (reactions), ни в explicit excluded "
            "— мерж через GITHUB_TOKEN не создаёт push-событие (#929): без "
            "диспатча/наблюдения этот workflow не запустится после "
            "автономного слияния. Добавь запись в reactions (если нужен "
            "явный диспатч после мержа) или в excluded с причиной."
        )
    for name in sorted(accounted - present):
        problems.append(
            f"config/merge-reactions.json ссылается на {name!r}, которого "
            "нет в .github/workflows/ — мёртвая запись (workflow "
            "переименован/удалён, реестр не обновлён)"
        )
    return problems


def main() -> int:
    # Глобалы читаются здесь по имени (не как значение по умолчанию
    # параметра функции, которое Python связал бы один раз при определении
    # check_registry_completeness) — тесты монkeypatch'ат WORKFLOWS_DIR/
    # REGISTRY_PATH этого модуля, и main() обязан увидеть подмену.
    problems = check_registry_completeness(WORKFLOWS_DIR, REGISTRY_PATH)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    reactions, excluded = registered_workflows(REGISTRY_PATH)
    print(
        f"merge-reactions-registry: {len(reactions)} workflow в реестре реакций, "
        f"{len(excluded)} осознанно исключены, новых незарегистрированных push-по-main нет"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
