#!/usr/bin/env python3
"""Снимок 6-7 дешёвых метрик здоровья конвейера (openspec/changes/
pipeline-health-self-audit, design.md §1-§2).

Три существующих детектора конвейера (`pulse_guard.py::failure_watch`,
`stall_detector.py`, `upstream_drift.py`) реагируют только на КРАСНОЕ —
упавший прогон, красную проверку, застрявшую задачу. Ни один не видит
ЗЕЛЁНУЮ, но ухудшающуюся систему: конвейер продолжает мержить PR, но реже;
PR проходят ревью, но дольше; воркер продолжает открывать PR, но чаще
проваливается. Этот модуль снимает числа, по которым такую деградацию
можно измерить, — сам не решает, регрессия это или нет (см.
`scripts/orchestra/health_regression.py`).

## Метрики и их цена (design.md §1, таблица «источник/уже читается/цена»)

  - `merge_throughput`      — PR слито за сутки, `search/issues` (1 запрос).
  - `pr_age_p50_hours`/`pr_age_p95_hours` — по уже прочитанному списку
    открытых PR (0 доп. запросов — тот же список, что `scheduler.open_pulls`).
  - `backlog_*`             — по уже прочитанному списку задач пула
    (0 доп. запросов — тот же список, что `scheduler.open_task_issues`).
  - `worker_success_rate`   — по `pulse_guard.recent_runs(worker.yml)`
    (0 доп. запросов сверх того, что уже читает предохранитель).
  - `pulse_cadence_ratio`   — по `pulse_guard.orchestra_tick_runs`
    (0 доп. запросов сверх того, что уже читает heartbeat_check).
  - `do_rows_read_pct`      — GraphQL Cloudflare Analytics, 1 запрос/сутки,
    НЕ через DO (`quotas.collect_cloudflare`, уже существующий сборщик).
  - `gh_rate_remaining_pct` — `gh api rate_limit`, не тратит собственную
    квоту (см. `scripts/lib/rate_guard.py`).

Честно: DO cost и GH rate remaining — необязательные («нет данных» → None),
если токены не заданы или запрос не удался — отсутствие ОДНОЙ метрики не
должно ронять весь снимок (fail loud по месту, silent-wrong в остальном
снимке — нет).

## Хранилище (design.md §2.1-§2.2)

JSONL на отдельной git-ветке `data/pipeline-health` (не main — не засоряет
историю снимками; не DO SQLite — бюджет DO узкий, #575). По прецеденту
`scripts/measure/dispatch_tail.py`/`data/dispatch-latency-tail` (ADR 0005),
но не той же функцией: та кампания пишет CSV построчно на КАЖДЫЙ замер, эта
пишет JSONL не чаще раза в календарные сутки UTC (`should_snapshot`) —
разные гарантии дедупликации, общий код не выносился бы без потери ясности
(design.md, «Не подтверждено», п.4 — CSV vs JSONL, выбор JSONL здесь).

Использование:
    python scripts/measure/pipeline_health.py snapshot   # снимок + запись (гейт раз/сутки)
    python scripts/measure/pipeline_health.py status     # текущая история без сети (локальный JSONL)

Среда: GH_TOKEN (чтение issues/PR/runs), GH_PIPELINE_PAT (git push на ветку
данных — тот же секрет, что dispatch_tail.py), CLOUDFLARE_API_TOKEN/
CLOUDFLARE_ACCOUNT_ID (опционально — DO cost).
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
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

GhFn = Callable[..., dict | list | None]

# review_labels.list_pages — обход страниц GitHub API (класс #308), одно
# место правды в scripts/lib/review_labels.py.
_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

# pulse_guard — recent_runs/orchestra_tick_runs/parse_time уже читают то, что
# нужно этому снимку; второй копии транспорта не заводим.
_PG_SPEC = importlib.util.spec_from_file_location(
    "pulse_guard", Path(__file__).resolve().parents[1] / "orchestra" / "pulse_guard.py")
pulse_guard = importlib.util.module_from_spec(_PG_SPEC)
_PG_SPEC.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

# quotas.collect_cloudflare — уже умеет находить DO rows_read интроспекцией
# и считать % от суточного лимита; второй сборщик того же факта не заводим.
_QZ_SPEC = importlib.util.spec_from_file_location(
    "quotas", Path(__file__).with_name("quotas.py"))
quotas = importlib.util.module_from_spec(_QZ_SPEC)
_QZ_SPEC.loader.exec_module(quotas)  # type: ignore[union-attr]

# data_branch_writer — одно место правды на «append + push с ретраем на
# data-ветку» (issue #882): раньше эта логика была независимой копией здесь
# и в dispatch_tail.py, обе с одним и тем же дефектом (тихий check=False на
# восстановлении после отказа push маскировал отказ авторизации под
# безобидную гонку параллельного писателя) — см. докстринг data_branch_writer.py.
_DBW_SPEC = importlib.util.spec_from_file_location(
    "data_branch_writer", Path(__file__).resolve().parents[1] / "lib" / "data_branch_writer.py")
data_branch_writer = importlib.util.module_from_spec(_DBW_SPEC)
_DBW_SPEC.loader.exec_module(data_branch_writer)  # type: ignore[union-attr]

# ── Одно место правды: пути и пороги кампании снимка ──────────────────────

DATA_BRANCH = "data/pipeline-health"
SNAPSHOT_PATH = "docs/research/data/pipeline-health.jsonl"

TASK_LABEL = "task"
BLOCKED_LABEL = "blocked"
STALE_UNCLAIMED_LABEL = "stale-unclaimed"

# Окно rolling success-rate воркера — то же порядок величины, что и history,
# читаемая предохранителем конвейера (recent_runs по умолчанию читает 10,
# здесь шире — для более устойчивого процента).
WORKER_SUCCESS_WINDOW = 30

# Каденс пульса: интервал cron orchestra.yml (`*/15 * * * *`) — одно место
# правды здесь; если интервал в workflow изменится, эта константа обязана
# смениться вместе с ним (иначе ожидаемое число тиков разойдётся молча).
ORCHESTRA_TICK_INTERVAL_MINUTES = 15
CADENCE_WINDOW_HOURS = 24

COMMIT_IDENTITY = ("-c", "user.name=edge-harness health-snapshot",
                   "-c", "user.email=7416604+mytab0r@users.noreply.github.com")

# ── Чистая логика (тестируется без сети) ──────────────────────────────────


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank перцентиль (тот же алгоритм, что
    `scripts/measure/dispatch_tail.py::percentile`, отдельная копия — та
    работает с int-миллисекундами конкретной кампании, эта с float-часами
    произвольного размера выборки; смысл один и тот же метод, повторно
    объявлен ради независимости модулей друг от друга)."""
    if not values:
        raise ValueError("percentile: пустой список значений")
    ordered = sorted(values)
    idx = max(0, round(p * len(ordered)) - 1)
    return ordered[idx]


