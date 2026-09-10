#!/usr/bin/env python3
"""Само-аудит здоровья конвейера → задача пула (openspec/changes/
pipeline-health-self-audit, design.md §3).

Соединяет снимок (`scripts/measure/pipeline_health.py`) и детектор
регрессии (`scripts/orchestra/health_regression.py`) с единственным каналом
заведения задач пула (`scripts/lib/pool_issue.py::create_pool_issue`) — тот
же канал, которым уже пользуются `stall_detector.py`/`pulse_guard.py::
failure_watch`/`upstream_drift.py`, второй канал не заводится.

## Дедуп и потолок (design.md §3.1) — СВОЙ, не общий с auto-detected/ci-failure

Отпечаток `regression:<метрика>` (по классу метрики, не по числовому
значению — значение меняется каждый день, класс «эта метрика деградирует»
нет). Поиск открытой задачи с тем же отпечатком — подстрока «Отпечаток:
`<fp>`» в теле открытых issues с меткой `self-audit` (тот же приём, что
`stall_detector.find_open_task`, независимая реализация — design.md §3.3:
раздельная метка даёт раздельный дедуп-поиск, три разных детектора не
путают друг друга).

`SELF_AUDIT_DAILY_CAP` = число метрик в наборе регрессии
(`health_regression.METRICS`) — по построению не может быть больше одной
задачи регрессии на метрику одновременно, отдельный числовой потолок не
изобретается сверху.

## «Улучшение» vs «пожар» (design.md §3.2)

Не гадание — прямая функция ОДНОГО расчёта `health_regression.classify_metric`
(тот же приём, что `pulse_guard.decide_gate_state`): `regression` → обычная
задача пула (`task` + `self-audit`), `fire` → та же задача плюс немедленная
эскалация тем же каналом, что `pulse_guard.escalate` (issue-комментарий +
Telegram).

Запуск: python scripts/orchestra/health_audit.py run
Тесты:  python -m pytest scripts/orchestra/test_health_audit.py -q
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

sys.path.insert(0, str(Path(__file__).resolve().parent))  # health_regression/pulse_guard соседи

import health_regression
import pulse_guard  # gh/escalate/issue_marker_times/post_issue_comment/WATCHDOG_ISSUE

_RL_SPEC = importlib.util.spec_from_file_location(
    "review_labels", Path(__file__).resolve().parents[1] / "lib" / "review_labels.py")
review_labels = importlib.util.module_from_spec(_RL_SPEC)
_RL_SPEC.loader.exec_module(review_labels)  # type: ignore[union-attr]

_PI_SPEC = importlib.util.spec_from_file_location(
    "pool_issue", Path(__file__).resolve().parents[1] / "lib" / "pool_issue.py")
pool_issue = importlib.util.module_from_spec(_PI_SPEC)
_PI_SPEC.loader.exec_module(pool_issue)  # type: ignore[union-attr]

_PH_SPEC = importlib.util.spec_from_file_location(
    "pipeline_health", Path(__file__).resolve().parents[1] / "measure" / "pipeline_health.py")
pipeline_health = importlib.util.module_from_spec(_PH_SPEC)
_PH_SPEC.loader.exec_module(pipeline_health)  # type: ignore[union-attr]

# ── Пороги/метки этого модуля (одно место правды) ─────────────────────────

TASK_LABEL = "task"
SELF_AUDIT_LABEL = "self-audit"
SELF_AUDIT_DAILY_CAP = len(health_regression.METRICS)  # design.md §3.1 — по числу метрик

CAP_EXHAUSTED_MARKER = "[self-audit: потолок исчерпан"

FINGERPRINT_SLUGS = {
    "merge_throughput": "merge-throughput",
    "pr_age_p95_hours": "pr-age-p95",
    "backlog_total": "backlog",
    "worker_success_rate": "worker-success-rate",
    "pulse_cadence_ratio": "pulse-cadence",
    "do_rows_read_pct": "do-cost",
}


def fingerprint_of(metric: str) -> str:
    return f"regression:{FINGERPRINT_SLUGS[metric]}"


def _fingerprint_line(fp: str) -> str:
    return f"Отпечаток: `{fp}`"


def open_audit_tasks(repo: str) -> list[dict]:
    """Постранично (класс #308) — сырая первая страница молча теряла бы
    задачи само-аудита за первой сотней открытых issues с меткой self-audit."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=open&labels={SELF_AUDIT_LABEL}&per_page=100", pulse_guard.gh)
    return [issue for issue in issues if "pull_request" not in issue]


