#!/usr/bin/env python3
"""Детектор регрессии здоровья конвейера, привязанный к слиянию — часы, не
сутки (issue #967, живой корень #878/#937).

## Ревизия 2 — критик нашёл ложную мотивацию и нерабочие пороги, обе правки ниже

**Мотивирующее утверждение было ложным.** Первая версия этого докстринга
утверждала «`pulse_guard` не поймал регрессию, потому что успехи изредка
вклинивались и не давали счётчику подряд идущих отказов взвестись». Это
неверно — проверено по журналу #120: предохранитель (`WORKER_FAILURE_PAUSE_
AFTER=3`) СРАБОТАЛ на третьем подряд отказе (03:09:34Z = мерж+4ч15м, РАНЬШЕ
детектора этого модуля) и был трижды погашен `RESUME_MARKER`
(`scheduler.resume_series_by_merge`) в ту же ночь: 01:41:37Z (мерж #862,
задача #860), 03:37:34Z (мерж #409, задача #389), 04:26:52Z (мерж #344,
задача #341). `resume_series_by_merge` УЖЕ проверяет совпадение задачи
(`run_claimed_task`) — это не «сброс чужим PR», предохранитель работал
СТРОГО по своему контракту.

Настоящая причина, по которой предохранитель не удержал паузу: его контракт
— «слияние задачи, над которой работал последний красный прогон, с высокой
вероятностью чинит ПРИЧИНУ серии» — верен для типичного случая «задача была
сложной, доделали, дальше пойдёт нормально», но СТРУКТУРНО не может увидеть
регрессию класса #878: сломан не конкретный прогон и не конкретная задача, а
САМ КРИТЕРИЙ УСПЕХА, общий для ВСЕХ задач. Каждое отдельное возобновление
(#862/#409/#344 — три РАЗНЫЕ задачи) было локально обоснованным по контракту
предохранителя: та конкретная задача действительно доехала до PR. Но
предохранитель, привязанный к задаче, в принципе не может увидеть узор
«много РАЗНЫХ задач подряд ловят один и тот же ложный провал» — это по
конструкции требует взгляда, игнорирующего границы задач, ровно то, что
считает этот модуль (агрегат по всем прогонам, не по конкретной задаче).

Открытая, не сделанная здесь работа (это НЕ территория данной задачи —
`pulse_guard.py`/`scheduler.py` заняты параллельным PR #950 на момент
написания, issue #967 называет границу явно): предохранитель мог бы сам
считать, сколько раз RESUME_MARKER сработал за короткое окно (скажем, 3
разных задачи резюмированы за 3 часа) — сама частота возобновлений МОГЛА бы
быть более узким, дешёвым и точным сигналом, чем агрегатный success-rate
этого модуля. Заведена отдельная узкая задача (issue #972) с этим
предложением — реализация вне рамок #967.

**Пороги первой версии были нерабочими — исправлено эмпирически.**
Критик прогнал реальный код на здоровом окне 2026-09-09T06:17Z..
2026-09-10T22:17Z (41 час, реальные прогоны `worker.yml`) и получил 17
ложных срабатываний из 41 (41%) — относительное отклонение recent-окна от
СВОЕГО СОСЕДНЕГО baseline тонет в шуме: baseline здорового конвейера этого
репозитория сам хронически ~48-50% (не благополучные 90%+), поэтому 3-5
подряд отказов — рутинное событие, не аномалия. Независимая проверка (issue
#967) РАСШИРИЛА вывод: свип по всему разумному пространству порогов
(observation 6-12ч, baseline 72ч, min_samples 3-10, оба порога regression/
fire 50%/100% из `health_regression`) НИ ОДНОЙ комбинации не даёт
одновременно приемлемый шум И своевременную поимку #878 — сигнал и шум
буквально неразличимы относительным отклонением на этих данных. Заявление
докстринга v1 «DRY на смысл регрессии, не копия чисел» было ложным: числа
(50%/100%) скопированы из `health_regression.py`, а СМЫСЛ (относительное
отклонение от соседнего скользящего baseline) для дневных снимков и часовых
прогонов — РАЗНЫЕ статистики с разными свойствами шума, откуda и нерабочий
результат.

Замена (проверено на реальных данных, см. `evaluate` ниже): АБСОЛЮТНЫЙ порог
recent-rate (не относительно baseline, а голое число), достаточная выборка
(`MIN_SAMPLES_RECENT`), окно `OBSERVATION_WINDOW_HOURS=8`. На независимой
кросс-проверке (119 часовых тиков, 2026-09-06..2026-09-10T22:00, ДО
инцидента) — 6 ложных срабатываний из 119 (5.0%), не 41%. `baseline` в новой
версии — ТОЛЬКО контекст в отчёте (человеку для сравнения), решение
`fire`/`ok` больше не зависит от него — попутно снимает риск
самозаглушения, который был у относительного дизайна (baseline-окно,
вбирающее сам инцидент, снижало бы наблюдаемое отклонение и давало ложный
`ok` без починки; абсолютный порог этого структурного изъяна не имеет —
`ok` теперь означает буквально «recent-rate вернулась выше порога»).

## Почему это отдельный механизм, не просто более частый снимок

`scripts/measure/pipeline_health.py::worker_success_rate` снимается РАЗ В
КАЛЕНДАРНЫЕ СУТКИ (`should_snapshot`) на COUNT-окне последних
`WORKER_SUCCESS_WINDOW` прогонов — по построению не мог бы поймать
регрессию быстрее чем на следующий день. `health_regression.py` сравнивает
СУТОЧНЫЕ снимки с суточным же baseline (`MIN_SAMPLES_FOR_BASELINE=5` снятых
ДНЕЙ) — тот же суточный шаг. Этот модуль считает TIME-BOXED success-rate
НА ЛЕТУ из уже читаемого списка прогонов (`pulse_guard.recent_runs`),
ничего не пишет на диск/git-ветку — источник (Actions API) сам по себе
всегда свежий.

## Алгоритм (эмпирический, НЕ импортирует пороги `health_regression` — v1
## делала это ошибочно, см. выше)

  recent  = success-rate прогонов `worker.yml` за последние
            `OBSERVATION_WINDOW_HOURS` (8) часов;
  fire    — recent_rate <= FIRE_RATE_THRESHOLD_PCT (10.0) И recent_n >=
            MIN_SAMPLES_RECENT (5);
  insufficient_data — recent_n < MIN_SAMPLES_RECENT;
  ok      — иначе.

Только ДВА решающих исхода (`fire`/`ok`) плюс честное `insufficient_data` —
промежуточный `regression` из v1 сознательно снят: свип показал, что более
мягкий порог (например, recent<=25%) добавляет 12 ложных из 41 (29%) без
проверенной пользы — не изобретаем точность, которой нет (см. design.md,
«Не подтверждено»).

`baseline`/`BASELINE_LOOKBACK_HOURS` вычисляются и несутся в `Verdict`
ТОЛЬКО для текста отчёта (человеку для контекста) — в решении `fire`/`ok`
не участвуют.

## Подозреваемые — честно, без угадывания одного (design.md, «Корреляция,
## не причинность»)

При `fire` называются ВСЕ PR, слитые в окне атрибуции
`[now - ATTRIBUTION_LOOKBACK_HOURS, now]` (`pipeline_health.search_merged_prs`)
— не один «самый вероятный» (на реальной ночи 2026-09-10/11 их 15, см.
`test_merge_health_watch.py`). Это ЦЕЛЕНАПРАВЛЕННО грубая сеть: попытка
исключить PR, чьё собственное окно «до» уже выглядело плохо, на разреженных
данных `worker.yml` даёт неустойчивые результаты — честно НЕ реализовано.

## Реакция — задача-расследование, не авто-revert (design.md, «Почему не
## авто-revert»)

На `fire`: (1) эскалация тем же каналом, что и остальной конвейер
(`pulse_guard.escalate`); (2) заведение/донесение улики в задачу пула с
метками `task`+`area:process`. Честно (критик, «приоритет — хвост очереди»):
тир-1 `area:process` тайбрейкается по НОМЕРУ ПО ВОЗРАСТАНИЮ
(`free_task.py::issue_priority_key`) — свежая задача получает МАКСИМАЛЬНЫЙ
номер среди тир-1, то есть встаёт в ХВОСТ тир-1-очереди (113 открытых
`area:process` на 2026-09-11), не берётся немедленно. Реальный канал
СРОЧНОСТИ — эскалация (issue #120 + Telegram, секунды), не автоматический
пикап воркером; задача пула — durable-трек для расследования, а не гарантия
скорости. Дедуп — фиксированный отпечаток (не по часу/дню, см. `_fingerprint`)
+ суточный потолок `MERGE_HEALTH_DAILY_CAP` (класс уже оплачен
`pulse_guard.FAILURE_WATCH_DAILY_CAP` — второй безлимитный источник задач
недопустим, см. design.md).

Честно (не сделано здесь, критик): `merge-health-watch.yml` сам не входит в
`pulse_guard.WATCHED_WORKFLOWS` — падение самого этого job'а не завело бы
задачу `failure_watch`'ом. Не исправлено сознательно: `pulse_guard.py`
занят параллельным PR #950 (та же причина, что у issue #972) — добавление
константы туда откладывается до слияния #950, чтобы не создавать конфликт
в чужом активном PR.

Запуск: python scripts/orchestra/merge_health_watch.py run
Тесты:  python -m pytest scripts/orchestra/test_merge_health_watch.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))  # pulse_guard сосед

import pulse_guard  # gh/recent_runs/parse_time/escalate/issue_marker_times/post_issue_comment/WATCHDOG_ISSUE

_PH_SPEC = importlib.util.spec_from_file_location(
    "pipeline_health", Path(__file__).resolve().parents[1] / "measure" / "pipeline_health.py")
pipeline_health = importlib.util.module_from_spec(_PH_SPEC)
_PH_SPEC.loader.exec_module(pipeline_health)  # type: ignore[union-attr]

_PI_SPEC = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parents[1] / "lib" / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]


# ── Пороги этого модуля (эмпирические — проверены сипом на реальной истории
# `worker.yml`, design.md §2/§4; НЕ выведены из множества независимых
# инцидентов — такой истории физически не существует, design.md «Не
# подтверждено») ────────────────────────────────────────────────────────

# Наблюдательное окно: на свипе (design.md §4) 8ч даёт лучший компромежуток
# между шумом на здоровых данных (5.0% ложных на 119 тиках) и скоростью
# поимки #878 (+7ч) среди проверенных вариантов (4/6/8/10/12ч).
OBSERVATION_WINDOW_HOURS = 8
# Абсолютный порог recent-rate — НЕ относительное отклонение (см. докстринг
# модуля, «Ревизия 2»): на свипе диапазон 0-10% дал одинаковый результат
# (FP=1/41 на контрольном окне критика, детект +7ч); порог поднят от строгих
# 0% до 10% ради небольшого запаса, не меняя свойств.
FIRE_RATE_THRESHOLD_PCT = 10.0
MIN_SAMPLES_RECENT = 5
# Baseline — ТОЛЬКО контекст отчёта, не участвует в решении fire/ok (см.
# докстринг модуля, «Ревизия 2» — самозаглушение относительного дизайна v1
# снято именно этим).
BASELINE_LOOKBACK_HOURS = 72
MIN_SAMPLES_BASELINE = 5
# Окно атрибуции подозреваемых — вдвое шире наблюдательного: слияние ПЕРЕД
# самым началом recent-окна тоже могло вызвать наблюдаемое падение.
ATTRIBUTION_LOOKBACK_HOURS = 2 * OBSERVATION_WINDOW_HOURS
# Одна страница Actions API на прогон — recent-окно (8ч) многократно
# перекрывается даже в пиковые дни этого репозитория (наблюдённый максимум
# ~90 прогонов/84ч, design.md); baseline-контекст при переполнении честно
# помечается `baseline_truncated` в Verdict, а не тихо считается по неполному
# окну (см. `evaluate`).
RUNS_FETCH_LIMIT = 100

TASK_LABEL = "task"
PROCESS_LABEL = "area:process"
# Фиксированный отпечаток (НЕ по часу/дню — см. докстринг модуля, «Ревизия
# 2», исправление блокера дедупа v1): один открытый PR-расследование на весь
# инцидент, не один на каждый час его длительности.
FINGERPRINT = "merge-health:worker_success_rate"
# Тот же класс тормоза, что `pulse_guard.FAILURE_WATCH_DAILY_CAP` — без
# потолка новый ИСТОЧНИК задач (эта область не пересекается с failure_watch/
# self-audit — свой счётчик, design.md «Пересечение с существующими
# детекторами») мог бы плодить задачи без ограничения при флаппинге между
# `fire` и `ok`.
MERGE_HEALTH_DAILY_CAP = 3


class Verdict(NamedTuple):
    status: str  # insufficient_data | ok | fire
    recent_rate: float | None
    recent_n: int
    baseline_rate: float | None  # контекст отчёта, не решение (см. докстринг)
    baseline_n: int
    baseline_truncated: bool
    now: str
    reason: str = ""


# ── Чистая логика (тестируется без сети) ───────────────────────────────────


def _concluded(runs: list[dict]) -> list[dict]:
    return [r for r in runs if r.get("conclusion") is not None]


def success_rate(runs: list[dict]) -> tuple[float | None, int]:
    """(rate%, n) по завершённым прогонам; честное (None, 0) без данных —
    тот же приём, что `pipeline_health.worker_success_rate`."""
    concluded = _concluded(runs)
    if not concluded:
        return None, 0
    ok = sum(1 for r in concluded if r["conclusion"] == "success")
    return round(100.0 * ok / len(concluded), 1), len(concluded)


def runs_in_window(runs: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Полуоткрытое окно [start, end) по `created_at` — тот же порядок
    границ, что `pipeline_health.pulse_cadence`."""
    return [r for r in runs if start <= pulse_guard.parse_time(r["created_at"]) < end]


