#!/usr/bin/env python3
"""Гвардия механизма регистрации CI-гвардий (#749): попытка добавить шаг
гвардии рукописно в `.github/workflows/repo-ci.yml` обязана краснеть, а не
пройти молча — иначе класс «репо-ci.yml как генератор конфликтов» (замер
issue #749: 17 из 40 открытых PR правили один файл ради дописывания своего
`- name:`/`run:`) вернётся следующим же PR, забывшим про каталог
`scripts/ci/guards/`.

Устройство:
  1. `guard_step_names` читает job `test` `.github/workflows/repo-ci.yml`
     (yaml.safe_load, не текстовый греп) и возвращает имена шагов,
     совпадающих с соглашением именования гвардий (`Тест…`/`Гвардия…`/
     `Smoke…`/`Юнит-тест…`, якорь на начало строки, регистронезависимо) —
     тот же приём, что уже используют exec_bit_guard.py/orphan_test_guard.py
     для похожей задачи различения «это гвардия» от «это инфраструктурный
     шаг» (checkout/setup-python/…).
  2. `ALLOWLIST` — замороженный список имён, остающихся рукописными шагами
     НА МОМЕНТ этого PR (#749: миграция механизма + одна гвардия как
     доказательство критерия приёмки; полная миграция остальных 66 —
     отдельная задача, объём которой обязан убывать, см. proposal.md
     openspec/changes/ci-guard-catalog). Список синхронизируется РУКАМИ при
     каждой следующей миграции — та же форма, что уже доказала себя в
     `scripts/lib/test_label_registry.py`/`test_infra_gh_inventory.py`.
  3. `check_no_undeclared_step` — двусторонняя сверка множества ИЗ ФАЙЛА
     против ALLOWLIST:
       - новое имя в файле, которого нет в ALLOWLIST → кто-то дописал
         гвардию рукописно вместо каталога (класс #749) — красный список с
         точным именем и подсказкой перенести в `scripts/ci/guards/`;
       - имя ALLOWLIST, которого больше нет в файле → запись устарела
         (шаг мигрирован/переименован/удалён) — тоже красный, чтобы
         ALLOWLIST не тащил мёртвые записи молча (тот же приём, что у
         `test_infra_gh_inventory.py`: мёртвая строка — тоже находка).

Запуск:
  python scripts/lib/ci_guard_registration_guard.py
  python -m pytest scripts/lib/test_ci_guard_registration_guard.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_CI = REPO_ROOT / ".github" / "workflows" / "repo-ci.yml"

# Якорь на начало строки — тот же приём, что уже используют
# exec_bit_guard.py/orphan_test_guard.py для похожего различения.
_GUARD_NAME_RE = re.compile(r"^(Тест|Гвардия|Smoke|Юнит-тест)", re.IGNORECASE)

# Замороженный список имён шагов job `test` .github/workflows/repo-ci.yml,
# остающихся рукописными на момент #749 (миграция механизма каталога +
# одна гвардия как доказательство критерия приёмки). Обновляй руками при
# каждой следующей миграции в scripts/ci/guards/ — число обязано убывать
# (условие issue #749, «оговорка про миграцию существующих 47/66/… шагов»).
ALLOWLIST = frozenset({
    'Тесты графа блокировок пула task_deps',
    'Тесты create_pool_issue — отказ до сетевого вызова при отсутствии task',
    'Тесты кампании dispatch-tail',
    'Тесты замера rows_read',
    'Тесты предохранителя конвейера orchestra',
    'Тесты гвардии waiting:owner (метка, варианты, эскалация)',
    'Тесты применения решения владельца (owner-decision)',
    'Гвардия синхронности формата кнопки Telegram (TS ↔ Python)',
    'Тесты инвариантов состояния репозитория',
    'Тесты автофикса архивации openspec/changes (#493/#506)',
    'Тесты гвардии бита исполнения',
    'Гвардия бита исполнения — живой снимок workflow (#510/#516)',
    'Тесты экономии диспатча wake_orchestra (#456)',
    'Тесты сборщика квот',
    'Тесты планировщика orchestra (логин в морду, архив сессий, петля состояния PR)',
    'Тесты механического ребейза конфликтных PR',
    'Тесты контракта PR ↔ задача',
    'Тесты детектора устойчивого простоя',
    'Тесты сигнала дрейфа пина апстрима (#134)',
    'Тесты сигнала ослабления защиты main (#370)',
    'Тесты гвардии «PR не заводится на эпик»',
    'Smoke «task-branch отказывает на эпике»',
    'Тесты меток-вердиктов ревью и выборочного подтягивания веток',
    'Тесты аренды задачи claim_task',
    'Тесты извлечения номера задачи task_ref',
    'Тесты исхода PR ветки pr_outcome',
    'Гвардия «резолвер PR → задача, не подстрока прозы»',
    'Гвардия пагинации — списочный ответ GitHub API без обхода страниц',
    'Гвардия «сырой stderr клиента модели без redact» (#743)',
    'Гвардия «газ метки достижим правкой тела PR»',
    'Гвардия пакетного менеджера standalone dsh-edge (#43)',
    'Тесты выбора свободной задачи free_task',
    'Гвардия разделения GitHub-токенов',
    'Гвардия реестра меток — у каждого тормоза-метки объявлен газ',
    'Гвардия инвентаря INFRA-GH.md — каждый workflow назван в таблице',
    'Тесты канарейки осиротевших тестов',
    'Гвардия паритета маскирования секретов (bash ↔ TS)',
    'Тесты AI-ревью (второй гейт)',
    'Гвардия гейта первого ревью ai-review.yml (#204)',
    'Тесты file_tasks.py — фильтр МАСШТАБ (#426)',
    'Тесты review_checklist.py — категория ЗАМЕЧАНИЕ (#462)',
    'Гвардия триггеров гейтов ревью (#208)',
    'Юнит-тесты плагина dsh-hands-streamer',
    'Юнит-тесты плагина plugin-manager',
    'Юнит-тесты общего разбора тела ошибки (plugins-src/shared)',
    'Юнит-тесты плагина runner-bridge',
    'Юнит-тесты логики инструментов интеграций',
    'Юнит-тесты клиентского бандла интеграций',
    'Юнит-тесты плагина provider-registry',
    'Гвардия «каталог плагинов не отравляет литерал namespace»',
    'Smoke task-branch — проверка на входе и рабочее дерево',
    'Smoke pr-create — Closes/Fixes/Resolves отклоняется до вызова gh (#496)',
    'Тесты паритета scripts/git/pr-create ↔ contract_check.py',
    'Smoke issue-create — issue без task отклоняется до вызова gh (#526)',
    'Тесты чистой логики duplicate_guard (#566)',
    'Smoke issue-create — похожий заголовок отклоняется до вызова gh (#566)',
    'Smoke issue-create — приоритет не объявлен явно отклоняется до вызова gh (#695)',
    'Smoke доводки PR — инструментарий из main, ветка PR отдельным worktree (#476)',
    'Smoke обёрток статусов журнала',
    'Тесты проверки рендера транскрипта (#131)',
    # Значение этого элемента — реальный факт YAML, не опечатка: имя шага в
    # repo-ci.yml несёт " #119)" ПОСЛЕ пробела, а YAML-парсер (тот же
    # yaml.safe_load, что использует эта гвардия) обрезает его как inline-
    # комментарий (`#` после пробела в незакавыченном plain-скаляре) — GitHub
    # Actions использует тот же парсер, поэтому реальное имя шага в UI прогона
    # тоже усечено. Не чинится этим PR (вне области #749), зафиксировано как
    # факт, а не подогнано под ожидание.
    'Smoke bash-клиентов на заглушках (класс Б1',
    'Smoke цепочки LLM-провайдеров (#727)',
    'Гвардия concurrency — не сериализовать разные джобы одной статической группой',
    'Гвардия «workflow из docs существует»',
    'Тесты гейта квоты rate_guard (#454)',
    'Тесты гвардии полноты карты документации',
    'Гвардия полноты карты документации — живой снимок (#670)',
})


def guard_step_names(repo_ci: Path = REPO_CI) -> set[str]:
    doc = yaml.safe_load(repo_ci.read_text(encoding="utf-8")) or {}
    job = (doc.get("jobs") or {}).get("test") or {}
    names: set[str] = set()
    for step in job.get("steps") or []:
        name = step.get("name")
        if name and _GUARD_NAME_RE.match(name):
            names.add(name)
    return names


def check_no_undeclared_step(
    repo_ci: Path = REPO_CI, allowlist: frozenset[str] = ALLOWLIST
) -> list[str]:
    found = guard_step_names(repo_ci)
    added = sorted(found - allowlist)
    removed = sorted(allowlist - found)
    problems: list[str] = []
    for name in added:
        problems.append(
            f"новый рукописный шаг гвардии в repo-ci.yml: {name!r} — перенеси в "
            "scripts/ci/guards/<имя>.sh (#749), не дописывай шаг в общий файл; "
            "если это осознанная инфраструктурная правка не про гвардию — "
            "добавь имя в ALLOWLIST scripts/lib/ci_guard_registration_guard.py"
        )
    for name in removed:
        problems.append(
            f"ALLOWLIST называет шаг {name!r}, которого больше нет в repo-ci.yml "
            "job `test` — обнови ALLOWLIST в "
            "scripts/lib/ci_guard_registration_guard.py (мигрирован в каталог "
            "или переименован?)"
        )
    return problems


def main() -> int:
    problems = check_no_undeclared_step()
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    print(
        f"ci-guard-registration: {len(ALLOWLIST)} рукописных шагов гвардий учтены "
        "в ALLOWLIST, новых незарегистрированных нет"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
