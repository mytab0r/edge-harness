#!/usr/bin/env python3
"""Планировщик оркестратора: следит за пулом задач и сливает проверенные PR.

Вызывается workflow'ом orchestra по расписанию (каждые 15 минут) и вручную.
Workflow держит concurrency-группу `orchestra`: два запуска планировщика
никогда не идут параллельно — это архитектурная сериализация слияний.

Обязанности:
  1. Просроченные назначения: задачу назначили, PR так и не появился за STALE_HOURS —
     назначение снимается, задача возвращается в пул (агент мог умереть посреди работы).
  2. Конфликты: открытые PR, которые больше не сливаются в main без ручного
     разрешения, получают метку `conflict` и комментарий со списком соперников.
  3. Очередь слияний: PR с зелёными проверками и чистым контрактом сливается.
     За один запуск — ровно один PR: после каждого слияния очередь пересчитывается
     следующим запуском, так конфликтующие слияния никогда не проходят одновременно.
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
          (счётчик — маркер в комментариях PR, переживает перезапуск).
       b. Красный обязательный чек или ai:changes-requested дольше порога —
          назначение снимается, задача возвращается в пул, PR не закрывается.
       c. После слияния — gh pr update-branch для остальных открытых PR,
          но выборочно (#252, review_labels.should_update_branch): только
          близкие к слиянию (оба вердикта зелёные) или уже в конфликте —
          подтягивание синхронизирует pr-review.yml/ai-review.yml и снимает
          валидный ai:*-вердикт без пользы для PR, которому рано сливаться.
          Конфликт (DIRTY) не молчит: строка в отчёте + метка conflict.
          И даже среди подходящих — не все разом (#252, третий заход):
          максимум один УСПЕШНО подтянутый кандидат за ПРОГОН — слот общий
          и живёт в update_branch (не по одному на каждую точку вызова),
          поэтому та же дисциплина держит и behind-ветку merge_queue ниже.
          Остальные получают строку и ждут следующего прогона — иначе
          подтягивание первого кандидата может сбросить ai:ok второго тем же
          циклом.
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
"""

import http.cookiejar
import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# gh()/parse_time() и логика предохранителя — одно место правды в pulse_guard
# (пороги WORKER_FAILURE_PAUSE_AFTER / HEARTBEAT_MAX_AGE_MINUTES живут там же;
# escalate — общий канал «поломка → задача-статус + Telegram», #120/#174;
# пороги петли открытого PR #196 — там же: AI_REVIEW_RETRY_AFTER_MINUTES,
# AI_REVIEW_MAX_ATTEMPTS, AI_REVIEW_RETRY_MARKER, UNHEALTHY_PR_AFTER_MINUTES).
from pulse_guard import (
    AI_REVIEW_MAX_ATTEMPTS,
    AI_REVIEW_RETRY_AFTER_MINUTES,
    AI_REVIEW_RETRY_MARKER,
    READY_STALL_MARKER,
    UNHEALTHY_PR_AFTER_MINUTES,
    WATCHDOG_ISSUE,
    conveyor_gate,
    escalate,
    gh,
    heartbeat_check,
    issue_marker_times,
    minutes_between,
    parse_time,
    post_issue_comment,
)
# Сигнал дрейфа пина апстрима (#134): вся логика — upstream_drift.py, здесь
# только вызов и честный сбой сверки (см. upstream_drift_lines ниже).
from upstream_drift import upstream_drift_check

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

# Номер задачи из текста PR/issue — одно место правды (#187): границы числа
# с обеих сторон, не подстрока (класс «#18 совпал с #180» на contract_check,
# 33570081734).
_tr_spec = importlib.util.spec_from_file_location(
    "task_ref", Path(__file__).resolve().parents[1] / "lib" / "task_ref.py")
task_ref = importlib.util.module_from_spec(_tr_spec)
_tr_spec.loader.exec_module(task_ref)

STALE_HOURS = 24
ONE_MERGE_PER_RUN = True
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
    issues = gh(f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100")
    return [issue for issue in issues if "pull_request" not in issue]


def open_pulls(repo: str) -> list[dict]:
    return gh(f"repos/{repo}/pulls?state=open&per_page=100")


def pr_references_issue(pull: dict, issue_number: int) -> bool:
    # Намеренно широкая семантика — ЛЮБОЕ упоминание, не только декларация
    # (в отличие от contract_check.py, #195): используется в reap_stale ниже,
    # чтобы не собрать замок с задачи, у которой открытый PR существует, но
    # ссылается на неё не первой строкой. Ошибиться в сторону «не трогать» тут
    # дешевле, чем в сторону «занята». Симметричная узкая проверка декларации —
    # task_ref.declares_task, для решений вида «эта задача уже занята PR».
    return task_ref.references_task(pull.get("body") or "", issue_number)


def reap_stale(repo: str, now: datetime, pulls: list[dict], merged: dict[int, dict] | None = None) -> list[str]:
    lines = []
    merged = merged or {}
    for issue in open_task_issues(repo):
        if not issue["assignees"]:
            continue
        if _issue_is_blocked(issue):
            continue
        number = issue["number"]
        if any(pr_references_issue(pull, number) for pull in pulls):
            continue
        timeline = gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")
        assigned_at = [
            event["created_at"] for event in timeline
            if event.get("event") == "assigned"
        ]
        if not assigned_at:
            continue
        last = parse_time(max(assigned_at))
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
                "Перебазируй на свежий main и продолжай — оркестратор подхватит."
            ),
        )
        lines.append(f"⚠️ PR #{pull['number']} помечен `conflict`")
    return lines


