#!/usr/bin/env python3
"""Инварианты состояния репозитория (#244): «такого состояния быть не должно».

Мета-течь, которую закрывает этот файл: за 2026-09-02/03 в конвейере нашли
~15 дефектов, и НИ ОДИН не нашла система — все нашли люди/агенты, читая логи.
Два уже существующих механизма ловят СВОЙ узкий класс отлично (белый спот без
task — repo-ci.yml, #179; реестр меток — scripts/lib/test_label_registry.py,
#207) и ничего больше — потому что каждый инвариант писался отдельно, без
общего дома. Этот файл — общий дом для проверок вида «такого состояния
репозитория быть не должно», по образцу scripts/orchestra/pulse_guard.py
(чистые decide_* функции + тонкая IO-обвязка на gh()), а не третий
параллельный механизм.

Каждая check_* функция принимает уже загруженные данные (без сети — тестируется
на прод-форме фикстур, доказывается мутацией) и возвращает список нарушений
(list[dict]), пустой список — здоровое состояние. IO ниже собирает данные через
gh() (общий с pulse_guard/scheduler, тот же субпроцесс-контракт) и печатает
отчёт; мутирующие вызовы (escalate) — только когда violations непусты.

Пять инвариантов первой волны, из них 2 выведен из состава после ревью
(см. блок-комментарий у бывшего check_free_task_count_mismatch):
  1. check_reopened_after_merge — открытая задача task без исполнителя,
     чей PR уже слит: воркер/scheduler.dispatch_worker выберут её снова
     (класс #18/#21/#78 при слитых PR #138/#177/#163).
  2. (retired) check_free_task_count_mismatch сравнивал «истинное» число
     свободных задач с тем, что вернёт scripts/worker/task.sh::free_task()
     через подстрочный scan("..."). #247 заменил scan() на
     scripts/lib/free_task.py::free_candidates — тот же критерий, что и
     метод A, сравнивать стало не с чем (тавтология), класс закрыт.
  3. check_stuck_review_gate — review:ok стоит дольше порога без НИКАКОГО
     ai:*-вердикта (класс #147, сутки простоя). Порог — существующее место
     правды pulse_guard.UNHEALTHY_PR_AFTER_MINUTES, своего числа не заводим.
  4. check_unarchived_complete_changes — openspec/changes/<id>/tasks.md
     полностью отмечен, а каталог не в openspec/changes/archive/.
  5. check_duplicate_evidence — два открытых task-issue ссылаются в теле на
     один и тот же file:line (класс #202/#213/#212). Честный потолок ниже.
  6. check_branch_protection_drift (#341) — enforce_admins/required_status_
     checks.strict/.contexts/allow_force_pushes/allow_deletions защиты
     main разошлись с EXPECTED_* (класс: admin-токен сливал мимо всех
     проверок, пока enforce_admins стоял в false и это нигде не
     проверялось). НЕ входит в стандартный отчёт build_report() и в
     CI_GATING: `GET .../branches/main/protection` требует токен с правом
     `administration`, которого у GITHUB_TOKEN нет структурно (не входит в
     перечисляемый набор scope Actions) — включается только вручную
     (`--check-branch-protection`, admin-токен владельца), см. docstring
     build_report.

Расписание: главный канал — периодический шаг orchestra.yml (cron */15 мин),
он же вызывает escalate() для инвариантов 1 и 3 (см. docstring escalate_*).
Дополнительно repo-ci.yml печатает тот же отчёт на каждый push/PR (видимость
раньше следующего пульса), но НЕ проваливает обязательную проверку `test`:
пять инвариантов проверяют СОСТОЯНИЕ РЕПОЗИТОРИЯ (issues/PR/openspec), а не
дифф текущего PR — обвал состояния, накопленный за месяцы, не вина автора
этого конкретного пуша, и превращать его в требование «почини чужой бэклог,
чтобы слить свой PR» было бы третьим по счёту тормозом без объявленного газа
(AGENTS.md, правило «Тормоз без газа не принимается»). Замер на живом
репозитории 2026-09-03 (см. README PR): инварианты 1/5 уже находят реальный
накопленный долг (17/7 нарушений) — сделать их required-гейтом немедленно
означало бы покрасить main для всех агентов из-за чужого долга.

Решение владельца (PR #249, комментарий 2026-09-03 «Решение по CI_GATING —
принято, чтобы не осталось в чате»): инварианты 3 и 4 — ноль нарушений на
момент замера, включаются в CI_GATING сразу, чтобы не дать долгу появиться
заново. Решение остаётся в силе по существу — но фактически включён только
4. Инвариант 3 сужен обратно до наблюдательного уже ПОСЛЕ включения: у его
газа (trigger_ai_review, scheduler.py) обнаружилась зависимость от такта
оркестратора, а такт ненадёжен (#269, см. подробности у CI_GATING ниже) —
это внешняя находка, не пересмотр решения владельца. Условие обратного
включения 3 — закрытие #269. Инвариант 2 выведен из состава после ревью PR
#249 (#247 закрыл сам класс substring-scan, сравнивать стало не с чем —
см. блок-комментарий на месте бывшего check_free_task_count_mismatch).
Инварианты 1 и 5 остаются наблюдательными по исходному решению, пока не
разгребён долг: 1 обнулит стадия приёмки, 5 — дедупликация по отпечатку
(#201/#243). Каждый включается отдельным шагом, привязанным к обнулению его
счётчика (или, для 3 — к закрытию #269), правкой CI_GATING ниже — не
разовым решением «включим всё позже».

Запуск:
  python scripts/orchestra/repo_invariants.py             # печать отчёта (repo-ci.yml)
  python scripts/orchestra/repo_invariants.py --orchestra  # печать + escalate (orchestra.yml)
"""

