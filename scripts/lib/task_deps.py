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

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

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
        ["gh", "api", *args], capture_output=True, text=True, encoding="utf-8",
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
        {body_field}
        labels(first: 20) {{ nodes {{ name }} }}
        assignees(first: 5) {{ nodes {{ login }} }}
        blockedBy(first: 20) {{ totalCount nodes {{ number state }} }}
        blocking(first: 20) {{ totalCount nodes {{ number state }} }}
      }}
    }}
  }}
}}
"""


def _is_open(node: dict) -> bool:
    return (node.get("state") or "").upper() == "OPEN"


def fetch_pool(
    repo: str, label: str = "task", gh_call=_default_gh, include_body: bool = False,
) -> list[dict]:
    """Открытый пул с меткой `label`, одним пагинированным GraphQL-проходом.
    Единственный источник, отдающий и список задач, и граф зависимостей —
    REST для этого недостаточен (см. docstring модуля). `gh_call` — тот же
    параметр, что у `gh_graphql`: подмена вызывающей стороной (scheduler.py
    передаёт свой `gh`, единый с REST-вызовами, тестируемый тем же `patch_gh`).

    `include_body` (по умолчанию `False`, задача #371) — добавляет
    скалярное поле `body` в тот же запрос (без второго прохода): нужно
    только детектору рассинхрона `scripts/lib/declared_deps.py`, обычные
    потребители (`free_task.py`, `scheduler.py`) текст тела не используют —
    не тянуть лишний трафик там, где он не читается."""
    if not _LABEL_TOKEN_RE.match(label):
        raise TaskDepsError(f"метка не похожа на литерал GitHub-метки: {label!r}")
    owner, name = _split_repo(repo)
    query = _POOL_QUERY_TMPL.format(label=label, body_field="body" if include_body else "")
    issues: list[dict] = []
    after: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "repo": name}
        if after is not None:
            variables["after"] = after
        data = gh_graphql(query, variables, gh_call=gh_call)
        connection = data["repository"]["issues"]
        for node in connection["nodes"]:
            # Находка ревью PR #367: `blocking(first: 20)`/`blockedBy(first: 20)` —
            # усечённая страница вложенного коннекшена, не сам пул задач (у него
            # своя пагинация выше, hasNextPage/endCursor). Без totalCount тяжёлый
            # блокировщик (>20 открытых блокируемых — ровно кейс, ради которого
            # эта задача заводилась) молча застыл бы на 20, и приоритет —
            # единственный смысл этого модуля — тихо соврал бы. Считаем это
            # silent-wrong и падаем громко, а не дотягиваем страницу: 20
            # прямых блокировок/блокируемых на одну задачу — само по себе
            # аномалия организации работы, требующая разбора человеком, не
            # тихого дотягивания.
            blocking_total = node["blocking"]["totalCount"]
            blocking_nodes = node["blocking"]["nodes"]
            if blocking_total > len(blocking_nodes):
                raise TaskDepsError(
                    f"issue #{node['number']}: blocking усечён "
                    f"({len(blocking_nodes)} из {blocking_total}) — "
                    "первая страница коннекшена не покрывает все связи, "
                    "приоритет по blocking_open соврёт молча"
                )
            blocked_by_total = node["blockedBy"]["totalCount"]
            blocked_by_nodes = node["blockedBy"]["nodes"]
            if blocked_by_total > len(blocked_by_nodes):
                raise TaskDepsError(
                    f"issue #{node['number']}: blockedBy усечён "
                    f"({len(blocked_by_nodes)} из {blocked_by_total}) — "
                    "первая страница коннекшена не покрывает все связи"
                )
            issue = {
                "number": node["number"],
                "title": node["title"],
                "labels": node["labels"]["nodes"],
                "assignees": node["assignees"]["nodes"],
                "blocking_open": sum(1 for b in blocking_nodes if _is_open(b)),
                "blocked_by_open": [b["number"] for b in blocked_by_nodes if _is_open(b)],
            }
            if include_body:
                issue["body"] = node.get("body") or ""
            issues.append(issue)
        page_info = connection["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return issues


# ── Транзитивный вес графа (задача #224) ─────────────────────────────────────
#
# `blocking_open`, отданный `fetch_pool`, — только ПРЯМОЙ счётчик (сколько
# открытых задач issue блокирует НАПРЯМУЮ, одна GraphQL-страница `blocking`).
# Цепочку A→B→C он не видит: A получает вес 1, хотя закрытие A в конечном
# счёте открывает путь и к C. Функции ниже строят эту цепочку локально, без
# второго сетевого обхода — целиком из уже прочитанного `blocked_by_open`
# каждой issue (то же поле, что использует `free_task.py`), поэтому дешёвы
# (чистый BFS по уже загруженному пулу) и детерминированы на неизменных
# данных (критерий 2 задачи #224 — два прогона дают одинаковый результат).
#
# Живой пример, обнаруживший разницу (замер на 303 открытых задачах пула
# 2026-09-12): issue #665 («починить 500 на ingest-эндпоинте морды») сама не
# помечена `ci-failure`, но НАПРЯМУЮ блокирует три открытые задачи класса
# «CI: worker.yml падает» (#577, #675, #981) — `blocking_open(665) == 4`
# и `transitive_blocking_counts(...)[665] == 4` совпадают здесь, потому что
# цепочка у #665 глубиной 1. Разница проявляется на #716 и #770: у обоих
# `blocking_open == 1`, но оба блокируют задачу, которая сама блокирует ещё
# одну — `transitive_blocking_counts` даёт им 2, а не 1, и поднимает их выше
# соседей с тем же прямым счётчиком, но без цепочки дальше.


def _forward_edges(issues: list[dict]) -> dict[int, set[int]]:
    """Рёбра «блокирующий → блокируемый», построенные из `blocked_by_open`
    КАЖДОЙ issue пула (не из `blocking`/`totalCount` — тот же факт, только
    прочитанный с другого конца, второго сетевого запроса не требуется).
    Блокирующий, который сам не входит в `issues` (не несёт метку `task`,
    которой отфильтрован пул), участвует только как источник ребра — это
    честная граница: граф считается ВНУТРИ пула с меткой, которым вызван
    `fetch_pool`, не по всем issues репозитория."""
    forward: dict[int, set[int]] = {}
    for issue in issues:
        target = issue["number"]
        for blocker in issue.get("blocked_by_open") or []:
            forward.setdefault(blocker, set()).add(target)
    return forward


def transitive_blocked(issues: list[dict], number: int) -> set[int]:
    """Множество issue, которые ТРАНЗИТИВНО зависят от `number` (BFS по
    `_forward_edges`, не только прямые соседи). Используется и для веса
    приоритета (`transitive_blocking_counts`), и для объяснения «кого именно
    закрытие этой задачи двигает» (`free_task.py::priority_reason`)."""
    forward = _forward_edges(issues)
    seen: set[int] = set()
    stack = list(forward.get(number, ()))
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(forward.get(node, ()))
    return seen


def transitive_blocking_counts(issues: list[dict]) -> dict[int, int]:
    """Обобщение `blocking_open` (прямой счётчик из GraphQL) на всю цепочку:
    для каждой issue — число ТРАНЗИТИВНО блокируемых ею открытых задач этого
    же пула. Совпадает с `blocking_open`, когда цепочка глубиной 1 (типичный
    сегодняшний случай — граф пула ещё разрежен, #720); расходится, когда
    блокируемая сама кого-то блокирует (см. докстринг раздела выше).

    Результат — `max(вес по рёбрам, blocking_open)`, не только вес по рёбрам:
    `_forward_edges` строится из `blocked_by_open` УЖЕ ПРИСУТСТВУЮЩИХ в
    `issues` узлов — если блокируемая задача сама не входит в этот список
    (другая метка, чем фильтровал `fetch_pool`, либо — только в тестовых
    фикстурах — синтетический номер без собственного узла), ребро до неё
    невидимо для BFS, а прямое поле `blocking_open` (отдаёт сам GraphQL,
    независимо от того, есть ли у цели свой узел в ЭТОЙ выборке) всё равно
    знает о нём. Без `max` транзитивный вес мог бы оказаться МЕНЬШЕ прямого
    счётчика — тихий регресс относительно #361, а не обобщение поверх него."""
    via_edges = {
        issue["number"]: len(transitive_blocked(issues, issue["number"]))
        for issue in issues
    }
    return {
        issue["number"]: max(
            via_edges.get(issue["number"], 0), int(issue.get("blocking_open") or 0),
        )
        for issue in issues
    }


def blockers_of(issues: list[dict], targets: set[int]) -> set[int]:
    """Множество issue, которые ТРАНЗИТИВНО блокируют ХОТЯ БЫ ОДНУ задачу из
    `targets` (обратный обход тех же рёбер — не «кого блокирует N», а «кто
    блокирует что-то из targets», включая многошаговую цепочку). Не включает
    сами `targets`.

    Назначение (задача #224, приоритет `free_task.py`): `targets` —
    множество задач, чинящих СЕЙЧАС красный механизм конвейера (метки
    `ci-failure`/`self-audit`, `free_task.BROKEN_LABELS`). Их прямые и
    косвенные блокировщики — тоже часть простоя: закрытие блокировщика
    приближает закрытие сломанного механизма, хотя сам блокировщик такой
    меткой не несёт. Живой пример (замер 2026-09-12): #665 и #610 не помечены
    `ci-failure`, но #665 напрямую блокирует #577/#675/#981 (все —
    `ci-failure`), а #610 блокирует #629 (`ci-failure`) — оба входят в
    результат при `targets` = множестве открытых `ci-failure`/`self-audit`."""
    reverse: dict[int, set[int]] = {}
    for issue in issues:
        node = issue["number"]
        for blocker in issue.get("blocked_by_open") or []:
            reverse.setdefault(node, set()).add(blocker)
    seen: set[int] = set()
    stack = list(targets)
    while stack:
        node = stack.pop()
        for blocker in reverse.get(node, ()):
            if blocker not in seen:
                seen.add(blocker)
                stack.append(blocker)
    return seen


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


# ── Перенос объявленной зависимости в граф — одно место на все пути ──────────


def wire_dependencies(
    repo: str, blocked: int, blocking_numbers: list[int], open_numbers: set[int],
    gh_call=_default_gh, log=print,
) -> list[int]:
    """Единственное место, переносящее «номера, объявленные структурным
    полем/строкой» в нативный `blockedBy` — задача #529, продолжение #371:
    до этой задачи цикл «пропустить номер вне открытого пула, иначе
    `add_dependency` + лог» жил ТОЛЬКО в `file_tasks.py::wire_declared_dependency`
    (путь АИ-ревью, строка «БЛОКИРУЕТСЯ:»). Второй вызывающий (авто-перенос
    структурного поля формы «Чем блокируется», `declared_deps.py::auto_wire`)
    переиспользует эту же функцию — третья копия одного цикла не заводится.

    Номер вне ТЕКУЩЕГО открытого пула с меткой `task` (уже закрыт, не задача,
    не существует) или ссылка на саму `blocked` — НЕ линкуется, печатается
    предупреждение: ложная связь опаснее отсутствующей (то же правило, что
    для ручной миграции #371). Повтор номера во входе (ответ поля
    «#55 #55», находка AI-ревью PR #537) даёт ОДНУ мутацию — дедупликация
    здесь, в единственном цикле переноса, закрывает случай для обоих путей
    (поле формы, строка «БЛОКИРУЕТСЯ:»)."""
    linked: list[int] = []
    for n in dict.fromkeys(blocking_numbers):
        if n == blocked or n not in open_numbers:
            log(f"    ! #{blocked}: «#{n}» — не открытая задача пула с меткой "
                f"task (или ссылка на себя) — связь НЕ поставлена")
            continue
        add_dependency(repo, blocked=blocked, blocking=n, gh_call=gh_call)
        linked.append(n)
        log(f"    -> #{blocked} заблокирована #{n} (нативный граф)")
    return linked


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