def pr_ages_hours(pulls: list[dict], now: datetime) -> list[float]:
    return [
        (now - pulse_guard.parse_time(p["created_at"])).total_seconds() / 3600
        for p in pulls
    ]


def pr_age_stats(pulls: list[dict], now: datetime) -> dict:
    ages = pr_ages_hours(pulls, now)
    if not ages:
        return {"p50": None, "p95": None}
    return {"p50": round(percentile(ages, 0.5), 1), "p95": round(percentile(ages, 0.95), 1)}


def backlog_counts(task_issues: list[dict]) -> dict:
    """Классификация уже прочитанного списка задач пула (открытые, labels=task)
    по состоянию — тот же критерий, что `scheduler.py::mark_stale_unclaimed`/
    `stale_blocked_guard.py` уже применяют по отдельности, здесь просто счёт."""
    free = in_progress = blocked = stale = 0
    for issue in task_issues:
        labels = {label["name"] for label in issue.get("labels", [])}
        if BLOCKED_LABEL in labels:
            blocked += 1
        elif issue.get("assignees"):
            in_progress += 1
        else:
            free += 1
        if STALE_UNCLAIMED_LABEL in labels:
            stale += 1
    return {
        "free": free, "in_progress": in_progress, "blocked": blocked,
        "stale_unclaimed": stale, "total": len(task_issues),
    }


