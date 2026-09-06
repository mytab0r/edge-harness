#!/usr/bin/env python3
"""Выбор свободной задачи для воркера — одно место правды (#245), доступное
и bash-скрипту (task.sh), и pytest напрямую.

Два дефекта одной функции `free_task()` (`scripts/worker/task.sh`), оба
чинятся здесь:

  1. «Занята» раньше значило «номер где-то упомянут в теле открытого PR»
     (`scan("#[0-9]+")` по всему тексту) — тот же класс подстрочного
     совпадения, что уже чинили в `scripts/orchestra/contract_check.py`
     (#187, #195). Здесь номер задачи, которую PR ОБЪЯВЛЯЕТ, берётся через
     `task_ref.task_from_branch` (`headRefName`) — единственный источник
     (#394, решение владельца 2026-09-06): тело PR не читается вовсе.

  2. Открытый PR у задачи БЕЗ исполнителя (issue.assignees пуст) больше не
     исключает её из пула. `scheduler.py::unhealthy_pulls` снимает
     исполнителя именно для того, чтобы задачу подхватили и довели
     существующий PR — раньше `free_task()` такую задачу считал занятой
     навсегда (PR не даёт её выбрать, а без исполнителя её и не «доводят»).
     Единственный критерий свободы — issue.assignees пуст: открытый PR без
     назначенного исполнителя на issue — сигнал «довести», не «пропустить».
     С исполнителем задача по-прежнему недоступна (кто-то уже работает).

Третий, независимый фильтр (#121, атомарная аренда): задача под живым замком
(`scripts/lib/claim_task.py::locked_tasks`, включая ещё не собранный протухший)
исключается из кандидатов — экономия прогона, не защита, гарантией остаётся
сам `claim` в task.sh. `locked` передаётся вызывающей стороной (task.sh знает
про `lease_cli`, здесь — только фильтрация множества).

Четвёртый фильтр (находка AI-ревью PR #471, #470): задача с меткой
`waiting:owner` БЕЗ исполнителя (типичный случай — авто-метка свежей задачи
или ручная разметка при заведении, до того как кто-то успел стать assignee)
проходила бы фильтр «нет assignees» как свободная — `oldest_free` выбирал бы
ровно её на каждом пульсе (она старейшая свободная), `claim()` отказывал бы
(`scripts/lib/claim_task.py`), воркер выходил бы зелёным no-op, а задачи ЗА
ней в очереди не брался бы вовсе: тормоз одной задачи останавливал бы весь
диспатч, пока владелец не ответит (класс #255). У `blocked` этой дыры нет,
потому что playbook эскалации (`task.sh`) оставляет assignee — задача и так
невидима для `free_candidates` по первому критерию; `waiting:owner` assignee
не гарантирует, поэтому фильтруется по метке явно, тем же местом правды, что
и `claim()` (`scripts/lib/claim_task.py`) — второй копии строки `waiting:owner`
не заводим, только по литералу, значение читается из `labels` — issues,
переданные без этого поля (объект без ключа `labels`), считаются НЕ несущими
метку (см. `_has_waiting_owner_label`).

Импорт task_ref — importlib по файлу (тот же приём, что в contract_check.py):
скрипты запускаются как файлы, не как пакет.

Четвёртый фильтр (task-rework-loop #256, design.md п.5): задачи с меткой
needs-spec или blocked не выбираются автоматически (EXCLUDED_LABELS ниже) —
обе означают «ждёт человека, не исполнителя», и автоматический выбор вернул
бы задачу в работу сразу после того, как route_to_needs_spec/эскалация
нарочно сняли её с исполнителя. Фильтр жив только при полном списке полей
в вызывающем task.sh (`--json …,labels`) — гвардия
test_free_task.py::test_task_sh_passes_labels_field_to_free_task красит
пропавший `labels`.

«Пусто» и «сломано» — разные состояния CLI (rc 1 против rc 2), и это различие
обязано доходить до вызывающего task.sh: незаловленное исключение здесь
(битый JSON пула, не загрузившийся task_ref.py) молча превращалось бы в
«свободных задач нет»/«PR нет» — воркер либо тихо простаивал при живом пуле,
либо открывал второй PR на задачу, у которой первый уже есть (находка
AI-ревью PR #247, 2026-09-03). Загрузка task_ref и разбор JSON поэтому
обёрнуты явно: любой сбой — код 2 и причина в stderr, fail loud вместо
silent-wrong.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_task_ref():
    try:
        spec = importlib.util.spec_from_file_location(
            "task_ref", Path(__file__).resolve().with_name("task_ref.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception as exc:  # noqa: BLE001 — любой сбой загрузки инструмента = код 2
        print(f"free_task.py: не смог загрузить task_ref.py: {exc}", file=sys.stderr)
        sys.exit(2)


task_ref = _load_task_ref()

# Одно место правды на литерал — тот же, что claim_task.py::claim уже
# использует для отказа в аренде (docs/agents/LABELS.md, строка waiting:owner).
WAITING_OWNER_LABEL = "waiting:owner"


def _has_waiting_owner_label(issue: dict[str, Any]) -> bool:
    """`labels` — форма `gh issue list --json labels` ([{"name": ...}, ...]).
    Issue без ключа `labels` вовсе (старые вызовы/фикстуры без этого поля)
    считается НЕ несущей метку — то же допущение, что уже применяет
    `assignees`."""
    return any(
        (label or {}).get("name") == WAITING_OWNER_LABEL
        for label in (issue.get("labels") or [])
    )


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — «сломано», не «пусто» (fail loud)
        print(f"free_task.py: не смог прочитать/разобрать {path}: {exc}", file=sys.stderr)
        sys.exit(2)


# Метки, исключающие задачу из выбора, — одно место правды на имя в
# scripts/lib/review_labels.py (NEEDS_SPEC_LABEL/BLOCKED_LABEL, реестр —
# docs/agents/LABELS.md, гвардия test_label_registry.py):
#   needs-spec — бюджет реворка исчерпан (task-rework-loop #256, design.md
#     п.5): задача ждёт уточнения требования аналитиком, не исполнителя;
#     выбор её воркером сжёг бы бюджет заново на новом PR, пока владельцу
#     уходит эскалация о задаче, которую уже «перерабатывают» (находка
#     AI-ревью PR #408 — тормоз без газа в обратную сторону);
#   blocked — эскалация владельцу (playbook): то же состояние ожидания.
# scheduler.py пропускает обе метки в unhealthy_pulls/reap_stale
# (BLOCKED_LABEL/_issue_is_blocked) — здесь закрыт второй путь класса
# «метка есть, а путь выбора её не проверяет». Константы берутся из
# review_labels (importlib — тот же приём, что task_ref выше), не литералами:
# переезд имени в одном месте не оставляет второй потребитель молча мёртвым.
_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().with_name("review_labels.py"))
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)

EXCLUDED_LABELS = (review_labels.NEEDS_SPEC_LABEL, review_labels.BLOCKED_LABEL)


def _label_names(issue: dict[str, Any]) -> set[str]:
    """Прод-форма `gh issue list --json labels` — массив объектов с полем
    `name` (замер живого API 2026-09-06), не плоские строки."""
    return {label["name"] for label in issue.get("labels") or [] if label.get("name")}


def free_candidates(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Открытые задачи пула без исполнителя, без живого замка аренды (#121),
    без метки `waiting:owner` (находка AI-ревью PR #471, #470 — без этого
    фильтра задача, ждущая владельца, но ещё без исполнителя, стопорила бы
    весь диспатч как «старейшая свободная», см. докстринг модуля) и без
    исключающей метки (EXCLUDED_LABELS выше, design.md task-rework-loop
    п.5), отсортированные по номеру (старейшая первой — воркер не должен
    хватать самую свежую косметику)."""
    locked = locked or set()
    free = [
        issue for issue in issues
        if not (issue.get("assignees") or [])
        and issue["number"] not in locked
        and not _has_waiting_owner_label(issue)
        and not (_label_names(issue) & set(EXCLUDED_LABELS))
    ]
    return sorted(free, key=lambda issue: issue["number"])


