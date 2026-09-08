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
  3. check_stuck_review_gate — гейт 1 (review:ok/review:large) отработал
     дольше порога без НИКАКОГО ai:*-вердикта (класс #147, сутки простоя;
     #432 — гейт 1 считается отработавшим и по review:large, не только по
     review:ok). Порог — существующее место
     правды pulse_guard.UNHEALTHY_PR_AFTER_MINUTES, своего числа не заводим.
     #472 (живой алерт 2026-09-06 «либо исчерпал, либо не сработал» на
     PR #387/#329/#327): каждое нарушение несёт ФАКТ по бюджету
     авто-повтора #196 (сколько попыток из лимита израсходовано — та же
     метрика, что видит scheduler.trigger_ai_review — и сколько из них в
     ТЕКУЩЕЙ эпохе гейта 1, отдельно от перенесённых из старой, уже решённой
     эпохи, класс #431/PR #439 не слит) и был ли вердикт ai:* хоть раз за
     всю жизнь PR — не список гипотез.
  4. check_unarchived_complete_changes — каталог openspec/changes/<id> не
     заархивирован, хотя завершён. Завершённость — ЛЮБОЕ из двух независимых
     условий (протокол: docs/agents/OPENSPEC-PROTOCOL.md): (a) быстрый путь
     — tasks.md существует и в нём отмечен каждый чекбокс (нужен хотя бы
     один); (b) второй путь, для 27 каталогов без tasks.md (созданы до того,
     как файл стал обязателен, задним числом не дописываются) — proposal.md
     декларирует задачу («Задача:»/«Задачи:» первым абзацем), эта задача
     закрыта с state_reason=completed, и ни один открытый PR не ссылается на
     путь этого change. Путь (b) НЕ применяется, если tasks.md вообще
     существует (даже с незакрытыми чекбоксами) — иначе живая работа с
     недоделанным чеклистом (например dsh-edge-plugin-system, 7 из 44
     чекбоксов не отмечены) ложно проходила бы как готовая, потому что её
     эпик-issue уже закрыт completed. Замер на живом репозитории 2026-09-07
     (см. git-историю добавления пути (b)): 23 каталога без tasks.md
     удовлетворяют пути (b) — реальный backlog, поэтому CI_GATING инвариант 4
     временно наблюдательный (см. блок CI_GATING ниже), не гейтящий.
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
  7. check_ambiguous_artifact_phrase (#219) — ни один *.md репозитория не
     содержит двусмысленной формулы принадлежности плагина (читалась двумя
     способами — «артефакт уже есть у владельца» против «наш, пишем мы»;
     цена: готовая ротация учёток пять суток лежала неподключённой, #215).
     Включён в CI_GATING сразу при создании: на момент включения ноль
     нарушений — фраза вычищена тем же PR, что и правило (#219). Честная
     граница: общий класс «утверждение о готовом артефакте без адреса»
     статически не выразим и этой гвардией НЕ покрыт — правило держится на
     ревью (AGENTS.md), инвариант закрывает только саму формулу.
  9. check_stalled_review_without_signal (#637) — PR прошёл гейт 1, вердикта
     ai:* нет дольше UNHEALTHY_PR_AFTER_MINUTES (то же подмножество, что уже
     нашёл инвариант 3), автоповтор #196 исчерпан В ТЕКУЩЕЙ ЭПОХЕ (новый тик
     оркестратора не поможет), и при этом НИ открытой автозадачи по
     отпечатку `gate:no-ai-verdict` (stall_detector, #201) НЕТ, НИ
     доставленного владельцу сигнала за сегодня (CAP_EXHAUSTED_DELIVERED_
     MARKER, stall_detector, #637) не найдено. Живой замер (прогон
     34194073339, 2026-09-08T06:20:31Z): 5 PR (#726/#721/#618/#453/#328)
     провисели 155–296 мин без вердикта, суточный потолок автозаведения
     исчерпан (5/5) чужими отпечатками — ни задачи, ни доставленного сигнала
     об этих пяти PR не было почти пять часов, и ничто в репозитории этого
     не поймало. Второй, независимый от stall_detector путь того же класса
     (тот же приём, что второй путь завершённости у инварианта 4): не
     разбирает текстовый отчёт пульса и не зависит от того, попал ли
     конкретный симптом в его регэкспы — смотрит прямо на факт (гейт решён,
     вердикта нет, бюджет исчерпан) и на факт присутствия сигнала (задача
     или доставка). В ESCALATING_INVARIANTS — сам доставляет сигнал
     (escalate, тот же канал, что 1/3), если найден.

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

Инвариант 4 СУЖЕН обратно до наблюдательного той же процедурой, что и 3, —
внешней находкой, а не пересмотром решения владельца о «4 гейтится сразу».
Второй путь завершённости (proposal.md + состояние задачи + отсутствие
открытого PR на путь change, см. docstring check_unarchived_complete_changes
выше) нашёл на живом репозитории 23 каталога без tasks.md, которые ему
удовлетворяют, — оставаясь в CI_GATING немедленно, инвариант 4 покрасил бы
обязательную проверку `test` на каждом из ~30 открытых PR разом (тот же
третий-по-счёту-тормоз-без-газа, от которого уже отказались для 1/5 выше).
Условие обратного включения — то же самое, чем инвариант 4 гасится в
GATING_RELEASE_CONDITION: разобрать backlog архивацией отдельными PR (план —
docs/agents/OPENSPEC-PROTOCOL.md, раздел про массовую архивацию), затем
вернуть 4 в CI_GATING этой же правкой константы.

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
# Бюджет авто-повтора #196 — те же константы, что читает
# scheduler.trigger_ai_review/ai_review_retry_count, не второе число (#472:
# факт по инварианту 3 обязан совпадать с тем, что реально видит газ #196).
AI_REVIEW_MAX_ATTEMPTS = pulse_guard.AI_REVIEW_MAX_ATTEMPTS
AI_REVIEW_RETRY_MARKER = pulse_guard.AI_REVIEW_RETRY_MARKER

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", REPO_ROOT / "scripts" / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

# Номер задачи PR — одно место правды, `task_ref.resolve_pr_task` (#394,
# решение владельца 2026-09-06: только имя agent-ветки, тело PR не читается
# вовсе). До #394 инвариант 1 держал собственный `primary_declared_task`
# (декларация первой строкой тела, симметрично тогдашнему contract_check.py)
# — после того как контракт перестал читать тело, второе определение того же
# правила здесь стало бы расхождением: PR может быть слит без единого номера
# в теле (шаблон прямо говорит, что «#N» — для человека, не источник истины),
# и инвариант 1 снова ослеп бы, теперь по новой причине. Резолвим тем же
# вызовом, что и контракт.
_TR_SPEC = importlib.util.spec_from_file_location(
    "task_ref", REPO_ROOT / "scripts" / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_TR_SPEC)
_TR_SPEC.loader.exec_module(task_ref)  # type: ignore[union-attr]

# ACCEPTANCE_PARTIAL_MARKER/ACCEPTANCE_FAIL_MARKER — только константы текста
# маркера приёмки (не вызов её функций, circular import: scheduler.py не
# импортирует repo_invariants, но держим импорт узким по духу остальных
# ленивых importlib выше). Нужны инварианту 1 (#467, см.
# check_reopened_after_merge) — приёмка (accept_merged_tasks) уже ставит
# такой маркер на задачу, когда сама вынесла терминальный вердикт по
# конкретному слитому PR; одно место правды на текст маркера, не вторая копия
# строки здесь.
_SCH_SPEC = importlib.util.spec_from_file_location(
    "scheduler", REPO_ROOT / "scripts" / "orchestra" / "scheduler.py")
scheduler = importlib.util.module_from_spec(_SCH_SPEC)
_SCH_SPEC.loader.exec_module(scheduler)  # type: ignore[union-attr]

# find_open_task/CAP_EXHAUSTED_DELIVERED_MARKER — одно место правды на
# отпечаток «gate:no-ai-verdict» и на маркер подтверждённой доставки потолка
# (#637, инвариант 9): вторая копия строки маркера здесь разошлась бы с
# stall_detector при первой же его правке, тот же класс, что предупреждает
# AGENTS.md про «одно место правды».
_SD_SPEC = importlib.util.spec_from_file_location(
    "stall_detector", REPO_ROOT / "scripts" / "orchestra" / "stall_detector.py")
stall_detector = importlib.util.module_from_spec(_SD_SPEC)
_SD_SPEC.loader.exec_module(stall_detector)  # type: ignore[union-attr]

TASK_LABEL = "task"
OPENSPEC_CHANGES = REPO_ROOT / "openspec" / "changes"

# Инварианты, включённые в обязательную проверку repo-ci.yml `test` — их
# нарушение роняет CI, а не просто печатается в отчёте. Решение владельца
# (PR #249, 2026-09-03) было включить 3 и 4 сразу — оба ноль нарушений на
# момент замера. Инвариант 4 включён: архивация спеки не зависит от
# расписания оркестратора. Инвариант 3 СУЖЕН обратно до наблюдательного —
# не пересмотром решения владельца, а внешней причиной, найденной уже после
# включения (#269): газ инварианта 3 — trigger_ai_review — вызывается только
# из main() оркестратора (`scheduler.py::trigger_ai_review`, вызов из
# `main()`; форма `file::symbol`, не `file:line` — номера строк гниют, класс
# уже закрывался этим переходом), а orchestra.yml (cron */15 мин)
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
#
# Инвариант 4 СУЖЕН обратно до наблюдательного той же процедурой, что и 3 —
# внешней находкой уже ПОСЛЕ решения владельца, не пересмотром самого
# решения (см. докстринг модуля). Второй, независимый путь завершённости
# (docs/agents/OPENSPEC-PROTOCOL.md) нашёл на живом репозитории 2026-09-07
# реальный backlog — 23 каталога без tasks.md, чья задача из proposal.md уже
# закрыта completed и на чей путь не ссылается ни один открытый PR. Оставить
# 4 в CI_GATING немедленно означало бы покрасить обязательную проверку
# `test` разом на ~30 открытых PR из-за чужого долга — тот же класс
# нарушения «Тормоз без газа не принимается», от которого уже отказались
# для 1/5. Условие обратного включения — ПРОВЕРЯЕМОЕ: backlog разобран
# (каталоги перенесены в openspec/changes/archive/ отдельными PR, план —
# OPENSPEC-PROTOCOL.md) до нуля нарушений по обоим путям, тогда 4
# возвращается в CI_GATING той же правкой константы.
#
# 1, 2, 5 остаются наблюдательными по исходному решению владельца (см.
# docstring выше). Включение любого номера — явная правка этой константы
# после проверки условия.
CI_GATING: frozenset[int] = frozenset({7})

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
       "заархивировать (ключ держим не в CI_GATING, см. комментарий выше "
       "константы — как и у 3, это факт про газ, а не про то, гейтит ли "
       "сейчас 4)",
    7: "переформулируй однозначно — «артефакт владельца по адресу X» либо "
       "«наш плагин (пишем мы, не апстрим)»; правило — AGENTS.md, "
       "«Утверждение о готовом артефакте обязано нести его адрес» (#219). "
       "Если формулировка нужна как цитата самого инцидента — расширь паттерн "
       "инварианта осознанно, с комментарием, почему цитата безопаснее",
}


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 1: открытая задача без исполнителя, чей PR уже слит
# ══════════════════════════════════════════════════════════════════════════



def _acceptance_already_verdicted(repo: str, issue_number: int, pr_number: int) -> bool:
    """Приёмка (scheduler.accept_merged_tasks) уже написала терминальный
    вердикт по ИМЕННО ЭТОМУ слитому PR — ACCEPTANCE_PARTIAL_MARKER («требует
    проверки человеком») или ACCEPTANCE_FAIL_MARKER («доработка»), оба вместе
    со снятием assignee как часть штатного пути приёмки, а не как «никем не
    замеченный пробел» (#467, см. check_reopened_after_merge). Текст маркера
    строится тем же способом, что и сама приёмка (f"{MARKER} PR #{n}") — одно
    место правды на форму строки, не вторая копия здесь.

    RuntimeError (комментарии не прочитаны — сеть/права) не глотаем молча:
    возвращаем False, чтобы инвариант сработал и не спрятал реальный сбой
    чтения за тишиной "уже обработано"."""
    for marker_const in (scheduler.ACCEPTANCE_PARTIAL_MARKER, scheduler.ACCEPTANCE_FAIL_MARKER):
        marker = f"{marker_const} PR #{pr_number}"
        try:
            if issue_marker_times(repo, issue_number, marker):
                return True
        except RuntimeError:
            return False
    return False


def check_reopened_after_merge(repo: str, open_tasks: list[dict], merged_pulls: list[dict]) -> list[dict]:
    """Открытая задача task без assignee, для которой уже есть слитый PR
    этой задачи (`task_ref.resolve_pr_task` по имени ветки). Механизм инцидента: reap_stale
    (scheduler.py) смотрит только ОТКРЫТЫЕ PR — если PR уже слит, «нет
    открытого PR, ссылающегося на задачу» читается как «работа брошена», и
    assignee снимается ДАЖЕ когда работа честно завершена, просто исполнитель
    ещё не сделал пост-мерж проверку и не закрыл issue. Свободная задача с
    уже слитым PR — именно то состояние, в котором free_task()/
    dispatch_worker выберут её заново (класс #18/#21/#78).

    Два исключения (#467, живой случай: 6 задач в одном алерте, растёт без
    возврата — «тормоз без газа»):

    - WATCHDOG_ISSUE (#120) — постоянный канал эскалации, не задача из пула;
      accept_merged_tasks её тоже явно пропускает (см. её докстринг), эта
      проверка не пропускала — сама WATCHDOG_ISSUE однажды оказалась в
      списке нарушителей своего же канала эскалации.
    - Задача, по которой приёмка УЖЕ вынесла терминальный вердикт для
      именно этого слитого PR (см. _acceptance_already_verdicted) — это не
      тихий пробел, который ловит инвариант 1, а уже озвученный исход:
      либо ждёт решения человека (маркер называет причину прямо в этой же
      задаче), либо ждёт НОВОГО PR от воркера (fail — штатный путь назад в
      пул, не нарушение). Без этого исключения список нарушителей растёт
      монотонно на каждый цикл приёмки без возврата."""
    unassigned = {t["number"]: t for t in open_tasks
                  if not t["assignees"] and t["number"] != WATCHDOG_ISSUE}
    by_task: dict[int, list[dict]] = {}
    for pull in merged_pulls:
        declared = task_ref.resolve_pr_task(pull)
        if declared is not None:
            by_task.setdefault(declared, []).append(pull)

    violations = []
    for number, pulls in sorted(by_task.items()):
        if number not in unassigned:
            continue
        newest = max(pulls, key=lambda p: p["merged_at"])
        if _acceptance_already_verdicted(repo, number, newest["number"]):
            continue
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
# Инвариант 3: гейт 1 отработал без вердикта ai:* дольше порога
# ══════════════════════════════════════════════════════════════════════════


def last_ai_verdict_ever(timeline: list[dict]) -> tuple[str, datetime] | None:
    """Последний по времени вердикт ai:* (labeled), КОГДА-ЛИБО поставленный на
    PR — может относиться к уже ЗАКРЫТОЙ эпохе гейта 1, если после него гейт 1
    переставлялся новым пушем (по построению check_stuck_review_gate ниже
    зовёт эту функцию только когда СЕЙЧАС ни одной ai:*-метки нет, поэтому
    найденный вердикт всегда старше anchor). None — вердикта не было НИ РАЗУ
    за всю жизнь PR: другой факт, чем «был, но эпоха его стёрла» (#472,
    живой случай #387 — там вердикта не было ни разу, в отличие от #329/#327,
    где вердикт был, просто в предыдущей эпохе)."""
    events = [
        (event["created_at"], (event.get("label") or {}).get("name"))
        for event in timeline
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") in review_labels.AI_VERDICTS
    ]
    if not events:
        return None
    when, name = max(events, key=lambda pair: pair[0])
    return name, parse_time(when)


def last_gate1_labeled_at(repo: str, pull: dict) -> datetime | None:
    """Момент публикации commit status `harness/review` на ТЕКУЩЕМ head PR
    (review_labels.status_posted_at, #345) — одно место правды со
    scheduler.last_gate1_labeled_at (находка ревью #424): эта функция раньше
    была НЕЗАВИСИМОЙ копией той же логики на таймлайн-событии "labeled" и
    уже расходилась с оригиналом дважды (#303 — пагинация, #432 — какая
    метка гейта 1 смотрится); третье расхождение (идемпотентность меток
    #203 заморозила "labeled" на первой простановке) чинится тем же
    прод-сигналом для обеих сторон сразу, а не третьей копией фикса.
    `check_pr.py` публикует `harness/review` каждым прогоном безусловно
    (#345), поэтому сигнал не зависит от того, переставилась метка или нет.

    None — статус `harness/review` на этом head не публиковался вовсе."""
    posted = review_labels.status_posted_at(
        repo, pull["head"]["sha"], review_labels.STATUS_REVIEW, gh)
    return parse_time(posted) if posted else None


def retry_budget_fact(repo: str, pr_number: int, anchor: datetime) -> dict:
    """Факт по бюджету авто-повтора #196 — считается ТЕМ ЖЕ вызовом
    (issue_marker_times по AI_REVIEW_RETRY_MARKER), который использует
    scheduler.ai_review_retry_count для решения «дёргать ли ai-review.yml
    снова» — не гадаем, а читаем то же число, что видит газ.

    attempts_total — за ВСЮ историю PR (так считает сегодняшний
    scheduler.trigger_ai_review — без since, #431/PR #439 со scoping'ом по
    эпохе ещё не слит в main). attempts_in_epoch — только попытки С МОМЕНТА
    anchor (текущая эпоха гейта 1). Расхождение между ними — сам факт
    переноса бюджета из старой, уже решённой эпохи (живой случай #329/#327,
    2026-09-06, issue #472): total уже равен лимиту, а in_epoch — 0, потому
    что все три маркера остались от эпохи, которая давно получила вердикт."""
    attempt_times = sorted(issue_marker_times(repo, pr_number, AI_REVIEW_RETRY_MARKER))
    attempts_in_epoch = sum(1 for moment in attempt_times if moment >= anchor)
    return {
        "attempts_total": len(attempt_times),
        "attempts_in_epoch": attempts_in_epoch,
        "attempts_limit": AI_REVIEW_MAX_ATTEMPTS,
        "last_attempt_at": attempt_times[-1].isoformat() if attempt_times else None,
    }


def check_stuck_review_gate(repo: str, now: datetime, open_pulls: list[dict]) -> list[dict]:
    """Гейт 1 отработал (review:ok ИЛИ review:large, review_labels.
    gate1_decided, #432) дольше UNHEALTHY_PR_AFTER_MINUTES, и НИ ОДНОЙ ai:*
    метки ещё нет. scheduler.trigger_ai_review (#196) уже пытается сам
    перезапустить ai-review.yml на меньшем пороге
    (AI_REVIEW_RETRY_AFTER_MINUTES) с ограничением попыток
    (AI_REVIEW_MAX_ATTEMPTS) — этот инвариант ловит случай, когда PR застрял
    несмотря на газ #196.

    #472 (живой алерт 2026-09-06 «либо исчерпал попытки, либо не сработал»):
    каждое нарушение несёт retry_budget_fact (сколько попыток из лимита
    израсходовано всего и сколько в ТЕКУЩЕЙ эпохе — расхождение само
    называет перенос бюджета из старой эпохи, класс #431/PR #439) и
    last_ai_verdict_ever (был ли вердикт хоть раз за всю жизнь PR) — факт, не
    гипотеза. Условие входа «гейт 1 не пускает» здесь физически невозможно:
    и этот инвариант, и trigger_ai_review проверяют один и тот же
    review_labels.gate1_decided — расходиться им не на чем."""
    ai_labels = set(review_labels.AI_VERDICTS)
    violations = []
    for pull in open_pulls:
        labels = {label["name"] for label in pull["labels"]}
        if not review_labels.gate1_decided(labels) or labels & ai_labels:
            continue
        anchor = last_gate1_labeled_at(repo, pull)
        if anchor is None:
            continue
        age = minutes_between(anchor, now)
        if age <= UNHEALTHY_PR_AFTER_MINUTES:
            continue
        timeline = review_labels.list_timeline(repo, pull["number"], gh)
        verdict_ever = last_ai_verdict_ever(timeline)
        violations.append({
            "pr": pull["number"],
            "age_minutes": round(age, 1),
            "labeled_at": anchor.isoformat(),
            "verdict_ever": None if verdict_ever is None else {
                "label": verdict_ever[0], "at": verdict_ever[1].isoformat(),
            },
            **retry_budget_fact(repo, pull["number"], anchor),
        })
    return violations


def stuck_gate_fact_line(item: dict) -> str:
    """Строка факта по застрявшему PR (#472) — заменяет прежнее «либо
    исчерпал попытки, либо не сработал». Три факта, каждый читается из уже
    собранных данных: возраст текущей эпохи, бюджет авто-повтора (с явным
    отличием «исчерпан в этой эпохе» от «перенёсся из старой, решённой» —
    и не смешивая их со случаем «бюджет ещё есть»), был ли вердикт вообще.
    Причину провала конкретной попытки (квота/транспорт/контракт) эта
    строка не называет: она потребовала бы отдельного обращения к Actions
    API (логи прогона) на КАЖДЫЙ застрявший PR каждые 15 минут — новый,
    дорогой класс вызовов, который эта задача сознательно не заводит (см.
    issue #472, экономия квоты GitHub API — она же сегодня отжирала себя у
    чужих обязательных проверок)."""
    total = item["attempts_total"]
    in_epoch = item["attempts_in_epoch"]
    limit = item["attempts_limit"]
    if total < limit:
        budget = f"не исчерпан ({total}/{limit}) — должен сработать на ближайшем тике оркестратора"
    elif in_epoch >= limit:
        budget = f"исчерпан в этой же эпохе ({total}/{limit})"
    else:
        budget = (
            f"исчерпан СТАРОЙ эпохой ({total}/{limit}, в текущей — {in_epoch}/{limit}) — "
            "перенос бюджета между эпохами, класс #431/PR #439 (не слит)"
        )
    if item["last_attempt_at"]:
        budget += f", последняя попытка {item['last_attempt_at']}"
    verdict = item["verdict_ever"]
    verdict_text = (
        "вердикта ai:* не было НИ РАЗУ за всю жизнь PR" if verdict is None
        else f"вердикт был — {verdict['label']} в {verdict['at']} (до текущей эпохи)"
    )
    return (
        f"PR #{item['pr']} — {int(item['age_minutes'])} мин без вердикта в "
        f"текущей эпохе (с {item['labeled_at']}); автоповтор: {budget}; {verdict_text}"
    )


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 4: openspec/changes/<id> полностью отмечен и не заархивирован
# ══════════════════════════════════════════════════════════════════════════

_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]", re.MULTILINE)

# Абзац-декларация задачи в proposal.md — первая строка начинается ровно с
# «Задача:»/«Задачи:» (обе формы встречаются, docs/agents/OPENSPEC-PROTOCOL.md,
# двоеточие сразу после слова — не «Задача владельца:» и не любая другая
# проза, начинающаяся с той же приставки: task-rework-loop реально открывается
# абзацем «Задача владельца: сформулирована в чате...» — это чужая, ещё не
# заведённая декларация, не форма протокола, и якорь обязан её не матчить
# (живая находка при ревью PR #663 — старый якорь `^Задач` матчил и её; не
# страховало только отсутствие #N в этом конкретном абзаце фикстюры).
# Абзац — до пустой строки, не вся страница: та же проза task-rework-loop
# НИЖЕ по файлу упоминает #200/#245/#247 как чужой контекст (зависимости,
# история), а собственной заведённой задачи ещё не имеет (proposal.md прямо
# говорит «issue в пуле пока не заведена этим change») — взять первый `#N`
# по ВСЕМУ файлу означало бы приписать change чужой номер и закрыть его по
# чужому состоянию.
_DECLARED_TASK_PARA_RE = re.compile(r"^Задач(?:а|и):")


def declared_change_task(proposal_text: str) -> int | None:
    """Первый номер задачи из абзаца-декларации, `None` — декларации нет
    (change ещё не заведён в пуле, или вопрос завершённости решается только
    через tasks.md). Переиспользует task_ref.extract_task_refs — то же
    место правды на форму `#N` с границей числа, что и весь остальной
    репозиторий (#187), не вторая копия регэкспа здесь."""
    if not proposal_text:
        return None
    for para in re.split(r"\n\s*\n", proposal_text):
        stripped = para.strip()
        if not stripped or not _DECLARED_TASK_PARA_RE.match(stripped):
            continue
        refs = task_ref.extract_task_refs(para)
        return refs[0] if refs else None
    return None


def fetch_task_states(repo: str, numbers: set[int]) -> dict[int, dict]:
    """`{номер: {"state":.., "state_reason":..}}` для заданных issue —
    точечные вызовы по номеру (не листинг): номеров мало (один на change,
    ≤44 на сегодня), а закрытых issue в репозитории уже 200+ и растёт —
    листинг задним числом расширялся бы бесконечно (см. fetch_merged_pulls
    выше про тот же компромисс на стороне PR). Только 404 (issue не
    существует под этим номером) не поднимаем — задача с этим номером просто
    не попадёт в словарь, и второй путь check_unarchived_complete_changes
    честно её пропустит, не упав. Любую другую ошибку (сеть, квота, 5xx)
    поднимаем дальше — молча проглотить её означало бы, что зелёный
    инвариант 4 на самом деле «не подтверждено» вместо «нарушений нет»
    (ревью PR #663: неотличимость сети от 404 могла бы подсветить backlog
    как разобранный при обычном сетевом сбое ровно в момент, когда кто-то
    проверяет условие возврата 4 в CI_GATING)."""
    states: dict[int, dict] = {}
    for number in sorted(numbers):
        try:
            issue = gh(f"repos/{repo}/issues/{number}")
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc) and "Not Found" not in str(exc):
                raise
            continue
        if issue:
            states[number] = {
                "state": issue.get("state"),
                "state_reason": issue.get("state_reason"),
            }
    return states


def check_unarchived_complete_changes(
    changes_dir: Path,
    task_states: dict[int, dict] | None = None,
    open_pull_texts: list[str] | None = None,
) -> list[dict]:
    """Каталог openspec/changes/<id> завершён, но не лежит под
    openspec/changes/archive/ (её пока не существует вовсе — задокументиро-
    ванный факт задачи #244) — ЛЮБОЕ из двух НЕЗАВИСИМЫХ условий:

    (a) быстрый путь — tasks.md существует, и в нём отмечен КАЖДЫЙ чекбокс
        (и есть хотя бы один). Годится для change, заведённых после того,
        как tasks.md стал обязателен.

    (b) второй путь — применяется, ТОЛЬКО когда tasks.md вообще НЕ
        существует (не «существует, но не всё отмечено» — незакрытый
        чеклист это реальная незавершённая работа, второй путь не должен её
        перебивать чужим фактом): proposal.md декларирует задачу
        (declared_change_task), эта задача закрыта с state_reason=completed
        (task_states — предзагруженный словарь, IO снаружи, см.
        fetch_task_states), и ни один открытый PR не ссылается на путь
        `openspec/changes/<id>` буквально (open_pull_texts — предзагруженные
        title+body открытых PR, IO снаружи, см. fetch_open_pulls). Оба
        аргумента `None` по умолчанию — путь (b) тогда просто не
        применяется (существующие вызовы с одним `changes_dir` не меняют
        поведения, см. тесты test_unarchived_complete_*).

    Подробный протокол и цифры реального backlog —
    docs/agents/OPENSPEC-PROTOCOL.md."""
    if not changes_dir.is_dir():
        return []
    violations = []
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        tasks_md = entry / "tasks.md"
        if tasks_md.exists():
            boxes = _CHECKBOX_RE.findall(tasks_md.read_text(encoding="utf-8"))
            if boxes and all(box.lower() == "x" for box in boxes):
                violations.append({"change": entry.name, "checked": len(boxes)})
            continue  # tasks.md существует — путь (b) не подменяет его сигнал
        if task_states is None or open_pull_texts is None:
            continue
        proposal_md = entry / "proposal.md"
        if not proposal_md.exists():
            continue
        task_number = declared_change_task(proposal_md.read_text(encoding="utf-8"))
        if task_number is None:
            continue
        state = task_states.get(task_number)
        if not state or state.get("state") != "closed" or state.get("state_reason") != "completed":
            continue
        needle = f"openspec/changes/{entry.name}"
        if any(needle in text for text in open_pull_texts):
            continue
        violations.append({"change": entry.name, "task": task_number, "reason": "task-closed"})
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
# Инвариант 7: двусмысленная формула принадлежности плагина (#219)
# ══════════════════════════════════════════════════════════════════════════

# Правило AGENTS.md «Утверждение о готовом артефакте обязано нести его адрес»
# в общем виде статически НЕ выразимо — оно про смысл текста, и этот инвариант
# не претендует на его покрытие (правило держится на ревью, честно). Гвардится
# узкий класс, оплаченный задачей #219: формула принадлежности плагина вида
# «плагин + словоформа + „владельца“», читавшаяся двумя способами — «артефакт
# уже есть у владельца» против «наш, пишем мы». Из-за этого исполнители и
# владелец одновременно ждали работу друг от друга, и готовая ротация учёток
# пять суток лежала неподключённой (#215). Формула запрещена к употреблению
# вовсе: вместо неё — «артефакт владельца по адресу X» либо «наш плагин
# (пишем мы, не апстрим)». Паттерн объявлен один раз здесь; дословно формулу
# нигде не пишем (включая этот файл), чтобы будущий рефакторинг сканера не
# поймал инвариант на его собственном исходнике.
_AMBIGUOUS_ARTIFACT_PHRASE = re.compile(r"плагин\w*\s+владельца", re.IGNORECASE)
_SCAN_SKIP_DIRS = frozenset({".git", "node_modules"})


def check_ambiguous_artifact_phrase(docs_root: Path) -> list[dict]:
    """Инвариант 7: ни один markdown-документ репозитория не содержит
    двусмысленной формулы принадлежности плагина (любые словоформы «плагин*»
    с «владельца», регистр не важен). Сканируются ВСЕ *.md под корнем —
    документ остаётся документом в любом каталоге, включая архив спек и
    шаблоны .github. Замена — однозначная формулировка с адресом или
    принадлежностью; правило — AGENTS.md, «Утверждение о готовом артефакте
    обязано нести его адрес» (#219)."""
    if not docs_root.is_dir():
        return []
    violations = []
    for path in sorted(docs_root.rglob("*.md")):
        if set(path.parts) & _SCAN_SKIP_DIRS:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = _AMBIGUOUS_ARTIFACT_PHRASE.search(line)
            if match:
                violations.append({
                    "file": path.relative_to(docs_root).as_posix(),
                    "line": lineno,
                    "match": match.group(0),
                })
    return violations


# ══════════════════════════════════════════════════════════════════════════
# Инвариант 9 (#637): PR готов к слиянию, застрял без вердикта — и владелец
# об этом НЕ узнал ни задачей, ни доставленным сигналом
# ══════════════════════════════════════════════════════════════════════════
#
# Живой замер (прогон 34194073339, шаг «Обход пула и очередь слияний»,
# 2026-09-08T06:20:31Z): 5 PR без вердикта AI от 155 до 296 минут, у каждого
# автоповторов 3/3 («не дёргаю снова, нужен человек»): #726 (225 мин), #721
# (238), #618 (175), #453 (155), #328 (296). Последней строкой того же шага:
# «🚨 потолок автозаведённых задач в сутки исчерпан (5/5) — отпечаток
# gate:pipeline-paused НЕ заведён, нужен человек». Конвейер стоял, механизм
# распознал это верно и не смог сообщить владельцу — сессия узнала о
# простое только потому, что кто-то прочитал лог прогона руками. Этот
# инвариант — второй, независимый от stall_detector канал того же класса:
# не разбирает текст отчёта пульса (регэкспы stall_detector.extract_signals
# могут не покрыть конкретную формулировку), а смотрит прямо на факт,
# который уже вычисляет инвариант 3 (гейт 1 решён, вердикта нет), плюс факт
# исчерпания бюджета автоповтора В ЭТОЙ ЖЕ ЭПОХЕ (новый тик оркестратора не
# добавит попытки), плюс факт присутствия сигнала — задача класса
# `gate:no-ai-verdict` (stall_detector.find_open_task) или подтверждённая
# доставка потолка за сегодня (stall_detector.CAP_EXHAUSTED_DELIVERED_MARKER).
# Ни того, ни другого — нарушение, даже если stall_detector в этом самом
# пульсе почему-то не увидел сигнал вовсе.


def check_stalled_review_without_signal(
    stuck_violations: list[dict], has_open_signal_task: bool, delivered_today: bool,
) -> list[dict]:
    """Подмножество нарушений инварианта 3, чей бюджет автоповтора #196 уже
    ИСЧЕРПАН В ТЕКУЩЕЙ ЭПОХЕ (`attempts_in_epoch >= attempts_limit` —
    поэтому новый тик оркестратора точно не разрешит эпизод сам, в отличие
    от «бюджет ещё не исчерпан», который стоит подождать), и при этом ни
    открытой автозадачи (`has_open_signal_task`), ни доставленного сигнала
    за сегодня (`delivered_today`) не найдено. Один и тот же
    `has_open_signal_task`/`delivered_today` — на всю партию: оба факта
    описывают состояние на уровне репозитория/дня, не конкретного PR (тот
    же PR-независимый масштаб, что у CAP_EXHAUSTED_DELIVERED_MARKER в
    stall_detector — потолок и его доставка считаются на СУТКИ, не на
    отпечаток)."""
    if has_open_signal_task or delivered_today:
        return []
    return [
        item for item in stuck_violations
        if item["attempts_in_epoch"] >= item["attempts_limit"]
    ]


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


def changes_without_tasks_md(changes_dir: Path) -> dict[str, int]:
    """`{имя_change: номер_задачи}` для каталогов БЕЗ tasks.md, чей
    proposal.md декларирует задачу (declared_change_task) — только эти
    номера стоит спрашивать у fetch_task_states (второй путь
    check_unarchived_complete_changes не смотрит дальше, см. её докстринг).
    Чистая файловая функция (как и сам check_*), сетевой IO — отдельно."""
    if not changes_dir.is_dir():
        return {}
    result: dict[str, int] = {}
    for entry in sorted(changes_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        if (entry / "tasks.md").exists():
            continue
        proposal_md = entry / "proposal.md"
        if not proposal_md.exists():
            continue
        task_number = declared_change_task(proposal_md.read_text(encoding="utf-8"))
        if task_number is not None:
            result[entry.name] = task_number
    return result


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
    declared_tasks = changes_without_tasks_md(OPENSPEC_CHANGES)
    task_states = fetch_task_states(repo, set(declared_tasks.values()))
    open_pull_texts = [
        (pull.get("title") or "") + "\n" + (pull.get("body") or "") for pull in open_pulls
    ]

    findings: dict[int, list] = {}
    lines = ["## Инварианты состояния репозитория (#244)"]

    v1 = check_reopened_after_merge(repo, open_tasks, merged_pulls)
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
        lines.append(f"🚨 [3] {len(v3)} открытых PR с гейтом 1 (review:ok/review:large) без вердикта ai:* дольше {UNHEALTHY_PR_AFTER_MINUTES} мин:")
        for item in v3:
            lines.append(f"   — {stuck_gate_fact_line(item)}")
    else:
        lines.append("💚 [3] нет застрявших PR с гейтом 1 без ai:*")

    v4 = check_unarchived_complete_changes(OPENSPEC_CHANGES, task_states, open_pull_texts)
    findings[4] = v4
    if v4:
        lines.append(f"🚨 [4] {len(v4)} openspec/changes завершены и не заархивированы:")
        for item in v4:
            if "checked" in item:
                lines.append(f"   — openspec/changes/{item['change']} ({item['checked']} чекбоксов)")
            else:
                lines.append(
                    f"   — openspec/changes/{item['change']} (задача #{item['task']} "
                    "закрыта completed, нет tasks.md, нет открытого PR на этот путь)"
                )
    else:
        lines.append("💚 [4] нет завершённых незаархивированных change")

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

    v7 = check_ambiguous_artifact_phrase(REPO_ROOT)
    findings[7] = v7
    if v7:
        lines.append(f"🚨 [7] {len(v7)} мест называют принадлежность плагина двусмысленно (#219):")
        for item in v7:
            lines.append(f"   — {item['file']}:{item['line']} — «{item['match']}»")
    else:
        lines.append("💚 [7] двусмысленной формулы принадлежности плагина нет")

    # Инвариант 9 (#637): ленивый — сеть только если ЕСТЬ кандидат
    # (attempts_in_epoch >= attempts_limit хоть у одного PR из v3), тот же
    # приём холостого хода, что у остальных инвариантов (гвардия
    # test_idle_guard_healthy_snapshot_no_violations_no_mutating_calls не
    # должна ловить лишний вызов на здоровом снимке без застрявших PR).
    any_exhausted = any(item["attempts_in_epoch"] >= item["attempts_limit"] for item in v3)
    if any_exhausted:
        has_open_signal_task = stall_detector.find_open_task(repo, "gate:no-ai-verdict") is not None
        delivered_marker = f"{stall_detector.CAP_EXHAUSTED_DELIVERED_MARKER} {now.date().isoformat()}]"
        delivered_today = bool(issue_marker_times(repo, WATCHDOG_ISSUE, delivered_marker))
    else:
        has_open_signal_task = False
        delivered_today = False
    v9 = check_stalled_review_without_signal(v3, has_open_signal_task, delivered_today)
    findings[9] = v9
    if v9:
        lines.append(
            f"🚨 [9] {len(v9)} PR готовы к слиянию, застряли без вердикта и БЕЗ "
            "доставленного сигнала владельцу (ни задачи gate:no-ai-verdict, ни "
            f"подтверждённой доставки потолка сегодня, #637):"
        )
        for item in v9:
            lines.append(f"   — {stuck_gate_fact_line(item)}")
    else:
        lines.append("💚 [9] нет застрявших PR без доставленного сигнала владельцу")

    return lines, findings


def summary(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


ESCALATING_INVARIANTS = (1, 3, 9)


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
            f"{len(v3)} PR с гейтом 1 (review:ok/review:large) без вердикта ai:* "
            f"дольше {UNHEALTHY_PR_AFTER_MINUTES} мин:\n"
            + "\n".join(f"— {stuck_gate_fact_line(item)}" for item in v3)
        )
        result = escalate_if_new(repo, 3, key, text)
        if result:
            lines.append(f"📣 инвариант 3 эскалирован: {result}")
    if findings.get(9):
        v9 = findings[9]
        key = ",".join(f"#{i['pr']}" for i in v9)
        text = (
            "🚨 edge-harness: инвариант 9 (застрял без сигнала владельцу, #637) — "
            f"{len(v9)} PR готовы к слиянию, вердикта ai:* нет, автоповтор #196 "
            "исчерпан в текущей эпохе, и ни задачи gate:no-ai-verdict, ни "
            "доставленного сигнала за сегодня не найдено:\n"
            + "\n".join(f"— {stuck_gate_fact_line(item)}" for item in v9)
            + "\n\nЧто дальше: поставь вердикт ai:* вручную (issue-comment "
            "ai-review.yml) или перезапусти прогон — тот же газ, что у "
            "инварианта 3 (GATING_RELEASE_CONDITION[3])."
        )
        result = escalate_if_new(repo, 9, key, text)
        if result:
            lines.append(f"📣 инвариант 9 эскалирован: {result}")
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
