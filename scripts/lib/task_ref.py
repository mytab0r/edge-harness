#!/usr/bin/env python3
"""Номер задачи в тексте PR/issue — одно место правды (#187).

Класс проблемы: `f"#{n}" in text` и `line.split("#")` — сравнение подстрокой.
`#18` содержится в `#180`, `#181`, `#5180` — вылезло на живом прогоне orchestra
(33570081734): контракт спутал PR #185 (задача #182) с задачей #18. Число,
на которое ссылается `#N`, обязано иметь границу с обеих сторон: слева не
цифра (иначе `#18` внутри `#5180`), справа не цифра (иначе `#18` внутри
`#180`).

Импорт — importlib по файлу (паттерн claim_task/review_labels): скрипты
запускаются как файлы, не как пакет.
"""

import re

# Слева: не должно быть цифры прямо перед `#`-числом (запрещает `5#18`→`518`
# и `#5` перед `180`, т.е. случай "числовой хвост предыдущего номера слипся").
# Справа: не должно быть цифры сразу после числа (запрещает `#180` матчить `#18`).
_TASK_REF_RE = re.compile(r"(?<!\d)#(\d+)(?!\d)")


def extract_task_refs(text: str) -> list[int]:
    """Все номера задач `#N` из текста, с границей числа с обеих сторон.

    Порядок — как в тексте, дубликаты не схлопываются (вызывающий код решает
    сам, нужна ли уникальность и первый элемент).
    """
    if not text:
        return []
    return [int(match.group(1)) for match in _TASK_REF_RE.finditer(text)]


def references_task(text: str, task_number: int) -> bool:
    """Упоминает ли текст задачу `#task_number` — без ложных срабатываний на
    `#<task_number><ещё цифры>` или `<ещё цифры><task_number>` (класс #187)."""
    return task_number in extract_task_refs(text)


def declared_tasks(text: str) -> list[int]:
    """Задачи, ОБЪЯВЛЕННЫЕ телом PR — соглашение репозитория: номер задачи на
    строке, которая начинается с `#N` (обычно первая строка тела), а не любое
    упоминание номера в прозе (#195, второй экземпляр класса #187).

    `references_task`/`extract_task_refs` по всему тексту — это «упомянута»,
    не «объявлена»: у reap_stale/after_merge в scheduler.py широкая семантика
    нужна осознанно (не потерять живой PR или не снять чужой замок), там
    следует использовать их. Здесь — только для решений вида «эта задача уже
    занята этим PR», где декларация должна быть симметричной для своего и
    чужого PR.
    """
    if not text:
        return []
    refs = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            continue
        refs.extend(extract_task_refs(stripped))
    return refs


def declares_task(text: str, task_number: int) -> bool:
    """Объявляет ли тело PR задачу `#task_number` (см. declared_tasks)."""
    return task_number in declared_tasks(text)
