#!/usr/bin/env python3
"""Детектор устойчивого простоя конвейера → задача пула (#201).

Класс проблемы: оркестратор уже умеет НАЗВАТЬ причину простоя — печатает
её строкой в отчёт (`GITHUB_STEP_SUMMARY`) каждый пульс, — но эта строка там
и умирает. Каждый НОВЫЙ вид поломки требует человека, который прочитает лог,
поймёт класс и заведёт задачу вручную (замер: 2026-09-02/03 около пятнадцати
дефектов конвейера, ни один не породил автоматической задачи, issue #201).

Этот модуль читает уже готовый отчёт пульса (тот же список строк, что
scheduler.main() и так печатает) и превращает УСТОЙЧИВУЮ причину простоя с
НЕЗНАКОМЫМ отпечатком в задачу пула с уликами. Он не пересчитывает состояние
GitHub заново и не трогает scheduler.py дальше одной вставки в main() —
одна ответственность, минимальное пересечение с параллельно работающими
агентами (см. AGENTS.md этой задачи).

## Отпечаток

Строится из СТРУКТУРИРОВАННЫХ данных, уже посчитанных остальным кодом
оркестратора и просто напечатанных в отчёт (имя красного обязательного
чека, факт «нет PR к сроку», факт «нет вердикта AI после исчерпанных
повторов», факт паузы предохранителя), а не из свободного пересказа:

  - `check:red:<имя>`      — красный обязательный чек с этим именем
                             (scheduler.merge_queue/unhealthy_pulls, строка
                             содержит «красные проверки: …»).
  - `gate:no-ai-verdict`   — PR держит review:ok без вердикта AI дольше
                             порога, и авто-повторы (#196) уже исчерпаны.
  - `worker:no-pr`         — задача просрочена (reap_stale): воркер её взял,
                             но не открыл PR за STALE_HOURS.
  - `gate:pipeline-paused` — предохранитель (pulse_guard) остановил диспатч
                             воркера сериями красных worker.yml.
  - `archive:session-failed` / `archive:morde-unreachable` — архив сессии
                             раннера после мержа сломан (#119/#174).
  - `warn:<нормализованный текст>` — общий случай: любая другая строка-
                             предупреждение (⚠️/🚨) отчёта, которую не
                             покрыл ни один специфичный разбор выше. Текст
                             нормализуется (числа/номера → `N`), чтобы одно и
                             то же предупреждение с разными PR/run-номерами
                             не плодило разные отпечатки.

Каждый Signal несёт дословную строку отчёта как улику — «нет цитаты из лога
— нет задачи» выполняется по построению: Signal не создаётся без исходной
строки.

## Предохранители

  1. Дедупликация — по отпечатку, не по похожести заголовка (см. issue #201,
     второй комментарий: искать по машиночитаемому ключу, не текстом).
     `find_open_task` ищет среди ОТКРЫТЫХ задач с меткой `auto-detected`
     машиночитаемую строку `Отпечаток: `<fp>`` в теле. Нашли — комментарий с
     новой уликой, новая задача не создаётся НИКОГДА при живом дубликате.
     Комментарий пишется, только если улика ИЗМЕНИЛАСЬ: тот же приём, что
     ESCALATION_MARKER (issue_marker_times по маркеру с хэшем улики) — иначе
     хронический простой того же отпечатка пишет одинаковый комментарий
     каждый пульс (до 96 в сутки при интервале 15 мин), и новая улика тонет
     в потоке повторов (находка AI-ревью PR #248).
  2. Устойчивость — отпечаток обязан продержаться STALL_PERSIST_MINUTES:
     первое наблюдение только оставляет след-маркер в WATCHDOG_ISSUE
     (переиспользуем канал pulse_guard, тот же приём, что PAUSE_MARKER),
     задача заводится только когда маркер того же отпечатка уже старше
     порога. Разовый блип не плодит задачу. Маркер живёт в WATCHDOG_ISSUE
     вечно, поэтому счётчик обязан сбрасываться по факту закрытия прошлой
     автозадачи с тем же отпечатком (`_closed_task_reset_times`) — иначе
     блип того же отпечатка после решения находит старый маркер и заводит
     вторую задачу мгновенно, без устойчивости в новом эпизоде (находка
     AI-ревью PR #248).
  3. Суточный потолок — STALL_DAILY_CAP новых автозадач; превышение не
     тонет молча (строка отчёта + один раз в сутки — комментарий в
     WATCHDOG_ISSUE и Telegram, CAP_EXHAUSTED_MARKER, #610), но и НЕ красит
     прогон: исчерпание потолка — сигнал «нужен человек», не отказ пульса
     (инвариант «наблюдатель провалов не реагирует на свою инфраструктуру
     мониторинга» — иначе failure_watch завёл бы задачу «CI: orchestra.yml
     падает» на эту же эскалацию, замкнутый цикл, живой случай
     #578/#580/#589/#592/#598).
  4. Метка `auto-detected` — на каждой заведённой задаче (плюс обычная
     `task`, чтобы воркер мог её взять). Строка реестра — docs/agents/LABELS.md
     (#207).
  5. Эскалация владельцу (escalate_stale_auto_tasks) — автозадача не
     закрытая дольше ESCALATE_AFTER_HOURS уходит тем же каналом, что
     pulse_guard.escalate (issue-комментарий + Telegram), текст обязан
     заканчиваться разделом «что дальше» (#170).
  6. Груминг перед эскалацией (groom_auto_tasks, #830) — вызывается между
     detect_and_act (выше) и escalate_stale_auto_tasks (п.5 выше) на том же
     снимке отчёта пульса, закрывает машинно-бесспорное ДО того, как задача
     доживёт до порога эскалации: (а) отпечаток стоп-задачи устойчиво не
     воспроизводится (симметрично STALL_PERSIST_MINUTES появления, см. её
     докстринг); (б) дубль по отпечатку среди открытых `auto-detected`
     (защита от исторических/ручных случаев — `find_open_task` и так не
     даёт создать дубль при заведении). Задачи с назначенным исполнителем не
     трогает. Это ТОЛЬКО детерминированный груминг — судейские решения
     (устарела ли по существу, семантический дубль, приоритет) — этап 2,
     отдельная задача (см. openspec/changes/pm-pool-groom-deterministic/).

## Честный потолок

Детектор ТОЛЬКО превращает устойчивый простой в задачу пула. Чинит воркер —
агент, который может не справиться (для этого и есть эскалация выше). Если
сломан сам детектор или оркестратор — самолечения здесь нет и быть не
может: этот модуль исполняется ВНУТРИ того же пульса orchestra, который он
проверяет. Мёртвый пульс закрывает только внешний сторож (#194) и heartbeat
(#120, pulse_guard.heartbeat_check).

Пороги — константы здесь и только здесь (одно место правды ДЛЯ ЭТОГО
детектора; пороги предохранителя конвейера и петли #196 остаются в
pulse_guard.py, как и были, — сюда не дублируются, а импортируются функции
работы с ними: gh, escalate, issue_marker_times, post_issue_comment).

Запуск тестов: python -m pytest scripts/orchestra/test_stall_detector.py -q
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from pulse_guard import (
    WATCHDOG_ISSUE,
    escalate,
    gh,
    issue_marker_times,
    minutes_between,
    parse_time,
    post_issue_comment,
)

# list_pages — обход страниц GitHub API (класс #308), одно место правды в
# scripts/lib/review_labels.py (тот же приём, что scheduler.py/repo_invariants.py).
_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

# Единственное место правды на заведение issue пула (#526): без него labels
# без `task` собирались бы копией той же проверки, что уже есть у
# file_tasks.py/scheduler.py/upstream_drift.py — вместо этого все четверо
# зовут одну функцию.
_PI_SPEC = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parents[1] / "lib" / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

# ── Пороги (одно место правды этого детектора) ────────────────────────────

# Отпечаток должен продержаться дольше этого с момента первого наблюдения
# (маркер в WATCHDOG_ISSUE), прежде чем заведётся задача. Пульс orchestra —
# каждые 15 минут (cron orchestra.yml, тот же интервал, что у pulse_guard);
# порог кратен интервалу, чтобы задача не заводилась раньше следующего пульса.
STALL_PERSIST_MINUTES = 30

# Потолок автозаведённых задач в сутки — без него незнакомый, но безобидный
# отпечаток (например, флапающий тест) мог бы жечь пул задачами. Превышение
# не тонет молча (см. detect_and_act).
STALL_DAILY_CAP = 5

# Автозадача, не закрытая дольше этого — эскалация владельцу (см. модульный
# docstring, «Честный потолок»): воркеры сами не справились.
ESCALATE_AFTER_HOURS = 48

# Груминг (#830, предохранитель 6 модульного docstring): отпечаток обязан
# ОТСУТСТВОВАТЬ этот срок подряд, прежде чем задача закроется автоматически —
# то же значение, что STALL_PERSIST_MINUTES появления: порог, доказывающий
# появление отпечатка, симметрично доказывает и его исчезновение (одно место
# правды для обоих направлений одного и того же вопроса «отпечаток жив?»).
RESOLVE_QUIET_MINUTES = STALL_PERSIST_MINUTES

TASK_LABEL = "task"
# Метка происхождения — строка реестра docs/agents/LABELS.md (#207).
AUTO_LABEL = "auto-detected"

SIGNAL_MARKER_PREFIX = "[симптом:"
ESCALATION_MARKER = "[симптом: эскалация владельцу]"

# Эскалация исчерпания суточного потолка (#610): «нужен человек» — не отказ
# пульса (главный doctring модуля, предохранитель #3), поэтому НЕ красит
# прогон — красит только реальный сбой самого детектора (RuntimeError в
# detect_and_act/escalate_stale_auto_tasks, см. scheduler.main). Но и молчать
# нельзя: строка в GITHUB_STEP_SUMMARY видна только тому, кто зашёл в Actions.
# Дедуп — один раз в календарные сутки (маркер несёт ISO-дату), не на каждый
# непринятый отпечаток и не на каждый пульс — иначе несколько отпечатков,
# упёршихся в один и тот же исчерпанный потолок за один день, шлют по
# эскалации на каждый, хотя факт один («потолок сегодня исчерпан»).
CAP_EXHAUSTED_MARKER = "[симптом: потолок автозаведения исчерпан"


# ── Извлечение отпечатков из уже готового отчёта пульса ───────────────────

_RED_CHECKS_RE = re.compile(r"красные проверки: ([^)\n]+)")
_NO_VERDICT_RE = re.compile(r"без вердикта AI.*не дёргаю снова")
_STALE_RE = re.compile(r"♻️ #\d+ просрочена")
_PAUSE_RE = re.compile(r"конвейер на паузе")
_ARCHIVE_FAIL_RE = re.compile(r"не заархивирована \(возможность сломана\)")
_MORDE_UNREACHABLE_RE = re.compile(r"недоступна для архива сессий")


class Signal(NamedTuple):
    fingerprint: str
    evidence: str  # дословная строка отчёта — обязательная цитата (см. docstring)


def _slug(name: str) -> str:
    token = re.sub(r"[^a-z0-9а-яё]+", "-", name.strip().lower())
    return token.strip("-") or "unknown"


def _normalize_warn(text: str) -> str:
    """Нормализация текста предупреждения для warn:<…>: номера (#N, голые
    числа) выкидываются, чтобы одно и то же предупреждение с разными
    PR/run-номерами не плодило разные отпечатки — только СУТЬ строки."""
    text = re.sub(r"^[^\wа-яёА-ЯЁ]+", "", text.strip())  # ведущий эмодзи
    text = re.sub(r"#\d+", "#N", text)
    text = re.sub(r"\d+", "N", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text[:120]


def extract_signals(lines: list[str]) -> list[Signal]:
    """Разбирает готовый отчёт пульса (список строк) на отпечатки. Порядок
    проверок — от специфичного к общему: специфичный разбор снимает строку
    с рассмотрения (`continue`), общий `warn:`/`crit:` ловит остальное."""
    signals: list[Signal] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        red_checks = _RED_CHECKS_RE.search(stripped)
        if red_checks:
            for name in red_checks.group(1).split(","):
                name = name.strip()
                if name:
                    signals.append(Signal(f"check:red:{_slug(name)}", stripped))
            continue
        if _NO_VERDICT_RE.search(stripped):
            signals.append(Signal("gate:no-ai-verdict", stripped))
            continue
        if _STALE_RE.search(stripped):
            signals.append(Signal("worker:no-pr", stripped))
            continue
        if _ARCHIVE_FAIL_RE.search(stripped):
            signals.append(Signal("archive:session-failed", stripped))
            continue
        if _MORDE_UNREACHABLE_RE.search(stripped):
            signals.append(Signal("archive:morde-unreachable", stripped))
            continue
        if _PAUSE_RE.search(stripped):
            signals.append(Signal("gate:pipeline-paused", stripped))
            continue
        if stripped.startswith("⚠️") or stripped.startswith("🚨"):
            signals.append(Signal(f"warn:{_normalize_warn(stripped)}", stripped))
    return signals


def group_by_fingerprint(signals: list[Signal]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for signal in signals:
        grouped.setdefault(signal.fingerprint, []).append(signal.evidence)
    return grouped


# ── Дедупликация: открытая задача с тем же отпечатком уже есть? ───────────


def _fingerprint_line(fingerprint: str) -> str:
    return f"Отпечаток: `{fingerprint}`"


def _evidence_marker(fingerprint: str, evidence_text: str) -> str:
    """Маркер конкретной улики для уже открытой задачи — тот же приём, что
    ESCALATION_MARKER (issue_marker_times по маркеру в теле комментария).
    Хэш улики (не сама улика: маркер должен остаться коротким и стабильным)
    отличает «та же улика опять» от «улика изменилась» — комментарий с
    новой уликой пишется только во втором случае (см. предохранитель 1)."""
    digest = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()[:12]
    return f"[симптом-улика: {fingerprint}:{digest}]"


def open_auto_tasks(repo: str) -> list[dict]:
    """Постранично (review_labels.list_pages, класс #308) — сырой
    одностраничный вызов молча терял бы автозадачи за первой сотней открытых
    issues с меткой auto-detected (находка гвардии test_pagination_guard.py)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={review_labels.label_query_value(AUTO_LABEL)}"
        "&per_page=100", gh)
    return [issue for issue in issues if "pull_request" not in issue]


