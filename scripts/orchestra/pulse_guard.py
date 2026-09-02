#!/usr/bin/env python3
"""Предохранитель конвейера и «кто следит за следящим» (#120).

Два механизма, живущие рядом, потому что у них один канал оповещения и один
носитель решения — отчёт оркестратора + Telegram + след в задаче #120:

1. Предохранитель конвейера: перед dispatch воркера считаются подряд идущие
   НЕуспешные прогоны worker.yml (conclusion=failure/cancelled, любой success
   между ними сбрасывает счётчик). От WORKER_FAILURE_PAUSE_AFTER подряд —
   диспатч остановлен, сигнал уходит один раз на серию (не на каждый пульс).
2. «Кто следит за следящим»: мёртвый scheduler сам крикнуть не может, поэтому
   возраст последнего успешного пульса проверяет КАЖДЫЙ запуск планировщика:
   если предыдущий успех старше HEARTBEAT_MAX_AGE_MINUTES — значит пульсы
   пропадали, и этот, опоздавший, запуск кричит в Telegram, пока жив.
   Полный охват (scheduler не запустился вовсе) даёт только внешний монитор —
   не подтверждено, отложено; GitHub нативно шлёт failure-письма владельцу.

Единственный канал решения — отчёт + задача #120 + Telegram; «метки-статуса на
workflow» у GitHub нет, а commit-статусы живут на sha и умирают на squash-мерже.

Маркеры серий: сигнал «один раз на серию» определяется по комментариям в #120,
содержащим маркер-токен. Токены скобочные и нечаянно не пишутся в прозе:
PAUSE_MARKER ставится только сигнальным комментарием, поэтому «уже оповещено»
не спутать с обычным текстом обсуждения.

Пороги — константы здесь и только здесь (одно место правды); scheduler.py
импортирует их вместе с gh()/parse_time().
"""

import json
import os
import subprocess
import sys
from datetime import datetime

WORKER_WORKFLOW = "worker.yml"
ORCHESTRA_WORKFLOW = "orchestra.yml"

# Неуспех = явный красный вывод. Отмена (cancelled) — тоже не успех: серия
# «запустили и отменили» жжёт минуты так же, как падение.
FAILURE_CONCLUSIONS = ("failure", "cancelled")

# Подряд красных прогонов worker.yml, после которых авто-диспетч останавливается.
WORKER_FAILURE_PAUSE_AFTER = 3

# Полуоткрытое состояние (#205): разомкнутый предохранитель сам не снимается —
# зелёного прогона неоткуда взяться без диспатча. Поэтому по истечении выдержки
# разрешается РОВНО ОДИН пробный диспатч. Выдержка растёт экспоненциально с
# каждой красной пробой (2^(N-1) * база) и упирается в потолок; оркестратор
# бежит каждые 15 минут (cron orchestra.yml), поэтому база кратна интервалу —
# первая проба не раньше следующего пульса после начала паузы.
PROBE_BACKOFF_BASE_MINUTES = 15
PROBE_BACKOFF_MAX_MINUTES = 240

# Пульс orchestra идёт каждые 15 минут (cron orchestra.yml); три пропущенных
# интервала — пульсы пропали. Пока опоздавший запуск жив, он обязан крикнуть.
HEARTBEAT_MAX_AGE_MINUTES = 45

# Задача-статус: туда идут сигнальные комментарии (и маркеры серий).
WATCHDOG_ISSUE = 120

PAUSE_MARKER = "[статус конвейера: пауза]"
HEARTBEAT_MARKER = "[статус пульса: пропадал]"
# Номер пробы дописывается через пробел: "[статус конвейера: проба N]" — маркер
# ищется подстрокой без номера (issue_marker_times), номер разбирается отдельно
# (probe_marker_attempts) там, где нужна выдержка, а не просто факт «была проба».
PROBE_MARKER = "[статус конвейера: проба"


def gh(*args: str) -> dict | list | None:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True, text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args[:2])}: {result.stderr.strip()}")
    # Часть успешных вызовов (например, POST .../dispatches) отвечает 204 без
    # тела — отсутствие JSON это успех, а не ошибка разбора.
    return json.loads(result.stdout) if result.stdout.strip() else None


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def minutes_between(start: datetime, now: datetime) -> float:
    return (now - start).total_seconds() / 60


# ── Чистые решения: серия красных → можно ли диспетчить ─────────────────────────


