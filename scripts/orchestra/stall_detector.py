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
     тонет молча, а кричит строкой отчёта («потолок исчерпан»).
  4. Метка `auto-detected` — на каждой заведённой задаче (плюс обычная
     `task`, чтобы воркер мог её взять). Строка реестра — docs/agents/LABELS.md
     (#207).
  5. Эскалация владельцу (escalate_stale_auto_tasks) — автозадача не
     закрытая дольше ESCALATE_AFTER_HOURS уходит тем же каналом, что
     pulse_guard.escalate (issue-комментарий + Telegram), текст обязан
     заканчиваться разделом «что дальше» (#170).

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

TASK_LABEL = "task"
# Метка происхождения — строка реестра docs/agents/LABELS.md (#207).
AUTO_LABEL = "auto-detected"

SIGNAL_MARKER_PREFIX = "[симптом:"
ESCALATION_MARKER = "[симптом: эскалация владельцу]"


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
        f"repos/{repo}/issues?state=open&labels={AUTO_LABEL}&per_page=100", gh)
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
    result = gh(
        "-X", "POST", f"repos/{repo}/issues",
        "-f", f"title={title}",
        "-f", "body=" + body,
        "-f", f"labels[]={TASK_LABEL}",
        "-f", f"labels[]={AUTO_LABEL}",
    )
    return result["number"]


def auto_tasks_created_since(repo: str, since: datetime) -> int:
    """Все issues (открытые и закрытые) с меткой auto-detected, созданные не
    раньше `since` — суточный потолок считается по факту создания, не по
    текущей открытости (закрытая сегодня автозадача всё равно сожгла квоту).
    Постранично (review_labels.list_pages, класс #308) — сырой одностраничный
    вызов молча занижал бы потолок после сотни автозадач за всё время."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all&labels={AUTO_LABEL}&per_page=100", gh)
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
        f"repos/{repo}/issues?state=closed&labels={AUTO_LABEL}&per_page=100", gh)
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