import argparse
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# gh()/parse_time()/minutes_between()/escalate()/WATCHDOG_ISSUE — одно место
# правды в pulse_guard (тот же субпроцесс-контракт gh api, тот же канал
# эскалации #120 + Telegram, третий канал не заводим).
_PG_SPEC = importlib.util.spec_from_file_location(
    "pulse_guard", Path(__file__).resolve().parent / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_PG_SPEC)
_PG_SPEC.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

gh = pulse_guard.gh
parse_time = pulse_guard.parse_time
minutes_between = pulse_guard.minutes_between
escalate = pulse_guard.escalate
issue_marker_times = pulse_guard.issue_marker_times
WATCHDOG_ISSUE = pulse_guard.WATCHDOG_ISSUE
# «PR нездоров дольше этого — действуй» — уже объявленный порог для «состояние
# держится слишком долго» (scheduler.py, #196). Инварианты 1 и 3 переиспользуют
# его как порог эскалации, а не заводят своё число.
UNHEALTHY_PR_AFTER_MINUTES = pulse_guard.UNHEALTHY_PR_AFTER_MINUTES

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", REPO_ROOT / "scripts" / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

TASK_LABEL = "task"
OPENSPEC_CHANGES = REPO_ROOT / "openspec" / "changes"

# Инварианты, включённые в обязательную проверку repo-ci.yml `test` — их
# нарушение роняет CI, а не просто печатается в отчёте. Решение владельца
# (PR #249, 2026-09-03) было включить 3 и 4 сразу — оба ноль нарушений на
# момент замера. Инвариант 4 включён: архивация спеки не зависит от
# расписания оркестратора. Инвариант 3 СУЖЕН обратно до наблюдательного —
# не пересмотром решения владельца, а внешней причиной, найденной уже после
# включения (#269): газ инварианта 3 — trigger_ai_review — вызывается только
# из main() оркестратора (scheduler.py:702), а orchestra.yml (cron */15 мин)
# фактически не идёт по расписанию (7 прогонов за сутки вместо 96, интервалы
# до 4.5 часов — замер #269). При таком интервале PR успевает покраснеть по
# инварианту 3 (порог UNHEALTHY_PR_AFTER_MINUTES=120 мин) раньше, чем газ
# доберётся до него — тормоз в руках не того, кто в него упёрся.
#
# Условие обратного включения инварианта 3 — ПРОВЕРЯЕМОЕ, не «когда починим»:
# добавь 3 в CI_GATING после закрытия #269 (расписание orchestra.yml снова
# идёт по кадансу, заявленному в cron, — тогда trigger_ai_review успевает
# сработать раньше UNHEALTHY_PR_AFTER_MINUTES с тем же запасом, что заложен
# числами AI_REVIEW_RETRY_AFTER_MINUTES=30 vs UNHEALTHY_PR_AFTER_MINUTES=120).
# 1, 2, 5 остаются наблюдательными по исходному решению владельца (см.
# docstring выше). Включение любого номера — явная правка этой константы
# после проверки условия.
CI_GATING: frozenset[int] = frozenset({4})

