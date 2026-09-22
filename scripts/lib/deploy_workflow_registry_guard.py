#!/usr/bin/env python3
"""Гвардия класса «деплойный workflow не под наблюдением» (issue #1421,
обобщение #1041/#1419).

Инвариант 17 (`repo_invariants.check_frontend_deploy_stale`) сравнивает
последний ЗЕЛЁНЫЙ прогон каждого деплойного workflow с текущим main и
называет факт «прод молча устарел». Смотрит он на список
`FRONTEND_DEPLOY_WORKFLOWS`, и список этот рукописный.

Чем оплачен этот файл. #1041 написал инвариант под ОДИН workflow
(`deploy-dsh-edge.yml`). Второй, `deploy-worker.yml`, был красным на каждом
прогоне девять суток (2026-09-13 … 2026-09-21), прод-морда не обновлялась, и
заметил это человек, открывший вкладку Actions руками (#1419). Механизм,
умеющий ровно это состояние, просто не знал про второй файл. #1419 дописал
его в список — то есть починил СЛУЧАЙ.

Класс же в другом: список ведётся руками, а множество деплойных workflow
живёт своей жизнью. Третий появится — и будет невидим ровно тем же способом,
каким был невидим второй, только узнают об этом снова через девять суток и
снова руками. Гвардия убирает саму возможность: признак «этот workflow
деплоит» читается из файлов workflow, а не из памяти автора.

Устройство:
  1. `deploy_workflows` — скан `.github/workflows/*.yml`. Деплойным считается
     workflow, у которого ХОТЯ БЫ ОДИН шаг реально деплоит: `run` содержит
     `wrangler deploy` / `wrangler versions upload` / `wrangler pages deploy`,
     либо `uses: cloudflare/wrangler-action`. Комментарии внутри `run`
     вырезаются ДО сопоставления — иначе деплойным становится любой файл,
     где про `wrangler deploy` лишь НАПИСАНО. Честно: на дереве 2026-09-22
     это ничего не меняет (исполнено — множество деплойных совпадает с
     вырезанием и без него), защита смотрит вперёд.
  2. `registered` — объединение `FRONTEND_DEPLOY_WORKFLOWS` (под наблюдением
     инварианта 17) и `UNWATCHED_DEPLOY_WORKFLOWS` (осознанные исключения с
     НАЗВАННОЙ причиной — тот же газ, что у excluded в
     `merge_reactions_registry_guard`).
  3. `check_registry_completeness` — сверка в обе стороны: незарегистрированный
     деплойный workflow красит гвардию; запись, ссылающаяся на файл, которого
     нет, красит её же (мёртвая ссылка — workflow переименовали, список не
     поправили); запись, стоящая и там и там, красит третьей (противоречие:
     «наблюдаем» и «сознательно не наблюдаем» одновременно).

Место правды одно: оба списка объявлены в `repo_invariants.py` рядом с самим
инвариантом, здесь они только читаются.

Запуск:
  python scripts/lib/deploy_workflow_registry_guard.py
  python -m pytest scripts/lib/test_deploy_workflow_registry_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

# --- rate_guard: исчерпанный бюджет API — предупреждение, не красный required-гейт (#1004) ---
# Завышение детектора, принятое сознательно: замер 2026-09-22 показал, что под
# заглушкой `gh` с ответом 403 эта гвардия проходит зелёной (EXIT=0) — путь до
# API на её прогоне не исполняется, хотя загружаемый `repo_invariants` вызовы
# наружу содержит. Обёртка здесь стоит не потому, что отказ наблюдался, а
# потому, что её цена нулевая: `run_guard_main` ловит ТОЛЬКО отказ формы
# «бюджет исчерпан» и пробрасывает всё остальное. Обратная ошибка — пропустить
# настоящего потребителя — стоит красной обязательной проверки на чужой
# причине.
_rate_guard_spec = importlib.util.spec_from_file_location(
    "rate_guard", Path(__file__).resolve().parent / "rate_guard.py")
_rate_guard = importlib.util.module_from_spec(_rate_guard_spec)
_rate_guard_spec.loader.exec_module(_rate_guard)
# --- конец rate_guard ---

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# `repo_invariants` тянет за собой соседей по scripts/orchestra (scheduler,
# pulse_guard) обычным `import`, а не по файлу — под pytest их находит сам
# rootdir, но при прямом запуске этого файла из scripts/lib каталог соседей в
# sys.path не попадает, и загрузка падает на `No module named 'pulse_guard'`.
# Путь добавляется явно, до exec_module.
_ORCHESTRA_DIR = REPO_ROOT / "scripts" / "orchestra"
if str(_ORCHESTRA_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRA_DIR))

_RI_SPEC = importlib.util.spec_from_file_location(
    "repo_invariants", _ORCHESTRA_DIR / "repo_invariants.py")
repo_invariants = importlib.util.module_from_spec(_RI_SPEC)
sys.modules["repo_invariants"] = repo_invariants
_RI_SPEC.loader.exec_module(repo_invariants)  # type: ignore[union-attr]

# Признак «шаг реально деплоит». Формы взяты из живых файлов дерева, не
# придуманы: `npx wrangler deploy` (deploy-dsh-edge.yml, deploy-worker.yml).
# `versions upload` и `pages deploy` добавлены потому, что это те же ворота в
# прод у Cloudflare — появись такой шаг, он обязан быть под наблюдением или
# осознанно исключён, а не проскочить мимо гвардии молча.
_DEPLOY_RUN_RE = re.compile(
    r"wrangler(?:@[^\s]+)?\s+(?:deploy\b|versions\s+upload\b|pages\s+deploy\b)"
)
_DEPLOY_USES_RE = re.compile(r"^cloudflare/wrangler-action(?:@|$)")


def _strip_shell_comments(script: str) -> str:
    """Тело `run:` без строк-комментариев.

    Честно о цене: на дереве 2026-09-22 вырезание не меняет НИЧЕГО, и это
    исполнено, а не предположено — девять комментариев со словами `wrangler
    deploy` лежат внутри тех же двух файлов, которые деплоят и так, а вне их
    `wrangler deploy` не упоминается нигде. То есть это защита вперёд, а не
    починка живого случая; сказано прямо, чтобы оборона не читалась как
    находка.

    Защита нужна потому, что рассказ о деплое здесь — обычное дело:
    комментарий «раньше тут стоял `wrangler deploy`» в чужом workflow
    объявил бы его деплойным, и список исключений начал бы наполняться
    записями «это не деплой, это комментарий» — гвардия чинила бы
    собственный дефект чужими руками.

    Режется только строка, начинающаяся с `#` после отступа. Хвостовой `# …`
    в конце команды НЕ трогаем: отличить комментарий от `#` внутри кавычек
    или подстановки нечем, а ошибка здесь дороже — вырезав лишнее, гвардия
    пропустит настоящий деплой."""
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _steps(doc: dict) -> list[dict]:
    """Все шаги всех job'ов документа. Незнакомую форму не глотаем молча —
    пропускаем только то, что заведомо не шаг (не словарь)."""
    steps: list[dict] = []
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in (job.get("steps") or []):
            if isinstance(step, dict):
                steps.append(step)
    return steps


def workflow_deploys(path: Path) -> bool:
    """Деплоит ли этот workflow — по шагам файла, не по его имени.

    Имя — плохой признак: `dsh-edge-pr-smoke.yml` про деплой не говорит
    ничего, а `deploy-worker.yml` мог бы называться как угодно. Признак
    машинный: шаг, запускающий wrangler в прод-режиме."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        return False
    for step in _steps(doc):
        uses = step.get("uses")
        if isinstance(uses, str) and _DEPLOY_USES_RE.match(uses.strip()):
            return True
        run = step.get("run")
        if isinstance(run, str) and _DEPLOY_RUN_RE.search(_strip_shell_comments(run)):
            return True
    return False