class UpdateBranchBudgetExhausted(RuntimeError):
    """Слот update_branch этого прогона уже занят (см. update_branch, #252,
    третий заход) — не инфраструктурный сбой, вызывающий код обязан поймать
    её отдельно и написать строку "подтянет следующий прогон", а не
    смешивать с реальными ошибками update-branch (конфликт, сеть)."""


# Слот на весь ПРОГОН планировщика, не на точку вызова: если бы дисциплина
# «максимум один успешно подтянутый update-branch за прогон» жила локальной
# переменной внутри update_remaining_pulls (как было раньше), вторая точка
# вызова — behind-ветка merge_queue ниже — могла бы независимо подтянуть ещё
# один PR тем же прогоном и снова запустить цикл push → сброс ai:ok →
# pr-review → ai-review, который эта задача (#252) и закрывает. Слот живёт
# здесь, в самой функции, которую обе точки вызова обязаны использовать для
# настоящего push'а — обойти его, не обходя update_branch, нельзя. Если
# появится третья точка вызова, ей тоже придётся идти через update_branch:
# другого способа дёрнуть PUT .../update-branch в этом файле нет.
_update_branch_used_this_run = False


def reset_update_branch_budget() -> None:
    """Обнуляет слот update_branch. main() вызывает это ровно один раз в
    начале каждого прогона планировщика — без явного сброса единожды
    потраченный слот остался бы закрытым до перезапуска процесса. Тесты
    сбрасывают его тем же вызовом перед каждым сценарием (см. autouse-фикстуру
    в test_scheduler.py)."""
    global _update_branch_used_this_run
    _update_branch_used_this_run = False


def update_branch(repo: str, pr_number: int) -> None:
    """gh pr update-branch. Обновление через GITHUB_TOKEN не зажигает проверки
    (защита GitHub от рекурсии) — бот-PR навсегда зависает в blocked, поэтому
    PAT, если задан. Один вызов — переиспользуется merge_queue (PR behind) и
    after_merge → update_remaining_pulls (#196, поведение 3: подтянуть
    остальных после слияния).

    Слот на прогон (см. _update_branch_used_this_run выше): вторая попытка
    подтянуть ЛЮБОЙ PR этим же прогоном — из любой точки вызова — кидает
    UpdateBranchBudgetExhausted вместо push'а. Успех отмечает слот занятым;
    неудачная попытка (RuntimeError/CalledProcessError, вероятный конфликт)
    слот не трогает — head не изменился, следующий кандидат в этом же
    прогоне ничем не рискует."""
    global _update_branch_used_this_run
    if _update_branch_used_this_run:
        raise UpdateBranchBudgetExhausted(
            f"слот update_branch этого прогона уже занят до PR #{pr_number}"
        )
    pat = os.environ.get("ORCHESTRA_PAT")
    if pat:
        subprocess.run(
            ["gh", "api", "-X", "PUT", f"repos/{repo}/pulls/{pr_number}/update-branch",
             "-H", f"Authorization: Bearer {pat}"],
            capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
            check=True,
        )
    else:
        gh("-X", "PUT", f"repos/{repo}/pulls/{pr_number}/update-branch")
    _update_branch_used_this_run = True


