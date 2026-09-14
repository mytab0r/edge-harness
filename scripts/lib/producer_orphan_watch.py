#!/usr/bin/env python3
"""Производитель задач пула без измеримого потребителя (issue #1277).

## Класс дефекта

Репозиторий несёт девять программных производителей задач пула, все — через
одну точку `scripts/lib/pool_issue.py::create_pool_issue`:
`scripts/review/file_tasks.py` (review-findings), `scripts/orchestra/
pulse_guard.py::failure_watch` (failure-watch), `stall_detector.py`
(stall-detector), `dependabot_alert_watch.py` (dependabot-alert-watch),
`upstream_drift.py` (upstream-drift), `health_audit.py` (health-audit),
`merge_health_watch.py` (merge-health-watch), `scheduler.py::after_merge`
(checklist-tail — хвост чеклиста ревью), `scheduler.py::
replace_closed_task_prs` (task-replacement). Ни один из девяти кранов не
несёт счётчика на выходе: живая улика (замер 2026-09-14/15,
`gh api graphql`, репозиторий mytab0r/edge-harness, поиск
`"Хвост чеклиста ревью PR" in:title`) — 112 открытых задач одного
производителя (checklist-tail), 0 когда-либо закрытых, 0 когда-либо взятых
в работу, старейшая висит 8 суток. Ускорялось: 8 задач за предыдущую
неделю, до 23 в сутки на пике. Ни один инвариант репозитория этого не
увидел — восемь суток нашёл человек, не система.

Этот модуль — чистая (без сети) логика классификации и вердикта; сетевой
фетч и wiring в общий отчёт — `scripts/orchestra/repo_invariants.py`
(инвариант 23).

## Требование 1: признак производителя — машинный, переживающий переименование

Заголовок и метка — самый дешёвый признак производителя, и самый хрупкий:
живой замер на дату внедрения показал, что минимум ДВА производителя
(review-findings, checklist-tail) несут только `labels=["task"]` без единой
отличительной метки, и ещё ДВА (stall-detector, task-replacement) делят
одну и ту же метку `auto-detected`. Различить их по заголовку/метке
постфактум для review-findings и checklist-tail сегодня физически нельзя —
у review-findings заголовок и тело пишет модель ревью, произвольны по
конструкции.

Решение: `pool_issue.create_pool_issue` (issue #1277) теперь принимает
обязательный `producer` и проставляет его скрытым HTML-комментарием первой
строкой тела (`pool_issue.producer_marker`/`extract_producer`) — единая
точка создания делает пропуск этого признака невозможным для СЛЕДУЮЩЕГО,
десятого производителя (тот же приём, что REQUIRED_LABEL уже применяет к
`task`).

Честная граница, что ломает этот признак (обязана быть названа, не
скрыта — AGENTS.md):
  - issues, заведённые ДО этого PR, маркера не несут — `_legacy_classify`
    ниже (title/label-эвристика) — единственный путь их разглядеть, и он
    ЗАМОРОЖЕН по конструкции (не эволюционирует для новых производителей,
    только чтобы не терять историю в обратном прогоне и в переходный
    период сразу после внедрения маркера);
  - review-findings НЕ распознаётся даже `_legacy_classify` — заголовок и
    тело задачи пишет модель AI-ревью, отличить их от произвольной ручной
    задачи со схожей меткой `task` эвристикой нельзя. Это не пробел
    реализации, а сам факт, который требование 1 просило обнаружить.
  - ручная правка тела issue, стирающая HTML-комментарий, лишает эту
    ОДНУ issue признака — не защищено технически.

## Требование 2: пороги — из замера, не из головы

Тот же замер (2026-09-15, полный дамп `label:task` — 828 issue, GraphQL,
классификация ниже) по каждому из девяти:

    producer                  total closed close%  oldest_open_d
    review-findings              —      —    —      — (не измерим legacy-классификатором, см. выше)
    failure-watch                32     30   94%     7
    stall-detector                39     37   95%     1
    dependabot-alert-watch          1      0    0%     2
    upstream-drift                 3      3  100%     —
    health-audit                    0      0   n/a     — (ни разу не сработал)
    merge-health-watch              0      0   n/a     — (ни разу не сработал)
    checklist-tail                112      0    0%     8   <- цель замера
    task-replacement                2      1   50%     8

`MIN_SAMPLE_SIZE = 10` — с запасом ниже самого малого «здорового» объёма
(32, failure-watch) и выше самого большого «молодого» (3, upstream-drift):
разделяет производителей с реальной историей от тех, что либо только
начали работать, либо структурно редки.

`DEAD_CLOSE_RATE_MAX = 0.10` (10%) — с запасом ниже пола здоровых
производителей (94–100%) и выше потолка мёртвого (0%, checklist-tail,
N=112 — не случайность малой выборки).

`DEFAULT_MIN_OLDEST_OPEN_HOURS = 24` — переиспользует `scheduler.
STALE_HOURS` по значению (24ч, порог «нет исполнителя сутки», #427) — не
второй порог правды на «сколько это разумно висит нетронутым» рядом с уже
существующим; передаётся аргументом, чтобы этот модуль не тянул тяжёлый
`scheduler.py` только ради одной константы.

Обратный прогон на исторических данных (тот же датасет, скользящее окно 30
дней, симуляция по дням от первой заведённой issue checklist-tail): с
этими порогами инвариант сработал бы 2026-09-07 (21 задача в окне, 0
закрыто, старейшая открытая — 26.3ч) — на СЛЕДУЮЩИЙ день после появления
первой issue этого производителя, за 7 суток до того, как долг обнаружил
человек (2026-09-14).

## Требование 3: третье состояние (issue #1096, носитель check_result.py)

`evaluate_producer` возвращает `check_result.CheckResult` НА КАЖДОГО
производителя отдельно — «производитель молодой, данных мало»
(`total < MIN_SAMPLE_SIZE` → `unknown()`) НЕ схлопывается ни в «здоров»
(`ok()`), ни в «производит мусор» (`violation()`). Отдельно: производитель
с достаточным объёмом, но чья САМАЯ СВЕЖАЯ открытая задача моложе
`min_oldest_open_hours` — тоже `unknown()`, а не преждевременный вердикт
«мёртв»: единственная открытая задача, которой минуты от роду, ещё не
успела быть взятой никем по естественной причине, не по дефекту
производителя.

## Требование 4: честная граница — что решает машина, что человек

Эта проверка отвечает РОВНО на один факт: «у производителя X за окно N
задач, закрыто M, доля Z% — ниже порога». Она НЕ решает, что с этим
делать (остановить производителя, поднять приоритет находок, признать
журналом вместо задач пула, как уже сделано для самого checklist-tail в
#1262/#1268) — это суждение остаётся человеку/следующей задаче, названо
здесь явно, не подделывается под автоматическое решение (AGENTS.md,
«Решение — это механизм, а не текст» — здесь наоборот: решение НЕ
механизм, и это тоже нужно сказать вслух).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import re
from datetime import datetime
from typing import NamedTuple

_PI_SPEC = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parent / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

_CR_SPEC = importlib.util.spec_from_file_location(
    "check_result", Path(__file__).resolve().parent / "check_result.py")
check_result = importlib.util.module_from_spec(_CR_SPEC)
_CR_SPEC.loader.exec_module(check_result)  # type: ignore[union-attr]


# ── Реестр производителей (issue #1277) ─────────────────────────────────────
# Канонический список id, ровно те же строки, что передаются в
# `pool_issue.create_pool_issue(..., producer=<id>)` на дату внедрения
# (2026-09-15). Порядок — порядок появления в живом замере, не значим.
PRODUCER_IDS: tuple[str, ...] = (
    "review-findings",
    "failure-watch",
    "stall-detector",
    "dependabot-alert-watch",
    "upstream-drift",
    "health-audit",
    "merge-health-watch",
    "checklist-tail",
    "task-replacement",
)

# Скользящее окно измерения (issue #1277, требование текста задачи —
# «за скользящее окно»). 30 дней — с запасом больше суточного пика
# притока (16-23/сутки на checklist-tail) и достаточно для набора
# MIN_SAMPLE_SIZE даже у умеренно активных производителей (failure-watch/
# stall-detector дают 32/39 за всю историю репозитория, младше 30 дней).
WINDOW_DAYS = 30

# Пороги — см. докстринг модуля, «Требование 2», обоснование числами.
MIN_SAMPLE_SIZE = 10
DEAD_CLOSE_RATE_MAX = 0.10
DEFAULT_MIN_OLDEST_OPEN_HOURS = 24.0


# ── Legacy-классификатор (ТОЛЬКО для issues без маркера, issue #1277) ───────
# Заморожен по конструкции: новый производитель обязан проставлять
# `producer` через `create_pool_issue` (иначе RuntimeError ДО сети) — сюда
# новые записи не добавляются, это костыль для истории до маркера, не
# второй параллельный механизм классификации.
_TAIL_TITLE_RE = re.compile(r"^Хвост чеклиста ревью PR #\d+$")
_STALL_TITLE_RE = re.compile(r"^Простой конвейера: ")
_REPLACE_TITLE_RE = re.compile(r"^Докрытие #\d+: ")
_UPSTREAM_TITLE_RE = re.compile(r"^dsh-edge: пин апстрима отстаёт от ")
_MERGE_HEALTH_TITLE_RE = re.compile(r"^Регрессия worker_success_rate после слияния ")
_HEALTH_AUDIT_TITLE_RE = re.compile(r"^Регрессия здоровья конвейера: ")


def _issue_labels(issue: dict) -> set[str]:
    raw = issue.get("labels")
    if isinstance(raw, dict):  # GraphQL: {"nodes": [{"name": ...}, ...]}
        raw = raw.get("nodes") or []
    names: set[str] = set()
    for label in raw or []:
        if isinstance(label, dict):
            names.add(label.get("name", ""))
        else:
            names.add(str(label))
    return names


def _legacy_classify(issue: dict) -> str | None:
    """Title/label-эвристика, ТОЛЬКО для issue без маркера тела (заведены до
    issue #1277). Порядок веток значим: `_TAIL_TITLE_RE`/`_REPLACE_TITLE_RE`
    проверяются раньше меток, т.к. заголовок здесь специфичнее самой метки
    (`auto-detected` делят два разных производителя — checklist-tail этой
    меткой не пользуется вовсе, но task-replacement и stall-detector
    неразличимы БЕЗ заголовка)."""
    title = issue.get("title") or ""
    labels = _issue_labels(issue)
    if _TAIL_TITLE_RE.match(title):
        return "checklist-tail"
    if _REPLACE_TITLE_RE.match(title):
        return "task-replacement"
    if _STALL_TITLE_RE.match(title):
        return "stall-detector"
    if "dependabot-alert" in labels:
        return "dependabot-alert-watch"
    if _HEALTH_AUDIT_TITLE_RE.match(title) or "self-audit" in labels:
        return "health-audit"
    if _MERGE_HEALTH_TITLE_RE.match(title):
        return "merge-health-watch"
    if "ci-failure" in labels:
        return "failure-watch"
    if _UPSTREAM_TITLE_RE.match(title):
        return "upstream-drift"
    return None
    # review-findings НЕ распознаётся здесь намеренно — см. докстринг модуля,
    # «Требование 1»: заголовок и тело пишет модель ревью, отличить от
    # ручной задачи с той же меткой `task` эвристикой нельзя.


def classify_producer(issue: dict) -> str | None:
    """Единая точка классификации issue → id производителя. Маркер тела —
    ПЕРВЫЙ и единственный признак, устойчивый к переименованию (issue
    #1277); `_legacy_classify` — только запасной путь для issue без маркера
    (заведены до этого PR). `None` — issue не опознана ни одним из девяти
    производителей (ручная задача, находка ревью до маркера, площадка для
    любого будущего производителя, который забудет передать `producer` —
    что структурно невозможно после этого PR, `create_pool_issue` откажет
    раньше сети)."""
    marker = pool_issue.extract_producer(issue.get("body"))
    if marker:
        return marker
    return _legacy_classify(issue)


class ProducerStats(NamedTuple):
    producer: str
    total: int
    closed: int
    open_count: int
    ever_taken: int
    oldest_open_hours: float | None


def _is_closed(issue: dict) -> bool:
    state = (issue.get("state") or "").upper()
    return state == "CLOSED"


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _created_at(issue: dict) -> datetime:
    value = issue.get("created_at") or issue.get("createdAt")
    return _parse_iso(value)


def _ever_assigned(issue: dict) -> bool:
    """True, если issue хоть раз несла назначенного — REST-форма отдаёт
    `assignees` списком объектов, GraphQL/Search — тоже списком (Search API,
    используемый фетчем инварианта 23, идентичен REST-issue схеме); дополнительно
    принимается GraphQL `{"totalCount": N}` (использован в замере issue
    #1277) для совместимости с уже собранными офлайн-датасетами."""
    assignees = issue.get("assignees")
    if isinstance(assignees, list):
        return len(assignees) > 0
    if isinstance(assignees, dict):
        return (assignees.get("totalCount") or 0) > 0
    return False


def compute_stats(issues: list[dict], now: datetime) -> dict[str, ProducerStats]:
    """Чистая функция (без сети): группирует уже загруженные issues
    (прод-форма REST/Search API — number/title/body/state/created_at/
    closed_at/labels/assignees) по `classify_producer`, считает метрики по
    КАЖДОМУ из `PRODUCER_IDS` — производитель без единой issue за окно
    ВСЁ РАВНО попадает в результат с total=0 (health-audit/merge-health-watch
    на дату замера, «ни разу не сработал» — не «нет данных о нём вообще»,
    см. `evaluate_producer`). Issues, не опознанные `classify_producer`
    (ручные задачи, review-findings до маркера), в статистику не входят —
    это измерение девяти конкретных кранов, не всего пула."""
    buckets: dict[str, list[dict]] = {pid: [] for pid in PRODUCER_IDS}
    for issue in issues:
        producer = classify_producer(issue)
        if producer is None or producer not in buckets:
            continue
        buckets[producer].append(issue)

    stats: dict[str, ProducerStats] = {}
    for producer, items in buckets.items():
        total = len(items)
        closed = sum(1 for i in items if _is_closed(i))
        open_items = [i for i in items if not _is_closed(i)]
        ever_taken = sum(1 for i in items if _is_closed(i) or _ever_assigned(i))
        oldest_open_hours = None
        if open_items:
            oldest_created = min(_created_at(i) for i in open_items)
            oldest_open_hours = (now - oldest_created).total_seconds() / 3600
        stats[producer] = ProducerStats(
            producer=producer, total=total, closed=closed,
            open_count=len(open_items), ever_taken=ever_taken,
            oldest_open_hours=oldest_open_hours,
        )
    return stats


def evaluate_producer(
    stats: ProducerStats, *,
    min_sample: int = MIN_SAMPLE_SIZE,
    dead_close_rate_max: float = DEAD_CLOSE_RATE_MAX,
    min_oldest_open_hours: float = DEFAULT_MIN_OLDEST_OPEN_HOURS,
) -> check_result.CheckResult:
    """Вердикт ОДНОГО производителя, три исхода (issue #1096/#1277,
    «Требование 3»):

    - `unknown()` — данных недостаточно (total < min_sample): производитель
      либо ни разу не сработал (total=0, health-audit/merge-health-watch на
      дату внедрения), либо слишком молод, чтобы отличить «дырявый» от
      «просто повезло с малой выборкой».
    - `unknown()` — объёма достаточно, доля закрытия ниже порога, но
      старейшая открытая задача моложе `min_oldest_open_hours`: рано считать
      застоем, ей ещё не дали времени быть взятой (не индульгенция навечно —
      следующий прогон пересчитает заново, окно скользит).
    - `violation([...])` — объём выше порога, доля закрытия на/ниже порога,
      и достаточно времени прошло: ровно тот факт, что улика issue #1277
      (checklist-tail, 112/0/8 суток) демонстрирует.
    - `ok()` — доля закрытия выше порога."""
    if stats.total < min_sample:
        return check_result.unknown(
            f"{stats.producer}: {stats.total} задач за окно {WINDOW_DAYS} дн. "
            f"(порог {min_sample}) — недостаточно данных для вердикта")
    close_rate = stats.closed / stats.total
    if close_rate > dead_close_rate_max:
        return check_result.ok()
    if stats.oldest_open_hours is None:
        # close_rate <= порог, но открытых задач нет вовсе — при total>=min_sample
        # это математически невозможно (closed<total обязано означать open>0),
        # честно fail loud, а не молчаливый ok()/unknown() по недосмотру формы.
        raise RuntimeError(
            f"{stats.producer}: close_rate={close_rate:.0%} <= порога, но "
            "oldest_open_hours=None при total>=min_sample — данные "
            "противоречивы (closed < total обязано означать открытые есть)")
    if stats.oldest_open_hours < min_oldest_open_hours:
        return check_result.unknown(
            f"{stats.producer}: доля закрытия {close_rate:.0%} на/ниже порога "
            f"({dead_close_rate_max:.0%}), но старейшая открытая задача моложе "
            f"{min_oldest_open_hours:.0f}ч — рано считать застоем")
    return check_result.violation([{
        "producer": stats.producer,
        "total": stats.total,
        "closed": stats.closed,
        "close_rate": close_rate,
        "open_count": stats.open_count,
        "ever_taken": stats.ever_taken,
        "oldest_open_hours": stats.oldest_open_hours,
    }])


def evaluate_all(
    stats_by_producer: dict[str, ProducerStats], **kwargs,
) -> dict[str, check_result.CheckResult]:
    """Вердикт по каждому производителю из `stats_by_producer` (обычно —
    результат `compute_stats`, уже несёт все `PRODUCER_IDS`, включая
    total=0). `**kwargs` — пороги, форвардятся в `evaluate_producer`
    (нужно тестам мутации: подмена порога должна красить/гасить конкретный
    производитель без правки самого модуля)."""
    return {p: evaluate_producer(s, **kwargs) for p, s in stats_by_producer.items()}
