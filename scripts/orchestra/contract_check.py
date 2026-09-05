#!/usr/bin/env python3
"""Контракт PR с пулом задач. Запускается workflow'ом orchestra на каждый PR.

Правила (нарушение = проверка красная, такой PR не слить):
  1. PR с меткой `orchestra:skip` — явный обход контракта (мелочи вне пула).
  2. В теле PR есть ссылка на задачу `#N`.
  3. Задача #N существует как issue (не PR), открыта, помечена меткой `task`
     и не помечена `blocked` — см. task_eligibility_problems, одно место
     правды. Задача НЕПРИГОДНА хотя бы по одной из этих причин — контракт
     не выполняет над ней ни одного изменяющего вызова (не назначает
     исполнителя, не сверяет дубликаты PR): нарушение только докладывается.
  4. Задача назначена ровно одному исполнителю (проверяется/чинится только
     для пригодной задачи, см. правило 3).
  5. У этой задачи нет ДРУГОГО открытого PR — второй PR на ту же задачу закрывается
     оркестратором, брать задачу надо через назначение, а не через гонку веток
     (тоже только для пригодной задачи).

Среда: runner с `gh`, GH_TOKEN с правами issues/pull-requests.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

# Номер задачи из текста PR/issue — одно место правды (#187): границы числа
# с обеих сторон, не подстрока (`#18` не должен матчить `#180`/`#5180`).
_TR_SPEC = importlib.util.spec_from_file_location(
    "task_ref", Path(__file__).resolve().parents[1] / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_TR_SPEC)
_TR_SPEC.loader.exec_module(task_ref)

SKIP_LABEL = "orchestra:skip"
TASK_LABEL = "task"
# Эскалация playbook (scheduler.py: BLOCKED_LABEL) — «ждёт владельца», не
# кандидат на авто-назначение. Строка та же, что и в scheduler.py, но
# отдельная константа: тащить сюда модуль scheduler.py ради одного имени —
# лишняя связка (contract_check и scheduler уже читают общие lib-модули, но
# не друг друга).
BLOCKED_LABEL = "blocked"


def run_gh(*args: str) -> None:
    result = subprocess.run(["gh", *args], capture_output=True, text=True,
                            env={**os.environ, "NO_COLOR": "1"})
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {result.stderr.strip()}")


def gh(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _all_open_pulls(repo: str) -> list[dict]:
    """Все открытые PR постранично, не только первая страница `per_page=100`
    (класс #294/#303/#308: сырой `pulls?state=open&per_page=100` без обхода
    молча теряет хвост — на репозитории за сотню открытых PR второй PR на ту
    же задачу за первой сотней не находился бы, и контракт «одна задача — один
    PR» тихо переставал бы работать именно там, где список самый длинный).
    Листание — та же форма, что review_labels.list_pr_files/list_timeline:
    короткая страница (`len(chunk) < 100`) значит «дальше страниц нет»."""
    page = 1
    pulls: list[dict] = []
    while True:
        chunk = gh(f"repos/{repo}/pulls?state=open&per_page=100&page={page}")
        if not isinstance(chunk, list) or not chunk:
            break
        pulls.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return pulls


def task_eligibility_problems(issue: dict, issue_number: int) -> list[str]:
    """Единое место правды «можно ли вообще действовать над этой задачей» —
    существует ли она как issue (не PR), открыта, несёт метку `task`, не
    `blocked`. Пока этот список непуст, main() не имеет права выполнить НИ
    ОДНОГО изменяющего вызова над issue_number (назначение, метки, комментарии
    от её имени) — только копить причины отказа.

    Живой случай, который эта функция закрывает (лог прогона 2026-09-06,
    контракт PR #359): «contract: авто-назначение mytab0r на #131» сразу
    следом за «Задача #131 закрыта — возьми открытую». Раньше проверка
    состояния и авто-назначение стояли рядом в одной ветке if/else, и ничего
    не мешало назначению выполниться уже ПОСЛЕ того, как о непригодности было
    известно — правило есть, действие ему не подчинялось. Здесь пригодность
    считается один раз, до всех действующих вызовов, и других мест, где эти
    условия проверяются заново, в контракте больше нет."""
    problems: list[str] = []
    if "pull_request" in issue:
        # Это PR, а не issue — остальные поля (state/labels исполнителя)
        # созданы для issue и не значат то же самое на PR; дальше нечего
        # проверять.
        problems.append(f"#{issue_number} — это PR, а не задача из пула.")
        return problems
    if issue["state"] != "open":
        problems.append(f"Задача #{issue_number} закрыта — возьми открытую или заведи новую.")
    labels_issue = {label["name"] for label in issue["labels"]}
    if TASK_LABEL not in labels_issue:
        problems.append(f"На задаче #{issue_number} нет метки `{TASK_LABEL}`.")
    if BLOCKED_LABEL in labels_issue:
        problems.append(
            f"Задача #{issue_number} помечена `{BLOCKED_LABEL}` — ждёт решения владельца, "
            "бери свободную из пула."
        )
    return problems


def fail(messages: list[str], repo: str, pr_number: int) -> None:
    # Провал громкий на самом PR: метка + комментарий, а не только строка в логах CI.
    try:
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/labels",
               "-f", "labels[]=contract:failed")
        body = "Контракт PR ↔ задача нарушен:" + "".join(f"\n- {m}" for m in messages)
        run_gh("api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/comments", "-f", f"body={body}")
    except RuntimeError as error:
        print(f"contract: не смог оставить комментарий на PR: {error}")
    for message in messages:
        print(f"::error::{message}")
    print(f"contract: FAIL ({len(messages)} нарушений)")
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]

    pull = gh(f"repos/{repo}/pulls/{args.pr}")

    labels = {label["name"] for label in pull["labels"]}
    if SKIP_LABEL in labels:
        print(f"contract: SKIP (метка {SKIP_LABEL})")
        return 0

    # Dependabot и другие боты-поставщики зависимостей — вне пула задач по природе:
    # их судят проверки (test/canary/review), а не контракт «PR ↔ задача».
    if pull["user"]["login"] in ("dependabot[bot]",):
        print("contract: SKIP (dependabot)")
        return 0

    problems: list[str] = []
    body = pull["body"] or ""
    refs = [line for line in body.splitlines() if "#" in line]
    # Закрытие задачи — действие исполнителя ПОСЛЕ пост-мерж проверки, с уликами:
    # мерж доказывает PR, а не готовность задачи (кейс #56/#57: Closes закрыл
    # задачу до зелёной канарейки). GitHub сам авто-закрывает issue по ключевым
    # словам при слиянии, поэтому единственная преграда — контракт на входе.
    close_lines = [
        line for line in refs
        if line.lstrip().lower().startswith(("closes", "fixes", "resolves"))
    ]
    if close_lines:
        problems.append(
            "Не пиши Closes/Fixes/Resolves: задачу закрывает исполнитель ПОСЛЕ "
            "пост-мерж проверки (деплой/канарейка/E2E), приложив улики. "
            "Ссылайся на задачу просто #N."
        )
    # Номер задачи признаётся только на ПЕРВОЙ непустой строке тела (без
    # учёта HTML-комментариев), начинающейся с `#` и сразу цифрой — не любое
    # упоминание issue в тексте PR и не любая строка с ведущим `#` (#251,
    # #312) — декларация, не упоминание. Одно место правды —
    # task_ref.declared_tasks (#195): то же правило применяется и к чужим
    # PR ниже, симметрично.
    issue_numbers = task_ref.declared_tasks(body)
    if not issue_numbers:
        problems.append("В теле PR нет ссылки на задачу (#N). Один PR — одна задача из пула.")

    if issue_numbers:
        issue_number = issue_numbers[0]
        issue = gh(f"repos/{repo}/issues/{issue_number}")
        # Пригодность считается ОДИН раз, ДО единого изменяющего вызова над
        # этой issue (см. task_eligibility_problems) — непригодна, дальше не
        # действуем вовсе, только докладываем причину.
        eligibility = task_eligibility_problems(issue, issue_number)
        if eligibility:
            problems.extend(eligibility)
        else:
            assignees = [a["login"] for a in issue["assignees"]]
            author = pull["user"]["login"]
            if not assignees:
                # Забыли назначиться — назначаем автора PR автоматически: первый PR
                # по свободной задаче её занимает. Не на памяти, а в контракте.
                gh("-X", "POST", f"repos/{repo}/issues/{issue_number}/assignees",
                   "-f", f"assignees[]={author}")
                print(f"contract: авто-назначение {author} на #{issue_number}")
                assignees = [author]
            if assignees != [author]:
                problems.append(
                    f"Задача #{issue_number} занята не тобой "
                    f"(назначено: {', '.join(assignees)}). Бери свободную из пула."
                )
            # Чужие открытые PR на ту же задачу — гонка веток; она разрешается здесь.
            # Симметрично своему PR: конфликт только если чужой PR ОБЪЯВЛЯЕТ эту
            # задачу (ПЕРВАЯ непустая строка тела, без учёта HTML-комментариев,
            # начинающаяся с `#` и сразу цифрой, #251, #312), а не просто
            # упоминает её номер в прозе описания (#195 — второй экземпляр
            # асимметрии #187: своя декларация уже была узкой, чужая гонялась
            # по всему тексту).
            others = []
            pulls = _all_open_pulls(repo)
            for other in pulls:
                if other["number"] == args.pr:
                    continue
                other_body = other["body"] or ""
                if task_ref.declares_task(other_body, issue_number):
                    others.append(other["number"])
            if others:
                problems.append(
                    f"На задачу #{issue_number} уже есть открытый PR #{others[0]}. "
                    "Второй PR на ту же задачу не проходит контракт."
                )

    if problems:
        fail(problems, repo, args.pr)
    # Прошёл — снимаем метку провала, если была.
    try:
        run_gh("api", "-X", "DELETE", f"repos/{repo}/issues/{args.pr}/labels/contract:failed")
    except Exception:
        pass
    print("contract: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