def find_open_task(repo: str, fingerprint: str, issues: list[dict] | None = None) -> dict | None:
    issues = open_auto_tasks(repo) if issues is None else issues
    marker = _fingerprint_line(fingerprint)
    for issue in issues:
        if marker in (issue.get("body") or ""):
            return issue
    return None


# ── Создание задачи ────────────────────────────────────────────────────────


def _render_body(fingerprint: str, evidence: list[str], run_url: str | None) -> str:
    lines = [
        "Автоматически заведено детектором простоя (#201): с момента первого "
        f"наблюдения этого эпизода прошло больше {STALL_PERSIST_MINUTES} мин, "
        "и отпечаток не совпал ни с одной уже открытой автозадачей. Честно: "
        "это НЕ гарантия «на каждом пульсе подряд» — детектор видит только "
        "отчёт СВОЕГО прогона, не историю между прогонами (находка AI-ревью "
        "PR #248); единичный сайтинг эпизода, случившийся давно и без "
        "закрытой автозадачи по тому же отпечатку с тех пор, тоже пройдёт "
        "этот порог.",
        "",
        _fingerprint_line(fingerprint),
        "",
        "## Улики",
        "",
    ]
    lines += [f"- `{item}`" for item in evidence]
    if run_url:
        lines.append(f"- прогон: {run_url}")
    lines += [
        "",
        "## Критерий готовности",
        "",
        "Отпечаток больше не встречается в отчётах оркестратора (проверяется "
        "живым прогоном `orchestra`, не фактом закрытия issue) — либо причина "
        "устранена по существу, если отпечаток относится к целому классу "
        "(например `check:red:<имя>` — сама проверка снова зелёная).",
        "",
        "## Честный потолок",
        "",
        "Задачу завёл автодетектор (#201): он умеет только заметить устойчивую "
        "причину простоя и превратить её в работу пула. Решает воркер — агент, "
        "который может не справиться; если задача провисит дольше "
        f"{ESCALATE_AFTER_HOURS} ч, сработает отдельная эскалация владельцу.",
    ]
    return "\n".join(lines)


