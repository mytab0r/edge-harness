#!/usr/bin/env python3
"""Гвардия «PR не заводится на эпик» (#376): ветка на эпик не сходится по
определению — критерий приёмки эпика распределён по нескольким стройкам,
недостижим в одном PR, поэтому каждый раунд ревью законно находит новое (класс
воспроизведён живьём: PR #162, 44 раунда ревью на ветку `agent/77-...`,
привязанную прямо к эпику #77, вместо узкой задачи на одну стройку).

Признак эпика — `is_epic_issue` из `scripts/orchestra/scheduler.py` (уже
рабочая логика приёмки #335, PR #342): заголовок с префиксом «ЭПИК» ИЛИ
незакрытые нативные sub-issues (`sub_issues_summary`). Модуль импортируется тем
же способом, что использует `scripts/orchestra/test_scheduler.py` — второй
копии признака здесь нет, одно место правды.

CLI: `epic_guard.py <номер>` — exit 0, если номер НЕ эпик (можно заводить
ветку); exit 1 и сообщение с газом (что делать вместо этого + список открытых
sub-issues, если удалось их получить), если это эпик; exit 2 при поломке
инструмента (сеть/gh) — тот же класс ошибок, которым `task-branch` уже требует
рабочую сеть (git fetch/ls-remote), офлайн-режима у входа в задачу нет."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_EPIC = 1
EXIT_ERROR = 2

_ORCHESTRA_DIR = Path(__file__).resolve().parents[1] / "orchestra"
if str(_ORCHESTRA_DIR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRA_DIR))  # scheduler.py делает `from pulse_guard import …`

_spec = importlib.util.spec_from_file_location("scheduler", _ORCHESTRA_DIR / "scheduler.py")
scheduler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scheduler)


def current_repo() -> str | None:
    """GITHUB_REPOSITORY в CI уже задан; локально — тем же способом, что
    scripts/git/task-branch применяет для аренды задачи (`gh repo view`)."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    repo = result.stdout.strip()
    return repo or None


def open_sub_issues(repo: str, number: int) -> list[dict]:
    """Открытые нативные sub-issues эпика — только для текста отказа (газ:
    что взять вместо эпика). Лучше-эффорт: REST не отдаёт список (только
    сводку sub_issues_summary — is_epic_issue выше), список берётся отдельным
    GraphQL-запросом; сбой этого запроса не мешает самому отказу, просто
    список в сообщении будет пуст."""
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){"
        "repository(owner:$owner,name:$name){issue(number:$number){"
        "subIssues(first:50){nodes{number title state}}}}}"
    )
    result = subprocess.run(
        ["gh", "api", "graphql",
         "-f", f"query={query}",
         "-f", f"owner={owner}",
         "-f", f"name={name}",
         "-F", f"number={number}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    try:
        nodes = json.loads(result.stdout)["data"]["repository"]["issue"]["subIssues"]["nodes"]
    except (KeyError, TypeError, ValueError):
        return []
    return [node for node in nodes if node.get("state") == "OPEN"]


def check(repo: str, number: int) -> tuple[bool, dict]:
    issue = scheduler.gh(f"repos/{repo}/issues/{number}")
    return scheduler.is_epic_issue(issue), issue


def refusal_text(number: int, issue: dict, subs: list[dict]) -> str:
    lines = [
        f"ОШИБКА: #{number} — эпик («{issue.get('title', '')}»), ветку на эпик заводить нельзя.",
        "Критерий готовности эпика распределён по нескольким стройкам — PR,",
        "скоупленный на номер эпика, не сходится по определению: каждый раунд",
        "ревью законно находит новое (класс воспроизведён: PR #162, 44 раунда",
        "ревью на ветку, привязанную к эпику #77, закрыт вместо трёх стройок).",
        f"Что делать: заведи узкую задачу на конкретную стройку эпика #{number}",
        "и работай по ней — task-branch примет номер этой узкой задачи.",
    ]
    if subs:
        lines.append(f"Открытые под-задачи эпика #{number}:")
        for sub in subs:
            lines.append(f"  #{sub['number']}: {sub.get('title', '')}")
    else:
        lines.append(
            f"Нативных открытых под-задач у #{number} не нашлось (или сеть их не отдала) — "
            "начни с design.md эпика и заведи узкую задачу на стройку сам."
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        print("использование: epic_guard.py <номер задачи>", file=sys.stderr)
        return EXIT_ERROR
    number = int(argv[1])
    repo = current_repo()
    if not repo:
        print("::error::epic_guard: репозиторий не определён (gh repo view не сработал) — "
              "почини сеть/gh, вход в задачу без этой проверки не даю", file=sys.stderr)
        return EXIT_ERROR
    try:
        is_epic, issue = check(repo, number)
    except RuntimeError as error:
        print(f"::error::epic_guard: не удалось проверить #{number} ({error}) — "
              "почини сеть/gh, вход в задачу без этой проверки не даю", file=sys.stderr)
        return EXIT_ERROR
    if not is_epic:
        return EXIT_OK
    subs = open_sub_issues(repo, number)
    print(refusal_text(number, issue, subs), file=sys.stderr)
    return EXIT_EPIC


if __name__ == "__main__":
    sys.exit(main(sys.argv))
