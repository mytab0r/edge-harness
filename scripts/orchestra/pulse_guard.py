#!/usr/bin/env python3
"""Предохранитель конвейера и «кто следит за следящим» (#120).

Три механизма, живущие рядом, потому что у них один канал оповещения и один
носитель решения — отчёт оркестратора + Telegram + след в задаче #120:

1. Предохранитель конвейера: перед dispatch воркера считаются подряд идущие
   НЕуспешные прогоны worker.yml (conclusion=failure/cancelled, любой success
   между ними сбрасывает счётчик). От WORKER_FAILURE_PAUSE_AFTER подряд —
   диспатч остановлен, сигнал уходит один раз на серию (не на каждый пульс).
   Возобновление (#220): success-маркер RESUME_MARKER в #120 (ставит его
   scheduler.after_merge, когда слит PR задачи, над которой работал последний
   красный прогон) считается виртуальным success серии — сброс без пробы.
2. «Кто следит за следящим»: мёртвый scheduler сам крикнуть не может, поэтому
   возраст последнего успешного пульса проверяет КАЖДЫЙ запуск планировщика:
   если предыдущий успех старше HEARTBEAT_MAX_AGE_MINUTES — значит пульсы
   пропадали, и этот, опоздавший, запуск кричит в Telegram, пока жив.
   Полный охват (scheduler не запустился вовсе) даёт только внешний монитор —
   не подтверждено, отложено; GitHub нативно шлёт failure-письма владельцу.
3. Гвардия непрочитанных провалов (#477): владелец — «кто-то мониторит
   ошибки?» — красный прогон worker.yml/hands.yml/orchestra.yml/deploy-*.yml
   был строкой в Actions без разбора. failure_watch дешёвым запросом
   (`status=failure`, малый per_page) находит свежий провал каждого из
   WATCHED_WORKFLOWS, достаёт последнюю содержательную строку `##[error]`
   из лога упавшего job'а (факт, не гипотеза — правило AGENTS.md) и
   классифицирует причину: 'infra' (известная сигнатура лимита/сети — лечится
   ожиданием, один тихий след в #120 на класс) или 'defect' (наш дефект —
   заводит задачу в пул, тоже одну на класс). Дедуп — по отпечатку КЛАССА
   причины (workflow + job + нормализованная строка ошибки), не по run id:
   повторяющийся баг не должен плодить второй сигнал на каждый новый прогон.
   Прогоны PR-событий не разбираются — красный обязательный чек на PR ведёт
   автодетектор простоя (stall_detector, #201, check:red:<имя>); исключение
   закрывает класс «один дефект — две задачи» (находка ревью PR #488).

Единственный канал решения — отчёт + задача #120 + Telegram; «метки-статуса на
workflow» у GitHub нет, а commit-статусы живут на sha и умирают на squash-мерже.

Маркеры серий: сигнал «один раз на серию» определяется по комментариям в #120,
содержащим маркер-токен. Токены скобочные и нечаянно не пишутся в прозе:
PAUSE_MARKER ставится только сигнальным комментарием, поэтому «уже оповещено»
не спутать с обычным текстом обсуждения.

Пороги — константы здесь и только здесь (одно место правды); scheduler.py
импортирует их вместе с gh()/parse_time().
"""

import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

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
# Скобочные, как остальные (докстринг модуля требует — находка ревью PR #318,
# п.2: голый CAPS-токен без скобок мог случайно процитироваться в обсуждении
# #120 и навсегда подавить комментарий как «уже бывший»). Пара маркеров, а не
# один: у эпизода «тиков нет вовсе» нет успешного прогона, чей timestamp можно
# сравнить (в отличие от HEARTBEAT_MARKER/pause_notification_pending) — эпизод
# закрывается ЯВНЫМ комментарием, когда тики снова нашлись (находка ревью
# PR #318, п.1: было — один комментарий на всю жизнь задачи #120).
HEARTBEAT_NO_TICKS_MARKER = "[статус пульса: тиков нет]"
HEARTBEAT_TICKS_RESUMED_MARKER = "[статус пульса: тики вернулись]"

# Success-маркер авто-возобновления (#220): scheduler.after_merge ставит его в
# #120, когда слит PR задачи ветки, а последний красный прогон worker.yml
# работал именно над этой задачей (след аренды «worker run <id>»). conveyor_gate
# считает маркер виртуальным success серии: красные прогоны и маркеры старше
# него перестают участвовать в решении, ожидать пробу (#205) не нужно. Номер PR
# дописывается в тот же маркер: "[статус конвейера: возобновлено по мержу #N]" —
# по полному токену (маркер+номер) дедуплицируется «один сигнал на мерж».
RESUME_MARKER = "[статус конвейера: возобновлено по мержу"

# ── Пороги петли открытого PR (#196) ─────────────────────────────────────────────
# Три поведения scheduler.py читают пороги отсюда — рядом с остальными порогами
# предохранителя, одно место правды на весь конвейер.

# review:ok стоит (или ai:failed) дольше этого — оркестратор сам дёргает
# ai-review.yml. Якорь отсчёта — время ПОСЛЕДНЕГО события "labeled: review:ok"
# в таймлайне PR: новый пуш переставляет review:ok заново (review_labels.py),
# так что таймер не тикает на устаревшем ревью.
AI_REVIEW_RETRY_AFTER_MINUTES = 30

# Верхняя граница авто-повторов ai-review на один PR: без неё сбойный провайдер
# (или контрактная ошибка модели) жёг бы квоту в цикле каждые 15 минут.
# Счётчик — по числу маркеров AI_REVIEW_RETRY_MARKER в комментариях PR (носитель
# переживает перезапуск оркестратора, см. scheduler.ai_review_retry_count).
AI_REVIEW_MAX_ATTEMPTS = 3

AI_REVIEW_RETRY_MARKER = "[ai-review: авто-повтор]"

# Красный обязательный чек или ai:changes-requested дольше этого — задача
# возвращается в пул (см. scheduler.unhealthy_pulls). Отсчёт — updated_at PR:
# он не тикает, пока PR не тронули (в отличие от review:ok, здесь нет
# перелейбловки на каждый пуш — красный чек живёт, пока его не почини́ли).
# Идемпотентность возврата — не отдельный маркер, а снятое assignee задачи
# (тот же приём, что уже использует reap_stale): второй маркер для того же
# класса «уже обработано» не заводится.
UNHEALTHY_PR_AFTER_MINUTES = 120

# Инвариант issue #269: PR полностью готов к слиянию (обе метки-гейта, все
# обязательные проверки зелёные), а слияния не произошло дольше того же
# UNHEALTHY_PR_AFTER_MINUTES — своего порога не заводим, это тот же класс
# «наблюдаемая величина — задержка, а не статус последнего прогона»
# (см. scheduler.stale_ready_pulls). Маркер — идемпотентность сигнала в #120,
# тот же приём, что PAUSE_MARKER/HEARTBEAT_MARKER выше.
READY_STALL_MARKER = "[статус: PR готов, слияние не идёт]"

