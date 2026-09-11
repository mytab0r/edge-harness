#!/usr/bin/env python3
"""Детектор регрессии здоровья конвейера, привязанный к слиянию — часы, не
сутки (issue #967, живой корень #878/#937).

## Почему это отдельный механизм, не просто более частый снимок

`scripts/measure/pipeline_health.py::worker_success_rate` уже существует, но
снимается РАЗ В КАЛЕНДАРНЫЕ СУТКИ (`should_snapshot`) на COUNT-окне последних
`WORKER_SUCCESS_WINDOW` прогонов — по построению не мог бы поймать регрессию
быстрее чем на следующий день. `scripts/orchestra/health_regression.py`
сравнивает СУТОЧНЫЕ снимки с суточным же baseline (`BASELINE_WINDOW_DAYS=14`,
`MIN_SAMPLES_FOR_BASELINE=5` снятых ДНЕЙ) — тот же суточный шаг.

Живое доказательство необходимости (issue #967): PR #878 слит
2026-09-10T22:54:34Z; `worker.yml` за 72ч до слияния — 39/82 успешных
(47.6%), за первые 6ч после — 0/5 (0%). Обнаружил человек утром, ~9ч спустя;
ни `pulse_guard`, ни суточный снимок это не поймали (первый — потому что
успехи изредка вклинивались и не давали счётчику подряд идущих отказов
взвестись; второй — потому что снимок ещё не наступил).

Этот модуль вместо снимка на диске считает TIME-BOXED success-rate воркера
НА ЛЕТУ из уже читаемого списка прогонов (`pulse_guard.recent_runs`) —
скользящее «недавнее окно» против «длинного baseline», оба окна кончаются в
момент запуска (`now`). Раз аргумент `now` двигается вместе с реальным
временем, а не календарной датой, метрика движется в пределах часов — именно
то, чего не даёт суточный гейт. Ничего не пишется на диск/ветку данных:
источник (Actions API) сам по себе всегда свежий, второй копии истории не
заводится (design.md, «Один источник, не вторая история»).

## Алгоритм (пропорционально `health_regression.classify_metric`, тот же
## смысл порогов REGRESSION_THRESHOLD_PCT/ESCALATION_THRESHOLD_PCT — общий
## модуль, не два разных значения «регрессии» в одном репозитории)

  recent  = success-rate прогонов `worker.yml` за последние
            `OBSERVATION_WINDOW_HOURS` часов;
  baseline = success-rate прогонов за `BASELINE_LOOKBACK_HOURS` часов ДО
             начала recent-окна (длинный, стабильный ориентир «нормы»);
  regression/fire — то же отклонение `health_regression.deviation_pct`, те
  же два порога (50%/100%) этого же модуля — DRY на СМЫСЛ регрессии, не
  копия чисел.

Три честных исхода, как и везде в этом семействе детекторов:
`insufficient_data` (мало прогонов хоть в одном из окон),
`ok`/`regression`/`fire`.

## Подозреваемые — честно, без угадывания одного (design.md, «Корреляция,
## не причинность»)

При `regression`/`fire` называются ВСЕ PR, слитые в окне атрибуции
`[now - ATTRIBUTION_LOOKBACK_HOURS, now]` (`pipeline_health.search_merged_prs`)
— не один «самый вероятный». Если слияний было много, отчёт перечисляет их
все с временем слияния, не выбирает. Это ЦЕЛЕНАПРАВЛЕННО грубая сеть: она не
пытается исключить PR, чьё собственное окно «до» уже выглядело плохо (такая
попытка на разреженных данных `worker.yml` этого репозитория даёт неустойчивые
результаты — честно НЕ реализовано, см. design.md «Не подтверждено»).
Подозреваемый называется подозреваемым, не виновным.

## Реакция — задача высшего приоритета, не авто-revert (design.md, «Почему
## не авто-revert»)

На `regression`/`fire`: (1) эскалация тем же каналом, что и остальной
конвейер (`pulse_guard.escalate` — issue-комментарий в #120 + Telegram, не
второй канал), с дедупом по календарному часу окна; (2) заведение/донесение
улики в задачу пула с метками `task`+`area:process` — `area:process` даёт ей
приоритет уровня 1 в `free_task.py::issue_priority_key` (тот же механизм,
которым уже размечены остальные мета-задачи конвейера — не новая
инфраструктура приоритета), то есть следующий свободный воркер возьмёт её
раньше обычных задач пула БЕЗ отдельного прямого workflow_dispatch (тот
дублировал бы дорогу диспетчера `scheduler.py`, трогать который эта задача
не вправе). Почему не авто-revert — см. design.md: у ЭТОГО класса регрессии
(критерий успеха самого конвейера) откат кода c равной вероятностью
возвращает СТАРЫЙ баг, который откатываемый PR как раз чинил (буквально
случай #878 — до него воркер считал успехом чужой предсуществующий PR).

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

sys.path.insert(0, str(Path(__file__).resolve().parent))  # health_regression/pulse_guard соседи

import health_regression  # deviation_pct + REGRESSION_THRESHOLD_PCT/ESCALATION_THRESHOLD_PCT
import pulse_guard  # gh/recent_runs/parse_time/escalate/issue_marker_times/WATCHDOG_ISSUE

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


# ── Пороги этого модуля (design.md, «Не подтверждено, п.1»: не выведены из
# данных — история наблюдений этой конкретной метрики короче суток на момент
# написания) ─────────────────────────────────────────────────────────────

# Пост-мержевое окно: 6ч даёт в среднем 3-8 прогонов worker.yml при
# наблюдённом каденсе этого репозитория (30-90 мин/прогон, см. design.md,
# живой замер) — достаточно для MIN_SAMPLES_RECENT, недостаточно, чтобы
# оправдать суточное ожидание.
OBSERVATION_WINDOW_HOURS = 6
# Длинный ориентир «нормы» — 72ч (3 суток) ДО начала recent-окна: заметно
# длиннее наблюдательного окна, чтобы редкий всплеск не считался «нормой»,
# но короче недели — цикл этого конвейера быстрее.
BASELINE_LOOKBACK_HOURS = 72
MIN_SAMPLES_RECENT = 3
MIN_SAMPLES_BASELINE = 5  # тот же порог, что MIN_SAMPLES_FOR_BASELINE у health_regression, другая единица (прогоны, не дни)
# Окно атрибуции подозреваемых — вдвое шире recent-окна: слияние ПЕРЕД самым
# началом recent-окна тоже могло вызвать наблюдаемое падение (его эффект
# целиком лежит внутри recent-окна, а само слияние — чуть раньше границы).
ATTRIBUTION_LOOKBACK_HOURS = 2 * OBSERVATION_WINDOW_HOURS
# Сколько последних прогонов запрашивать у Actions API за один вызов: должно
# перекрывать BASELINE_LOOKBACK_HOURS + OBSERVATION_WINDOW_HOURS часов при
# практическом каденсе; см. design.md «Честный потолок — только одна
# страница». recent_runs одним вызовом не листает страницы — тот же
# осознанный предел, что уже принят у worker_success_rate (WORKER_SUCCESS_WINDOW).
RUNS_FETCH_LIMIT = 100

TASK_LABEL = "task"
PROCESS_LABEL = "area:process"
FINGERPRINT_PREFIX = "merge-health:worker_success_rate"


class Verdict(NamedTuple):
    status: str  # insufficient_data | ok | regression | fire
    baseline_rate: float | None
    baseline_n: int
    recent_rate: float | None
    recent_n: int
    deviation_pct: float | None
    recent_start: str
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
             baseline_hours: int = BASELINE_LOOKBACK_HOURS,
             min_recent: int = MIN_SAMPLES_RECENT,
             min_baseline: int = MIN_SAMPLES_BASELINE) -> Verdict:
    """Rolling recent-окно (кончается в `now`) против длинного baseline
    (кончается в начале recent-окна) — оба честно фильтруются по времени, не
    по счётчику последних записей списка `runs` (порядок списка, отданный
    Actions API, для этого не гарантирован достаточно строго)."""
    recent_start = now - timedelta(hours=observation_hours)
    baseline_start = recent_start - timedelta(hours=baseline_hours)
    recent_rate, recent_n = success_rate(runs_in_window(runs, recent_start, now))
    baseline_rate, baseline_n = success_rate(runs_in_window(runs, baseline_start, recent_start))

    if recent_n < min_recent or baseline_n < min_baseline:
        return Verdict(
            "insufficient_data", baseline_rate, baseline_n, recent_rate, recent_n, None,
            recent_start.isoformat(), now.isoformat(),
            reason=(f"recent={recent_n} прогонов (нужно {min_recent}), "
                    f"baseline={baseline_n} прогонов (нужно {min_baseline})"),
        )

    deviation = health_regression.deviation_pct(recent_rate, baseline_rate, "lower_is_worse")
    status = "ok"
    if deviation >= health_regression.ESCALATION_THRESHOLD_PCT:
        status = "fire"
    elif deviation >= health_regression.REGRESSION_THRESHOLD_PCT:
        status = "regression"
    return Verdict(status, baseline_rate, baseline_n, recent_rate, recent_n,
                    round(deviation, 1), recent_start.isoformat(), now.isoformat())


class Suspect(NamedTuple):
    number: int
    title: str
    merged_at: str


def suspects_from_search(search_result: dict, now: datetime, *,
                         lookback_hours: int = ATTRIBUTION_LOOKBACK_HOURS) -> tuple[list[Suspect], bool]:
    """`(подозреваемые, обрезан_ли_результат)` — прод-форма `search/issues`
    (см. `pipeline_health.search_merged_prs`). Сортировка по времени слияния
    (старые → новые) — отчёт читает по порядку событий, не по номеру PR.
    `truncated=True` — `total_count` больше числа реально полученных items
    (одна страница, см. докстринг `search_merged_prs`): отчёт обязан назвать
    это честно, не притвориться полным списком."""
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
    if verdict.status == "ok":
        return (f"worker_success_rate: ok (baseline={verdict.baseline_rate}%/{verdict.baseline_n}, "
                f"recent={verdict.recent_rate}%/{verdict.recent_n}, отклонение={verdict.deviation_pct}%)")

    lines = [
        f"worker_success_rate: {verdict.status.upper()} — recent-окно с "
        f"{verdict.recent_start} по {verdict.now} упало относительно baseline",
        f"- baseline (последние {BASELINE_LOOKBACK_HOURS}ч до окна): "
        f"{verdict.baseline_rate}% ({verdict.baseline_n} прогонов)",
        f"- recent (последние {OBSERVATION_WINDOW_HOURS}ч): "
        f"{verdict.recent_rate}% ({verdict.recent_n} прогонов)",
        f"- отклонение в худшую сторону: {verdict.deviation_pct}%",
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


def _fingerprint(now: datetime) -> str:
    """Дедуп по календарному ЧАСУ recent-окна (не по дню, как у
    `health_audit.py`): детекция здесь часовая, повторное открытие той же
    задачи с новой уликой раз в час при затянувшемся инциденте — приемлемо,
    раз в 15 минут (каденс cron этого workflow) — было бы спамом."""
    return f"{FINGERPRINT_PREFIX}:{now.strftime('%Y-%m-%dT%H')}"


def _fingerprint_line(fp: str) -> str:
    return f"Отпечаток: `{fp}`"


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


def _find_open_task(repo: str, fp_prefix: str, issues: list[dict]) -> dict | None:
    for issue in issues:
        if fp_prefix in (issue.get("body") or ""):
            return issue
    return None


def render_body(verdict: Verdict, suspects: list[Suspect], truncated: bool, fp: str) -> str:
    report = render_report(verdict, suspects, truncated)
    return "\n".join([
        "Автоматически заведено детектором регрессии здоровья конвейера, "
        "привязанным к слиянию (issue #967, живой корень #878/#937): "
        "worker_success_rate упал в пределах часов после недавних слияний.",
        "",
        _fingerprint_line(fp),
        "",
        "## Числа",
        "",
        report,
        "",
        "## Честный потолок",
        "",
        "Подозреваемые названы по КОРРЕЛЯЦИИ времени слияния, не по причинности — "
        "детектор не читает содержимое диффов и не может отличить виновника от "
        "случайного соседа по времени. Причину и фикс ищет исполнитель.",
        "",
        "## Критерий готовности",
        "",
        "Следующий прогон этого детектора видит `ok` по тому же окну (метрика "
        "вернулась к baseline) — задача закрывается человеком/агентом по факту "
        "фикса, не автоматически.",
    ])


def run_watch(repo: str, now: datetime) -> list[str]:
    report: list[str] = []
    runs = pulse_guard.recent_runs(repo, pulse_guard.WORKER_WORKFLOW, per_page=RUNS_FETCH_LIMIT)
    verdict = evaluate(runs, now)
    report.append(render_report(verdict, [], False) if verdict.status in ("insufficient_data", "ok")
                  else f"worker_success_rate: {verdict.status}")

    if verdict.status not in ("regression", "fire"):
        return report

    suspects, truncated = fetch_suspects(repo, now)
    full_report = render_report(verdict, suspects, truncated)
    report = [full_report]

    fp = _fingerprint(now)
    open_issues = _open_watch_tasks(repo)
    existing = _find_open_task(repo, fp, open_issues)
    body = render_body(verdict, suspects, truncated, fp)

    if existing:
        pulse_guard.post_issue_comment(repo, existing["number"],
                                       f"{_fingerprint_line(fp)}\n\n{full_report}")
        report.append(f"📎 #{existing['number']}: улика по {fp} добавлена")
    else:
        title = f"Регрессия worker_success_rate после слияния ({now.strftime('%Y-%m-%d %H:%M')} UTC)"
        result = pool_issue.create_pool_issue(pulse_guard.gh, repo, title, body,
                                              [TASK_LABEL, PROCESS_LABEL])
        number = result["number"]
        report.append(f"🆕 задача #{number} заведена ({fp})")

    escalate_text = (
        f"🚨 edge-harness: регрессия worker_success_rate ({verdict.status}) — "
        f"{full_report}"
    )
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
    sub.add_parser("run", help="снять recent/baseline, при регрессии — эскалация + задача")
    args = parser.parse_args()
    return {"run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