def create_task(repo: str, fingerprint: str, evidence: list[str], run_url: str | None) -> int:
    body = _render_body(fingerprint, evidence, run_url)
    title = f"Простой конвейера: {fingerprint}"
    result = pool_issue.create_pool_issue(gh, repo, title, body, [TASK_LABEL, AUTO_LABEL])
    return result["number"]


def auto_tasks_created_since(repo: str, since: datetime) -> int:
    """Все issues (открытые и закрытые) с меткой auto-detected, созданные не
    раньше `since` — суточный потолок считается по факту создания, не по
    текущей открытости (закрытая сегодня автозадача всё равно сожгла квоту).
    Постранично (review_labels.list_pages, класс #308) — сырой одностраничный
    вызов молча занижал бы потолок после сотни автозадач за всё время."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all&labels={review_labels.label_query_value(AUTO_LABEL)}"
        "&per_page=100", gh)
    return sum(
        1 for issue in issues
        if "pull_request" not in issue and parse_time(issue["created_at"]) >= since
    )


# ── Устойчивость: маркер первого наблюдения в WATCHDOG_ISSUE ──────────────


def _sighting_marker(fingerprint: str) -> str:
    return f"{SIGNAL_MARKER_PREFIX} {fingerprint}]"


_FINGERPRINT_BODY_RE = re.compile(r"Отпечаток: `([^`]+)`")


def _closed_task_reset_times(repo: str) -> dict[str, datetime]:
    """Отпечаток → время закрытия последней автозадачи с этим отпечатком.

    Точка сброса счётчика устойчивости (находка AI-ревью PR #248, обход
    предохранителя). Маркер первого наблюдения в WATCHDOG_ISSUE никогда не
    удаляется — живёт там вечно. Без точки сброса один блип того же
    отпечатка ПОСЛЕ того, как предыдущая автозадача по нему уже закрыта,
    находит тот старый маркер: `min(seen)` возвращает многодневную давность,
    `age >= STALL_PERSIST_MINUTES` истинно немедленно, и задача заводится по
    одному блипу, не продержавшись ни минуты в ЭТОМ эпизоде. Сайтинги
    старше момента закрытия своей задачи не считаются в счёт нового эпизода
    — `detect_and_act` отфильтровывает их до вычисления возраста. Постранично
    (review_labels.list_pages, класс #308) — сырой одностраничный вызов молча
    терял бы точки сброса за первой сотней закрытых автозадач."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=closed&labels={review_labels.label_query_value(AUTO_LABEL)}"
        "&per_page=100", gh)
    resets: dict[str, datetime] = {}
    for issue in issues:
        if "pull_request" in issue or not issue.get("closed_at"):
            continue
        match = _FINGERPRINT_BODY_RE.search(issue.get("body") or "")
        if not match:
            continue
        fp = match.group(1)
        closed_at = parse_time(issue["closed_at"])
        if fp not in resets or closed_at > resets[fp]:
            resets[fp] = closed_at
    return resets


