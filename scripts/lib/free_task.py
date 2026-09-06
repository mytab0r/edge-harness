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

Пятый фильтр (находка ревью PR #478): задача, чей объявленный PR несёт
метку `conflict`, тоже исключается из ОБЩЕГО выбора. Без него — реальная дыра:
`scheduler.py::dispatch_conflict_rework` снимает assignee+замок ИМЕННО чтобы
адресно (вход `task=N`) довести конфликтный PR с бюджетом РОВНО одна попытка,
но освобождённая задача видна и generic-пульсу (`free_task()` без `--task`).
Если адресный прогон падает по квоте/крашу ДО того, как задача снова занята
(`task.sh::release-full` при quota_exhausted освобождает и то, и другое), она
временно свободна — и generic-пульс мог бы взять её В ОБХОД бюджета попыток,
даже ПОСЛЕ того, как владельцу уже ушла эскалация «бюджет исчерпан». Тот же
класс, который `unhealthy_pulls` уже закрывает СО СВОЕЙ стороны (conflict-PR
не считается «нездоровым», её задача никогда не освобождается ЭТИМ путём) —
но dispatch_conflict_rework ввёл НОВЫЙ путь освобождения, и его тоже нужно
исключить из общего пула, а не только направить в правильный адресный путь.

Импорт task_ref — importlib по файлу (тот же приём, что в contract_check.py):
скрипты запускаются как файлы, не как пакет.

«Пусто» и «сломано» — разные состояния CLI (rc 1 против rc 2), и это различие
обязано доходить до вызывающего task.sh: незаловленное исключение здесь
(битый JSON пула, не загрузившийся task_ref.py) молча превращалось бы в
«свободных задач нет»/«PR нет» — воркер либо тихо простаивал при живом пуле,
либо открывал второй PR на задачу, у которой первый уже есть (находка
AI-ревью PR #247, 2026-09-03). Загрузка task_ref и разбор JSON поэтому
обёрнуты явно: любой сбой — код 2 и причина в stderr, fail loud вместо
silent-wrong.

Приоритет внутри свободных (задача #361, владелец 2026-09-06): три уровня,
в этом порядке — (1) метка `area:process` (задача про сам процесс работы —
протокол, гвардии, контракт, ревью-гейты, CI-ворота — фундаментальнее любой
прикладной: блокирует ВСЮ остальную работу, не одну ветку); (2) число
ОТКРЫТЫХ задач, которые эта задача блокирует (нативный граф GitHub
`blockedBy`/`blocking`, поле `blocking_open`, читается
`scripts/lib/task_deps.py` — НЕ прозопарсинг тела, тот же запрещённый
класс, что уже закрыт для контракта «PR → задача», #251/#259); (3) номер
issue как proxy даты создания (монотонен по построению GitHub — меньше
номер, раньше создан, без исключений; design.md этого change объясняет,
почему не брать `createdAt` напрямую). `issue_priority_key`/
`prioritized_free` — новая сортировка; имя `oldest_free` и CLI-глагол
`oldest-free` сохранены ради совместимости вызова из task.sh, но читают
новый ключ — старая чистая сортировка по номеру не живёт вторым путём
параллельно, она — частный случай нового ключа при пустом графе и без меты
(все существующие тесты на «выбрать по номеру» остаются зелёными без
изменений: `labels`/`blocking_open` отсутствуют → уровни 1/2 не отличают
кандидатов → тайбрейк по номеру, тот же результат, что и раньше).

Граф пуст (ни один кандидат не блокирует ничего открытого) или мета-метка
не стоит ни у кого — не молчаливое вырождение: `main()` печатает
предупреждение в stderr при выборе (видимый сигнал, не тихий факт), выбор
при этом не останавливается (вырождение в уровень 3 для всех — легитимно).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

# Метка мета-уровня (docs/agents/LABELS.md): задача про сам процесс работы —
# протокол, гвардии, контракт, приёмка, аренда, ревью-гейты, CI-ворота.
META_LABEL = "area:process"


def _load_sibling(name: str):
    try:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).resolve().with_name(f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module
    except Exception as exc:  # noqa: BLE001 — любой сбой загрузки инструмента = код 2
        print(f"free_task.py: не смог загрузить {name}.py: {exc}", file=sys.stderr)
        sys.exit(2)


task_ref = _load_sibling("task_ref")
# CONFLICT_LABEL — одно место правды scripts/lib/review_labels.py, не вторая
# копия литерала "conflict" (тот же класс, что уже сводили #326/LABELS.md).
review_labels = _load_sibling("review_labels")

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


def free_candidates(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
    excluded: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Открытые задачи пула без исполнителя, без живого замка аренды (#121),
    без метки `waiting:owner` (находка AI-ревью PR #471, #470 — без этого
    фильтра задача, ждущая владельца, но ещё без исполнителя, стопорила бы
    весь диспатч как «старейшая свободная», см. докстринг модуля) и без
    объявленного PR в конфликте (`excluded` — `conflict_declared_tasks`,
    #478) — ФИЛЬТР, без сортировки (сортировку по приоритету #361 делает
    `prioritized_free`; исторически эта функция и сортировала по номеру —
    поведение перенесено в `prioritized_free`, здесь остаётся один фильтр,
    чтобы не задавать порядок в двух местах)."""
    locked = locked or set()
    excluded = excluded or set()
    return [
        issue for issue in issues
        if not (issue.get("assignees") or [])
        and issue["number"] not in locked
        and issue["number"] not in excluded
        and not _has_waiting_owner_label(issue)
    ]


def _is_meta(issue: dict[str, Any], meta_label: str = META_LABEL) -> bool:
    return any(label.get("name") == meta_label for label in (issue.get("labels") or []))


def issue_priority_key(issue: dict[str, Any], meta_label: str = META_LABEL) -> tuple:
    """Три уровня приоритета, в этом порядке (задача #361):
    (1) 0, если помечена `meta_label` (раньше всех), иначе 1;
    (2) минус число ОТКРЫТЫХ задач, которые блокирует эта (`blocking_open`,
        поле из `scripts/lib/task_deps.py` — больше блокирует, раньше);
    (3) номер issue — тайбрейк, proxy даты создания (меньше — раньше).
    Отсутствие `labels`/`blocking_open` в issue (REST-форма без графа) —
    легитимное значение «неизвестно», трактуется как «не мета»/«блокирует 0»,
    не ошибка: тайбрейк по номеру воспроизводит старое поведение целиком."""
    return (
        0 if _is_meta(issue, meta_label) else 1,
        -int(issue.get("blocking_open") or 0),
        issue["number"],
    )


def prioritized_free(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
    excluded: set[int] | None = None, meta_label: str = META_LABEL,
) -> list[dict[str, Any]]:
    """Свободные кандидаты (включая фильтр `excluded` — конфликтные задачи,
    #478), отсортированные по приоритету #361 (см. `issue_priority_key`) —
    старейшая-по-номеру больше не единственный критерий, это частный случай
    (уровень 3) при пустом графе/без меты."""
    candidates = free_candidates(issues, locked, excluded)
    return sorted(candidates, key=lambda issue: issue_priority_key(issue, meta_label))


def graph_is_empty(issues: list[dict[str, Any]], meta_label: str = META_LABEL) -> bool:
    """Ни у кого нет `blocking_open` > 0 и ни у кого нет `meta_label` — уровни
    1/2 не отличают НИ ОДНОГО кандидата, приоритет целиком вырождается в
    уровень 3 (номер). Не поломка (легитимное состояние на старте внедрения
    графа), но обязана быть видимым сигналом (AGENTS.md: fail loud, не
    silent-wrong) — печатается предупреждением в `main()`, не проглатывается."""
    return all(
        not int(issue.get("blocking_open") or 0) and not _is_meta(issue, meta_label)
        for issue in issues
    )


def oldest_free(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
    excluded: set[int] | None = None,
) -> dict[str, Any] | None:
    candidates = prioritized_free(issues, locked, excluded)
    return candidates[0] if candidates else None


def conflict_declared_tasks(prs: list[dict[str, Any]]) -> set[int]:
    """Номера задач, чей объявленный PR (`task_ref.task_from_branch` —
    единственный источник имени ветки, тот же, что `declared_pr_for_task`)
    несёт метку `conflict`. См. докстринг модуля (находка ревью PR #478) —
    такие задачи доводятся только адресно (`scheduler.py::
    dispatch_conflict_rework`, вход `task`), не через общий выбор.

    `prs` — принимаются ОБЕ формы ветки PR: плоское `headRefName`
    (`gh pr list --json number,headRefName,labels` — форма общего выбора
    воркера) и вложенная REST `head.ref` (снимок открытых PR scheduler.py):
    предикат «объявленный PR несёт conflict» не зависит от формы payload'а —
    вызов с REST-формой молча возвращал пустое множество, и исключение не
    работало (блокирующая находка 1 ревью PR #466). Labels — та же плоская
    форма `[{"name": ...}, ...]` в обеих формах."""
    result: set[int] = set()
    for pull in prs:
        branch = pull.get("headRefName") or (pull.get("head") or {}).get("ref") or ""
        number = task_ref.task_from_branch(branch)
        if number is None:
            continue
        names = {label.get("name") for label in pull.get("labels") or []}
        if review_labels.CONFLICT_LABEL in names:
            result.add(number)
    return result


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


def _parse_numbers(text: str) -> set[int]:
    """Формат общий и для `lease_cli locks` (замки), и для `conflict-tasks`
    ниже (номера через пробел, пусто — множество пусто)."""
    return {int(token) for token in text.split() if token}


def _print_numbers(numbers: set[int]) -> None:
    print(" ".join(str(n) for n in sorted(numbers)))


def main(argv: list[str]) -> int:
    if len(argv) in (2, 3, 4) and argv[0] == "oldest-free":
        issues = _load_json(Path(argv[1]))
        locked = _parse_numbers(argv[2]) if len(argv) >= 3 else None
        excluded = _parse_numbers(argv[3]) if len(argv) == 4 else None
        candidates = prioritized_free(issues, locked, excluded)
        if not candidates:
            return 1
        if graph_is_empty(candidates):
            print(
                "free_task.py: граф блокировок пуст и ни одна свободная задача "
                "не помечена area:process — приоритет сведён к дате создания "
                "(#361)", file=sys.stderr,
            )
        _print_issue_line(candidates[0])
        return 0
    if len(argv) == 3 and argv[0] == "declared-pr":
        task_number = int(argv[1])
        prs = _load_json(Path(argv[2]))
        pull = declared_pr_for_task(prs, task_number)
        if pull is None:
            return 1
        _print_pr_line(pull)
        return 0
    if len(argv) == 2 and argv[0] == "conflict-tasks":
        prs = _load_json(Path(argv[1]))
        _print_numbers(conflict_declared_tasks(prs))
        return 0
    print(
        "использование: free_task.py oldest-free <issues.json> [<locked>] [<excluded>] "
        "| declared-pr <N> <prs.json> | conflict-tasks <prs.json>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