# ── Расшивка конфликтов (issue #474) ──────────────────────────────────────────────
# Конфликт (mergeable_state=dirty, scheduler.mark_conflicts) раньше был тормозом
# без газа: метка + комментарий, дальше ждём человека. Причина конфликта почти
# всегда одна — main ушёл вперёд (механический дрейф), а разрешение —
# механическое (git rebase origin/main). Признака «дрейф или содержательный
# конфликт» ДО попытки нет: GitHub REST отдаёт только mergeable_state
# (dirty/clean), не конфликтующие ханки — поэтому решение простое (владелец,
# issue #474): РОВНО одна авто-попытка ребейза на PR (лифтайм-счётчик, тот же
# приём, что AI_REVIEW_MAX_ATTEMPTS — не сбрасывается по эпизодам конфликта,
# см. scheduler.conflict_rework_attempts); сошлось — mark_conflicts снимет
# метку сама на следующем проходе, не сошлось — эскалация владельцу.
CONFLICT_REWORK_MAX_ATTEMPTS = 1

CONFLICT_REWORK_MARKER = "[conflict: авто-ребейз]"

# Номер PR — часть маркера (по образцу READY_STALL_MARKER выше): эскалация
# конфликта решается по каждому PR отдельно, общий маркер без номера подавил
# бы навсегда все PR, кроме первого просигналившего.
CONFLICT_ESCALATION_MARKER = "[conflict: эскалация]"

# ── Гвардия непрочитанных провалов ключевых workflow (#477) ──────────────────
# worker.yml уже целиком под предохранителем conveyor_gate (пауза/проба) —
# сюда включён тоже, потому что предохранитель отвечает на «дать ли диспатч»,
# а failure_watch — на другой вопрос («кто-нибудь разберёт ПРИЧИНУ и заведёт
# задачу на дефект»); это разные обязанности одного и того же провала.
WATCHED_WORKFLOWS = (
    "worker.yml", "hands.yml", "orchestra.yml",
    "deploy-worker.yml", "deploy-dsh-edge.yml",
)

# Метка авто-заведённых задач по дефектам CI — рядом с обязательной `task`
# (пул), чтобы дедуп искал ТОЛЬКО среди них, не среди всего пула.
FAILURE_WATCH_LABEL = "ci-failure"

# Известные сигнатуры ИНФРАСТРУКТУРНЫХ (не наших) причин — лимиты провайдера/
# GitHub, сеть; лечится ожиданием, не диффом. Список закрытый и консервативный
# НАРОЧНО (см. classify_failure_cause): нераспознанное считается дефектом, а
# не тихо прощается как инфраструктура — fail loud по умолчанию.
INFRA_ERROR_SIGNATURES = (
    "rate_limit:",
    "rate limit reached",
    "secondary rate limit",
    "abuse-rate-limit",
    "quota_exhausted",
    "rate_limit_retry_budget_exceeded",
    "dial tcp",
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "i/o timeout",
    "etimedout",
    "econnreset",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "temporarily unavailable",
)

# Сигнатуры устаревшей базы: свой отдельный класс, не инфраструктура (не
# лечится ожиданием) и не «наш дефект кода» — газ уже назван в другом месте
# (issue #474, авто-ребейз конфликтных PR; task-branch/pre-commit сами
# отказывают на протухшей базе и просят git rebase origin/main). failure_watch
# узнаёт этот класс, чтобы НЕ дублировать задачу поверх уже объявленного газа —
# наблюдение без задачи и без сигнала в #120 (не спамить тем, что уже лечится).
STALE_BASE_SIGNATURES = (
    "main уехал вперёд",
    "non-fast-forward",
    "failed to push some refs",
    # «checks awaiting conflict resolution» была здесь до ревью PR #488 (раунд 2)
    # и удалена как мёртвая: на конфликтном PR проверки не запускаются вовсе
    # (docs/agents/PROTOCOL.md), строки в логе какого-либо прогона нет —
    # сигнатура не могла сработать ни на одном реальном логе.
)

FAILURE_WATCH_INFRA_MARKER = "[failure-watch: инфраструктура"

# Окно свежести провала (находка ревью PR #488): пульс ходит раз в 15 минут
# (cron orchestra.yml), окно — двойной период с запасом на задержку раннера.
# Без него `runs[0]` навсегда остаётся тем же старым красным прогоном после
# закрытия задачи (отпечаток исчезает из открытых → следующий пульс заводит
# задачу заново на уже почившую причину, бесконечный цикл).
FAILURE_WATCH_WINDOW_MINUTES = 30

# Потолок разобранных упавших job'ов на один красный прогон (находка ревью PR
# #488, чеклист: раньше разбирался только bad_jobs[0], хвост прятался молча).
# Каждый job — загрузка полного лога (дорогой запрос, квота API); красных
# job'ов на прогон обычно один-два. Остальные НЕ теряются — названы поимённо
# в наблюдении (тот же класс «не прятать хвост молча», что #308).
FAILURE_WATCH_MAX_JOBS_PER_RUN = 3


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


# ── Чистые решения: классификация причины провала (#477) ────────────────────────


def classify_failure_cause(text: str) -> str:
    """'stale_base' — устаревшая база (газ уже назван в #474/task-branch, не
    дублируем задачу); 'infra' — известная сигнатура лимита/сети, лечится
    ожиданием; 'defect' — всё остальное, В ТОМ ЧИСЛЕ нераспознанное (fail
    loud по умолчанию: не прощаем молча то, чего не понимаем)."""
    lowered = (text or "").lower()
    for signature in STALE_BASE_SIGNATURES:
        if signature in lowered:
            return "stale_base"
    for signature in INFRA_ERROR_SIGNATURES:
        if signature in lowered:
            return "infra"
    return "defect"