def detect_and_act(repo: str, now: datetime, lines: list[str], run_url: str | None = None) -> list[str]:
    """Вызывается КАЖДЫМ пульсом оркестратора с уже готовым отчётом (тем же
    списком строк, что печатается в GITHUB_STEP_SUMMARY). Пустой вход
    (здоровый конвейер) — пустой выход и НИ ОДНОГО сетевого вызова: холостой
    ход проверяется мутацией в test_stall_detector.py."""
    signals = extract_signals(lines)
    if not signals:
        return []

    report: list[str] = []
    grouped = group_by_fingerprint(signals)
    open_auto = open_auto_tasks(repo)
    created_today: int | None = None  # считаем лениво — только если понадобится создание
    reset_times: dict[str, datetime] | None = None  # тоже лениво — только если есть что фильтровать
    cap_escalated_today: bool | None = None  # лениво — только при первом исчерпании потолка

    for fingerprint, evidence in grouped.items():
        existing = find_open_task(repo, fingerprint, open_auto)
        if existing:
            evidence_text = evidence[-1]
            marker = _evidence_marker(fingerprint, evidence_text)
            if issue_marker_times(repo, existing["number"], marker):
                # Та же улика уже прокомментирована — повтор не пишем (класс
                # находки AI-ревью PR #248: хронический простой не спамит).
                report.append(f"📎 #{existing['number']}: улика по {fingerprint} не изменилась — молчу")
                continue
            post_issue_comment(
                repo, existing["number"],
                f"{marker}\nНовая улика по тому же отпечатку `{fingerprint}`:\n\n`{evidence_text}`"
                + (f"\n\nПрогон: {run_url}" if run_url else ""),
            )
            report.append(f"📎 #{existing['number']}: новая улика по {fingerprint} (задача уже открыта)")
            continue

        marker = _sighting_marker(fingerprint)
        seen = issue_marker_times(repo, WATCHDOG_ISSUE, marker)
        if seen:
            if reset_times is None:
                reset_times = _closed_task_reset_times(repo)
            boundary = reset_times.get(fingerprint)
            if boundary:
                # Сайтинги до закрытия своей же прошлой задачи — эпизод уже
                # решён, в счёт устойчивости НОВОГО эпизода не идут.
                seen = [t for t in seen if t > boundary]
        if not seen:
            post_issue_comment(
                repo, WATCHDOG_ISSUE,
                f"👀 {marker}\nВпервые замечен отпечаток `{fingerprint}`:\n\n`{evidence[-1]}`\n\n"
                f"Продержится дольше {STALL_PERSIST_MINUTES} мин — заведётся задача пула.",
            )
            report.append(f"👀 новый отпечаток {fingerprint} замечен впервые — жду устойчивости")
            continue

        age = minutes_between(min(seen), now)
        if age < STALL_PERSIST_MINUTES:
            report.append(f"👀 отпечаток {fingerprint} держится {int(age)} мин (< {STALL_PERSIST_MINUTES}) — жду")
            continue

        if created_today is None:
            created_today = auto_tasks_created_since(repo, now - timedelta(hours=24))
        if created_today >= STALL_DAILY_CAP:
            report.append(
                f"🚨 потолок автозаведённых задач в сутки исчерпан ({created_today}/{STALL_DAILY_CAP}) "
                f"— отпечаток {fingerprint} НЕ заведён, нужен человек"
            )
            # Видимый сигнал (#610): «нужен человек» тонуло в GITHUB_STEP_SUMMARY
            # — владелец не увидит без захода в Actions. НЕ поднимаем
            # hard-failure и не красим прогон (см. докстринг CAP_EXHAUSTED_MARKER
            # выше, инвариант «наблюдатель провалов не реагирует на свою
            # инфраструктуру мониторинга»: если бы этот сигнал красил прогон,
            # failure_watch (тот же пульс наблюдает orchestra.yml) завёл бы
            # задачу «CI: orchestra.yml падает» НА ЭТУ ЖЕ эскалацию — замкнутый
            # цикл, тот самый живой случай #578/#580/#589/#592/#598, который
            # #610 закрывает). Дедуп — один раз в календарные сутки, не на
            # каждый непринятый отпечаток этого же пульса (cap_escalated_today
            # кэширует решение на весь этот вызов, второй проверки маркера
            # в этом же цикле не заводим).
            if cap_escalated_today is None:
                cap_marker = f"{CAP_EXHAUSTED_MARKER} {now.date().isoformat()}]"
                try:
                    cap_escalated_today = bool(issue_marker_times(repo, WATCHDOG_ISSUE, cap_marker))
                except RuntimeError as error:
                    report.append(f"⚠️ эскалация потолка не проверена (маркеры #{WATCHDOG_ISSUE} недоступны): {error}")
                    cap_escalated_today = True  # не гадаем повторно на этом же пульсе
                else:
                    if not cap_escalated_today:
                        text = (
                            f"🚨 edge-harness: {cap_marker}\n"
                            f"Суточный потолок автозаведения ({STALL_DAILY_CAP}) исчерпан — "
                            f"новый отпечаток `{fingerprint}` НЕ заведён задачей, нужен человек.\n\n"
                            "Это НЕ отказ пульса: детектор простоя жив и работает, просто "
                            "накопилось больше устойчивых причин, чем безопасно заводить "
                            "автоматически за сутки — прогон нарочно НЕ покрашен красным "
                            "(инвариант «наблюдатель провалов не реагирует на свою "
                            "инфраструктуру мониторинга», #610).\n\n"
                            "Что дальше: посмотреть раздел «Детектор простоя» отчёта этого "
                            "прогона, решить руками по каждому непринятому отпечатку — "
                            "приоритизировать, завести задачу вручную (scripts/gh/issue-create) "
                            "или поднять STALL_DAILY_CAP, если объём временный."
                        )
                        result = escalate(repo, WATCHDOG_ISSUE, text)
                        report.append(f"🚨 эскалация потолка автозаведения ({result})")
                        cap_escalated_today = True
            continue

        number = create_task(repo, fingerprint, evidence, run_url)
        created_today += 1
        report.append(f"🆕 задача #{number} заведена автодетектором по отпечатку {fingerprint}")

    return report