def worker_success_rate(runs: list[dict], window: int = WORKER_SUCCESS_WINDOW) -> float | None:
    """% success среди последних `window` ЗАВЕРШЁННЫХ прогонов worker.yml
    (conclusion не None — прогон ещё бегущий не судит об исходе, тот же
    приём, что `pulse_guard.count_consecutive_failures`). Нет завершённых
    прогонов вовсе — честное «нет данных» (None), не 0/100%."""
    concluded = [r["conclusion"] for r in runs if r.get("conclusion") is not None][:window]
    if not concluded:
        return None
    successes = sum(1 for c in concluded if c == "success")
    return round(100.0 * successes / len(concluded), 1)


def pulse_cadence(tick_runs: list[dict], now: datetime,
                   window_hours: int = CADENCE_WINDOW_HOURS,
                   interval_minutes: int = ORCHESTRA_TICK_INTERVAL_MINUTES) -> dict:
    """Реальные тики job'а `orchestra` за последние `window_hours` против
    ожидаемого числа по интервалу cron — тот же класс замера, что
    research/21 уже применил к кампании dispatch-tail («schedule доставляет
    ~7% тиков»), здесь как метрика, не как разовый вывод."""
    since = now - timedelta(hours=window_hours)
    actual = sum(1 for r in tick_runs if pulse_guard.parse_time(r["created_at"]) >= since)
    expected = int(window_hours * 60 / interval_minutes)
    ratio = round(actual / expected, 3) if expected else None
    return {"actual": actual, "expected": expected, "ratio": ratio}


def merge_throughput_from_search(search_result: dict) -> int:
    """`total_count` дословно из ответа `search/issues` — прод-форма ответа
    GitHub, не пересказ (см. `test_pipeline_health.py`, фикстура из живого
    формата Search API)."""
    return int(search_result.get("total_count", 0) or 0)


def build_snapshot(
    today: date, *,
    merged_search: dict,
    open_pulls: list[dict],
    task_issues: list[dict],
    worker_runs: list[dict],
    tick_runs: list[dict],
    do_rows_read_pct: float | None,
    gh_rate_remaining_pct: float | None,
    now: datetime,
) -> dict:
    """Чистая сборка снимка из уже полученных сырых данных — I/O живёт
    отдельно в `collect()`, эта функция тестируется без единого сетевого
    вызова (прод-форма fixtures)."""
    ages = pr_age_stats(open_pulls, now)
    backlog = backlog_counts(task_issues)
    cadence = pulse_cadence(tick_runs, now)
    return {
        "date": today.isoformat(),
        "merge_throughput": merge_throughput_from_search(merged_search),
        "pr_age_p50_hours": ages["p50"],
        "pr_age_p95_hours": ages["p95"],
        "backlog_free": backlog["free"],
        "backlog_in_progress": backlog["in_progress"],
        "backlog_blocked": backlog["blocked"],
        "backlog_stale_unclaimed": backlog["stale_unclaimed"],
        "backlog_total": backlog["total"],
        "worker_success_rate": worker_success_rate(worker_runs),
        "pulse_ticks_24h": cadence["actual"],
        "pulse_ticks_expected_24h": cadence["expected"],
        "pulse_cadence_ratio": cadence["ratio"],
        "do_rows_read_pct": do_rows_read_pct,
        "gh_rate_remaining_pct": gh_rate_remaining_pct,
    }


