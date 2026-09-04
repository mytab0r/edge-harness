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
          И даже среди подходящих — не все разом (#252, второй заход):
          максимум один УСПЕШНО подтянутый кандидат за вызов, остальные
          получают строку и ждут следующего прогона — иначе подтягивание
          первого кандидата может сбросить ai:ok второго тем же циклом.
     Пороги — AI_REVIEW_RETRY_AFTER_MINUTES / AI_REVIEW_MAX_ATTEMPTS /
     UNHEALTHY_PR_AFTER_MINUTES в pulse_guard.py, рядом с остальными порогами
     предохранителя (одно место правды).
  10. Сигнал дрейфа пина апстрима (#134): релиз апстрима новее пина
      source-build морды (dsh-edge/upstream.json) кричит в задачу #134
      + метка update-available + Telegram, один раз на релиз. Логика в
      scripts/orchestra/upstream_drift.py; сбой сверки не роняет планировщик,
      но и не молчит — ⚠️ в отчёте (сломанная сверка прячет дрейф так же
      надёжно, как её отсутствие).
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


def reap_stale(repo: str, now: datetime, pulls: list[dict]) -> list[str]:
    lines = []
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


def update_branch(repo: str, pr_number: int) -> None:
    """gh pr update-branch. Обновление через GITHUB_TOKEN не зажигает проверки
    (защита GitHub от рекурсии) — бот-PR навсегда зависает в blocked, поэтому
    PAT, если задан. Один вызов — переиспользуется merge_queue (PR behind) и
    after_merge (#196, поведение 3: подтянуть остальных после слияния)."""
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


def pr_bad_checks(repo: str, pull: dict) -> list[str]:
    """Имена красных (не success/skipped/neutral) check-run'ов текущего head.
    Один и тот же критерий «красного обязательного чека», что и внутри
    merge_queue (гейт слияния) — но отдельный вызов: unhealthy_pulls (#196,
    поведение 2) читает состояние ДО очереди слияния и по другому набору PR
    (у задачи может быть несколько PR), переиспользовать один HTTP-ответ негде."""
    checks = gh(f"repos/{repo}/commits/{pull['head']['sha']}/check-runs?per_page=100")
    runs = checks.get("check_runs", [])
    return [run["name"] for run in runs if run["conclusion"] not in ("success", "skipped", "neutral")]


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
            update_branch(repo, pull["number"])
            skipped.append(f"#{pull['number']} — обновлена из main, проверки пойдут заново")
            continue
        if state not in ("clean", "unstable", "has_hooks"):
            skipped.append(f"#{pull['number']} — mergeable_state={state or 'не вычислен GitHub'}")
            continue
        checks = gh(f"repos/{repo}/commits/{pull['head']['sha']}/check-runs?per_page=100")
        runs = checks.get("check_runs", [])
        if not runs:
            skipped.append(f"#{pull['number']} — проверки ещё не заведены")
            continue
        bad = [run["name"] for run in runs if run["conclusion"] not in ("success", "skipped", "neutral")]
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
    files = gh(f"repos/{repo}/pulls/{number}/files?per_page=100")
    if any((f["filename"] or "").startswith("cf-worker/") for f in files):
        subprocess.run(
            ["gh", "workflow", "run", "deploy-worker.yml", "--ref", "main"],
            capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"},
            check=True,
        )
        lines.append("🚀 deploy-worker запущен (push от GITHUB_TOKEN триггеры не создаёт)")
    # Закрытие задачи — не здесь и не по ключевым словам: мерж доказывает PR,
    # а не готовность задачи, чей критерий часто живёт после мержа (деплой,
    # канарейка, E2E). Напоминаем исполнителю; закрытие — за ним, с уликами
    # (кейс #56/#57: Closes закрыл задачу до зелёной канарейки).
    # Намеренно любое упоминание, не только декларация (см. pr_references_issue
    # выше, #195): слитый PR мог упомянуть смежную задачу не первой строкой —
    # снять её замок и напомнить про закрытие безопаснее, чем оставить висеть.
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
            gh(
                "-X", "POST", f"repos/{repo}/issues/{task_number}/comments",
                "-f",
                "body=" + (
                    f"🔁 PR #{number} слит в main. Мерж — ещё не готовность: проведи "
                    "пост-мерж проверку (деплой/канарейка/E2E), приложи улики "
                    "и закрой задачу."
                ),
            )
            lines.append(f"🔁 #{task_number}: напоминание — закрыть после пост-мерж проверки")
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

    Максимум один УСПЕШНО подтянутый кандидат за вызов (#252, второй заход):
    подтягивание близких к слиянию PR меняет их head — и само может сбросить
    их же `ai:ok` (pr-review.yml перезапускается на пуш и снимает ai:*-метки),
    то есть тот кандидат, который секунду назад проходил предикат, после
    первого же подтягивания может из него выпасть. Подтягивать сразу
    нескольких — значит гонять этот цикл несколько раз за один прогон.
    Та же дисциплина, что уже у merge_queue («ровно один PR за запуск»,
    сериализация через возврат после первого действия). Слот считается
    занятым только УСПЕХОМ: неудачная попытка (вероятный конфликт) не
    трогает head, значит следующий кандидат в этом же вызове ничем не рискует.
    Пропущенные из-за уже занятого слота кандидаты не молчат — они получают
    отдельную строку с указанием, что подтянет их следующий прогон
    оркестратора (тот же газ без состояния, что и у should_update_branch)."""
    lines = []
    pulled = False
    for other in other_pulls:
        if other["number"] == merged_number or other.get("draft"):
            continue
        if not review_labels.should_update_branch(other["labels"]):
            lines.append(
                f"⏸️ PR #{other['number']} не подтянут из main после слияния #{merged_number} "
                "— не близок к слиянию и не в конфликте (#252)"
            )
            continue
        if pulled:
            lines.append(
                f"⏭️ PR #{other['number']} уже обновлён этим запуском — за раз подтягивается "
                f"только один кандидат (#252); подтянет следующий прогон оркестратора"
            )
            continue
        try:
            update_branch(repo, other["number"])
            lines.append(f"🔄 PR #{other['number']} обновлён из main после слияния #{merged_number}")
            pulled = True
        except (RuntimeError, subprocess.CalledProcessError) as error:
            lines.append(
                f"⚠️ PR #{other['number']} не обновлён из main после слияния #{merged_number} "
                f"(вероятен конфликт — попадёт под mark_conflicts): {error}"
            )
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
    timeline = gh(f"repos/{repo}/issues/{pr_number}/timeline?per_page=100")
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


def upstream_drift_lines(repo: str) -> list[str]:
    """Сигнал дрейфа пина апстрима (#134) — обёртка для main(): сверка не должна
    ронять планировщик (слияния важнее), но и не имеет права молчать: сломанная
    сверка прячет дрейф ровно так же, как её отсутствие до #134. Видимость — ⚠️
    в отчёте пульса; тот же приём, что у collect_stale (#124-класс)."""
    try:
        return upstream_drift_check(repo)
    except RuntimeError as error:
        return [f"⚠️ сверка пина с релизами апстрима не удалась — дрейф сейчас невидим: {error}"]


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    lines = [f"## Отчёт оркестратора {now.isoformat(timespec='seconds')}", ""]

    # «Кто следит за следящим» (#120): первой проверкой, пока этот запуск жив,
    # кричим о пропавших пульсах — остальная работа может не иметь смысла,
    # если конвейер стоял.
    lines += heartbeat_check(repo, now)

    # Дрейф пина апстрима (#134): релиз новее пина source-build морды кричит
    # в задачу #134 + метка + Telegram, один раз на релиз.
    lines += upstream_drift_lines(repo)

    pulls = open_pulls(repo)
    lines.append(f"Открытых PR: {len(pulls)}")

    stale_lines = reap_stale(repo, now, pulls)
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

    pool = open_task_issues(repo)
    free = sum(1 for issue in pool if not issue["assignees"])
    taken = len(pool) - free
    lines += ["", f"Пул задач: {free} свободно, {taken} в работе"]

    # Предохранитель (#120) решает, разрешён ли диспатч воркера в этом пульсе.
    conveyor_lines, dispatch_allowed = conveyor_gate(repo, now)
    worker_lines = dispatch_worker(repo, pool) if dispatch_allowed else []

    if (stale_lines or conflict_lines or unhealthy_lines or merge_lines or ai_retry_lines
            or lease_lines or conveyor_lines or worker_lines):
        lines += ["", "### Действия",
                  *stale_lines, *lease_lines, *conflict_lines, *unhealthy_lines, *merge_lines,
                  *ai_retry_lines, *conveyor_lines, *worker_lines]
    else:
        lines += ["", "Действий не требуется."]

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
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"::error::orchestra: {error}")
        sys.exit(1)
