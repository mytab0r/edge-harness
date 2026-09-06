#!/usr/bin/env python3
"""Граф блокировок пула задач — единственное место правды (задача #361).

Носитель факта «задача A блокирует задачу B» — нативный GitHub GraphQL
`Issue.blockedBy`/`Issue.blocking`/`Issue.issueDependenciesSummary` и
мутации `addBlockedBy`/`removeBlockedBy`, НЕ прозопарсинг тела issue
(старая конвенция `docs/agents/PROTOCOL.md` «строка «Зависимости» в теле
блокирующей задачи» — тот же запрещённый класс, что уже закрыт для
контракта «PR → задача», #251/#259: разбор свободного текста вместо
структурного факта).

Живая проверка (не только документация), сделанная при внедрении этого
модуля 2026-09-06:

  - Introspection `__type(name: "Issue")` подтвердил поля `blockedBy`,
    `blocking`, `issueDependenciesSummary` в схеме GraphQL этого репозитория
    и мутации `addBlockedBy`/`removeBlockedBy` в мутационном типе.
  - Round-trip на реальной паре issues (#350 ↔ #320, оба закрыты, выбраны
    как безопасные для проверки): `addBlockedBy` поставил связь, чтение
    `blockedBy.nodes`/`blocking.nodes` её увидело, `removeBlockedBy` снял,
    повторное чтение подтвердило 0 — мутации реально исполняются на этом
    плане/токене, не только объявлены в схеме.
  - REST `repos/{owner}/{repo}/issues/{n}` при этом отдаёт `sub_issues_summary`
    (декомпозиция, отдельный механизм), но НЕ отдаёт `blockedBy`/`blocking`
    ни в каком виде ни на одном полe ответа — граф зависимостей доступен
    ТОЛЬКО через GraphQL. Отсюда — `gh api graphql`, не `gh api` REST.

Sub-issues (декомпозиция, эпик → подзадачи, `docs/agents/PROTOCOL.md`)
этот модуль не трогает и не заменяет: разные семантики, разные поля схемы
(`subIssues`/`subIssuesSummary` vs `blockedBy`/`blocking`), смешивать
запрещено (design.md, развилка а).

CLI:
    python scripts/lib/task_deps.py pool <owner/repo> [label]
    python scripts/lib/task_deps.py block <owner/repo> <заблокированная#> <блокирующая#>
    python scripts/lib/task_deps.py unblock <owner/repo> <заблокированная#> <блокирующая#>

`pool` печатает JSON-список открытых задач с меткой `label` (по умолчанию
`task`): number, title, labels, assignees, blocking_open (сколько ОТКРЫТЫХ
задач блокирует), blocked_by_open (номера ОТКРЫТЫХ задач, блокирующих эту).
Форма `labels`/`assignees` — списки словарей с ключом `name`/`login`
соответственно, совместимо с тем, что уже отдаёт `gh issue list --json`
(используется тем же кодом `scripts/lib/free_task.py`, что и REST-форма).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

_LABEL_TOKEN_RE = re.compile(r"^[a-z][a-z0-9:_-]*$")


class TaskDepsError(RuntimeError):
    """Сбой вызова gh (сеть/права/GraphQL-ошибка) — fail loud, не silent-wrong."""


def _default_gh(*args: str) -> dict | list | None:
    """`gh api <args>` — тот же контракт, что `pulse_guard.gh`/`scheduler.gh`
    (аргументы БЕЗ `api`, префикс добавляется здесь), не своя отдельная
    форма: позволяет вызывающей стороне (scheduler.py) подставить СВОЙ
    вызов через `gh_call=`, единый с REST-вызовами того же модуля, и
    мокать его в тестах тем же приёмом (`patch_gh`), не вторым мок-путём
    на `task_deps.subprocess`."""
    result = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise TaskDepsError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def gh_graphql(
    query: str, variables: dict[str, object] | None = None, gh_call=_default_gh,
) -> dict:
    """Выполняет GraphQL-запрос через `gh_call("graphql", ...)` (по умолчанию
    `_default_gh`, реальный `gh api graphql`). Переменные — только скаляры
    (числа передаются `-F`, остальное `-f`, как того требует gh)."""
    args = ["graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        flag = "-F" if isinstance(value, (int, float)) and not isinstance(value, bool) else "-f"
        args += [flag, f"{key}={value}"]
    payload = gh_call(*args) or {}
    if not isinstance(payload, dict):
        raise TaskDepsError(f"gh api graphql: неожиданный ответ (не объект): {payload!r}")
    if payload.get("errors"):
        raise TaskDepsError(f"gh api graphql: {payload['errors']}")
    return payload.get("data") or {}


def _split_repo(repo: str) -> tuple[str, str]:
    if "/" not in repo:
        raise TaskDepsError(f"repo обязан быть в форме owner/name: {repo!r}")
    owner, name = repo.split("/", 1)
    return owner, name


# ── Чтение пула ──────────────────────────────────────────────────────────────

_POOL_QUERY_TMPL = """
query($owner: String!, $repo: String!, $after: String) {{
  repository(owner: $owner, name: $repo) {{
    issues(states: OPEN, labels: ["{label}"], first: 100, after: $after,
           orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        number
        title
        labels(first: 20) {{ nodes {{ name }} }}
        assignees(first: 5) {{ nodes {{ login }} }}
        blockedBy(first: 20) {{ nodes {{ number state }} }}
        blocking(first: 20) {{ nodes {{ number state }} }}
      }}
    }}
  }}
}}
"""


def _is_open(node: dict) -> bool:
    return (node.get("state") or "").upper() == "OPEN"


def fetch_pool(repo: str, label: str = "task", gh_call=_default_gh) -> list[dict]:
    """Открытый пул с меткой `label`, одним пагинированным GraphQL-проходом.
    Единственный источник, отдающий и список задач, и граф зависимостей —
    REST для этого недостаточен (см. docstring модуля). `gh_call` — тот же
    параметр, что у `gh_graphql`: подмена вызывающей стороной (scheduler.py
    передаёт свой `gh`, единый с REST-вызовами, тестируемый тем же `patch_gh`)."""
    if not _LABEL_TOKEN_RE.match(label):
        raise TaskDepsError(f"метка не похожа на литерал GitHub-метки: {label!r}")
    owner, name = _split_repo(repo)
    query = _POOL_QUERY_TMPL.format(label=label)
    issues: list[dict] = []
    after: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "repo": name}
        if after is not None:
            variables["after"] = after
        data = gh_graphql(query, variables, gh_call=gh_call)
        connection = data["repository"]["issues"]
        for node in connection["nodes"]:
            issues.append({
                "number": node["number"],
                "title": node["title"],
                "labels": node["labels"]["nodes"],
                "assignees": node["assignees"]["nodes"],
                "blocking_open": sum(1 for b in node["blocking"]["nodes"] if _is_open(b)),
                "blocked_by_open": [b["number"] for b in node["blockedBy"]["nodes"] if _is_open(b)],
            })
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return issues


def graph_is_empty(issues: list[dict]) -> bool:
    """Ни одна задача пула не блокирует и не блокирована ничем открытым —
    приоритет по графу выродится в тайбрейк по номеру для всех кандидатов
    (см. free_task.py::issue_priority_key). Видимый сигнал, не молчание."""
    return all(
        not issue.get("blocking_open") and not issue.get("blocked_by_open")
        for issue in issues
    )


# ── Запись связи (ручной шаг, тот же паттерн, что sub-issues в PROTOCOL.md) ──

_ISSUE_ID_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) { issue(number: $number) { id } }
}
"""