def evaluate(runs: list[dict], now: datetime, *,
             observation_hours: int = OBSERVATION_WINDOW_HOURS,
             fire_threshold_pct: float = FIRE_RATE_THRESHOLD_PCT,
             min_recent: int = MIN_SAMPLES_RECENT,
             baseline_hours: int = BASELINE_LOOKBACK_HOURS,
             min_baseline: int = MIN_SAMPLES_BASELINE) -> Verdict:
    """Решение — ТОЛЬКО по абсолютному recent-rate (эмпирика, см. докстринг
    модуля «Ревизия 2»). Baseline считается и несётся в `Verdict` для текста
    отчёта, но не участвует в ветвлении `fire`/`ok`/`insufficient_data`."""
    recent_start = now - timedelta(hours=observation_hours)
    baseline_start = recent_start - timedelta(hours=baseline_hours)
    recent = runs_in_window(runs, recent_start, now)
    recent_rate, recent_n = success_rate(recent)

    baseline_window = runs_in_window(runs, baseline_start, recent_start)
    baseline_rate, baseline_n = success_rate(baseline_window)
    # Честная граница (fail loud, не silent-wrong): страница Actions API
    # заполнена целиком (`len(runs) == RUNS_FETCH_LIMIT`) И самый старый
    # полученный прогон новее запрошенного начала baseline-окна — значит
    # часть окна физически не попала в выборку, baseline занижен по выборке,
    # не по факту. Решение fire/ok это не портит (baseline — не решающий
    # фактор), но текст отчёта обязан предупредить, а не выдать частичное
    # число за полное.
    oldest_seen = min((pulse_guard.parse_time(r["created_at"]) for r in runs), default=None)
    baseline_truncated = (
        len(runs) >= RUNS_FETCH_LIMIT
        and oldest_seen is not None
        and oldest_seen > baseline_start
    )

    if recent_n < min_recent:
        return Verdict(
            "insufficient_data", recent_rate, recent_n, baseline_rate, baseline_n,
            baseline_truncated, now.isoformat(),
            reason=f"recent={recent_n} прогонов за {observation_hours}ч (нужно {min_recent})",
        )

    status = "fire" if recent_rate <= fire_threshold_pct else "ok"
    return Verdict(status, recent_rate, recent_n, baseline_rate, baseline_n,
                    baseline_truncated, now.isoformat())