def update_branch_or_report(
    repo: str,
    pr_number: int,
    *,
    on_success: str,
    on_budget_exhausted: str,
    on_error: str,
) -> str:
    """Единственное место, где разбираются три исхода update_branch — успех,
    исчерпанный слот прогона (UpdateBranchBudgetExhausted), сетевой/иной сбой
    (RuntimeError/subprocess.CalledProcessError). Закрывает класс "разная
    обработка ошибок update_branch в разных точках вызова" (PR #288): раньше
    behind-ветка merge_queue ловила только UpdateBranchBudgetExhausted, а
    update_remaining_pulls — оба исхода, из-за чего сетевой сбой в
    merge_queue ронял весь main() без отчёта. Тот же класс уже чинили
    точечно в #248 (находка 3, вызовы детектора без try) и #253 (находка 4,
    ветки ok/fail без per-item try).

    Обе точки вызова обязаны идти через эту функцию, а не звать update_branch
    напрямую и заводить свой try/except — три параметра-текста обязательные,
    без значений по умолчанию, поэтому новая (третья) точка вызова, забывшая
    текст на сетевой сбой, падает TypeError'ом сразу при вызове, а не тонет в
    проде необработанным исключением. on_error форматируется через
    .format(error=...).

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


def bad_check_names(runs: list[dict]) -> list[str]:
    """Одно место правды (находка AI-ревью PR #253) для критерия «красного
    обязательного чека»: раньше `conclusion not in (success, skipped, neutral)`
    было продублировано трижды (pr_bad_checks, merge_queue, script_evidence) —
    расхождение критерия в одной из копий при правке двух других осталось бы
    незамеченным. Все три места зовут эту функцию. Пустой список runs даёт
    пустой список здесь — это НЕ «красных нет», а «проверки ещё не заведены»;
    вызывающий код обязан проверять пустоту runs отдельно (см. pr_is_merge_ready)."""
    return [run["name"] for run in runs if run["conclusion"] not in ("success", "skipped", "neutral")]


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


def merge_queue(repo: str, pulls: list[dict]) -> tuple[list[str], bool]:
    """Возвращает (строки отчёта, был_ли_жёсткий_сбой_after_merge) — см. after_merge."""
    lines = []
    skipped = []
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
            # Слот update_branch общий на весь прогон (#252, третий заход) —
            # эта ветка и update_remaining_pulls после слияния делят один и
            # тот же слот внутри update_branch, поэтому здесь тоже возможен
            # UpdateBranchBudgetExhausted, а не только сетевой сбой. Обработка
            # обоих исходов — в update_branch_or_report (#288), не здесь.
            skipped.append(update_branch_or_report(
                repo, pull["number"],
                on_success=f"#{pull['number']} — обновлена из main, проверки пойдут заново",
                on_budget_exhausted=(
                    f"#{pull['number']} — behind main и близок к слиянию, но слот update_branch "
                    "этого прогона уже занят другим PR; подтянет следующий прогон оркестратора (#252)"
                ),
                on_error=(
                    f"#{pull['number']} — behind main и близок к слиянию, но update_branch не удался "
                    "(вероятен конфликт — попадёт под mark_conflicts): {error}"
                ),
            ))
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
        gh(
            "-X", "PUT", f"repos/{repo}/pulls/{pull['number']}/merge",
            "-f", f"merge_method={MERGE_METHOD}",
        )
        lines.append(f"✅ PR #{pull['number']} слит ({MERGE_METHOD})")
        if skipped:
            lines += [f"   (отложены: {item})" for item in skipped]
        other_pulls = [p for p in pulls if p["number"] != pull["number"]]
        after_lines, hard_failure = after_merge(repo, pull, other_pulls)
        lines += after_lines
        return lines, hard_failure  # один за запуск: сериализация слияний
    if skipped:
        lines += [f"⏸️ {item}" for item in skipped]
    return lines, False


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


def after_merge(repo: str, pull: dict, other_pulls: list[dict] | None = None) -> tuple[list[str], bool]:
    """Действия после слияния. Merge через GITHUB_TOKEN НЕ создаёт push-события
    (защита GitHub от рекурсии), поэтому за деплоем и закрытием задач следим явно.
    other_pulls — открытые PR, кроме только что слитого (#196, поведение 3:
    подтянуть их из main); по умолчанию пусто — вызывающий код без списка
    остальных PR просто не подтягивает никого (сохраняет старое поведение).

    Возвращает (строки отчёта, был_ли_жёсткий_сбой_архивации). Мерж уже
    состоялся — жёсткий сбой не откатывает и не блокирует эту функцию, только
    поднимается наверх для эскалации (main() красит прогон ПОСЛЕ мержа)."""
    lines = []
    hard_failure = False
    number = pull["number"]
    # Пагинация (#294, третье место того же класса: check_pr.py и ai_review.py
    # уже читали через review_labels.list_pr_files, здесь оставалась сырая
    # первая страница) — PR за сотню файлов, где cf-worker/* стоят за сотой
    # позицией, молча не запускал бы deploy-worker.yml.
    files = review_labels.list_pr_files(repo, number, gh)
    if any((f["filename"] or "").startswith("cf-worker/") for f in files):
        subprocess.run(
            ["gh", "workflow", "run", "deploy-worker.yml", "--ref", "main"],
            capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
            check=True,
        )
        lines.append("🚀 deploy-worker запущен (push от GITHUB_TOKEN триггеры не создаёт)")
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
    for task_number in task_refs:
        # Release аренды (#121): слит PR — работа принята, замок больше не нужен.
        # Идемпотентно: замка может не быть (канал без аренды) — это не ошибка.
        try:
            lines.append(f"🔓 {claim_task.release(repo, int(task_number))}")
        except RuntimeError as error:
            lines.append(f"⚠️ замок task-{task_number} не снят: {error}")
        try:
            issue = gh(f"repos/{repo}/issues/{task_number}")
            if "pull_request" in issue or issue["state"] != "open":
                continue
            if "task" not in {label["name"] for label in issue["labels"]}:
                continue
            task_numbers.append(int(task_number))
            # Приёмка (accept_merged_tasks) видит только ДЕКЛАРИРУЮЩИЕ рефы
            # (task_ref.declares_task, первая строка тела PR) — merged_pr_map
            # строится из declared_tasks, не из extract_task_refs. Обещание
            # «приёмка закроет её сама» для ЛЮБОГО упоминания было ложным:
            # задача, упомянутая в прозе, не в карте приёмки, а через
            # ACCEPTANCE_PENDING_HOURS её снимает reap_stale с причиной
            # «PR не появился» — ровно тот класс неверных причин, что этот
            # PR чинит для объявленных задач (находка AI-ревью PR #253).
            if task_ref.declares_task(pull.get("body") or "", int(task_number)):
                reminder = (
                    f"🔁 PR #{number} слит в main. Мерж — ещё не готовность: если критерий "
                    "требует реального пост-мерж прогона (канарейка/E2E), проведи его и "
                    "оставь улику. Задачу закрывать не нужно и нельзя (`gh issue close` — "
                    "обход проверки, класс #56/#57) — стадия приёмки закроет её сама по "
                    "проверяемой улике (деплой/check-runs/файлы в main)."
                )
            else:
                reminder = (
                    f"🔁 PR #{number} слит в main и упомянул эту задачу, но не объявил её "
                    "первой строкой тела PR — стадия приёмки её не увидит и не закроет "
                    "сама. Если критерий требует реального пост-мерж прогона (канарейка/E2E), "
                    "проведи его и оставь улику; закрывай задачу только через PR, "
                    "декларирующий её номер первой строкой, иначе приёмка её не увидит."
                )
            gh(
                "-X", "POST", f"repos/{repo}/issues/{task_number}/comments",
                "-f", "body=" + reminder,
            )
            lines.append(f"🔁 #{task_number}: напоминание про пост-мерж проверку — закрывает приёмка (#227)")
        except RuntimeError as error:
            # один кривой реф не должен ронять остальные действия after_merge
            lines.append(f"⚠️ напоминание в #{task_number} не доставлено: {error}")
    # Архив сессий раннеров (#119) — только для ЗАДАЧ пула (метка task): номер из
    # тела PR может оказаться чужой активной задачей/PR без сессии, архивировать
    # его нельзя — утащим чужую живую сессию в архив.
    if task_numbers:
        archive_lines, hard_failure = archive_runner_sessions(task_numbers)
        lines += archive_lines
    lines += update_remaining_pulls(repo, pull["number"], other_pulls or [])
    return lines, hard_failure


def update_remaining_pulls(repo: str, merged_number: int, other_pulls: list[dict]) -> list[str]:
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

    Максимум один УСПЕШНО подтянутый кандидат за ПРОГОН, не за вызов этой
    функции (#252, третий заход): подтягивание близких к слиянию PR меняет
    их head — и само может сбросить их же `ai:ok` (pr-review.yml
    перезапускается на пуш и снимает ai:*-метки), то есть тот кандидат,
    который секунду назад проходил предикат, после первого же подтягивания
    может из него выпасть. Подтягивать сразу нескольких — значит гонять этот
    цикл несколько раз за один прогон. Слот общий с behind-веткой
    merge_queue: обе точки вызова делят один и тот же счётчик внутри
    update_branch (_update_branch_used_this_run), а не по локальной
    переменной на каждую точку вызова — иначе поведение осталось бы прежним,
    просто с двумя независимыми лимитами по одному вместо одного общего.
    Слот считается занятым только УСПЕХОМ: неудачная попытка (вероятный
    конфликт) не трогает head, значит следующий кандидат в этом же вызове
    ничем не рискует. Пропущенные из-за уже занятого слота кандидаты не
    молчат — они получают отдельную строку с указанием, что подтянет их
    следующий прогон оркестратора (тот же газ без состояния, что и у
    should_update_branch)."""
    lines = []
    for other in other_pulls:
        if other["number"] == merged_number or other.get("draft"):
            continue
        if not review_labels.should_update_branch(other["labels"]):
            lines.append(
                f"⏸️ PR #{other['number']} не подтянут из main после слияния #{merged_number} "
                "— не близок к слиянию и не в конфликте (#252)"
            )
            continue
        # Обработка всех трёх исходов — в update_branch_or_report (#288), не здесь.
        lines.append(update_branch_or_report(
            repo, other["number"],
            on_success=f"🔄 PR #{other['number']} обновлён из main после слияния #{merged_number}",
            on_budget_exhausted=(
                f"⏭️ PR #{other['number']} не подтянут из main после слияния #{merged_number} — слот "
                "update_branch этого прогона уже занят другим PR; подтянет следующий прогон "
                "оркестратора (#252)"
            ),
            on_error=(
                f"⚠️ PR #{other['number']} не обновлён из main после слияния #{merged_number} "
                "(вероятен конфликт — попадёт под mark_conflicts): {error}"
            ),
        ))
    return lines


def worker_runs_active(repo: str) -> bool:
    """Активный воркер = есть worker-ран в статусе in_progress или queued.
    Завершённые (в т.ч. упавшие) не считаются: упавший воркер при свободных
    задачах получит новый запуск — но пока задача назначена, пул свободных пуст
    и штурма не будет (возврат в пул только через stale-окно reap_stale)."""
    for status in ("in_progress", "queued"):
        payload = gh(
            f"repos/{repo}/actions/workflows/worker.yml/runs?status={status}&per_page=1"
        ) or {}
        if payload.get("workflow_runs"):
            return True
    return False


def dispatch_worker(repo: str, pool: list[dict]) -> list[str]:
    """Пульс конвейера: свободная задача есть, воркер простаивает → ровно один
    dispatch worker.yml за запуск оркестратора. Best-effort по построению:
    прав на dispatch нет, workflow нет на main, сеть — любой сбой диспатча
    не роняет оркестратор, слияния важнее подряда воркеру."""
    lines = []
    # Старейшая свободная — та же, которую воркер выберет oldest_free
    # (scripts/lib/free_task.py, #245): issues API отдаёт пул по убыванию
    # новизны, и без сортировки строка отчёта называла бы свежайшую задачу,
    # а не ту, которую воркер фактически возьмёт. Замки здесь не фильтруются —
    # это делает сам воркер на claim'е (#121).
    free = sorted(
        (issue for issue in pool if not issue["assignees"]),
        key=lambda issue: issue["number"],
    )
    if not free:
        return lines
    try:
        if worker_runs_active(repo):
            lines.append("👷 воркер уже работает — dispatch не нужен")
            return lines
        gh(
            "-X", "POST",
            f"repos/{repo}/actions/workflows/worker.yml/dispatches",
            "-f", "ref=main",
        )
        lines.append(
            f"👷 свободная задача #{free[0]['number']} — worker.yml запущен "
            "(воркер сам назначится и откроет PR)"
        )
    except RuntimeError as error:
        lines.append(f"⚠️ dispatch воркера не удался (не критично): {error}")
    return lines


# ── #196, поведение 1: готовый PR без вердикта — дёрнуть гейт самому ─────────────
# Триггер: review:ok стоит (или ai:failed — «ревью не состоялось ИЛИ провалено»,
# ADR 0007) дольше AI_REVIEW_RETRY_AFTER_MINUTES с момента ПОСЛЕДНЕГО события
# "labeled: review:ok" в таймлайне PR (новый пуш переставляет review:ok заново —
# см. review_labels.py, значит и таймер обязан отсчитывать от последней
# перестановки, а не от первого появления PR). ai:changes-requested и ai:ok сюда
# не попадают — это не «нет вердикта», это готовый вердикт (обрабатывает
# unhealthy_pulls/merge_queue соответственно).
#
# Носитель счётчика попыток — комментарий-маркер AI_REVIEW_RETRY_MARKER в самом
# PR (issues/{n}/comments — тот же endpoint, что и у задач, PR это issue).
# Обоснование выбора: 1) переживает перезапуск оркестратора (крон каждые 15 мин,
# память процесса не сохраняется) — комментарий читается заново каждым запуском,
# как это уже делает pulse_guard для маркеров серий; 2) не плодит новую метку
# в namespace review_labels.py (там только вердикты, не попытки); 3) виден
# человеку без доп. тулинга — то же качество, что у существующих следов
# reap_stale/mark_conflicts.


def last_review_ok_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Момент последней простановки review:ok — весь таймлайн (не только
    первая страница, review_labels.list_timeline, #303: класс потери хвоста
    на длинном таймлайне, тот же что list_pr_files/#294), None — метки не
    было вовсе."""
    timeline = review_labels.list_timeline(repo, pr_number, gh)
    labeled_at = [
        event["created_at"] for event in timeline
        if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == review_labels.REVIEW_OK
    ]
    return parse_time(max(labeled_at)) if labeled_at else None


def ai_review_retry_count(repo: str, pr_number: int) -> int:
    return len(issue_marker_times(repo, pr_number, AI_REVIEW_RETRY_MARKER))


def trigger_ai_review(repo: str, now: datetime, pulls: list[dict]) -> list[str]:
    lines = []
    for pull in pulls:
        labels = {label["name"] for label in pull["labels"]}
        if review_labels.REVIEW_OK not in labels:
            continue  # первый гейт ещё не пройден — рано
        has_verdict = bool(labels & set(review_labels.AI_VERDICTS))
        needs_retry = review_labels.AI_FAILED in labels
        if has_verdict and not needs_retry:
            continue  # ai:ok или ai:changes-requested — вердикт уже есть
        anchor = last_review_ok_labeled_at(repo, pull["number"])
        if anchor is None:
            continue  # событие не нашлось — не на чем считать порог, не гадаем
        age = minutes_between(anchor, now)
        if age < AI_REVIEW_RETRY_AFTER_MINUTES:
            continue  # ещё не истёк порог ожидания вердикта
        attempts = ai_review_retry_count(repo, pull["number"])
        if attempts >= AI_REVIEW_MAX_ATTEMPTS:
            lines.append(
                f"⏸️ PR #{pull['number']} без вердикта AI {int(age)} мин, но "
                f"авто-повторов уже {attempts}/{AI_REVIEW_MAX_ATTEMPTS} — не дёргаю снова, нужен человек"
            )
            continue
        gh(
            "-X", "POST", f"repos/{repo}/actions/workflows/ai-review.yml/dispatches",
            "-f", "ref=main", "-f", f"inputs[pr]={pull['number']}",
        )
        post_issue_comment(
            repo, pull["number"],
            f"🤖 {AI_REVIEW_RETRY_MARKER} Оркестратор сам запустил ai-review.yml: "
            f"{'review:ok' if not needs_retry else review_labels.AI_FAILED} держится "
            f"{int(age)} мин без готового вердикта (попытка {attempts + 1}/{AI_REVIEW_MAX_ATTEMPTS}).",
        )
        lines.append(
            f"🤖 PR #{pull['number']}: ai-review.yml запущен оркестратором "
            f"(попытка {attempts + 1}/{AI_REVIEW_MAX_ATTEMPTS}, {int(age)} мин без вердикта)"
        )
    return lines


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


def unhealthy_pulls(repo: str, now: datetime, pulls: list[dict]) -> list[str]:
    lines = []
    for issue in open_task_issues(repo):
        if not issue["assignees"]:
            continue
        if _issue_is_blocked(issue):
            continue
        number = issue["number"]
        referencing = [pull for pull in pulls if pr_references_issue(pull, number)]
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
            break  # одной причины на задачу достаточно — не дублируем комментарии
    return lines


# ── issue #269: PR полностью готов, а слияния не произошло — газ обязан гореть ──
# unhealthy_pulls (выше) ловит нездоровые PR (красный чек/ai:changes-requested).
# Этот инвариант — противоположный случай: PR ЗДОРОВ и готов (оба вердикта,
# зелёные проверки, mergeable_state допускает слияние), но остался несмерженным
# дольше порога. merge_queue сливает не более одного PR за прогон — если
# кандидатов несколько или прогон долго не случался (issue #269 — как раз про
# это), готовый PR может прождать часами при формально зелёных прогонах.


def last_ready_labeled_at(repo: str, pr_number: int) -> datetime | None:
    """Момент, когда PR стал полностью готов к слиянию: позже из двух событий
    'labeled' по обеим меткам-гейтам (review:ok, ai:ok) — тот же приём таймлайна
    (весь таймлайн постранично, review_labels.list_timeline), что
    last_review_ok_labeled_at. None — событие по какой-то из меток не найдено
    нигде в таймлайне (например, метка не проставлялась вовсе) — тогда
    возраст не считаем, не гадаем по неполным данным."""
    timeline = review_labels.list_timeline(repo, pr_number, gh)
    def labeled_at(label_name: str) -> list[str]:
        return [
            event["created_at"] for event in timeline
            if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label_name
        ]
    review_at = labeled_at(review_labels.REVIEW_OK)
    ai_at = labeled_at(review_labels.AI_OK)
    if not review_at or not ai_at:
        return None
    return parse_time(max(max(review_at), max(ai_at)))


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
        ready_since = last_ready_labeled_at(repo, pull["number"])
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

# Рядом со STALE_HOURS (то же назначение — «сколько ждать, прежде чем бить
# тревогу», но для другого канала: STALE_HOURS про назначение без PR,
# ACCEPTANCE_PENDING_HOURS — про PR, который слит, но улика не появляется).
ACCEPTANCE_PENDING_HOURS = 6

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
    """Task#N → самый свежий слитый PR, ОБЪЯВЛЯЮЩИЙ эту задачу (task_ref.declared_tasks
    — первая строка тела, то же соглашение, что уже применяет contract_check при
    авто-назначении). Узкая семантика намеренно: широкая (references_task, как в
    after_merge/reap_stale) годится для «не потерять живой PR», но не для «эта
    работа закрывает эту задачу» — там любое упоминание в прозе ложно закрыло бы
    чужую задачу."""
    best: dict[int, dict] = {}
    for pull in pulls:
        for number in task_ref.declared_tasks(pull.get("body") or ""):
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
    чека, что уже применяет pr_bad_checks/merge_queue (см. bad_check_names)."""
    payload = gh(f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100") or {}
    runs = payload.get("check_runs", [])
    if not runs:
        return "pending", "проверки PR на head sha ещё не найдены"
    bad = bad_check_names(runs)
    if bad:
        return "fail", f"красные проверки: {', '.join(bad)}"
    return "ok", f"{len(runs)} проверок PR зелёные (прогон с выводом)"


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


def accept_merged_tasks(
    repo: str, pool: list[dict], merged: dict[int, dict], now: datetime | None = None,
) -> tuple[list[str], bool]:
    """Стадия приёмки (#227) — см. блок комментариев выше. merged — карта
    Task#N → слитый PR (merged_pr_map(all_merged_pulls(repo)), один общий
    обход на весь прогон оркестратора, тот же, что использует reap_stale).
    now — момент прогона (по умолчанию текущее время), нужен только для
    порога ACCEPTANCE_PENDING_HOURS.
    Возвращает (строки отчёта, был_ли_жёсткий_сбой): жёсткий сбой эскалируется
    тут же на каждую затронутую задачу отдельно, но не прерывает обход
    остальных (мерж уже состоялся, задачи независимы)."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []
    hard_failure = False
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
        pull = merged.get(number)
        if pull is None:
            continue
        fail_marker = f"{ACCEPTANCE_FAIL_MARKER} PR #{pull['number']}"
        try:
            if issue_marker_times(repo, number, fail_marker):
                continue  # эта пара (задача, PR) уже провалила приёмку — не спамим
        except RuntimeError as error:
            lines.append(f"⚠️ #{number}: комментарии не прочитаны, приёмка отложена: {error}")
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
                lines.append(f"{text} (уже эскалировано, повторный Telegram не шлём)")
            else:
                escalation = escalate(repo, WATCHDOG_ISSUE, f"{error_marker} {text}")
                lines.append(f"{text} ({escalation})")
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
                    lines.append(f"⚠️ #{number}: маркер зависшей приёмки не прочитан: {error}")
                    continue
                if already_escalated:
                    lines.append(
                        f"⏳ #{number}: улика ({category}) не готова дольше "
                        f"{ACCEPTANCE_PENDING_HOURS} ч — уже эскалировано, жду новую работу")
                    continue
                text = (f"🚨 #{number}: приёмка PR #{pull['number']} ({category}) висит в "
                        f"pending дольше {ACCEPTANCE_PENDING_HOURS} ч после мержа — {detail}")
                escalation = escalate(repo, WATCHDOG_ISSUE, text)
                post_issue_comment(repo, number, f"{pending_marker} {detail} ({escalation}).")
                lines.append(text)
                hard_failure = True
                continue
            lines.append(f"⏳ #{number}: улика ({category}) ещё не готова — {detail}")
            continue

        if state in ("ok", "docs"):
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
                lines.append(f"⚠️ #{number}: закрытие приёмкой не завершено — {error}")
                continue
            try:
                lines.append(f"🔓 {claim_task.release(repo, int(number))}")
            except RuntimeError as error:
                # claim_task.release сам возвращает строку (не исключение) для
                # «замка не было» (см. _ref_missing) — RuntimeError сюда долетает
                # только на настоящей поломке (сеть/права/5xx), и её нельзя
                # глотать молча (тот же приём, что уже используют after_merge/
                # unhealthy_pulls).
                lines.append(f"⚠️ замок task-{number} не снят: {error}")
            lines.append(f"✅ #{number}: закрыта приёмкой ({category}) — {detail}")
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
            lines.append(f"⚠️ #{number}: отметка провала приёмки не завершена — {error}")
            continue
        try:
            lines.append(f"🔓 {claim_task.release(repo, int(number))}")
        except RuntimeError as error:
            lines.append(f"⚠️ замок task-{number} не снят: {error}")
        lines.append(f"♻️ #{number}: не закрыта, улика ({category}) провалена — {detail}")
    return lines, hard_failure


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines = [f"## Отчёт оркестратора {now.isoformat(timespec='seconds')}", ""]
    # Слот update_branch общий на ВЕСЬ этот прогон (#252, третий заход) —
    # обнуляем его один раз здесь, до merge_queue и до update_remaining_pulls,
    # которые обе точки вызова делят через один и тот же счётчик внутри
    # update_branch (см. reset_update_branch_budget).
    reset_update_branch_budget()

    # «Кто следит за следящим» (#120): первой проверкой, пока этот запуск жив,
    # кричим о пропавших пульсах — остальная работа может не иметь смысла,
    # если конвейер стоял.
    lines += heartbeat_check(repo, now)

    # Дрейф пина апстрима (#134): релиз новее пина source-build морды кричит
    # в задачу #134 + метка + Telegram, один раз на релиз.
    lines += upstream_drift_lines(repo)

    pulls = open_pulls(repo)
    lines.append(f"Открытых PR: {len(pulls)}")

    # Одна карта Task#N → слитый PR на весь прогон (#227) — reap_stale
    # (не путать слитый-но-непринятый PR с «PR не появился») и accept_merged_tasks
    # ниже читают её из одного источника, без второго обхода закрытых PR.
    merged = merged_pr_map(all_merged_pulls(repo))

    stale_lines = reap_stale(repo, now, pulls, merged)
    try:
        lease_lines = claim_task.collect_stale(repo, now)
    except RuntimeError as error:
        # сборщик замков не должен блокировать слияния, но и не молчит (#124-класс)
        lease_lines = [f"⚠️ обход замков задач не удался: {error}"]
    pulls = open_pulls(repo)  # состояние могло измениться
    conflict_lines = mark_conflicts(repo, pulls)
    # #196, поведение 2: нездоровый PR возвращает задачу в пул ДО очереди
    # слияния — освобождённая задача должна попасть в тот же отчёт, а
    # merge_queue ниже не зависит от пула задач.
    unhealthy_lines = unhealthy_pulls(repo, now, pulls)
    merge_lines, archive_hard_failure = merge_queue(repo, pulls)
    # #196, поведение 1: PR с review:ok без вердикта AI (или ai:failed)
    # дольше порога — оркестратор сам запускает ai-review.yml. Список PR
    # берём заново: merge_queue мог слить один PR этим же прогоном, и старый
    # снимок pulls содержал бы уже закрытый номер.
    ai_retry_lines = trigger_ai_review(repo, now, open_pulls(repo))
    # Инвариант issue #269: готовый PR, который так и не слился, кричит — та же
    # свежая выборка, что уже понадобилась trigger_ai_review выше.
    stale_ready_lines = stale_ready_pulls(repo, now, open_pulls(repo))

    # Приёмка (#227): задачи, чей PR уже слит, разбираются по улике ДО подсчёта
    # пула — свободно/в работе должно отражать уже закрытые этим же прогоном.
    pool = open_task_issues(repo)
    accept_lines, accept_hard_failure = accept_merged_tasks(repo, pool, merged, now)
    if accept_lines:
        pool = open_task_issues(repo)  # пересчёт: приёмка могла закрыть задачи
    free = sum(1 for issue in pool if not issue["assignees"])
    taken = len(pool) - free
    lines += ["", f"Пул задач: {free} свободно, {taken} в работе"]

    # Предохранитель (#120) решает, разрешён ли диспатч воркера в этом пульсе.
    conveyor_lines, dispatch_allowed = conveyor_gate(repo, now)
    worker_lines = dispatch_worker(repo, pool) if dispatch_allowed else []

    if (stale_lines or conflict_lines or unhealthy_lines or merge_lines or ai_retry_lines
            or stale_ready_lines or accept_lines or lease_lines or conveyor_lines or worker_lines):
        lines += ["", "### Действия",
                  *stale_lines, *lease_lines, *conflict_lines, *unhealthy_lines, *merge_lines,
                  *ai_retry_lines, *stale_ready_lines, *accept_lines, *conveyor_lines, *worker_lines]
    else:
        lines += ["", "Действий не требуется."]

    # Приёмка (#227): жёсткий сбой уже эскалирован по каждой затронутой задаче
    # внутри accept_merged_tasks — здесь только красим прогон, второй сигнал
    # не заводим (тот же принцип, что у archive_hard_failure ниже).
    if accept_hard_failure:
        lines.append("🚨 приёмка: минимум одна улика не проверена из-за поломки — прогон окрашен красным")

    # Архив сессий раннера после мержа (#119) сломан «возможность есть, но не
    # работает» (#174) — мерж уже состоялся, откатывать нельзя и остальную
    # очередь эта поломка не блокирует. Но fail loud: прогон обязан покраситься
    # ПОСЛЕ того, как отчёт уже сохранён, и эскалация уходит тем же каналом,
    # что предохранитель конвейера (#120), — не заводим третий канал сигнала.
    if archive_hard_failure:
        escalation = escalate(
            repo, WATCHDOG_ISSUE,
            "🚨 edge-harness: [статус: архив сессии раннера сломан]\n"
            "После мержа PR архивация сессии раннера в морде dsh-edge не удалась "
            "(возможность есть, но сломана — см. отчёт этого прогона orchestra выше). "
            "Мерж не откатывается; сессия останется в списке активных до ручного "
            "разбора или следующего успешного мержа той же задачи.",
        )
        lines.append(f"🚨 архив сессии раннера сломан — прогон окрашен красным ({escalation})")
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