# ── Эскалация: автозадача висит дольше второго порога ─────────────────────


def escalate_stale_auto_tasks(repo: str, now: datetime) -> list[str]:
    """Предохранитель #5 (см. docstring модуля): автозадача, не закрытая
    дольше ESCALATE_AFTER_HOURS, эскалируется владельцу через тот же канал,
    что pulse_guard (комментарий + Telegram) — ровно один раз (маркер на
    самой задаче, тот же приём, что маркеры серий pulse_guard)."""
    report: list[str] = []
    for issue in open_auto_tasks(repo):
        number = issue["number"]
        age_hours = minutes_between(parse_time(issue["created_at"]), now) / 60
        if age_hours < ESCALATE_AFTER_HOURS:
            continue
        if issue_marker_times(repo, number, ESCALATION_MARKER):
            continue  # уже эскалирована — не дублируем
        text = (
            f"🚨 edge-harness: {ESCALATION_MARKER}\n"
            f"Задача #{number} заведена автодетектором простоя (#201) "
            f"{int(age_hours)} ч назад и всё ещё открыта (порог {ESCALATE_AFTER_HOURS} ч) — "
            "автоматика довела дело до задачи пула, дальше её решает воркер, и, "
            "похоже, не справился или не взялся.\n\n"
            "Что дальше: задача остаётся в пуле с меткой `task`, любой воркер "
            "может взять её через assign — само по себе это не произойдёт "
            f"быстрее. Нужно участие владельца: посмотреть задачу #{number}, при "
            "необходимости приоритизировать её или решить руками."
        )
        result = escalate(repo, number, text)
        report.append(f"🚨 #{number}: эскалация по затянувшейся автозадаче ({result})")
    return report


