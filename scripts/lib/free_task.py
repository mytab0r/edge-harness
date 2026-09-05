#!/usr/bin/env python3
"""Выбор свободной задачи для воркера — одно место правды (#245), доступное
и bash-скрипту (task.sh), и pytest напрямую.

Два дефекта одной функции `free_task()` (`scripts/worker/task.sh`), оба
чинятся здесь:

  1. «Занята» раньше значило «номер где-то упомянут в теле открытого PR»
     (`scan("#[0-9]+")` по всему тексту) — тот же класс подстрочного
     совпадения, что уже чинили в `scripts/orchestra/contract_check.py`
     (#187, #195). Здесь номер задачи, которую PR ОБЪЯВЛЯЕТ, берётся из
     `task_ref.declared_tasks` — ПЕРВАЯ непустая строка тела (без учёта
     HTML-комментариев), начинающаяся с `#` и сразу цифрой, не любая
     строка с `#N` (#251, #312), симметрично contract_check.py.

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


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — «сломано», не «пусто» (fail loud)
        print(f"free_task.py: не смог прочитать/разобрать {path}: {exc}", file=sys.stderr)
        sys.exit(2)


def free_candidates(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Открытые задачи пула без исполнителя и без живого замка аренды (#121)
    — ФИЛЬТР, без сортировки (сортировку по приоритету делает
    `prioritized_free`; исторически эта функция и сортировала по номеру —
    поведение перенесено в `prioritized_free`, здесь остаётся один фильтр,
    чтобы не задавать порядок в двух местах)."""
    locked = locked or set()
    return [
        issue for issue in issues
        if not (issue.get("assignees") or []) and issue["number"] not in locked
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
    issues: list[dict[str, Any]], locked: set[int] | None = None, meta_label: str = META_LABEL,
) -> list[dict[str, Any]]:
    """Свободные кандидаты, отсортированные по приоритету #361 (см.
    `issue_priority_key`) — старейшая-по-номеру больше не единственный
    критерий, это частный случай (уровень 3) при пустом графе/без меты."""
    candidates = free_candidates(issues, locked)
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
) -> dict[str, Any] | None:
    candidates = prioritized_free(issues, locked)
    return candidates[0] if candidates else None


def declared_pr_for_task(prs: list[dict[str, Any]], task_number: int) -> dict[str, Any] | None:
    """Открытый PR, ОБЪЯВЛЯЮЩИЙ задачу `task_number` первым объявленным в теле
    (`declared_tasks(body)[0]` — ПЕРВАЯ непустая строка тела, без учёта
    HTML-комментариев, начинающаяся с `#` и сразу цифрой, #251, #312) —
    то же правило, что `contract_check.py`, применённое симметрично
    к «своему» и «чужому» PR."""
    for pull in prs:
        body = pull.get("body") or ""
        declared = task_ref.declared_tasks(body)
        if declared and declared[0] == task_number:
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
        candidates = prioritized_free(issues, locked)
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
    print(
        "использование: free_task.py oldest-free <issues.json> [<locked>] "
        "| declared-pr <N> <prs.json>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