def count_consecutive_failures(conclusions: list) -> int:
    """Подряд идущие неуспешные прогоны с начала списка (список — от нового к
    старому, как отдаёт GitHub). Первый success сбрасывает счётчик в 0; первый
    незавершённый прогон (conclusion=None) останавливает подсчёт: решение не
    принимается по тому, что ещё бежит."""
    streak = 0
    for conclusion in conclusions:
        if conclusion in FAILURE_CONCLUSIONS:
            streak += 1
        elif conclusion == "success":
            break
        else:  # None — прогон ещё не завершён: судить о серии рано
            break
    return streak


def decide_dispatch(failures: int, pause_after: int = WORKER_FAILURE_PAUSE_AFTER) -> bool:
    """True — диспатч воркера разрешён; False — конвейер на паузе."""
    return failures < pause_after


# ── Полуоткрытое состояние (#205): проба после выдержки ──────────────────────────


def probe_backoff_minutes(
    attempt: int,
    base: float = PROBE_BACKOFF_BASE_MINUTES,
    cap: float = PROBE_BACKOFF_MAX_MINUTES,
) -> float:
    """Выдержка перед пробой N (attempt считается от 1 — ещё не было ни одной
    красной пробы в этой серии). Растёт экспоненциально, упирается в потолок:
    предохранитель не должен пробовать чаще и чаще, если причина не уходит."""
    if attempt < 1:
        attempt = 1
    return min(base * (2 ** (attempt - 1)), cap)


def decide_gate_state(
    failures: int,
    probe_attempts: int,
    last_marker_at: datetime | None,
    now: datetime,
    pause_after: int = WORKER_FAILURE_PAUSE_AFTER,
) -> str:
    """Четыре исхода решения (три состояния предохранителя + первый вход):
    'closed' — failures < порога, диспатч обычный;
    'first'  — серия только что стала красной, маркера ещё нет — ставим
               PAUSE_MARKER, диспатч НЕ даём (выдержка отсчитывается от этого
               момента, проба идёт не раньше следующего пульса);
    'probe'  — серия красная, маркер есть, выдержка с последнего маркера
               (пауза или предыдущая красная проба) истекла — разрешена
               РОВНО ОДНА проба;
    'open'   — серия красная, выдержка ещё не истекла — диспатч запрещён.

    probe_attempts — сколько проб в этой серии УЖЕ было красными (0 для самой
    первой паузы)."""
    if decide_dispatch(failures, pause_after):
        return "closed"
    if last_marker_at is None:
        return "first"
    backoff = probe_backoff_minutes(probe_attempts + 1)
    return "probe" if minutes_between(last_marker_at, now) >= backoff else "open"


def probe_marker_attempts(markers: list[tuple[datetime, str]]) -> int:
    """Сколько красных проб уже было в открытой серии: наибольший номер,
    разобранный из тела маркеров '[статус конвейера: проба N]'. Без маркеров —
    0 (следующая проба будет первой)."""
    attempts = 0
    for _, body in markers:
        text = body.strip()
        idx = text.find(PROBE_MARKER)
        if idx == -1:
            continue
        rest = text[idx + len(PROBE_MARKER):].lstrip()
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            attempts = max(attempts, int(digits))
    return attempts


def pause_notification_pending(marker_times: list[datetime], last_success_at: datetime | None) -> bool:
    """Оповещать о серии нужно, только если с последнего успеха маркера ещё не было:
    маркер новее последнего success — серия та же, повтор не шлём. Успехов нет вовсе —
    серия бесконечна, живого маркера достаточно."""
    if not marker_times:
        return True
    if last_success_at is None:
        return False
    return max(marker_times) < last_success_at


# ── Чистые решения: возраст пульса ───────────────────────────────────────────────


def heartbeat_age_minutes(last_success_at: str | datetime, now: datetime) -> float:
    if isinstance(last_success_at, str):
        last_success_at = parse_time(last_success_at)
    return minutes_between(last_success_at, now)


def decide_heartbeat(last_success_at: str | datetime, now: datetime,
                     max_age_minutes: float = HEARTBEAT_MAX_AGE_MINUTES) -> str:
    """'ok' — пульс в норме; 'stale' — пульсы пропадали, кричать."""
    age = heartbeat_age_minutes(last_success_at, now)
    return "stale" if age > max_age_minutes else "ok"


# ── Тексты сигналов (детерминированные, тестируются на содержание) ───────────────


