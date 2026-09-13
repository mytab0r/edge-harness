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
"""
from __future__ import annotations

from typing import Callable, Sequence

GhFn = Callable[..., dict | list | None]

REQUIRED_LABEL = "task"


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
    и `scripts/gh/issue-create`: проверка на входе, не гвардия по факту)."""
    if REQUIRED_LABEL not in labels:
        raise RuntimeError(
            f"create_pool_issue: labels={list(labels)} без обязательной "
            f"«{REQUIRED_LABEL}» — issue не видна пулу воркера "
            f"(класс #179, живой случай #523/#425), не завожу.")
    args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", "body=" + body]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    return gh(*args)