def find_open_task(repo: str, fp: str, issues: list[dict] | None = None) -> dict | None:
    issues = open_audit_tasks(repo) if issues is None else issues
    marker = _fingerprint_line(fp)
    for issue in issues:
        if marker in (issue.get("body") or ""):
            return issue
    return None


def audit_tasks_created_since(repo: str, since: datetime) -> int:
    """Все issues (открытые и закрытые) с меткой self-audit, созданные не
    раньше `since` — суточный потолок по факту создания, не по текущей
    открытости (закрытая сегодня задача всё равно заняла квоту суток)."""
    issues = review_labels.list_pages(
        f"repos/{repo}/issues?state=all&labels={SELF_AUDIT_LABEL}&per_page=100", pulse_guard.gh)
    return sum(
        1 for issue in issues
        if "pull_request" not in issue and pulse_guard.parse_time(issue["created_at"]) >= since
    )


def render_body(fp: str, c: health_regression.Classification, snapshot_ref: str) -> str:
    lines = [
        "Автоматически заведено само-аудитом здоровья конвейера "
        "(openspec/changes/pipeline-health-self-audit): метрика "
        f"«{c.label}» отклонилась от базовой линии и держится "
        f"{c.streak_days} дн. подряд (порог {health_regression.MIN_REGRESSION_STREAK_DAYS}) — "
        "это регрессия («зелёно, но хуже»), не разовый плохой день.",
        "",
        _fingerprint_line(fp),
        "",
        "## Числа",
        "",
        f"- baseline (медиана за последние {health_regression.BASELINE_WINDOW_DAYS} дней): {c.baseline}",
        f"- сегодня ({c.today_date}): {c.today}",
        f"- отклонение в худшую сторону: {c.deviation_pct}%",
        f"- держится подряд: {c.streak_days} дн.",
        "",
        "## Снимок",
        "",
        f"- {snapshot_ref}",
        "",
        "## Критерий готовности",
        "",
        "Метрика вернулась к baseline (проверяется СЛЕДУЮЩИМ снимком, не "
        "декларацией исполнителя) — либо владелец решил, что это новая "
        "норма (тогда порог/baseline пересматривается руками, не автоматически).",
        "",
        "## Честный потолок",
        "",
        "Детектор ТОЛЬКО заметил числовую регрессию по метрике здоровья "
        "конвейера (design.md §2.3) — причину и фикс ищет исполнитель.",
    ]
    return "\n".join(lines)


def _evidence_marker(fp: str, day: str) -> str:
    return f"[self-audit-улика: {fp}:{day}]"


def _escalate_cap_once(repo: str, now: datetime, fp: str) -> str:
    """Потолок исчерпан — не тонет молча, но и не красит прогон (тот же
    инвариант, что stall_detector.py: «наблюдатель провалов не реагирует на
    свою инфраструктуру мониторинга»). Дедуп — один раз в календарные сутки."""
    marker = f"{CAP_EXHAUSTED_MARKER} {now.date().isoformat()}]"
    if pulse_guard.issue_marker_times(repo, pulse_guard.WATCHDOG_ISSUE, marker):
        return "потолок уже эскалирован сегодня — молчу"
    text = (
        f"🚨 edge-harness: {marker}\n"
        f"Суточный потолок само-аудита ({SELF_AUDIT_DAILY_CAP}) исчерпан — "
        f"новая регрессия `{fp}` НЕ заведена задачей, нужен человек.\n\n"
        "Это НЕ отказ пульса: детектор регрессии жив, просто накопилось "
        "больше одновременных деградаций, чем безопасно заводить за сутки "
        "(по построению — не больше одной задачи на метрику).\n\n"
        "Что дальше: посмотреть открытые задачи с меткой self-audit, решить "
        "руками по непринятому отпечатку."
    )
    return pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)


