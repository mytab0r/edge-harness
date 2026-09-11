#!/usr/bin/env python3
"""Планировщик оркестратора: следит за пулом задач и сливает проверенные PR.

Вызывается workflow'ом orchestra по расписанию (каждые 15 минут) и вручную.
Workflow держит concurrency-группу `orchestra`: два запуска планировщика
никогда не идут параллельно — это архитектурная сериализация слияний.

Обязанности:
  1. Просроченные назначения: задачу назначили, PR так и не появился за STALE_HOURS —
     назначение снимается, задача возвращается в пул (агент мог умереть посреди работы).
  2. Конфликты: открытые PR, у которых mergeable_state=dirty, получают метку
     `conflict` и комментарий со списком соперников (mark_conflicts). Расшивка
     не ждёт человека (#474): dispatch_conflict_rework снимает assignee+замок
     задачи и запускает worker.yml адресно (вход `task`, тот же путь, что уже
     доводит открытые PR, #245/#394) на авто-ребейз — одна попытка на PR
     (лифтайм-бюджет), не сошлось — эскалация владельцу с файлами-кандидатами.
  3. Очередь слияний: PR с зелёными проверками и чистым контрактом сливается.
     За один проход очереди — ровно один PR (сериализация merge_queue, #252/#288
     не меняется), но один ЗАПУСК планировщика (#297) — это цикл таких проходов
     (merge_loop): слить готовый → подтянуть следующего → дождаться его проверок →
     слить, пока есть что сливать, до потолка MERGE_LOOP_MAX_MERGES или таймаута
     MERGE_LOOP_TIMEOUT_SECONDS. Раньше цикл ограничивался внешней периодикой
     (schedule доставляет ~7% тиков, docs/research/21) — теперь запуск сам
     проводит всю очередь, какая накопилась к моменту события.
  4. Пульс конвейера: если в пуле есть свободная задача и активного worker-рана
     нет — ровно один `workflow_dispatch` воркера (scripts/worker/task.sh,
     docs/agents/WORKER-PLAYBOOK.md). Best-effort: сбой диспатча не роняет
     планировщик.
  5. Предохранитель конвейера (#120): WORKER_FAILURE_PAUSE_AFTER красных прогонов
     worker.yml подряд останавливают диспатч; сигнал — Telegram + задача #120,
     один на серию. Логика в scripts/orchestra/pulse_guard.py (пороги — там).
  6. «Кто следит за следящим» (#120): каждый запуск сначала проверяет возраст
     последнего успешного пульса orchestra — пропавшие пульсы кричат в Telegram,
     пока этот запуск сам жив.
  7. Замки задач (#121): протухшие аренды (refs/locks/task-*, TTL в
     scripts/lib/claim_task.py) снимаются со следом в задаче; после слияния PR
     замки упомянутых в его теле задач освобождаются.
  8. Архив сессий раннера после мержа (#119): сбой (морда недоступна для логина,
     RPC отклонил архив не по «сессии нет») — возможность ЕСТЬ, но сломана
     (#174): fail loud — мерж уже состоялся и не откатывается, но прогон
     окрашивается красным ПОСЛЕ того, как отчёт сохранён, а сигнал уходит
     тем же каналом, что предохранитель конвейера (issue #120 + Telegram).
  9. Петля состояния открытого PR (#196) — три поведения, все смотрят не
     только на факт создания/слияния PR, но и на его состояние в промежутке:
       a. review:ok без вердикта AI дольше порога (или ai:failed) — оркестратор
          сам дёргает ai-review.yml, с ограничением числа попыток на PR
          (счётчик — маркер в комментариях PR, переживает перезапуск), но
          ТОЛЬКО за текущую эпоху review:ok — новый пуш даёт свежий бюджет
          (#431). Исчерпание бюджета — не молчание: эскалация тем же каналом,
          что предохранитель конвейера. quota_exhausted провайдера (#419) не
          тратит автоповтор вовсе — сразу эскалация, ретраить его в CI
          бессмысленно (docs/runbooks/switch-llm-provider.md).
       b. Красный обязательный чек или ai:changes-requested дольше порога —
          назначение снимается, задача возвращается в пул, PR не закрывается.
       c. После слияния — gh pr update-branch для остальных открытых PR,
          но выборочно (#252, review_labels.should_update_branch): только
          близкие к слиянию (оба вердикта зелёные) или уже в конфликте —
          подтягивание синхронизирует pr-review.yml/ai-review.yml и снимает
          валидный ai:*-вердикт без пользы для PR, которому рано сливаться.
          Конфликт (DIRTY) не молчит: строка в отчёте + метка conflict.
          До UPDATE_BRANCH_BUDGET_PER_PASS подтянутых кандидатов за ПРОХОД
          (#252 третий заход, потолок поднят #761 — было «максимум один») —
          счётчик общий и живёт в update_branch (не по одному на каждую
          точку вызова), поэтому та же дисциплина держит и behind-ветку
          merge_queue ниже. Кандидаты сверх бюджета получают строку и ждут
          следующего прохода цикла слияний (merge_loop, #297). Раньше
          «проход» и «прогон» совпадали (один merge_queue на весь запуск);
          с #297 прогон — это несколько проходов подряд (см. пункт 3), у
          каждого свой бюджет.
     Пороги — AI_REVIEW_RETRY_AFTER_MINUTES / AI_REVIEW_MAX_ATTEMPTS /
     UNHEALTHY_PR_AFTER_MINUTES в pulse_guard.py, рядом с остальными порогами
     предохранителя (одно место правды).
  10. Сигнал дрейфа пина апстрима (#134): релиз апстрима новее пина
      source-build морды (dsh-edge/upstream.json) кричит в задачу #134
      + метка update-available + Telegram, один раз на релиз. Логика в
      scripts/orchestra/upstream_drift.py; сбой сверки не роняет планировщик,
      но и не молчит — ⚠️ в отчёте (сломанная сверка прячет дрейф так же
      надёжно, как её отсутствие).
  11. Инвариант «готовый PR не должен ждать» (#269): PR, у которого ОБЕ метки-
      гейта стоят и все обязательные проверки зелёные, но слияния не
      произошло дольше UNHEALTHY_PR_AFTER_MINUTES с момента готовности —
      кричит тем же каналом escalate(), что предохранитель конвейера (#120).
      Наблюдаемая величина именно эта (задержка слияния готового PR), а не
      статус последнего прогона оркестратора — тот может быть сплошь success,
      пока сам прогон не случается достаточно часто (issue #269, #297).
  12. Запрет переоткрытия (#369, решение владельца): закрытая задача не
      переоткрывается никогда — остаток работы оформляется НОВОЙ, более узкой
      задачей со ссылкой на закрытую. GitHub не умеет отклонить reopen нативно
      (нет ни repo-настройки, ни API-поля), поэтому носитель правила — пульс:
      issue из пула с `state_reason == "reopened"` закрывается обратно с
      комментарием-отказом ДО того, как её увидит accept_merged_tasks (см.
      reject_reopened_tasks).
  13. Видимость непринятых задач (#427): reap_stale (п.1) снимает только
      заброшенные НАЗНАЧЕННЫЕ задачи — задача, которую никто не брал вовсе,
      не отслеживалась ничем (замер 2026-09-06: 84 из 109 открытых без
      исполнителя). mark_stale_unclaimed ставит метку `stale-unclaimed` на
      задачу без исполнителя старше STALE_HOURS (тот же порог, что у п.1 —
      второе число правды не заводится) и снимает её сама, как только задачу
      берут. Это НЕ тормоз: ничего не закрывается и не переназначается —
      только видимость; газ («взять/переформулировать/закрыть как отпавшую»)
      — вручную, владелец или агент, наткнувшийся на метку (машина не умеет
      отличить нужное от отпавшего). Использует уже прочитанный `pool` этого
      же прогона — второго обхода Issues не заводится.
  14. Детектор устойчивого простоя (#201): каждый пульс отдаёт уже собранный
      отчёт (строки выше) в scripts/orchestra/stall_detector.py — устойчивый
      (дольше порога), незнакомый (нет открытой автозадачи) отпечаток причины
      простоя заводит задачу пула с уликами; известный отпечаток получает
      комментарий с новой уликой, не вторую задачу. Своя ответственность,
      свой модуль — вся логика, пороги и честный потолок там.
  15. WIP-лимит перед взятием НОВОЙ задачи (#464, решение владельца
      2026-09-06: «вместо того чтобы доделать текущие ПР, агенты идут и
      создают новые»): dispatch_worker до этого пункта смотрел только на пул
      задач, не на очередь открытых PR — 29 открытых, 25 из них реально ждут
      доработки, не мешали диспетчу тридцатой задачи. wip_gate — второй,
      независимый от предохранителя конвейера (п.5) тормоз: считает открытые
      PR, реально ждущие чужого труда (REWORK_LABELS — не вердикта, который
      придёт сам), и держит закрытым взятие НОВОЙ задачи, пока их больше или
      равно WIP_LIMIT. Доводку уже открытых PR гейт НЕ трогает (находка ревью
      PR #466: закрывать диспетч воркера целиком — тормоз без газа, очередь
      физически не могла бы разгрестись сама) — dispatch_worker при закрытом
      гейте по-прежнему вправе адресно запустить воркера на задачу, у которой
      уже есть открытая ветка/PR (см. её докстринг и main()); блокируется
      только выбор задачи, у которой PR ещё нет вовсе. Газ — слияние/закрытие
      PR ниже порога, снимается автоматически следующим прогоном; если гейт
      держит взятие НОВЫХ задач закрытым дольше WIP_GATE_STUCK_HOURS —
      отдельный сигнал тем же каналом, что предохранитель конвейера: сигнал
      сам различает «доводить есть что, но не успевает», «свободны только
      конфликтные — их ведёт авто-расшивка #474» и «доводить нечего» по
      данным того же прогона (AGENTS.md: алерт не гадает) — затор не в
      притоке задач, а в доработке существующих PR. При погашенном
      предохранителе (#120) гейт молчит и закрывает свой эпизод:
      действующий тормоз один, назван предохранителем (блокирующая находка
      2 ревью PR #466).
  16. Замена PR, чья ветка называет закрытую задачу (#543, живой замер:
      владелец делал это руками 9 раз за сутки — PR #359/#384/#388/#167/
      #231/#393/#439/#415 и задача #530). Контракт резолвит задачу PR ТОЛЬКО
      по имени ветки (task_ref.resolve_pr_task, #394/#398) — правка тела PR
      контракт больше не читает вовсе, единственный официальный выход —
      новая ветка на новый номер (docs/agents/PROTOCOL.md). replace_closed_task_prs
      заводит узкую задачу-замену (pool_issue.create_pool_issue, метки
      task+auto-detected) и метит PR (тело + комментарий) на уже прочитанном
      снимке pulls/pool этого прогона — сама смена ветки/открытие нового PR
      остаётся исполнителю (закрытие/переоткрытие чужого PR — необратимое
      действие фонового job'а, здесь сознательно не делается, см. живой
      эксперимент в openspec/changes/archive/contract-task-from-branch/design.md:
      переименование ветки закрывает открытый PR вместо переориентации).
      Идемпотентность — маркер в теле PR плюс поиск существующей замены по
      конвенции `Related: #<старая>` (Search API, отдельный бюджет 30/мин,
      не общий core 5000/час, #454) — ловит и ручные замены (живой случай
      #431→#538), не только свои.
  17. Доводка PR по находкам AI-ревью (дефект B watchdog-issue #120: 23
      открытых PR с ai:changes-requested, старейшие #241/#261/#262 висят с
      2026-09-03 — восемь суток, потому что находки читает только человек,
      случайно открывший PR). dispatch_ai_review_rework — по образцу
      dispatch_conflict_rework (п.2, issue #474): адресный worker.yml
      (снятие assignee+замка, task.sh режим доводки #245, второй промпт не
      заводится), бюджет AI_REWORK_MAX_ATTEMPTS попыток НА ОТПЕЧАТОК диффа
      (review_labels.diff_fingerprint — дедуп «тот же head, тот же
      отпечаток находок», не лифтайм на PR: новый отпечаток — новая, ещё не
      пробованная задача), исчерпание эскалирует тем же каналом (#120), что
      и предохранитель конвейера. Порядок в main() — после dispatch_
      conflict_rework, до wip_gate/dispatch_worker: доводка существующих PR
      обязана иметь приоритет перед взятием НОВЫХ задач (прямая связь с
      п.15 — WIP-гейт душит новые задачи именно потому, что открытых PR,
      ждущих доработки, много). PR с меткой `conflict` исключены — их ведёт
      п.2, второго диспатча на тот же PR за пульс это не даёт.
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import http.cookiejar
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# gh()/parse_time() и логика предохранителя — одно место правды в pulse_guard
# (пороги WORKER_FAILURE_PAUSE_AFTER / HEARTBEAT_MAX_AGE_MINUTES живут там же;
# escalate — общий канал «поломка → задача-статус + Telegram», #120/#174;
# пороги петли открытого PR #196 — там же: AI_REVIEW_RETRY_AFTER_MINUTES,
# AI_REVIEW_MAX_ATTEMPTS, AI_REVIEW_RETRY_MARKER, UNHEALTHY_PR_AFTER_MINUTES;
# серия красных и её сброс мержем #220 — там же: WORKER_WORKFLOW,
# FAILURE_CONCLUSIONS, recent_runs, RESUME_MARKER).
from pulse_guard import (
    AI_REVIEW_EXHAUSTED_MARKER,
    AI_REVIEW_MAX_ATTEMPTS,
    AI_REVIEW_QUOTA_MARKER,
    AI_REVIEW_RETRY_AFTER_MINUTES,
    AI_REVIEW_RETRY_MARKER,
    AI_REWORK_ESCALATION_MARKER,
    AI_REWORK_MARKER,
    AI_REWORK_MAX_ATTEMPTS,
    ALLOW_PROD_WRITES_ENV,
    CONFLICT_BUDGET_RESET_MARKER,
    CONFLICT_ESCALATION_MARKER,
    CONFLICT_REWORK_MARKER,
    CONFLICT_REWORK_MAX_ATTEMPTS,
    FAILURE_CONCLUSIONS,
    READY_STALL_MARKER,
    RESUME_MARKER,
    UNHEALTHY_PR_AFTER_MINUTES,
    WATCHDOG_ISSUE,
    WORKER_GIT_STEP_MARKER,
    WORKER_WORKFLOW,
    all_issue_comments,
    announce_write_mode,
    conveyor_gate,
    escalate,
    failure_watch,
    gh,
    heartbeat_check,
    independent_pulse_check,
    issue_marker_times,
    merge_telegram_text,
    minutes_between,
    parse_time,
    post_issue_comment,
    prod_writes_allowed,
    recent_runs,
    resume_alert_text,
    send_telegram,
)
# Сигнал дрейфа пина апстрима (#134): вся логика — upstream_drift.py, здесь
# только вызов и честный сбой сверки (см. upstream_drift_lines ниже).
from upstream_drift import upstream_drift_check

# Детектор устойчивого простоя (#201): читает уже готовый отчёт пульса и
# заводит задачу пула по незнакомому отпечатку причины — своя ответственность,
# свой модуль (см. docstring stall_detector.py), сюда только вызов из main().
# AUTO_LABEL (#543) — та же метка происхождения «заведено автоматикой»,
# переиспользуется replace_closed_task_prs ниже: второй одноимённой константы
# не заводим.
from stall_detector import AUTO_LABEL, detect_and_act, escalate_stale_auto_tasks, groom_auto_tasks

# claim_task живёт в scripts/lib (общее место для всех каналов): TTL замка —
# одна константа LOCK_TTL_HOURS там, сюда не дублируется.
_LIB = Path(__file__).resolve().parents[1] / "lib" / "claim_task.py"
_claim_spec = importlib.util.spec_from_file_location("claim_task", _LIB)
claim_task = importlib.util.module_from_spec(_claim_spec)
_claim_spec.loader.exec_module(claim_task)

# Метки-вердикты ревью (review:*, ai:*) и формулировка гейта слияния —
# одно место правды в scripts/lib/review_labels.py (общее для check_pr,
# ai_review и scheduler).
_rl_spec = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(review_labels)

# Чеклист некритичных замечаний ревью в теле PR (#462, третья категория
# находок) — одно место правды в lib, общее с scripts/review/ai_review.py:
# тот пишет пункты при вердикте, этот читает незакрытые при слиянии.
_rc_spec = importlib.util.spec_from_file_location(
    "review_checklist", Path(__file__).resolve().parents[1] / "lib" / "review_checklist.py")
review_checklist = importlib.util.module_from_spec(_rc_spec)
_rc_spec.loader.exec_module(review_checklist)

# Единственное место правды на заведение issue пула (#526): без него labels
# без `task` собирались бы копией той же проверки, что уже есть у
# file_tasks.py/stall_detector.py/upstream_drift.py — вместо этого все
# четверо зовут одну функцию.
_pi_spec = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parents[1] / "lib" / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_pi_spec)
_pi_spec.loader.exec_module(pool_issue)

# Номер задачи из текста PR/issue — одно место правды (#187): границы числа
# с обеих сторон, не подстрока (класс «#18 совпал с #180» на contract_check,
# 33570081734).
_tr_spec = importlib.util.spec_from_file_location(
    "task_ref", Path(__file__).resolve().parents[1] / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(task_ref)

# Приоритет выбора свободной задачи (#361) — одно место правды в
# scripts/lib/free_task.py, ЧИТАЕТСЯ здесь, не переписывается локальной
# сортировкой по номеру (та была вторым местом правды до этого change —
# dispatch_worker ниже утверждал «та же задача, которую выберет oldest_free»,
# сам сортируя по номеру мимо приоритета). task_deps — граф блокировок
# (blockedBy/blocking), тот же источник, что использует
# scripts/worker/task.sh при реальном выборе.
_ft_spec = importlib.util.spec_from_file_location(
    "free_task", Path(__file__).resolve().parents[1] / "lib" / "free_task.py")
free_task = importlib.util.module_from_spec(_ft_spec)
_ft_spec.loader.exec_module(free_task)

_td_spec = importlib.util.spec_from_file_location(
    "task_deps", Path(__file__).resolve().parents[1] / "lib" / "task_deps.py")
task_deps = importlib.util.module_from_spec(_td_spec)
_td_spec.loader.exec_module(task_deps)

# Персистентное состояние квоты провайдеров (#857, openspec/changes/
# provider-quota-gating) — одно место правды на формат reset-хинта и на
# носитель (vars.DSH_PROVIDER_QUOTA_UNTIL), design.md там же.
_pqs_spec = importlib.util.spec_from_file_location(
    "provider_quota_state", Path(__file__).resolve().parent / "provider_quota_state.py")
provider_quota_state = importlib.util.module_from_spec(_pqs_spec)
_pqs_spec.loader.exec_module(provider_quota_state)

# Реестр «слитый путь → workflow, реагирующий на push по main» (#955,
# следствие #929) — одно место правды, читается ЗДЕСЬ (диспатч) и
# scripts/orchestra/repo_invariants.py (инвариант 15, проверка, что диспатч
# реально дал прогон). Заменяет прежний локальный dispatch_deploy_on_merge
# (subprocess.run(["gh","workflow","run",...]) без дедупа по sha).
_mr_spec = importlib.util.spec_from_file_location(
    "merge_reactions", Path(__file__).resolve().parents[1] / "lib" / "merge_reactions.py")
merge_reactions = importlib.util.module_from_spec(_mr_spec)
_mr_spec.loader.exec_module(merge_reactions)

STALE_HOURS = 24
TASK_LABEL = "task"
# Одно место правды — review_labels.py (см. should_update_branch там же,
# #252): раньше здесь была вторая локальная константа "conflict".
CONFLICT_LABEL = review_labels.CONFLICT_LABEL
# Эскалация playbook (task.sh: `gh issue edit $number --add-label blocked`) —
# «нужен владелец», назначение при этом намеренно остаётся. reap_stale и
# unhealthy_pulls обязаны пропускать такую задачу: иначе снятие исполнителя
# по таймеру возвращает задачу в пул, oldest_free/scheduler снова выбирают её
# как старейшую, пульс диспатчит воркера на то же самое блокирующее условие —
# вечный цикл без газа (замер AI-ревью PR #247, 2026-09-03). Газ — тот же, что
# у самой метки (docs/agents/LABELS.md): владелец снимает `blocked` вручную.
BLOCKED_LABEL = "blocked"
MERGE_METHOD = "squash"
# Видимость непринятых задач (#427) — метка, не тормоз (см. mark_stale_unclaimed
# ниже и запись в docs/agents/LABELS.md). Порог — STALE_HOURS выше, то же число,
# что у reap_stale: второй порог правды не заводится.
STALE_UNCLAIMED_LABEL = "stale-unclaimed"


def _guard_raw_subprocess_write(description: str) -> bool:
    """True — вызывающий обязан выполнить реальный изменяющий вызов; False —
    пропустить его (описание уже напечатано warning'ом).

    Тот же класс и та же функция решения, что pulse_guard.gh()/send_telegram
    (2026-09-11, находка владельца: watchdog-issue #120 несёт 112 из 573
    комментариев от логина `mytab0r`, не `github-actions[bot]` — доказательство,
    что `python scheduler.py` реально запускается вне GitHub Actions личным
    токеном и способен слить PR/задиспетчить воркера в обход `concurrency:
    group: orchestra`). `gh()` гейтит СВОИ вызовы сама (класс закрыт там), но в
    scheduler.py есть ДВА места, где изменяющий вызов идёт В ОБХОД gh()
    (сырой `subprocess.run` — ORCHESTRA_PAT-путь update_branch, `gh workflow
    run` в dispatch_deploy_on_merge): точечный if внутри main() их не поймал
    бы (класс, не случай) — обе точки читают ЭТУ ЖЕ функцию prod_writes_
    allowed, что и gh(), а не заводят свою копию решения.

    ТРЕТЬЯ поверхность (находка AI-ревью PR #950): claim_task.release/
    release_full/collect_stale (`scripts/lib/claim_task.py`) несут СВОЙ gh() с
    собственным subprocess.run — тот же класс обхода, что и два места выше,
    только не в этом файле. claim_task — общая библиотека МНОГИХ каналов
    (task-branch/task.sh/dsh_task.sh claim'ят ЛОКАЛЬНО и по замыслу), поэтому
    её gh() не гейтится безусловно (это сломало бы штатный локальный claim) —
    вместо второй копии предиката claim_task.gh() получает ИНЪЕЦИРУЕМЫЙ хук
    (claim_task.set_write_guard), который ниже подключается именно к этой
    функции: единственная точка, где планировщик решает писать вне CI, и
    единственный потребитель хука."""
    if prod_writes_allowed():
        return True
    print(
        f"::warning::DRY-RUN (вне GitHub Actions, {ALLOW_PROD_WRITES_ENV} не задан) — "
        f"изменяющий вызов пропущен: {description}",
        file=sys.stderr,
    )
    return False


# claim_task — общая библиотека (см. её собственный gh(), докстринг
# set_write_guard) — гейт подключается ЗДЕСЬ ОДИН РАЗ, тем же предикатом, что
# использует остальной scheduler.py, а не второй копией.
claim_task.set_write_guard(_guard_raw_subprocess_write)

# ── Цикл слияний внутри одного прогона (#297) ────────────────────────────────
# Было буквально «ровно один PR за запуск» (см. шапку модуля, пункт 3, и
# merge_queue): расписание, на которое рассчитывал следующий запуск,
# доставляется ~7% тиков (docs/research/21-github-actions.md, замер
# 2026-09-04) — готовый к слиянию PR ждал часами. Пульс перешёл на
# событийный workflow_dispatch из pr-review.yml/ai-review.yml сразу по
# готовности PR (#297) — но событие приходит РЕДКО (по факту готовности), а
# очередь может содержать несколько готовых PR сразу (например, после того,
# как пульс молчал часами). Один прогон обязан суметь провести всю очередь,
# а не одного кандидата, иначе сериализация update_branch (#252/#288: один
# слот на проход) снова растягивает слияния на десяток отдельных прогонов —
# только событийных вместо периодических, разница без выгоды.
#
# Потолок — число слияний, а не время: одно слияние обычно стоит одного
# update_branch следующему кандидату (after_merge → update_remaining_pulls)
# плюс ожидания его проверок — оба бюджета ниже.
MERGE_LOOP_MAX_MERGES = 5
# Общий бюджет времени ЦИКЛА слияний одного прогона (не всего main(): пульс,
# дрейф пина, пул задач и диспатч воркера вокруг цикла считаются отдельно).
# Job живёт до 6 часов (docs/research/21), но держать concurrency-слот
# orchestra занятым долго блокирует следующее событие того же PR. 20 минут —
# запас на 2-3 итерации pr-review+ai-review при быстром пути diff_unchanged
# (#294): полный дорогой прогон AI-ревью (до 70 минут, ai-review.yml) сюда
# не уложится — тогда цикл честно останавливается по таймауту, а не убивает
# то ревью на середине, оно всё равно доводится своим job'ом.
MERGE_LOOP_TIMEOUT_SECONDS = 20 * 60
# Пауза между попытками цикла, пока идут проверки только что обновлённого
# PR — без неё цикл был бы busy-loop'ом, упирающимся во вторичный лимит
# GitHub API (80 запросов/мин, docs/research/21) без всякой пользы.
MERGE_LOOP_POLL_SECONDS = 30


def _issue_is_blocked(issue: dict) -> bool:
    return BLOCKED_LABEL in {label["name"] for label in issue.get("labels") or []}


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        with open(path, "a", encoding="utf-8") as file:
            file.write(text)


def open_task_issues(repo: str) -> list[dict]:
    # Пагинация (#308, тот же класс, что list_pr_files/list_timeline, #294/
    # #303): сырая первая страница `per_page=100` молча теряла хвост — при
    # 106 открытых задачах с меткой task (107 сырых записей issues на этой
    # выборке, одна из них — #248, сама PR под меткой task, отфильтрована
    # ниже по ключу pull_request; замер 2026-09-05, живой репозиторий —
    # `open_task_issues` и `search/issues?...&label:task` сошлись на 107)
    # воркер и планировщик не видели последние 7, без ошибки и без
    # предупреждения.
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(TASK_LABEL)}"
        "&per_page=100", gh)
    return [issue for issue in issues if "pull_request" not in issue]


def open_pulls(repo: str) -> list[dict]:
    # Тот же класс #308: список PR растёт тем же темпом, что и пул задач —
    # первая страница молча теряла бы хвост тем же образом.
    #
    # Дефект A (watchdog-issue #120, 2026-09-11): раньше list_pages на
    # неожиданной форме ответа (не-list — например тело вторичного
    # рейт-лимита GitHub) тихо возвращал уже накопленное (здесь — пустой
    # список на первой же странице), и wip_gate ниже по main() читал это как
    # «доработки нет», открывая диспатч новых задач на 27 реально открытых
    # PR, ждущих доработки. Теперь list_pages поднимает RuntimeError в этом
    # случае — main() (см. `if __name__ == "__main__"`) красит прогон и
    # выходит ДО того, как wip_gate/dispatch_worker увидят искажённый
    # снимок: пустой/некорректный список PR либо честная пустота (state=open
    # и правда даёт []), либо явный сбой — третьего («тихо открыть гейт») не
    # остаётся. Газ: orchestra.yml — в WATCHED_WORKFLOWS failure_watch
    # (#477), красный прогон сам заметен следующим успешным пульсом, а
    # транзиентный сбой (сеть/рейт-лимит) самолечится следующим прогоном
    # через 15 минут без ручного участия.
    return review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)


def pr_references_issue(pull: dict, issue_number: int) -> bool:
    # Намеренно широкая семантика — ЛЮБОЕ упоминание, не только ветка
    # (в отличие от contract_check.py, #195): используется ТОЛЬКО в reap_stale
    # ниже, чтобы не собрать замок с задачи, у которой открытый PR существует,
    # но ссылается на неё не веткой. Ошибиться в сторону «не трогать» тут
    # дешевле, чем в сторону «занята» — reap_stale в худшем случае оставит
    # чужую (в смысле «не эту») задачу висеть чуть дольше, замок ЧУЖОЙ задачи
    # эта ошибка не трогает.
    #
    # unhealthy_pulls (#286) эту функцию НЕ ИСПОЛЬЗУЕТ (см. её отдельный
    # докстринг ниже) — там цена той же широты другая: не «не освободили
    # вовремя», а «освободили ЧУЖУЮ задачу» по случайному упоминанию номера в
    # прозе тела PR. Живой случай: 2026-09-04 задача #90 дважды лишилась
    # аренды, потому что её номер был упомянут в теле совершенно другого PR
    # #181 (собственная задача которого, по ветке agent/179-…, — #179) —
    # широкий матч посчитал PR #181 «нездоровым PR задачи #90» и снял чужой
    # замок.
    #
    # После #394 тело PR не обязано называть номер вовсе (шаблон прямо
    # говорит, что строка «#N» — для человека, не источник истины), поэтому
    # чистого references_task(body) стало недостаточно: контракт-проходящий
    # PR с телом без единого номера был бы невидим для reap_stale. Добавляем
    # ветку как второй, столь же широкий признак.
    return (
        task_ref.references_task(pull.get("body") or "", issue_number)
        or task_ref.resolve_pr_task(pull) == issue_number
    )


def last_assigned_at(repo: str, number: int) -> datetime | None:
    """Момент последнего события `assigned` в таймлайне issue — `None`, если
    задачу никогда не назначали. Одно место правды (#815): reap_stale ниже и
    reap_stalled_worker_run (гвардия test_reap_stale_reads_timeline_through_
    paginated_helper требует РОВНО ОДНО вхождение review_labels.list_timeline
    в исходнике) читают один и тот же признак «когда взяли аренду» — второй
    независимый обход того же таймлайна не заводим.

    Пагинация (#308, тот же класс, что last_gate1_labeled_at/
    last_ready_labeled_at, #303): сырая первая страница таймлайна молча
    теряла событие assigned за первой сотней записей на длинном таймлайне."""
    timeline = review_labels.list_timeline(repo, number, gh)
    assigned_at = [event["created_at"] for event in timeline if event.get("event") == "assigned"]
    return parse_time(max(assigned_at)) if assigned_at else None


def reap_stale(
    repo: str, now: datetime, pulls: list[dict], merged: dict[int, dict] | None = None,
    *, pool: list[dict],
) -> list[str]:
    """pool — открытые задачи (open_task_issues(repo)) одного прогона (#443):
    раньше эта функция читала open_task_issues сама, отдельным HTTP-обходом
    от unhealthy_pulls/main(), хотя все три смотрят на один и тот же список в
    пределах одного прогона планировщика (между вызовами ничего, что меняет
    состав/assignees открытых задач, не происходит — heartbeat/дрейф пина/
    open_pulls/merged_pr_map их не трогают). Снятое здесь назначение
    отражается СРАЗУ в переданных объектах (issue["assignees"] обнуляется
    ниже) — тем же приёмом, что mark_conflicts уже применяет к pull["labels"]
    (см. _set_conflict_label): вызывающий код, отдавший тот же `pool` дальше
    (unhealthy_pulls, reject_reopened_tasks, accept_merged_tasks), видит
    актуальное состояние без второго запроса к GitHub."""
    lines = []
    merged = merged or {}
    for issue in pool:
        if not issue["assignees"]:
            continue
        if _issue_is_blocked(issue):
            continue
        number = issue["number"]
        if any(pr_references_issue(pull, number) for pull in pulls):
            continue
        last = last_assigned_at(repo, number)
        if last is None:
            continue
        merged_pull = merged.get(number)
        if merged_pull is not None and last <= parse_time(merged_pull["merged_at"]):
            # Работа уже слита — приёмку (#227, accept_merged_tasks ниже) ведёт
            # проверяемая улика, а не «PR не появился»: без этой проверки
            # reap_stale красноречиво врал именно так — часть задач из замера
            # #227 (#192, #189, #187…) оказались БЕЗ исполнителя ровно потому,
            # что их слитый PR закрылся и стал невидим для open_pulls, а через
            # STALE_HOURS reap_stale снял назначение с неверной причиной.
            # Гвард НЕ постоянный: сравниваем с временем ТЕКУЩЕГО назначения,
            # не с фактом «номер когда-либо встречался в слитом PR» — иначе
            # после провала приёмки и новой аренды та же задача была бы
            # невидима для reap навсегда (замечание AI-ревью, PR #253):
            # старый merged-PR остаётся в карте, а новое назначение (после
            # `last > merged_at`) обязано подчиняться обычному таймеру ниже.
            continue
        if now - last < timedelta(hours=STALE_HOURS):
            continue
        who = ", ".join(a["login"] for a in issue["assignees"])
        gh(
            "-X", "DELETE", f"repos/{repo}/issues/{number}/assignees",
            "-f", f"assignees[]={who}",
        )
        # Локальная мутация вслед за серверной (см. докстринг pool выше) —
        # дальнейшие потребители того же снимка обязаны увидеть снятие сразу.
        issue["assignees"] = []
        gh(
            "-X", "POST", f"repos/{repo}/issues/{number}/comments",
            # body уходит ЗНАЧЕНИЕМ аргумента "-f body=…" (форма gh api):
            # keyword-аргумент gh() не принимает и роняет весь прогон (#124).
            "-f", "body=" + (
                f"Назначение снято оркестратором: за {STALE_HOURS} часов не появился PR, "
                f"а задача назначена {who}. Задача возвращена в пул — бери через "
                "атомарную аренду: python3 scripts/lib/claim_task.py claim "
                f"{number} (#121, ADR 0006)."
            ),
        )
        lines.append(f"♻️ #{number} просрочена ({who}), возвращена в пул")
    return lines


def mark_stale_unclaimed(repo: str, now: datetime, pool: list[dict]) -> list[str]:
    """Видимость «никто не взял» (#427): reap_stale выше снимает только
    заброшенные НАЗНАЧЕННЫЕ задачи — задачу, которую вообще никто не брал,
    не отслеживает ничто (замер 2026-09-06: 84 из 109 открытых без
    исполнителя, 44/36/21 старше суток/двух/трёх). Метка `stale-unclaimed` —
    не тормоз: ничего не закрывается и не переназначается автоматически,
    она только делает факт видимым. Газ («взять / переформулировать /
    закрыть как отпавшую») — вручную, владелец или агент, наткнувшийся на
    метку: у машины нет признака «нужное или отпавшее», и это не
    изображается автоматикой.

    Порог — STALE_HOURS, тот же, что у reap_stale (не второй порог правды).
    Возраст считается от `created_at` issue, а не от точного момента, когда
    задача стала свободной: второе потребовало бы обхода timeline на каждую
    свободную задачу пула (доп. запрос на issue) — сознательный отказ ради
    нулевой цены: `pool` уже прочитан этим же прогоном main() для отчёта
    «Пул задач», новый обход Issues здесь не заводится. Плата за упрощение:
    задача, вернувшаяся в пул через reap_stale, помечается сразу (её
    created_at уже старше порога), а не после нового отсчёта — это ближе
    к цели механизма (видимость «висит без исполнителя»), чем к точному
    таймеру.

    Метка снимается сама, как только у задачи появляется исполнитель —
    следующий прогон видит issue["assignees"] непустым и убирает её."""
    lines = []
    for issue in pool:
        number = issue["number"]
        has_label = STALE_UNCLAIMED_LABEL in {label["name"] for label in issue.get("labels") or []}
        if issue["assignees"]:
            if has_label:
                gh("-X", "DELETE", f"repos/{repo}/issues/{number}/labels/{STALE_UNCLAIMED_LABEL}")
                lines.append(f"✅ #{number} взята в работу — {STALE_UNCLAIMED_LABEL} снята")
            continue
        if _issue_is_blocked(issue):
            continue  # blocked уже сигнализирует владельцу отдельно, не дублируем
        if has_label:
            continue
        age_hours = minutes_between(parse_time(issue["created_at"]), now) / 60
        if age_hours < STALE_HOURS:
            continue
        gh("-X", "POST", f"repos/{repo}/issues/{number}/labels",
           "-f", f"labels[]={STALE_UNCLAIMED_LABEL}")
        lines.append(f"🏷️ #{number} без исполнителя {int(age_hours)}ч — {STALE_UNCLAIMED_LABEL}")
    return lines


def _set_conflict_label(repo: str, pull: dict, *, present: bool) -> None:
    """Единственное место, которое меняет метку CONFLICT_LABEL сразу и на
    сервере, и в переданном объекте `pull` — одним действием, а не двумя
    независимыми (класс «устаревшая метка в памяти», найден 2026-09-04:
    mark_conflicts снимала/ставила метку через gh, но `pull["labels"]`
    оставался прежним, и тот же объект уходил дальше в merge_queue/
    update_remaining_pulls, где review_labels.should_update_branch читал уже
    неактуальное состояние). Вызывающий код не может забыть обновить
    `pull["labels"]` отдельно — этой возможности здесь просто нет."""
    labels = pull["labels"]
    has = any(label["name"] == CONFLICT_LABEL for label in labels)
    if present and not has:
        gh("-X", "POST", f"repos/{repo}/issues/{pull['number']}/labels", "-f", f"labels[]={CONFLICT_LABEL}")
        labels.append({"name": CONFLICT_LABEL})
    elif not present and has:
        gh("-X", "DELETE", f"repos/{repo}/issues/{pull['number']}/labels/{CONFLICT_LABEL}")
        pull["labels"] = [label for label in labels if label["name"] != CONFLICT_LABEL]


def mark_conflicts(repo: str, pulls: list[dict]) -> list[str]:
    lines = []
    for pull in pulls:
        # mergeable_state живёт только на endpoint'е одиночного PR: в списке он
        # всегда отсутствует, и доверие ему — тихая потеря всех кандидатов.
        single = gh(f"repos/{repo}/pulls/{pull['number']}")
        state = single.get("mergeable_state")
        labels = {label["name"] for label in pull["labels"]}
        if state != "dirty":
            # Газ (#270): раньше метка не снималась никогда. Снимаем только по
            # явно неконфликтным состояниям (review_labels.CONFLICT_CLEAR_STATES);
            # None/"unknown" не в счёт — «не знаю» не значит «нет конфликта».
            if CONFLICT_LABEL in labels and state in review_labels.CONFLICT_CLEAR_STATES:
                _set_conflict_label(repo, pull, present=False)
                lines.append(f"✅ PR #{pull['number']}: метка `conflict` снята (mergeable_state={state})")
            continue
        if CONFLICT_LABEL in labels:
            continue
        _set_conflict_label(repo, pull, present=True)
        rivals = ", ".join(f"#{other['number']}" for other in pulls if other["number"] != pull["number"]) or "нет"
        gh(
            "-X", "POST", f"repos/{repo}/issues/{pull['number']}/comments",
            "-f", "body=" + (
                f"PR конфликтует с main (открытые конкуренты: {rivals}). "
                # Текст переписан под #474: раньше звал человека перебазировать
                # руками ("Перебазируй … — оркестратор подхватит"), хотя ничего
                # не подхватывало — dispatch_conflict_rework ниже теперь и есть
                # тот, кто подхватывает, следующим проходом (до 15 мин, cron
                # orchestra.yml).
                "Оркестратор запустит адресный авто-ребейз воркером следующим "
                "проходом (до 15 мин). Не сойдётся с первой попытки — эскалация "
                f"владельцу в #{WATCHDOG_ISSUE}."
            ),
        )
        lines.append(f"⚠️ PR #{pull['number']} помечен `conflict`")
    return lines


def conflict_overlap_hint(repo: str, pull: dict) -> str:
    """Эвристика «какие файлы могли столкнуться» — для текста эскалации, не
    для решения о дальнейших действиях. GitHub REST отдаёт только
    mergeable_state (dirty/clean), не сами конфликтующие ханки, а
    scheduler.py принципиально не заводит локальный git-клон (единственный
    источник состояния здесь — gh api, см. докстринг модуля). Поэтому здесь —
    ПЕРЕСЕЧЕНИЕ файлов, изменённых PR, и файлов, изменённых в main с момента
    base-коммита PR: не точные конфликтующие строки, но достаточно, чтобы
    человек не гадал с нуля. Пустая строка — определить не удалось (нет
    base.sha или сбой API) — вызывающий код обязан сказать это честно
    (AGENTS.md: «не знаешь — пиши «не подтверждено»»), не выдать пустоту за
    «пересечения нет»."""
    base_sha = ((pull.get("base") or {}).get("sha")) or ""
    if not base_sha:
        return ""
    try:
        pr_files = {f["filename"] for f in review_labels.list_pr_files(repo, pull["number"], gh)}
        compare = gh(f"repos/{repo}/compare/{base_sha}...main")
        main_files = {f["filename"] for f in (compare or {}).get("files") or []}
    except RuntimeError:
        return ""
    return ", ".join(sorted(pr_files & main_files))


def conflict_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Момент последней простановки метки CONFLICT_LABEL (тот же приём, что
    last_gate1_labeled_at: max(), не min() — метка могла сниматься/ставиться
    несколькими эпизодами конфликта, нас интересует начало ТЕКУЩЕГО). Возраст
    ИМЕННО эпизода конфликта, не возраст PR (issue #588, обход старых
    конфликтов первыми в dispatch_conflict_rework): PR мог быть открыт неделю
    назад и стать dirty только сегодня — created_at PR тут соврал бы про
    голодание. None — событие не нашлось (таймлайн не отдал его / метки нет
    вовсе).

    Только для СОРТИРОВКИ очереди по возрасту — не путать с границей бюджета
    conflict_rework_attempts (conflict_first_labeled_at ниже, min() вместо
    max(): там нужна лифтайм-граница, не сбрасывающаяся по эпизодам)."""
    timeline = review_labels.list_timeline(repo, pr_number, gh)
    labeled_at = [
        event["created_at"] for event in timeline
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") == CONFLICT_LABEL
    ]
    return parse_time(max(labeled_at)) if labeled_at else None


def conflict_first_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Момент ПЕРВОЙ простановки метки CONFLICT_LABEL за всю историю PR —
    min(), не max() (в отличие от conflict_labeled_at выше, который нарочно
    даёт старт ТЕКУЩЕГО эпизода — для сортировки очереди по возрасту).

    Это граница лифтайм-бюджета conflict_rework_attempts (находка ревью PR
    #597): владелец решил (issue #474, зафиксировано в дельта-спеке
    openspec/changes/conflict-auto-rebase/specs/journal-tasks-hands/spec.md —
    требование про бюджет попыток — и в docs/agents/LABELS.md, строка
    `conflict`) — РОВНО одна авто-попытка ребейза на PR, счётчик
    НЕ сбрасывается по эпизодам конфликта (тот же компромисс, что уже принят
    для AI_REVIEW_MAX_ATTEMPTS). max() как граница обнулял бы бюджет каждый
    раз, когда mark_conflicts снимает метку и ставит её заново на новом
    дрейфе — min() лифтайм сохраняет. Защита от прогона-открывателя (см.
    conflict_rework_attempts) при этом не теряется: даже первая простановка
    метки случается уже ПОСЛЕ того, как PR создан и исходный прогон
    завершился."""
    timeline = review_labels.list_timeline(repo, pr_number, gh)
    labeled_at = [
        event["created_at"] for event in timeline
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name") == CONFLICT_LABEL
    ]
    return parse_time(min(labeled_at)) if labeled_at else None


def conflict_rework_attempts(repo: str, pr_number: int, task_number: int) -> int:
    """Число ЗАСЧИТАННЫХ попыток авто-ребейза (issue #588) — не сырых
    диспатчей. CONFLICT_REWORK_MARKER ставится СРАЗУ на dispatch (защита от
    гонки, см. докстринг dispatch_conflict_rework) как заметка для человека,
    читающего PR, — это момент диспатча, не факт настоящей попытки, и она
    больше не читается обратно этой функцией: замер живых случаев
    (#567/#542/#408) — все три упали ДО git-шага (сеть/деплой морды,
    отсутствующий скрипт), а бюджет CONFLICT_REWORK_MAX_ATTEMPTS=1 сгорал на
    инфраструктуре, ни разу не дойдя до git rebase.

    Засчитывается только прогон, САМ отметивший, что дошёл до git-шага:
    комментарий «WORKER_GIT_STEP_MARKER worker run N» в комментариях ЗАДАЧИ
    (scripts/worker/task.sh, ставится непосредственно перед прогоном DSH —
    единственный устойчивый признак «агент реально получил промпт и мог
    попытаться», не текст ошибки, тот протухнет при первой смене
    формулировки). Атрибуция задаче — самим носителем: комментарий живёт в
    той задаче, которую воркер получил входом `task`, чужую он не
    комментирует.

    Читаются КОММЕНТАРИИ, а не окно recent_runs(per_page=10) (блокирующая
    находка ревью PR #597): засчитанная попытка выпадала из топ-10 свежих
    прогонов worker.yml уже через ~2–3 часа живой истории (~3.3 слияния/час,
    плюс ретраи и доводки — каждый даёт свой прогон), а попытка легитимно
    живёт до 280 минут. Потерянная засчитанная попытка возвращала бы
    attempts=0 на каждом следующем тике: PR с несошедшейся попыткой
    получал бы адресный диспатч бесконечно, ни разу не дойдя до эскалации, —
    голодание #588 перестроилось бы, а не ушлило. Комментарий git-шага —
    постоянный след (id прогона + created_at в одном комментарии), его и
    читаем; с границей ниже сравнивается время КОММЕНТАРИЯ, а не начала
    прогона — отметка ставится в начале DSH-шага, расхождение минутное
    (честная цена: прогон, стартовавший ДО простановки метки, но дошедший
    до git-шага ПОСЛЕ неё, засчитается; PR-открывающий прогон так попасть
    не может — свою отметку он ставит раньше, чем создаёт сам PR).

    since = conflict_first_labeled_at(...), не conflict_labeled_at (находка
    ревью PR #597): граница обязана быть лифтайм-, не по-эпизодной — иначе
    снятие+повторная простановка метки (новый эпизод дрейфа) обнуляла бы
    засчитанные попытки в обход задокументированного решения владельца
    (#474). Без границы вовсе исходный прогон, ОТКРЫВШИЙ этот PR задолго до
    того, как main ушёл вперёд (тот прогон тоже дошёл до git-шага — он и есть
    источник PR), был бы ошибочно засчитан как попытка авто-РЕБЕЙЗА: его
    отметка всегда старше первой простановки метки (PR создаётся этим же
    прогоном ПОЗЖЕ отметки, метка приходит снаружи и того позже). Нет
    метки/события — 0 (не «неизвестно считаем исчерпанным»: PR остаётся
    доступным для дальнейшей обработки, смотри также docstring
    dispatch_conflict_rework — ниже эта же величина участвует в решении на
    равных с worker_runs_active).

    Сброс бюджета (issue #822, авария #794): CONFLICT_BUDGET_RESET_MARKER в
    комментариях той же ЗАДАЧИ (второй канал чтения не заводится — тот же
    all_issue_comments(task_number), что уже читается ниже) сдвигает границу
    отсчёта вперёд на момент своей последней публикации — max(), а не min()
    (симметрично conflict_first_labeled_at выше: та граница лифтайм-, эта её
    осознанно СМЕЩАЕТ по решению владельца, не отменяет). Маркера нет —
    поведение не меняется вовсе (since остаётся conflict_first_labeled_at).
    Идемпотентность — по конструкции issue_marker_times: несколько маркеров
    сброса берутся max()'ом времени, не суммируются в накопленный сдвиг;
    попытки ДО эффективной границы (в т.ч. настоящие пост-аварийные, если
    маркер сброса опубликован задним числом ошибочно) этой функцией не
    просто игнорируются — они и есть то, что маркер обязан выкинуть из
    подсчёта, это и есть его смысл."""
    since = conflict_first_labeled_at(repo, pr_number)
    if since is None:
        return 0
    reset_times = issue_marker_times(repo, task_number, CONFLICT_BUDGET_RESET_MARKER)
    if reset_times:
        since = max(since, max(reset_times))
    git_step_run = re.compile(
        rf"{re.escape(WORKER_GIT_STEP_MARKER)}.*worker run (\d+)(?!\d)"
    )
    counted: set[str] = set()
    for comment in all_issue_comments(repo, task_number):
        created_at = comment.get("created_at")
        if not created_at or parse_time(created_at) < since:
            continue
        match = git_step_run.search(comment.get("body") or "")
        if match:
            counted.add(match.group(1))  # сет: повторная отметка того же прогона — не вторая попытка
    return len(counted)


def dispatch_conflict_rework(
    repo: str, pulls: list[dict], *, pool: list[dict],
) -> tuple[list[str], list[str], bool]:
    """Расшивка конфликта не должна ждать человека (issue #474): PR с меткой
    conflict (mark_conflicts выше) почти всегда просто отстал от main
    (механический дрейф, ~55 слияний/сутки на живом репозитории) — git
    rebase origin/main решает это без содержательного решения. Путь
    переиспользован целиком, второй не изобретается: worker.yml уже
    принимает вход `task` (#245/#394 — доводка существующего открытого PR,
    task.sh: checkout ветки, запрет второго PR, тот же промпт, что и для
    ai:changes-requested/красного чека), а снятие assignee+замка задачи —
    тот же приём, что unhealthy_pulls уже применяет к нездоровым PR
    НЕ-conflict классов (conflict там намеренно исключён — «свой цикл
    ручного разрешения»; эта функция и есть тот цикл, теперь автоматический).

    Признака «механический дрейф или содержательный конфликт» ДО попытки не
    существует: GitHub REST отдаёт только mergeable_state, не конфликтующие
    ханки. Поэтому решение простое (владелец, issue #474): РОВНО одна
    авто-попытка ребейза на PR (CONFLICT_REWORK_MAX_ATTEMPTS=1, лифтайм-
    счётчик — conflict_rework_attempts выше, граница conflict_first_labeled_at,
    — НЕ сбрасывается по эпизодам конфликта, тот же компромисс, что уже
    принят для AI_REVIEW_MAX_ATTEMPTS; CONFLICT_REWORK_MARKER в комментарии
    PR ниже — только заметка для человека на момент диспатча, счётчик её не
    перечитывает). Сошлось —
    mark_conflicts снимет метку сама следующим проходом (это и есть признак
    «был дрейф», ПОСТфактум); не сошлось — эскалация владельцу (escalate,
    тот же канал, что предохранитель конвейера #120) с файлами-кандидатами
    (conflict_overlap_hint — эвристика, честно помечена как таковая).

    Идемпотентность — worker_runs_active (тот же гейт, что dispatch_worker):
    воркер один на репозиторий, пока прошлый прогон жив (in_progress/
    queued), второй dispatch не уходит. Третий элемент возвращаемого
    кортежа (dispatched) — сигнал main() не звать следом обычный
    dispatch_worker в этом же проходе: «ровно один workflow_dispatch воркера
    за пульс» не должно превратиться в два только из-за гонки — GitHub не
    гарантирует, что только что созданный прогон немедленно виден как
    queued в следующем же запросе статуса.

    Порядок обхода — от старейшего конфликта к новейшему (issue #588): сырой
    порядок `pulls` (open_pulls(), `GET /pulls?state=open`) отдаёт НОВЫЕ PR
    первыми, а один workflow_dispatch за проход (см. выше) раньше всегда
    доставался самому свежему конфликту — замер живого репозитория: 10 из 14
    конфликтных PR не получили ни одной попытки, возраст конфликта до 84
    часов. conflict_labeled_at (момент простановки CONFLICT_LABEL), не
    created_at PR: PR мог быть открыт неделю назад и стать dirty только
    сегодня — возраст самого PR тут соврал бы про голодание, honest сигнал —
    возраст ИМЕННО эпизода конфликта."""
    observations: list[str] = []
    actions: list[str] = []
    dispatched = False
    pool_by_number = {issue["number"]: issue for issue in pool}
    conflict_pulls = [
        p for p in pulls
        if CONFLICT_LABEL in {label["name"] for label in p["labels"]}
    ]
    # task_ref.resolve_pr_task — вычисление ЛОКАЛЬНОЕ (regex по имени ветки
    # agent/N-slug), не HTTP-вызов: PR без объявленной задачи не может
    # участвовать в дальнейшем цикле (ветка ниже сразу делает `continue`)
    # — дешёвая фильтрация раньше дорогой (conflict_labeled_at — вызов
    # timeline), а не только экономии ради: без неё PR, для которого нет
    # смысла звать API вовсе, всё равно платил бы за сортировку.
    schedulable, unscheduled = [], []
    for p in conflict_pulls:
        (schedulable if task_ref.resolve_pr_task(p) is not None else unscheduled).append(p)
    schedulable.sort(
        key=lambda p: conflict_labeled_at(repo, p["number"]) or parse_time(p["created_at"])
    )
    conflict_numbers = {p["number"] for p in conflict_pulls}
    ordered_pulls = unscheduled + schedulable + [p for p in pulls if p["number"] not in conflict_numbers]
    for pull in ordered_pulls:
        labels = {label["name"] for label in pull["labels"]}
        if CONFLICT_LABEL not in labels:
            continue
        number = pull["number"]
        task_number = task_ref.resolve_pr_task(pull)
        if task_number is None:
            observations.append(
                f"⚠️ PR #{number} в конфликте, но ветка не называет задачу "
                "(agent/N-slug) — авто-расшивка недоступна, нужен человек"
            )
            continue
        attempts = conflict_rework_attempts(repo, number, task_number)
        if attempts >= CONFLICT_REWORK_MAX_ATTEMPTS:
            # Гонка (найдена живым прогоном #474, PR #408: маркер попытки
            # ставится СРАЗУ на dispatch, а сам worker.yml идёт до 280 мин) —
            # без этой проверки следующий тик оркестратора (каждые 15 мин)
            # увидел бы attempts >= порога ДО того, как единственная попытка
            # вообще успела завершиться, и эскалировал бы «не сошлось», хотя
            # прогон ещё идёт. worker_runs_active — тот же гейт, что ниже:
            # пока прогон жив, эскалация ждёт, не дублирует dispatch и не
            # торопится с вердиктом.
            if dispatched or worker_runs_active(repo):
                observations.append(
                    f"⏸️ PR #{number}: авто-попытка ребейза ещё идёт (worker.yml активен) — "
                    "решение об эскалации отложено"
                )
                continue
            marker = f"{CONFLICT_ESCALATION_MARKER} #{number}"
            try:
                already = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
            except RuntimeError as error:
                observations.append(f"⚠️ не смог сверить маркер эскалации конфликта #{number}: {error}")
                continue
            if already:
                continue  # уже эскалировано этим эпизодом — не спамим, ждём владельца
            # Находка ревью PR #478: метка `conflict` намеренно переживает
            # mergeable_state None/unknown (mark_conflicts — «„не знаю“ не
            # значит „нет конфликта“»), значит на момент эскалации PR МОГ уже
            # успешно перебазироваться (ребейз прошёл, прогон кончился), а
            # пересчёт mergeable ещё не вернулся из None/unknown в явное
            # состояние — метка честно ещё стоит, но факта «остаётся dirty»
            # уже нет. Без перепроверки эскалация соврала бы «содержательный
            # конфликт», а маркер CONFLICT_ESCALATION_MARKER подавил бы её
            # навсегда для этого PR. Один дешёвый вызов в редкой ветке —
            # перечитать актуальный mergeable_state перед тем, как объявлять
            # факт, а не доверять метке, которая по своей природе может
            # отставать.
            single = gh(f"repos/{repo}/pulls/{number}")
            state = single.get("mergeable_state")
            if state != "dirty":
                observations.append(
                    f"⏸️ PR #{number}: mergeable_state={state!r} не подтверждён как dirty — "
                    "эскалация отложена (mark_conflicts разберётся со снятием метки следующим проходом)"
                )
                continue
            overlap = conflict_overlap_hint(repo, pull)
            overlap_text = overlap or "не удалось определить (см. PR вручную)"
            # Находка ревью PR #478 ("алерт не гадает", AGENTS.md, тот же
            # класс, что инвариант 3/#472): единственный ПОДТВЕРЖДЁННЫЙ факт
            # здесь — mergeable_state=dirty после одной ЗАСЧИТАННОЙ попытки
            # (conflict_rework_attempts, #588: сбой ДО git-шага сюда уже не
            # доходит — он не увеличивает attempts, PR получает новый
            # адресный dispatch вместо эскалации). Причину отсюда всё ещё не
            # различить: квота провайдера, таймаут 280 минут или неудавшийся
            # push ПОСЛЕ того, как агент начал работу, дают тот же итог, что
            # настоящий содержательный конфликт. Текст называет ФАКТ
            # (conclusion последнего прогона, атрибутированного этой задаче),
            # не утверждает причину.
            run_conclusion = last_worker_run_conclusion(repo, task_number)
            run_note = (
                f"последний прогон worker.yml по этой задаче завершился с conclusion={run_conclusion!r}"
                if run_conclusion is not None
                else "прогон worker.yml по этой задаче не атрибутирован (аренда сгорела до следа?) — см. лог worker.yml вручную"
            )
            text = (
                f"🚨 edge-harness: {marker}\n"
                f"PR #{number} (задача #{task_number}) остаётся dirty после {attempts} "
                f"авто-попытки ребейза worker.yml ({run_note}). Причина отсюда не различается "
                "(инфраструктурный сбой воркера/квота/таймаут дают тот же итог, что настоящий "
                "содержательный конфликт) — нужно решение владельца: посмотреть лог последнего "
                f"прогона и разобраться. Файлы-кандидаты (пересечение изменений PR и main, "
                f"не точные конфликтующие строки): {overlap_text}."
            )
            escalation = escalate(repo, WATCHDOG_ISSUE, text)
            actions.append(
                f"🚨 PR #{number}: авто-расшивка конфликта исчерпана ({attempts}/"
                f"{CONFLICT_REWORK_MAX_ATTEMPTS}) — эскалация владельцу ({escalation})"
            )
            continue
        issue = pool_by_number.get(task_number)
        if issue is None:
            observations.append(
                f"⚠️ PR #{number} в конфликте, задача #{task_number} не найдена в открытом пуле "
                "— авто-расшивка недоступна"
            )
            continue
        if _issue_is_blocked(issue):
            continue  # эскалация playbook уже идёт своим путём — не мешаем ей
        if dispatched or worker_runs_active(repo):
            observations.append(f"⏸️ PR #{number} в конфликте, но воркер занят — расшивка отложена")
            continue
        if issue["assignees"]:
            who = ", ".join(a["login"] for a in issue["assignees"])
            gh("-X", "DELETE", f"repos/{repo}/issues/{task_number}/assignees", "-f", f"assignees[]={who}")
            # Мутация pool сразу вслед за серверной (тот же приём, что
            # reap_stale/unhealthy_pulls) — потребители этого же снимка
            # видят актуальное состояние без второго запроса.
            issue["assignees"] = []
        try:
            release_note = claim_task.release(repo, int(task_number))
        except RuntimeError as error:
            release_note = f"замок не снят: {error}"
        gh(
            "-X", "POST", f"repos/{repo}/actions/workflows/worker.yml/dispatches",
            "-f", "ref=main", "-f", f"inputs[task]={task_number}",
        )
        post_issue_comment(
            repo, number,
            f"🤖 {CONFLICT_REWORK_MARKER} Оркестратор снял назначение с задачи #{task_number} "
            f"и запустил worker.yml адресно (попытка {attempts + 1}/{CONFLICT_REWORK_MAX_ATTEMPTS}): "
            "main ушёл вперёд, git rebase origin/main почти всегда решает такой конфликт сам.",
        )
        actions.append(
            f"🔧 PR #{number} в конфликте — задача #{task_number} освобождена ({release_note}), "
            f"worker.yml запущен адресно на авто-ребейз (попытка {attempts + 1}/"
            f"{CONFLICT_REWORK_MAX_ATTEMPTS})"
        )
        dispatched = True
    return observations, actions, dispatched


# ── Дефект B: доводка PR по находкам AI-ревью (по образцу dispatch_conflict_
# rework выше, тот же механизм — не второй канал) ────────────────────────────
#
# Диагноз: `ai:changes-requested` производится самым дорогим циклом
# репозитория (~50 прогонов ai-review.yml/сутки, живой вызов модели), гейт
# слияния читает только МЕТКУ (review_labels.merge_label_gate), а прозу
# находок не читает никто — PR ждёт человека или агента, который случайно
# откроет именно этот PR. Живой замер: 23 открытых PR с ai:changes-requested,
# старейшие #241/#261/#262 висят с 2026-09-03 (восемь суток на момент задачи).
#
# Порядок в main() (см. вызов ниже): ПОСЛЕ dispatch_conflict_rework, ДО
# wip_gate/dispatch_worker — доводка уже открытых PR обязана иметь приоритет
# перед взятием НОВЫХ задач (это прямо связано с дефектом A: WIP-гейт душит
# новые задачи именно потому, что открытых PR, ждущих доработки, много —
# нет смысла плодить тридцать первую, пока не разгребается существующая
# очередь). Conflict — раньше ai-rework: PR с ОБЕИМИ метками (conflict И
# ai:changes-requested) обслуживает только dispatch_conflict_rework в этом
# пульсе — расшивка дрейфа main могла бы сама изменить дифф и снять
# ai:changes-requested следующим прогоном ревью, а task.sh (режим доводки
# #245) в любом случае даёт агенту команду прочитать ПОСЛЕДНИЙ вердикт PR
# (`gh pr view --comments`), не привязан к тому, ЧЕМ именно был вызван —
# второго диспатча на тот же PR за тот же пульс это не требует.

def ai_rework_dispatched_at(repo: str, pr_number: int, fingerprint: str) -> datetime | None:
    """Момент, когда авто-доводка по находкам ai-review уже запускалась НА
    ЭТОМ ТОЧНОМ отпечатке диффа (AI_REWORK_MARKER + `fp:{fingerprint}` в
    комментариях PR — тот же канал, что CONFLICT_REWORK_MARKER, только ключ
    не время, а отпечаток). None — на этом отпечатке ещё не пробовали
    (новый пуш с реальной правкой даёт новый отпечаток и чистый бюджет —
    см. блок-комментарий выше про отличие от conflict_first_labeled_at)."""
    marker = f"{AI_REWORK_MARKER} fp:{fingerprint}"
    times = issue_marker_times(repo, pr_number, marker)
    return max(times) if times else None


def ai_rework_attempts(repo: str, pr_number: int, task_number: int, fingerprint: str) -> int:
    """Число ЗАСЧИТАННЫХ попыток авто-доводки НА ЭТОМ ОТПЕЧАТКЕ — тот же
    приём защиты от гонки/инфраслучайных провалов, что
    conflict_rework_attempts выше: засчитывается только прогон, САМ
    отметивший, что дошёл до git-шага (WORKER_GIT_STEP_MARKER в комментариях
    ЗАДАЧИ), не сам факт диспатча (тот ставится сразу, до всякой реальной
    попытки — заметка для человека, не источник счётчика)."""
    since = ai_rework_dispatched_at(repo, pr_number, fingerprint)
    if since is None:
        return 0
    git_step_run = re.compile(
        rf"{re.escape(WORKER_GIT_STEP_MARKER)}.*worker run (\d+)(?!\d)"
    )
    counted: set[str] = set()
    for comment in all_issue_comments(repo, task_number):
        created_at = comment.get("created_at")
        if not created_at or parse_time(created_at) < since:
            continue
        match = git_step_run.search(comment.get("body") or "")
        if match:
            counted.add(match.group(1))
    return len(counted)


def dispatch_ai_review_rework(
    repo: str, pulls: list[dict], *, pool: list[dict],
) -> tuple[list[str], list[str], bool]:
    """Адресный диспатч worker.yml на доводку PR по находкам ai-review —
    по образцу dispatch_conflict_rework (issue #474): снятие assignee+замка
    задачи, worker.yml с input `task` (task.sh режим доводки #245, тот же
    промпт, что уже используют conflict/красный чек), бюджет попыток +
    эскалация владельцу тем же каналом (#120), что и предохранитель
    конвейера.

    Бюджет — AI_REWORK_MAX_ATTEMPTS попыток НА ОТПЕЧАТОК диффа
    (ai_rework_attempts), не лифтайм на PR: изменившийся отпечаток — это
    новая, ещё не пробованная задача (агент реально что-то поменял), сгоревшая
    попытка на СТАРОМ отпечатке не должна её блокировать. Исчерпание бюджета
    на текущем отпечатке (диспатч не сдвинул код — тот же класс инфрасбоя,
    что #588 у conflict) эскалирует, не диспетчит второй раз вслепую.

    Дедуп «тот же head, тот же отпечаток находок» — ai_rework_dispatched_at:
    неизменный отпечаток при уже висящем маркере не даёт второй, дублирующий
    dispatch за тот же пульс/следующие пульсы, пока не исчерпан бюджет.

    Conflict-PR исключены (см. блок-комментарий у модуля выше) —
    dispatch_conflict_rework уже обслуживает их, второго диспатча на тот же
    PR за пульс это не даёт.

    Идемпотентность — worker_runs_active, тот же гейт, что и у
    dispatch_conflict_rework/dispatch_worker: воркер один на репозиторий,
    пока прошлый прогон жив. Третий элемент кортежа (dispatched) — сигнал
    main() не звать следом dispatch_worker в этом же проходе."""
    observations: list[str] = []
    actions: list[str] = []
    dispatched = False
    pool_by_number = {issue["number"]: issue for issue in pool}
    for pull in pulls:
        labels = {label["name"] for label in pull["labels"]}
        if review_labels.AI_CHANGES not in labels:
            continue
        if CONFLICT_LABEL in labels:
            continue  # своя очередь — dispatch_conflict_rework (см. блок-комментарий выше)
        number = pull["number"]
        task_number = task_ref.resolve_pr_task(pull)
        if task_number is None:
            observations.append(
                f"⚠️ PR #{number} несёт ai:changes-requested, но ветка не называет "
                "задачу (agent/N-slug) — авто-доводка недоступна, нужен человек"
            )
            continue
        try:
            files = review_labels.list_pr_files(repo, number, gh)
        except RuntimeError as error:
            observations.append(
                f"⚠️ PR #{number}: не удалось прочитать файлы для отпечатка находок: {error}"
            )
            continue
        fingerprint = review_labels.diff_fingerprint(files)
        try:
            attempts = ai_rework_attempts(repo, number, task_number, fingerprint)
        except RuntimeError as error:
            observations.append(f"⚠️ PR #{number}: не удалось сверить бюджет авто-доводки: {error}")
            continue
        if attempts >= AI_REWORK_MAX_ATTEMPTS:
            if dispatched or worker_runs_active(repo):
                observations.append(
                    f"⏸️ PR #{number}: авто-доводка по находкам ai-review ещё идёт "
                    "(worker.yml активен) — решение об эскалации отложено"
                )
                continue
            marker = f"{AI_REWORK_ESCALATION_MARKER} #{number} fp:{fingerprint[:12]}"
            try:
                already = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
            except RuntimeError as error:
                observations.append(f"⚠️ не смог сверить маркер эскалации доводки #{number}: {error}")
                continue
            if already:
                continue  # уже эскалировано на этом отпечатке — не спамим, ждём владельца
            # Перепроверка (тот же приём, что dispatch_conflict_rework перед
            # эскалацией конфликта): метка могла уже смениться между снимком
            # `pulls` и этим моментом — эскалировать «застрял» на PR, который
            # ai-review уже одобрил, было бы ложью.
            single = gh(f"repos/{repo}/pulls/{number}")
            current_labels = {label["name"] for label in single.get("labels") or []}
            if review_labels.AI_CHANGES not in current_labels:
                observations.append(
                    f"⏸️ PR #{number}: ai:changes-requested уже снят — эскалация не нужна"
                )
                continue
            run_conclusion = last_worker_run_conclusion(repo, task_number)
            run_note = (
                f"последний прогон worker.yml по этой задаче завершился с conclusion={run_conclusion!r}"
                if run_conclusion is not None
                else "прогон worker.yml по этой задаче не атрибутирован (аренда сгорела до следа?) — см. лог worker.yml вручную"
            )
            text = (
                f"🚨 edge-harness: {marker}\n"
                f"PR #{number} (задача #{task_number}) остаётся с ai:changes-requested "
                f"на том же отпечатке диффа после {attempts} авто-попытки доводки "
                f"worker.yml ({run_note}). Причина отсюда не различается (инфраструктурный "
                "сбой воркера/квота/таймаут дают тот же итог, что незакрытые находки) — "
                "нужно решение владельца: посмотреть лог последнего прогона и находки "
                "ai-review (gh pr view --comments) и разобраться руками."
            )
            escalation = escalate(repo, WATCHDOG_ISSUE, text)
            actions.append(
                f"🚨 PR #{number}: авто-доводка по находкам ai-review исчерпана "
                f"({attempts}/{AI_REWORK_MAX_ATTEMPTS} на этом отпечатке) — эскалация "
                f"владельцу ({escalation})"
            )
            continue
        issue = pool_by_number.get(task_number)
        if issue is None:
            observations.append(
                f"⚠️ PR #{number}: задача #{task_number} не найдена в открытом пуле — "
                "авто-доводка недоступна"
            )
            continue
        if _issue_is_blocked(issue):
            continue  # эскалация playbook уже идёт своим путём — не мешаем ей
        if dispatched or worker_runs_active(repo):
            observations.append(
                f"⏸️ PR #{number} ждёт доводки по находкам ai-review, но воркер занят — отложено"
            )
            continue
        if issue["assignees"]:
            who = ", ".join(a["login"] for a in issue["assignees"])
            gh("-X", "DELETE", f"repos/{repo}/issues/{task_number}/assignees", "-f", f"assignees[]={who}")
            issue["assignees"] = []
        try:
            release_note = claim_task.release(repo, int(task_number))
        except RuntimeError as error:
            release_note = f"замок не снят: {error}"
        gh(
            "-X", "POST", f"repos/{repo}/actions/workflows/worker.yml/dispatches",
            "-f", "ref=main", "-f", f"inputs[task]={task_number}",
        )
        post_issue_comment(
            repo, number,
            f"🤖 {AI_REWORK_MARKER} fp:{fingerprint} Оркестратор снял назначение с задачи "
            f"#{task_number} и запустил worker.yml адресно (попытка {attempts + 1}/"
            f"{AI_REWORK_MAX_ATTEMPTS} на этом отпечатке диффа) на доводку по находкам "
            "ai-review — см. gh pr view --comments.",
        )
        actions.append(
            f"🔧 PR #{number} с ai:changes-requested — задача #{task_number} освобождена "
            f"({release_note}), worker.yml запущен адресно на доводку (попытка "
            f"{attempts + 1}/{AI_REWORK_MAX_ATTEMPTS})"
        )
        dispatched = True
    return observations, actions, dispatched


class UpdateBranchBudgetExhausted(RuntimeError):
    """Бюджет update_branch этого ПРОХОДА исчерпан (см. update_branch, #252,
    третий заход; терминология уточнена в #297 — прогон планировщика теперь
    может состоять из нескольких проходов, см. merge_loop; #761 — потолок
    поднят со «слот на один успех» до счётчика UPDATE_BRANCH_BUDGET_PER_PASS)
    — не инфраструктурный сбой, вызывающий код обязан поймать её отдельно и
    написать строку "подтянет следующий проход", а не смешивать с реальными
    ошибками update-branch (конфликт, сеть)."""


class AiReviewRunning(RuntimeError):
    """update_branch отказался двигать head PR, потому что по этому же PR
    прямо сейчас летит прогон ai-review.yml (in_progress/queued) — не
    инфраструктурный сбой и не занятый слот прохода, вызывающий код обязан
    поймать её отдельно и написать строку "подтянет следующий проход", а не
    смешивать с реальными ошибками update-branch.

    Живой инцидент (2026-09-06): оркестратор дёрнул update-branch на PR #488
    в 18:50, пока с 18:38 по нему же считал платный прогон ai-review против
    старого head — прогон досчитал впустую (verdict-шаг отбросил вердикт по
    ADR 0007 п.5, «вердикт привязан к head»), а новый прогон стартовал сразу
    вслед за подтягиванием. Тормоз без газа не годится (AGENTS.md): газ здесь
    — то же самое чтение, что и тормоз, разнесённое по времени — как только
    прогон, вызвавший отказ, закончится (success/failure/cancelled), то же
    чтение other_active_ai_review_runs в следующем проходе отдаёт пустой
    список, и update_branch пробует снова без ручного вмешательства."""


# Бюджет на один ПРОХОД (merge_queue + update_remaining_pulls внутри одного
# его вызова), не на точку вызова внутри прохода: если бы счётчик жил
# локальной переменной внутри update_remaining_pulls, вторая точка вызова —
# behind-ветка merge_queue ниже — могла бы независимо подтянуть ещё
# UPDATE_BRANCH_BUDGET_PER_PASS PR тем же проходом, удвоив фактический
# потолок. Счётчик живёт здесь, в самой функции, которую обе точки вызова
# обязаны использовать для настоящего push'а — обойти его, не обходя
# update_branch, нельзя. Если появится третья точка вызова, ей тоже придётся
# идти через update_branch: другого способа дёрнуть PUT .../update-branch в
# этом файле нет.
#
# До #297 «проход» и «прогон» планировщика совпадали — счётчик сбрасывался
# один раз в начале main(). Теперь прогон — это цикл проходов (merge_loop), и
# каждый проход обнуляет счётчик заново (см. reset_update_branch_budget ниже
# и её вызов внутри merge_loop).
#
# #761 — потолок «один успешно подтянутый PR за проход» (#252, третий заход)
# был защитой от дефекта, который к 2026-09-08 уже починен: причина слота —
# diff_fingerprint считался по sha блоба ГОЛОВЫ PR (review_labels.py, до #740),
# поэтому любое подтягивание main меняло отпечаток файла, даже если патч
# автора не сдвинулся ни на байт, и сбрасывало ai:ok — второй, дорогой гейт
# (ai-review.yml) перезапускался вхолостую (замер #252: 142 прогона за 14.5ч).
# PR #753 (задача #740, слито 2026-09-08T07:52:53Z) переписал
# review_labels._file_content_key/diff_fingerprint на sha256 трёхточечного
# `patch` (относительно merge-base PR-ветки, не относительно головы) — чистое
# подтягивание main, не пересекающееся построчно с патчем автора, отпечаток
# больше не меняет (см. docstринг _file_content_key и тест
# test_update_branch_budget_raise_does_not_reopen_review_storm ниже). Держать
# потолок «один за проход» после этого — тормоз без причины (AGENTS.md).
#
# Число UPDATE_BRANCH_BUDGET_PER_PASS обосновано замером на живом
# репозитории (2026-09-08): 38 открытых PR, 25 из них проходят
# should_update_branch (5 — оба вердикта зелёные, 20 — метка conflict;
# `gh api repos/mytab0r/edge-harness/pulls?state=open&per_page=100`) — то
# есть при старом бюджете «1 за проход» этому конкретному состоянию
# репозитория понадобилось бы 25 проходов цикла слияний (до 20 минут
# MERGE_LOOP_TIMEOUT_SECONDS, MERGE_LOOP_POLL_SECONDS=30 между проходами —
# 25×30с = 750с чистого сна, почти весь таймаут). Секундарный лимит GitHub
# API — 80 content-generating запросов/мин и 500/час (docs/research/21) —
# каждый успешный update_branch тратит РОВНО один content-generating запрос
# (сам PUT; обе проверки AiReviewRunning — GET, в лимит не считаются),
# значит даже верхняя граница 50 в один проход — 50/500 часового лимита,
# 10-кратный запас, и на порядок ниже 80/мин при любой разумной длительности
# прохода. 50 выбрано с запасом ×2 над сегодняшним измеренным пиком (25) —
# не «без потолка» (защита от аномального всплеска, например бага, который
# резко пометит сотни PR conflict), но достаточно, чтобы сегодняшний реальный
# бэклог подтянулся ЗА ОДИН проход, а не за 25.
UPDATE_BRANCH_BUDGET_PER_PASS = 50
_update_branch_calls_this_pass = 0


def reset_update_branch_budget() -> None:
    """Обнуляет счётчик update_branch. merge_loop вызывает это в начале
    КАЖДОГО своего прохода (до #297 — main() вызывал один раз в начале всего
    прогона) — без явного сброса исчерпанный счётчик остался бы закрытым до
    следующего прохода. Тесты сбрасывают его тем же вызовом перед каждым
    сценарием (см. autouse-фикстуру в test_scheduler.py)."""
    global _update_branch_calls_this_pass
    _update_branch_calls_this_pass = 0


def update_branch(repo: str, pr_number: int) -> None:
    """gh pr update-branch. Обновление через GITHUB_TOKEN не зажигает проверки
    (защита GitHub от рекурсии) — бот-PR навсегда зависает в blocked, поэтому
    PAT, если задан. Один вызов — переиспользуется merge_queue (PR behind) и
    after_merge → update_remaining_pulls (#196, поведение 3: подтянуть
    остальных после слияния).

    Бюджет на проход (см. UPDATE_BRANCH_BUDGET_PER_PASS/
    _update_branch_calls_this_pass выше; #297 разделило понятия «проход» и
    «прогон» — см. merge_loop; #761 — счётчик вместо булева слота):
    (UPDATE_BRANCH_BUDGET_PER_PASS + 1)-я попытка подтянуть ЛЮБОЙ PR этим же
    проходом — из любой точки вызова — кидает UpdateBranchBudgetExhausted
    вместо push'а. Успех увеличивает счётчик на 1; неудачная попытка
    (RuntimeError/CalledProcessError, вероятный конфликт) счётчик не трогает
    — head не изменился, следующий кандидат в этом же проходе ничем не
    рискует.

    Тормоз гонки «оркестратор двигает head, пока по нему летит ai-review»
    (см. AiReviewRunning выше, живой инцидент 2026-09-06, PR #488): ПЕРЕД
    самим push'ом (и до расхода бюджета — неудачная попытка его не трогает)
    проверяется, нет ли прогона ai-review.yml (in_progress/queued) для
    именно этого PR. Один и тот же предикат, что уже отказывает ручному
    workflow_dispatch того же PR (`review_labels.other_active_ai_review_runs`,
    #399) — второй копии этой проверки не заводится, обе точки входа (ручной
    повтор ревью и автоматическое подтягивание ветки) читают один и тот же
    список активных прогонов."""
    global _update_branch_calls_this_pass
    if _update_branch_calls_this_pass >= UPDATE_BRANCH_BUDGET_PER_PASS:
        raise UpdateBranchBudgetExhausted(
            f"бюджет update_branch этого прохода ({UPDATE_BRANCH_BUDGET_PER_PASS}) "
            f"исчерпан до PR #{pr_number}"
        )
    running = review_labels.other_active_ai_review_runs(repo, pr_number, exclude_run_id=None, gh_func=gh)
    if running:
        run_ids = ", ".join(str(run.get("id")) for run in running)
        raise AiReviewRunning(
            f"по PR #{pr_number} прямо сейчас летит ai-review.yml (run {run_ids}) "
            "— head не двигаю, пока прогон не закончится"
        )
    pat = os.environ.get("ORCHESTRA_PAT")
    description = f"gh api -X PUT repos/{repo}/pulls/{pr_number}/update-branch"
    if _guard_raw_subprocess_write(description):
        if pat:
            subprocess.run(
                ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/update-branch",
                 "-H", f"Authorization: Bearer {pat}"],
                capture_output=True, text=True, encoding="utf-8", env={**os.environ, "NO_COLOR": "1"},
                check=True,
            )
        else:
            gh("-X", "PUT", f"repos/{repo}/pulls/{pr_number}/update-branch")
    _update_branch_calls_this_pass += 1


def update_branch_or_report(
    repo: str,
    pr_number: int,
    *,
    on_success: str,
    on_budget_exhausted: str,
    on_ai_review_running: str,
    on_error: str,
) -> str:
    """Единственное место, где разбираются четыре исхода update_branch —
    успех, исчерпанный слот прогона (UpdateBranchBudgetExhausted), летящий
    ai-review этого же PR (AiReviewRunning, см. update_branch), сетевой/иной
    сбой (RuntimeError/subprocess.CalledProcessError). Закрывает класс "разная
    обработка ошибок update_branch в разных точках вызова" (PR #288): раньше
    behind-ветка merge_queue ловила только UpdateBranchBudgetExhausted, а
    update_remaining_pulls — оба исхода, из-за чего сетевой сбой в
    merge_queue ронял весь main() без отчёта. Тот же класс уже чинили
    точечно в #248 (находка 3, вызовы детектора без try) и #253 (находка 4,
    ветки ok/fail без per-item try).

    Обе точки вызова обязаны идти через эту функцию, а не звать update_branch
    напрямую и заводить свой try/except — четыре параметра-текста
    обязательные, без значений по умолчанию, поэтому новая (третья) точка
    вызова, забывшая текст на один из исходов, падает TypeError'ом сразу при
    вызове, а не тонет в проде необработанным исключением. on_error
    форматируется через .format(error=...).

    AiReviewRunning разбирается ПЕРЕД RuntimeError (она — его подкласс):
    иначе более общая ветка перехватила бы её первой и «летит ai-review»
    ушло бы читателю отчёта как обычный сетевой сбой — тот же класс подмены
    диагноза, что уже развели on_budget_exhausted/on_error.

    subprocess.CalledProcessError разбирается отдельно от RuntimeError
    (находка AI-ревью PR #288): в проде ORCHESTRA_PAT задан
    (.github/workflows/orchestra.yml), значит update_branch падает не через
    gh()/RuntimeError, а через subprocess.run(check=True) — исключение несёт
    stderr `gh api`, а str(CalledProcessError) его не включает (только код
    возврата). Без разбора отдельно строка отчёта теряет причину сбоя —
    остаётся голое "returned non-zero exit status 1"."""
    try:
        update_branch(repo, pr_number)
    except UpdateBranchBudgetExhausted:
        return on_budget_exhausted
    except AiReviewRunning:
        return on_ai_review_running
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or str(error)).strip()
        return on_error.format(error=detail)
    except RuntimeError as error:
        return on_error.format(error=error)
    return on_success


def pr_check_runs(repo: str, pull: dict) -> list[dict]:
    """check-run'ы текущего head — общая точка HTTP для pr_bad_checks и
    pr_is_merge_ready (#303, находка ревью: раньше pr_is_merge_ready не мог
    отличить «проверки ещё не заведены» от «проверки зелёные», не читая сам
    список — вынесено сюда, чтобы обе функции читали один и тот же ответ, не
    делая по два вызова gh() на PR)."""
    checks = gh(f"repos/{repo}/commits/{pull['head']['sha']}/check-runs?per_page=100")
    return checks.get("check_runs", [])


def latest_check_runs(runs: list[dict]) -> list[dict]:
    """Один чек-ран на имя — самый свежий по `started_at`, если тот же чек
    перезапускался (rerun) на одном и том же head sha (#467). GitHub
    check-runs API отдаёт КАЖДУЮ попытку отдельным объектом с тем же `name` —
    старая упавшая попытка не пропадает из ответа после успешного повтора;
    branch protection сама учитывает только последнюю попытку при решении
    «можно ли сливать», наш собственный разбор — нет, если не отфильтровать
    явно. Живой случай: PR #437 (задача #432) — `contract` на одном head sha
    сначала `failure` (04:02:20Z), потом `success` того же имени (rerun,
    04:03:39Z); без этого фильтра приёмка находила первую попытку и красила
    задачу в fail, хотя PR к тому моменту был зелёным и уже слит."""
    latest: dict[str, dict] = {}
    for run in runs:
        name = run["name"]
        current = latest.get(name)
        if current is None or run.get("started_at", "") > current.get("started_at", ""):
            latest[name] = run
    return list(latest.values())


def bad_check_names(runs: list[dict]) -> list[str]:
    """Одно место правды (находка AI-ревью PR #253) для критерия «красного
    обязательного чека»: раньше `conclusion not in (success, skipped, neutral)`
    было продублировано трижды (pr_bad_checks, merge_queue, script_evidence) —
    расхождение критерия в одной из копий при правке двух других осталось бы
    незамеченным. Все три места зовут эту функцию. Пустой список runs даёт
    пустой список здесь — это НЕ «красных нет», а «проверки ещё не заведены»;
    вызывающий код обязан проверять пустоту runs отдельно (см. pr_is_merge_ready).
    Дедуп по имени (см. latest_check_runs, #467) — на входе с уже отфильтрованным
    списком это холостой проход, безопасно для всех трёх вызывающих."""
    return [run["name"] for run in latest_check_runs(runs) if run["conclusion"] not in ("success", "skipped", "neutral")]


def pr_bad_checks(repo: str, pull: dict) -> list[str]:
    """Имена красных check-run'ов текущего head (см. bad_check_names).
    Один и тот же критерий «красного обязательного чека», что и внутри
    merge_queue (гейт слияния) — но отдельный вызов: unhealthy_pulls (#196,
    поведение 2) читает состояние ДО очереди слияния и по другому набору PR
    (у задачи может быть несколько PR), переиспользовать один HTTP-ответ негде.
    Пустой список check-run'ов здесь намеренно трактуется как «красных нет»
    (PR ещё не нездоров, просто рано судить) — в отличие от pr_is_merge_ready,
    где пустой список обязан значить «не готов» (см. bad_check_names)."""
    return bad_check_names(pr_check_runs(repo, pull))


def merge_queue(
    repo: str, pulls: list[dict],
) -> tuple[list[str], list[str], bool, int | None, bool]:
    """Возвращает (наблюдения, действия, был_ли_жёсткий_сбой_after_merge,
    номер слитого PR или None, была_ли_обновлена_ветка) — см. after_merge.

    Наблюдения vs действия разведены по #456: причины ПРОПУСКА кандидата
    (черновик, не тот mergeable_state, проверки не готовы/красные, гейт меток
    не пройден, бюджет update_branch этого прохода исчерпан) ничего не
    меняют — раньше они попадали в тот же список, что реальное слияние или
    обновление ветки, и делали merge_lines непустым на каждом проходе с
    открытой, но не готовой очередью PR (обычное дело), заставляя main()
    считать это «действием».

    Последние два поля (#297) — сигнал для merge_loop ниже: слитый номер
    решает, продолжать ли цикл (очередь изменилась — есть смысл посмотреть
    заново); признак обновления ветки решает, ждать ли следующую попытку
    (проверки только что перезапущены) или остановиться немедленно (эта
    попытка ничего не сдвинула, повтор без нового внешнего события даст тот
    же результат). Один проход этой функции обновляет НЕ БОЛЬШЕ
    UPDATE_BRANCH_BUDGET_PER_PASS веток (#761 — было «не больше одной»,
    #252/#288; счётчик общий с update_remaining_pulls, см. update_branch) —
    цикл вокруг может делать таких проходов несколько подряд, каждый со
    своим бюджетом update_branch (см. merge_loop)."""
    actions: list[str] = []
    skipped = []
    updated = False
    for pull in pulls:
        if pull.get("draft"):
            skipped.append(f"#{pull['number']} — черновик")
            continue
        # mergeable_state живёт только на endpoint'е одиночного PR: в списке он
        # всегда отсутствует, и доверие ему — тихая потеря всех кандидатов.
        single = gh(f"repos/{repo}/pulls/{pull['number']}")
        state = single.get("mergeable_state")
        if state == "behind":
            # Ветка отстала от main — обновляем серверно (см. update_branch),
            # но только выборочно (#252, review_labels.should_update_branch):
            # PR без обоих вердиктов/в доработке подтягивать невыгодно — см.
            # докстринг предиката, он же газ к этому тормозу.
            if not review_labels.should_update_branch(pull["labels"]):
                skipped.append(
                    f"#{pull['number']} — behind main, но не близок к слиянию и не в конфликте, "
                    "подтягивание пропущено (#252)"
                )
                continue
            # Бюджет update_branch — на этот ПРОХОД функции (см. merge_loop,
            # #297): эта ветка и update_remaining_pulls после слияния делят
            # один и тот же счётчик внутри update_branch, поэтому здесь тоже
            # возможен UpdateBranchBudgetExhausted, а не только сетевой сбой.
            # Обработка обоих исходов — в update_branch_or_report (#288), не здесь.
            success_text = f"#{pull['number']} — обновлена из main, проверки пойдут заново"
            budget_exhausted_text = (
                f"#{pull['number']} — behind main и близок к слиянию, но бюджет update_branch "
                f"этого прохода ({UPDATE_BRANCH_BUDGET_PER_PASS}) исчерпан; подтянет следующая "
                "итерация цикла слияний (#252/#297/#761)"
            )
            ai_review_running_text = (
                f"#{pull['number']} — behind main и близок к слиянию, но по нему прямо сейчас летит "
                "ai-review.yml; head не трогаю, чтобы не сорвать прогон (тормоз гонки, живой инцидент "
                "PR #488) — следующий проход подтянет, когда прогон закончится"
            )
            result = update_branch_or_report(
                repo, pull["number"],
                on_success=success_text,
                on_budget_exhausted=budget_exhausted_text,
                on_ai_review_running=ai_review_running_text,
                on_error=(
                    f"#{pull['number']} — behind main и близок к слиянию, но update_branch не удался "
                    "(вероятен конфликт — попадёт под mark_conflicts): {error}"
                ),
            )
            if result == success_text:
                updated = True
                actions.append(result)
            elif result in (budget_exhausted_text, ai_review_running_text):
                # Слот занят этим же прогоном ИЛИ head намеренно не тронут
                # из-за летящего ai-review — попытки push'а не было вовсе.
                skipped.append(result)
            else:
                # Сетевой/иной сбой реальной попытки push'а — действие с
                # неудачным результатом, не наблюдение.
                actions.append(result)
            continue
        if state not in ("clean", "unstable", "has_hooks"):
            skipped.append(f"#{pull['number']} — mergeable_state={state or 'не вычислен GitHub'}")
            continue
        checks = gh(f"repos/{repo}/commits/{pull['head']['sha']}/check-runs?per_page=100")
        runs = checks.get("check_runs", [])
        if not runs:
            skipped.append(f"#{pull['number']} — проверки ещё не заведены")
            continue
        bad = bad_check_names(runs)
        if bad:
            skipped.append(f"#{pull['number']} — красные проверки: {', '.join(bad)}")
            continue
        labels = {label["name"] for label in pull["labels"]}
        # Гейт слияния по меткам-вердиктам — формулировка в review_labels
        # (одно место правды): оба гейта ревью, детерминированный (review:ok,
        # ставит scripts/review/check_pr.py) и AI (ai:ok, ставит
        # scripts/review/ai_review.py, #18), обязаны быть зелёными.
        gate_reason = review_labels.merge_label_gate(labels)
        if gate_reason:
            skipped.append(f"#{pull['number']} — {gate_reason}")
            continue
        # Ответ PUT .../merge несёт 'sha' слитого коммита (merge_commit_sha) —
        # единственный надёжный источник ЗДЕСЬ И СЕЙЧАС: словарь `pull` выше
        # собран запросом СПИСКА до мержа, GitHub заполняет merge_commit_sha
        # только после того, как мерж реально произошёл. Нужен сразу же,
        # синхронно (#955/#929) — react_to_merge внутри after_merge дедупит
        # диспатч по этому sha, без него дедуп невозможен по построению.
        merge_result = gh(
            "-X", "PUT", f"repos/{repo}/pulls/{pull['number']}/merge",
            "-f", f"merge_method={MERGE_METHOD}",
        )
        actions.append(f"✅ PR #{pull['number']} слит ({MERGE_METHOD})")
        observations = [f"⏸️ {item}" for item in skipped]
        other_pulls = [p for p in pulls if p["number"] != pull["number"]]
        merge_sha = (merge_result or {}).get("sha")
        after_observations, after_actions, hard_failure = after_merge(
            repo, pull, other_pulls, merge_sha=merge_sha)
        observations += after_observations
        actions += after_actions
        return observations, actions, hard_failure, pull["number"], True  # один за проход: см. merge_loop
    observations = [f"⏸️ {item}" for item in skipped]
    return observations, actions, False, None, updated


def merge_loop(repo: str, pulls: list[dict]) -> tuple[list[str], list[str], bool, list[dict]]:
    """Цикл слияний одного прогона (#297) — main() зовёт эту функцию вместо
    одиночного merge_queue. Возвращает (наблюдения, действия,
    был_ли_жёсткий_сбой, финальный список открытых PR) — четвёртое поле
    добавлено дедупликацией запросов GitHub API (#443): раньше main() ПОСЛЕ
    этой функции трижды сам перечитывал open_pulls(repo) для
    trigger_ai_review/stale_ready_pulls/accept_merged_tasks, хотя нужный
    снимок уже лежит здесь — цикл сам ведёт актуальный `pulls`, обновляя его
    КАЖДЫЙ раз, когда что-то реально изменилось (слияние или подтянутая
    ветка), и не трогая его, когда проход не сделал ничего. Возвращаемое
    значение — тот же снимок, что дал бы свежий open_pulls(repo) в момент
    возврата, без отдельного HTTP-вызова.

    Каждая итерация — один проход merge_queue (сериализация «одно слияние за
    проход» не меняется, #252/#288; «одно обновление ветки за проход» —
    поднято до UPDATE_BRANCH_BUDGET_PER_PASS, #761); бюджет update_branch
    сбрасывается заново КАЖДУЮ итерацию (было — один раз на весь прогон),
    поэтому цикл способен обновить несколько РАЗНЫХ PR подряд — до
    UPDATE_BRANCH_BUDGET_PER_PASS за проход (см. update_branch, почему это
    больше не тормозит ai:ok — #740).

    Останов, любое из условий:
      - слито MERGE_LOOP_MAX_MERGES PR (потолок числа слияний за прогон);
      - истёк MERGE_LOOP_TIMEOUT_SECONDS с начала цикла (общий таймаут);
      - проход не сделал прогресса (не слил и не обновил ни одну ветку) —
        без нового внешнего события (новый пуш/лейбл) следующая попытка
        прямо сейчас даст тот же результат, ждать бессмысленно.
    Прогресс без слияния (только update_branch) ждёт MERGE_LOOP_POLL_SECONDS
    и повторяет — обновлённому PR нужно время на пересчёт проверок
    (pr-review.yml/ai-review.yml запускаются заново), а не мгновенно
    доступный mergeable_state."""
    observations: list[str] = []
    actions: list[str] = []
    hard_failure = False
    merged_count = 0
    deadline = time.monotonic() + MERGE_LOOP_TIMEOUT_SECONDS
    while merged_count < MERGE_LOOP_MAX_MERGES and time.monotonic() < deadline:
        # Бюджет на ЭТОТ проход (см. merge_queue) — сбрасывается заново каждую
        # итерацию, не один раз на весь прогон (было так до #297).
        reset_update_branch_budget()
        iter_observations, iter_actions, iter_hard_failure, merged_number, updated = merge_queue(repo, pulls)
        observations += iter_observations
        actions += iter_actions
        hard_failure = hard_failure or iter_hard_failure
        if merged_number is not None:
            merged_count += 1
            pulls = open_pulls(repo)  # слияние закрыло PR — состояние изменилось
            continue
        if not updated:
            break  # ни слияния, ни обновления ветки — новых событий этот прогон не дождётся
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(MERGE_LOOP_POLL_SECONDS, remaining))
        pulls = open_pulls(repo)  # проверки могли завершиться за паузу
    if merged_count:
        actions.append(
            f"🔁 цикл слияний: {merged_count} PR слито за прогон "
            f"(потолок {MERGE_LOOP_MAX_MERGES}, #297)"
        )
    return observations, actions, hard_failure, pulls


# ── Сессии раннеров в морде dsh-edge (#119) ───────────────────────────────────────
# После слияния PR задача закончена: сессия раннера harness-<N> уходит в архив
# морды, в списке активных остаются только живые задачи. Архив не удаляется —
# история читаема. Сессии может не быть (PR без раннера) — это норма, не ошибка.

DSH_EDGE_URL = os.environ.get("DSH_EDGE_URL", "")
DSH_EDGE_ACCESS_KEY = os.environ.get("DSH_EDGE_ACCESS_KEY", "")

# Одно место правды (#225): Cloudflare перед мордой режет запросы без явного
# User-Agent библиотечным клиентом — urllib.request шлёт дефолтный
# `Python-urllib/3.x`, и CF отвечает 403 `error code: 1010` ДО приложения
# (доказано прямым экспериментом на живой морде, docs/research/12-…md).
# Значение проверено фактически (POST /api/auth/login с заведомо неверным
# accessKey): собственное имя проходит фильтр (ответ приложения 401), значит
# маскироваться под браузер/curl не пришлось.
MORDE_USER_AGENT = "edge-harness-orchestra/1.0 (+https://github.com/mytab0r/edge-harness)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Контракт логина морды (docs/research/12-dsh-edge-session-api.md:13-14):
    POST /api/auth/login отвечает 303 + Set-Cookie — это УСПЕХ, не «иди по
    Location». HTTPRedirectHandler по умолчанию молча делает второй GET по
    Location без куки/тела и получает 403 (Origin/куки не те) — тот 403
    раньше всплывал как ошибка логина, хотя логин прошёл. Рабочие реализации
    (scripts/lib/dsh-edge-session.sh, канарейка deploy-dsh-edge.yml) читают
    303 без -L по той же причине — здесь то же самое место правды."""

    def redirect_request(self, *args, **kwargs):
        return None


def _morde_opener() -> urllib.request.OpenerDirector:
    """Opener с cookie-jar и БЕЗ автослежения за редиректом (см. _NoRedirect):
    логин обменивает access-ключ на куку владельца через 303, а не через
    переход по Location.

    addheaders задаёт User-Agent (#225, MORDE_USER_AGENT) на уровне opener'а —
    он летит в КАЖДЫЙ запрос через этот opener (и логин, и RPC), так что
    заголовок объявлен и применён в одном месте, а не в каждом Request по
    отдельности."""
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", MORDE_USER_AGENT)]
    return opener


def _morde_login(opener: urllib.request.OpenerDirector) -> None:
    """303 — единственный успешный код логина (кука уже осела в cookie-jar
    опенера к моменту исключения — HTTPCookieProcessor читает Set-Cookie до
    того, как _NoRedirect решает не идти по Location). Любой другой код —
    громкая ошибка: раньше resp.read() без проверки статуса делал успех и
    неуспех неотличимыми, а автослежение urllib за редиректом превращало
    303-успех в 403 вторым GET без куки/тела (см. _NoRedirect)."""
    data = urllib.parse.urlencode({"accessKey": DSH_EDGE_ACCESS_KEY}).encode()
    req = urllib.request.Request(
        DSH_EDGE_URL.rstrip("/") + "/api/auth/login", data=data, method="POST")
    try:
        with opener.open(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as error:
        status = error.code
        if status != 303:
            raise RuntimeError(f"логин в морду не удался: HTTP {status}") from error
        return
    if status != 303:
        raise RuntimeError(f"логин в морду не удался: ожидали HTTP 303, получили {status}")


def _morde_rpc(opener: urllib.request.OpenerDirector, method: str, payload: dict) -> dict:
    """Мутирующий RPC морды (workspace.archiveSession и т.п.) — третий,
    отдельный от gh()/subprocess транспорт, тот же класс прод-записи вне
    GitHub Actions (2026-09-11): гейтится ТОЙ ЖЕ prod_writes_allowed, что и
    gh()/send_telegram/_guard_raw_subprocess_write — второй копии решения не
    заводим. Оба вызывающих (archive_runner_sessions) игнорируют возвращаемое
    значение — `{}` безопасен как заглушка dry-run."""
    if not _guard_raw_subprocess_write(f"POST /api/{method} (морда dsh-edge) payload={payload}"):
        return {}
    body = json.dumps({
        "type": "client-request",
        "rpcId": "orchestra",
        "method": method,
        "payload": payload,
    }).encode()
    req = urllib.request.Request(
        DSH_EDGE_URL.rstrip("/") + "/api/" + method,
        data=body, method="POST",
        headers={"content-type": "application/json"})
    with opener.open(req, timeout=30) as resp:
        result = json.load(resp)
    inner = result.get("result", {})
    if not inner.get("ok"):
        error = inner.get("error", {})
        raise RuntimeError(f'{error.get("code", "unknown")}: {error.get("message", "")}')
    return inner.get("value", {})


def _morde_ingest(opener: urllib.request.OpenerDirector, session_id: str, events: list[dict]) -> dict:
    """POST /api/sessions/<id>/ingest (патч 0004-harness-ingest) — форма ответа
    и ошибок другая, чем у RPC-конверта _morde_rpc: тело запроса — сырой
    `{"events":[...]}` БЕЗ обёртки client-request, успех — сырой JSON
    `{appended,lastSeq}`, отказ — обычный HTTP-код (400 allowlist/форма, 404
    нет сессии, 413 потолки), а не `{result:{ok:false}}` (docs/research/12,
    dsh-edge/patches/0004-harness-ingest.patch).

    Тот же гейт прод-записи, что _morde_rpc выше (2026-09-11) — единственный
    вызывающий (append_session_notes) игнорирует возвращаемое значение."""
    if not _guard_raw_subprocess_write(f"POST /api/sessions/{session_id}/ingest (морда dsh-edge)"):
        return {}
    body = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        DSH_EDGE_URL.rstrip("/") + f"/api/sessions/{session_id}/ingest",
        data=body, method="POST",
        headers={"content-type": "application/json"})
    try:
        with opener.open(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error


def archive_runner_sessions(task_numbers: list[int]) -> tuple[list[str], bool]:
    """#119: архив сессий раннера по каждому номеру задачи из тела слитого PR.

    Возвращает (строки отчёта, был_ли_жёсткий_сбой). «Жёсткий сбой» —
    инфраструктурная поломка (логин в морду не прошёл, сеть, RPC вернул НЕ
    session-not-found): возможность архивировать ЕСТЬ, но она сломана — по
    правилу fail loud такой сбой не может быть неотличим от «сессии нет» или
    «конфигурации нет» (оба — норма, не поломка). Мерж уже состоялся и не
    откатывается: жёсткий сбой не прерывает обход остальных номеров, только
    помечает результат — эскалацию и красный код делает вызывающий main()."""
    if not DSH_EDGE_URL or not DSH_EDGE_ACCESS_KEY:
        return (["⚠️ DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY не заданы — архив сессий раннеров пропущен (#119)"],
                False)
    try:
        opener = _morde_opener()
        _morde_login(opener)
    except (RuntimeError, OSError, urllib.error.URLError, ValueError) as error:
        return ([f"🚨 морда dsh-edge недоступна для архива сессий (возможность сломана, не отсутствует): {error}"],
                True)
    lines = []
    hard_failure = False
    for number in task_numbers:
        session_id = f"harness-{number}"
        try:
            _morde_rpc(opener, "workspace.archiveSession", {"sessionId": session_id})
            lines.append(f"🗄️ #{number}: сессия {session_id} заархивирована в морде")
        except RuntimeError as error:
            if "session-not-found" in str(error):
                lines.append(f"🗄️ #{number}: сессии раннера в морде нет — архивировать нечего")
            else:
                lines.append(f"🚨 #{number}: сессия {session_id} не заархивирована (возможность сломана): {error}")
                hard_failure = True
        except (OSError, ValueError) as error:
            lines.append(f"🚨 #{number}: архив сессии не удался (возможность сломана): {error}")
            hard_failure = True
    return lines, hard_failure


# ── Заметки-итоги в сессии раннера (#480) ─────────────────────────────────────────
# Сессия harness-<N> обрывается ровно в момент, когда агент закончил работу —
# что случилось ПОСЛЕ (слияние, приёмка, возврат в пул) в ней не видно вовсе,
# и владелец идёт сверять с GitHub руками. Дописывается СТРОГО в тех местах,
# где пульс УЖЕ обнаруживает факт как часть существующего одноразового
# действия (after_merge/accept_merged_tasks/unhealthy_pulls) — новый опрос по
# всем сессиям на каждый пульс не заводится (квота GitHub API и DO rows_read
# и так была на пределе, #320/#325). «PR открыт» и позитивный вердикт гейтов
# намеренно вне скоупа: то и другое либо уже видно живым транскриптом (агент
# сам вызывает `gh pr create` внутри хода), либо секунды спустя сменяется
# слиянием — отдельное отслеживание потребовало бы нового маркера/опроса.
# Счётчик процесса (#794): монотонно растёт на каждую заметку за весь прогон
# scheduler.py, а не за один вызов append_session_notes — main() зовёт эту
# функцию из трёх разных мест за один запуск (after_merge, unhealthy_pulls,
# accept_merged_tasks), и одна и та же сессия harness-<N> может получить
# заметку из более чем одного места за один пульс.
_SESSION_NOTE_SEQ = itertools.count()


def _session_note_message_id(session_id: str) -> str:
    """Идентификатор message.id заметки-итога (#794).

    dsh-session::assertMessageEventShape (dsh-session/lib/index.js, пин
    0.1.2-rc.1 — proof в PR #794) валит холодную загрузку сессии без
    непустой строки message.id: «session event at seq N lacks an identified
    message». Проверено на самом пакете: без id — падает с этим текстом;
    с id, но без остальной формы сообщения — падает на другом поле
    («message has invalid source»), то есть один только id недостаточен
    (см. append_session_notes ниже, там же остальная форма).

    Формат id — тот же приём, что апстрим сам применяет при миграции старых
    сообщений без identity (dsh-session-persistence/lib/index.js::legacyMessageId,
    `legacy-message:${id}:${seq}`): составной, не случайный. Реальный seq
    события здесь неизвестен — его назначает сервер внутри session.append,
    писатель его не видит, поэтому вместо seq — счётчик процесса
    (_SESSION_NOTE_SEQ), уникальный в пределах одного запуска scheduler.py.
    Уникальность МЕЖДУ запусками даёт GITHUB_RUN_ID — эта функция вызывается
    только изнутри workflow'а orchestra (переменная в GitHub Actions задана
    всегда); Date.now-подобная случайность как единственный источник не
    нужна и не используется — 'local' ниже это не тайбрейкер, а честная
    метка «вызвано вне Actions» (локальный прогон/тест), где два запуска
    подряд в пределах одной секунды и так не пишут в одну морду."""
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    return f"harness-note:{session_id}:{run_id}:{next(_SESSION_NOTE_SEQ)}"


def append_session_notes(notes: list[tuple[int, str]]) -> tuple[list[str], bool]:
    """notes — [(номер задачи, текст заметки), …], собранные вызывающей
    функцией за ОДИН проход (не по одной заметке): один логин в морду на весь
    вызов, а не на каждую задачу. Пустой список — ноль сетевых вызовов вовсе
    (гвардия холостого хода, тот же приём, что stale_ready_pulls).

    Возвращает (строки отчёта, был_ли_жёсткий_сбой) — тот же контракт, что
    archive_runner_sessions: сессии нет (задача без раннера, например
    ручной PR) — норма, не ошибка; сама морда недоступна — возможность
    сломана, сигнал громкий, но не валит вызывающую стадию (мерж/приёмка уже
    состоялись и не откатываются)."""
    if not notes:
        return ([], False)
    if not DSH_EDGE_URL or not DSH_EDGE_ACCESS_KEY:
        return ([], False)  # см. archive_runner_sessions — конфигурации нет, канал просто пуст
    try:
        opener = _morde_opener()
        _morde_login(opener)
    except (RuntimeError, OSError, urllib.error.URLError, ValueError) as error:
        return ([f"🚨 морда dsh-edge недоступна для лога итогов сессии (возможность сломана, не отсутствует): {error}"],
                True)
    lines: list[str] = []
    hard_failure = False
    for number, text in notes:
        session_id = f"harness-{number}"
        event = {
            "type": "assistant/message",
            "data": {
                "turn": 1, "step": 1,
                "message": {
                    "id": _session_note_message_id(session_id),
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    # kind обязан быть "model" (assertMessageEventShape) — эта
                    # заметка не результат вызова модели, а системная запись
                    # scheduler.py; provider/model называют это честно, а не
                    # выдают заметку за настоящий ответ провайдера.
                    "source": {"kind": "model", "provider": "orchestra-scheduler", "model": "session-note"},
                },
            },
        }
        try:
            _morde_ingest(opener, session_id, [event])
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                continue  # сессии раннера в морде нет — писать некуда, это норма
            lines.append(f"🚨 #{number}: лог итогов не дописан в сессию {session_id} (возможность сломана): {error}")
            hard_failure = True
        except (OSError, ValueError) as error:
            lines.append(f"🚨 #{number}: лог итогов не дописан в сессию {session_id} (возможность сломана): {error}")
            hard_failure = True
    return lines, hard_failure


def run_claimed_task(repo: str, task_number: int, run_id: int | str) -> bool:
    """След аренды: работал ли прогон `worker run <run_id>` над задачей #N.

    Формат следа — одно место правды в scripts/worker/task.sh (CLAIM_VIA =
    «worker run <GITHUB_RUN_ID>», гвардия формата — в test_scheduler.py);
    в задачу его кладёт claim_task.claim. Здесь только чтение, и чтение с
    границей по цифре: подстрока «worker run 123» без неё совпала бы с чужим
    следом «worker run 1234»."""
    pattern = re.compile(rf"worker run {re.escape(str(run_id))}(?!\d)")
    return any(
        pattern.search(comment.get("body") or "")
        for comment in all_issue_comments(repo, task_number)
    )


def last_worker_run_conclusion(repo: str, task_number: int) -> str | None:
    """Conclusion последнего прогона worker.yml, атрибутированного задаче
    (run_claimed_task ищет след аренды среди свежих прогонов, новее→старше) —
    None, если атрибуции не нашлось вовсе (аренда сгорела до следа/прогон
    ещё не отметился). Используется dispatch_conflict_rework::escalate ниже
    (находка ревью PR #478 — "алерт не гадает"): текст эскалации обязан
    называть ФАКТ (conclusion прогона), а не утверждать причину («содержательный
    конфликт»), которую отсюда не различить (инфраструктурный сбой/квота/
    таймаут дают тот же итог «PR всё ещё dirty», что и настоящий конфликт)."""
    for run in recent_runs(repo, WORKER_WORKFLOW, per_page=10):
        if run_claimed_task(repo, task_number, run.get("id")):
            return run.get("conclusion")
    return None


def resume_series_by_merge(repo: str, pull: dict, task_number: int) -> str | None:
    """#220, эвристика авто-возобновления предохранителя: слит PR задачи ветки,
    а последний красный прогон worker.yml работал над этой же задачей — причина
    серии с высокой вероятностью починена этим мержем, и серия сбрасывается
    сразу, без ожидания пробы (#205). Сам сброс — success-маркер RESUME_MARKER
    в #120: conveyor_gate считает его виртуальным success (series_anchor),
    старые красные прогоны и маркеры прошлой серии перестают участвовать в
    решении. Тело последнего красного прогона (display_title «worker») PR-номер
    не несёт, поэтому связь берётся из следа аренды — единственного места, где
    прогон и задача уже сопоставлены самим фактом захвата.

    Все проверки — по фактам, не по «сейчас»: серия должна быть жива (красный
    новее зелёного), мерж — новее начала красного прогона (красный ПОСЛЕ мержа
    — довод «причина жива», возобновлять его мерж не даёт права), задача —
    занята именно этим прогоном. Возвращает строку отчёта или None (связи или
    серии нет — не событие, а не ошибка)."""
    runs = recent_runs(repo, WORKER_WORKFLOW, per_page=10)
    last_red = next((r for r in runs if r.get("conclusion") in FAILURE_CONCLUSIONS), None)
    last_ok = next((r for r in runs if r.get("conclusion") == "success"), None)
    if last_red is None:
        return None
    if last_ok is not None and parse_time(last_ok["created_at"]) >= parse_time(last_red["created_at"]):
        return None  # серия закрыта зелёным прогоном — возобновлять нечего
    merged_at = (gh(f"repos/{repo}/pulls/{pull['number']}") or {}).get("merged_at")
    if not merged_at or parse_time(last_red["created_at"]) >= parse_time(merged_at):
        return None  # мерж не позже красного прогона: красный уже видел фикс
    if not run_claimed_task(repo, task_number, last_red.get("id")):
        return None  # последний красный работал над другой задачей — связи нет
    resume_token = f"{RESUME_MARKER} #{pull['number']}]"
    if issue_marker_times(repo, WATCHDOG_ISSUE, resume_token):
        return None  # сброс этим мержем уже сигналился — один сигнал на мерж
    text = resume_alert_text(pull["number"], task_number, last_red)
    status = escalate(repo, WATCHDOG_ISSUE, text)
    # Сброс ДОКАЗАН маркером в #120 — судим по факту (перечитываем #120), а не
    # по вызову escalate и не по его человеческой строке: канал best-effort и
    # глотает отказ постинга, а парсинг прозы статуса молча ломается от любой
    # переформулировки (тот же класс врущего отчёта, что чинили в пульсе
    # после #318). Гейт видит только #120 — значит сброс для него существует
    # только вместе с маркером там.
    if not issue_marker_times(repo, WATCHDOG_ISSUE, resume_token):
        return (f"⚠️ сброс мержем #{pull['number']} не подтверждён маркером в "
                f"#{WATCHDOG_ISSUE} — серия НЕ снята, возобновление остаётся за "
                f"пробой (#205); {status}")
    return (f"🔄 серия красных {WORKER_WORKFLOW} сброшена мержем #{pull['number']} "
            f"(задача #{task_number}; {status})")


def after_merge(
    repo: str, pull: dict, other_pulls: list[dict] | None = None,
    merge_sha: str | None = None,
) -> tuple[list[str], list[str], bool]:
    """Действия после слияния. Merge через GITHUB_TOKEN НЕ создаёт push-события
    (защита GitHub от рекурсии), поэтому за диспатчем реагирующих workflow и
    закрытием задач следим явно. other_pulls — открытые PR, кроме только что
    слитого (#196, поведение 3: подтянуть их из main); по умолчанию пусто —
    вызывающий код без списка остальных PR просто не подтягивает никого
    (сохраняет старое поведение). merge_sha — 'sha' из ответа `PUT
    .../merge` (#955/#929): без него react_to_merge не сможет дедупить
    диспатч по коммиту и откажет громко (RuntimeError), а не тихо продиспатчит
    вслепую.

    Возвращает (наблюдения, действия, был_ли_жёсткий_сбой_архивации) — #456:
    все строки этой функции сама по себе — действия (мерж уже случился,
    release/dispatch/напоминание/Telegram/архив — реальные вызовы), кроме
    того, что кладёт update_remaining_pulls (там есть настоящие наблюдения —
    см. её докстринг). Мерж уже состоялся — жёсткий сбой не откатывает и не
    блокирует эту функцию, только поднимается наверх для эскалации (main()
    красит прогон ПОСЛЕ мержа)."""
    observations: list[str] = []
    actions = []
    hard_failure = False
    number = pull["number"]
    # Пагинация (#294, третье место того же класса: check_pr.py и ai_review.py
    # уже читали через review_labels.list_pr_files, здесь оставалась сырая
    # первая страница) — PR за сотню файлов, где cf-worker/* стоят за сотой
    # позицией, молча не запускал бы deploy-worker.yml.
    files = review_labels.list_pr_files(repo, number, gh)
    # Единая функция диспатча по реестру config/merge-reactions.json (#955,
    # следствие #929) — заменяет прежние два отдельных вызова
    # dispatch_deploy_on_merge (cf-worker/→deploy-worker.yml,
    # dsh-edge/→deploy-dsh-edge.yml) и добавляет repo-ci.yml/codeql.yml/
    # worker-ci.yml, у которых раньше не было явного диспатча вовсе (замер
    # #929: обязательный гейт `test` и security-скан не гонялись на слитом
    # main с 2026-09-10). Дедуп по head_sha внутри react_to_merge закрывает
    # живой класс дублирования (deploy-worker.yml получал push+dispatch на
    # один и тот же коммит, #929). RuntimeError (merge_sha пуст — источник
    # PUT .../merge не отдал 'sha' — или сетевой сбой самого диспатча) не
    # роняет всю функцию: мерж УЖЕ состоялся, закрытие задач/Telegram/архив
    # ниже обязаны отработать независимо от судьбы этого одного канала.
    try:
        actions.extend(merge_reactions.react_to_merge(gh, repo, files, merge_sha))
    except RuntimeError as error:
        observations.append(f"⚠️ реакция на мерж (config/merge-reactions.json) не выполнена: {error}")
    # Закрытие задачи — не здесь и не по ключевым словам: мерж доказывает PR,
    # а не готовность задачи, чей критерий часто живёт после мержа (деплой,
    # канарейка, E2E). Напоминаем исполнителю про реальный пост-мерж прогон,
    # если критерий его требует; закрытие делает стадия приёмки ниже
    # (accept_merged_tasks, #227) по проверяемой улике — НЕ воркер и НЕ по
    # ключевым словам (кейс #56/#57: Closes закрыл задачу до зелёной
    # канарейки). Текст комментария не зовёт закрывать задачу вручную —
    # WORKER-PLAYBOOK прямо запрещает `gh issue close` после #227, и
    # напоминание, говорящее обратное, было бы тем же обходом проверки.
    # Намеренно любое упоминание, не только декларация (см. pr_references_issue
    # выше, #195): слитый PR мог упомянуть смежную задачу не первой строкой —
    # снять её замок и напомнить про пост-мерж проверку безопаснее, чем
    # оставить висеть.
    task_refs = sorted(set(task_ref.extract_task_refs(pull.get("body") or "")))
    task_numbers: list[int] = []
    # Номер и заголовок первой задачи ВЕТКИ (#404, ревью PR #402; переведено
    # на #394, решение владельца 2026-09-06) — для Telegram «слито в main»;
    # один PR = одно сообщение, даже если в теле несколько задач. Захват —
    # ТОЛЬКО когда номер совпадает с задачей ветки (own_task ниже): «этот PR
    # выполнил задачу N» — узкая семантика (#259), упоминание в прозе не
    # делает задачу выполненной этим PR.
    first_task: tuple[int, str] | None = None
    # Задача этого PR — единственный источник, имя ветки (#394, решение
    # владельца 2026-09-06: тело не читается вовсе). Используется ниже, чтобы
    # напоминание говорило правду про ИМЕННО этот случай (задача из ветки или
    # просто упомянутая в прозе — не то же самое, что «объявлена»).
    own_task = task_ref.resolve_pr_task(pull)
    for task_number in task_refs:
        # Release аренды (#121): слит PR — работа принята, замок больше не нужен.
        # Идемпотентно: замка может не быть (канал без аренды) — это не ошибка.
        try:
            actions.append(f"🔓 {claim_task.release(repo, int(task_number))}")
        except RuntimeError as error:
            actions.append(f"⚠️ замок task-{task_number} не снят: {error}")
        try:
            issue = gh(f"repos/{repo}/issues/{task_number}")
            if "pull_request" in issue or issue["state"] != "open":
                continue
            if "task" not in {label["name"] for label in issue["labels"]}:
                continue
            task_numbers.append(int(task_number))
            # Приёмка видит задачу «своей», если её номер — задача ветки
            # этого PR (`task_ref.resolve_pr_task`, #394: merged_pr_map
            # регистрирует под тем же числом). Обещание «приёмка закроет её
            # сама» для ЛЮБОГО упоминания было ложным: задача, упомянутая
            # только в прозе, не задача ветки, а через ACCEPTANCE_PENDING_HOURS
            # её снимает reap_stale с причиной «PR не появился» — ровно тот
            # класс неверных причин, что этот PR чинит для объявленных задач
            # (находка AI-ревью PR #253).
            if int(task_number) == own_task:
                if first_task is None:
                    # Захват здесь, на ветке own_task (#404, переведено на
                    # #394): «выполнена» вправе звучать только о задаче
                    # ветки — приёмка (#227) закрывает именно такую, и в
                    # чате не появится «#N выполнена» о задаче из чужого
                    # упоминания в прозе.
                    first_task = (int(task_number), issue.get("title") or "")
                reminder = (
                    f"🔁 PR #{number} слит в main. Мерж — ещё не готовность: если критерий "
                    "требует реального пост-мерж прогона (канарейка/E2E), проведи его и "
                    "оставь улику. Задачу закрывать не нужно и нельзя (`gh issue close` — "
                    "обход проверки, класс #56/#57) — стадия приёмки закроет её сама по "
                    "проверяемой улике (деплой/check-runs/файлы в main)."
                )
            else:
                reminder = (
                    f"🔁 PR #{number} слит в main и упомянул эту задачу, но не назвал её "
                    "именем ветки — стадия приёмки её не увидит и не закроет сама. Если "
                    "критерий требует реального пост-мерж прогона (канарейка/E2E), проведи "
                    "его и оставь улику; закрывай задачу только через PR на ветке "
                    "agent/<N>-<slug>, иначе приёмка её не увидит."
                )
            gh(
                "-X", "POST", f"repos/{repo}/issues/{task_number}/comments",
                "-f", "body=" + reminder,
            )
            actions.append(f"🔁 #{task_number}: напоминание про пост-мерж проверку — закрывает приёмка (#227)")
        except RuntimeError as error:
            # один кривой реф не должен ронять остальные действия after_merge
            actions.append(f"⚠️ напоминание в #{task_number} не доставлено: {error}")
    # Telegram «задача выполнена — слито в main» (#170): мерж — единственный факт,
    # на котором звучит «выполнена»; раньше об этом канале молчал вовсе, и владелец
    # узнавал о готовности только руками. Один PR = одно сообщение (по задаче
    # ВЕТКИ, #404/#394 — упоминания в прозе каналу не доверяются), даже если
    # тело упоминает несколько. Задачи ветки нет (dependabot, orchestra:skip,
    # PR без agent-ветки) — сообщений нет вовсе. Best-effort, как весь канал
    # (#120): место правды — комментарий в задаче выше, недоставленный Telegram
    # мерж не откатывает и прогон не красит, но и не молчит — ⚠️ в отчёте.
    if first_task is not None:
        tg_task_number, tg_task_title = first_task
        if send_telegram(
            merge_telegram_text(repo, pull["number"], tg_task_number, tg_task_title),
            as_html=True,
        ):
            actions.append(f"📣 Telegram: «#{tg_task_number} выполнена — слито в main» доставлено")
        else:
            actions.append("⚠️ Telegram: сообщение о слиянии не доставлено — след в задаче выше остаётся местом правды")
    # Авто-возобновление предохранителя по мержу (#220) — только для задачи
    # ветки (own_task): «последний красный прогон работал над этой задачей»
    # сопоставляет след аренды с задачей именно этого PR, упоминание в прозе
    # такой связью не является (та же узкая семантика, что у own_task выше).
    # Best-effort, как всё after_merge: мерж уже состоялся, недоступность
    # прогонов/маркеров его не откатывает — газ возобновления остаётся у
    # пробы (#205), просто срабатывающей медленнее.
    if own_task is not None:
        try:
            resume_line = resume_series_by_merge(repo, pull, own_task)
        except RuntimeError as error:
            resume_line = None
            actions.append(f"⚠️ авто-возобновление предохранителя не сработало: {error}")
        if resume_line:
            actions.append(resume_line)
    # Архив сессий раннеров (#119) — только для ЗАДАЧ пула (метка task): номер из
    # тела PR может оказаться чужой активной задачей/PR без сессии, архивировать
    # его нельзя — утащим чужую живую сессию в архив.
    if task_numbers:
        # Заметка-итог в сессии раннера (#480): «PR слит в main» — факт,
        # который эта функция и так обнаружила (мерж), без нового опроса.
        # Дописывается ДО архива (находка ревью PR #489: обратный порядок
        # льёт заметку в уже заархивированную сессию — комбинацию, которую
        # design.md прямо называет непроверенной живьём).
        note_lines, note_hard_failure = append_session_notes(
            [(n, f"🔀 PR #{number} слит в main.") for n in task_numbers])
        actions += note_lines
        archive_lines, hard_failure = archive_runner_sessions(task_numbers)
        actions += archive_lines
        hard_failure = hard_failure or note_hard_failure
    # Чеклист некритичных замечаний ревью (#462, третья категория находок):
    # незакрытые пункты НЕ блокировали слияние (иначе некритичное стало бы
    # критичным и вернуло бы конвейер к вечным кругам, тот же класс решения,
    # что у review:large) и НЕ теряются молча — ОДНА задача-хвост со ссылкой
    # на PR, не issue на каждый пункт. `pull.get("body")` (не отдельный
    # перезапрос) — тело PR несёт чеклист уже с момента ai:ok (гейт слияния
    # требует его до того, как PR вообще попадёт в очередь на слияние), а не
    # изменяется в промежутке между списком PR и этим моментом.
    try:
        unresolved = review_checklist.unresolved_items(pull.get("body") or "")
        if unresolved:
            tail_title = review_checklist.tail_issue_title(number)
            open_titles = {
                issue["title"]
                for issue in review_labels.list_pages(
                    f"repos/{repo}/issues?state=open&labels=task&per_page=100", gh)
                if "pull_request" not in issue
            }
            if tail_title in open_titles:
                observations.append(
                    f"ℹ️ хвост чеклиста PR #{number}: задача уже заведена (идемпотентность по заголовку)")
            else:
                created = pool_issue.create_pool_issue(
                    gh, repo, tail_title,
                    review_checklist.tail_issue_body(repo, number, unresolved),
                    ["task"],
                )
                actions.append(
                    f"📋 хвост чеклиста PR #{number}: заведена #{created['number']} "
                    f"({len(unresolved)} незакрытых пунктов)")
    except RuntimeError as error:
        # Мерж уже состоялся — недоступность GitHub здесь не откатывает его,
        # но и не молчит: видимое ⚠️ в отчёте, тот же приём, что у release
        # замка/напоминания выше в этой функции.
        observations.append(f"⚠️ хвост чеклиста PR #{number} не заведён: {error}")
    remaining_observations, remaining_actions = update_remaining_pulls(repo, pull["number"], other_pulls or [])
    observations += remaining_observations
    actions += remaining_actions
    return observations, actions, hard_failure


def update_remaining_pulls(repo: str, merged_number: int, other_pulls: list[dict]) -> tuple[list[str], list[str]]:
    """#196, поведение 3: сливаем по одному, а очередь пересчитывается только
    следующим запуском — без этого каждое слияние гарантированно оставляет
    остаток BEHIND main. gh pr update-branch для остальных открытых PR;
    конфликт (DIRTY после обновления не поможет — GitHub решит это при
    следующем пересчёте mergeable_state) не молчит: строка в отчёте, а
    mark_conflicts следующего прохода подхватит метку conflict.

    Выборочно, не для всех (#252, review_labels.should_update_branch — одно
    место правды): подтягивание — это push в чужую ветку, синхронизирует
    pr-review.yml и снимает валидные ai:*-метки без всякой пользы для PR,
    которому рано сливаться. Кандидат подтягивается, только если он реально
    близок к слиянию (оба вердикта зелёные) или уже в конфликте (подтягивание
    может его расшить).

    Максимум UPDATE_BRANCH_BUDGET_PER_PASS успешно подтянутых кандидатов за
    ПРОХОД, не за вызов этой функции (#252, третий заход, потолок поднят
    #761; терминология «проход» vs «прогон» уточнена в #297 — см.
    merge_loop). До #740 (PR #753, слито 2026-09-08) подтягивание кандидата
    меняло его собственный head → `diff_fingerprint` (тогда — sha блоба
    головы) менялся вслепую от чужого коммита → pr-review.yml перезапускался
    на пуш и снимал `ai:ok` того же кандидата (#294), даже если патч автора
    не сдвинулся ни на байт — тот кандидат, который секунду назад проходил
    предикат, после собственного же подтягивания выпадал из него. Единый
    слот «один за проход» был газом для этого узкого дефекта, не общим
    правилом: после #740 diff_fingerprint считается по трёхточечному patch
    (устойчив к чистому подтягиванию main без пересечения строк, см.
    _file_content_key), собственный ai:ok кандидата чистое подтягивание
    больше не сбрасывает — подтягивать несколько кандидатов подряд в одном
    проходе теперь безопасно для их же вердиктов.
    Бюджет общий с behind-веткой merge_queue: обе точки вызова делят один и
    тот же счётчик внутри update_branch (_update_branch_calls_this_pass), а
    не локальную переменную на каждую точку вызова — иначе поведение
    осталось бы прежним, просто с двумя независимыми лимитами вместо одного
    общего. Счётчик увеличивается только УСПЕХОМ: неудачная попытка
    (вероятный конфликт) не трогает head, значит следующий кандидат в этом
    же вызове ничем не рискует. Кандидаты, не уместившиеся в бюджет прохода,
    не молчат — они получают отдельную строку с указанием, что подтянет их
    следующий проход цикла слияний (merge_loop, #297; счётчик сбрасывается
    заново КАЖДЫЙ проход — reset_update_branch_budget внутри merge_loop, не
    один раз на весь запуск, как было до #297).

    Возвращает (наблюдения, действия) — разведено по #456: «не подтянут, не
    близок к слиянию» и «бюджет update_branch этого прохода исчерпан» ничего
    не меняют (push не делался вовсе); успешное обновление и сетевой сбой при
    реальной попытке push'а — оба действия (второе — попытка с эффектом,
    пусть и неудачным)."""
    observations: list[str] = []
    actions: list[str] = []
    for other in other_pulls:
        if other["number"] == merged_number or other.get("draft"):
            continue
        if not review_labels.should_update_branch(other["labels"]):
            observations.append(
                f"⏸️ PR #{other['number']} не подтянут из main после слияния #{merged_number} "
                "— не близок к слиянию и не в конфликте (#252)"
            )
            continue
        # Обработка всех четырёх исходов — в update_branch_or_report (#288), не здесь.
        budget_exhausted_text = (
            f"⏭️ PR #{other['number']} не подтянут из main после слияния #{merged_number} — бюджет "
            f"update_branch этого прохода ({UPDATE_BRANCH_BUDGET_PER_PASS}) исчерпан; подтянет "
            "следующий прогон оркестратора (#252/#761)"
        )
        ai_review_running_text = (
            f"⏸️ PR #{other['number']} не подтянут из main после слияния #{merged_number} — по нему "
            "прямо сейчас летит ai-review.yml; head не трогаю, чтобы не сорвать прогон (тормоз гонки, "
            "живой инцидент PR #488) — подтянет следующий прогон, когда прогон закончится"
        )
        result = update_branch_or_report(
            repo, other["number"],
            on_success=f"🔄 PR #{other['number']} обновлён из main после слияния #{merged_number}",
            on_budget_exhausted=budget_exhausted_text,
            on_ai_review_running=ai_review_running_text,
            on_error=(
                f"⚠️ PR #{other['number']} не обновлён из main после слияния #{merged_number} "
                "(вероятен конфликт — попадёт под mark_conflicts): {error}"
            ),
        )
        if result in (budget_exhausted_text, ai_review_running_text):
            # Слот занят самим этим прогоном ИЛИ head намеренно не тронут
            # из-за летящего ai-review (не сеть/сервер) — попытки push'а
            # не было вовсе, значит и наблюдение, не действие.
            observations.append(result)
        else:
            actions.append(result)
    return observations, actions


# ── Зависший (но по GH ещё in_progress) прогон worker.yml (#815) ────────────
# Живой инцидент 2026-09-09: прогон #34339807907 (проба предохранителя,
# стартовала ДО фикса #795, застряла на баге #794) провисела in_progress ~3ч
# и concurrency-группой worker.yml (group: worker, cancel-in-progress: false,
# worker.yml:26-29 — один воркер на репозиторий) блокировала ОДНОВРЕМЕННО
# обычный dispatch_worker, dispatch_conflict_rework И восстановление
# предохранителя (пробы 2/3 из #120 анонсированы, но не стартовали —
# worker_runs_active видел «занято»). Единственной границей была жёсткая
# 280-минутная стенка job'а (worker.yml:41) — до неё прогон держал ВЕСЬ пул.
#
# Порог обоснован замером живых длительностей worker.yml (100 последних
# прогонов, gh run list --workflow=worker.yml, снято 2026-09-09):
#   - success с реальной DSH-работой (длительность > 5 мин, 24 прогона):
#     медиана ≈ 73 мин, p75 ≈ 140 мин, МАКСИМУМ 151.1 мин — легитимный
#     success достигает этого значения РОВНО ПЯТЬ РАЗ (~20% выборки), что
#     соответствует комментарию task.sh о 150-минутном таймауте DSH за
#     попытку (worker.yml:34-40) — успешный прогон честно донашивает работу
#     почти до этой границы;
#   - остальные success (без реальной работы — "свободных задач нет",
#     no-op) укладываются в 0.3–1.7 мин;
#   - failure почти все короткие (30–90с — ранний отказ до тяжёлой работы).
# Порог из диагноза инцидента (60–90 мин, по трём прогонам 06:14/06:33/06:37)
# НЕ подтвердился при замере на большей выборке: два из тех трёх были
# no-op-возвратами (~90с), а третий (69 мин) — обычный рабочий прогон внутри
# наблюдаемого разброса легитимных success (16.6–151.1 мин) — порог 60–90 мин
# отменял бы примерно 40% реально работающих прогонов. WORKER_STALL_MINUTES
# обязан быть заметно ВЫШЕ максимума легитимного success (151.1), а не его
# медианы, и заметно ниже 280-минутного жёсткого таймаута:
WORKER_STALL_MINUTES = 200


def _run_age_minutes(run: dict, now: datetime) -> float | None:
    """Возраст прогона в минутах от `run_started_at` (когда GitHub его знает)
    или `created_at` (queued/раннее, пока job ещё не подхватил раннер) —
    `None`, если ни одного поля нет вовсе (не прод-форма ответа)."""
    started = run.get("run_started_at") or run.get("created_at")
    return minutes_between(parse_time(started), now) if started else None


def _run_is_stalled(run: dict, now: datetime,
                     threshold_minutes: float = WORKER_STALL_MINUTES) -> bool:
    """Чистое решение по уже полученному прогону (без сетевого вызова) —
    вынесено отдельно от stalled_worker_run/worker_runs_active, чтобы тесты,
    не относящиеся к обнаружению зависания, могли зафиксировать «прогон не
    завис» одной строкой monkeypatch, не подбирая правдоподобный recent
    timestamp под реальные wall-clock часы прогона тестов (тот же класс
    хрупкости, которого правило репозитория «тест кормится прод-формой»
    требует избегать: фиксированная дата фикстуры неизбежно «стареет» по
    календарю сама по себе)."""
    age = _run_age_minutes(run, now)
    return age is not None and age >= threshold_minutes


def stalled_worker_run(repo: str, now: datetime) -> dict | None:
    """Прогон worker.yml, который GitHub всё ещё числит `in_progress`, но
    который идёт дольше WORKER_STALL_MINUTES — сильнее наблюдаемого максимума
    легитимного success (см. обоснование порога выше). Признак свежести
    heartbeat (HANDS_TOKEN + HARNESS_URL/api/status, cf-worker/src/
    harness.ts::handsAreAlive) сюда НЕ подключён: оркестратор (orchestra.yml)
    не получает эти секреты/vars в своём окружении — добавление живого
    сетевого вызова в этот гейт (используется несколькими диспетчерами за
    пульс, см. вызовы ниже) вне рамок этого фикса, отдельное решение
    владельца. Деградация честная и единственный сигнал сегодня —
    длительность `in_progress`, признак грубее heartbeat (не отличает
    «завис» от «просто редкая долгая легитимная работа за порогом»), но не
    молчит: сообщение действия ниже (main()) прямо называет длительность и
    порог, а не гадает."""
    payload = gh(
        f"repos/{repo}/actions/workflows/{WORKER_WORKFLOW}/runs?status=in_progress&per_page=1"
    ) or {}
    runs = payload.get("workflow_runs") or []
    if not runs:
        return None
    run = runs[0]
    return run if _run_is_stalled(run, now) else None


def worker_runs_active(repo: str, now: datetime | None = None) -> bool:
    """Активный воркер = есть worker-ран в статусе in_progress или queued.
    Завершённые (в т.ч. упавшие) не считаются: упавший воркер при свободных
    задачах получит новый запуск — но пока задача назначена, пул свободных пуст
    и штурма не будет (возврат в пул только через stale-окно reap_stale).

    Зависший in_progress (#815, stalled_worker_run выше) — ИСКЛЮЧЕНИЕ: дольше
    WORKER_STALL_MINUTES без завершения не блокирует ни один из трёх
    диспетчеров, читающих эту функцию (dispatch_worker, dispatch_conflict_rework,
    проба предохранителя — она тоже уходит через dispatch_worker, отдельного
    гейта у неё нет). Сама отмена зависшего прогона и освобождение его задачи —
    отдельный шаг main() (reap_stalled_worker_run), не побочный эффект этой
    read-only проверки: функцию читают несколько мест за один пульс, мутировать
    GitHub при каждом чтении было бы сюрпризом и лишними вызовами."""
    now = now or datetime.now(timezone.utc)
    for status in ("in_progress", "queued"):
        payload = gh(
            f"repos/{repo}/actions/workflows/{WORKER_WORKFLOW}/runs?status={status}&per_page=1"
        ) or {}
        runs = payload.get("workflow_runs") or []
        if not runs:
            continue
        if status == "in_progress" and _run_is_stalled(runs[0], now):
            continue  # завис — не блокирует, см. докстринг
        return True
    return False


def reap_stalled_worker_run(
    repo: str, now: datetime, pool: list[dict], pulls: list[dict],
) -> tuple[list[str], list[str]]:
    """Разряжает находку stalled_worker_run (#815): отменяет зависший прогон
    (best-effort — сам гейт worker_runs_active уже не блокирует диспатч
    независимо от исхода отмены) и освобождает его задачу СРАЗУ, не дожидаясь
    обычных 24ч reap_stale/claim_task.collect_stale — иначе задача осталась бы
    занятой почти сутки после того, как сам факт зависания уже установлен.

    Задача, арендованная зависшим прогоном, определяется структурным
    признаком (не парсингом прозы, класс которого запрещён AGENTS.md): среди
    открытых незаблокированных назначенных задач без открытого PR (тот же
    критерий, что уже применяет reap_stale) берётся та, чьё событие timeline
    `assigned` не старше момента старта прогона — task.sh берёт аренду в
    первые секунды job'а (см. п.4 playbook), задолго до тяжёлой DSH-работы.
    Совпадений может не быть (адресный прогон на задачу с уже открытым PR —
    её reap_stale не тронул бы; либо аренда не найдена вовсе) — тогда отмена
    прогона всё равно происходит, задача остаётся на обычном пути."""
    observations: list[str] = []
    actions: list[str] = []
    run = stalled_worker_run(repo, now)
    if run is None:
        return observations, actions
    run_id = run["id"]
    started = run.get("run_started_at") or run.get("created_at")
    start = parse_time(started)
    age_minutes = minutes_between(start, now)
    try:
        gh("-X", "POST", f"repos/{repo}/actions/runs/{run_id}/cancel")
        cancel_note = "отменён"
    except RuntimeError as error:
        cancel_note = f"отменить не удалось: {error}"
    task_number = None
    for issue in pool:
        if not issue["assignees"] or _issue_is_blocked(issue):
            continue
        number = issue["number"]
        if any(pr_references_issue(pull, number) for pull in pulls):
            continue
        last = last_assigned_at(repo, number)
        if last is not None and last >= start:
            task_number = number
            break
    if task_number is not None:
        release_note = claim_task.release_full(repo, task_number)
        pool_issue = next((i for i in pool if i["number"] == task_number), None)
        if pool_issue is not None:
            pool_issue["assignees"] = []
        try:
            gh(
                "-X", "POST", f"repos/{repo}/issues/{task_number}/comments",
                "-f", "body=" + (
                    f"♻️ Прогон worker.yml (run {run_id}) завис {int(age_minutes)} мин "
                    f"без завершения (порог {WORKER_STALL_MINUTES} мин) — оркестратор счёл его "
                    f"зависшим, отменил ({cancel_note}) и снял аренду ({release_note}). Задача "
                    "возвращена в пул: python3 scripts/lib/claim_task.py claim "
                    f"{task_number} (#121, #815)."
                ),
            )
        except RuntimeError as error:
            print(f"::warning::след о снятии аренды #{task_number} не оставлен: {error}",
                  file=sys.stderr)
        actions.append(
            f"🧟 worker run {run_id} завис ({int(age_minutes)} мин, порог "
            f"{WORKER_STALL_MINUTES}) — {cancel_note}; задача #{task_number} освобождена "
            f"({release_note})"
        )
    else:
        actions.append(
            f"🧟 worker run {run_id} завис ({int(age_minutes)} мин, порог "
            f"{WORKER_STALL_MINUTES}) — {cancel_note}; арендованная им задача не определена "
            "(см. лог прогона вручную)"
        )
    return observations, actions


# ── WIP-лимит перед взятием НОВОЙ задачи (issue #464) ────────────────────────
# Владелец, 2026-09-06: «вместо того чтобы доделать текущие ПР, агенты идут и
# создают новые» — dispatch_worker до этой правки смотрел только на пул задач
# и на то, не бежит ли уже воркер (см. её докстринг ниже); число открытых PR
# не участвовало в решении вовсе. Замер того же дня: 29 открытых PR, из них 25
# реально ждут чужого труда (см. REWORK_LABELS), а dispatch_worker с той же
# готовностью диспетчит воркера на тридцатую задачу.
#
# Порог обоснован замером на живом репозитории (2026-09-06):
#   - темп слияний ~1.6–1.8 PR/час (43 слито за последние 24ч, 47 — с полуночи
#     UTC предыдущих суток, `search/issues?...+is:merged+merged:>=...`);
#   - время жизни PR от открытия до слияния (те же 47): медиана 2.1ч (типичный
#     случай), p75 13.8ч, среднее 16.3ч (утянуто вверх PR старше 2.5 суток —
#     именно застрявшие в доработке тянут очередь вверх);
#   - закон Литтла (L = λW): при цикле доработки вдвое короче нынешнего p75
#     (~7ч — всё ещё щедрее медианы, не разовая случайность) устойчивая
#     очередь ≈ 1.7×7 ≈ 12.
# WIP_LIMIT = 12: выше темпа обработки (гейт стоит МЕЖДУ пульсами дольше, чем
# требует здоровый цикл доработки, не душит сам конвейер слияний — merge_loop
# сливает независимо от этого гейта, см. main()), ниже наблюдаемых 25/29
# (гейт закрыт уже на сегодняшних живых данных — см. test_wip_gate_matches_
# live_repository_snapshot_2026_09_06 в test_scheduler.py).
WIP_LIMIT = 12

# CONTRACT_FAILED_LABEL — review_labels.py (одно место правды: тот же литерал
# был задублирован четырежды — дважды в contract_check.py, дважды здесь).
# REWORK_LABELS — то, что реально доказывает «PR не движется сам, а ждёт
# чужого труда»: ai:changes-requested — гейт 2 провален и НЕ самовосстанавливается
# (в отличие от ai:failed — тот самолечится авто-повтором trigger_ai_review,
# см. AI_REVIEW_MAX_ATTEMPTS выше, поэтому здесь его нет — тот же приём,
# что pr_is_unhealthy уже применяет: ai:failed туда тоже не входит);
# CONFLICT_LABEL — застрял, ждёт ребейза; CONTRACT_FAILED_LABEL — деталь PR не
# проходит контракт «PR↔задача». PR без единой из этих меток либо ждёт
# вердикта (движется сам), либо уже готов к слиянию — в лимит не входит.
CONTRACT_FAILED_LABEL = review_labels.CONTRACT_FAILED_LABEL
REWORK_LABELS = frozenset({review_labels.AI_CHANGES, CONFLICT_LABEL, CONTRACT_FAILED_LABEL})

# Носитель состояния «эпизод WIP-гейта открыт/закрыт» — комментарии-маркеры в
# WATCHDOG_ISSUE, тот же приём, что PAUSE_MARKER/RESUME_MARKER (conveyor_gate)
# и HEARTBEAT_NO_TICKS/TICKS_RESUMED (heartbeat_check): переживают перезапуск
# оркестратора, читаются заново каждым прогоном.
WIP_GATE_OPEN_MARKER = "[статус конвейера: WIP-лимит закрыл диспатч]"
WIP_GATE_CLOSE_MARKER = "[статус конвейера: WIP-лимит снят]"
# Префикс без часов — issue_marker_times ищет подстрокой, число дописывается
# в само сообщение (тот же приём, что PROBE_MARKER в pulse_guard). Текст про
# «новые задачи», не про воркера целиком — воркер при закрытом гейте
# по-прежнему диспетчируется на доводку уже открытых PR (см. dispatch_worker).
WIP_GATE_STUCK_MARKER_PREFIX = "[статус: WIP-лимит держит новые задачи дольше "

# Пункт задачи (AGENTS.md, «тормоз без газа не принимается», п.5 этого
# change): если гейт держит взятие НОВЫХ задач закрытым дольше этого —
# сигнал владельцу тем же каналом, что предохранитель конвейера (#120). Не
# второй порог правды: то же обоснование, что у WIP_LIMIT выше (здоровый цикл
# доработки ~7ч) плюс запас на то, что доработка PR — не мгновенное действие;
# 8ч — рабочий день, разумный срок для «очередь должна была хоть немного
# поредеть». Сигнал не утверждает, что диспатчи доводки были (находка ревью
# PR #466), и не гадает между причинами (AGENTS.md, «алерт не гадает»):
# причины различает сам по данным того же прогона — см. текст эскалации
# в wip_gate ниже.
WIP_GATE_STUCK_HOURS = 8


def pr_needs_rework(pull: dict) -> bool:
    """True — PR реально ждёт чужого труда (см. REWORK_LABELS), не движется
    сам по себе. Черновики и PR ботов исключены (п.2 задачи): черновик ещё не
    просит ревью; GitHub App-логины оканчиваются на `[bot]` (нативное
    соглашение платформы) — живьём на этом репозитории не встречались (замер
    2026-09-06: все 29 открытых PR от mytab0r), но фильтр остаётся на случай,
    если появится dependabot и подобные."""
    if pull.get("draft"):
        return False
    login = (pull.get("user") or {}).get("login") or ""
    if login.endswith("[bot]"):
        return False
    labels = {label["name"] for label in pull.get("labels") or []}
    return bool(labels & REWORK_LABELS)


def wip_gate(
    repo: str, now: datetime, pulls: list[dict], pool: list[dict], *,
    dispatch_allowed: bool,
) -> tuple[list[str], list[str], bool]:
    """WIP-лимит перед взятием НОВОЙ задачи (issue #464, см. блок констант
    выше) — второй, независимый от conveyor_gate тормоз, но НЕ перед всем
    dispatch_worker целиком: закрытый гейт (allowed=False) запрещает выбор
    задачи, у которой ещё нет открытого PR, и не трогает доводку уже открытых
    (находка ревью PR #466 — закрывать диспетч воркера целиком тут значило бы
    держать тормоз без газа: очередь физически не смогла бы разгрестись сама,
    только вручную). Разведение «новая задача»/«доводка» — в dispatch_worker,
    см. её докстринг; здесь только счётчик и решение по НОВЫМ.

    `pulls` — тот же снимок open_pulls, что merge_loop уже довёл до
    актуального состояния этим прогоном (#443/#456: второй обход PR здесь не
    заводится). `pool` — пул задач этого же прогона: из него и `pulls`
    считается, есть ли при закрытом гейте вообще что доводить (свободная
    задача с уже открытым PR). Без `pool` stuck-эскалация могла бы только
    гадать между «доводка не успевает» и «доводить нечего» — по AGENTS.md
    («алерт не гадает») причины различает сам, данные для этого в прогоне
    уже есть. `dispatch_allowed` — вердикт conveyor_gate этого же прогона:
    при погашенном предохранителе (#120) доводка не диспетчится вовсе и
    тормоз уже назван conveyor_gate — гейт молчит, не открывает эпизод и не
    эскалирует, а открытый эпизод закрывает, чтобы stuck-часы считали только
    время, когда WIP был действующим тормозом диспатча (блокирующая находка
    2 ревью PR #466: иначе через 8ч сигнал говорил бы «доводка не успевает»,
    хотя доводки не было вовсе).

    Возвращает (наблюдения, действия, разрешён_ли_диспетч) — тот же контракт,
    что conveyor_gate: «диспатч разрешён без изменений» и «уже оповещено» —
    наблюдения (ничего не меняют на сервере), реальная простановка/снятие
    маркера — действие.

    Тормоз называет газ (AGENTS.md): строка при срабатывании прямо называет
    число и порог, газ — слияние/закрытие PR ниже порога, снимается
    автоматически следующим прогоном (episode-маркер закрывается сам, см.
    ветку count < WIP_LIMIT ниже) — ручного участия не требуется."""
    observations: list[str] = []
    actions: list[str] = []
    rework = [pull for pull in pulls if pr_needs_rework(pull)]
    count = len(rework)

    try:
        open_times = issue_marker_times(repo, WATCHDOG_ISSUE, WIP_GATE_OPEN_MARKER)
        close_times = issue_marker_times(repo, WATCHDOG_ISSUE, WIP_GATE_CLOSE_MARKER)
    except RuntimeError as error:
        # Маркеры недоступны — решение «разрешён ли диспатч» само по себе не
        # гадает (зависит только от count/WIP_LIMIT, который известен точно),
        # но длительность эпизода посчитать не можем — не эскалируем вслепую.
        print(f"::warning::маркеры WIP-гейта в #{WATCHDOG_ISSUE} не прочитаны: {error}", file=sys.stderr)
        if count < WIP_LIMIT:
            return ([f"🟢 WIP: {count} PR ждут доработки (лимит {WIP_LIMIT}) — новые задачи разрешены"],
                    [], True)
        return ([f"⏸️ новые задачи не берутся: {count} открытых PR ждут доработки при лимите "
                 f"{WIP_LIMIT} — сначала доводим (маркеры #{WATCHDOG_ISSUE} недоступны)"], [], False)

    last_close = max(close_times) if close_times else None
    # Открывающие маркеры ПОСЛЕ последнего закрытия — только они принадлежат
    # текущему (ещё не закрытому) эпизоду; более старые — эхо прошлого,
    # уже закрытого (тот же приём, что episode_reopened в pulse_guard).
    episode_opens = [t for t in open_times if last_close is None or t > last_close]

    if not dispatch_allowed:
        # Предохранитель конвейера (#120) погасил диспатч целиком — тормоз
        # уже назван conveyor_gate, и доводка в этом пульсе не диспетчится
        # вовсе. Гейт молчит: ни наблюдения о «своём» тормозе, ни маркера
        # открытия, ни stuck-эскалации — иначе спустя WIP_GATE_STUCK_HOURS
        # сигнал говорил бы владельцу «доводка не успевает», хотя доводки
        # не было вовсе (две противоречащие причинные истории из данных,
        # различимых в этом же прогоне; блокирующая находка 2 ревью PR #466,
        # AGENTS.md «алерт не гадает»).
        if episode_opens:
            # WIP перестал быть действующим тормозом с началом паузы —
            # закрываем эпизод: stuck-часы считают только время, когда гейт
            # реально держал диспутч. После снятия предохранителя эпизод
            # откроется заново (если счётчик всё ещё ≥ лимита) с чистого
            # листа — сам, без ручного участия.
            try:
                post_issue_comment(
                    repo, WATCHDOG_ISSUE,
                    f"✅ {WIP_GATE_CLOSE_MARKER}\nДиспутч погашен предохранителем конвейера "
                    "(#120): в паузу действующий тормоз — предохранитель, а не WIP-лимит, "
                    "поэтому эпизод WIP закрыт. После снятия предохранителя при счётчике "
                    "всё ещё ≥ лимита эпизод откроется заново, часы — с чистого листа.",
                )
                actions.append(
                    "⏸️ WIP-эпизод закрыт на время паузы предохранителя (#120): "
                    "действующий тормоз — предохранитель, stuck-часы не тикают")
            except RuntimeError as error:
                actions.append(f"⚠️ закрытие WIP-эпизода в #{WATCHDOG_ISSUE} не оставлено: {error}")
        return observations, actions, False

    if count < WIP_LIMIT:
        if episode_opens:
            try:
                post_issue_comment(
                    repo, WATCHDOG_ISSUE,
                    f"✅ {WIP_GATE_CLOSE_MARKER}\n"
                    f"Открытых PR, ждущих доработки: {count} < {WIP_LIMIT} — WIP-лимит снят, "
                    "новые задачи снова диспетчируются.",
                )
                actions.append(f"✅ WIP-лимит снят: {count} < {WIP_LIMIT} — эпизод в #{WATCHDOG_ISSUE} закрыт")
            except RuntimeError as error:
                actions.append(f"⚠️ закрытие эпизода WIP-гейта в #{WATCHDOG_ISSUE} не оставлено: {error}")
        else:
            observations.append(
                f"🟢 WIP: {count} PR ждут доработки (лимит {WIP_LIMIT}) — новые задачи разрешены")
        return observations, actions, True

    # count >= WIP_LIMIT — новые задачи не берутся. Строка обязана появиться
    # в отчёте В ЛЮБОМ случае (тормоз называет газ, AGENTS.md) — не только
    # когда эпизод только что открылся: наблюдение, отдельное от факта
    # простановки маркера ниже (тот — действие, само решение — нет).
    line = (f"⏸️ новые задачи не берутся: {count} открытых PR ждут доработки при лимите "
            f"{WIP_LIMIT} — сначала доводим (газ: слияние/закрытие PR ниже порога снимает "
            "тормоз автоматически следующим прогоном)")
    observations.append(line)
    if not episode_opens:
        try:
            post_issue_comment(
                repo, WATCHDOG_ISSUE,
                f"⏸️ {WIP_GATE_OPEN_MARKER}\n"
                f"Открытых PR, ждущих доработки: {count} ≥ лимита {WIP_LIMIT}. Новые задачи не "
                "диспетчируются, пока очередь не поредеет.",
            )
            actions.append(f"⏸️ WIP-лимит закрыл диспатч: {count} ≥ {WIP_LIMIT} — эпизод в #{WATCHDOG_ISSUE} открыт")
            episode_opens = [now]
        except RuntimeError as error:
            actions.append(f"⚠️ маркер WIP-гейта в #{WATCHDOG_ISSUE} не оставлен: {error}")
            return observations, actions, False

    episode_start = min(episode_opens)
    stuck_hours = minutes_between(episode_start, now) / 60
    if stuck_hours > WIP_GATE_STUCK_HOURS:
        try:
            stuck_markers = [
                t for t in issue_marker_times(repo, WATCHDOG_ISSUE, WIP_GATE_STUCK_MARKER_PREFIX)
                if t >= episode_start
            ]
        except RuntimeError as error:
            # Находка ревью PR #466: чтение упало — не то же самое, что
            # «маркеров нет», и не повод эскалировать вслепую (та же
            # политика, что уже применяет ветка недоступных маркеров в
            # начале функции, и escalate_if_new в repo_invariants). Раньше
            # `stuck_markers = []` в except делало ровно это — при
            # ПОСТОЯННОМ сбое чтения (не разовом) эскалация уходила заново
            # каждый пульс (каждые 15 минут), а не один раз на эпизод.
            # Пропускаем этот прогон целиком — сигнал уйдёт следующим
            # успешным чтением, warning уже есть в actions.
            actions.append(f"⚠️ маркеры затянувшегося WIP-гейта в #{WATCHDOG_ISSUE} не прочитаны: {error}")
            return observations, actions, False
        if not stuck_markers:
            # Текст сигнала (находка ревью PR #466 + AGENTS.md «алерт не
            # гадает»): не утверждает непроверяемое «воркер всё это время
            # диспетчируется на доводку» и не предлагает владельцу выбрать
            # между гипотезами — причины различает сам по данным этого же
            # прогона (pool × pulls, второго обхода нет). Есть свободные
            # задачи с открытым PR — доводка гейтом не запрещена и адресуется
            # воркеру (dispatch_worker), значит очередь, не редеющая 8ч,
            # означает «не успевает». Таких задач нет — «доводить нечего»,
            # диспатчей доводки не было вовсе.
            targets = rework_target_numbers(pulls, pool)
            conflict_targets = (free_task.conflict_declared_tasks(pulls)
                                & {issue["number"] for issue in pool if not issue["assignees"]})
            if targets:
                listed = ", ".join(f"#{n}" for n in sorted(targets))
                rework_fact = (
                    f"доводить есть что: свободные задачи с уже открытым PR — {listed}; "
                    "закрытый гейт им доводку не запрещает (dispatch_worker), слияния идут "
                    "независимо (merge_loop) — очередь не редеет, значит доводка не "
                    "успевает за темпом появления находок ревью"
                )
            elif conflict_targets:
                # Блокирующая находка 1 ревью PR #466: конфликтные PR — тоже
                # доводка, но своя (#474): одна авто-попытка ребейза с
                # бюджетом по маркеру, после исчерпания — эскалация и ожидание
                # человека. Молчать об этом («доводить нечего») или звать это
                # «доводкой, которая не успевает» — обе неточности: алерт
                # называет свой цикл и его газ.
                listed = ", ".join(f"#{n}" for n in sorted(conflict_targets))
                rework_fact = (
                    f"адресная доводка (dispatch_worker) недоступна: единственные свободные "
                    f"задачи с открытым PR — конфликтные ({listed}); их ведёт авто-расшивка "
                    "конфликтов #474 (ровно одна попытка ребейза, бюджет по маркеру, после "
                    "исчерпания — отдельная эскалация владельцу); если конфликт после "
                    "попытки не разошёлся — очередь ждёт ручного разрешения"
                )
            else:
                rework_fact = (
                    "доводить нечего: ни у одной свободной задачи нет открытого PR — "
                    "диспатчей доводки не было вовсе, очередь ждёт ручной доработки "
                    "владельца"
                )
            escalation = escalate(
                repo, WATCHDOG_ISSUE,
                f"🚨 edge-harness: {WIP_GATE_STUCK_MARKER_PREFIX}{WIP_GATE_STUCK_HOURS}ч]\n"
                f"WIP-лимит держит взятие НОВЫХ задач закрытым {int(stuck_hours)}ч (порог "
                f"{WIP_GATE_STUCK_HOURS}ч): {count} PR ждут доработки при лимите {WIP_LIMIT}. "
                f"{rework_fact} — затор не в притоке новых задач, а в доработке существующих: "
                "требует внимания владельца.",
            )
            actions.append(
                f"🚨 WIP-гейт держит взятие новых задач {int(stuck_hours)}ч > "
                f"{WIP_GATE_STUCK_HOURS}ч ({escalation})")

    return observations, actions, False


def declared_pr_task_numbers(pulls: list[dict]) -> set[int]:
    """Номера задач, у которых уже есть открытый PR — единственный источник
    task_ref.resolve_pr_task (имя agent-ветки, #394), тот же узкий источник
    (`task_from_branch`), что использует `scripts/lib/free_task.py::
    declared_pr_for_task` для того же вопроса симметрично со стороны task.sh.
    Не `pr_references_issue` — та широкая (любое упоминание в прозе,
    #286/#699) и для этого вопроса не годится."""
    numbers = set()
    for pull in pulls:
        number = task_ref.resolve_pr_task(pull)
        if number is not None:
            numbers.add(number)
    return numbers


def rework_target_numbers(pulls: list[dict], pool: list[dict]) -> set[int]:
    """Свободные задачи, у которых уже есть открытый ПР БЕЗ метки `conflict`:
    единственные, кого dispatch_worker вправе диспетчить при закрытом
    WIP-гейте (доводка, #245), и тот же счётчик для stuck-эскалации wip_gate —
    алерт называет факт «доводить есть что/нечего», а не гадает между
    гипотезами (AGENTS.md). Conflict-объявленные задачи исключены (блокирующая
    находка 1 ревью PR #466): у них свой цикл с решением владельца (#474) —
    ровно одна авто-попытка ребейза с бюджетом по маркеру, который ведёт
    dispatch_conflict_rework (работает и при закрытом гейте); адресная ветка
    маркер бюджета не ставит, поэтому без исключения диспетчила бы
    конфликтный PR каждые 15 минут бесконечно, невидимо для бюджета, по
    которому владелец уже эскалирован. `free_task.conflict_declared_tasks` —
    то же одно место правды, что применяет общий выбор воркера (#478). Оба
    слагаемых уже прочитаны этим же прогоном: `pool` — пул задач, `pulls` —
    снимок открытых PR (#443/#456), второго обхода нет."""
    declared = declared_pr_task_numbers(pulls) - free_task.conflict_declared_tasks(pulls)
    return {issue["number"] for issue in pool if not issue["assignees"]} & declared


def dispatch_worker(
    repo: str, pool: list[dict], *, wip_allowed: bool, pulls: list[dict],
) -> tuple[list[str], list[str]]:
    """Пульс конвейера: свободная задача есть, воркер простаивает → ровно один
    dispatch worker.yml за запуск оркестратора. Best-effort по построению:
    прав на dispatch нет, workflow нет на main, сеть — любой сбой диспатча
    не роняет оркестратор, слияния важнее подряда воркеру.

    `wip_allowed` — решение wip_gate (issue #464): False не запрещает диспатч
    целиком (находка ревью PR #466 — WIP-гейт, закрывающий ВЕСЬ dispatch_worker,
    держит недоступной и доводку уже открытых PR, а `free_task.free_candidates`
    считает такую задачу свободной именно затем, чтобы её довели, — тормоз без
    газа: очередь физически не может разгрестись сама). При wip_allowed=False
    эта функция по-прежнему вправе адресно запустить воркера, но только на
    задачу, у которой УЖЕ есть открытый PR (declared_pr_task_numbers по
    `pulls` — тот же снимок, что main() передал в wip_gate): worker.yml
    получает конкретный номер через input `task` (task.sh, режим #245 —
    «PR уже открыт, довожу его, новый не открываю»), поэтому воркер не
    может вместо этого случайно подхватить настоящую новую задачу тем же
    вызовом free_task() без аргумента. Если среди свободных задач ни одна не
    объявлена открытым PR — доводить нечего, диспатча не будет, гейт держит
    свой тормоз честно.

    Возвращает (наблюдения, действия) — разведено по #456: «воркер уже
    работает — dispatch не нужен» ничего не меняет, это факт состояния, а не
    действие этого прогона."""
    observations: list[str] = []
    actions: list[str] = []
    # Есть ли ХОТЬ ОДНА свободная — дешёвая проверка на уже полученном
    # REST-пуле (assignees приходит с ним бесплатно, лишнего запроса не надо).
    if not free_task.free_candidates(pool):
        return observations, actions
    try:
        if worker_runs_active(repo):
            observations.append("👷 воркер уже работает — dispatch не нужен")
            return observations, actions
        if not wip_allowed:
            # WIP-гейт закрыл взятие новых задач — только доводка уже открытых
            # PR. Приоритет (#361) сюда не тянется: это АДРЕСНЫЙ прогон —
            # worker.yml получает конкретный номер через input `task`, воркер
            # ничего не выбирает, поэтому и граф блокировок для выбора не
            # нужен (лишний GraphQL-запрос при закрытом гейте не делаем);
            # среди нескольких целей берём старейшую по номеру — детерминизм.
            # rework_target_numbers — то же одно место правды, что читает
            # stuck-эскалация wip_gate: у алерта и у диспатча не могут
            # разойтись ответы на вопрос «есть ли что доводить».
            targets = rework_target_numbers(pulls, pool)
            rework_free = sorted(
                (issue for issue in free_task.free_candidates(pool) if issue["number"] in targets),
                key=lambda issue: issue["number"],
            )
            if not rework_free:
                observations.append(
                    "⏸️ WIP-лимит закрыл новые задачи, и ни у одной свободной задачи ещё "
                    "нет открытого PR — доводить нечего, dispatch не запущен"
                )
                return observations, actions
            target = rework_free[0]["number"]
            gh(
                "-X", "POST",
                f"repos/{repo}/actions/workflows/worker.yml/dispatches",
                "-f", "ref=main", "-f", f"inputs[task]={target}",
            )
            actions.append(
                f"👷 WIP-лимит закрыл новые задачи, но задача #{target} уже с открытым PR — "
                "worker.yml запущен адресно на её доводку"
            )
            return observations, actions
        # Имя задачи в отчёте — та же функция приоритета (#361), что реально
        # использует scripts/worker/task.sh (scripts/lib/free_task.py), не
        # локальная сортировка по номеру мимо неё (была вторым местом правды
        # до этого change). Полный приоритет (мета → блокирует открытых →
        # номер) требует графа блокировок — GraphQL, отдельный от REST-пула
        # запрос; сбой ЭТОГО дополнительного запроса не должен останавливать
        # сам dispatch — деградирует до приоритета по REST-пулу (мета+номер,
        # без графа), видимым предупреждением, не молча и не падением.
        try:
            named_pool = task_deps.fetch_pool(repo, gh_call=gh)
        except Exception as error:  # noqa: BLE001 — деградация к REST-пулу, не падение пульса
            observations.append(f"⚠️ граф блокировок недоступен для отчёта (не критично): {error}")
            named_pool = pool
        candidates = free_task.prioritized_free(named_pool)
        gh(
            "-X", "POST",
            f"repos/{repo}/actions/workflows/worker.yml/dispatches",
            "-f", "ref=main",
        )
        if candidates:
            actions.append(
                f"👷 свободная задача #{candidates[0]['number']} — worker.yml запущен "
                "(воркер сам назначится и откроет PR)"
            )
        else:
            actions.append("👷 worker.yml запущен (воркер сам назначится и откроет PR)")
    except RuntimeError as error:
        actions.append(f"⚠️ dispatch воркера не удался (не критично): {error}")
    return observations, actions


# ── #196, поведение 1: готовый PR без вердикта — дёрнуть гейт самому ─────────────
# Триггер: гейт 1 отработал (review:ok ИЛИ review:large — review_labels.
# gate1_decided, #432; или ai:failed — «ревью не состоялось ИЛИ провалено»,
# ADR 0007) дольше AI_REVIEW_RETRY_AFTER_MINUTES с момента публикации
# commit status `harness/review` на ТЕКУЩЕМ head (review_labels.
# status_posted_at). ai:changes-requested и ai:ok сюда не попадают — это не
# «нет вердикта», это готовый вердикт (обрабатывает unhealthy_pulls/
# merge_queue соответственно).
#
# Якорь — commit status, не таймлайн-событие "labeled" (находка ревью #424,
# живой класс): #203 сделал перестановку вердикт-меток идемпотентной — новый
# пуш с ТЕМ ЖЕ вердиктом больше не выбрасывает "labeled" вовсе, и таймер,
# отсчитывающий от последней перестановки, замер бы навсегда на первой
# простановке. `check_pr.py` публикует `harness/review` КАЖДЫМ прогоном
# безусловно на точный head (#345, второй канал для required status
# checks) — этот сигнал не зависит от идемпотентности метки и обновляется
# при каждом пуше, даже без смены вердикта.
#
# Носитель счётчика попыток — комментарий-маркер AI_REVIEW_RETRY_MARKER в самом
# PR (issues/{n}/comments — тот же endpoint, что и у задач, PR это issue).
# Обоснование выбора: 1) переживает перезапуск оркестратора (крон каждые 15 мин,
# память процесса не сохраняется) — комментарий читается заново каждым запуском,
# как это уже делает pulse_guard для маркеров серий; 2) не плодит новую метку
# в namespace review_labels.py (там только вердикты, не попытки); 3) виден
# человеку без доп. тулинга — то же качество, что у существующих следов
# reap_stale/mark_conflicts.
#
# #431 (тормоз без газа, живой случай #367/#249): счётчик считался за ВСЮ
# историю PR — после первых же AI_REVIEW_MAX_ATTEMPTS маркеров (пусть даже
# растянутых на несколько РАЗНЫХ эпох review:ok) PR терял газ навсегда, а
# исчерпание не вело ни к какому видимому действию. Теперь: 1) счётчик
# считается только с якоря ТЕКУЩЕЙ эпохи (ai_review_retry_count(since=anchor)) —
# новый пуш даёт свежий бюджет; 2) исчерпание эскалирует тем же каналом, что
# предохранитель конвейера (issue #120 + Telegram), идемпотентно на PR+эпоху;
# 3) quota_exhausted (провайдер, ai_dsh.sh/#419) не тратит автоповтор вовсе —
# ретраить исчерпанную на неделю квоту внутри CI бессмысленно, эскалация сразу.


def last_gate1_labeled_at(repo: str, pull: dict) -> datetime | None:
    """Момент публикации commit status `harness/review` на ТЕКУЩЕМ head PR
    (review_labels.status_posted_at, #345) — якорь для таймера #196,
    заменивший таймлайн-событие "labeled" (находка ревью #424): с #203
    перестановка вердикт-меток идемпотентна, и `labeled` по гейту 1 не
    выбрасывается вовсе, если новый пуш подтвердил ТОТ ЖЕ вердикт — старый
    якорь замерзал на первой простановке метки навсегда. Commit status
    публикуется `check_pr.py` каждым прогоном безусловно (второй канал
    вердикта, #345), поэтому обновляется на каждом пуше, даже без смены
    решения.

    Раньше эта же логика (таймлайн, review:ok/review:large, #432) была
    задублирована в repo_invariants.py и уже расходилась дважды (#303,
    #432) — теперь обе стороны читают один и тот же
    review_labels.status_posted_at, третьего расхождения не заводим.

    None — статус `harness/review` на этом head не публиковался вовсе."""
    posted = review_labels.status_posted_at(
        repo, pull["head"]["sha"], review_labels.STATUS_REVIEW, gh)
    return parse_time(posted) if posted else None


def ai_review_retry_count(repo: str, pr_number: int, since: datetime | None = None) -> int:
    """Число уже потраченных автоповторов ai-review НА ЭТОМ PR — с момента
    `since` (эпоха текущего review:ok, #431), либо за всю историю, если
    since не задан (совместимость: last_gate1_labeled_at уже гарантирует
    якорь для реальных вызовов trigger_ai_review, но функция остаётся
    честной и без него)."""
    times = issue_marker_times(repo, pr_number, AI_REVIEW_RETRY_MARKER)
    if since is not None:
        times = [t for t in times if t > since]
    return len(times)


def latest_ai_failure_reason(repo: str, pr_number: int, anchor: datetime | None = None) -> str | None:
    """Тег причины последнего вердикта error (review_labels.reason_tag,
    ai_review.build_comment, #431) — факт `reason:` в шапке последнего
    доверенного AI-комментария ЭТОЙ ЭПОХИ (созданного строго после `anchor`
    — той же простановки review:ok/review:large, что уже скоупит
    ai_review_retry_count). None — комментария нет, его вердикт не error,
    тега ещё нет (комментарий написан до #431), либо единственный
    подходящий комментарий старше anchor (эпоха прошлая — причина
    неизвестна В ЭТОЙ эпохе) — тогда решение принимается по общему бюджету,
    как раньше.

    Находка ревью PR #439: без anchor функция читала последний доверенный
    комментарий БЕЗ ограничения по эпохе — пуш с неизменным диффом
    (подтягивание main) переставляет review:ok, открывая новую эпоху N+1, а
    свежий прогон ai-review ещё не ответил (очередь раннеров — иногда часы,
    docs/research/21); trigger_ai_review читал `reason: quota_exhausted` из
    ПРОШЛОЙ эпохи N и закрывал автоповторы новой эпохи, не дав ей ни одной
    попытки."""
    comment = review_labels.latest_ai_comment(repo, pr_number, gh, since=anchor)
    if comment is None:
        return None
    facts = review_labels.header_facts(comment.get("body") or "")
    if facts.get("reviewer") != "error":
        return None
    return facts.get("reason")


# ── #727: авто-повтор не жжётся вслепую в ту же квоту ────────────────────────
# Фактическая причина ai:failed «all_providers_exhausted» несёт факт `reset-at`
# в шапке комментария (ai_review.build_comment, header_facts) — «имя: дата;
# имя2: дата2» для КАЖДОГО опробованного провайдера. Ближайший момент, когда
# повтор МОЖЕТ иметь смысл, — минимум этих дат (первый освободившийся
# провайдер), не максимум: как только освободится хотя бы один, цепочка
# снова жива.
AI_REVIEW_CHAIN_COOLDOWN_MARKER = "[цепочка провайдеров: авто-повтор придержан]"


def parse_reset_hint_dates(reset_hint: str) -> list[datetime]:
    """Разбирает «имя: дата; …» (ai_review.build_comment, #727) в список
    дат — прод-форма встречается в ДВУХ видах: ISO («2026-09-10T00:00:00Z»,
    смоук-фикстура dsh-clients.smoke.sh) и «YYYY-MM-DD HH:MM:SS» без зоны
    (живой прогон 34176910458, «Your limit will reset at 2026-09-10
    08:51:55») — вторая форма читается как UTC (dsh зону не называет, это
    ближайшее разумное допущение, не факт). Строка, которая не разобралась
    ни одним форматом, пропускается молча — вызывающий обязан трактовать
    пустой результат как «даты нет», не как «дата в прошлом».

    С #857 разбор дат делегирован provider_quota_state.parse_reset_hint_pairs
    (одно место правды на формат — тот же самый парсер теперь пишет
    персистентное состояние квоты, см. sync_provider_quota_state ниже).
    Список (не словарь) сохранён ради обратной совместимости вызывающих,
    которым важен только набор дат (min(reset_dates) и т.п.), не имя."""
    return list(provider_quota_state.parse_reset_hint_pairs(reset_hint).values())


def sync_provider_quota_state(repo: str, reset_hint: str, now: datetime) -> str | None:
    """Персистентное состояние квоты провайдеров (vars.DSH_PROVIDER_QUOTA_UNTIL,
    {provider: reset_iso}, design.md provider-quota-gating, #857) — газ
    «провайдер снова успешен ИЛИ срок прошёл» из AGENTS.md «Тормоз без газа
    не принимается»: протухшие записи (`now` перевалил за `reset_iso`)
    снимаются здесь же, попутно с записью нового `reset_hint`. Чейн-раннер
    (`dsh_run_with_provider_chain`, scripts/lib/dsh-ci.sh) читает эту же
    переменную ДО попытки провайдера и пропускает его, не тратя вызов —
    вот для чего пишется состояние.

    Вызывается ИЗ trigger_ai_review там, где уже вычисляется reset-at факт
    последнего комментария ai-review (не заводит второй сетевой обход PR
    ради этого механизма). Пустой reset_hint — вызывающий обязан не звать
    эту функцию вовсе (нечего мержить, а expire_stale без свежего факта не
    даёт достаточного повода тратить сетевой раунд-трип на каждый PR/тик —
    протухание всё равно снимется, когда придёт следующий реальный факт,
    ЛИБО не аффектит корректность гейта: чтение в dsh-ci.sh само сравнивает
    reset_iso с текущим временем).

    Best-effort: сбой чтения/записи переменной не должен ронять остальной
    тик пульса — сюда уже дошли после успешного гейта 1, падать здесь
    значило бы терять остальную работу trigger_ai_review из-за
    второстепенного механизма. Возвращает строку-наблюдение или None
    (состояние не поменялось — не льём дополнительный шум в отчёт)."""
    try:
        state = provider_quota_state.load_quota_state(repo, gh)
        state, expired = provider_quota_state.expire_stale(state, now)
        state, changed = provider_quota_state.merge_reset_hints(state, reset_hint, now)
        if expired or changed:
            provider_quota_state.save_quota_state(repo, state, gh)
    except RuntimeError as error:
        return (
            f"⚠️ квота провайдеров: не удалось обновить vars.{provider_quota_state.QUOTA_VAR_NAME}: "
            f"{error}"
        )
    if not (expired or changed):
        return None
    bits = []
    if changed:
        bits.append(f"обновлено из reset-at ({reset_hint})")
    if expired:
        bits.append(f"сняты протухшие: {', '.join(sorted(expired))}")
    return f"💾 квота провайдеров: {'; '.join(bits)}"


def trigger_ai_review(repo: str, now: datetime, pulls: list[dict]) -> tuple[list[str], list[str]]:
    """Возвращает (наблюдения, действия) — разведено по #456: «бюджет
    авто-повторов исчерпан, не дёргаю снова» ничего не меняет (диспатча не
    было) и раньше попадало в тот же список, что реальный запуск ai-review.yml."""
    observations: list[str] = []
    actions: list[str] = []
    for pull in pulls:
        labels = {label["name"] for label in pull["labels"]}
        if not review_labels.gate1_decided(labels):
            continue  # первый гейт ещё не пройден — рано
        has_verdict = bool(labels & set(review_labels.AI_VERDICTS))
        needs_retry = review_labels.AI_FAILED in labels
        if has_verdict and not needs_retry:
            continue  # ai:ok или ai:changes-requested — вердикт уже есть
        anchor = last_gate1_labeled_at(repo, pull)
        if anchor is None:
            continue  # событие не нашлось — не на чем считать порог, не гадаем
        age = minutes_between(anchor, now)
        if age < AI_REVIEW_RETRY_AFTER_MINUTES:
            continue  # ещё не истёк порог ожидания вердикта
        # needs_retry — два НЕЗАВИСИМЫХ фильтра «не жечь автоповтор вслепую»,
        # оба смотрят на прошлый вердикт ai:failed до общего бюджета ниже
        # (#196/#431/#727 — три задачи, один и тот же принцип «холостой повтор
        # хуже честной эскалации»), проверяются по очереди:
        if needs_retry:
            # 1) Цепочка провайдеров исчерпана целиком (#727) и дата сброса
            # известна и ещё не наступила — авто-повтор НЕ дёргает
            # ai-review.yml вслепую в ту же квоту, эскалирует владельцу вместо
            # этого (идемпотентно на эпизод — маркер держит один Telegram на
            # всё окно ожидания). Сигнал — факт `reset-at` в шапке последнего
            # комментария (может отсутствовать без anchor-скоупинга, читает
            # всю историю PR: список дат сброса не зависит от эпохи review:ok).
            comment = review_labels.latest_ai_comment(repo, pull["number"], gh)
            facts = review_labels.header_facts(comment.get("body") or "") if comment else {}
            reset_hint = facts.get("reset-at", "")
            # #857: персистентное состояние квоты (vars.DSH_PROVIDER_QUOTA_UNTIL)
            # — пишется здесь же, где reset-at факт уже вычислен (не заводит
            # второй сетевой обход ради этого механизма). Пустой факт — нечего
            # мержить, вызов пропускается вовсе (см. sync_provider_quota_state
            # докстринг).
            if reset_hint:
                quota_note = sync_provider_quota_state(repo, reset_hint, now)
                if quota_note:
                    observations.append(quota_note)
            reset_dates = parse_reset_hint_dates(reset_hint)
            if reset_dates and now < min(reset_dates):
                next_viable = min(reset_dates)
                marker = f"{AI_REVIEW_CHAIN_COOLDOWN_MARKER} #{pull['number']}"
                already = issue_marker_times(repo, pull["number"], marker)
                if already:
                    observations.append(
                        f"⏳ PR #{pull['number']}: цепочка провайдеров исчерпана до "
                        f"{next_viable.isoformat()} — авто-повтор придержан (уже "
                        "эскалировано), жду сброса"
                    )
                else:
                    text = (
                        f"🚨 edge-harness: PR #{pull['number']} — все провайдеры цепочки "
                        f"исчерпаны, ближайший сброс {next_viable.isoformat()} — авто-повтор "
                        "(#196) не дёргаю вслепую в ту же квоту.\n\n"
                        "Что дальше:\n"
                        "- Действие по умолчанию: ждать — новый пуш в PR или ручной "
                        "workflow_dispatch ai-review.yml после даты сброса пройдёт как обычно.\n"
                        "- Ускорить: добавить провайдера в vars.DSH_PROVIDER_CHAIN "
                        "(docs/runbooks/switch-llm-provider.md)."
                    )
                    escalation = escalate(repo, WATCHDOG_ISSUE, text)
                    post_issue_comment(repo, pull["number"], f"🤖 {marker}\n{text}\n({escalation})")
                    observations.append(
                        f"🚨 PR #{pull['number']}: цепочка исчерпана до {next_viable.isoformat()} "
                        f"— эскалация вместо авто-повтора ({escalation})"
                    )
                continue
            # 2) Квота провайдера исчерпана надолго (#431/#419), скоуплено ЭТОЙ
            # эпохой (anchor) — тег причины `reason: quota_exhausted` в шапке,
            # не требует известной даты сброса (в отличие от (1) выше): ждать
            # квоту внутри CI бессмысленно в любом случае, эскалация с первого
            # обнаружения, автоповтор не тратится вовсе.
            reason = latest_ai_failure_reason(repo, pull["number"], anchor=anchor)
            if reason == review_labels.FAILURE_REASON_QUOTA_EXHAUSTED:
                marker = f"{AI_REVIEW_QUOTA_MARKER} #{pull['number']}"
                try:
                    already = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
                except RuntimeError as error:
                    observations.append(
                        f"⚠️ PR #{pull['number']}: не смог сверить эскалацию квоты "
                        f"#{WATCHDOG_ISSUE}: {error}"
                    )
                    continue
                if any(marker_at > anchor for marker_at in already):
                    continue  # уже эскалировано в этой эпохе — молчим
                text = (
                    f"🚨 edge-harness: {marker}\n"
                    f"PR #{pull['number']}: ai-review упал с {review_labels.AI_FAILED} "
                    f"(квота провайдера исчерпана надолго, {int(age)} мин без вердикта) — "
                    "повтор внутри CI бессмысленен (docs/runbooks/switch-llm-provider.md). "
                    "Автоповтор не трачу: нужно сменить провайдера или дождаться сброса "
                    "квоты, затем новый пуш заведёт ревью заново."
                )
                result = escalate(repo, WATCHDOG_ISSUE, text)
                actions.append(
                    f"🚨 PR #{pull['number']}: квота провайдера исчерпана — эскалировано "
                    f"({result}), автоповтор не трачу"
                )
                continue

        attempts = ai_review_retry_count(repo, pull["number"], since=anchor)
        if attempts >= AI_REVIEW_MAX_ATTEMPTS:
            marker = f"{AI_REVIEW_EXHAUSTED_MARKER} #{pull['number']}"
            try:
                already = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
            except RuntimeError as error:
                observations.append(
                    f"⚠️ PR #{pull['number']}: не смог сверить эскалацию исчерпания "
                    f"#{WATCHDOG_ISSUE}: {error}"
                )
                continue
            if any(marker_at > anchor for marker_at in already):
                observations.append(
                    f"⏸️ PR #{pull['number']} без вердикта AI {int(age)} мин, но "
                    f"авто-повторов уже {attempts}/{AI_REVIEW_MAX_ATTEMPTS} в этой эпохе — "
                    "уже эскалировано"
                )
                continue
            text = (
                f"🚨 edge-harness: {marker}\n"
                f"PR #{pull['number']}: авто-повторов ai-review уже "
                f"{attempts}/{AI_REVIEW_MAX_ATTEMPTS} с последней простановки review:ok, "
                f"вердикта нет {int(age)} мин — дальше нужен человек (новый пуш заведёт "
                "свежий бюджет, ручной `gh workflow run ai-review.yml -f pr=N` тоже работает)."
            )
            result = escalate(repo, WATCHDOG_ISSUE, text)
            actions.append(
                f"🚨 PR #{pull['number']}: авто-повторы исчерпаны "
                f"({attempts}/{AI_REVIEW_MAX_ATTEMPTS}) — эскалировано ({result})"
            )
            continue

        # Разрыв 2 (#779, самый дорогой из трёх — живой замер: три диспатча
        # этой же функции по PR #711 за 7 минут, 21:03:44Z/21:07:23Z/21:10:30Z,
        # весь бюджет эпохи на ОДНОМ отпечатке диффа): до этой строки функция
        # проверяла гейт 1, наличие вердикта, порог возраста, кулдаун цепочки
        # провайдеров, quota_exhausted и бюджет попыток — «летит ли прогон
        # ЭТОГО PR прямо сейчас» в списке не было вовсе. Один и тот же
        # предикат, что уже читают update_branch (AiReviewRunning выше) и
        # ai_review.py::cmd_should_run (#399) — третьей копии не заводим.
        # Проверка — ПЕРЕД самим диспатчем и ПЕРЕД маркер-комментарием
        # AI_REVIEW_RETRY_MARKER: попытка (ai_review_retry_count) считается
        # только по факту этого маркера, поэтому отказ здесь не тратит
        # бюджет — следующий проход планировщика увидит тот же ai:failed и
        # попробует снова, как только текущий прогон освободит счётчик
        # активных (тот же газ, что у AiReviewRunning: чтение, разнесённое
        # по времени).
        active = review_labels.other_active_ai_review_runs(
            repo, pull["number"], exclude_run_id=None, gh_func=gh)
        if active:
            run_ids = ", ".join(str(run.get("id")) for run in active)
            observations.append(
                f"⏳ PR #{pull['number']}: ai-review.yml уже летит (run {run_ids}) — "
                "автоповтор не дублирую, попытка не потрачена"
            )
            continue

        gh(
            "-X", "POST", f"repos/{repo}/actions/workflows/ai-review.yml/dispatches",
            "-f", "ref=main", "-f", f"inputs[pr]={pull['number']}",
        )
        # Причина в сообщении — ai:failed, если он и есть настоящий повод
        # (needs_retry), иначе фактическая метка гейта 1 на PR (review:ok
        # или review:large, #432) — не жёстко "review:ok", как было раньше.
        gate1_label = next((l for l in review_labels.GATE1_LABELS if l in labels), review_labels.REVIEW_OK)
        post_issue_comment(
            repo, pull["number"],
            f"🤖 {AI_REVIEW_RETRY_MARKER} Оркестратор сам запустил ai-review.yml: "
            f"{gate1_label if not needs_retry else review_labels.AI_FAILED} держится "
            f"{int(age)} мин без готового вердикта (попытка {attempts + 1}/{AI_REVIEW_MAX_ATTEMPTS}).",
        )
        actions.append(
            f"🤖 PR #{pull['number']}: ai-review.yml запущен оркестратором "
            f"(попытка {attempts + 1}/{AI_REVIEW_MAX_ATTEMPTS}, {int(age)} мин без вердикта)"
        )
    return observations, actions


# ── Разблокировка уже застрявших ai:ok+review:large без large-ok (#939) ──────
#
# Мандат владельца 2026-09-11: эскалация владельцу по причине размера
# (review:large сверх check_pr.LARGE_DIFF_HUGE_LINES, старое поведение
# large_ok_decision.'escalate', #204/#901) убрана из ai_review.py целиком —
# huge_diff_size_gate теперь читает ОТДЕЛЬНОЕ суждение модели о размере из
# КАЖДОГО НОВОГО вердикта. Но у PR, получивших ai:ok ДО этой правки (живые
# случаи — #870 +2472, #408 +3548), вердикт уже вынесен: should_run_ai_review
# не назначит новый прогон, пока дифф PR не изменится (issue #901,
# «нового вердикта не будет, пока не сдвинется голова PR») — фикс политики
# сам по себе НЕ разблокирует уже стоящие PR, это отдельный, известный класс
# тормоза без газа (AGENTS.md).
#
# Газ — тот же путь, что уже даёт владельцу ai-review.yml для пересмотра
# НЕИЗМЕНИВШЕГОСЯ диффа (#294, workflow_dispatch с input force: true,
# cmd_should_run обходит сверку отпечатка): пульс форсирует ОДИН такой
# прогон на каждый застрявший PR — свежий ответ модели пройдёт через уже
# исправленный huge_diff_size_gate и либо получит review:large-ok, либо
# станет rework/error с конкретной причиной, тем же путём, что и любой
# новый PR. Идемпотентность — маркер-комментарий в самом PR (тот же приём,
# что AI_REVIEW_RETRY_MARKER выше): один форс-прогон на PR, не бесконечный
# повтор, если модель не даст суждения о размере во второй раз тоже (тогда
# huge_diff_size_gate уже отправит его в ai:failed — тот дальше идёт по
# обычному циклу автоповтора #196/trigger_ai_review, второй копии здесь не
# заводим).
STUCK_LARGE_OK_MARKER = "[авто-пересмотр размера #939]"


def stuck_large_ok_pulls(repo: str, pulls: list[dict]) -> tuple[list[str], list[str]]:
    """PR с ai:ok + review:large, но БЕЗ review:large-ok — суждения о размере
    ещё не было (вердикт вынесен до #939) — получают ОДИН форсированный
    прогон ai-review.yml. Возвращает (наблюдения, действия), тот же формат,
    что и остальные поведения #196 в этом файле."""
    observations: list[str] = []
    actions: list[str] = []
    for pull in pulls:
        labels = {label["name"] for label in pull["labels"]}
        if review_labels.AI_OK not in labels or review_labels.REVIEW_LARGE not in labels:
            continue
        if review_labels.LARGE_OK in labels:
            continue
        marker = f"{STUCK_LARGE_OK_MARKER} #{pull['number']}"
        already = issue_marker_times(repo, pull["number"], marker)
        if already:
            observations.append(
                f"⏸️ PR #{pull['number']}: авто-пересмотр размера уже запущен, жду свежий вердикт"
            )
            continue
        active = review_labels.other_active_ai_review_runs(
            repo, pull["number"], exclude_run_id=None, gh_func=gh)
        if active:
            observations.append(
                f"⏳ PR #{pull['number']}: ai-review.yml уже летит — авто-пересмотр размера подождёт"
            )
            continue
        gh(
            "-X", "POST", f"repos/{repo}/actions/workflows/ai-review.yml/dispatches",
            "-f", "ref=main", "-f", f"inputs[pr]={pull['number']}", "-f", "inputs[force]=true",
        )
        post_issue_comment(
            repo, pull["number"],
            f"🤖 {marker} Оркестратор форсирует пересмотр ai-review.yml: вердикт "
            "ai:ok получен до правки политики размера (#939) — модель тогда не "
            "судила объём диффа явно, старый ai:ok сам по себе больше не "
            "подтверждает размер. Новый прогон пройдёт через huge_diff_size_gate "
            "и поставит review:large-ok, если объём оправдан, либо rework/"
            "ai:failed с причиной, как у любого нового PR — решения владельца "
            "не требуется.",
        )
        actions.append(
            f"🤖 PR #{pull['number']}: авто-пересмотр размера запущен (ai:ok без large-ok, #939)"
        )
    return observations, actions


# ── #196, поведение 2: нездоровый PR — вернуть задачу в пул ──────────────────────
# Красный обязательный чек ИЛИ ai:changes-requested дольше UNHEALTHY_PR_AFTER_MINUTES
# (отсчёт — updated_at PR: в отличие от review:ok, здесь нет перелейбловки на
# каждый пуш, «нездоровье» живёт, пока его не почини́ли, — updated_at не тикает,
# пока PR не тронули). Идемпотентность без отдельного маркера: снятие assignee
# у задачи делает её невидимой для follow-up вызовов reap_stale/этой же функции
# (issue["assignees"] пуст), тот же приём, что уже использует reap_stale.


def pr_is_unhealthy(repo: str, pull: dict) -> str | None:
    """Причина нездоровья PR или None, если PR здоров. Черновик и уже
    помеченный conflict исключены: conflict — отдельный класс (mark_conflicts),
    там уже есть свой цикл ручного разрешения."""
    labels = {label["name"] for label in pull["labels"]}
    if pull.get("draft") or CONFLICT_LABEL in labels:
        return None
    if review_labels.AI_CHANGES in labels:
        return f"метка {review_labels.AI_CHANGES}"
    bad = pr_bad_checks(repo, pull)
    if bad:
        return f"красные проверки: {', '.join(bad)}"
    return None


def unhealthy_pulls(repo: str, now: datetime, pulls: list[dict], *, pool: list[dict]) -> list[str]:
    """pool — тот же снимок open_task_issues(repo), что уже прочитан для
    reap_stale выше (#443, см. её докстринг о причинах одного снимка на
    прогон): reap_stale к этому моменту уже могла обнулить assignees части
    issue-объектов ПРЯМО В pool — эта функция обязана видеть то же
    актуальное состояние, не более старую копию своим отдельным запросом.

    Сопоставление PR → задача — ТОЛЬКО через task_ref.resolve_pr_task (имя
    agent-ветки, #394), НЕ через pr_references_issue (#286). Класс прежней
    ошибки: pr_references_issue матчит ЛЮБОЕ упоминание номера в прозе тела
    PR — годится для reap_stale (там ошибка «не потрогали» дёшева), но не
    здесь: unhealthy_pulls СНИМАЕТ исполнителя и замок с найденной задачи, а
    прозу тела чужого PR легко упомянуть контекстом, не декларацией (живой
    случай 2026-09-04: тело PR #181 упомянуло #90, и #90 дважды лишилась
    аренды — PR #181 при этом был чужой, его собственная задача из имени
    ветки была #179). Здесь шире — вреднее: PR без agent-ветки нужной формы
    не сопоставляется ни с одной задачей вовсе, «наверное эта» не выбираем."""
    lines = []
    # Заметки-итоги в сессии раннера (#480) — один логин на весь обход, см.
    # append_session_notes.
    session_notes: list[tuple[int, str]] = []
    for issue in pool:
        if not issue["assignees"]:
            continue
        if _issue_is_blocked(issue):
            continue
        number = issue["number"]
        referencing = [pull for pull in pulls if task_ref.resolve_pr_task(pull) == number]
        if not referencing:
            continue
        for pull in referencing:
            reason = pr_is_unhealthy(repo, pull)
            if reason is None:
                continue
            age = minutes_between(parse_time(pull["updated_at"]), now)
            if age < UNHEALTHY_PR_AFTER_MINUTES:
                continue
            who = ", ".join(a["login"] for a in issue["assignees"])
            gh(
                "-X", "DELETE", f"repos/{repo}/issues/{number}/assignees",
                "-f", f"assignees[]={who}",
            )
            # Локальная мутация вслед за серверной (см. докстринг pool у
            # reap_stale) — дальнейшие потребители того же снимка (main(),
            # reject_reopened_tasks, accept_merged_tasks) обязаны увидеть
            # снятие сразу, без отдельного перечитывания.
            issue["assignees"] = []
            try:
                lines.append(f"🔓 {claim_task.release(repo, int(number))}")
            except RuntimeError as error:
                lines.append(f"⚠️ замок task-{number} не снят: {error}")
            post_issue_comment(
                repo, number,
                f"♻️ Задача возвращена в пул оркестратором: PR #{pull['number']} нездоров "
                f"дольше {UNHEALTHY_PR_AFTER_MINUTES} мин ({reason}). "
                f"PR не закрыт — доработай существующий #{pull['number']}, не переделывай с нуля.",
            )
            lines.append(
                f"♻️ #{number} возвращена в пул: PR #{pull['number']} нездоров "
                f"{int(age)} мин ({reason})"
            )
            session_notes.append(
                (number, f"♻️ Задача #{number} возвращена в пул: PR #{pull['number']} нездоров ({reason})."))
            break  # одной причины на задачу достаточно — не дублируем комментарии
    note_lines, _note_hard_failure = append_session_notes(session_notes)
    lines += note_lines
    return lines


# ── issue #269: PR полностью готов, а слияния не произошло — газ обязан гореть ──
# unhealthy_pulls (выше) ловит нездоровые PR (красный чек/ai:changes-requested).
# Этот инвариант — противоположный случай: PR ЗДОРОВ и готов (оба вердикта,
# зелёные проверки, mergeable_state допускает слияние), но остался несмерженным
# дольше порога. merge_queue сливает не более одного PR за проход — если
# кандидатов несколько или события/запуски долго не случались (issue #269 —
# как раз про это), готовый PR может прождать часами при формально зелёных
# прогонах. merge_loop (#297) гоняет проходы подряд внутри одного запуска,
# пока есть что сливать, но не отменяет сам инвариант — это обоснование
# потолка/таймаута цикла, а не остаточный баг.


def last_ready_labeled_at(repo: str, pull: dict) -> datetime | None:
    """Момент, когда PR стал полностью готов к слиянию: позже из двух commit
    status'ов на ТЕКУЩЕМ head — `harness/review` и `harness/ai-review`
    (review_labels.status_posted_at, #345). None — какой-то из двух статусов
    на этом head не публиковался вовсе (например, гейт ещё не отработал) —
    тогда возраст не считаем, не гадаем по неполным данным.

    Якорь — commit status, не таймлайн-событие "labeled" (находка ревью
    #424, тот же класс, что last_gate1_labeled_at): #203 сделал перестановку
    ОБОИХ вердикт-меток идемпотентной (ai_review.py::cmd_verdict — тем же
    классом, что check_pr.py), и "labeled: review:ok"/"labeled: ai:ok" не
    выбрасываются вовсе, если очередной пуш подтвердил тот же вердикт —
    старый якорь замерзал на первой обоюдной готовности навсегда,
    `stale_ready_pulls` (#269) подавлял бы повторный сигнал условием
    `marker_at > ready_since` бесконечно. Оба статуса публикуются каждым
    прогоном безусловно (#345) — сигнал обновляется на каждом пуше.

    Намеренно ТОЛЬКО review:ok (через STATUS_REVIEW == success, см.
    review_status_state), не review_labels.gate1_decided (#432): готов к
    СЛИЯНИЮ — это merge_label_gate, а он review:large не пропускает
    (блокирует до review:large-ok) — «готовность» здесь про разрешение
    слияния, а не про то, что гейт 1 вообще отработал. Сам commit status
    `harness/review` публикуется при ЛЮБОМ вердикте (ok/changes-requested/
    large), поэтому наличие статуса само по себе не значит «review:ok» —
    вызывающий код (stale_ready_pulls) уже отфильтровал PR по
    pr_is_merge_ready ДО вызова этой функции, здесь фильтрация не
    дублируется."""
    sha = pull["head"]["sha"]
    review_at = review_labels.status_posted_at(repo, sha, review_labels.STATUS_REVIEW, gh)
    ai_at = review_labels.status_posted_at(repo, sha, review_labels.STATUS_AI_REVIEW, gh)
    if not review_at or not ai_at:
        return None
    return parse_time(max(review_at, ai_at))


def pr_is_merge_ready(repo: str, pull: dict) -> bool:
    """Тот же критерий готовности, что merge_queue проверяет перед PUT /merge
    (одно место правды по смыслу — сериализация решения дублирует условие,
    не значение): черновик и неподходящий mergeable_state исключены, ПУСТОЙ
    список check-run'ов исключён явно (#303, находка ревью: pr_bad_checks на
    пустом списке отдаёт [] — «красных нет» — но merge_queue в этом же месте
    (пункт «проверки ещё не заведены») не считает такой PR готовым; без
    отдельной проверки здесь докстринг расходился с кодом на этом самом
    случае), красные проверки исключены, обе метки-гейта обязаны стоять.
    `pull` обязан уже нести mergeable_state (в списке open_pulls его нет —
    вызывающий код читает одиночный PR, как и merge_queue)."""
    if pull.get("draft"):
        return False
    if pull.get("mergeable_state") not in ("clean", "unstable", "has_hooks"):
        return False
    runs = pr_check_runs(repo, pull)
    if not runs:
        return False
    if bad_check_names(runs):
        return False
    labels = {label["name"] for label in pull["labels"]}
    return review_labels.merge_label_gate(labels) is None


def stale_ready_pulls(repo: str, now: datetime, pulls: list[dict]) -> list[str]:
    """Инвариант issue #269: готовый PR не должен ждать слияния дольше
    UNHEALTHY_PR_AFTER_MINUTES — это и есть наблюдаемая величина «задержка
    слияния», а не статус последнего прогона оркестратора (тот может быть
    сплошь success, пока сам прогон не случается достаточно часто). Сигнал —
    тот же канал escalate(), что предохранитель конвейера (#120), идемпотентно
    для КАЖДОГО PR по отдельности: номер PR — часть самого маркера
    (f"{READY_STALL_MARKER} #{n}", по образцу AI_REVIEW_RETRY_MARKER выше),
    а не общий текст на всю задачу-статус #120. Общий маркер без номера
    (#303, находка ревью) даёт ложное молчание: маркер, поставленный по PR
    #301, новее момента готовности #302 — #302 подавлен навсегда, новых
    маркеров по нему уже не будет, пока молчат про #301.

    Маркеры читаются ЛЕНИВО (только для PR, прошедшего фильтры готовности и
    возраста выше) — пустая очередь PR или очередь без просроченных кандидатов
    обязана давать ноль вызовов gh() за маркерами, как и остальные механизмы
    (гвардия холостого хода)."""
    lines: list[str] = []
    for pull in pulls:
        if pull.get("draft"):
            continue
        single = gh(f"repos/{repo}/pulls/{pull['number']}")  # mergeable_state только тут
        candidate = {**pull, "mergeable_state": single.get("mergeable_state")}
        if not pr_is_merge_ready(repo, candidate):
            continue
        ready_since = last_ready_labeled_at(repo, candidate)
        if ready_since is None:
            continue
        age = minutes_between(ready_since, now)
        if age < UNHEALTHY_PR_AFTER_MINUTES:
            continue
        marker = f"{READY_STALL_MARKER} #{pull['number']}"
        try:
            markers = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
        except RuntimeError as error:
            lines.append(f"⚠️ не смог сверить маркеры готовности #{WATCHDOG_ISSUE}: {error}")
            continue
        if any(marker_at > ready_since for marker_at in markers):
            continue  # уже оповещено про эту готовность именно этого PR
        text = (
            f"🚨 edge-harness: {marker}\n"
            f"PR #{pull['number']} готов к слиянию (обе метки-гейта, зелёные проверки) "
            f"{int(age)} мин — дольше {UNHEALTHY_PR_AFTER_MINUTES}, слияния не произошло. "
            "Проверь status.pulse_healthy (cf-worker) и последние прогоны orchestra.yml."
        )
        result = escalate(repo, WATCHDOG_ISSUE, text)
        lines.append(f"🚨 PR #{pull['number']}: готов {int(age)} мин, слияние не идёт ({result})")
    return lines


def upstream_drift_lines(repo: str) -> list[str]:
    """Сигнал дрейфа пина апстрима (#134) — обёртка для main(): сверка не должна
    ронять планировщик (слияния важнее), но и не имеет права молчать: сломанная
    сверка прячет дрейф ровно так же, как её отсутствие до #134. Видимость — ⚠️
    в отчёте пульса; тот же приём, что у collect_stale (#124-класс)."""
    try:
        return upstream_drift_check(repo)
    except RuntimeError as error:
        return [f"⚠️ сверка пина с релизами апстрима не удалась — дрейф сейчас невидим: {error}"]


# ── Приёмка (#227): задача закрывается только по проверяемой улике ──────────────
# Мерж доказывает PR, не готовность задачи (кейс #56/#57) — но напоминание
# «закрой после пост-мерж проверки» (after_merge выше) адресовано исполнителю,
# которого уже нет: разовый job воркера завершился. Здесь — исполнитель,
# который реально вызывается: сам оркестратор, детерминированно, без диспатча
# нового LLM-воркера. Выбор обоснован дважды: 1) большинство переходов из #243
# («мерж есть», «проверки зелёные», «файл существует в main») — вычислимые
# факты, не решения — заводить ради них воркер с DSH (минуты раннера + токены
# провайдера на каждую из 33+ задач в очереди) значит платить за суждение там,
# где хватает правила; 2) worker/task.sh занят другим агентом (PR #247) — им
# и не место: это НЕ «взять задачу из пула», а служебный проход планировщика.
#
# Виды улики — по составу файлов слитого PR (classify_acceptance):
#   deploy  — задет cf-worker/: deploy-worker.yml (не PUSH-триггер под
#             GITHUB_TOKEN — after_merge зовёт его сам) содержит канарейку UI
#             последним шагом (deploy-worker.yml, «Канарейка UI на проде»),
#             так что зелёный прогон = зелёная канарейка; вторая улика —
#             {DSH_EDGE_URL}/api/health отвечает 200 (это публичный health
#             самой этой морды, cf-worker/src/config.ts:healthUrl).
#   script  — обычный код/скрипты/workflow: зелёные check-runs PR — это и есть
#             прогон с выводом, тот же критерий «красного обязательного чека»,
#             что уже использует merge_queue/pr_bad_checks.
#   docs    — только docs/**, openspec/**, *.md: наблюдаемого результата в
#             рантайме по природе нет (правка не исполняется) — законный
#             третий исход, не молчаливое закрытие: закрывается с явным
#             обоснованием и проверкой, что заявленные файлы физически на
#             месте в main (единственная проверяемая форма «источник правды»).
#
# Три исхода, ни один не тихий (см. ACCEPTANCE_*_MARKER ниже):
#   ok/docs — комментарий с уликой (или обоснованием для docs) → issue закрыт.
#   fail    — комментарий с причиной провала → issue НЕ закрыт, снят assignee
#             (задача возвращается в пул на доработку), замок снят.
#   hard failure (возможность сломана: сеть/секрет/API, не «улики нет») —
#             задача не трогается, эскалация в WATCHDOG_ISSUE + Telegram
#             (тот же канал, что #120/#174) — к владельцу, не по умолчанию.
#
# Идемпотентность провала: комментарий-маркер с номером PR ищется ПЕРЕД
# повторной проверкой той же пары (задача, PR) — иначе красная улика спамила
# бы тот же комментарий каждые 15 минут, пока никто не пришлёт новую работу.
# ok/docs идемпотентны по построению: закрытый issue выходит из open_task_issues
# и вторым проходом уже не встретится.
#
# Четвёртый исход — pending дольше ACCEPTANCE_PENDING_HOURS (найдено в разборе
# AI-ревью PR #253): reap_stale больше не трогает задачи из merged (см. выше) —
# единственный путь назад для них теперь эта функция. Улика, которая не
# появляется (например, deploy-worker.yml не запустился и не запустится —
# gh workflow run упал где-то ещё), раньше самовосстанавливалась через
# STALE_HOURS reap, теперь не восстанавливалась бы никак и висела строкой
# «⏳» в каждом пульсе бесконечно. Порог — эскалация тем же каналом, что
# жёсткий сбой (WATCHDOG_ISSUE + Telegram), идемпотентно через тот же
# маркер-паттерн.

ACCEPT_DEPLOY = "deploy"
ACCEPT_SCRIPT = "script"
ACCEPT_DOCS = "docs"

CF_WORKER_PREFIX = "cf-worker/"
DOC_PATH_PREFIXES = ("docs/", "openspec/")

ACCEPTANCE_OK_MARKER = "[приёмка: улика]"
ACCEPTANCE_FAIL_MARKER = "[приёмка: доработка]"
ACCEPTANCE_DOCS_MARKER = "[приёмка: без наблюдаемого результата]"
ACCEPTANCE_PENDING_MARKER = "[приёмка: зависла]"
ACCEPTANCE_ERROR_MARKER = "[приёмка: сбой]"
ACCEPTANCE_PARTIAL_MARKER = "[приёмка: требует проверки человеком]"
ACCEPTANCE_EPIC_MARKER = "[приёмка: эпик завершён]"

# Рядом со STALE_HOURS (то же назначение — «сколько ждать, прежде чем бить
# тревогу», но для другого канала: STALE_HOURS про назначение без PR,
# ACCEPTANCE_PENDING_HOURS — про PR, который слит, но улика не появляется).
ACCEPTANCE_PENDING_HOURS = 6

# Дисклеймер о неполноте в теле закрывающего PR (#335): task_ref.resolve_pr_task
# (#394 — имя ветки) отвечает «к какой задаче относится PR», не «закрывает
# ли он её целиком» — приёмка раньше читала декларацию тела и закрывала
# задачу вопреки собственному тексту PR. Живой случай: PR #303
# объявляет #297 первой строкой и пишет прозой «эта часть — открытая #297»/
# «#269 остаётся открытой до решения #297» — приёмка закрыла #297 вопреки
# дисклеймеру. Список — ОДНО место правды для всех веток приёмки (#335).
# Ложноположительный риск принят намеренно (маркер может относиться к чужому
# упомянутому issue, не к объявленному) — минимально достаточная защита
# сейчас дешевле точного справочника машиночитаемых критериев, развилка и
# цена каждого варианта —
# openspec/changes/acceptance-partial-pr-guard/design.md, следующий шаг —
# задача пула #355.
PARTIAL_DISCLAIMER_MARKERS = (
    "не реализован",   # реализован/-а/-о/-ы — общий префикс всех форм
    "остаётся открыт",  # открытой/-ым/-а — общий префикс
    "стопгэп",
    "propose-фаза",
    "перенесён в #",
    "перенесена в #",
    "перенесено в #",
)

# Сузить ложноположительный класс (#467): маркер внутри абзаца, который
# описывает РАССМОТРЕННУЮ И ОТКЛОНЁННУЮ альтернативу ДРУГОЙ, не заявленной
# здесь работы — не дисклеймер критерия ЭТОЙ задачи. Признак — прозаический,
# минимально достаточный (не машиночитаемое поле, см. #355), одно из
# нескольких характерных слов, которыми исполнитель в этом репозитории
# отмечает «я это обдумал и сознательно не стал делать».
_DISCLAIMER_REJECTED_ALT_SIGNALS = ("рассмотрен", "отклонен", "отклонён", "решил не", "решили не")


def partial_disclaimer(body: str, task_number: int) -> str | None:
    """Найденный маркер неполноты (см. PARTIAL_DISCLAIMER_MARKERS) в теле PR,
    без учёта регистра, или None. Сравнение по подстроке, не по регекспу —
    маркеры уже достаточно специфичны, лишняя мощь регекспа тут не нужна.
    «Ё» нормализуется в «е» с обеих сторон (находка AI-ревью PR #342): «е»
    вместо «ё» в живых телах PR — норма («перенесен в #», не «перенесён»),
    без нормализации маркер молча не находится и приёмка тихо закрывает
    задачу вопреки дисклеймеру — тот самый класс, который #335 и чинит.

    Абзац с маркером (текст между пустыми строками — та же мелкая единица
    прозы, которую исполнитель пишет вокруг самой фразы) НЕ считается
    дисклеймером ИМЕННО ЭТОЙ задачи (`task_number`), если ОБА условия верны
    разом:
    1. абзац не упоминает #task_number (task_ref.references_task — номер с
       границей с обеих сторон, не подстрока);
    2. абзац несёт явный сигнал рассмотренной-и-отклонённой альтернативы
       (см. _DISCLAIMER_REJECTED_ALT_SIGNALS).

    Оба условия разом, не по отдельности — у каждого поодиночке есть живой
    контрпример. Живой ложноположительный случай (#467, #454): PR #455 несёт
    абзац «Дешёвая предпроверка... — рассмотрено и НЕ реализовано» под
    заголовком «Что НЕ сделано в этом PR и почему» — абзац не упоминает #454
    и явно называет рассмотренную-отклонённую альтернативу другой работы;
    собственный критерий #454 при этом выполнен и доказан тестами тем же
    телом PR. Живой ИСТИННО положительный случай, который «нет #N» в одиночку
    сломал бы: PR #382 (задача #370) несёт абзац «Смежные события (не
    реализовано, для отдельного обсуждения)» — тоже не упоминает #370, но и
    не несёт сигнала отклонённой альтернативы (это открытые предложения на
    будущее, не отказ) — #370 сам требует явного решения владельца независимо
    от улик, дисклеймер обязан остаться в силе. Требование обоих условий
    разом различает эти два случая без регрессии (test_partial_disclaimer_*
    в test_scheduler.py — прод-форма обоих тел)."""
    for paragraph in (body or "").split("\n\n"):
        lowered = paragraph.lower().replace("ё", "е")
        for marker in PARTIAL_DISCLAIMER_MARKERS:
            if marker.replace("ё", "е") not in lowered:
                continue
            mentions_own_task = task_ref.references_task(paragraph, task_number)
            looks_like_rejected_alt = any(sig in lowered for sig in _DISCLAIMER_REJECTED_ALT_SIGNALS)
            if not mentions_own_task and looks_like_rejected_alt:
                continue  # похоже на отклонённую альтернативу другой работы, не на дисклеймер этой задачи
            return marker
    return None


# Заголовок эпика в этом репозитории начинается с «ЭПИК» (проверено фактом
# по живым issues: #116, #77, #17 «ЭПИК. Фаза 2…», #115 — все четыре начинаются
# так, вариации «ЭПИК:»/«ЭПИК.» покрыты одним префиксом без двоеточия).
EPIC_TITLE_PREFIX = "ЭПИК"


def is_epic_issue(issue: dict) -> bool:
    """Родительская задача (#335): реализация эпика обычно распределена по
    нескольким PR на дочерние задачи — слитый PR, объявляющий эпик первой
    строкой, не значит «эпик готов целиком», приёмка не должна закрывать
    эпик автоматом. Два признака, оба уже есть в ответе issues API (лишнего
    запроса нет): заголовок с префиксом «ЭПИК» и незакрытые нативные
    sub-issues (`sub_issues_summary`, #202). Ни один не покрывает оба
    случая сам по себе — #115 эпик по заголовку, но `sub_issues_summary`
    у него нулевой (дочерние задачи не оформлены как нативные sub-issues),
    поэтому проверяем оба.

    `sub_issues_summary` подтверждён живым ответом ИМЕННО того эндпоинта,
    которым пользуется `open_task_issues` — `GET issues?state=open&labels=task`
    (не только `GET issues/{n}`): замер 2026-09-05, #77 вернулся с
    `{"total": 8, "completed": 6, "percent_completed": 75}` прямо в
    list-ответе, поле не пустое и не требует отдельного запроса на issue.
    Второй сигнал живой, не мёртвый."""
    title = issue.get("title") or ""
    if title.startswith(EPIC_TITLE_PREFIX):
        return True
    summary = issue.get("sub_issues_summary") or {}
    total = summary.get("total") or 0
    completed = summary.get("completed") or 0
    return total > completed

DSH_EDGE_HEALTH_TIMEOUT = 15


def _is_doc_path(path: str) -> bool:
    # AGENTS.md/CLAUDE.md отдельно не перечисляются: любое имя на .md уже
    # ловится первым условием (находка AI-ревью PR #253 — было мёртвое
    # множество DOC_PATH_NAMES, недостижимое по той же причине).
    return path.endswith(".md") or path.startswith(DOC_PATH_PREFIXES)


def classify_acceptance(filenames: list[str]) -> str:
    """Вид улики по составу изменённых файлов слитого PR — см. блок выше."""
    if any(name.startswith(CF_WORKER_PREFIX) for name in filenames):
        return ACCEPT_DEPLOY
    if filenames and all(_is_doc_path(name) for name in filenames):
        return ACCEPT_DOCS
    return ACCEPT_SCRIPT


def all_merged_pulls(repo: str, per_page: int = 100, max_pages: int = 5) -> list[dict]:
    """Слитые PR (до max_pages*per_page штук — сейчас в репозитории порядка
    сотни слитых, одной-двух страниц хватает с запасом на рост). Один общий
    обход на весь прогон приёмки — дешевле, чем поиск на каждую задачу пула."""
    pulls: list[dict] = []
    page = 1
    while page <= max_pages:
        batch = gh(f"repos/{repo}/pulls?state=closed&per_page={per_page}&page={page}") or []
        if not batch:
            break
        pulls.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return [pull for pull in pulls if pull.get("merged_at")]


def merged_pr_map(pulls: list[dict]) -> dict[int, dict]:
    """Task#N → самый свежий слитый PR за эту задачу. Число берётся из
    `task_ref.resolve_pr_task` (#394, решение владельца 2026-09-06:
    единственный источник — имя agent-ветки, то же соглашение, что уже
    применяет contract_check при авто-назначении). Узкая семантика
    намеренна: широкая (references_task, как в after_merge/reap_stale)
    годится для «не потерять живой PR», но не для «эта работа закрывает эту
    задачу» — там любое упоминание в прозе ложно закрыло бы чужую задачу."""
    best: dict[int, dict] = {}
    for pull in pulls:
        number = task_ref.resolve_pr_task(pull)
        if number is None:
            continue
        current = best.get(number)
        if current is None or pull["merged_at"] > current["merged_at"]:
            best[number] = pull
    return best


def deploy_evidence(repo: str, merged_at: datetime, merge_commit_sha: str | None) -> tuple[str, str]:
    """('ok'|'fail'|'pending', детали). Поднимает RuntimeError только на
    инфраструктурный сбой (DSH_EDGE_URL не задан, /api/health недоступен) —
    это отличается от 'fail' (улика получена и она красная).

    Улика — прогон deploy-worker.yml ИМЕННО ЭТОГО мержа, а не «самый новый
    после merged_at»: оркестратор сливает по одному PR каждые 15 минут, и
    при двух cf-worker-мержах подряд «самый новый» после первого мержа —
    это прогон ВТОРОГО (список идёт от нового к старому), и задача первого
    тогда судится по чужому прогону (найдено в разборе AI-ревью PR #253).
    Точный критерий — head_sha прогона равен merge_commit_sha этого PR (тот
    же коммит, что и включает push-триггер деплоя). merge_commit_sha не
    всегда доступен (пул не даёт его сразу после мержа) — тогда резерв:
    самый РАННИЙ прогон после merged_at (его диспатчит push сразу же после
    мержа), а не самый новый."""
    payload = gh(f"repos/{repo}/actions/workflows/deploy-worker.yml/runs?per_page=10") or {}
    runs = payload.get("workflow_runs", [])
    if merge_commit_sha:
        candidate = next((r for r in runs if r.get("head_sha") == merge_commit_sha), None)
    else:
        after = [r for r in runs if parse_time(r["created_at"]) >= merged_at]
        candidate = min(after, key=lambda r: parse_time(r["created_at"])) if after else None
    if candidate is None:
        return "pending", "deploy-worker.yml после мержа ещё не запускался"
    if candidate.get("conclusion") is None:
        return "pending", f"deploy-worker.yml ещё выполняется — {candidate['html_url']}"
    if candidate["conclusion"] != "success":
        return "fail", (f"deploy-worker.yml={candidate['conclusion']} "
                         f"(канарейка UI — последний шаг этого джоба) — {candidate['html_url']}")
    if not DSH_EDGE_URL:
        raise RuntimeError("DSH_EDGE_URL не задан — /api/health не проверить")
    # Класс #225 (найдено повторно в разборе AI-ревью PR #253): /api/health
    # публичный (не нужен логин), но всё равно идёт ЧЕРЕЗ МОРДУ Cloudflare —
    # запрос обязан нести явный User-Agent тем же _morde_opener(), которым
    # ходят _morde_login/_morde_rpc, иначе дефолтный `Python-urllib/3.x`
    # получает 403 error code:1010 ДО приложения (доказано живым запросом),
    # и deploy-класс приёмки не закрывается НИКОГДА.
    req = urllib.request.Request(DSH_EDGE_URL.rstrip("/") + "/api/health")
    try:
        opener = _morde_opener()
        with opener.open(req, timeout=DSH_EDGE_HEALTH_TIMEOUT) as resp:
            status = resp.status
    except (urllib.error.URLError, OSError) as error:
        # OSError — не только сетевые ошибки: socket.timeout (= TimeoutError с
        # 3.10) при чтении ответа НЕ оборачивается urllib в URLError и раньше
        # пробивал бы голый except RuntimeError вызывающего accept_merged_tasks
        # (находка AI-ревью PR #253, тот же приём, что уже в archive_runner_sessions).
        raise RuntimeError(f"/api/health недоступен: {error}") from error
    if status != 200:
        return "fail", f"деплой зелёный ({candidate['html_url']}), но /api/health вернул {status}"
    return "ok", f"deploy-worker.yml зелёный ({candidate['html_url']}), /api/health=200"


def script_evidence(repo: str, head_sha: str) -> tuple[str, str]:
    """('ok'|'fail'|'pending', детали) — тот же критерий красного обязательного
    чека, что уже применяет pr_bad_checks/merge_queue (см. bad_check_names).

    «Ещё выполняется» (status != completed на самом свежем чек-ране с этим
    именем, см. latest_check_runs) и «завершился и он красный» — разные
    исходы (#467, AGENTS.md «fail loud, не silent-wrong»): раньше оба читались
    одним и тем же bad_check_names (conclusion=None у незавершённого чека —
    "не success/skipped/neutral", то есть формально "плохой"), и приёмка
    писала «результат не достигнут» про чек, который просто ещё не досчитал.
    Проверяем незавершённость ДО красноты — незавершённый чек не судим по
    conclusion вовсе, откладываем на следующий пульс."""
    payload = gh(f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100") or {}
    runs = payload.get("check_runs", [])
    if not runs:
        return "pending", "проверки PR на head sha ещё не найдены"
    latest = latest_check_runs(runs)
    # default "completed" на отсутствующем поле — только страховка для старых
    # фикстур тестов без status (реальный ответ GitHub несёт его всегда);
    # у настоящего check-run без status считаем, что он уже завершён.
    unfinished = sorted({run["name"] for run in latest if run.get("status", "completed") != "completed"})
    if unfinished:
        return "pending", f"проверки ещё выполняются: {', '.join(unfinished)}"
    bad = bad_check_names(latest)
    if bad:
        return "fail", f"красные проверки: {', '.join(bad)}"
    return "ok", f"{len(latest)} проверок PR зелёные (прогон с выводом)"


def docs_missing(repo: str, files_payload: list[dict]) -> list[str]:
    """Файлы из тела PR, которых нет в main, хотя должны быть — редкий случай
    (переименовали/удалили ПОСЛЕ мержа, а не самим этим PR): единственная
    проверяемая форма критерия «источник правды на месте» для правки, не
    имеющей наблюдаемого результата в рантайме.

    `status=removed` из files API (пул PR удалил файл — например архивация
    openspec/changes/* в specs, добавления и удаления одним PR) — это ЕСТЬ
    результат мержа, а не пропажа улики: отсутствие такого файла в main не
    проверяем вовсе, иначе легальная чистка вечно судилась бы как провал
    (найдено в разборе AI-ревью PR #253). Для `renamed` проверяем новый путь
    (`filename`), не старый (`previous_filename`) — старый закономерно исчез.
    «Файла нет» отличается по точной форме gh «HTTP 404» (тот же приём, что
    is_not_found в scripts/review/ai_review.py) — любой другой отказ (ратлимит,
    сеть, 5xx) не «файла нет», а «возможность сломана»: поднимаем наверх, там
    его ловит общий except в accept_merged_tasks и эскалирует, а не тихо
    засчитывает как провал улики."""
    missing = []
    for entry in files_payload:
        if entry.get("status") == "removed":
            continue
        name = entry["filename"]
        try:
            gh(f"repos/{repo}/contents/{name}?ref=main")
        except RuntimeError as error:
            if "HTTP 404" not in str(error):
                raise
            missing.append(name)
    return missing


REOPEN_REJECTED_MARKER = "[оркестратор: переоткрытие закрытой задачи отклонено]"

# ── Замена PR, чья ветка называет закрытую задачу (#543) ──────────────────────
#
# Класс проблемы (живой замер владельца, 2026-09-06): приёмка закрывает
# задачу, пока PR по ней ещё открыт и дорабатывается. contract_check.py
# резолвит задачу PR ТОЛЬКО по имени ветки (task_ref.resolve_pr_task, #394/
# #398) — правка тела PR для этого вопроса больше не читается вовсе, значит
# больше не помогает пройти контракт. Единственный официальный выход —
# scripts/git/task-branch на новый номер (docs/agents/PROTOCOL.md); эту
# ветку и новый PR заводит исполнитель (см. ниже, почему НЕ этот код).
#
# Отклонённый способ — переименование существующей ветки (GitHub `POST
# .../branches/{branch}/rename`): живой эксперимент в
# openspec/changes/archive/contract-task-from-branch/design.md (#394) прямо
# показал, что для НЕ-default ветки GitHub закрывает открытый PR вместо
# переориентации на новое имя — «перевешивание» в буквальном смысле (тот же
# номер PR продолжает работать под новым именем ветки) технически
# недостижимо. Открыть НОВЫЙ PR с новой ветки автоматически и закрыть
# старый молча — тоже отклонено для ЭТОГО кода: закрытие чужого открытого
# PR фоновым job'ом необратимо (человек мог быть посреди пуша), а сам факт
# «задача закрыта» не отличает докрытие (работа продолжается) от «PR
# устарел и годится на слом» (см. AGENTS.md: «не подменять настоящую
# ошибку»). Значит то, что этот код умеет сделать безопасно и обратимо —
# завести узкую задачу-замену и сделать её видимой на PR; саму смену ветки
# исполнитель делает сам, по инструкции в комментарии.
TASK_REPLACEMENT_MARKER = "<!-- task-replacement:auto -->"


def _existing_task_replacement(repo: str, task_number: int) -> int | None:
    """Уже заведённая замена закрытой задаче `task_number` — ищем по
    конвенции `Related: #<task_number>` в теле issue через Search API
    (отдельный бюджет 30 запросов/мин, не общий core 5000/час — квота
    именно core разряжалась в проде и роняла чужие обязательные проверки,
    #454; эта проверка её не трогает вовсе). Конвенция не изобретена этим
    change — тот же формат уже применяет владелец вручную (живой случай
    #431→#538: тело #538 начинается с «Related: #431 (закрыта приёмкой,
    работа продолжается в PR #439)»), поэтому один и тот же поиск ловит и
    ручные, и автоматические замены — не плодит вторую поверх уже
    существующей человеческой. `None` — ни одной не нашли, можно заводить."""
    query = f'repo:{repo} in:body "Related: #{task_number}"'
    result = gh("-X", "GET", "search/issues", "-f", f"q={query}")
    for item in (result or {}).get("items") or []:
        if "pull_request" in item:
            continue  # ищем задачу-замену, не PR со случайным совпадением текста
        return item["number"]
    return None


def _mark_pr_with_replacement(repo: str, pull: dict, replacement_number: int) -> None:
    """Отмечает тело PR маркером + Related — один и тот же вызов используют
    оба исхода replace_closed_task_prs (свежесозданная замена и уже
    найденная существующая): маркер — единственный способ не спрашивать
    Search API повторно на каждом пульсе про один и тот же PR (экономия
    того же бюджета, что и сам поиск)."""
    body = pull.get("body") or ""
    if TASK_REPLACEMENT_MARKER in body:
        return
    banner = (
        f"{TASK_REPLACEMENT_MARKER}\n"
        f"Related: #{replacement_number} (докрытие — ветка этого PR называет "
        "уже закрытую задачу, см. комментарий ниже)\n\n"
    )
    new_body = banner + body
    gh("-X", "PATCH", f"repos/{repo}/pulls/{pull['number']}", "-f", f"body={new_body}")
    pull["body"] = new_body  # мутируем снимок — тот же приём, что reject_reopened_tasks


def _replacement_pr_comment(task_number: int, replacement_number: int) -> str:
    return (
        f"{TASK_REPLACEMENT_MARKER}\n"
        f"🔁 Задача #{task_number} закрыта, а эта ветка её всё ещё называет — "
        "contract_check.py резолвит задачу PR только по имени ветки (#398) и "
        "тело PR для этого вопроса не читает вовсе, поэтому правка тела не "
        "помогает пройти контракт.\n\n"
        f"Автоматически заведена узкая задача-замена #{replacement_number} "
        f"(Related: #{task_number}).\n\n"
        "Что дальше:\n"
        f"- работа ещё нужна — заведи новую ветку `scripts/git/task-branch "
        f"{replacement_number}-<slug>` тем же диффом (тот же коммит, новое "
        "имя) и открой новый PR; этот можно закрыть как докрытый;\n"
        "- этот PR на самом деле устарел и продолжать не нужно — закрой его; "
        f"в этом случае закрой и задачу-замену #{replacement_number} вручную "
        "как ненужную (в этом репозитории задачи закрываются только по "
        "улике постмерж-приёмки — автоматического закрытия по факту "
        "закрытия PR нет, эта задача сама себя не закроет)."
    )


_LEADING_TASK_REF_RE = re.compile(r"^#(\d+)\b[:\s—-]*")


def _create_task_replacement(repo: str, pull: dict, task_number: int) -> int:
    pr_number = pull["number"]
    branch = ((pull.get("head") or {}).get("ref")) or "?"
    pr_title = (pull.get("title") or f"PR #{pr_number}").strip()
    # PR старой конвенции сам называет закрытую задачу первым словом
    # заголовка («#335: …») — без среза «Докрытие #335: #335: …» дублирует
    # номер (живой случай PR #397 при ретроспективном прогоне #543).
    leading = _LEADING_TASK_REF_RE.match(pr_title)
    if leading and int(leading.group(1)) == task_number:
        pr_title = _LEADING_TASK_REF_RE.sub("", pr_title, count=1).strip() or pr_title
    title = f"Докрытие #{task_number}: {pr_title}"
    body = (
        f"Related: #{task_number} (закрыта, работа продолжается в PR #{pr_number}).\n\n"
        f"Заведено автоматически (#543): ветка PR #{pr_number} (`{branch}`) "
        f"называет закрытую задачу #{task_number}; контракт резолвит задачу PR "
        "только по имени ветки (#398) и правку тела для этого больше не "
        "читает, поэтому контракт продолжит красить этот PR, пока имя ветки "
        "не сменится.\n\n"
        "## Что дальше\n\n"
        "- работа ещё нужна — заведи новую ветку `scripts/git/task-branch "
        "<номер этой задачи>-<slug>` тем же диффом, что уже лежит в "
        f"PR #{pr_number}, и открой новый PR (contract требует имя ветки "
        "agent/<N>-<slug>; переименовывать СУЩЕСТВУЮЩУЮ ветку нельзя — "
        "переименование закрывает открытый PR вместо переориентации, живой "
        "эксперимент в "
        "openspec/changes/archive/contract-task-from-branch/design.md);\n"
        f"- PR #{pr_number} на самом деле устарел — закрой его; тогда закрой "
        "и эту задачу вручную как ненужную (задачи здесь закрываются только "
        "по улике постмерж-приёмки, автозакрытия по факту закрытия PR нет).\n\n"
        "## Оставшаяся работа (тело исходного PR, без изменений)\n\n"
        f"{pull.get('body') or '_тело PR пустое_'}"
    )
    result = pool_issue.create_pool_issue(
        gh, repo, title, body, [TASK_LABEL, AUTO_LABEL])
    return result["number"]


def replace_closed_task_prs(repo: str, pulls: list[dict], *, pool: list[dict]) -> list[str]:
    """Один открытый PR, чья ветка резолвится (task_ref.resolve_pr_task) на
    ЗАКРЫТУЮ задачу — заводит задачу-замену и метит PR (см. блок комментариев
    выше — почему именно так, не переименованием ветки/новым PR). `pool` —
    уже прочитанный снимок открытых задач этого прогона (main()) — второго
    обхода Issues здесь нет, только числа сравниваются.

    Пропускает: PR без agent-ветки (resolve_pr_task вернул None — не наш
    случай); PR, чья задача ЕСТЬ среди открытых (`pool`, никакого сетевого
    вызова); PR, уже помеченный TASK_REPLACEMENT_MARKER (обработан раньше);
    задачу, которая при сетевой проверке оказалась PR'ом или не закрыта на
    самом деле (другая причина непригодности — не эта функция её чинит, см.
    task_eligibility_problems в contract_check.py).

    Сбой сети/API на одном PR не должен уронить обход остальных — попадает
    строкой-предупреждением в отчёт, следующий PR обрабатывается как ни в
    чём не бывало (тот же приём, что у lease_observations в main())."""
    open_numbers = {issue["number"] for issue in pool}
    lines: list[str] = []
    for pull in pulls:
        task_number = task_ref.resolve_pr_task(pull)
        if task_number is None or task_number in open_numbers:
            continue
        pr_number = pull["number"]
        if TASK_REPLACEMENT_MARKER in (pull.get("body") or ""):
            continue
        try:
            existing = _existing_task_replacement(repo, task_number)
        except RuntimeError as error:
            lines.append(f"⚠️ PR #{pr_number}: поиск существующей замены задаче #{task_number} не удался — {error}")
            continue
        if existing is not None:
            try:
                _mark_pr_with_replacement(repo, pull, existing)
            except RuntimeError as error:
                lines.append(f"⚠️ PR #{pr_number}: не смог отметить уже существующую замену #{existing} — {error}")
                continue
            lines.append(
                f"🔁 PR #{pr_number}: задача #{task_number} закрыта, замена #{existing} "
                "уже существует — тело PR отмечено")
            continue
        try:
            issue = gh(f"repos/{repo}/issues/{task_number}")
        except RuntimeError as error:
            lines.append(f"⚠️ PR #{pr_number}: не смог проверить состояние задачи #{task_number} — {error}")
            continue
        if "pull_request" in issue or issue["state"] != "closed":
            continue  # другая причина непригодности — вне ответственности этой функции
        try:
            replacement_number = _create_task_replacement(repo, pull, task_number)
            _mark_pr_with_replacement(repo, pull, replacement_number)
            post_issue_comment(repo, pr_number, _replacement_pr_comment(task_number, replacement_number))
        except RuntimeError as error:
            lines.append(f"⚠️ PR #{pr_number}: замена задаче #{task_number} не заведена — {error}")
            continue
        lines.append(f"🔁 PR #{pr_number}: задача #{task_number} закрыта — заведена замена #{replacement_number}")
    return lines


def reject_reopened_tasks(repo: str, pool: list[dict]) -> list[str]:
    """Закрытая задача не переоткрывается никогда (решение владельца, #369,
    дословно: «если закрыли то всё, создавайте новую»). GitHub не умеет
    отклонить reopen нативно — ни repo-настройки, ни API-поля под это нет
    (Lock conversation ограничивает только новые комментарии участников без
    write-доступа, состояние issue не трогает); носитель правила — эта
    проверка пульса.

    Issues API уже отдаёт `state_reason` в САМОМ списке `open_task_issues`
    (тот же приём, что уже применяет is_epic_issue для sub_issues_summary —
    поле есть в list-ответе, второго запроса на issue не нужно): "reopened"
    однозначно значит «текущее открытое состояние достигнуто событием
    reopened», в отличие от issue, открытого с рождения (там `state_reason`
    — null). Проверено фактом на живых #111/#114/#115/#158 (закрыты и
    переоткрыты человеком 2026-09-05): `state_reason` остаётся "reopened"
    и после последующих assign/unassign — соседние события его не сбрасывают
    (не подтверждено для остальных типов событий — не проверялось).

    Найдя такую issue — закрывает её обратно с комментарием-отказом,
    называющим готовое действие (завести НОВУЮ, более узкую задачу со
    ссылкой на эту как related — не переоткрывать снова), и снимает
    assignee/lock, если реопен успел их вернуть.

    Дедупликации не нужно: как только issue закрыта, `state=open` фильтр
    `open_task_issues` больше её не вернёт — второй раз в `pool` она не
    попадёт (тот же приём, которым ok/docs-ветка accept_merged_tasks сама
    выходит из повторной обработки). Новое переоткрытие после этого —
    новое нарушение и снова отклоняется, а не повтор старого; отдельный
    маркер-комментарий для дедупликации (как у accept_merged_tasks) тут не
    нужен вовсе.

    Вызывается в main() ДО accept_merged_tasks на том же снимке pool: пока
    issue не успела снова смэтчиться со старым merged_pr_map по декларации
    первой строки PR (тот класс, который защищал несостоявшийся PR #366 —
    при этом правиле сценарий, который он защищал, больше не существует).

    WATCHDOG_ISSUE — постоянный канал эскалации (#120), не задача из пула,
    и намеренно исключён (тот же приём, что у accept_merged_tasks)."""
    lines: list[str] = []
    for issue in pool:
        if issue.get("state_reason") != "reopened":
            continue
        number = issue["number"]
        if number == WATCHDOG_ISSUE:
            continue
        text = (
            f"{REOPEN_REJECTED_MARKER} закрытая задача не переоткрывается "
            "никогда (решение владельца). Остаток работы, если он есть, "
            "оформляется НОВОЙ, более узкой задачей со ссылкой на эту "
            f"(#{number}) как related — не переоткрытием этой. Закрываю обратно."
        )
        try:
            post_issue_comment(repo, number, text)
            gh("-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed")
        except RuntimeError as error:
            lines.append(f"⚠️ #{number}: переоткрытие не отклонено — {error}")
            continue
        if issue["assignees"]:
            # По одному -f assignees[]=<login> на ассайни (gh api: массив — это
            # повторный key[], а не запятая внутри одного значения) — при двух
            # и более ассайни склейка через ", " ушла бы одной строкой и GitHub
            # отклонил бы весь запрос как несуществующий логин.
            assignee_args = [
                arg for a in issue["assignees"] for arg in ("-f", f"assignees[]={a['login']}")
            ]
            try:
                gh("-X", "DELETE", f"repos/{repo}/issues/{number}/assignees", *assignee_args)
            except RuntimeError as error:
                lines.append(f"⚠️ #{number}: assignee не снят после отклонённого переоткрытия — {error}")
        try:
            lines.append(f"🔓 {claim_task.release(repo, int(number))}")
        except RuntimeError as error:
            lines.append(f"⚠️ замок task-{number} не снят: {error}")
        lines.append(f"🚫 #{number}: переоткрытие отклонено, задача закрыта обратно")
    return lines


def accept_merged_tasks(
    repo: str, pool: list[dict], merged: dict[int, dict], now: datetime | None = None,
    *, open_pulls_list: list[dict],
) -> tuple[list[str], list[str], bool]:
    """Стадия приёмки (#227) — см. блок комментариев выше. merged — карта
    Task#N → слитый PR (merged_pr_map(all_merged_pulls(repo)), один общий
    обход на весь прогон оркестратора, тот же, что использует reap_stale).
    now — момент прогона (по умолчанию текущее время), нужен только для
    порога ACCEPTANCE_PENDING_HOURS. open_pulls_list — открытые PR того же
    прогона (open_pulls(repo)) — обязателен (keyword-only, без дефолта): молчаливый
    None вернул бы инцидент #320/#325 тихо для любого забывшего вызывающего
    (находка AI-ревью PR #358) — пустой список `[]` explicit допустим и
    означает «других открытых PR не было», но это решение вызывающего, а не
    дефолт по умолчанию.

    Возвращает (наблюдения, действия, был_ли_жёсткий_сбой) — #456: «улика ещё
    не готова» и «приёмка отложена другим открытым PR» ничего не меняют
    (ничего не запощено, ничего не закрыто) — раньше эти строки попадали в
    тот же список, что реальные закрытия/провалы/эскалации.

    Живой случай (проверки на входе вместо гвардий постфактум, задача о
    приёмке при открытом втором PR): приёмка закрыла #320, пока по нему был
    открыт второй PR #325 — тот немедленно упал на contract («задача #320
    закрыта»), хотя работа по нему ещё шла. Улика (`state == "ok"/"docs"`)
    относится к ОДНОМУ слитому PR — этого недостаточно, если задачу СЕЙЧАС
    же объявляет ещё один открытый PR: закрытие оборвало бы его работу.
    Проверка ниже — до PATCH state=closed, не после."""
    now = now or datetime.now(timezone.utc)
    observations: list[str] = []
    actions: list[str] = []
    hard_failure = False
    # Заметки-итоги в сессии раннера (#480): собираются за весь обход пула,
    # один логин в морду на всю функцию — не на каждую задачу (см.
    # append_session_notes).
    session_notes: list[tuple[int, str]] = []
    for issue in pool:
        number = issue["number"]
        if number == WATCHDOG_ISSUE:
            # #120 — постоянный канал эскалации pulse_guard (heartbeat/pause
            # маркеры), не разовая задача: PR #126, реализовавший предохранитель,
            # объявляет #120 первой строкой и давно слит с зелёными проверками —
            # merged_pr_map найдёт его для ЛЮБОГО прогона, а #120 намеренно
            # остаётся открытым навсегда. Закрыть его приёмкой — не «не та
            # задача провалилась», а тихая порча канала эскалации молчаливым
            # побочным эффектом, обнаружено при разборе AI-ревью PR #253.
            continue
        if is_epic_issue(issue):
            # Эпик — не закрывается приёмкой автоматом (см. is_epic_issue),
            # запрет сохраняется всегда. Но у WATCHDOG_ISSUE открытость —
            # функция (постоянный канал эскалации), а у эпика открытость
            # значит «работа не завершена» — когда все нативные sub-issues
            # уже закрыты (`sub_issues_summary.total == completed`, оба > 0),
            # молчание навсегда означало бы, что завершённый эпик никто
            # никогда не увидит и не закроет вручную (находка AI-ревью
            # PR #342: «тормоз без газа»). Разовый идемпотентный маркер —
            # не автозакрытие, только сигнал человеку.
            summary = issue.get("sub_issues_summary") or {}
            total = summary.get("total") or 0
            completed = summary.get("completed") or 0
            if total > 0 and total == completed:
                try:
                    if not issue_marker_times(repo, number, ACCEPTANCE_EPIC_MARKER):
                        post_issue_comment(
                            repo, number,
                            f"{ACCEPTANCE_EPIC_MARKER} все {total} дочерних sub-issues "
                            f"закрыты — эпик выглядит завершённым и требует ручного "
                            f"закрытия (приёмка эпики автоматом не закрывает).")
                        actions.append(
                            f"📋 #{number}: {ACCEPTANCE_EPIC_MARKER} все {total} "
                            f"дочерних sub-issues закрыты — требует ручного закрытия")
                except RuntimeError as error:
                    actions.append(f"⚠️ #{number}: маркер завершённого эпика не поставлен — {error}")
            continue
        pull = merged.get(number)
        if pull is None:
            continue

        disclaimer = partial_disclaimer(pull.get("body") or "", number)
        if disclaimer is not None:
            partial_marker = f"{ACCEPTANCE_PARTIAL_MARKER} PR #{pull['number']}"
            try:
                already_marked = bool(issue_marker_times(repo, number, partial_marker))
            except RuntimeError as error:
                actions.append(f"⚠️ #{number}: комментарии не прочитаны, приёмка отложена: {error}")
                continue
            # Дедуп по факту завершения расчистки, не по одному лишь маркеру
            # (находка AI-ревью PR #342, класс воспроизведён в этой же ветке
            # предыдущей правкой): комментарий мог встать, а DELETE assignees/
            # claim_task.release ниже — упасть отдельным HTTP-сбоем. Пока
            # исполнитель ещё висит на задаче, «маркер уже стоит» не значит
            # «уже обработано» — уходим молча только когда пул реально принял
            # задачу обратно.
            if already_marked and not issue["assignees"]:
                continue  # и маркер есть, и assignee уже снят прошлым пульсом
            if not already_marked:
                text = (f"{partial_marker} тело PR #{pull['number']} содержит дисклеймер "
                        f"о неполноте («{disclaimer}») — критерий требует проверки человеком, "
                        f"не закрываю. Задача возвращена в пул, докрытие — новый PR.")
                try:
                    post_issue_comment(repo, number, text)
                except RuntimeError as error:
                    actions.append(f"⚠️ #{number}: отметка «требует проверки человеком» не завершена — {error}")
                    continue
                actions.append(f"⚠️ #{number}: {text}")
            if issue["assignees"]:
                who = ", ".join(a["login"] for a in issue["assignees"])
                try:
                    gh("-X", "DELETE", f"repos/{repo}/issues/{number}/assignees", "-f", f"assignees[]={who}")
                except RuntimeError as error:
                    actions.append(f"⚠️ #{number}: assignee не снят (маркер уже стоит) — {error}")
                    continue
            try:
                actions.append(f"🔓 {claim_task.release(repo, int(number))}")
            except RuntimeError as error:
                # Та же гарантия, что и у fail/ok-веток ниже: сбой снятия замка
                # не должен ронять обход остальных задач пула (найдено при
                # разборе AI-ревью PR #342 — было «висит вечно», без освобождения
                # ни assignee, ни замка не снимался).
                actions.append(f"⚠️ замок task-{number} не снят: {error}")
            continue

        fail_marker = f"{ACCEPTANCE_FAIL_MARKER} PR #{pull['number']}"
        try:
            already_failed = bool(issue_marker_times(repo, number, fail_marker))
        except RuntimeError as error:
            actions.append(f"⚠️ #{number}: комментарии не прочитаны, приёмка отложена: {error}")
            continue
        if already_failed:
            if not issue["assignees"]:
                continue  # эта пара (задача, PR) уже провалила приёмку и уже в пуле
            # Тот же класс, что и в ветке дисклеймера выше (находка AI-ревью
            # PR #342): fail_marker стоит, но снятие assignee тогда не
            # завершилось — не спамим комментарий провала повторно, но обязаны
            # довести расчистку (assignee + замок) до конца.
            who = ", ".join(a["login"] for a in issue["assignees"])
            try:
                gh("-X", "DELETE", f"repos/{repo}/issues/{number}/assignees", "-f", f"assignees[]={who}")
            except RuntimeError as error:
                actions.append(f"⚠️ #{number}: assignee не снят (маркер уже стоит) — {error}")
                continue
            try:
                actions.append(f"🔓 {claim_task.release(repo, int(number))}")
            except RuntimeError as error:
                actions.append(f"⚠️ замок task-{number} не снят: {error}")
            continue

        category = None
        merged_at = parse_time(pull["merged_at"])
        try:
            # Пагинация (#294/#253, четвёртое место того же класса: after_merge
            # выше уже переведён на review_labels.list_pr_files) — PR за сотню
            # файлов, где cf-worker/* стоят за сотой позицией, классифицировал
            # бы приёмку как "script" вместо "deploy", разойдясь с after_merge.
            files_payload = review_labels.list_pr_files(repo, pull["number"], gh)
            filenames = [f["filename"] for f in files_payload]
            category = classify_acceptance(filenames)
            if category == ACCEPT_DOCS:
                missing = docs_missing(repo, files_payload)
                if missing:
                    state, detail = "fail", f"файлы отсутствуют в main: {', '.join(missing)}"
                else:
                    # removed-файлы (архивация) не проверяются физически —
                    # их отсутствие в main и есть результат мержа; в улику
                    # попадают только добавленные/изменённые/переименованные.
                    checked = [f["filename"] for f in files_payload if f.get("status") != "removed"]
                    if checked:
                        state, detail = "docs", f"файлы на месте в main: {', '.join(checked)}"
                    else:
                        state, detail = "docs", "правка — только удаления (архивация), физической проверки нет"
            elif category == ACCEPT_DEPLOY:
                state, detail = deploy_evidence(repo, merged_at, pull.get("merge_commit_sha"))
            else:
                state, detail = script_evidence(repo, pull["head"]["sha"])
        except RuntimeError as error:
            # Идемпотентность (находка AI-ревью PR #253): ветки fail/pending
            # уже дедуплицируют эскалацию маркером — эта ветка эскалировала
            # на КАЖДОМ пульсе, пока возможность не восстановится (временный
            # HTTP 502/лежащая морда деплой-класса → Telegram каждые 15 мин
            # в тот самый канал, где маркеры заведены ради «один сигнал на
            # серию»). Сама проверка улики повторяется каждый пульс (в
            # отличие от fail — сбой может исчезнуть сам), эскалация — нет.
            # Маркер живёт в WATCHDOG_ISSUE вместе с самой эскалацией (не в
            # задаче #number — жёсткий сбой её не трогает, см. соседний тест
            # test_accept_merged_tasks_escalates_hard_failure_without_touching_task).
            error_marker = f"{ACCEPTANCE_ERROR_MARKER} #{number} PR #{pull['number']}"
            text = (f"🚨 #{number}: приёмка PR #{pull['number']} "
                    f"({category or '?'}) не смогла проверить улику — возможность сломана: {error}")
            try:
                already_escalated = issue_marker_times(repo, WATCHDOG_ISSUE, error_marker)
            except RuntimeError:
                already_escalated = []  # маркер не прочитан — эскалируем громко, не молчим
            if already_escalated:
                actions.append(f"{text} (уже эскалировано, повторный Telegram не шлём)")
            else:
                escalation = escalate(repo, WATCHDOG_ISSUE, f"{error_marker} {text}")
                actions.append(f"{text} ({escalation})")
            hard_failure = True
            continue

        if state == "pending":
            # Единственный путь назад для merged-задач теперь эта функция
            # (reap_stale их больше не трогает) — улика, которая не
            # появляется, раньше самовосстанавливалась через STALE_HOURS
            # reap, а без этого порога зависла бы в pending навсегда молча.
            if now - merged_at > timedelta(hours=ACCEPTANCE_PENDING_HOURS):
                pending_marker = f"{ACCEPTANCE_PENDING_MARKER} PR #{pull['number']}"
                try:
                    already_escalated = issue_marker_times(repo, number, pending_marker)
                except RuntimeError as error:
                    actions.append(f"⚠️ #{number}: маркер зависшей приёмки не прочитан: {error}")
                    continue
                if already_escalated:
                    observations.append(
                        f"⏳ #{number}: улика ({category}) не готова дольше "
                        f"{ACCEPTANCE_PENDING_HOURS} ч — уже эскалировано, жду новую работу")
                    continue
                text = (f"🚨 #{number}: приёмка PR #{pull['number']} ({category}) висит в "
                        f"pending дольше {ACCEPTANCE_PENDING_HOURS} ч после мержа — {detail}")
                escalation = escalate(repo, WATCHDOG_ISSUE, text)
                post_issue_comment(repo, number, f"{pending_marker} {detail} ({escalation}).")
                actions.append(text)
                hard_failure = True
                continue
            observations.append(f"⏳ #{number}: улика ({category}) ещё не готова — {detail}")
            continue

        if state in ("ok", "docs"):
            # Проверка на входе (см. докстринг функции, живой случай #320/#325):
            # задачу может ПРЯМО СЕЙЧАС объявлять ещё один открытый PR — улика
            # про уже слитый не отменяет его работу. task_ref.resolve_pr_task —
            # то же единственное место правды, что и у остальных решений
            # «какая задача у этого PR» (#259), не самодельное сравнение строк.
            other_open = [
                p for p in (open_pulls_list or [])
                if p.get("number") != pull["number"]
                and task_ref.resolve_pr_task(p) == number
            ]
            if other_open:
                others = ", ".join(f"#{p['number']}" for p in other_open)
                observations.append(
                    f"⏳ #{number}: приёмка отложена — задачу ещё объявляет "
                    f"открытый PR {others}, закрывать по PR #{pull['number']} рано ({category})")
                continue
            marker = ACCEPTANCE_DOCS_MARKER if state == "docs" else ACCEPTANCE_OK_MARKER
            reasoning = ("документационная правка, наблюдаемого результата по природе нет — "
                         "закрываю с этим обоснованием"
                         if state == "docs" else "улика получена — закрываю задачу")
            try:
                post_issue_comment(
                    repo, number,
                    f"✅ {marker} PR #{pull['number']} ({category}): {reasoning}. {detail}.")
                gh("-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed")
            except RuntimeError as error:
                # Сетевой/API сбой на комментарии или PATCH не должен ронять
                # обход остальных задач — та же гарантия, что уже даёт этот
                # приём чтению маркеров и claim_task.release ниже (найдено в
                # разборе AI-ревью PR #253: докстринг функции обещал это для
                # ВСЕХ пунктов пульса, а тут обещание не выполнялось).
                actions.append(f"⚠️ #{number}: закрытие приёмкой не завершено — {error}")
                continue
            try:
                actions.append(f"🔓 {claim_task.release(repo, int(number))}")
            except RuntimeError as error:
                # claim_task.release сам возвращает строку (не исключение) для
                # «замка не было» (см. _ref_missing) — RuntimeError сюда долетает
                # только на настоящей поломке (сеть/права/5xx), и её нельзя
                # глотать молча (тот же приём, что уже используют after_merge/
                # unhealthy_pulls).
                actions.append(f"⚠️ замок task-{number} не снят: {error}")
            actions.append(f"✅ #{number}: закрыта приёмкой ({category}) — {detail}")
            session_notes.append((number, f"✅ Задача #{number} закрыта приёмкой ({category}): {detail}."))
            continue

        # state == "fail"
        try:
            post_issue_comment(
                repo, number,
                f"♻️ {fail_marker} — улика ({category}) показала, что результат не достигнут: "
                f"{detail}. Задача НЕ закрыта: нужна доработка (новый PR или правка "
                f"существующей работы), не тихое закрытие.")
            if issue["assignees"]:
                who = ", ".join(a["login"] for a in issue["assignees"])
                gh("-X", "DELETE", f"repos/{repo}/issues/{number}/assignees", "-f", f"assignees[]={who}")
        except RuntimeError as error:
            actions.append(f"⚠️ #{number}: отметка провала приёмки не завершена — {error}")
            continue
        try:
            actions.append(f"🔓 {claim_task.release(repo, int(number))}")
        except RuntimeError as error:
            actions.append(f"⚠️ замок task-{number} не снят: {error}")
        actions.append(f"♻️ #{number}: не закрыта, улика ({category}) провалена — {detail}")
        session_notes.append((number, f"♻️ Задача #{number} не закрыта приёмкой ({category}) — {detail}."))
    note_lines, note_hard_failure = append_session_notes(session_notes)
    actions += note_lines
    hard_failure = hard_failure or note_hard_failure
    return observations, actions, hard_failure


def render_action_report(observations: list[str], actions: list[str]) -> list[str]:
    """Собирает секции отчёта из уже классифицированных источников (#456):
    «наблюдения» (замок ещё жив, PR в очереди, дозволен диспатч без изменений
    и т.п.) — факты без изменения состояния, показываются отдельно и всегда,
    если есть. «### Действия»/«Действий не требуется» решается ПО СПИСКУ
    ДЕЙСТВИЙ, не по факту наличия любых строк вообще — до #456 главный
    источник лжи был именно здесь: conveyor_gate в здоровом состоянии
    (диспатч разрешён без изменений — обычное дело) и collect_stale
    («замок жив» — почти всегда есть хоть одна активная аренда) сами по себе
    делали список «есть что показать» непустым на КАЖДОМ прогоне, даже когда
    планировщик не изменил ни одного бита состояния."""
    result: list[str] = []
    if observations:
        result += ["", "### Наблюдения (без изменения состояния)", *observations]
    if actions:
        result += ["", "### Действия", *actions]
    else:
        result += ["", "Действий не требуется."]
    return result


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines = [f"## Отчёт оркестратора {now.isoformat(timespec='seconds')}", ""]
    # Прод-запись только внутри GitHub Actions (2026-09-11) — режим
    # объявляется ГРОМКО и ПЕРВЫМ, до единого изменяющего вызова (тормоз не
    # молчит о своём режиме, AGENTS.md). См. pulse_guard.prod_writes_allowed
    # и блок-комментарий там же — живое доказательство: watchdog-issue #120
    # несёт 112 из 573 комментариев от логина mytab0r, не github-actions[bot].
    # Печатается в лог прогона (stdout), НЕ в `lines` отчёта: `lines` читает
    # stall_detector.detect_and_act как источник симптомов простоя (⚠️/🚨-
    # строки), и ежепрогонная строка режима записи (⚠️ вне CI в юнит-тестах,
    # где ALLOW_PROD_WRITES_ENV поднят фикстурой) ложно матчила бы её как
    # новый, ничем не примечательный симптом на каждом прогоне — режим записи
    # не про здоровье конвейера, отдельная забота.
    print(announce_write_mode())
    # Слот update_branch раньше обнулялся один раз здесь на весь прогон
    # (#252, третий заход). С #297 прогон — это цикл из нескольких проходов
    # (merge_loop), и каждый проход обнуляет свой собственный слот сам — см.
    # merge_loop и reset_update_branch_budget внутри него.

    # «Кто следит за следящим» (#120): первой проверкой, пока этот запуск жив,
    # кричим о пропавших пульсах — остальная работа может не иметь смысла,
    # если конвейер стоял.
    lines += heartbeat_check(repo, now)

    # Сторож независимого DO-пульса (#689): heartbeat_check выше видит «жив»,
    # если сработал ЛЮБОЙ канал workflow_dispatch, а событийный (wake_orchestra.sh)
    # и независимый (cf-worker alarm(), GH_DISPATCH_TOKEN) дают один и тот же
    # event — маскировка возможна ВНУТРИ heartbeat_check, не только между
    # schedule/workflow_dispatch. Рядом с heartbeat_check — тот же «до всей
    # остальной работы» довод.
    lines += independent_pulse_check(repo, now)

    # Гвардия непрочитанных провалов ключевых workflow (#477): рядом с
    # heartbeat_check — тот же «до всей остальной работы» довод, дешёвый
    # запрос (status=completed, малый per_page), не блокирует остальной пульс.
    failure_watch_observations, failure_watch_actions = failure_watch(repo, now)

    # Дрейф пина апстрима (#134): релиз новее пина source-build морды кричит
    # в задачу #134 + метка + Telegram, один раз на релиз.
    lines += upstream_drift_lines(repo)

    pulls = open_pulls(repo)
    lines.append(f"Открытых PR: {len(pulls)}")

    # Одна карта Task#N → слитый PR на весь прогон (#227) — reap_stale
    # (не путать слитый-но-непринятый PR с «PR не появился») и accept_merged_tasks
    # ниже читают её из одного источника, без второго обхода закрытых PR.
    merged = merged_pr_map(all_merged_pulls(repo))

    # Один снимок открытых задач на большую часть прогона (#443): раньше
    # reap_stale и unhealthy_pulls каждая сама опрашивала open_task_issues(repo)
    # (2 страницы на ~125 задач) в отдельный момент — между их вызовами ничего,
    # что меняет состав/assignees открытых задач, не происходит (heartbeat/
    # дрейф пина/open_pulls/merged_pr_map/collect_stale/mark_conflicts их не
    # трогают). Обе функции мутируют переданные issue-объекты СРАЗУ вслед за
    # своими же серверными изменениями (см. их докстринги) — reject_reopened_tasks
    # и accept_merged_tasks ниже видят актуальное состояние без второго обхода.
    pool = open_task_issues(repo)

    # Замена PR, чья ветка называет закрытую задачу (#543) — на том же
    # снимке pulls/pool, без дополнительного обхода Issues (кроме точечной
    # сетевой проверки состояния конкретной закрытой задачи и Search API,
    # см. докстринг replace_closed_task_prs). Заведённые замены пополняют
    # пул свободных задач — пересчёт нужен, чтобы wip_gate/dispatch_worker
    # ниже увидели их этим же прогоном, а не следующим.
    replacement_lines = replace_closed_task_prs(repo, pulls, pool=pool)
    if replacement_lines:
        pool = open_task_issues(repo)  # пересчёт: заведённые замены — новые свободные задачи

    # Наблюдения и действия разведены по #456 — см. render_action_report:
    # функции ниже возвращают их отдельно там, где смешивали раньше; там, где
    # функция и раньше была чистым источником действий (reap_stale/
    # mark_conflicts/unhealthy_pulls/stale_ready_pulls/reject_reopened_tasks —
    # строка появляется, только если состояние реально изменилось), список
    # идёт прямо в actions без переклассификации.
    stale_lines = reap_stale(repo, now, pulls, merged, pool=pool)
    # Зависший (но по GH ещё in_progress) прогон worker.yml (#815) — рядом с
    # reap_stale/collect_stale: та же роль («аренда, которую пора вернуть в
    # пул раньше обычного таймера»), только признак другой (длительность
    # in_progress, не возраст назначения/замка). worker_runs_active сам уже
    # не блокирует диспатч на зависший прогон независимо от исхода отмены
    # здесь — этот шаг только доводит дело до конца (отмена + освобождение
    # задачи), best-effort.
    stalled_observations, stalled_actions = reap_stalled_worker_run(repo, now, pool, pulls)
    try:
        lease_observations, lease_actions = claim_task.collect_stale(repo, now)
    except RuntimeError as error:
        # сборщик замков не должен блокировать слияния, но и не молчит (#124-класс)
        lease_observations, lease_actions = [], [f"⚠️ обход замков задач не удался: {error}"]
    # collect_stale трогает только замки задач/комментарии, не PR (#443) —
    # повторное чтение open_pulls(repo) здесь было чистой тратой: состояние
    # PR не могло измениться со времени снимка выше.
    conflict_lines = mark_conflicts(repo, pulls)
    # #196, поведение 2: нездоровый PR возвращает задачу в пул ДО очереди
    # слияния — освобождённая задача должна попасть в тот же отчёт, а
    # merge_queue ниже не зависит от пула задач.
    unhealthy_lines = unhealthy_pulls(repo, now, pulls, pool=pool)
    merge_observations, merge_actions, archive_hard_failure, pulls = merge_loop(repo, pulls)
    # #196, поведение 1: PR с review:ok без вердикта AI (или ai:failed)
    # дольше порога — оркестратор сам запускает ai-review.yml. merge_loop уже
    # вернул актуальный список открытых PR (#443): если он что-то слил или
    # подтянул за свой цикл, снимок обновлён ВНУТРИ самой функции — второй
    # HTTP-вызов open_pulls(repo) здесь не нужен, closed-номера уже отфильтрованы.
    ai_observations, ai_actions = trigger_ai_review(repo, now, pulls)
    # Разблокировка уже стоящих ai:ok+review:large без large-ok (#939) — тот
    # же снимок pulls, независимый триггер (не бюджет автоповторов #196:
    # у этих PR вердикт УЖЕ есть, обычный trigger_ai_review их не видит вовсе).
    stuck_large_ok_observations, stuck_large_ok_actions = stuck_large_ok_pulls(repo, pulls)
    # Инвариант issue #269: готовый PR, который так и не слился, кричит — тот же
    # снимок, что уже обслужил trigger_ai_review выше (#443: раньше здесь был
    # ЕЩЁ один открытый open_pulls(repo), хотя между двумя вызовами ничто не
    # меняет состав открытых PR — ни trigger_ai_review, ни dispatch ai-review.yml
    # не мержат и не закрывают PR).
    stale_ready_lines = stale_ready_pulls(repo, now, pulls)

    # Приёмка (#227): задачи, чей PR уже слит, разбираются по улике ДО подсчёта
    # пула — свободно/в работе должно отражать уже закрытые этим же прогоном.
    # Запрет переоткрытия (#369) — ДО accept_merged_tasks: переоткрытая задача
    # закрывается обратно раньше, чем успеет снова смэтчиться со старым
    # merged_pr_map по декларации первой строки PR. `pool` — тот же снимок,
    # что уже видели reap_stale/unhealthy_pulls (их мутации отражены в нём).
    reopen_lines = reject_reopened_tasks(repo, pool)
    if reopen_lines:
        pool = open_task_issues(repo)  # пересчёт: отклонённое переоткрытие закрыло задачи
    # Проверка на входе, не гвардия постфактум (см. докстринг accept_merged_tasks,
    # инцидент #320/#325): свежий снимок открытых PR НАМЕРЕННО не переиспользует
    # pulls выше — PR, который стал причиной этой приёмки, мог открыться прямо
    # перед этой строкой, и только явный поздний запрос страхует от гонки.
    accept_observations, accept_actions, accept_hard_failure = accept_merged_tasks(
        repo, pool, merged, now, open_pulls_list=open_pulls(repo))
    if accept_actions:
        pool = open_task_issues(repo)  # пересчёт: приёмка могла закрыть задачи
    free = sum(1 for issue in pool if not issue["assignees"])
    taken = len(pool) - free
    lines += ["", f"Пул задач: {free} свободно, {taken} в работе"]

    # Видимость непринятых задач (#427) — метка, не тормоз; использует уже
    # прочитанный `pool` этого прогона, второго обхода Issues не заводит.
    stale_unclaimed_lines = mark_stale_unclaimed(repo, now, pool)

    # Предохранитель (#120) решает, разрешён ли диспатч воркера в этом пульсе.
    conveyor_observations, conveyor_actions, dispatch_allowed = conveyor_gate(repo, now)
    # Расшивка конфликтов (#474) — за тем же предохранителем: конвейер уже
    # нездоров (диспатч закрыт) — не добавляем новых прогонов воркеру и по
    # этому классу тоже, класс тот же («сломан сам worker.yml»), не другой.
    # `pulls` — тот же снимок, что merge_loop уже довёл до актуального
    # состояния этим прогоном (#443/#456: второго обхода нет). WIP-лимит
    # (#464) расшивку НЕ трогает: адресный прогон на конкретный
    # сконфликтовавший PR — доводка существующего PR, а не новая задача.
    if dispatch_allowed:
        conflict_rework_observations, conflict_rework_actions, conflict_rework_dispatched = (
            dispatch_conflict_rework(repo, pulls, pool=pool)
        )
    else:
        conflict_rework_observations, conflict_rework_actions, conflict_rework_dispatched = [], [], False
    # Доводка по находкам AI-ревью (дефект B) — тем же порядком, что конфликты:
    # доводка уже открытых PR обязана иметь приоритет перед взятием НОВЫХ задач
    # (см. блок-комментарий у dispatch_ai_review_rework — это прямо связано с
    # дефектом A: WIP-гейт душит новые задачи именно потому, что открытых PR,
    # ждущих доработки, много). `not conflict_rework_dispatched` — та же
    # дисциплина «ровно один workflow_dispatch воркера за пульс», что уже
    # применяется ниже к dispatch_worker.
    if dispatch_allowed and not conflict_rework_dispatched:
        ai_rework_observations, ai_rework_actions, ai_rework_dispatched = (
            dispatch_ai_review_rework(repo, pulls, pool=pool)
        )
    else:
        ai_rework_observations, ai_rework_actions, ai_rework_dispatched = [], [], False
    # WIP-лимит (#464) — второй, независимый тормоз, но только на НОВЫЕ
    # задачи: пока открытых PR, реально ждущих доработки, больше или равно
    # WIP_LIMIT (см. wip_gate), dispatch_worker не берёт задачу без открытого
    # PR, но доводку уже открытых по-прежнему диспетчирует (находка ревью
    # PR #466 — см. докстринг dispatch_worker). `pulls` и `pool` — те же
    # снимки, что merge_loop и пул уже довели до актуального состояния этим
    # прогоном (#443/#456: второго обхода нет). conveyor_gate (#120) —
    # единственный тормоз, закрывающий dispatch_worker целиком: тот сигналит
    # о сломанном самом конвейере, а не о размере очереди доработки.
    wip_observations, wip_actions, wip_allowed = wip_gate(
        repo, now, pulls, pool, dispatch_allowed=dispatch_allowed)
    # dispatch_worker пропускается этим проходом, если расшивка конфликта уже
    # ушла: «ровно один workflow_dispatch воркера за пульс» (докстринг модуля,
    # п.4) не должен превратиться в два только из-за гонки worker_runs_active
    # (только что созданный прогон не обязан быть виден как queued немедленно).
    if dispatch_allowed and not conflict_rework_dispatched and not ai_rework_dispatched:
        worker_observations, worker_actions = dispatch_worker(
            repo, pool, wip_allowed=wip_allowed, pulls=pulls)
    else:
        worker_observations, worker_actions = [], []

    observations = (
        lease_observations + merge_observations + ai_observations + stuck_large_ok_observations
        + accept_observations + conveyor_observations + conflict_rework_observations
        + ai_rework_observations + wip_observations + worker_observations
        + failure_watch_observations + stalled_observations
    )
    actions = (
        stale_lines + replacement_lines + lease_actions + conflict_lines + unhealthy_lines
        + merge_actions + ai_actions + stuck_large_ok_actions + stale_ready_lines + reopen_lines
        + accept_actions
        + stale_unclaimed_lines + conveyor_actions + conflict_rework_actions
        + ai_rework_actions + wip_actions + worker_actions + failure_watch_actions
        + stalled_actions
    )
    lines += render_action_report(observations, actions)

    # Приёмка (#227): жёсткий сбой уже эскалирован по каждой затронутой задаче
    # внутри accept_merged_tasks — здесь только красим прогон, второй сигнал
    # не заводим (тот же принцип, что у archive_hard_failure ниже).
    if accept_hard_failure:
        lines.append("🚨 приёмка: минимум одна улика не проверена из-за поломки — прогон окрашен красным")

    # Детектор устойчивого простоя (#201): читает уже собранный отчёт этого
    # пульса (строки выше) и заводит задачу пула по НЕЗНАКОМОМУ отпечатку
    # причины простоя, держащемуся дольше порога устойчивости — дедупликация,
    # суточный потолок и эскалация владельцу живут в stall_detector.py.
    run_url = None
    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        run_url = f"{server_url}/{repo}/actions/runs/{run_id}"
    # Сбой детектора (сеть/gh, авторизация) не должен утащить с собой уже
    # посчитанные строки выше (мержи, диспатч, дрейф пина) — тот же приём,
    # что у archive_hard_failure ниже: отчёт сохраняется, прогон красится
    # ПОСЛЕ summary(lines), не вместо него (находка ревью PR #248: без этого
    # RuntimeError внутри детектора терял отчёт целиком до записи в
    # GITHUB_STEP_SUMMARY). groom_auto_tasks (#830) — ПОСЛЕ detect_and_act
    # (задача, только что заведённая этим же пульсом, не должна тут же
    # считаться «отпечаток не воспроизводится») и ДО escalate_stale_auto_tasks
    # (задача, закрытая грумом на этом пульсе, никогда не доживает до
    # эскалации владельцу) — детерминированный груминг пула (закрытие
    # неактуальных/дублирующих стоп-задач), см. docstring stall_detector.py.
    stall_hard_failure = False
    try:
        stall_lines = detect_and_act(repo, now, lines, run_url)
        stall_lines += groom_auto_tasks(repo, now, lines)
        stall_lines += escalate_stale_auto_tasks(repo, now)
    except RuntimeError as error:
        stall_lines = [f"🚨 детектор простоя (#201) не отработал (возможность сломана, не отсутствует): {error}"]
        stall_hard_failure = True
    if stall_lines:
        lines += ["", "### Детектор простоя (#201)", *stall_lines]

    # Архив сессий раннера после мержа (#119) сломан «возможность есть, но не
    # работает» (#174) — мерж уже состоялся, откатывать нельзя и остальную
    # очередь эта поломка не блокирует. Но fail loud: прогон обязан покраситься
    # ПОСЛЕ того, как отчёт уже сохранён, и эскалация уходит тем же каналом,
    # что предохранитель конвейера (#120), — не заводим третий канал сигнала.
    if archive_hard_failure or stall_hard_failure:
        broken = []
        if archive_hard_failure:
            broken.append(
                "После мержа PR архивация сессии раннера в морде dsh-edge не удалась "
                "(возможность есть, но сломана — см. отчёт этого прогона orchestra выше). "
                "Мерж не откатывается; сессия останется в списке активных до ручного "
                "разбора или следующего успешного мержа той же задачи."
            )
        if stall_hard_failure:
            broken.append(
                "Детектор устойчивого простоя (#201) не отработал этот пульс "
                "(см. отчёт выше) — заведение автозадачи по свежему отпечатку могло "
                "не случиться, а известные автозадачи не получили новую улику."
            )
        escalation = escalate(
            repo, WATCHDOG_ISSUE,
            "🚨 edge-harness: [статус: " + (
                "архив сессии раннера сломан" if archive_hard_failure and not stall_hard_failure else
                "детектор простоя сломан" if stall_hard_failure and not archive_hard_failure else
                "архив сессии раннера и детектор простоя сломаны"
            ) + "]\n" + "\n".join(broken),
        )
        lines.append(f"🚨 прогон окрашен красным ({escalation})")
        summary(lines)
        return 1

    summary(lines)
    return 1 if accept_hard_failure else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::orchestra: {error}")
        sys.exit(1)