class Suspect(NamedTuple):
    number: int
    title: str
    merged_at: str


def suspects_from_search(search_result: dict, now: datetime, *,
                         lookback_hours: int = ATTRIBUTION_LOOKBACK_HOURS) -> tuple[list[Suspect], bool]:
    """`(подозреваемые, обрезан_ли_результат)` — прод-форма `search/issues`
    (см. `pipeline_health.search_merged_prs`). Сортировка по времени слияния
    (старые → новые), НЕ по порядку самого ответа API (тот сортирован по
    времени СОЗДАНИЯ issue, не слияния — докстринг `search_merged_prs`
    v1 ошибочно утверждал обратное). `truncated=True` — `total_count` больше
    числа реально полученных items (одна страница)."""
    items = search_result.get("items", [])
    total = int(search_result.get("total_count", 0) or 0)
    start = now - timedelta(hours=lookback_hours)
    suspects = []
    for item in items:
        pr = item.get("pull_request") or {}
        merged_at = pr.get("merged_at")
        if not merged_at:
            continue
        merged_dt = pulse_guard.parse_time(merged_at)
        if start <= merged_dt <= now:
            suspects.append(Suspect(item["number"], item.get("title", ""), merged_at))
    suspects.sort(key=lambda s: s.merged_at)
    return suspects, total > len(items)