# Единое место правды: что снимает блокировку каждого инварианта из
# CI_GATING (AGENTS.md, «Тормоз без газа не принимается» — сообщение об
# ошибке обязано называть газ, а не просто константировать красный CI).
# Ключи должны покрывать ВСЕ номера в CI_GATING — гвардия ниже (main())
# падает громко, если для включённого инварианта газ не назван.
GATING_RELEASE_CONDITION: dict[int, str] = {
    3: "поставь вердикт ai:* на PR (issue-comment ai-review.yml) — авто-повтор "
       "уже пытается сам (scheduler.trigger_ai_review, #196); если исчерпал "
       "попытки — перезапусти ai-review.yml вручную или сними review:ok",
    4: "перенеси openspec/changes/<id> в openspec/changes/archive/ (создай "
       "каталог, если его ещё нет) — раздел полностью выполнен, самое время "
       "заархивировать",
}


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 1: открытая задача без исполнителя, чей PR уже слит
# ══════════════════════════════════════════════════════════════════════════

# Первая НЕпустая строка тела PR, если это РОВНО `#N` и больше ничего.
# Строже task_ref.declared_tasks (который матчит ЛЮБУЮ строку, начинающуюся
# с `#`, — заголовки `## Что сделано` не страдают, там нет цифр сразу после
# `#`, но живая находка на PR #137 показала другой случай: перенос строки
# внутри абзаца прозы уронил ` #119 из пула...` на новую строку, и
# declared_tasks() принял её за декларацию — задача #119 никогда не
# объявлялась PR #137 первой строкой, реальная первая строка — `#18`. Здесь
# те же слова, что и во всех живых декларациях (#18, #205, #207, #80…) —
# ровно `#N`, ничего больше на строке.
_BARE_TASK_REF_RE = re.compile(r"#(\d+)")


def primary_declared_task(body: str) -> int | None:
    if not body:
        return None
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.fullmatch(_BARE_TASK_REF_RE, stripped)
        return int(match.group(1)) if match else None
    return None