def failure_fingerprint(workflow: str, job_name: str, error_text: str) -> str:
    """Отпечаток КЛАССА провала — workflow + упавший job + нормализованная
    строка ошибки (без hex/id, схлопнутые пробелы, обрезка). НЕ run id:
    одинаковый баг на разных прогонах обязан схлопнуться в одну запись —
    правило «один сигнал на класс», не на каждый прогон."""
    normalized = re.sub(r"\b[0-9a-f]{6,}\b", "", (error_text or "").lower())
    normalized = re.sub(r"\d+", "N", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()[:160]
    raw = f"{workflow}|{job_name}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


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


def series_anchor(last_success_at: datetime | None, resume_at: datetime | None) -> datetime | None:
    """#220: точка, левее которой серия не существует — последний реальный
    success ИЛИ success-маркер возобновления (сброс мержем), что новее.
    None, когда нет ни того, ни другого: серия бесконечна, в счёт всё."""
    times = [t for t in (last_success_at, resume_at) if t is not None]
    return max(times) if times else None


def runs_after(runs: list[dict], anchor: datetime | None) -> list[dict]:
    """Прогоны новее якоря серии. Красные прогоны старше якоря объяснены
    (закрыты success'ом или сброшены мержем, #220) и подсчёт серии не кормят:
    series_anchor + эта фильтрация вместе дают ровно семантику «виртуального
    success», вставленного в момент якоря."""
    if anchor is None:
        return runs
    return [r for r in runs if parse_time(r["created_at"]) > anchor]


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


def episode_reopened(open_times: list[datetime], close_times: list[datetime]) -> bool:
    """Двухмаркерный приём для эпизода без success-timestamp для сравнения
    (находка ревью PR #318, п.1): открывающего маркера ещё не было — это
    первое объявление; открывающий маркер старше самого свежего закрывающего —
    предыдущий эпизод закрылся, текущий — новый, объявляем; открывающий маркер
    новее (или закрывающего вовсе нет) — эпизод всё ещё тот же, повтор не шлём."""
    if not open_times:
        return True
    if not close_times:
        return False
    return max(open_times) < max(close_times)


# ── Чистые решения: возраст пульса ───────────────────────────────────────────────


ORCHESTRA_TICK_EVENTS = ("schedule", "workflow_dispatch")


def real_orchestra_ticks(runs: list[dict]) -> list[dict]:
    """Клиентский фильтр-гвардия (защита в глубину) поверх серверного:
    прогоны РЕАЛЬНОГО job'а `orchestra` (schedule/workflow_dispatch), не
    `contract` того же файла (issue #133, живой замер 2026-09-05).

    `orchestra.yml` несёт два job'а: `contract` — на КАЖДЫЙ `pull_request`,
    быстрая проверка контракта PR↔задача, ничего общего с пульсом планировщика
    (`if: github.event_name == 'pull_request'`); `orchestra` — сам
    планировщик (мерж-очередь, предохранитель, ЭТОТ watchdog) — только на
    `schedule`/`workflow_dispatch` (`if: github.event_name != 'pull_request'`).
    `heartbeat_check` до этой правки брал последний success БЕЗ учёта
    события: частые зелёные `contract`-прогоны (от параллельных PR нескольких
    агентов, десятки в час) маскировали то, что job `orchestra` не
    запускался часами — ровно тогда, когда наблюдатель нужнее всего (тот же
    класс силент-неправды, что #303 уже закрыл в `fetchLatestOrchestraRunId`
    фильтром `event=workflow_dispatch`, только там событие ровно одно
    легитимное, а здесь два: `schedule` И `workflow_dispatch`, поэтому
    фильтр — исключение `pull_request`, а не единственное разрешённое
    значение). Живая улика: 2026-09-05, DO-пульс (`workflow_dispatch`) не
    создавал ран orchestra.yml с 04:35 до как минимум 14:24 (~9ч49м при
    цикле 15 мин), а `heartbeat_check` не закричал ни разу — маскировали
    непрерывные `pull_request`-прогоны `contract`.

    Клиентский фильтр САМ ПО СЕБЕ не спасает от труncации: если он применяется
    к одной сырой странице (`per_page=100`) и между двумя настоящими тиками
    пролетело ≥100 `pull_request`-прогонов `contract` (достижимо при
    параллельной работе нескольких агентов — ровно находка ревью PR #318),
    отфильтрованный список молча пустеет и настоящий тик теряется за
    страницей. Поэтому решающая фильтрация — серверная, в `orchestra_tick_runs`
    (`?event=...`, по одному запросу на легитимное событие, тот же приём, что
    #303 уже применил в cf-worker/src/harness.ts): страница GitHub для каждого
    события содержит ТОЛЬКО прогоны этого события, contract её не засоряет.
    Эта функция остаётся как чистый юнит и вторая линия защиты, не единственная.

    Allowlist по `ORCHESTRA_TICK_EVENTS`, не denylist `!= "pull_request"`
    (находка AI-ревью PR #318, второй раунд): `ORCHESTRA_TICK_EVENTS` —
    уже единственное место правды о легитимных событиях для серверного
    фильтра выше (`orchestra_tick_runs`). Denylist держал бы второе,
    расходящееся определение «легитимного» здесь — третий триггер
    `orchestra.yml` (например `push`) молча прошёл бы эту вторую линию
    защиты, ровно тот же класс маскировки через чёрный ход, что #303 и
    сам этот PR уже закрывали для других мест."""
    return [run for run in runs if run.get("event") in ORCHESTRA_TICK_EVENTS]


def orchestra_tick_runs(repo: str, per_page: int = 100) -> list[dict]:
    """Реальные тики job'а `orchestra`: серверный фильтр `?event=...`, по
    одному запросу на каждое легитимное событие (`schedule`,
    `workflow_dispatch`), результаты слиты и отсортированы по свежести.
    В отличие от одной сырой страницы + клиентского `real_orchestra_ticks`,
    страница на каждый запрос не тратится на `pull_request`-прогоны
    `contract` — GitHub фильтрует их до пагинации, не после (найдено ревью
    PR #318: `exclude_pull_requests=true` на этом же эндпоинте проверен живым
    запросом и НЕ фильтрует по событию — параметр относится к другому
    признаку; используем `event=` явно на каждое легитимное значение)."""
    runs: list[dict] = []
    for event in ORCHESTRA_TICK_EVENTS:
        runs.extend(recent_runs(repo, ORCHESTRA_WORKFLOW, per_page=per_page, event=event))
    runs.sort(key=lambda run: run["created_at"], reverse=True)
    return real_orchestra_ticks(runs)


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
        f"потолок {PROBE_BACKOFF_MAX_MINUTES} мин); мерж PR, чинившего причину "
        f"(задачи последнего красного прогона), сбрасывает счётчик сразу (#220); "
        f"либо ручной `gh workflow run worker`, который сбрасывает счётчик тоже."
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


def resume_alert_text(pr_number: int, task_number: int, run: dict | None) -> str:
    """Сигнал сброса серии мержем (#220). Маркер RESUME_MARKER с номером PR —
    единственное, по чему conveyor_gate отличает возобновление от обычного
    текста и по чему дедуплицируется повторный сигнал о том же мерже; ссылка
    на прогон — та же улика, что в pause/probe текстах выше."""
    run_line = ""
    if run:
        run_line = f"\nПоследний красный: {run.get('display_title') or run.get('name') or 'run'} — {run.get('html_url', 'без ссылки')}"
    return (
        f"✅ edge-harness: {RESUME_MARKER} #{pr_number}]\n"
        f"Слит PR #{pr_number} — задача #{task_number}, над которой работал "
        f"последний красный прогон {WORKER_WORKFLOW}. Причина серии с высокой "
        f"вероятностью починена этим мержем: серия сбрасывается сразу, без "
        f"ожидания пробы (#220)."
        f"{run_line}\n"
        f"Следующий пульс диспатчит воркера как обычно; если причина жива, "
        f"серия наберётся заново и предохранитель сработает как обычно."
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


# ── Telegram-HTML (#170): один формат сообщений на всех отправителей ──────────────
# Класс #170: parse_mode не передавался нигде, Telegram рендерил plain text и
# ссылки оставались голыми URL. Теперь parse_mode=HTML шлют ОБА отправителя
# репозитория (send_telegram ниже и telegram_report в scripts/worker/task.sh) —
# эти два места и есть весь список, новых отправителей заводить нельзя.


SHORT_TITLE_WORDS = 6


def tg_html(plain: str) -> str:
    """Экранирование plain-текста до безопасного Telegram-HTML. При
    parse_mode=HTML символы <, >, & управляющие: заголовок задачи или причина
    сбоя с ними иначе развалили бы доставку всего сообщения (400 от Telegram).
    Атрибутных значений из пользовательского текста мы не строим, кавычки
    не трогаем (quote=False)."""
    return html.escape(plain or "", quote=False)


def short_title(title: str, words: int = SHORT_TITLE_WORDS) -> str:
    """«Заголовок задачи в двух словах» (#170): первые SHORT_TITLE_WORDS слов,
    остальное молча отбрасывается — сообщение обязано остаться коротким.
    Разбивка строго по пробелам, не по позициям: байтовая обрезка режет
    кириллицу посреди символа."""
    return " ".join((title or "").split()[:words])


def merge_telegram_text(repo: str, pr_number: int, task_number: int, task_title: str) -> str:
    """Сообщение о слиянии (#170) — слово «выполнена» звучит ТОЛЬКО здесь, на факт
    слияния в main. «PR открыт» воркера — другой факт и другой текст
    (scripts/worker/task.sh). Номера задачи и PR — кликабельные ссылки, заголовок
    экранирован. Ссылки строятся из repo+номера, а не из html_url ответа API:
    источник детерминирован и не зависит от того, какие поля дошли в dict."""
    return (
        f"✅ задача <a href=\"https://github.com/{repo}/issues/{task_number}\">#{task_number}</a>"
        f" выполнена — слито в main:"
        f" <a href=\"https://github.com/{repo}/pull/{pr_number}\">#{pr_number}</a>"
        f" «{tg_html(short_title(task_title))}»"
    )


# ── IO-обвязка: чтение прогонов, сигналы, след в задаче ──────────────────────────


def recent_runs(repo: str, workflow: str, per_page: int = 10, event: str | None = None) -> list[dict]:
    query = f"per_page={per_page}"
    if event:
        query += f"&event={event}"
    payload = gh(
        f"repos/{repo}/actions/workflows/{workflow}/runs?{query}"
    ) or {}
    return payload.get("workflow_runs", [])


def failing_jobs(repo: str, run: dict) -> list[dict]:
    """Job'ы прогона с неуспешным conclusion — общий источник для
    last_failure_error и failure_watch (одна выборка, не вторая копия
    запроса). Пустой список и при отсутствии упавших job'ов, и при сбое
    самого запроса (best-effort, вызывающий решает, что это значит)."""
    try:
        payload = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=20") or {}
    except RuntimeError:
        return []
    return [job for job in payload.get("jobs", []) if job.get("conclusion") in FAILURE_CONCLUSIONS]


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


# Строки-boilerplate раннера, аннотированные ##[error] самим GitHub Actions,
# а не причиной провала (находка ревью PR #488, живой замер на прогоне
# 34027035455): «Process completed with exit code N» и завершающие строки
# раннера идут ПОСЛЕ содержательной ::error::-строки (например, из die() в
# task.sh) и раньше неё в списке "с конца" — если их не пропускать, отпечаток
# схлопывает разные дефекты одного job'а в один класс по коду выхода.
LAST_ERROR_LOG_BOILERPLATE = (
    "process completed with exit code",
    "the operation was canceled",
    "the job running on runner",
)

# ANSI-escape сырого лога не должны попадать в факт: строка уходит в тело
# задачи и след #120/Telegram (мусорные управляющие коды в тексте сигнала).
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


def last_error_log_line(repo: str, job_id: int) -> str | None:
    """Последняя СОДЕРЖАТЕЛЬНАЯ строка `##[error]` из лога упавшего job'а —
    конкретный факт, не гипотеза (правило AGENTS.md, PR #475): не заставляет
    человека открывать Actions, чтобы увидеть, что именно сломалось.
    Boilerplate-строки самого раннера (LAST_ERROR_LOG_BOILERPLATE) —
    не причина, пропускаются. Best-effort: недоступность лога (квота/права/
    раннер убит до записи) или отсутствие содержательной строки не роняет
    классификацию — просто нет строки, вызывающий откатывается на имена
    упавших шагов.

    `--allow-escape-sequences` (находка живой проверки PR #488): сырой лог
    job'а почти всегда несёт ANSI-escape, и gh (замер на 2.98.0, 2026-09-06)
    отдаёт на него ОТКАЗ с exit 1 («the response contains terminal escape
    sequences») — без флага факта не бывает НИКОГДА, весь failure_watch
    мёртв в проде при живых зелёных тестах на моках. Флаг безопасен здесь:
    лог нашего репозитория мы разбираем САМИ подстрочным поиском
    `##[error]` и никогда не печатаем в терминал, где escape могли бы сыграть."""
    try:
        result = subprocess.run(
            ["gh", "api", "--allow-escape-sequences",
             f"repos/{repo}/actions/jobs/{job_id}/logs"],
            capture_output=True, text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except OSError as error:
        print(f"::warning::лог job {job_id} не прочитан: {error}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    marker = "##[error]"
    for line in reversed(result.stdout.splitlines()):
        idx = line.find(marker)
        if idx == -1:
            continue
        candidate = ANSI_ESCAPE_RE.sub("", line[idx:]).strip()
        lowered = candidate.lower()
        if any(pattern in lowered for pattern in LAST_ERROR_LOG_BOILERPLATE):
            continue
        return candidate
    return None


def all_issue_comments(repo: str, issue_number: int) -> list[dict]:
    """Все комментарии issue постранично, не только первая страница
    `per_page=100` (#276, тот же класс, что review_labels.list_pr_files/
    list_timeline и #294/#303: молчаливая обрезка на самом длинном обсуждении
    прятала бы САМЫЙ СВЕЖИЙ маркер серии — issue_marker_times/issue_markers_any
    решают «уже сигналили в этом эпизоде» по последнему маркеру, поэтому именно
    длинная задача-статус #120/#134, у которой маркеров и так больше всего,
    первой теряла бы хвост). Листание — та же форма: короткая страница
    (`len(chunk) < 100`) значит «дальше страниц нет». Публичная (без
    подчёркивания), потому что читается и вне пары «предохранитель/пульс» —
    scheduler.resume_series_by_merge ищет в задачах след аренды (#220)."""
    page = 1
    comments: list[dict] = []
    while True:
        chunk = gh(f"repos/{repo}/issues/{issue_number}/comments?per_page=100&page={page}") or []
        if not isinstance(chunk, list) or not chunk:
            break
        comments.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return comments


def issue_marker_times(repo: str, issue_number: int, marker: str) -> list[datetime]:
    payload = all_issue_comments(repo, issue_number)
    return [
        parse_time(comment["created_at"])
        for comment in payload
        if marker in (comment.get("body") or "")
    ]


def issue_markers_any(repo: str, issue_number: int, markers: tuple[str, ...]) -> list[tuple[datetime, str]]:
    """Как issue_marker_times, но для нескольких маркеров сразу и с телом
    комментария — нужно там, где решение зависит не только от факта маркера,
    но и от его содержимого (номер попытки пробы, #205)."""
    payload = all_issue_comments(repo, issue_number)
    result = []
    for comment in payload:
        body = comment.get("body") or ""
        if any(marker in body for marker in markers):
            result.append((parse_time(comment["created_at"]), body))
    return result


def post_issue_comment(repo: str, issue_number: int, text: str) -> None:
    gh("-X", "POST", f"repos/{repo}/issues/{issue_number}/comments", "-f", "body=" + text)


def send_telegram(text: str, as_html: bool = False, reply_markup: dict | None = None) -> bool:
    """Best-effort: место правды — комментарий в задаче #120, Telegram — активный
    канал. Промах кричит warning'ом в лог, не молчит (см. WORKER-PLAYBOOK).

    Единственный Python-отправитель репозитория (#170; второй — bash-овский
    telegram_report в scripts/worker/task.sh) и ВСЕГДА шлёт parse_mode=HTML:
    без него Telegram трактует текст как plain text и кликабельных ссылок не
    бывает. По умолчанию текст считается plain и экранируется (tg_html) —
    сигнальные тексты выше не содержат разметки, а их динамические части
    (заголовки прогонов, причины сбоев) больше не могут развалить доставку
    случайным < или &. as_html=True — текст уже собран как Telegram-HTML
    (merge_telegram_text): его динамические части обязаны были пройти tg_html
    у сборщика, повторное экранирование убило бы ссылки.

    reply_markup (#254) — инлайн-клавиатура решения владельца (см.
    build_decision_keyboard); необязательна, обычные алерты её не передают —
    сигнатура обратно совместима, поведение существующих вызовов не меняется."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("::warning::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — сигнал не отправлен",
              file=sys.stderr)
        return False
    payload_text = text if as_html else tg_html(text)
    args = ["curl", "-fsS", "--max-time", "30", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "--data-urlencode", f"chat_id={chat}",
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", f"text={payload_text}"]
    if reply_markup is not None:
        args += ["--data-urlencode", f"reply_markup={json.dumps(reply_markup)}"]
    try:
        result = subprocess.run(args, capture_output=True, text=True)
    except OSError as error:
        print(f"::warning::curl недоступен, сигнал не отправлен: {error}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"::warning::Telegram не принял сигнал: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


# Префикс комментария-решения — то же значение, что cf-worker/src/config.ts::
# TELEGRAM.decisionCommentPrefix (пишет apply_owner_decision.py) и что разбирает
# scripts/orchestra/waiting_owner_guard.py::DECISION_MARKER_RE (#471, слит) —
# одно место правды по формату в Python-стороне этого модуля. Синхронность с
# config.ts проверяет scripts/lib/test_telegram_callback_format_sync.py (#254,
# находка ревью PR #486: раньше это утверждалось прозой без гвардии — тест
# юнит-стороны Python и юнит-тест TS проверяли один и тот же литерал каждый
# сам по себе, ни один не читал оба исходника, рассинхрон прошёл бы CI зелёным).
DECISION_COMMENT_PREFIX = "РЕШЕНИЕ"

# Префикс callback_data — то же значение, что cf-worker/src/config.ts::
# TELEGRAM.callbackPrefix (разбирает cf-worker/src/harness.ts::
# parseOwnerDecisionCallback) — синхронность проверяет тот же
# test_telegram_callback_format_sync.py, что и DECISION_COMMENT_PREFIX выше.
# Лимит байт — свойство Bot API, не выбор репозитория, сверять нечего (см.
# докстринг гвардии).
OWNER_DECISION_CALLBACK_PREFIX = "wo"
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64


def build_decision_keyboard(issue_number: int, options: list[str]) -> dict:
    """Инлайн-клавиатура решения владельца (#254): одна кнопка на вариант,
    подпись — сам текст варианта (владелец видит формулировку, не номер),
    callback_data — `wo:<issue>:<option>` (1-based номер, тот же формат
    разбирает cf-worker). Каждая кнопка — отдельная строка клавиатуры: у
    Telegram узкий экран, а вариантов решения обычно 2-4, не про экономию
    места в ряд. Вызывается из waiting_owner_guard.py (#470/#471) для задач с
    машиночитаемым блоком «## Варианты владельца»."""
    if not options:
        raise ValueError("build_decision_keyboard: нужен хотя бы один вариант")
    keyboard = []
    for i, option in enumerate(options, start=1):
        callback_data = f"{OWNER_DECISION_CALLBACK_PREFIX}:{issue_number}:{i}"
        assert len(callback_data.encode("utf-8")) <= TELEGRAM_CALLBACK_DATA_MAX_BYTES, (
            f"callback_data превышает лимит Bot API 64 байта: {callback_data!r}"
        )
        keyboard.append([{"text": option, "callback_data": callback_data}])
    return {"inline_keyboard": keyboard}


def escalate(repo: str, issue_number: int, text: str, options: list[str] | None = None) -> str:
    """Канал эскалации поломок — общий с предохранителем конвейера: комментарий
    в задачу-статус + Telegram, best-effort по каждому (см. send_telegram).
    Переиспользуется вне пары «предохранитель/пульс» (scheduler.archive_runner_sessions,
    #119/#174) — нельзя заводить второй канал для того же класса «поломка после
    мержа», один канал решения уже есть.

    options (#254) — перечисленные варианты решения владельца (из блока
    «## Варианты владельца», waiting_owner_guard.py): если заданы, Telegram-
    сообщение уходит с инлайн-кнопками (build_decision_keyboard), иначе
    поведение не меняется (обычный текстовый алерт, как раньше)."""
    try:
        post_issue_comment(repo, issue_number, text)
        posted = True
    except RuntimeError as error:
        print(f"::warning::след в #{issue_number} не оставлен: {error}", file=sys.stderr)
        posted = False
    delivered = (
        send_telegram(text, reply_markup=build_decision_keyboard(issue_number, options))
        if options else send_telegram(text)
    )
    return (f"Telegram: {'доставлен' if delivered else 'НЕ доставлен'}; "
            f"след в #{issue_number}: {'оставлен' if posted else 'НЕ оставлен'}")


# ── Сценарии, вызываемые scheduler.py ────────────────────────────────────────────


def heartbeat_check(repo: str, now: datetime) -> list[str]:
    """«Кто следит за следящим»: вызывается КАЖДЫМ запуском планировщика до всей
    остальной работы. Опоздавший запуск — единственный, кто может закричать о
    пропавших пульсах, поэтому проверка первая.

    Выборка идёт по `orchestra_tick_runs` — серверный фильтр `?event=...` на
    оба легитимных события (`schedule`, `workflow_dispatch`), не клиентская
    фильтрация одной сырой страницы: `per_page=100` на КАЖДЫЙ запрос не
    тратится на `pull_request`-прогоны `contract`, так что труncация страницы
    настоящими тиками (найдено ревью PR #318) больше не молчит."""
    runs = orchestra_tick_runs(repo, per_page=100)
    last_ok = next((r for r in runs if r.get("conclusion") == "success"), None)
    if last_ok is None:
        # Пустой результат ПОСЛЕ серверного фильтра — не «выборка коротка»
        # (см. docstring выше), а «настоящих тиков не найдено вовсе» за 100
        # последних прогонов каждого легитимного события: сам по себе редкий
        # и тревожный случай (workflow мог быть отключён GitHub'ом после 60
        # дней простоя, docs/research/21), поэтому кричим, а не молчим ℹ️.
        text = (f"🚨 edge-harness: {HEARTBEAT_NO_TICKS_MARKER}\n"
                f"Успешных прогонов {ORCHESTRA_WORKFLOW} (schedule/workflow_dispatch) "
                "не найдено за последние 100 прогонов каждого события — пульс не "
                "подтверждён, возможен отключённый workflow (docs/research/21).")
        delivered = send_telegram(text)
        # posted/attempted — находка AI-ревью PR #318, третий раунд: строка
        # отчёта раньше безусловно утверждала «след в #120», даже если
        # post_issue_comment упал (RuntimeError уходил только в warning) —
        # именно в сценарии «тиков нет» канал задачи может быть сломан по
        # той же причине, что и пульс, поэтому отчёт не может тут врать.
        attempted = False
        posted = False
        try:
            # Двухмаркерный приём (episode_reopened, находка ревью PR #318, п.1):
            # без этого первое же «тиков нет» глушило бы канал комментария
            # навсегда — issue_marker_times ищет подстроку по ВСЕЙ истории #120.
            open_times = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_NO_TICKS_MARKER)
            close_times = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_TICKS_RESUMED_MARKER)
            if episode_reopened(open_times, close_times):
                attempted = True
                post_issue_comment(repo, WATCHDOG_ISSUE, text)
                posted = True
        except RuntimeError as error:
            print(f"::warning::след в #{WATCHDOG_ISSUE} не оставлен: {error}", file=sys.stderr)
        trace = "оставлен" if posted else ("НЕ оставлен" if attempted else "не требовался — эпизод не новый")
        return [f"🚨 успешных прогонов {ORCHESTRA_WORKFLOW} (schedule/workflow_dispatch) "
                f"не найдено (Telegram: {'доставлен' if delivered else 'НЕ доставлен'}; "
                f"след в #{WATCHDOG_ISSUE}: {trace})"]
    try:
        # Эпизод HEARTBEAT_NO_TICKS закрывается явно, как только тики снова
        # нашлись: без этого закрывающего маркера episode_reopened никогда не
        # увидит момент восстановления и следующий «тиков нет» останется
        # заглушен первым же старым маркером (тот же класс, что фикс выше).
        open_times = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_NO_TICKS_MARKER)
        if open_times:
            close_times = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_TICKS_RESUMED_MARKER)
            if not close_times or max(open_times) > max(close_times):
                post_issue_comment(
                    repo, WATCHDOG_ISSUE,
                    f"✅ edge-harness: {HEARTBEAT_TICKS_RESUMED_MARKER}\n"
                    f"Успешный прогон {ORCHESTRA_WORKFLOW} снова найден — эпизод "
                    "«тиков нет» закрыт.",
                )
    except RuntimeError as error:
        print(f"::warning::закрытие эпизода в #{WATCHDOG_ISSUE} не оставлено: {error}", file=sys.stderr)
    age = heartbeat_age_minutes(last_ok["created_at"], now)
    if decide_heartbeat(last_ok["created_at"], now) == "ok":
        return [f"💗 пульс orchestra в норме: последний успех {int(age)} мин назад "
                f"(порог {HEARTBEAT_MAX_AGE_MINUTES})"]
    text = heartbeat_alert_text(age, last_ok)
    delivered = send_telegram(text)
    # posted/attempted — тот же класс, что и в ветке HEARTBEAT_NO_TICKS выше
    # (находка AI-ревью PR #318, третий раунд): безусловное «след в #120» в
    # тексте отчёта было неверно, если post_issue_comment упал.
    attempted = False
    posted = False
    try:
        # Telegram — на каждый опоздавший запуск; след в задаче — один на эпизод:
        # новый комментарий только если прежний маркер старше последнего успеха
        # (пульсы успели восстановиться и снова пропали).
        markers = issue_marker_times(repo, WATCHDOG_ISSUE, HEARTBEAT_MARKER)
        if pause_notification_pending(markers, parse_time(last_ok["created_at"])):
            attempted = True
            post_issue_comment(repo, WATCHDOG_ISSUE, text)
            posted = True
    except RuntimeError as error:
        print(f"::warning::след в #{WATCHDOG_ISSUE} не оставлен: {error}", file=sys.stderr)
    trace = "оставлен" if posted else ("НЕ оставлен" if attempted else "не требовался — эпизод не новый")
    return [f"🚨 пульс orchestra пропадал: последний успех {int(age)} мин назад "
            f"> {HEARTBEAT_MAX_AGE_MINUTES} (Telegram: "
            f"{'доставлен' if delivered else 'НЕ доставлен'}; след в #{WATCHDOG_ISSUE}: {trace})"]


def conveyor_gate(repo: str, now: datetime) -> tuple[list[str], list[str], bool]:
    """Предохранитель: перед dispatch воркера. Возвращает (наблюдения,
    действия, разрешён_ли_диспетч) — разведено по #456: «диспатч разрешён» и
    «пауза уже объявлена» ничего не меняют на сервере и раньше делали
    conveyor_lines непустым буквально на КАЖДОМ прогоне (closed — обычное
    состояние здорового конвейера), из-за чего main() красноречиво врал
    «### Действия» там, где планировщик ничего не сделал. Действие — только
    там, где эта функция реально пишет (post_issue_comment/send_telegram):
    первая тревога серии (state=="first") и пробный диспатч (state=="probe").
    Три состояния (#205):

    closed — failures < порога, диспатч обычный, тихо;
    probe  — серия красная, выдержка с последнего маркера серии истекла —
             РОВНО ОДИН пробный диспатч в этом пульсе, маркер пробы ставится
             сразу (следующий пульс уже видит его и не пробует повторно, даже
             если этот прогон worker.yml ещё не успеет завершиться);
    open   — серия красная, выдержка не истекла — диспатч заблокирован, тихо
             (маркер уже стоит, второй раз не оповещаем).

    Сброс серии без пробы (#220): success-маркер RESUME_MARKER в #120 (его
    ставит scheduler.after_merge для мержа, чинившего причину) работает как
    виртуальный success — series_anchor берёт его вместо последнего зелёного
    прогона, и красные прогоны/маркеры старше якоря в решении не участвуют.

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
        all_markers = issue_markers_any(repo, WATCHDOG_ISSUE, (PAUSE_MARKER, RESUME_MARKER))
    except RuntimeError as error:
        if decide_dispatch(failures):
            return ([f"🟢 серия красных worker.yml: {failures} "
                     f"(порог {WORKER_FAILURE_PAUSE_AFTER}) — диспатч разрешён"], [],
                    True)
        # не смогли прочитать маркеры — не гадаем о выдержке, диспатч не даём
        # (fail loud: серия красная, значит по умолчанию заперто). Ничего не
        # писали на сервер — тоже наблюдение, не действие.
        print(f"::warning::маркеры #{WATCHDOG_ISSUE} не прочитаны: {error}", file=sys.stderr)
        return ([f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
                 f"подряд — диспатч остановлен (маркеры #{WATCHDOG_ISSUE} недоступны)"], [],
                False)
    # #220: success-маркер возобновления — виртуальный success серии. Якорь —
    # что новее, последний success или сброс мержем; красные прогоны и маркеры
    # серий старше якоря объяснены и в решении не участвуют — иначе сброс
    # мержем не снял бы ни счётчик, ни выдержку/номер попытки прошлой серии.
    resume_at = max((t for t, body in all_markers if RESUME_MARKER in body), default=None)
    anchor = series_anchor(last_ok_at, resume_at)
    failures = count_consecutive_failures(
        [r.get("conclusion") for r in runs_after(runs, anchor)])
    # Маркеры прошлой серии (старше якоря: success или сброс мержем) не в счёт —
    # иначе новая серия унаследует чужой номер попытки и выдержку с первого же
    # пульса. Маркеры возобновления выпадают сами: resume_at — наибольший из
    # них, якорь не бывает его старше, поэтому «строже якоря» не бывает.
    markers = [(t, body) for t, body in all_markers if anchor is None or t > anchor]
    if not markers and decide_dispatch(failures):
        allowed_line = (f"🟢 серия красных worker.yml: {failures} "
                        f"(порог {WORKER_FAILURE_PAUSE_AFTER}) — диспатч разрешён")
        if resume_at is not None and (last_ok_at is None or resume_at > last_ok_at):
            # сброс именно мержем, а не зелёным прогоном — назови причину
            allowed_line += " (серия сброшена мержем, см. #120)"
        return ([allowed_line], [], True)
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
        # Уже оповещено раньше этим же прогоном серии — второй раз ничего не
        # пишем, значит это наблюдение, не действие.
        return ([f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
                 f"подряд — диспатч остановлен (уже оповещено, см. #{WATCHDOG_ISSUE})"], [],
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
        return ([], [f"🔎 пробный диспатч после паузы {int(backoff)} мин (попытка {attempt}) — "
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
    return ([], [f"🚨 конвейер на паузе: {failures} красных прогонов {WORKER_WORKFLOW} "
             f"подряд — диспатч остановлен (Telegram: "
             f"{'доставлен' if delivered else 'НЕ доставлен'}; сигнал в #{WATCHDOG_ISSUE})"],
            False)


def open_ci_failure_fingerprints(repo: str) -> set[str]:
    """Отпечатки, уже заведённые задачами с меткой FAILURE_WATCH_LABEL —
    дедуп «дефект этого класса уже в пуле» без второго обхода Issues.

    Листает страницы сама (класс #308, тот же приём, что all_issue_comments
    выше): список НЕ ограничен по природе — долгоживущие незакрытые дефекты
    CI со временем накопятся так же, как обычный пул задач, а сырая первая
    страница молча потеряла бы хвост — та же ошибка дедупа, что и без него,
    просто отложенная во времени."""
    page = 1
    found: set[str] = set()
    marker = "<!-- failure-fingerprint: "
    while True:
        chunk = gh(
            f"repos/{repo}/issues?state=open&labels={FAILURE_WATCH_LABEL}"
            f"&per_page=100&page={page}"
        ) or []
        if not isinstance(chunk, list) or not chunk:
            break
        for issue in chunk:
            body = issue.get("body") or ""
            idx = body.find(marker)
            if idx == -1:
                continue
            rest = body[idx + len(marker):]
            found.add(rest.split(" ", 1)[0].split("-->", 1)[0].strip())
        if len(chunk) < 100:
            break
        page += 1
    return found


def failure_watch_task_body(workflow: str, job_name: str, fact: str, run_url: str, fingerprint: str) -> str:
    """Тело авто-заведённой задачи — тот же формат, что шаблон «📋 Задача в
    пул» (Цель/Критерий/Площадь), плюс отпечаток класса HTML-комментарием:
    open_ci_failure_fingerprints ищет именно эту строку, не парсит прозу."""
    return (
        f"## Цель\n"
        f"`{workflow}` (job «{job_name}») перестаёт падать этой причиной.\n\n"
        f"## Критерий готовности\n"
        f"Следующий прогон `{workflow}` на этом коде зелёный, либо причина "
        "документированно устранена в другом месте (тогда — закрыть со ссылкой).\n\n"
        f"## Площадь\n"
        "area:orchestra\n\n"
        f"## Контекст и ссылки\n"
        f"Живой прогон: {run_url}\n"
        f"Факт: {fact}\n\n"
        f"<!-- failure-fingerprint: {fingerprint} -->\n"
    )


def failure_watch(repo: str, now: datetime) -> tuple[list[str], list[str]]:
    """Провалы ключевых workflow (#477): дешёвый опрос `status=failure` (одна
    страница малого `per_page` на workflow, без выгрузки логов всех прогонов
    подряд — квота API дорога, см. rate_guard.py) по каждому
    WATCHED_WORKFLOWS. Для самого свежего провала — по каждому упавшему job'у
    (до FAILURE_WATCH_MAX_JOBS_PER_RUN): последняя содержательная строка
    `##[error]` из лога (факт, не гипотеза), классификация
    (classify_failure_cause) и дедуп по failure_fingerprint (класс причины,
    НЕ run id — один сигнал на класс):

    'stale_base' — газ уже назван в другом месте (#474, task-branch/pre-commit
                   сами просят git rebase) — только наблюдение, без задачи и
                   без следа в #120 (не дублировать уже объявленный газ);
    'infra'      — известная сигнатура лимита/сети — лечится ожиданием: один
                   тихий след в #120 на класс (issue_marker_times — тот же
                   приём, что у остальных маркеров серий), Telegram не шлём —
                   это не тревога, требующая действия человека;
    'defect'     — наш дефект: задача в пул (label task + FAILURE_WATCH_LABEL)
                   с конкретным фактом, если такого класса ещё нет среди
                   открытых issues с меткой FAILURE_WATCH_LABEL.

    Прогоны `event=pull_request` не разбираются (находка ревью PR #488,
    раунд 2): красный прогон PR-события — это красный обязательный чек на PR,
    его устойчивую причину уже заводит автодетектор простоя (stall_detector,
    #201, отпечаток `check:red:<имя>`); вторая задача с меткой ci-failure на
    тот же дефект — ровно тот спам, который запрещает критерий #477. Вариант
    «перед заведением сверяться и с открытыми auto-detected» не выбран:
    отпечатки двух детекторов несопоставимы (хэш против slug), а после
    исключения PR-событий классы двух путей не пересекаются.

    Строка ошибки не прочиталась — задача НЕ заводится (находка ревью PR #488,
    чеклист): фолбэк-факт «шаги: …» грубее отпечатка с настоящей строкой,
    задача по нему мигает во вторую, когда лог на следующем пульсе
    прочитается. Критерий #477 требует точную причину — остаётся громкое
    наблюдение (⚠️); устойчивый случай подберёт автодетектор #201 (класс
    warn:), факт не теряется — ссылка на прогон в строке наблюдения."""
    observations: list[str] = []
    actions: list[str] = []
    ci_fingerprints: set[str] | None = None  # ленивая инициализация — только если дошли до дефекта

    for workflow in WATCHED_WORKFLOWS:
        try:
            payload = gh(
                f"repos/{repo}/actions/workflows/{workflow}/runs?status=failure&per_page=20"
            ) or {}
        except RuntimeError as error:
            observations.append(f"⚠️ failure-watch {workflow}: список провалов не прочитан ({error})")
            continue
        runs = payload.get("workflow_runs", [])
        # Класс «красный прогон PR-события» уже ведёт check:red-путь #201 (см.
        # докстринг) — вычитается ДО окна свежести. Фильтр клиентский, не
        # серверный `event=`: у API нет «не равно», а перебор легитимных
        # событий — по запросу на каждое событие каждого workflow против одной
        # страницы здесь. per_page=20 — запас, чтобы PR-прогоны не вытеснили
        # свежий провал основного события за страницу: PR-триггер среди
        # отслеживаемых есть только у orchestra.yml (job contract), двадцати
        # красных contract-прогонов за окно свежести не бывает; потеря хвоста
        # за страницей стоит не дороже окна ниже — вне окна разбор и так
        # не идёт.
        runs = [r for r in runs if r.get("event") != "pull_request"]
        # Окно свежести (находка ревью PR #488): провал старше окна уже не
        # актуален — задачу на него заводить поздно и незачем, `now` не
        # декорация. Без фильтра `runs[0]` навсегда остаётся тем же старым
        # красным прогоном после закрытия задачи по нему.
        fresh_cutoff = now - timedelta(minutes=FAILURE_WATCH_WINDOW_MINUTES)
        runs = [r for r in runs if parse_time(r["created_at"]) >= fresh_cutoff]
        if not runs:
            continue
        run_item = runs[0]  # самый свежий провал этого workflow в пределах окна
        bad_jobs = failing_jobs(repo, run_item)
        if not bad_jobs:
            observations.append(
                f"⚠️ failure-watch {workflow}: прогон {run_item.get('html_url')} красный, "
                "упавший job не найден (детали недоступны)")
            continue
        run_url = run_item.get("html_url", "")
        # Каждый упавший job — отдельный кандидат класса: разные job'ы одного
        # прогона падают по разным причинам, и раньше разбирался только
        # bad_jobs[0] (находка ревью PR #488, чеклист). Лог job'а — дорогой
        # запрос, поэтому потолок FAILURE_WATCH_MAX_JOBS_PER_RUN; хвост НЕ
        # прячется — назван поимённо в наблюдении после цикла.
        for job in bad_jobs[:FAILURE_WATCH_MAX_JOBS_PER_RUN]:
            error_line = last_error_log_line(repo, job["id"])
            job_name = job.get("name", "?")
            step_names = ", ".join(
                step["name"] for step in job.get("steps", [])
                if step.get("conclusion") in FAILURE_CONCLUSIONS
            ) or "нет (упал до шагов)"
            if error_line is None:
                observations.append(
                    f"⚠️ failure-watch {workflow} (job «{job_name}»): прогон {run_url} красный, "
                    f"строка ##[error] недоступна (шаги: {step_names}) — задачу не "
                    "заводим без факта, устойчивый случай возьмёт автодетектор (#201, warn:)")
                continue
            fact = error_line
            cause = classify_failure_cause(f"{error_line} {step_names}")
            fingerprint = failure_fingerprint(workflow, job_name, fact)

            if cause == "stale_base":
                observations.append(
                    f"⏭️ failure-watch {workflow} (job «{job_name}»): устаревшая база "
                    f"(класс {fingerprint}) — газ уже назван в #474/task-branch "
                    "(git rebase), задачу не дублируем")
                continue

            if cause == "infra":
                marker = f"{FAILURE_WATCH_INFRA_MARKER} {fingerprint}]"
                try:
                    already = bool(issue_marker_times(repo, WATCHDOG_ISSUE, marker))
                except RuntimeError as error:
                    observations.append(
                        f"⚠️ failure-watch {workflow} (job «{job_name}»): маркеры "
                        f"#{WATCHDOG_ISSUE} не прочитаны ({error})")
                    continue
                if already:
                    observations.append(
                        f"🕓 failure-watch {workflow} (job «{job_name}»): инфраструктурная "
                        f"причина, класс {fingerprint} (уже сигналили) — молча ждём")
                    continue
                text = (
                    f"🕓 edge-harness: {marker}\n"
                    f"{workflow} (job «{job_name}») падает по инфраструктурной причине, "
                    "не наш дефект — лечится ожиданием, задача в пул не заводится.\n"
                    f"Факт: {fact}\n"
                    f"{run_url}"
                )
                # posted — находка ревью PR #488 (раунд 2; тот же класс, что
                # heartbeat_check после PR #318): упавший пост уходит в
                # warning, и отчёт не вправе утверждать «след оставлен».
                posted = True
                try:
                    post_issue_comment(repo, WATCHDOG_ISSUE, text)
                except RuntimeError as error:
                    posted = False
                    print(f"::warning::след в #{WATCHDOG_ISSUE} не оставлен: {error}", file=sys.stderr)
                observations.append(
                    f"🕓 failure-watch {workflow} (job «{job_name}»): инфраструктурная "
                    f"причина, класс {fingerprint} — след в #{WATCHDOG_ISSUE} "
                    f"{'оставлен' if posted else 'НЕ оставлен'}, диспатч не трогаем")
                continue

            # cause == "defect": заводим задачу, если класса ещё нет в пуле.
            if ci_fingerprints is None:
                try:
                    ci_fingerprints = open_ci_failure_fingerprints(repo)
                except RuntimeError as error:
                    observations.append(
                        f"⚠️ failure-watch {workflow}: список задач {FAILURE_WATCH_LABEL} не прочитан ({error})")
                    continue
            if fingerprint in ci_fingerprints:
                observations.append(
                    f"🔁 failure-watch {workflow} (job «{job_name}»): дефект класса "
                    f"{fingerprint} уже в пуле — не дублируем")
                continue
            title = f"CI: {workflow} падает — {job_name}"
            body = failure_watch_task_body(workflow, job_name, fact, run_url, fingerprint)
            try:
                gh("-X", "POST", f"repos/{repo}/issues",
                   "-f", "title=" + title, "-f", "body=" + body,
                   "-f", "labels[]=task", "-f", "labels[]=" + FAILURE_WATCH_LABEL)
            except RuntimeError as error:
                observations.append(
                    f"⚠️ failure-watch {workflow} (job «{job_name}»): задача по дефекту не заведена ({error})")
                continue
            ci_fingerprints.add(fingerprint)
            actions.append(
                f"🛠️ failure-watch {workflow}: заведена задача по дефекту "
                f"(job «{job_name}», класс {fingerprint}) — {fact}")
        hidden = bad_jobs[FAILURE_WATCH_MAX_JOBS_PER_RUN:]
        if hidden:
            observations.append(
                f"⚠️ failure-watch {workflow}: ещё {len(hidden)} упавших job'ов не разобраны "
                f"(потолок {FAILURE_WATCH_MAX_JOBS_PER_RUN} логов на прогон): "
                + ", ".join(f"«{j.get('name', '?')}»" for j in hidden))

    return observations, actions