def render_report(verdict: Verdict, suspects: list[Suspect], truncated: bool) -> str:
    if verdict.status == "insufficient_data":
        return f"worker_success_rate: insufficient_data ({verdict.reason})"
    baseline_note = (f"{verdict.baseline_rate}%/{verdict.baseline_n}"
                     + (" (неполное окно — см. RUNS_FETCH_LIMIT)" if verdict.baseline_truncated else "")
                     if verdict.baseline_rate is not None else "нет данных")
    if verdict.status == "ok":
        return (f"worker_success_rate: ok (recent={verdict.recent_rate}%/{verdict.recent_n} "
                f"за {OBSERVATION_WINDOW_HOURS}ч, для контекста baseline {BASELINE_LOOKBACK_HOURS}ч={baseline_note})")

    lines = [
        f"worker_success_rate: FIRE — {verdict.recent_rate}% успешных за последние "
        f"{OBSERVATION_WINDOW_HOURS}ч ({verdict.recent_n} прогонов, порог "
        f"<= {FIRE_RATE_THRESHOLD_PCT}%)",
        f"- для контекста, baseline за предыдущие {BASELINE_LOOKBACK_HOURS}ч: {baseline_note}",
        "",
    ]
    if not suspects:
        lines.append(
            f"Подозреваемых слияний в окне атрибуции ({ATTRIBUTION_LOOKBACK_HOURS}ч) не найдено — "
            "падение не привязано ни к одному слиянию этим окном (может быть внешней причиной: "
            "квота провайдера, инфраструктура GitHub)."
        )
    else:
        word = "Подозреваемый" if len(suspects) == 1 else f"Подозреваемых {len(suspects)}, по времени слияния"
        lines.append(f"{word} (корреляция по времени, НЕ причинность):")
        for s in suspects:
            lines.append(f"  - PR #{s.number} слит {s.merged_at}: {s.title}")
        if truncated:
            lines.append(
                "  (список обрезан первой страницей Search API — в окне атрибуции могло "
                "быть больше слияний, чем показано)"
            )
    return "\n".join(lines)