def check_reopened_after_merge(open_tasks: list[dict], merged_pulls: list[dict]) -> list[dict]:
    """Открытая задача task без assignee, для которой уже есть слитый PR,
    декларировавший её первой строкой. Механизм инцидента: reap_stale
    (scheduler.py) смотрит только ОТКРЫТЫЕ PR — если PR уже слит, «нет
    открытого PR, ссылающегося на задачу» читается как «работа брошена», и
    assignee снимается ДАЖЕ когда работа честно завершена, просто исполнитель
    ещё не сделал пост-мерж проверку и не закрыл issue. Свободная задача с
    уже слитым PR — именно то состояние, в котором free_task()/
    dispatch_worker выберут её заново (класс #18/#21/#78)."""
    unassigned = {t["number"]: t for t in open_tasks if not t["assignees"]}
    by_task: dict[int, list[dict]] = {}
    for pull in merged_pulls:
        declared = primary_declared_task(pull.get("body") or "")
        if declared is not None:
            by_task.setdefault(declared, []).append(pull)

    violations = []
    for number, pulls in sorted(by_task.items()):
        if number not in unassigned:
            continue
        newest = max(pulls, key=lambda p: p["merged_at"])
        violations.append({
            "issue": number,
            "title": unassigned[number]["title"],
            "prs": sorted(p["number"] for p in pulls),
            "merged_at": newest["merged_at"],
        })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 2 — ВЫВЕДЕН ИЗ СОСТАВА (находка AI-ревью PR #249, 4 независимых
# раунда). Первоначальный вариант сравнивал «открытые task-issue без
# assignee» с тем, что вернёт scripts/worker/task.sh::free_task(), решавший
# «занята» подстрочным scan("#[0-9]+") по телам ЧУЖИХ PR — класс, из-за
# которого воркеру было доступно 50 из 66 свободных задач (замер 2026-09-03).
# #247 (free-task-declared-scope) заменил этот scan() на
# scripts/lib/free_task.py::free_candidates — тот же критерий, что и метод A
# (issue.assignees пуст), плюс фильтр по живым замкам аренды (#121). Извлекать
# паттерн scan(...) из task.sh стало нечего (RuntimeError на каждом прогоне —
# инвариант вечно печатал «не удалось проверить», найдено ревью), а
# реализовать метод B через free_candidates() означало бы сравнивать функцию
# саму с собой — не независимая проверка, а тавтология. Класс, который
# инвариант ловил, закрыт #247; воскрешать его есть смысл только если
# появится НОВЫЙ независимый способ занять задачу без ведома free_task.py.
# ══════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 3: review:ok без вердикта ai:* дольше порога
# ══════════════════════════════════════════════════════════════════════════


def last_review_ok_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Момент последней простановки review:ok — весь таймлайн через
    review_labels.list_timeline, не сырая первая страница `per_page=100`
    (находка AI-ревью PR #249: у долгоживущего PR, который сам же разгоняют
    авто-повторы #196, событие `labeled` уезжает за первую сотню — сырой
    вызов возвращал None и застрявший гейт молча пропускался, ровно тот
    класс, который #303 уже закрыл для scheduler.last_review_ok_labeled_at
    той же функцией; копия здесь была рассинхронизирована с исправлением)."""
    timeline = review_labels.list_timeline(repo, pr_number, gh)
    labeled_at = [
        event["created_at"] for event in timeline
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") == review_labels.REVIEW_OK
    ]
    return parse_time(max(labeled_at)) if labeled_at else None


def check_stuck_review_gate(repo: str, now: datetime, open_pulls: list[dict]) -> list[dict]:
    """review:ok стоит дольше UNHEALTHY_PR_AFTER_MINUTES, и НИ ОДНОЙ ai:*
    метки ещё нет. scheduler.trigger_ai_review (#196) уже пытается сам
    перезапустить ai-review.yml на меньшем пороге
    (AI_REVIEW_RETRY_AFTER_MINUTES) с ограничением попыток
    (AI_REVIEW_MAX_ATTEMPTS) — этот инвариант ловит случай, когда газ #196
    исчерпал попытки, не сработал вовсе (оркестратор не бежал) или ещё не
    было задеплоено — застрявший гейт, класс #147 (сутки простоя)."""
    ai_labels = set(review_labels.AI_VERDICTS)
    violations = []
    for pull in open_pulls:
        labels = {label["name"] for label in pull["labels"]}
        if review_labels.REVIEW_OK not in labels or labels & ai_labels:
            continue
        labeled_at = last_review_ok_labeled_at(repo, pull["number"])
        if labeled_at is None:
            continue
        age = minutes_between(labeled_at, now)
        if age <= UNHEALTHY_PR_AFTER_MINUTES:
            continue
        violations.append({
            "pr": pull["number"],
            "age_minutes": round(age, 1),
            "labeled_at": labeled_at.isoformat(),
        })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 4: openspec/changes/<id> полностью отмечен и не заархивирован
# ══════════════════════════════════════════════════════════════════════════

_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]", re.MULTILINE)


def check_unarchived_complete_changes(changes_dir: Path) -> list[dict]:
    """Каталог openspec/changes/<id>/tasks.md, где ВСЕ чекбоксы отмечены
    (и их хотя бы один), а сам каталог лежит не под openspec/changes/archive/
    (её пока не существует вовсе — задокументированный факт задачи #244)."""
    if not changes_dir.is_dir():
        return []
    violations = []
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        tasks_md = entry / "tasks.md"
        if not tasks_md.exists():
            continue
        boxes = _CHECKBOX_RE.findall(tasks_md.read_text(encoding="utf-8"))
        if boxes and all(box.lower() == "x" for box in boxes):
            violations.append({"change": entry.name, "checked": len(boxes)})
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 5: два открытых task-issue ссылаются на один и тот же file:line
# ══════════════════════════════════════════════════════════════════════════

# Честный потолок (в стиле docs/agents/LABELS.md): сигнал берётся по ФОРМЕ
# ссылки в обратных кавычках — `path/to/file.ext:NNN` или `...:NNN-MMM`, а не
# по тексту заголовка раздела. Живые данные используют вперемешку «Факты»,
# «Факт», «Где», «Где именно (file:line)» или вовсе без заголовка (#222) —
# заголовок не единое место, поэтому матчинг по нему был бы хрупким по
# конструкции. Расширения ограничены исходным кодом (py/sh/ts/tsx/js/mjs/
# yml/yaml) — документация (.md) сознательно исключена: замер на живом
# репозитории показал ложную склейку #244/#201 и #219/#215 через `AGENTS.md`
# и через символ `escalate` — оба цитируют общее место (канонический раздел
# правил, инфраструктурную функцию) как ПОДДЕРЖИВАЮЩИЙ контекст, а не как
# адрес бага. Символьные ссылки (`file.py::func`, `` `file.py` — `func()` ``)
# рассматривались и отклонены по той же причине: `pulse_guard.py::escalate`
# оказался ложным совпадением между #244 и #201 при том же замере — точное
# совпадение file:line в КОДЕ достаточно редкое совпадение, чтобы быть
# сигналом; совпадение имени часто используемой функции — нет. Следствие:
# пара #222/#149 (символьные, не числовые ссылки) этим инвариантом не
# ловится — дисциплина автора/ревьюера, не механическая проверка.
_CODE_EXTENSIONS = r"(?:py|sh|ts|tsx|js|mjs|yml|yaml)"
_LOCATOR_RE = re.compile(rf"`([\w./-]+\.{_CODE_EXTENSIONS}):(\d+)(?:-(\d+))?`")


def extract_locators(body: str) -> set[tuple[str, int, int]]:
    if not body:
        return set()
    locators = set()
    for line in body.splitlines():
        for match in _LOCATOR_RE.finditer(line):
            file_, start, end = match.group(1), int(match.group(2)), match.group(3)
            locators.add((file_, start, int(end) if end else start))
    return locators


def _locators_overlap(a: tuple[str, int, int], b: tuple[str, int, int]) -> bool:
    file_a, start_a, end_a = a
    file_b, start_b, end_b = b
    return file_a == file_b and start_a <= end_b and start_b <= end_a


def check_duplicate_evidence(open_tasks: list[dict]) -> list[dict]:
    """Два открытых task-issue, чьи тела ссылаются на пересекающийся диапазон
    строк одного файла (класс #202/#213/#212: contract_check.py разбирался
    трижды под разными номерами, каждый раз заново)."""
    per_issue = [
        (t["number"], t["title"], extract_locators(t.get("body") or ""))
        for t in open_tasks
    ]
    violations = []
    for i in range(len(per_issue)):
        num_a, title_a, locs_a = per_issue[i]
        if not locs_a:
            continue
        for j in range(i + 1, len(per_issue)):
            num_b, title_b, locs_b = per_issue[j]
            if not locs_b:
                continue
            shared = next(
                (
                    (a, b) for a in locs_a for b in locs_b
                    if _locators_overlap(a, b)
                ),
                None,
            )
            if shared is None:
                continue
            violations.append({
                "issues": [num_a, num_b],
                "titles": [title_a, title_b],
                "shared_location": f"{shared[0][0]}:{shared[0][1]}-{shared[0][2]}",
            })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 6: настройки защиты main-ветки не откатились молча (#341)
# ══════════════════════════════════════════════════════════════════════════
#
# Замер аудита #341 (2026-09-06): admin-токен сливал мимо всех проверок до
# 2026-09-05, потому что enforce_admins стоял в false и это нигде не
# проверялось — `grep -rn protection scripts/` на тот момент был пуст, ни
# одна из настроек защиты ветки не читалась никаким механизмом репозитория.
# Найдено ЖИВОЙ мутацией: PR со сломанным `contract` слился (HTTP 405 при
# попытке повторить проверку задним числом подтвердил, что она была
# пропущена), а не догадкой.
#
# Единственное место правды на ожидаемое состояние — EXPECTED_* ниже. Любая
# правка настроек защиты ветки обязана сопровождаться правкой этих констант
# в ТОМ ЖЕ PR — иначе следующий прогон немедленно закричит о расхождении,
# что и есть цель инварианта (не «настройки правильные раз и навсегда», а
# «расхождение видно на первом же прогоне после отката»).
EXPECTED_ENFORCE_ADMINS = True
EXPECTED_STATUS_CHECKS_STRICT = True
# «test»/«contract» — обязательные проверки на момент задачи #341.
# «harness/review»/«harness/ai-review» (Commit Status API, #345) добавляются
# сюда ТОЙ ЖЕ правкой, что добавляет их в required_status_checks.contexts на
# GitHub — см. openspec/changes/verdict-commit-status/tasks.md, «владелец
# включает контексты» (последовательность обязательна: статусы существуют на
# живом PR раньше, чем становятся обязательными — иначе вся очередь открытых
# PR молча зависает в «Expected»).
EXPECTED_STATUS_CHECK_CONTEXTS = frozenset({"test", "contract"})
EXPECTED_ALLOW_FORCE_PUSHES = False
EXPECTED_ALLOW_DELETIONS = False

# Что именно ломается при каждом конкретном расхождении (AGENTS.md: «Инвариант
# обязан называть, что именно сломается при расхождении, а не просто
# constatировать несовпадение») — печатается вместе с violation, не только
# «ожидалось X, получено Y».
_BRANCH_PROTECTION_CONSEQUENCE = {
    "enforce_admins": "admin-токен (в т.ч. учётка владельца) сможет сливать в main "
                       "мимо обязательных проверок — класс #341, уже приводил к "
                       "слиянию PR с красным contract",
    "required_status_checks.strict": "PR сливается без пересборки проверок на "
                                      "актуальном main — зелёный чек на устаревшей "
                                      "базе не значит зелёный чек на итоговом коде",
    "required_status_checks.contexts": "обязательная проверка выпала из гейта — PR "
                                        "с красным этим контекстом сможет слиться "
                                        "(или наоборот, новый контекст стал "
                                        "обязательным без подтверждения живым "
                                        "прогоном и вся очередь виснет в «Expected»)",
    "allow_force_pushes": "историю main можно переписать force-push — слитые "
                           "коммиты и их проверки становятся заменяемыми задним числом",
    "allow_deletions": "ветку main можно удалить целиком",
}


def check_branch_protection_drift(protection: dict) -> list[dict]:
    """Инвариант 6: `enforce_admins`/`required_status_checks.strict`/
    `.contexts`/`allow_force_pushes`/`allow_deletions` защиты ветки main не
    откатились молча относительно EXPECTED_* выше. `protection` — прод-форма
    `GET /repos/{repo}/branches/main/protection` целиком (см.
    fetch_branch_protection): большинство булевых настроек лежат как
    `{"enabled": bool}` — расхождение с задачей #341 в том, что до этой
    правки НИ ОДНА из них не читалась вообще ни одним механизмом репозитория
    (`grep -rn protection scripts/` был пуст)."""
    violations = []

    enforce_admins = (protection.get("enforce_admins") or {}).get("enabled")
    if enforce_admins is not EXPECTED_ENFORCE_ADMINS:
        violations.append({
            "setting": "enforce_admins",
            "expected": EXPECTED_ENFORCE_ADMINS,
            "actual": enforce_admins,
            "consequence": _BRANCH_PROTECTION_CONSEQUENCE["enforce_admins"],
        })

    rsc = protection.get("required_status_checks") or {}
    strict = rsc.get("strict")
    if strict is not EXPECTED_STATUS_CHECKS_STRICT:
        violations.append({
            "setting": "required_status_checks.strict",
            "expected": EXPECTED_STATUS_CHECKS_STRICT,
            "actual": strict,
            "consequence": _BRANCH_PROTECTION_CONSEQUENCE["required_status_checks.strict"],
        })

    contexts = frozenset(rsc.get("contexts") or [])
    if contexts != EXPECTED_STATUS_CHECK_CONTEXTS:
        violations.append({
            "setting": "required_status_checks.contexts",
            "expected": sorted(EXPECTED_STATUS_CHECK_CONTEXTS),
            "actual": sorted(contexts),
            "consequence": _BRANCH_PROTECTION_CONSEQUENCE["required_status_checks.contexts"],
        })

    allow_force_pushes = (protection.get("allow_force_pushes") or {}).get("enabled")
    if allow_force_pushes is not EXPECTED_ALLOW_FORCE_PUSHES:
        violations.append({
            "setting": "allow_force_pushes",
            "expected": EXPECTED_ALLOW_FORCE_PUSHES,
            "actual": allow_force_pushes,
            "consequence": _BRANCH_PROTECTION_CONSEQUENCE["allow_force_pushes"],
        })

    allow_deletions = (protection.get("allow_deletions") or {}).get("enabled")
    if allow_deletions is not EXPECTED_ALLOW_DELETIONS:
        violations.append({
            "setting": "allow_deletions",
            "expected": EXPECTED_ALLOW_DELETIONS,
            "actual": allow_deletions,
            "consequence": _BRANCH_PROTECTION_CONSEQUENCE["allow_deletions"],
        })

    return violations


# ══════════════════════════════════════════════════════════════════════════
# IO: сбор данных, отчёт, эскалация
# ══════════════════════════════════════════════════════════════════════════


def fetch_open_task_issues(repo: str) -> list[dict]:
    """Постранично (review_labels.list_pages, класс #308) — сырой одностраничный
    вызов молча терял бы задачи за первой сотней открытых issues с меткой task
    (находка гвардии scripts/lib/test_pagination_guard.py на этом же PR)."""
    payload = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100", gh)
    return [issue for issue in payload if "pull_request" not in issue]


def fetch_merged_pulls(repo: str, max_pages: int = 5) -> list[dict]:
    """Слитые PR — REST отдаёт state=closed вперемешку с незлитыми, отбираем
    по merged_at. max_pages=5 (500 PR) — с запасом покрывает весь репозиторий
    на момент задачи #244 (98 слитых всего); если PR станет больше, инвариант
    честно недосмотрит самые старые, а не упадёт — тот же компромисс, что уже
    принят в scheduler.py (per_page=100 без пагинации совсем)."""
    results = []
    for page in range(1, max_pages + 1):
        batch = gh(
            f"repos/{repo}/pulls?state=closed&per_page=100&page={page}"
            "&sort=updated&direction=desc"
        )
        if not batch:
            break
        results.extend(pull for pull in batch if pull.get("merged_at"))
        if len(batch) < 100:
            break
    return results


def fetch_open_pulls(repo: str) -> list[dict]:
    """Постранично (review_labels.list_pages, класс #308) — тот же приём, что
    fetch_open_task_issues выше."""
    return review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)


def fetch_branch_protection(repo: str) -> dict:
    """`GET /repos/{repo}/branches/main/protection` целиком — один запрос,
    не listing, страница не нужна (см. check_branch_protection_drift, #341)."""
    return gh(f"repos/{repo}/branches/main/protection")


def build_report(repo: str, now: datetime,
                  check_branch_protection: bool = False) -> tuple[list[str], dict[int, list]]:
    """Возвращает (строки отчёта, {номер_инварианта: violations}). Чистых
    мутирующих вызовов здесь нет — только GET (см. gh()); гвардия холостого
    хода проверяет именно это.

    check_branch_protection — инвариант 6 (#341) выключен по умолчанию:
    `GET /branches/main/protection` требует у токена право `administration`
    (документировано самим GitHub на этом эндпоинте), а у GITHUB_TOKEN такого
    права нет вовсе — оно не входит в перечисляемый набор scope'ов Actions
    (`actions/checks/contents/deployments/discussions/id-token/issues/
    packages/pages/pull-requests/repository-projects/security-events/
    statuses`, без `administration`), и никакая правка `permissions:` в
    workflow этого не меняет — это ограничение самого GITHUB_TOKEN, не
    настройка репозитория. Включать этот инвариант в repo-ci.yml/orchestra.yml
    с GITHUB_TOKEN означало бы падать 403 на КАЖДОМ прогоне и по построению,
    что хуже отсутствия проверки. Инвариант вызывается вручную (main()
    `--check-branch-protection`) владельцем/агентом с токеном, у которого
    реально есть admin-права на репозиторий (например, `gh auth token`
    личной учётки) — см. docs/research/21-github-actions.md."""
    open_tasks = fetch_open_task_issues(repo)
    merged_pulls = fetch_merged_pulls(repo)
    open_pulls = fetch_open_pulls(repo)

    findings: dict[int, list] = {}
    lines = ["## Инварианты состояния репозитория (#244)"]

    v1 = check_reopened_after_merge(open_tasks, merged_pulls)
    findings[1] = v1
    if v1:
        lines.append(f"🚨 [1] {len(v1)} открытых задач без исполнителя с уже слитым PR:")
        for item in v1:
            lines.append(
                f"   — #{item['issue']} «{item['title']}» — слит PR"
                f" {', '.join('#' + str(n) for n in item['prs'])} ({item['merged_at']})"
            )
    else:
        lines.append("💚 [1] нет открытых задач с уже слитым PR")

    findings[2] = []  # выведен из состава — см. блок-комментарий выше, класс закрыт #247

    v3 = check_stuck_review_gate(repo, now, open_pulls)
    findings[3] = v3
    if v3:
        lines.append(f"🚨 [3] {len(v3)} открытых PR с review:ok без вердикта ai:* дольше {UNHEALTHY_PR_AFTER_MINUTES} мин:")
        for item in v3:
            lines.append(f"   — PR #{item['pr']} — {int(item['age_minutes'])} мин без ai:*")
    else:
        lines.append("💚 [3] нет застрявших review:ok без ai:*")

    v4 = check_unarchived_complete_changes(OPENSPEC_CHANGES)
    findings[4] = v4
    if v4:
        lines.append(f"🚨 [4] {len(v4)} openspec/changes полностью отмечены и не заархивированы:")
        for item in v4:
            lines.append(f"   — openspec/changes/{item['change']} ({item['checked']} чекбоксов)")
    else:
        lines.append("💚 [4] нет полностью отмеченных незаархивированных change")

    v5 = check_duplicate_evidence(open_tasks)
    findings[5] = v5
    if v5:
        lines.append(f"🚨 [5] {len(v5)} пар открытых задач с пересекающейся уликой file:line:")
        for item in v5:
            a, b = item["issues"]
            lines.append(f"   — #{a} и #{b} — {item['shared_location']}")
    else:
        lines.append("💚 [5] нет открытых задач с пересекающейся уликой file:line")

    if check_branch_protection:
        protection = fetch_branch_protection(repo)
        v6 = check_branch_protection_drift(protection)
        findings[6] = v6
        if v6:
            lines.append(f"🚨 [6] {len(v6)} настроек защиты main разошлись с ожидаемыми:")
            for item in v6:
                lines.append(
                    f"   — {item['setting']}: ожидалось {item['expected']!r}, "
                    f"сейчас {item['actual']!r} — {item['consequence']}"
                )
        else:
            lines.append("💚 [6] защита main совпадает с ожидаемой (enforce_admins/strict/"
                          "contexts/force-push/deletions)")
    else:
        lines.append("⏭️ [6] защита main не проверена в этом прогоне (нужен токен с "
                      "правом administration — GITHUB_TOKEN его структурно не имеет; "
                      "запусти вручную: --check-branch-protection с admin-токеном)")

    return lines, findings


def summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


ESCALATING_INVARIANTS = (1, 3)


def escalate_if_new(repo: str, invariant_id: int, marker_key: str, text: str) -> str | None:
    """Эскалация «один раз на состояние»: маркер кодирует конкретный набор
    нарушителей (не просто факт «инвариант N нарушен») — тот же приём, что
    pulse_guard.PAUSE_MARKER/HEARTBEAT_MARKER (issue_marker_times ищет
    подстроку). Набор изменился (новый нарушитель, старый пропал) — новый
    маркер, новая эскалация; тот же набор — тишина, конвейер не спамит
    Telegram каждые 15 минут одним и тем же списком."""
    marker = f"[инвариант {invariant_id}: {marker_key}]"
    try:
        if issue_marker_times(repo, WATCHDOG_ISSUE, marker):
            return None  # уже эскалировано именно это состояние
    except RuntimeError as error:
        print(f"::warning::не удалось прочитать маркеры #{WATCHDOG_ISSUE}: {error}", file=sys.stderr)
        return None
    return escalate(repo, WATCHDOG_ISSUE, f"{marker}\n{text}")


def run_escalations(repo: str, findings: dict[int, list]) -> list[str]:
    lines = []
    if findings.get(1):
        v1 = findings[1]
        key = ",".join(f"#{i['issue']}" for i in v1)
        text = (
            "🚨 edge-harness: инвариант 1 (задача открыта, PR уже слит) — "
            f"{len(v1)} задач без исполнителя с уже слитым PR: " + key + ". "
            "Воркер/диспетч может выбрать их снова (класс #18/#21/#78) — "
            "закрой после пост-мерж проверки или переназначь."
        )
        result = escalate_if_new(repo, 1, key, text)
        if result:
            lines.append(f"📣 инвариант 1 эскалирован: {result}")
    if findings.get(3):
        v3 = findings[3]
        key = ",".join(f"#{i['pr']}" for i in v3)
        text = (
            "🚨 edge-harness: инвариант 3 (застрявший гейт) — "
            f"{len(v3)} PR с review:ok без вердикта ai:* дольше "
            f"{UNHEALTHY_PR_AFTER_MINUTES} мин: " + key + ". "
            "Авто-повтор #196 либо исчерпал попытки, либо не сработал — нужен человек."
        )
        result = escalate_if_new(repo, 3, key, text)
        if result:
            lines.append(f"📣 инвариант 3 эскалирован: {result}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orchestra", action="store_true",
                         help="периодический режим: report + escalate (инварианты 1 и 3)")
    parser.add_argument(
        "--check-branch-protection", action="store_true",
        help="включить инвариант 6 (защита main-ветки, #341) — требует токен "
             "с правом administration (GITHUB_TOKEN его структурно не имеет, "
             "запускать вручную с admin-токеном владельца, не из CI)")
    args = parser.parse_args()

    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines, findings = build_report(repo, now,
                                    check_branch_protection=args.check_branch_protection)

    if args.orchestra:
        lines += run_escalations(repo, findings)

    summary(lines)

    missing_gas = sorted(CI_GATING - GATING_RELEASE_CONDITION.keys())
    if missing_gas:
        # Тормоз без газа не принимается (AGENTS.md) — падаем громко ДО того,
        # как инвариант без объявленного условия снятия покрасит main.
        raise RuntimeError(
            f"CI_GATING содержит {missing_gas}, но GATING_RELEASE_CONDITION "
            "не называет для них условие снятия — допиши газ, прежде чем гейтить"
        )

    gating_violations = [n for n in CI_GATING if findings.get(n)]
    if gating_violations and not args.orchestra:
        for number in gating_violations:
            print(
                f"::error::инвариант {number} нарушен и включён в CI_GATING "
                f"({len(findings[number])} нарушений) — снимается: "
                f"{GATING_RELEASE_CONDITION[number]}"
            )
        return 1

    # Инвариант 6 не в CI_GATING (не может быть — GITHUB_TOKEN без
    # administration), но ручной запуск с --check-branch-protection обязан
    # быть fail loud сам по себе: тихое расхождение защиты ветки — ровно та
    # дыра, ради которой инвариант написан.
    if args.check_branch_protection and findings.get(6):
        for item in findings[6]:
            print(f"::error::branch protection: {item['setting']} — {item['consequence']}")
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::repo_invariants: {error}")
        sys.exit(1)
