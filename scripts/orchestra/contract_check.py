#!/usr/bin/env python3
"""Контракт PR с пулом задач. Запускается workflow'ом orchestra на каждый PR.

Правила (нарушение = проверка красная, такой PR не слить):
  1. PR с меткой `orchestra:skip` — явный обход контракта (мелочи вне пула).
  2. В теле PR есть ссылка на задачу `#N`.
  3. Задача #N открыта и помечена меткой `task`.
  4. Задача назначена ровно одному исполнителю.
  5. У этой задачи нет ДРУГОГО открытого PR — второй PR на ту же задачу закрывается
     оркестратором, брать задачу надо через назначение, а не через гонку веток.

Среда: runner с `gh`, GH_TOKEN с правами issues/pull-requests.
"""

import argparse
import json
import os
import subprocess
import sys

SKIP_LABEL = "orchestra:skip"
TASK_LABEL = "task"


def gh(*args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fail(messages: list[str]) -> None:
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
    issue_numbers = []
    for line in refs:
        for token in line.split("#")[1:]:
            head = token.strip().split()[0] if token.strip() else ""
            digits = "".join(ch for ch in head if ch.isdigit())
            if digits and line.lstrip().lower().startswith(("closes", "fixes", "resolves", "#")):
                issue_numbers.append(int(digits))
    if not issue_numbers:
        problems.append("В теле PR нет ссылки на задачу (#N). Один PR — одна задача из пула.")

    if issue_numbers:
        issue_number = issue_numbers[0]
        issue = gh(f"repos/{repo}/issues/{issue_number}")
        if "pull_request" in issue:
            problems.append(f"#{issue_number} — это PR, а не задача из пула.")
        else:
            if issue["state"] != "open":
                problems.append(f"Задача #{issue_number} закрыта — возьми открытую или заведи новую.")
            labels_issue = {label["name"] for label in issue["labels"]}
            if TASK_LABEL not in labels_issue:
                problems.append(f"На задаче #{issue_number} нет метки `{TASK_LABEL}`.")
            assignees = [a["login"] for a in issue["assignees"]]
            if len(assignees) != 1:
                problems.append(
                    f"Задачу #{issue_number} должно брать ровно одно назначение "
                    f"(сейчас: {', '.join(assignees) or 'никто'}). Назначь себя через API, не через силу."
                )
            # Чужие открытые PR на ту же задачу — гонка веток; она разрешается здесь.
            others = []
            pulls = gh(f"repos/{repo}/pulls?state=open&per_page=100")
            for other in pulls:
                if other["number"] == args.pr:
                    continue
                other_body = other["body"] or ""
                if f"#{issue_number}" in other_body:
                    others.append(other["number"])
            if others:
                problems.append(
                    f"На задачу #{issue_number} уже есть открытый PR #{others[0]}. "
                    "Второй PR на ту же задачу не проходит контракт."
                )

    if problems:
        fail(problems)
    print("contract: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