# ── I/O ─────────────────────────────────────────────────────────────────


def fetch_suspects(repo: str, now: datetime,
                   lookback_hours: int = ATTRIBUTION_LOOKBACK_HOURS) -> tuple[list[Suspect], bool]:
    start = now - timedelta(hours=lookback_hours)
    search_result = pipeline_health.search_merged_prs(repo, pulse_guard.gh, start, now)
    return suspects_from_search(search_result, now, lookback_hours=lookback_hours)


def _fingerprint_line(fp: str = FINGERPRINT) -> str:
    return f"Отпечаток: `{fp}`"


def _evidence_marker(day: str) -> str:
    return f"[merge-health-улика: {day}]"


def _open_watch_tasks(repo: str) -> list[dict]:
    """`PROCESS_LABEL` ("area:process") несёт двоеточие — GitHub трактует
    некодированное `:` в query-параметре `labels=` как URL-синтаксис, не
    байт значения фильтра, и молча отвечает пустым списком, не ошибкой
    (класс #938, живое доказательство: `gh api ".../issues?labels=waiting:owner"`
    → `[]`, тот же запрос с `%3A` → список задач). `quote(..., safe="")`
    кодирует ВСЕ символы вне unreserved RFC 3986, включая двоеточие."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={quote(PROCESS_LABEL, safe='')}&per_page=100",
        pulse_guard.gh)
    return [issue for issue in issues if "pull_request" not in issue]


def _find_open_task(fp: str, issues: list[dict]) -> dict | None:
    for issue in issues:
        if _fingerprint_line(fp) in (issue.get("body") or ""):
            return issue
    return None


def _tasks_created_since(repo: str, since: datetime) -> int:
    """Тот же приём, что `health_audit.audit_tasks_created_since` — суточный
    потолок по факту СОЗДАНИЯ (не текущей открытости): закрытая сегодня
    задача этого же механизма всё равно заняла квоту суток."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all&labels={quote(PROCESS_LABEL, safe='')}&per_page=100",
        pulse_guard.gh)
    return sum(
        1 for issue in issues
        if "pull_request" not in issue
        and _fingerprint_line(FINGERPRINT) in (issue.get("body") or "")
        and pulse_guard.parse_time(issue["created_at"]) >= since
    )


