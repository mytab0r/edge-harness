"""Единое место правды на создание issue пула программным путём (#526).

Живой случай, из-за которого этот файл появился: issue #523 заведена агентом
напрямую (`gh issue create --label white-spot`), issue #425 — без единой
метки вовсе. Обе обошли шаблон (шаблон применяется только к веб-форме, не к
API/CLI-созданию) и остались без `task` — пул воркера физически их не видит
(`scripts/orchestra/scheduler.py::open_task_issues` читает только
`labels=task`, класс #179). До этого файла четыре программных создателя
(`scripts/review/file_tasks.py`, `scripts/orchestra/scheduler.py::after_merge`
— хвост чеклиста, `scripts/orchestra/stall_detector.py::create_task`,
`scripts/orchestra/upstream_drift.py::create_bump_issue`) сами собирали
`-f labels[]=task` каждый в своём месте — рабочий код по факту, но
без единой точки, которая сделала бы пропуск метки невозможным для СЛЕДУЮЩЕГО
создателя. `create_pool_issue` — эта точка: labels без `task` — RuntimeError
ДО сетевого вызова, не постфактум-гвардия по уже созданной issue.

Канал агентских/ручных запросов (`gh issue create` в терминале, вне
Python-скриптов репозитория) этот файл не покрывает — там единственный
документированный путь — `scripts/gh/issue-create` (тот же инвариант, на
bash). Из cf-worker (Cloudflare Workers, `cf-worker/src/harness.ts`) позвать
ни этот модуль, ни bash-обёртку нельзя — там своя проверка
(`cf-worker/test/harness.spec.ts`, ассерт `labels` содержит `task`).

#720: обязательное машиночитаемое объявление связи в теле issue.
Две формы записи (та же семантика, что у веб-форм и AI-ревью):
  1. Структурное поле формы: заголовок `### Чем блокируется` (уровень `#{1,6}`,
     пустая строка между заголовком и ответом опциональна) + ответ
     (номера через пробел `#123 #124` или явное `ничем`).
  2. Инлайн-конвенция: последняя непустая строка тела — `БЛОКИРУЕТСЯ: …`
     (номера через пробел `#123 #124` или `ничем`).
Ответ «ничем» — ВАЛИДЕН: поле обязательное, а не непустое (то же правило,
что `declared_deps.form_field_numbers` реализует). Отсутствие ОБОИХ форм —
RuntimeError ДО сетевого вызова.
"""
from __future__ import annotations

import re
from typing import Callable, Sequence

GhFn = Callable[..., dict | list | None]

REQUIRED_LABEL = "task"

# Структурное поле «Чем блокируется» — тот же шаблон, что
# declared_deps._FORM_FIELD_RE, но без вырезания fenced/HTML (здесь тело
# не от шаблона, шумов нет; проверяем только наличие поля по контракту).
_FORM_FIELD_RE = re.compile(
    r"^#{1,6}\s*Чем\s+блокируется\s*\n+([^\n]*)",
    re.IGNORECASE | re.MULTILINE,
)

# Инлайн-конвенция AI-ревью — последняя непустая строка,
# тот же шаблон, что ai_review.BLOCKED_BY_RE.
_INLINE_RE = re.compile(r"^БЛОКИРУЕТСЯ:\s*(ничем|#\d+(?:\s+#\d+)*)\s*$")


def _has_declared_dependency(body: str) -> bool:
    """Проверяет, что тело несет машиночитаемое объявление связи.
    Возвращает True, если найдена ХОТЯ БЫ ОДНА из двух форм:
    - структурное поле «Чем блокируется» (заголовок + ответ, ответ может быть «ничем»)
    - инлайн-строка «БЛОКИРУЕТСЯ: …» (последняя непустая строка, значение может быть «ничем»)
    """
    if not body:
        return False
    # 1) Структурное поле формы
    match = _FORM_FIELD_RE.search(body)
    if match:
        value = match.group(1).strip()
        if value == "" or value.lower() == "ничем":
            return True
        # есть номера — тоже валидно
        if re.search(r"#\d+", value):
            return True
    # 2) Инлайн-конвенция: последняя непустая строка
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines:
        match = _INLINE_RE.match(lines[-1])
        if match:
            return True
    return False


def _dependency_error_hint() -> str:
    """Готовая строка для вставки в тело — газ к тормозу."""
    return (
        "### Чем блокируется\n"
        "ничем\n"
    )


def create_pool_issue(
    gh: GhFn,
    repo: str,
    title: str,
    body: str,
    labels: Sequence[str],
) -> dict:
    """Заводит issue POST'ом `repos/{repo}/issues` через переданный `gh`
    (тот же `gh(*args)`, что уже используют file_tasks.py/scheduler.py/
    stall_detector.py — подключение по инъекции зависимости, не импорт
    транспорта: у каждого вызывающего свой модуль gh() поверх `gh api`,
    второй копии транспорта здесь не заводим).

    Отказывает ДО вызова gh, если среди labels нет `task` — issue не
    создаётся, сеть не тратится (тот же приём, что у `scripts/git/pr-create`
    и `scripts/gh/issue-create`: проверка на входе, не гвардия по факту).

    #720: Отказывает ДО вызова gh, если тело не несет машиночитаемого
    объявления связи (структурное поле «Чем блокируется» ИЛИ инлайн
    «БЛОКИРУЕТСЯ: …»). Ответ «ничем» валиден. В тексте отказа — готовая
    строка для вставки.
    """
    if REQUIRED_LABEL not in labels:
        raise RuntimeError(
            f"create_pool_issue: labels={list(labels)} без обязательной "
            f"«{REQUIRED_LABEL}» — issue не видна пулу воркера "
            f"(класс #179, живой случай #523/#425), не завожу."
        )
    if not _has_declared_dependency(body):
        hint = _dependency_error_hint()
        raise RuntimeError(
            f"create_pool_issue: тело issue не содержит машиночитаемого "
            f"объявления связи (поле «Чем блокируется» или строка "
            f"«БЛОКИРУЕТСЯ: …») — задача #720: 89% пула без связи, уровень "
            f"приоритета не работает. Добавь в тело:\n{hint}"
        )
    args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", "body=" + body]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    return gh(*args)