# ── Груминг перед эскалацией (#830) ────────────────────────────────────────
#
# Детерминированный, без LLM, вызывается КАЖДЫМ пульсом между detect_and_act
# (выше) и escalate_stale_auto_tasks (выше) — закрывает машинно-бесспорное,
# чтобы стоп-задача, чья причина уже исчезла, не доживала до эскалации
# владельцу (#427, живой триггер #677 — владелец: «я просил PM, который
# будет сам это делать»). Судейские решения (устарела ли по существу,
# семантический дубль, приоритет) — этап 2, отдельная задача (proposal.md
# openspec/changes/pm-pool-groom-deterministic/).


def _silence_marker(fingerprint: str) -> str:
    return f"[симптом-тишина: {fingerprint}]"


def _revival_marker(fingerprint: str) -> str:
    return f"[симптом-снова-жив: {fingerprint}]"


def _silence_episode_start(repo: str, fingerprint: str) -> datetime | None:
    """Момент начала ТЕКУЩЕГО эпизода тишины отпечатка, или None, если
    эпизода сейчас нет (тишина ещё не замечена ни разу, либо последний
    переходный маркер — «снова жив»). Симметрично `_closed_task_reset_times`
    (сброс устойчивости появления, #248): самый свежий из двух переходных
    маркеров решает, идёт ли эпизод СЕЙЧАС — а не факт, что маркер тишины
    вообще когда-то был."""
    silence = issue_marker_times(repo, WATCHDOG_ISSUE, _silence_marker(fingerprint))
    if not silence:
        return None
    revival = issue_marker_times(repo, WATCHDOG_ISSUE, _revival_marker(fingerprint))
    last_silence = max(silence)
    if revival and max(revival) > last_silence:
        return None
    return last_silence