def read_rows(text: str) -> list[dict]:
    """JSONL → список снимков. Пустой текст — пустой список (ветки/файла ещё
    нет), одна строка — один снимок; повреждённая строка обрывает разбор
    громко (fail loud — тихо потерянный снимок хуже красного прогона)."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def rows_to_jsonl(rows: list[dict]) -> str:
    if not rows:
        return ""
    return "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n"


def last_snapshot_date(rows: list[dict]) -> date | None:
    if not rows:
        return None
    return date.fromisoformat(rows[-1]["date"])


def should_snapshot(last: date | None, today: date) -> bool:
    """Гейт «не чаще раза в календарные сутки UTC» (design.md §2.2) — тот же
    приём, что `do_rows_read.py --days N` уже применяет («снимать НА
    следующий день ИЛИ позже»): без него пульс писал бы новую строку каждые
    15 минут, 96 строк/сутки вместо одной."""
    return last is None or today > last


# ── I/O: сбор сырых данных (тонкая обёртка, только сеть) ──────────────────


def _do_rows_read_pct(account_id: str | None, token: str | None) -> float | None:
    if not account_id or not token:
        return None
    try:
        rows = quotas.collect_cloudflare(account_id, token)
    except RuntimeError as error:
        print(f"::warning::DO rows_read недоступен для снимка здоровья: {error}", file=sys.stderr)
        return None
    for row in rows:
        if row.resource == "DO rows_read/сутки":
            return row.pct
    return None


def _gh_rate_remaining_pct(gh: GhFn) -> float | None:
    try:
        data = gh("rate_limit")
        core = data["resources"]["core"]
        limit = core["limit"]
        if not limit:
            return None
        return round(100.0 * core["remaining"] / limit, 1)
    except (RuntimeError, KeyError, TypeError, ZeroDivisionError) as error:
        print(f"::warning::GitHub rate_limit недоступен для снимка здоровья: {error}", file=sys.stderr)
        return None


def collect(repo: str, gh: GhFn, now: datetime) -> dict:
    """Собирает сырые данные и строит снимок. Каждый источник — тот же
    вызов/тот же список, что уже читает остальной конвейер (design.md §1) —
    единственный НОВЫЙ сетевой вызов на снимок — `search/issues` (merge
    throughput) и, раз в сутки, GraphQL Cloudflare (DO cost)."""
    today = now.date()
    day_str = today.isoformat()
    merged_search = gh(
        f"search/issues?q=repo:{repo}+is:pr+is:merged+merged:{day_str}..{day_str}"
    ) or {}
    open_pulls = review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)
    task_issues = [
        issue for issue in review_labels.list_pages(
            f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100", gh)
        if "pull_request" not in issue
    ]
    worker_runs = pulse_guard.recent_runs(repo, pulse_guard.WORKER_WORKFLOW,
                                          per_page=WORKER_SUCCESS_WINDOW)
    tick_runs = pulse_guard.orchestra_tick_runs(repo)
    do_pct = _do_rows_read_pct(os.environ.get("CLOUDFLARE_ACCOUNT_ID"),
                               os.environ.get("CLOUDFLARE_API_TOKEN"))
    rate_pct = _gh_rate_remaining_pct(gh)
    return build_snapshot(
        today, merged_search=merged_search, open_pulls=open_pulls,
        task_issues=task_issues, worker_runs=worker_runs, tick_runs=tick_runs,
        do_rows_read_pct=do_pct, gh_rate_remaining_pct=rate_pct, now=now,
    )


# ── Git-транспорт: делегирован data_branch_writer.py (issue #882, одно место
# правды на append+push с ретраем — см. докстринг там) ─────────────────────


def origin_url() -> str:
    return data_branch_writer.origin_url()


def clone_data_branch(workdir: str) -> None:
    data_branch_writer.clone_data_branch(origin_url(), workdir, DATA_BRANCH)


def _snapshot_is_duplicate(date_str: str):
    def check(current_text: str) -> bool:
        rows = read_rows(current_text)
        is_dup = bool(rows) and rows[-1].get("date") == date_str
        if is_dup:
            print(f"снимок за {date_str} уже на {DATA_BRANCH} — не дублирую")
        return is_dup
    return check


def append_snapshot_and_push(workdir: str, snapshot: dict) -> bool:
    """Строка снимка + push через data_branch_writer.append_and_push (issue
    #882): гонка за ветку — свежий, ГРОМКО подтверждённый с сервера, fetch/
    checkout и повтор (до 5 раз); отказ авторизации — красный шаг немедленно,
    не тихий повтор (см. докстринг data_branch_writer.py). Дубль того же дня
    не пишется — гейт `should_snapshot` уже проверил ДО клона, но гонка двух
    параллельных пультов в одну минуту всё же возможна: `_snapshot_is_duplicate`
    перечитывает файл ПОСЛЕ громкого fetch/checkout и видит уже записанный
    день — это факт с сервера, не устаревшая локальная копия."""
    return data_branch_writer.append_and_push(
        workdir=workdir,
        data_branch=DATA_BRANCH,
        rel_path=SNAPSHOT_PATH,
        commit_identity=COMMIT_IDENTITY,
        commit_message=f"health snapshot {snapshot['date']}",
        is_duplicate=_snapshot_is_duplicate(snapshot["date"]),
        render_next=lambda current: current + json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True) + "\n",
    )


def fetch_history(repo: str) -> list[dict]:
    """История снимков с data-ветки без локального клона (для регрессии,
    которая читает, но не пишет) — один GitHub API запрос, тот же класс
    вызова, что `dispatch_tail.py::Github.contents`."""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{SNAPSHOT_PATH}?ref={DATA_BRANCH}"],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        if "404" in result.stderr or "Not Found" in result.stdout:
            return []
        raise RuntimeError(f"чтение истории снимков не удалось: {result.stderr.strip()[:300]}")
    import base64
    blob = json.loads(result.stdout)
    return read_rows(base64.b64decode(blob["content"]).decode())


def snapshot_and_store(repo: str, gh: GhFn, now: datetime) -> tuple[dict | None, str]:
    """Гейтованный снимок: собирает и пишет, только если с последнего снимка
    прошли календарные сутки. Возвращает (снимок либо None, человекочитаемая
    строка отчёта)."""
    history = fetch_history(repo)
    last = last_snapshot_date(history)
    today = now.date()
    if not should_snapshot(last, today):
        return None, f"снимок за {today.isoformat()} уже снят (последний: {last})"
    snapshot = collect(repo, gh, now)
    workdir = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "pipeline-health-data")
    clone_data_branch(workdir)
    wrote = append_snapshot_and_push(workdir, snapshot)
    verb = "записан" if wrote else "уже был записан параллельным прогоном"
    return snapshot, f"снимок здоровья за {snapshot['date']} {verb}: {snapshot}"


# ── CLI ────────────────────────────────────────────────────────────────────


def cmd_snapshot(_args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = pulse_guard.gh
    snapshot, message = snapshot_and_store(repo, gh, datetime.now(timezone.utc))
    print(message)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.jsonl)
    rows = read_rows(path.read_text(encoding="utf-8")) if path.exists() else []
    if not rows:
        print(f"{path}: снимков нет")
        return 0
    for row in rows[-args.last:]:
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="собрать снимок и записать на data-ветку (гейт раз/сутки)")
    status = sub.add_parser("status", help="последние снимки локального JSONL (без сети)")
    status.add_argument("--jsonl", default=SNAPSHOT_PATH)
    status.add_argument("--last", type=int, default=7)
    args = parser.parse_args()
    return {"snapshot": cmd_snapshot, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