_ADD_BLOCKED_BY = """
mutation($issueId: ID!, $blockingIssueId: ID!) {
  addBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId}) {
    issue { number }
  }
}
"""

_REMOVE_BLOCKED_BY = """
mutation($issueId: ID!, $blockingIssueId: ID!) {
  removeBlockedBy(input: {issueId: $issueId, blockingIssueId: $blockingIssueId}) {
    issue { number }
  }
}
"""


def issue_node_id(repo: str, number: int, gh_call=_default_gh) -> str:
    owner, name = _split_repo(repo)
    data = gh_graphql(
        _ISSUE_ID_QUERY, {"owner": owner, "repo": name, "number": number}, gh_call=gh_call,
    )
    issue = data["repository"]["issue"]
    if issue is None:
        raise TaskDepsError(f"issue #{number} не найдена в {repo}")
    return issue["id"]


def add_dependency(repo: str, blocked: int, blocking: int, gh_call=_default_gh) -> None:
    """`blocked` заблокирована `blocking`: `blocking` должна закрыться раньше."""
    issue_id = issue_node_id(repo, blocked, gh_call=gh_call)
    blocking_id = issue_node_id(repo, blocking, gh_call=gh_call)
    gh_graphql(
        _ADD_BLOCKED_BY, {"issueId": issue_id, "blockingIssueId": blocking_id}, gh_call=gh_call,
    )


def remove_dependency(repo: str, blocked: int, blocking: int, gh_call=_default_gh) -> None:
    issue_id = issue_node_id(repo, blocked, gh_call=gh_call)
    blocking_id = issue_node_id(repo, blocking, gh_call=gh_call)
    gh_graphql(
        _REMOVE_BLOCKED_BY, {"issueId": issue_id, "blockingIssueId": blocking_id}, gh_call=gh_call,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3) and argv[0] == "pool":
        repo = argv[1]
        label = argv[2] if len(argv) == 3 else "task"
        print(json.dumps(fetch_pool(repo, label)))
        return 0
    if len(argv) == 4 and argv[0] == "block":
        add_dependency(argv[1], int(argv[2]), int(argv[3]))
        print(f"#{argv[2]} заблокирована #{argv[3]}")
        return 0
    if len(argv) == 4 and argv[0] == "unblock":
        remove_dependency(argv[1], int(argv[2]), int(argv[3]))
        print(f"#{argv[2]} больше не заблокирована #{argv[3]}")
        return 0
    print(
        "использование: task_deps.py pool <owner/repo> [label] "
        "| block <owner/repo> <заблокированная#> <блокирующая#> "
        "| unblock <owner/repo> <заблокированная#> <блокирующая#>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except TaskDepsError as error:
        print(f"task_deps.py: {error}", file=sys.stderr)
        sys.exit(2)
