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

## Маркер производителя (issue #1277)

Замер 2026-09-15: девять программных производителей зовут `create_pool_issue`
(`scripts/review/file_tasks.py`, `scripts/orchestra/pulse_guard.py::
failure_watch`, `stall_detector.py`, `dependabot_alert_watch.py`,
`upstream_drift.py`, `health_audit.py`, `merge_health_watch.py`,
`scheduler.py::after_merge` — хвост чеклиста, `scheduler.py::
replace_closed_task_prs`), и различить их постфактум («какой производитель
завёл вот эту issue») машинно было НЕЧЕМ: минимум два производителя несут
только `labels=["task"]` без единой отличительной метки (review-findings,
хвост чеклиста), ещё два делят одну и ту же метку `auto-detected`
(stall-detector и task-replacement) — заголовок и метка это признак,
который переживает переименование только случайно, не по конструкции
(живой урок: класс #891/#893 — структурный признак ломается молча).

`producer` — обязательный параметр (не опциональный default, чтобы
пропустить его было НЕВОЗМОЖНО, а не просто нежелательно): значение
проставляется скрытым HTML-комментарием `<!-- pool-issue-producer: <id> -->`
первой строкой тела. Комментарий невидим в рендере GitHub (обычный markdown
HTML-comment), но читается обратно через API как обычный текст — маркер
переживает ЛЮБУЮ последующую правку заголовка/меток той же issue, потому
что он не является ни тем, ни другим, а получен здесь, в единственной точке
создания, и никогда не пересоздаётся руками.

Честная граница (что ломает этот признак — issue #1277, требование 1):
  - issue, заведённые ДО этого PR, маркера не несут вовсе — обратной силы
    у него нет (см. `scripts/lib/producer_orphan_watch.py`, `_legacy_classify`
    — отдельный, замороженный по построению эвристический классификатор
    ТОЛЬКО для истории до маркера, не для новых производителей);
  - владелец/агент, вручную отредактировавший тело issue и случайно (или
    намеренно) стерший HTML-комментарий, лишает эту конкретную issue
    признака — не защищено техническими средствами, только тем, что маркер
    невидим в рендере и незачем его трогать;
  - производитель, вызывающий `repos/{repo}/issues` POST'ом НАПРЯМУЮ, минуя
    `create_pool_issue` (обходя и REQUIRED_LABEL, и producer) — уже отдельно
    ловится CI-гвардией `scripts/ci/guards/pool-issue-create-guard.sh`, тот
    же периметр, вторая копия проверки здесь не заводится.
"""
from __future__ import annotations

import re
from typing import Callable, Sequence

GhFn = Callable[..., dict | list | None]

REQUIRED_LABEL = "task"

# Маркер производителя (issue #1277) — скрытый HTML-комментарий, первая
# строка тела. `[a-z0-9][a-z0-9-]*` — тот же алфавит, что у меток GitHub,
# сознательно уже (не любой символ) — производитель это машинный id, не
# свободный текст.
PRODUCER_MARKER_PREFIX = "<!-- pool-issue-producer: "
PRODUCER_MARKER_SUFFIX = " -->"
PRODUCER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PRODUCER_MARKER_RE = re.compile(
    r"<!--\s*pool-issue-producer:\s*([a-z0-9][a-z0-9-]*)\s*-->")


def producer_marker(producer: str) -> str:
    return f"{PRODUCER_MARKER_PREFIX}{producer}{PRODUCER_MARKER_SUFFIX}"


def extract_producer(body: str | None) -> str | None:
    """Обратное чтение маркера из уже созданной issue (`issue["body"]`,
    прод-форма REST/GraphQL/Search API — везде одно и то же поле). `None`,
    если маркера нет вовсе (issue заведена до этого PR, либо не через
    `create_pool_issue`, либо маркер стёрт ручной правкой — три причины,
    неразличимые отсюда, см. докстринг модуля)."""
    if not body:
        return None
    match = PRODUCER_MARKER_RE.search(body)
    return match.group(1) if match else None


def create_pool_issue(
    gh: GhFn,
    repo: str,
    title: str,
    body: str,
    labels: Sequence[str],
    producer: str,
) -> dict:
    """Заводит issue POST'ом `repos/{repo}/issues` через переданный `gh`
    (тот же `gh(*args)`, что уже используют file_tasks.py/scheduler.py/
    stall_detector.py — подключение по инъекции зависимости, не импорт
    транспорта: у каждого вызывающего свой модуль gh() поверх `gh api`,
    второй копии транспорта здесь не заводим).

    Отказывает ДО вызова gh, если среди labels нет `task` — issue не
    создаётся, сеть не тратится (тот же приём, что у `scripts/git/pr-create`
    и `scripts/gh/issue-create`: проверка на входе, не гвардия по факту).

    `producer` — ОБЯЗАТЕЛЬНЫЙ (issue #1277): невалидный/пустой id — тот же
    класс отказа ДО сети, что и отсутствие `task` — иначе следующий,
    десятый производитель молча остался бы неразличимым, как review-findings
    и хвост чеклиста сегодня (см. докстринг модуля)."""
    if REQUIRED_LABEL not in labels:
        raise RuntimeError(
            f"create_pool_issue: labels={list(labels)} без обязательной "
            f"«{REQUIRED_LABEL}» — issue не видна пулу воркера "
            f"(класс #179, живой случай #523/#425), не завожу.")
    if not producer or not PRODUCER_ID_RE.match(producer):
        raise RuntimeError(
            f"create_pool_issue: producer={producer!r} невалиден (нужен "
            "непустой id из [a-z0-9-], первый символ — буква/цифра) — без "
            "него производитель этой issue неразличим постфактум (issue "
            "#1277), не завожу.")
    body_with_marker = f"{producer_marker(producer)}\n{body}"
    args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}",
            "-f", "body=" + body_with_marker]
    for label in labels:
        args += ["-f", f"labels[]={label}"]
    return gh(*args)