def deploy_workflows(workflows_dir: Path = WORKFLOWS_DIR) -> set[str]:
    """Имена файлов (`deploy-worker.yml`, не путь) всех деплойных workflow."""
    return {path.name for path in sorted(workflows_dir.glob("*.y*ml"))
            if workflow_deploys(path)}


def registered() -> tuple[set[str], dict[str, str]]:
    """(под наблюдением инварианта 17, осознанно исключённые → причина).
    Раздельно, чтобы отчёт называл, КАКИМ путём workflow учтён."""
    watched = set(repo_invariants.FRONTEND_DEPLOY_WORKFLOWS)
    excluded = dict(getattr(repo_invariants, "UNWATCHED_DEPLOY_WORKFLOWS", {}))
    return watched, excluded


def check_registry_completeness(workflows_dir: Path = WORKFLOWS_DIR) -> list[str]:
    """Сверка списков инварианта 17 с реальностью дерева. Возвращает список
    нарушений (пустой — гвардия зелёная); печать решает вызывающий."""
    present = {path.name for path in workflows_dir.glob("*.y*ml")}
    deploying = deploy_workflows(workflows_dir)
    watched, excluded = registered()
    accounted = watched | set(excluded)

    problems: list[str] = []
    for name in sorted(deploying - accounted):
        problems.append(
            f"{name} деплоит (шаг с wrangler), но не значится ни в "
            "FRONTEND_DEPLOY_WORKFLOWS, ни в UNWATCHED_DEPLOY_WORKFLOWS "
            "(scripts/orchestra/repo_invariants.py). Инвариант 17 его не "
            "видит — красный деплой этого workflow простоит незамеченным "
            "ровно так же, как простоял deploy-worker.yml девять суток "
            "(#1419). Газ: добавь в FRONTEND_DEPLOY_WORKFLOWS, либо в "
            "UNWATCHED_DEPLOY_WORKFLOWS с причиной, почему устаревание "
            "этого деплоя наблюдать не нужно."
        )
    for name in sorted(accounted - present):
        problems.append(
            f"списки деплойных workflow ссылаются на {name!r}, которого нет "
            "в .github/workflows/ — мёртвая запись (workflow переименован "
            "или удалён, список не поправлен)"
        )
    for name in sorted(accounted - deploying):
        if name not in present:
            continue  # уже сказано выше, не дублируем
        problems.append(
            f"{name} числится деплойным, но ни одного шага с wrangler в нём "
            "нет — либо деплой оттуда убрали (тогда убери и запись), либо "
            "признак деплоя уехал и гвардия перестала его узнавать (тогда "
            "чини признак: ложно-зелёная гвардия хуже отсутствующей)"
        )
    for name in sorted(watched & set(excluded)):
        problems.append(
            f"{name} стоит и в FRONTEND_DEPLOY_WORKFLOWS, и в "
            "UNWATCHED_DEPLOY_WORKFLOWS — «наблюдаем» и «сознательно не "
            "наблюдаем» одновременно; какое из двух правда, читатель не "
            "различит"
        )
    for name, reason in sorted(excluded.items()):
        if not (reason or "").strip():
            problems.append(
                f"{name} исключён из наблюдения без причины — тормоз без "
                "газа (AGENTS.md): следующий читатель не узнает, можно ли "
                "исключение снимать"
            )
    return problems


def main() -> int:
    problems = check_registry_completeness(WORKFLOWS_DIR)
    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        return 1
    watched, excluded = registered()
    print(
        f"deploy-workflow-registry: {len(watched)} деплойных workflow под "
        f"инвариантом 17, {len(excluded)} осознанно исключены, "
        "незарегистрированных деплоев нет"
    )
    return 0


if __name__ == "__main__":
    sys.exit(_rate_guard.run_guard_main(
        main, guard="deploy-workflow-registry-guard"))