def render_body(verdict: Verdict, suspects: list[Suspect], truncated: bool) -> str:
    report = render_report(verdict, suspects, truncated)
    return "\n".join([
        "Автоматически заведено детектором регрессии здоровья конвейера, "
        "привязанным к слиянию (issue #967, живой корень #878/#937): "
        "worker_success_rate упал в пределах часов.",
        "",
        _fingerprint_line(),
        "",
        "## Числа",
        "",
        report,
        "",
        "## Честный потолок",
        "",
        "Подозреваемые названы по КОРРЕЛЯЦИИ времени слияния, не по причинности. "
        "Приоритет `area:process` — тир-1 по номеру задачи, но тайбрейк по "
        "возрастанию номера ставит свежую задачу в ХВОСТ тир-1-очереди, не "
        "гарантирует немедленный пикап при большом бэклоге — реальная срочность "
        "идёт эскалацией (#120 + Telegram), не автоматическим воркером.",
        "",
        "## Критерий готовности",
        "",
        "Следующий прогон детектора видит `ok` (recent-rate выше порога) — "
        "задача закрывается человеком/агентом по факту фикса, не автоматически.",
    ])


def run_watch(repo: str, now: datetime) -> list[str]:
    report: list[str] = []
    runs = pulse_guard.recent_runs(repo, pulse_guard.WORKER_WORKFLOW, per_page=RUNS_FETCH_LIMIT)
    verdict = evaluate(runs, now)

    if verdict.status != "fire":
        report.append(render_report(verdict, [], False))
        return report

    suspects, truncated = fetch_suspects(repo, now)
    full_report = render_report(verdict, suspects, truncated)
    report = [full_report]

    open_issues = _open_watch_tasks(repo)
    existing = _find_open_task(FINGERPRINT, open_issues)
    today = now.date().isoformat()

    if existing:
        marker = _evidence_marker(today)
        try:
            already_today = bool(pulse_guard.issue_marker_times(repo, existing["number"], marker))
        except RuntimeError as error:
            print(f"::warning::не удалось проверить дедуп улики #{existing['number']}: {error}",
                  file=sys.stderr)
            already_today = False
        if already_today:
            report.append(f"📎 #{existing['number']}: улика за {today} уже оставлена")
        else:
            pulse_guard.post_issue_comment(repo, existing["number"], f"{marker}\n\n{full_report}")
            report.append(f"📎 #{existing['number']}: новая улика")
    else:
        created_today = _tasks_created_since(repo, now - timedelta(hours=24))
        if created_today >= MERGE_HEALTH_DAILY_CAP:
            text = (
                f"🚨 edge-harness: суточный потолок задач merge-health ({MERGE_HEALTH_DAILY_CAP}) "
                f"исчерпан — новый `fire` НЕ заведён отдельной задачей.\n\n{full_report}"
            )
            esc_result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
            report.append(f"🚨 потолок исчерпан ({created_today}/{MERGE_HEALTH_DAILY_CAP}) — {esc_result}")
            return report
        title = f"Регрессия worker_success_rate после слияния ({now.strftime('%Y-%m-%d %H:%M')} UTC)"
        body = render_body(verdict, suspects, truncated)
        result = pool_issue.create_pool_issue(pulse_guard.gh, repo, title, body,
                                              [TASK_LABEL, PROCESS_LABEL])
        number = result["number"]
        report.append(f"🆕 задача #{number} заведена")

    escalate_text = f"🚨 edge-harness: регрессия worker_success_rate (fire) — {full_report}"
    esc_result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, escalate_text)
    report.append(f"🚨 эскалация #{pulse_guard.WATCHDOG_ISSUE}: {esc_result}")
    return report


# ── CLI ────────────────────────────────────────────────────────────────────


def cmd_run(_args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)
    for line in run_watch(repo, now):
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="снять recent-rate, при fire — эскалация + задача (с дедупом/потолком)")
    args = parser.parse_args()
    return {"run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