def pause_alert_text(failures: int, run: dict | None, error: str) -> str:
    run_line = ""
    if run:
        run_line = f"\nПоследний красный: {run.get('display_title') or run.get('name') or 'run'} — {run.get('html_url', 'без ссылки')}"
    return (
        f"🚨 edge-harness: {PAUSE_MARKER}\n"
        f"{failures} красных прогонов {WORKER_WORKFLOW} подряд "
        f"(порог {WORKER_FAILURE_PAUSE_AFTER}) — авто-диспетч воркера остановлен."
        f"{run_line}\n"
        f"Ошибка последнего прогона: {error}\n"
        f"Возобновление: полуоткрытое состояние (#205) само пробует диспатч по "
        f"истечении выдержки (от {PROBE_BACKOFF_BASE_MINUTES} мин, экспоненциально, "
        f"потолок {PROBE_BACKOFF_MAX_MINUTES} мин) — либо ручной "
        "`gh workflow run worker`, который сбрасывает счётчик сразу."
    )


def probe_alert_text(attempt: int, backoff_minutes: float, run: dict | None, error: str) -> str:
    run_line = ""
    if run:
        run_line = f"\nПоследний красный: {run.get('display_title') or run.get('name') or 'run'} — {run.get('html_url', 'без ссылки')}"
    return (
        f"🔎 edge-harness: {PAUSE_MARKER}\n"
        f"{PROBE_MARKER} {attempt}]\n"
        f"Пробный диспатч после паузы {int(backoff_minutes)} мин — проверяем, "
        f"жива ли причина серии красных {WORKER_WORKFLOW}."
        f"{run_line}\n"
        f"Ошибка последнего прогона: {error}\n"
        "Зелёная проба замкнёт предохранитель; красная — выдержка вырастет "
        "экспоненциально и уйдёт следующая проба."
    )


def heartbeat_alert_text(age_minutes: float, run: dict | None) -> str:
    run_line = ""
    if run:
        run_line = f"\nПоследний успех: {run.get('html_url', 'без ссылки')}"
    return (
        f"🚨 edge-harness: {HEARTBEAT_MARKER}\n"
        f"Пульсы orchestra пропадали: последний успешный прогон "
        f"{int(age_minutes)} мин назад (порог {HEARTBEAT_MAX_AGE_MINUTES} = "
        "3 интервала по 15 мин). Этот прогон опоздал — кричу, пока жив.\n"
        "Частые причины: расписание отключено после 60 дней без активности "
        "(docs/research/21-github-actions.md), красные прогоны — см. Actions и почту. "
        "Полный охват мёртвого пульса даст только внешний монитор (не подтверждено, отложено)."
        f"{run_line}"
    )


# ── IO-обвязка: чтение прогонов, сигналы, след в задаче ──────────────────────────


def recent_runs(repo: str, workflow: str, per_page: int = 10) -> list[dict]:
    payload = gh(
        f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={per_page}"
    ) or {}
    return payload.get("workflow_runs", [])


def last_failure_error(repo: str, run: dict) -> str:
    """Человекочитаемая причина последнего красного прогона: упавшие job'ы и шаги.
    Best-effort: недоступность деталей не мешает факту паузы, но и не теряется —
    уходит в текст сигнала."""
    try:
        payload = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=20") or {}
    except RuntimeError as error:
        return f"детали недоступны: {error}"
    bad = []
    for job in payload.get("jobs", []):
        if job.get("conclusion") not in FAILURE_CONCLUSIONS:
            continue
        steps = ", ".join(
            step["name"] for step in job.get("steps", [])
            if step.get("conclusion") in FAILURE_CONCLUSIONS
        )
        bad.append(f"{job['name']} — шаги: {steps or 'нет (упал до шагов)'}")
    if not bad:
        return f"упавшие job'ы не найдены (conclusion прогона: {run.get('conclusion')})"
    return "; ".join(bad)


def issue_marker_times(repo: str, issue_number: int, marker: str) -> list[datetime]:
    payload = gh(f"repos/{repo}/issues/{issue_number}/comments?per_page=100") or []
    return [
        parse_time(comment["created_at"])
        for comment in payload
        if marker in (comment.get("body") or "")
    ]


def issue_markers_any(repo: str, issue_number: int, markers: tuple[str, ...]) -> list[tuple[datetime, str]]:
    """Как issue_marker_times, но для нескольких маркеров сразу и с телом
    комментария — нужно там, где решение зависит не только от факта маркера,
    но и от его содержимого (номер попытки пробы, #205)."""
    payload = gh(f"repos/{repo}/issues/{issue_number}/comments?per_page=100") or []
    result = []
    for comment in payload:
        body = comment.get("body") or ""
        if any(marker in body for marker in markers):
            result.append((parse_time(comment["created_at"]), body))
    return result