def oldest_free(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
) -> dict[str, Any] | None:
    candidates = free_candidates(issues, locked)
    return candidates[0] if candidates else None


def declared_pr_for_task(prs: list[dict[str, Any]], task_number: int) -> dict[str, Any] | None:
    """Открытый PR, чья ветка называет `task_number` (#394, одно место
    правды #259, решение владельца 2026-09-06: единственный источник — имя
    agent-ветки, тело PR не читается вовсе). То же правило, что
    `contract_check.py`, применённое симметрично к «своему» и «чужому» PR.

    `prs` — форма `gh pr list --json number,headRefName` (плоское поле
    `headRefName`, не вложенный REST `head.ref`)."""
    for pull in prs:
        if task_ref.task_from_branch(pull.get("headRefName") or "") == task_number:
            return pull
    return None


def _print_issue_line(issue: dict[str, Any]) -> None:
    print(f"{issue['number']}\t{issue['title']}")


def _print_pr_line(pull: dict[str, Any]) -> None:
    print(f"{pull['number']}\t{pull.get('headRefName') or ''}")


def _parse_locked(text: str) -> set[int]:
    """`lease_cli locks` печатает номера через пробел (пусто — замков нет)."""
    return {int(token) for token in text.split() if token}


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3) and argv[0] == "oldest-free":
        issues = _load_json(Path(argv[1]))
        locked = _parse_locked(argv[2]) if len(argv) == 3 else None
        issue = oldest_free(issues, locked)
        if issue is None:
            return 1
        _print_issue_line(issue)
        return 0
    if len(argv) == 3 and argv[0] == "declared-pr":
        task_number = int(argv[1])
        prs = _load_json(Path(argv[2]))
        pull = declared_pr_for_task(prs, task_number)
        if pull is None:
            return 1
        _print_pr_line(pull)
        return 0
    print(
        "использование: free_task.py oldest-free <issues.json> [<locked>] "
        "| declared-pr <N> <prs.json>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
