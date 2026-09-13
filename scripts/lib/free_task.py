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

Приоритет внутри свободных (задача #361, владелец 2026-09-06; пересмотрен
задачей #224 — «граф блокировок вместо выдуманных дат») — ТРИ уровня, в
этом порядке:

  (1) «Механизм конвейера сейчас красный» (`BROKEN_LABELS` —
      `ci-failure`/`self-audit`) ИЛИ задача транзитивно блокирует хотя бы
      одну такую (`task_deps.blockers_of`) — факт, не самооценка: конвейер
      измеримо стоит, пока это открыто, и стоимость простоя растёт с каждым
      прогоном, а не с датой заведения. Замер задачи #224 на живом пуле
      (303 открытых): #665 («починить 500 на ingest-эндпоинте морды») сама
      не несёт `ci-failure`, но НАПРЯМУЮ блокирует три открытые
      `ci-failure`-задачи (#577/#675/#981) — старым ключом (только метка
      `area:process` как уровень 1) она стояла НИЖЕ всех 114 задач
      `area:process`, включая те, что не блокируют ничего; новым — на
      первом месте всего пула.
  (2) Метка `area:process` (задача про сам процесс работы — протокол,
      гвардии, контракт, ревью-гейты, CI-ворота), если задача не попала в
      уровень 1. Уровень остаётся большим (114 из 303 открытых, замер
      2026-09-12) — вырожденным его делает НЕ размер группы, а то, что
      внутри неё раньше не было отличающего сигнала: подсортировка тем же
      транзитивным весом (см. (3)) убирает эту вырожденность там, где граф
      её видит, честно оставляя тай-брейк по номеру там, где сигнала
      физически нет (не выдуманная дата — явно объявленное «неизвестно»,
      см. `priority_reason`).
  (3) Транзитивный вес графа блокировок (`task_deps.transitive_blocking_counts`,
      НЕ прямой `blocking_open`) — сколько ОТКРЫТЫХ задач этого же пула
      закрытие текущей задачи в конечном счёте приближает, включая цепочки
      длиннее одного шага (A блокирует B, B блокирует C — A получает вес
      за оба). Прямой счётчик `blocking_open` остаётся полем данных (его
      по-прежнему отдаёт `task_deps.fetch_pool`), но сортировку ведёт
      транзитивный — обобщение старого уровня 2 задачи #361, не второй
      параллельный расчёт.
  (4) Номер issue как proxy даты создания (тайбрейк, без изменений с #361).

`issue_priority_key`/`prioritized_free` — та же новая сортировка, что и
раньше по имени; `oldest_free`/CLI-глагол `oldest-free` сохранены ради
совместимости вызова из task.sh, но читают новый ключ. Старая чистая
сортировка по номеру — частный случай нового ключа при пустом графе и без
меты/поломки (все прежние тесты «выбрать по номеру» остаются зелёными без
изменений: без `labels`/`blocked_by_open` уровни 1/2/3 не отличают
кандидатов → тайбрейк по номеру, тот же результат).

Граф пуст (ни один кандидат не блокирует ничего открытого и не является
предком сломанного) или ни у кого нет ни `BROKEN_LABELS`, ни мета-метки —
не молчаливое вырождение: `main()` печатает предупреждение в stderr при
выборе (видимый сигнал, не тихий факт), выбор при этом не останавливается
(вырождение в уровень 4 для всех — легитимно). Связка с #720 (объявление
связи не спрашивают при заведении — граф долго останется разреженным):
приоритет здесь СОЗНАТЕЛЬНО спроектирован работающим и на разреженном графе
— уровень 1 (сейчас красный) не зависит от графа вовсе, уровень 3
(транзитивный вес) деградирует к 0 для большинства задач, но не ломает
сортировку и не искажает уровни 1/2. Автоматическое обратное заполнение
графа из уже написанных тел (регэксп по прозе «Связано: #N» и т.п.) здесь
СОЗНАТЕЛЬНО не делается: `docs/agents/PROTOCOL.md`/`declared_deps.py`
и так называют «ложная связь опаснее отсутствующей» для СТРУКТУРНОГО поля
формы — для вольной прозы старых 177 issue из #720 у нас ещё меньше
уверенности в номере, а массовая мутация графа по эвристике на живых 300
issues необратима иначе, чем ещё 300 отменяющих мутаций. Дешёвое решение
без этого риска — сам приоритет не требует плотного графа (см. выше).

`priority_reason(issue, pool)` отвечает на критерий 1 задачи #224 («для
любой открытой задачи — почему она здесь») фактами этого же ключа: тир,
какая именно метка/цепочка его дала, транзитивный вес и его источник (кого
именно закрытие этой задачи двигает), номер-тайбрейк.

Шестой потребитель (#695, живой случай 2026-09-07: задачи #684/#685/#689
заведены с `area:orchestra` вместо `area:process` и встали в хвост очереди
из 200 задач, при этом группа приоритета уже несла #194/#168/#226 про тот
же дефект другими словами) — `scripts/gh/issue-create` обязан ПЕРЕД
заведением новой задачи показать текущий верх группы приоритета, чтобы
«та же проблема другими словами» ловилась глазами, раз дедуп по схожести
заголовка (`duplicate_guard.py`) её не ловит и не может (честный потолок
токенного сравнения). CLI-режимы `meta-label` (печатает `META_LABEL` —
одно место правды на литерал для bash-обёртки, не вторая копия строки
"area:process") и `priority-top <repo> [n]` (сеть — `task_deps.fetch_pool`,
сортировка — тот же `issue_priority_key`, что и `prioritized_free`, не
второй экземпляр сортировки) обслуживают именно этот путь.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

# Метка мета-уровня (docs/agents/LABELS.md): задача про сам процесс работы —
# протокол, гвардии, контракт, приёмка, аренда, ревью-гейты, CI-ворота.
META_LABEL = "area:process"

# Метки «механизм конвейера сейчас красный», задача #224 — зеркалят буквальные
# строки `scripts/orchestra/pulse_guard.py::FAILURE_WATCH_LABEL` ("ci-failure")
# и `scripts/orchestra/health_audit.py::SELF_AUDIT_LABEL` ("self-audit"), НЕ
# импортом (lib не должен зависеть от orchestra — обратная зависимость уже
# есть: `scheduler.py` сам загружает `free_task.py` через importlib, обратный
# импорт создал бы цикл), а второй копией литерала, синхронизацию которой
# держит гвардия по исходнику (`test_broken_labels_mirror_orchestra_literals`
# в test_free_task.py) — тот же приём, что уже применяет `docs/agents/
# LABELS.md` для меток-вердиктов, не второй source of truth без проверки.
BROKEN_LABELS = frozenset({"ci-failure", "self-audit"})


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
# fetch_pool — тот же GraphQL-обход, что уже читает task.sh для приоритета
# (docstring выше) — CLI `priority-top` его переиспользует, не заводит
# второй сетевой путь.
task_deps = _load_sibling("task_deps")

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


def _is_broken(issue: dict[str, Any], broken_labels: frozenset[str] = BROKEN_LABELS) -> bool:
    return any(
        (label.get("name") in broken_labels) for label in (issue.get("labels") or [])
    )


def _urgent_numbers(
    issues: list[dict[str, Any]], broken_labels: frozenset[str] = BROKEN_LABELS,
) -> set[int]:
    """Множество «уровень 1» задачи #224: сами несут `broken_labels` (метка
    факта «механизм сейчас красный») ИЛИ транзитивно блокируют хотя бы одну
    такую (`task_deps.blockers_of` — обратный BFS по `blocked_by_open` того
    же пула, без сети). Считается один раз на пул (не на кандидата), потому
    что зависит от ВСЕХ issues, не только от одной — сортировка по нему
    вызывает эту функцию один раз, не по разу на каждый ключ сравнения."""
    broken = {issue["number"] for issue in issues if _is_broken(issue, broken_labels)}
    return broken | task_deps.blockers_of(issues, broken)


def issue_priority_key(
    issue: dict[str, Any], meta_label: str = META_LABEL,
    urgent: set[int] | None = None, transitive: dict[int, int] | None = None,
) -> tuple:
    """Кортеж из трёх позиций сравнения, четыре уровня по смыслу (задача
    #224, обобщает #361):
    (1) `tier` — 0, если задача в `urgent` (несёт `BROKEN_LABELS` или
        транзитивно блокирует такую — см. `_urgent_numbers`); иначе 1, если
        помечена `meta_label`; иначе 2 — единственная позиция сравнения,
        различающая уровни (1)/(2) исходного описания задачи #224;
    (2) минус транзитивный вес (`transitive`, задача #224: обобщение
        `blocking_open` на всю цепочку, не только прямых соседей — см.
        `task_deps.transitive_blocking_counts`) — решает ВНУТРИ `tier`;
    (3) номер issue — тайбрейк, proxy даты создания (меньше — раньше).

    `urgent`/`transitive` — предвычисленные вызывающей стороной один раз на
    ВЕСЬ пул (`prioritized_free`/`priority_top`), не второй экземпляр
    расчёта на каждый ключ сравнения. Отсутствие обоих (вызов с одной
    issue, без контекста пула) — легитимное значение «неизвестно»,
    деградирует к прежнему полю `blocking_open` и к «не urgent» — тайбрейк
    по номеру воспроизводит поведение #361 целиком, не ошибка."""
    is_urgent = issue["number"] in urgent if urgent is not None else _is_broken(issue)
    weight = (
        transitive.get(issue["number"], 0) if transitive is not None
        else int(issue.get("blocking_open") or 0)
    )
    tier = 0 if is_urgent else (1 if _is_meta(issue, meta_label) else 2)
    return (tier, -weight, issue["number"])


def priority_top(
    issues: list[dict[str, Any]], top_n: int = 15, meta_label: str = META_LABEL,
) -> list[dict[str, Any]]:
    """Верх группы приоритета (#695) — `issues` уже отфильтрованы по
    `meta_label` вызывающей стороной (`task_deps.fetch_pool(repo, meta_label)`
    сам фильтрует GraphQL-запросом), здесь только сортировка ТЕМ ЖЕ ключом,
    что `prioritized_free` (`issue_priority_key`, не второй экземпляр), и
    срез до `top_n`. Печать перед заведением новой задачи в
    `scripts/gh/issue-create` (класс #695: «та же проблема другими словами»
    не ловится дедупом по схожести заголовка, но ловится глазами, если верх
    очереди виден в момент заведения). `urgent`/`transitive` считаются на
    том же `issues` (уже отфильтрованном подмножестве, задача #224) — честная
    граница: цепочка через issue вне этого подмножества не видна (то же
    ограничение уже было у прежнего прямого `blocking_open`)."""
    urgent = _urgent_numbers(issues)
    transitive = task_deps.transitive_blocking_counts(issues)
    return sorted(
        issues, key=lambda issue: issue_priority_key(issue, meta_label, urgent, transitive),
    )[:top_n]


def prioritized_free(
    issues: list[dict[str, Any]], locked: set[int] | None = None,
    excluded: set[int] | None = None, meta_label: str = META_LABEL,
) -> list[dict[str, Any]]:
    """Свободные кандидаты (включая фильтр `excluded` — конфликтные задачи,
    #478), отсортированные по приоритету #224/#361 (см. `issue_priority_key`)
    — старейшая-по-номеру больше не единственный критерий, это частный
    случай (уровень 4) при пустом графе/без меты/без поломки. `urgent`/
    `transitive` считаются на ПОЛНОМ `issues` (не только на `candidates`) —
    цепочка блокировки может проходить через занятую/чужую задачу, которая
    сама не кандидат, но участвует в графе."""
    candidates = free_candidates(issues, locked, excluded)
    urgent = _urgent_numbers(issues)
    transitive = task_deps.transitive_blocking_counts(issues)
    return sorted(
        candidates, key=lambda issue: issue_priority_key(issue, meta_label, urgent, transitive),
    )


def graph_is_empty(issues: list[dict[str, Any]], meta_label: str = META_LABEL) -> bool:
    """Ни у кого нет транзитивного веса > 0, ни у кого нет `meta_label` и
    никто не «сейчас красный» (`BROKEN_LABELS`/`_urgent_numbers`) — уровни
    1/2/3 не отличают НИ ОДНОГО кандидата, приоритет целиком вырождается в
    уровень 4 (номер). Не поломка (легитимное состояние на старте внедрения
    графа/до заведения ci-failure задач), но обязана быть видимым сигналом
    (AGENTS.md: fail loud, не silent-wrong) — печатается предупреждением в
    `main()`, не проглатывается."""
    urgent = _urgent_numbers(issues)
    transitive = task_deps.transitive_blocking_counts(issues)
    return all(
        not transitive.get(issue["number"], 0)
        and not _is_meta(issue, meta_label)
        and issue["number"] not in urgent
        for issue in issues
    )


def priority_reason(
    issue: dict[str, Any], pool: list[dict[str, Any]], meta_label: str = META_LABEL,
    broken_labels: frozenset[str] = BROKEN_LABELS,
) -> str:
    """Отвечает на критерий 1 задачи #224 («для любой открытой задачи можно
    получить ответ, почему она в очереди на этом месте, фактом»): рендерит
    тот же ключ, что реально сортирует (`issue_priority_key`), в виде фраз,
    называющих ИСТОЧНИК каждой цифры — не пересказ, а те же данные (метки
    issue, `blocked_by_open`, транзитивный вес того же `pool`), поэтому
    объяснение не может разойтись с реальным порядком (единое место
    правды — не вторая формула).

    `pool` — ВЕСЬ пул (не только кандидаты), тот же аргумент, что получают
    `prioritized_free`/`priority_top` — транзитивный вес и «urgent» без
    полного контекста были бы недосчитаны."""
    number = issue["number"]
    own_broken = sorted(
        label.get("name") for label in (issue.get("labels") or [])
        if label.get("name") in broken_labels
    )
    urgent = _urgent_numbers(pool, broken_labels)
    transitive = task_deps.transitive_blocking_counts(pool)
    weight = transitive.get(number, 0)
    blocked_by = issue.get("blocked_by_open") or []

    if own_broken:
        tier_text = f"тир 0 — сама несёт метку «сейчас красный»: {', '.join(own_broken)}"
    elif number in urgent:
        # Не просто факт «да, urgent» — конкретные номера «сейчас красных»
        # задач среди транзитивно блокируемых этой (пересечение множеств).
        broken_numbers = {i["number"] for i in pool if _is_broken(i, broken_labels)}
        reached = task_deps.transitive_blocked(pool, number) & broken_numbers
        reached_text = ", ".join(f"#{n}" for n in sorted(reached)) or "не найдено (внутренняя нестыковка)"
        tier_text = f"тир 0 — блокирует (прямо или через цепочку) «сейчас красную» задачу: {reached_text}"
    elif _is_meta(issue, meta_label):
        tier_text = f"тир 1 — метка «{meta_label}» (про сам процесс работы)"
    else:
        tier_text = "тир 2 — не чинит текущий красный CI и не про процесс"

    blocked_by_text = (
        f"сама ждёт {len(blocked_by)} открытых блокирующих: "
        + ", ".join(f"#{n}" for n in blocked_by)
        if blocked_by else "сама ничем не заблокирована"
    )
    return (
        f"#{number}: {tier_text}; транзитивно блокирует {weight} открытых "
        f"задач этого пула; {blocked_by_text}; тай-брейк — номер issue "
        f"(меньше номер при равенстве тира и веса — раньше)."
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
        # graph_is_empty — на ВЕСЬ пул (issues), не только candidates: сигнал
        # «нет ни одного отличающего признака» обязан учитывать граф целиком
        # (цепочка urgent/transitive может идти через занятую/чужую issue,
        # которая сама не кандидат — тот же довод, что у prioritized_free).
        if graph_is_empty(issues):
            print(
                "free_task.py: граф блокировок пуст, ни одна свободная задача "
                "не помечена area:process и никто не чинит текущий красный CI "
                "(ci-failure/self-audit) — приоритет сведён к дате создания "
                "(#361/#224)", file=sys.stderr,
            )
        _print_issue_line(candidates[0])
        return 0
    if len(argv) == 3 and argv[0] == "why":
        # Критерий 1 задачи #224: объяснимость — печатает ФАКТ, не гадание.
        try:
            number = int(argv[1])
        except ValueError:
            print(f"free_task.py: why: N обязан быть числом: {argv[1]!r}", file=sys.stderr)
            return 2
        pool = _load_json(Path(argv[2]))
        target = next((i for i in pool if i.get("number") == number), None)
        if target is None:
            print(f"free_task.py: why: #{number} не найдена в переданном пуле", file=sys.stderr)
            return 1
        print(priority_reason(target, pool))
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
    if len(argv) == 1 and argv[0] == "meta-label":
        # Одно место правды на литерал META_LABEL для bash-обёртки
        # (scripts/gh/issue-create, #695) — не вторая копия строки.
        print(META_LABEL)
        return 0
    if len(argv) in (2, 3) and argv[0] == "priority-top":
        # Верх группы приоритета ПЕРЕД заведением новой задачи (#695): сеть —
        # task_deps.fetch_pool (тот же путь, что task.sh), сортировка — тот
        # же issue_priority_key, что prioritized_free. Информационная печать,
        # не гейт — сбой сети предупреждает в stderr и возвращает 0 (как
        # duplicate_guard.py check: недоступность инструмента не блокирует
        # реальную работу агента).
        #
        # Тестовый шов: PRIORITY_TOP_FIXTURE=<путь> подменяет сетевой вызов
        # чтением готового JSON пула (форма fetch_pool: number/title/labels/
        # blocking_open/...) — тот же приём, что DUPLICATE_GUARD_FIXTURE в
        # duplicate_guard.py, по той же причине (bash-обёртка зовёт этот CLI
        # отдельным процессом python3, monkeypatch недоступен физически).
        repo = argv[1]
        try:
            top_n = int(argv[2]) if len(argv) == 3 else 15
        except ValueError:
            print(f"free_task.py: priority-top: N обязан быть числом: {argv[2]!r}", file=sys.stderr)
            return 2
        fixture = os.environ.get("PRIORITY_TOP_FIXTURE")
        if fixture:
            issues = _load_json(Path(fixture))
        else:
            try:
                issues = task_deps.fetch_pool(repo, META_LABEL)
            except Exception as exc:  # noqa: BLE001 — диагностика, не повод блокировать
                print(
                    f"free_task.py: priority-top: не смог получить пул {META_LABEL} "
                    f"({exc}) — верх приоритета не показан.", file=sys.stderr,
                )
                return 0
        for candidate in priority_top(issues, top_n):
            _print_issue_line(candidate)
        return 0
    print(
        "использование: free_task.py oldest-free <issues.json> [<locked>] [<excluded>] "
        "| declared-pr <N> <prs.json> | conflict-tasks <prs.json> "
        "| meta-label | priority-top <owner/repo> [N] | why <N> <issues.json>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