def post_issue_comment(repo: str, issue_number: int, text: str) -> None:
    gh("-X", "POST", f"repos/{repo}/issues/{issue_number}/comments", "-f", "body=" + text)


def send_telegram(text: str) -> bool:
    """Best-effort: место правды — комментарий в задаче #120, Telegram — активный
    канал. Промах кричит warning'ом в лог, не молчит (см. WORKER-PLAYBOOK)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("::warning::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сигнал не отправлен",
              file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", "30", "-X", "POST",
             f"https://api.telegram.org/bot{token}/sendMessage",
             "--data-urlencode", f"chat_id={chat}",
             "--data-urlencode", f"text={text}"],
            capture_output=True, text=True,
        )
    except OSError as error:
        print(f"::warning::curl недоступен, сигнал не отправлен: {error}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"::warning::Telegram не принял сигнал: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def escalate(repo: str, issue_number: int, text: str) -> str:
    """Канал эскалации поломок — общий с предохранителем конвейера: комментарий
    в задачу-статус + Telegram, best-effort по каждому (см. send_telegram).
    Переиспользуется вне пары «предохранитель/пульс» (scheduler.archive_runner_sessions,
    #119/#174) — нельзя заводить второй канал для того же класса «поломка после
    мержа», один канал решения уже есть."""
    try:
        post_issue_comment(repo, issue_number, text)
        posted = True
    except RuntimeError as error:
        print(f"::warning::след в #{issue_number} не оставлен: {error}", file=sys.stderr)
        posted = False
    delivered = send_telegram(text)
    return (f"Telegram: {'доставлен' if delivered else 'НЕ доставлен'}; "
            f"след в #{issue_number}: {'оставлен' if posted else 'НЕ оставлен'}")


# ── Сценарии, вызываемые scheduler.py ────────────────────────────────────────────


def heartbeat_check(repo: str, now: datetime) -> list[str]:
    """«Кто следит за следящим»: вызывается КАЖДЫМ запуском планировщика до всей
    остальной работы. Опоздавший запуск — единственный, кто может закричать о
    пропавших пульсах, поэтому проверка первая."""
    runs = recent_runs(repo, ORCHESTRA_WORKFLOW, per_page=5)
    last_ok = next((r for r in runs if r.get("conclusion") == "success"), None)
    if last_ok is None:
        return [f"ℹ️ успешных прогонов {ORCHESTRA_WORKFLOW} не найдено — возраст пульса не оценить"]
    age = heartbeat_age_minutes(last_ok["created_at"], now)
    if decide_heartbeat(last_ok["created_at"], now) == "ok":
        return [f"💗 пульс orchestra в норме: последний успех {int(age)} мин назад "
                f"(порог {HEARTBEAT_MAX_AGE_MINUTES})"]
    text = heartbeat_alert_text(age, last_ok)
    delivered = send_telegram(text)
    try:
        # Telegram — на каждый опоздавший запуск; след в задаче — один на эпизод:
        # новый комментарий только если прежний маркер старше последнего успеха
        # (пульсы успели восстановиться и снова пропали).
        markers = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_MARKER)
        if pause_notification_pending(markers, parse_time(last_ok["created_at"])):
            post_issue_comment(repo, WATCHDOG_ISSUE, text)
    except RuntimeError as error:
        print(f"::warning::след в #{WATCHDOG_ISSUE} не оставлен: {error}", file=sys.stderr)
    return [f"🚨 пульс orchestra пропадал: последний успех {int(age)} мин назад "
            f"> {HEARTBEAT_MAX_AGE_MINUTES} (Telegram: "
            f"{'доставлен' if delivered else 'НЕ доставлен'}; след в #{WATCHDOG_ISSUE})"]


def conveyor_gate(repo: str, now: datetime) -> tuple[list[str], bool]:
    """Предохранитель: перед dispatch воркера. Возвращает (строки отчёта,
    разрешён_ли_диспетч). Три состояния (#205):

    closed — failures < порога, диспатч обычный, тихо;
    probe  — серия красная, выдержка с последнего маркера серии истекла —
             РОВНО ОДИН пробный диспатч в этом пульсе, маркер пробы ставится
             сразу (следующий пульс уже видит его и не пробует повторно, даже
             если этот прогон worker.yml ещё не успеет завершиться);
    open   — серия красная, выдержка не истекла — диспатч заблокирован, тихо
             (маркер уже стоит, второй раз не оповещаем).

    Носитель состояния — комментарии-маркеры в #120 (тот же приём, что уже
    даёт issue_marker_times для пульса и что #196 использует для счётчика
    попыток ai-review): переживают перезапуск оркестратора, читаются заново
    каждым прогоном планировщика.

    count_consecutive_failures останавливается на первом незавершённом прогоне
    (conclusion=None) и возвращает 0 — это верно для «серии ещё не было», но
    ломается, если 0 означает «идёт проба текущей красной серии»: диспатч
    воркера на предыдущем пульсе ещё выполняется. Поэтому маркеры активной
    серии читаются ДО решения по failures — если маркер есть, failures=0 не
    может означать «closed», решение отдаётся decide_gate_state."""
    runs = recent_runs(repo, WORKER_WORKFLOW, per_page=10)
    failures = count_consecutive_failures([r.get("conclusion") for r in runs])

    last_ok = next((r for r in runs if r.get("conclusion") == "success"), None)
    last_ok_at = parse_time(last_ok["created_at"]) if last_ok else None
    try:
        all_markers = issue_markers_any(repo, WATCHDOG_ISSUE, (PAUSE_MARKER,))
    except RuntimeError as error:
        if decide_dispatch(failures):
            return ([f"🟢 серия красных worker.yml: {failures} "
                     f"(порог {WORKER_FAILURE_PAUSE_AFTER}) — диспатч разрешён"],
                    True)
        # не смогли прочитать маркеры — не гадаем о выдержке, диспатч не даём
        # (fail loud: серия красная, значит по умолчанию заперто)
        print(f"::warning::маркеры #{WATCHDOG_ISSUE} не прочитаны: {error}", file=sys.stderr)
        return ([f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
                 f"подряд — диспатч остановлен (маркеры #{WATCHDOG_ISSUE} недоступны)"],
                False)
    # Маркеры прошлой серии (старше последнего success) не в счёт — иначе новая
    # серия унаследует чужой номер попытки и выдержку с первого же пульса.
    markers = [(t, b) for t, b in all_markers if last_ok_at is None or t > last_ok_at]
    if not markers and decide_dispatch(failures):
        return ([f"🟢 серия красных worker.yml: {failures} "
                 f"(порог {WORKER_FAILURE_PAUSE_AFTER}) — диспатч разрешён"],
                True)
    marker_times = [t for t, _ in markers]
    last_marker_at = max(marker_times) if marker_times else None
    probe_attempts = probe_marker_attempts(markers)
    # Маркер активной серии уже доказывает, что порог был достигнут раньше —
    # даже если сейчас failures=0 из-за незавершённой пробы (conclusion=None
    # останавливает count_consecutive_failures на 0, но это не значит «серия
    # закрылась»). Не даём decide_gate_state спутать это с closed.
    effective_failures = max(failures, WORKER_FAILURE_PAUSE_AFTER) if markers else failures
    state = decide_gate_state(effective_failures, probe_attempts, last_marker_at, now)

    if state == "open":
        return ([f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
                 f"подряд — диспатч остановлен (уже оповещено, см. #{WATCHDOG_ISSUE})"],
                False)

    error = last_failure_error(repo, runs[0]) if runs else "прогонов не найдено"
    if state == "probe":
        attempt = probe_attempts + 1
        backoff = probe_backoff_minutes(attempt)
        text = probe_alert_text(attempt, backoff, runs[0] if runs else None, error)
        try:
            post_issue_comment(repo, WATCHDOG_ISSUE, text)
        except RuntimeError as err:
            print(f"::warning::сигнал в #{WATCHDOG_ISSUE} не доставлен: {err}", file=sys.stderr)
        delivered = send_telegram(text)
        return ([f"🔎 пробный диспатч после паузы {int(backoff)} мин (попытка {attempt}) — "
                 f"{failures} красных {WORKER_WORKFLOW} подряд (Telegram: "
                 f"{'доставлен' if delivered else 'НЕ доставлен'}; сигнал в #{WATCHDOG_ISSUE})"],
                True)

    # state == "first": серия только что стала красной, маркера ещё нет —
    # ставим PAUSE_MARKER, диспатч не даём (выдержка отсчитается от этого
    # маркера, проба — не раньше следующего пульса).
    text = pause_alert_text(failures, runs[0] if runs else None, error)
    try:
        post_issue_comment(repo, WATCHDOG_ISSUE, text)
    except RuntimeError as err:
        print(f"::warning::сигнал в #{WATCHDOG_ISSUE} не доставлен: {err}", file=sys.stderr)
    delivered = send_telegram(text)
    return ([f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
             f"подряд — диспатч остановлен (Telegram: "
             f"{'доставлен' if delivered else 'НЕ доставлен'}; сигнал в #{WATCHDOG_ISSUE})"],
            False)