def groom_auto_tasks(repo: str, now: datetime, lines: list[str]) -> list[str]:
    """PM-груминг пула автозадач ДО эскалации владельцу (#830) — см. блок
    комментариев выше и docstring модуля, предохранитель 6.

    (а) Отпечаток стоп-задачи, отсутствующий в `extract_signals(lines)` ЭТОГО
    пульса, запускает/продолжает эпизод тишины (переходные маркеры на
    WATCHDOG_ISSUE, тот же канал, что `_sighting_marker`). «Отпечаток жив
    сейчас» — ВСЕГДА прямой пересчёт этого пульса, никогда производная от
    того, постился ли комментарий-улика в detect_and_act (тот антиспам-канал
    молчит на идентичной улике, #248, и потому непригоден как признак
    «жив», см. proposal.md «Отвергнутые варианты»). Эпизод, продержавшийся
    RESOLVE_QUIET_MINUTES без единого повторного появления, — задача
    закрывается с уликой-комментарием.

    (б) Два и более открытых `auto-detected` с ОДНИМ отпечатком (защита от
    исторических/ручных дублей — `find_open_task` и так не даёт создать
    дубль при заведении) — оставляет открытой задачу с наименьшим номером,
    остальные закрывает как дубликат.

    Задачи с назначенным исполнителем (`assignees` не пуст) груминг не
    трогает вовсе (ни (а), ни (б)) — воркер уже взял её в работу.

    Задачи без метки `auto-detected` физически не видны этой функции —
    единственный источник, `open_auto_tasks`, читает только эту метку."""
    report: list[str] = []
    active = {s.fingerprint for s in extract_signals(lines)}
    open_tasks = open_auto_tasks(repo)

    # (б) дубликаты по отпечатку — раньше (а), чтобы не гонять логику
    # тишины на задаче, которая всё равно закрывается сейчас как дубликат.
    by_fingerprint: dict[str, list[dict]] = {}
    for issue in open_tasks:
        match = _FINGERPRINT_BODY_RE.search(issue.get("body") or "")
        if match:
            by_fingerprint.setdefault(match.group(1), []).append(issue)
    closed_numbers: set[int] = set()
    for fingerprint, issues in by_fingerprint.items():
        if len(issues) < 2:
            continue
        survivor, *duplicates = sorted(issues, key=lambda i: i["number"])
        for dup in duplicates:
            if dup.get("assignees"):
                report.append(f"👤 #{dup['number']}: дубль по {fingerprint}, но уже в работе — не трогаю")
                continue
            text = (
                f"🧹 PM-груминг (#830): дубликат отпечатка `{fingerprint}` — уже открыта "
                f"#{survivor['number']} с тем же отпечатком. Закрываю как дубликат, дальнейшие "
                f"улики копятся там."
            )
            try:
                post_issue_comment(repo, dup["number"], text)
                gh("-X", "PATCH", f"repos/{repo}/issues/{dup['number']}", "-f", "state=closed")
            except RuntimeError as error:
                report.append(f"⚠️ #{dup['number']}: дубль не закрыт грумом — {error}")
                continue
            closed_numbers.add(dup["number"])
            report.append(f"🧹 #{dup['number']}: закрыт грумом как дубликат #{survivor['number']}")

    # (а) отпечаток больше не воспроизводится
    for issue in open_tasks:
        number = issue["number"]
        if number in closed_numbers or issue.get("assignees"):
            continue
        match = _FINGERPRINT_BODY_RE.search(issue.get("body") or "")
        if not match:
            continue
        fingerprint = match.group(1)

        if fingerprint in active:
            if _silence_episode_start(repo, fingerprint) is not None:
                post_issue_comment(
                    repo, WATCHDOG_ISSUE,
                    f"👀 {_revival_marker(fingerprint)}\nОтпечаток `{fingerprint}` снова "
                    f"наблюдается (задача #{number}) — эпизод тишины прерван.",
                )
                report.append(f"👀 отпечаток {fingerprint} снова жив — эпизод тишины прерван")
            continue

        start = _silence_episode_start(repo, fingerprint)
        if start is None:
            post_issue_comment(
                repo, WATCHDOG_ISSUE,
                f"🤫 {_silence_marker(fingerprint)}\nОтпечаток `{fingerprint}` впервые не "
                f"воспроизведён этим пульсом (задача #{number}). Продержится тишина дольше "
                f"{RESOLVE_QUIET_MINUTES} мин — закрою автогрумом.",
            )
            report.append(f"🤫 отпечаток {fingerprint} впервые тих — жду устойчивости тишины")
            continue

        quiet_minutes = minutes_between(start, now)
        if quiet_minutes < RESOLVE_QUIET_MINUTES:
            report.append(
                f"🤫 отпечаток {fingerprint} тих {int(quiet_minutes)} мин "
                f"(< {RESOLVE_QUIET_MINUTES}) — жду"
            )
            continue

        text = (
            f"🧹 PM-груминг (#830): отпечаток `{fingerprint}` не воспроизводится "
            f"{int(quiet_minutes)} мин подряд (порог {RESOLVE_QUIET_MINUTES}) — причина "
            "простоя устранена. Закрываю автоматически (детерминированный груминг перед "
            "эскалацией владельцу)."
        )
        try:
            post_issue_comment(repo, number, text)
            gh("-X", "PATCH", f"repos/{repo}/issues/{number}", "-f", "state=closed")
        except RuntimeError as error:
            report.append(f"⚠️ #{number}: не закрыт грумом — {error}")
            continue
        report.append(
            f"✅ #{number}: закрыт грумом — отпечаток {fingerprint} не воспроизводится "
            f"{int(quiet_minutes)} мин"
        )

    return report