def run_self_audit(repo: str, classifications: list[health_regression.Classification],
                   snapshot_ref: str, now: datetime) -> list[str]:
    """Вызывается с уже посчитанной классификацией (`health_regression.
    classify_all`) — сам не читает историю снимков, чтобы оставаться
    тестируемым без сети (прод-форма кормится в тестах готовыми
    Classification, не через классификатор заново)."""
    report: list[str] = []
    targets = [c for c in classifications if c.status in ("regression", "fire")]
    if not targets:
        return report

    open_issues = open_audit_tasks(repo)
    created_today: int | None = None

    for c in targets:
        fp = fingerprint_of(c.metric)
        existing = find_open_task(repo, fp, open_issues)
        if existing:
            marker = _evidence_marker(fp, c.today_date)
            if pulse_guard.issue_marker_times(repo, existing["number"], marker):
                report.append(f"📎 #{existing['number']}: {fp} — улика за {c.today_date} уже оставлена")
                continue
            text = (f"{marker}\nПродолжает деградировать: {c.today} "
                    f"(отклонение {c.deviation_pct}%, держится {c.streak_days} дн.)")
            if c.status == "fire":
                result = pulse_guard.escalate(repo, existing["number"], f"🚨 {text}")
                report.append(f"🚨 #{existing['number']}: пожар подтверждён повторно ({result})")
            else:
                pulse_guard.post_issue_comment(repo, existing["number"], text)
                report.append(f"📎 #{existing['number']}: новая улика по {fp}")
            continue

        if created_today is None:
            created_today = audit_tasks_created_since(repo, now - timedelta(hours=24))
        if created_today >= SELF_AUDIT_DAILY_CAP:
            result = _escalate_cap_once(repo, now, fp)
            report.append(f"🚨 потолок само-аудита исчерпан ({created_today}/{SELF_AUDIT_DAILY_CAP}) "
                          f"— {fp} НЕ заведён ({result})")
            continue

        body = render_body(fp, c, snapshot_ref)
        title = f"Регрессия здоровья конвейера: {c.label}"
        result = pool_issue.create_pool_issue(pulse_guard.gh, repo, title, body,
                                              [TASK_LABEL, SELF_AUDIT_LABEL])
        created_today += 1
        number = result["number"]
        report.append(f"🆕 задача #{number} заведена само-аудитом по {fp}")
        if c.status == "fire":
            text = (
                f"🚨 edge-harness: регрессия здоровья конвейера — «{c.label}» "
                f"деградирует {c.streak_days} дн. подряд, отклонение "
                f"{c.deviation_pct}% (порог пожара {health_regression.ESCALATION_THRESHOLD_PCT}%) "
                f"— задача #{number} заведена."
            )
            esc_result = pulse_guard.escalate(repo, number, text)
            report.append(f"🚨 #{number}: эскалация пожара ({esc_result})")

    return report


# ── CLI ────────────────────────────────────────────────────────────────────


def cmd_run(_args: argparse.Namespace) -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    now = datetime.now(timezone.utc)

    snapshot, snap_message = pipeline_health.snapshot_and_store(repo, pulse_guard.gh, now)
    print(snap_message)

    try:
        history = pipeline_health.fetch_history(repo)
    except RuntimeError as error:
        print(f"::error::история снимков недоступна — само-аудит пропущен: {error}")
        return 1

    if not history:
        print("история снимков пуста — само-аудиту не на чем считать регрессию (недостаточно данных)")
        return 0

    classifications = health_regression.classify_all(history)
    for c in classifications:
        print(f"{c.metric}: {c.status}" + (f" ({c.reason})" if c.reason else
              f" (baseline={c.baseline}, сегодня={c.today}, отклонение={c.deviation_pct}%, streak={c.streak_days})"))

    snapshot_ref = f"`{pipeline_health.SNAPSHOT_PATH}` на ветке `{pipeline_health.DATA_BRANCH}`, дата {history[-1]['date']}"
    audit_lines = run_self_audit(repo, classifications, snapshot_ref, now)
    for line in audit_lines:
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="снимок (гейт раз/сутки) + регрессия + само-аудит")
    args = parser.parse_args()
    return {"run": cmd_run}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
