#!/usr/bin/env python3
"""Кампания замера хвоста задержки «repository_dispatch → старт job'а» (задача #4).

Первые 22 замера (2026-08-28, медиана 8.3 с) сняты за один короткий сеанс и хвост
распределения не ловят. Эта кампания снимает 100+ замеров в разное время суток
(включая американский рабочий день) автоматически — воркер не может жить дольше
одного job'а, а критерий задачи требует ≥24 часов разброса. Обоснование выбора
метрики и схемы — docs/decisions/0005-dispatch-tail-campaign.md.

Схема (dual-writer, один замер = ровно одна строка CSV):
  1. тик → `dispatch`: POST /dispatches (event_type dispatch-latency-probe)
     под PAT. GITHUB_TOKEN для диспатча — документированное исключение из
     запрета рекурсии, но живьём не проверено, а push/PR от него не зажигают
     проверок точно: один PAT на весь пишущий путь (docs/research/21-github-actions.md).
     Каденция — самоподдержка: тик в конце диспатчит следующий workflow_dispatch
     (`chain`), cron — страховка смерти цепочки. Замер 2026-09-04: cron `*/15` на
     этом репозитории доставил ~7% тиков (31 из ~449 за 112 ч), dispatch-события —
     32 из 32; долгие кампании на schedule не строятся
     (docs/research/21-github-actions.md, «Замер schedule на этом репозитории»).
  2. Подъём workflow → job `probe` → `record`: замеряет себя сам по серверным
     таймстампам GitHub (run_created_at, run_started_at) и пишет строку.
  3. `dispatch` ждёт строку своего probe_id в CSV до TIMEOUT_S; не дождался —
     пишет строку timeout (тяжёлый хвост сам по себе результат; семантика
     базового замера 2026-08-28 — его скрипт удалён cleanup'ом 09ef969).

Метрики в строке:
  latency_ms = run_started_at − sent_at        — «диспатч отправлен → job начал
                                                 выполняться» (часы диспетчера + GitHub);
  queue_ms    = run_started_at − run_created_at — чистое ожидание раннера по часам
                                                 GitHub, без сетевого пути до API.

CSV живёт на ветке DATA_BRANCH и попадает в main финальным PR кампании вместе со
сводкой в docs/research/99-open-questions.md (маркеры MARK_BEGIN/MARK_END).
Критерий покрытия и лимиты — константы ниже, одно место правды.

Использование (в CI, из .github/workflows/dispatch-latency-probe.yml):
    python scripts/measure/dispatch_tail.py dispatch     # тик кампании: замер + сторож
    python scripts/measure/dispatch_tail.py record       # сторона измеряемого job'а
    python scripts/measure/dispatch_tail.py chain        # следующая ступень цепочки тиков
    python scripts/measure/dispatch_tail.py tick_failed  # след тика, умершего до диспатча
    python scripts/measure/dispatch_tail.py status       # покрытие + сводка (без записи)
    python scripts/measure/dispatch_tail.py finalize     # завершить кампанию руками

Среда: GH_PIPELINE_PAT (широкий PAT конвейера: contents/actions/issues/pull-requests write; до задачи #6 имя было GH_DISPATCH_TOKEN),
GITHUB_REPOSITORY; для record — PROBE_ID, SENT_AT_MS, GITHUB_RUN_ID.
"""

import argparse
import base64
import csv
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Единственное место правды кампании ────────────────────────────────────────────

CSV_PATH = "docs/research/data/dispatch-latency-tail.csv"
DATA_BRANCH = "data/dispatch-latency-tail"
WORKFLOW_FILE = ".github/workflows/dispatch-latency-probe.yml"
ISSUE_NUMBER = 4
PR_TITLE = "Замер хвоста задержки dispatch: данные кампании (#4)"

# Критерий готовности задачи #4 — «100+ замеров в разное время (включая
# американский рабочий день)». Бизнес-окно США: пн–пт 13:00–24:00 UTC —
# это 9:00–20:00 ET и 6:00–17:00 PT в летнем времени, объединение по всем зонам.
MIN_ROWS = 100
MIN_BUSINESS_ROWS = 20
MIN_SPAN_HOURS = 24
MAX_CAMPAIGN_DAYS = 7  # предохранитель: дольше — финализация с громким «не достигнут»
# Сознательное продление кампании — снаружи кода (env в workflow), а не правкой
# константы: иначе после unmet-финализации первый же тик снова финализирует
# (overdue считается от первой строки CSV) и кампания запирается намертво.
CAMPAIGN_MAX_DAYS_ENV = "CAMPAIGN_MAX_DAYS"

TIMEOUT_S = 600  # как у базового замера 2026-08-28: нет старта за 600 с = таймаут
POLL_INTERVAL_S = 15
# Пауза перед диспатчем следующей ступени цепочки: вместе с временем тика даёт
# ~7–16 мин на цикл и ограничивает повтор при сбое (худший ретрай — раз в 5 мин,
# а не цикл красных запусков вплотную).
CHAIN_DELAY_S = 300

MARK_BEGIN = "<!-- dispatch-tail:begin -->"
MARK_END = "<!-- dispatch-tail:end -->"

API = "https://api.github.com"
UA = "edge-harness/0.1 (dispatch-latency-probe; +https://github.com/mytab0r/edge-harness)"
COMMIT_IDENTITY = ("-c", "user.name=edge-harness campaign",
                   "-c", "user.email=7416604+mytab0r@users.noreply.github.com")

CSV_FIELDS = [
    "probe_id", "sent_at", "run_id", "run_url",
    "run_created_at", "run_started_at", "queue_ms", "latency_ms",
    "status", "timeout_s", "note",
]

# ── Чистая логика (тестируется напрямую) ─────────────────────────────────────────


def parse_github_time(raw: str) -> datetime:
    """ISO-таймстамп GitHub API: '2026-08-31T13:05:00Z' (бывает с .000 и +00:00)."""
    if not raw:
        raise ValueError("пустой таймстамп")
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def sent_at_datetime(sent_at_ms: int) -> datetime:
    return datetime.fromtimestamp(sent_at_ms / 1000, tz=timezone.utc)


def compute_row(probe_id: str, sent_at_ms: int, run: dict) -> dict:
    """Строка CSV по данным о собственном run'е (прод-форма ответа GitHub API)."""
    started = parse_github_time(run["run_started_at"])
    created = parse_github_time(run["created_at"])
    return {
        "probe_id": probe_id,
        "sent_at": str(sent_at_ms),
        "run_id": str(run["id"]),
        "run_url": run["html_url"],
        "run_created_at": run["created_at"],
        "run_started_at": run["run_started_at"],
        "queue_ms": str(max(0, epoch_ms(started) - epoch_ms(created))),
        "latency_ms": str(max(0, epoch_ms(started) - sent_at_ms)),
        "status": "ok",
        "timeout_s": "",
        "note": "",
    }


def timeout_row(probe_id: str, sent_at_ms: int, note: str = "") -> dict:
    """Строка таймаута: старт не наступил за TIMEOUT_S — сам по себе результат."""
    return {
        "probe_id": probe_id, "sent_at": str(sent_at_ms), "run_id": "", "run_url": "",
        "run_created_at": "", "run_started_at": "", "queue_ms": "", "latency_ms": "",
        "status": "timeout", "timeout_s": str(TIMEOUT_S), "note": note,
    }


def dispatch_failed_row(probe_id: str, sent_at_ms: int, note: str) -> dict:
    return {
        "probe_id": probe_id, "sent_at": str(sent_at_ms), "run_id": "", "run_url": "",
        "run_created_at": "", "run_started_at": "", "queue_ms": "", "latency_ms": "",
        "status": "dispatch_failed", "timeout_s": "", "note": note,
    }


def rows_to_csv(rows: list[dict]) -> str:
    import io
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def read_rows(text: str) -> list[dict]:
    """CSV кампании → строки. Пустой текст — пустой список (ветки/файла ещё нет)."""
    if not text.strip():
        return []
    return list(csv.DictReader(text.splitlines()))


def campaign_max_days() -> int:
    """Лимит дней кампании. CAMPAIGN_MAX_DAYS в env — сознательное продление
    снаружи кода (владелец правит env в workflow и включает workflow заново)."""
    raw = os.environ.get(CAMPAIGN_MAX_DAYS_ENV, "")
    return int(raw) if raw.strip() else MAX_CAMPAIGN_DAYS


def is_us_business(moment: datetime) -> bool:
    """Американский рабочий день: пн–пт 13:00–24:00 UTC (см. константы выше)."""
    return moment.weekday() < 5 and 13 <= moment.hour < 24


def coverage(rows: list[dict], now: datetime | None = None,
             max_days: int = MAX_CAMPAIGN_DAYS) -> dict:
    """Критерий задачи #4 по накопленным строкам. Чистая функция."""
    now = now or datetime.now(timezone.utc)
    ok = [r for r in rows if r.get("status") == "ok"]
    starts = [sent_at_datetime(int(r["sent_at"])) for r in ok]
    span_h = 0.0
    if len(starts) >= 2:
        span_h = (max(starts) - min(starts)).total_seconds() / 3600
    business = sum(1 for s in starts if is_us_business(s))
    first = min(starts) if starts else None
    overdue = first is not None and (now - first) > timedelta(days=max_days)
    return {
        "ok_rows": len(ok), "total_rows": len(rows), "business_rows": business,
        "span_h": round(span_h, 1),
        "met": len(ok) >= MIN_ROWS and business >= MIN_BUSINESS_ROWS and span_h >= MIN_SPAN_HOURS,
        "overdue": overdue,
    }


def should_chain(cov: dict, workflow_on_main: bool) -> str:
    """Диспатчить ли следующую ступень цепочки тиков. Пустая строка — да; иначе
    человекочитаемая причина остановки (одна на все «нет», одно место правды)."""
    if not workflow_on_main:
        return (f"{WORKFLOW_FILE} отсутствует на main — кампания неактивна, "
                "цепочку не продолжаю (иначе красная цепочка зациклится)")
    if cov["met"]:
        return "покрытие достигнуто — финализация без новой ступени цепочки"
    if cov["overdue"]:
        return "лимит дней исчерпан — цепочка остановлена, дальше финализация"
    return ""


def finalize_outcome(cov: dict) -> dict:
    """Что финализация говорит наружу при данном покрытии — одно место правды.

    ready: переводить ли PR из черновика. Unmet-финализация обязана отличаться
    от успешной снаружи (ревью #108, находка 2): готовым PR с «не достигнут»
    оркестратор сольёт как поставку задачи, и провал кампании станет неотличим
    от успеха."""
    if cov["met"]:
        return {
            "ready": True,
            "verdict": "",
            "closing": "После мержа критерий задачи выполнен на main; закрой задачу "
                       "с уликами (ссылки на CSV и этот PR).",
        }
    return {
        "ready": False,
        "verdict": "Финализация по лимиту дней: критерий НЕ достигнут, кампания остановлена.",
        "closing": "Критерий задачи НЕ выполнен — задачу НЕ закрывать, PR остаётся "
                   "черновиком. Продолжить сбор: задай CAMPAIGN_MAX_DAYS в env шага "
                   "«Тик кампании» (сознательное продление) и "
                   "`gh workflow enable dispatch-latency-probe.yml`.",
    }


def coverage_markdown(cov: dict) -> str:
    return (f"Кампания: {cov['ok_rows']}/{MIN_ROWS} замеров · "
            f"бизнес-окно США {cov['business_rows']}/{MIN_BUSINESS_ROWS} · "
            f"разброс {cov['span_h']}/{MIN_SPAN_HOURS} ч · "
            f"{'✅ покрытие достигнуто' if cov['met'] else '⏳ идёт'}"
            + (" · ⚠️ лимит кампании исчерпан" if cov["overdue"] else ""))


def percentile(values: list[int], p: float) -> int:
    """Перцентиль как в базовом скрипте 2026-08-28 (nearest-rank, без интерполяции)."""
    ordered = sorted(values)
    return ordered[max(0, round(p * len(ordered)) - 1)]


def summarize(rows: list[dict]) -> str:
    """Сводка кампании в markdown — финальный текст между маркерами в доках."""
    ok = [r for r in rows if r.get("status") == "ok" and r.get("latency_ms")]
    timeouts = [r for r in rows if r.get("status") == "timeout"]
    failed = [r for r in rows if r.get("status") == "dispatch_failed"]
    if not ok:
        return ("**Успешных замеров нет** — кампания не набрала данных; "
                f"таймаутов {len(timeouts)}, сбоев диспатча {len(failed)}.")

    vals = [int(r["latency_ms"]) for r in ok]
    starts = sorted(sent_at_datetime(int(r["sent_at"])) for r in ok)
    qvals = [int(r["queue_ms"]) for r in ok if r.get("queue_ms")]
    business = [int(r["latency_ms"]) for r in ok
                if is_us_business(sent_at_datetime(int(r["sent_at"])))]
    off = [int(r["latency_ms"]) for r in ok
           if not is_us_business(sent_at_datetime(int(r["sent_at"])))]

    def fmt(ms: int) -> str:
        return f"{ms / 1000:.1f} с"

    lines = [
        f"**Замеров: {len(ok)}** (таймаутов {len(timeouts)}, сбоев диспатча "
        f"{len(failed)}), окно {min(starts).isoformat(timespec='minutes')} … "
        f"{max(starts).isoformat(timespec='minutes')} UTC.",
        "",
        f"- min {fmt(min(vals))} · медиана **{fmt(statistics.median(vals))}** · "
        f"p90 {fmt(percentile(vals, 0.9))} · p99 {fmt(percentile(vals, 0.99))} · "
        f"max {fmt(max(vals))} — метрика «диспатч отправлен → job начал выполняться»",
    ]
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(starts, starts[1:])]
    if gaps:
        lines.append(
            f"- интервалы между замерами: медиана {statistics.median(gaps):.0f} мин, "
            f"max {max(gaps) / 60:.1f} ч — сбор неравномерен по построению (каденция "
            "и её история — ADR 0005); плотные серии смещают доли времени суток, "
            "бакеты ниже показывают разброс по окнам")
    if qvals and max(qvals) == 0:
        lines.append(
            "- queue_ms = 0 во всех строках: этой схемой чистое ожидание раннера не "
            "измеряется — GitHub создаёт run в момент его старта (created == started "
            "до секунды); колонка оставлена схеме, где run существует до выдачи раннера")
    if qvals and max(qvals) > 0:
        lines.append(
            f"- чистое ожидание раннера (часы GitHub, без сетевого пути до API): "
            f"медиана {fmt(statistics.median(qvals))}, p99 {fmt(percentile(qvals, 0.99))}, "
            f"max {fmt(max(qvals))}")
    if business:
        lines.append(
            f"- американский рабочий день (пн–пт 13:00–24:00 UTC): медиана "
            f"{fmt(statistics.median(business))}, max {fmt(max(business))} "
            f"({len(business)} замеров)")
    if off:
        lines.append(
            f"- прочее время: медиана {fmt(statistics.median(off))}, "
            f"max {fmt(max(off))} ({len(off)} замеров)")
    worst = sorted(ok, key=lambda r: -int(r["latency_ms"]))[:5]
    lines.append("- худшие замеры: " + "; ".join(
        f"[{int(r['latency_ms']) / 1000:.0f} с]({r['run_url']})" for r in worst))
    lines.append(
        "- сравнение с базовыми 22 замерами 2026-08-28 (медиана 8.3 с, p90 10.8 с, "
        "max 17 с): та метрика считала до первого heartbeat и включала бутстрап job'а "
        "(единицы секунд); здесь — до первого шага job'а. Хвост распределения "
        "(минуты-часы) в обеих метриках одинаков по смыслу.")
    if timeouts:
        lines.append(
            f"**Таймаутов (>{TIMEOUT_S} с без старта): {len(timeouts)}** — "
            + ", ".join(sent_at_datetime(int(r["sent_at"])).isoformat(timespec="minutes")
                        for r in timeouts[-3:])
            + (" (последние)" if len(timeouts) > 3 else ""))
    if failed:
        lines.append(
            f"**Сбоев диспатча: {len(failed)}** — транспорт кампании ломался; "
            "эти строки в числовую сводку не входят.")
    return "\n".join(lines)


def splice_summary(doc: str, block: str) -> str:
    """Вставка сводки между маркерами. Нет маркеров — громкий отказ, не тихая правка."""
    if MARK_BEGIN not in doc or MARK_END not in doc:
        raise ValueError(f"маркеры {MARK_BEGIN}/{MARK_END} не найдены в документе — "
                         "некуда вставлять сводку кампании")
    head, rest = doc.split(MARK_BEGIN, 1)
    _, tail = rest.split(MARK_END, 1)
    return f"{head}{MARK_BEGIN}\n{block}\n{MARK_END}{tail}"


# ── GitHub API (тонкая обёртка, только транспорт) ─────────────────────────────────


class Github:
    def __init__(self, token: str, repo: str):
        if not token:
            raise RuntimeError("GH_PIPELINE_PAT не задан — кампании нечем авторизоваться")
        self.token = token
        self.repo = repo

    def request(self, method: str, path: str, body: dict | None = None) -> dict | list | None:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": UA,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                text = response.read().decode()
                return json.loads(text) if text.strip() else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path}: HTTP {error.code}: {detail}") from error

    def contents(self, path: str, ref: str) -> str | None:
        """Текст файла с ветки; None = файла нет. 404 — штатный случай, не ошибка."""
        try:
            blob = self.request("GET", f"/repos/{self.repo}/contents/{path}?ref={ref}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return None
            raise
        assert isinstance(blob, dict)
        return base64.b64decode(blob["content"]).decode()

    def run(self, run_id: str) -> dict:
        payload = self.request("GET", f"/repos/{self.repo}/actions/runs/{run_id}")
        assert isinstance(payload, dict)
        return payload

    def open_pulls_on_branch(self) -> list[dict]:
        owner = self.repo.split("/")[0]
        payload = self.request(
            "GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{DATA_BRANCH}")
        return payload or []


# ── Git-транспорт записи CSV (push с перезапуском на гонку ветки) ─────────────────


def git(*args: str, cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {proc.stderr.strip()[:300]}")
    return proc


def clone_data_branch(workdir: str) -> None:
    """Свежий клон под данные: main + ветка данных (клон job'а не трогаем)."""
    origin = origin_url()
    git("clone", "--quiet", "--no-tags", origin, workdir, cwd=".")
    git("fetch", "--quiet", "origin", DATA_BRANCH, cwd=workdir, check=False)
    exists = git("rev-parse", "--verify", "--quiet", f"origin/{DATA_BRANCH}",
                 cwd=workdir, check=False).returncode == 0
    base = f"origin/{DATA_BRANCH}" if exists else "origin/main"
    git("checkout", "--quiet", "-B", DATA_BRANCH, base, cwd=workdir)


def append_and_push(workdir: str, row: dict, commit_message: str) -> bool:
    """Строка в CSV + push. Гонка за ветку — сброс на свежую и повтор (до 5 раз).

    Возвращает True если строка записана; False если probe_id уже есть — дубль не
    пишется (таймаут-строка диспетчера опередила опоздавший job: старт позже
    TIMEOUT_S уже зафиксирован, тихое задвоение хуже).
    """
    csv_file = Path(workdir) / CSV_PATH
    # Граница формата: перенос строки внутри поля ломает read_rows при разборе.
    # Чистится здесь, у писателя, а не в call site'ах.
    row = {key: str(value).replace("\r", " ").replace("\n", " ")
           for key, value in row.items()}
    for _ in range(5):
        rows = read_rows(csv_file.read_text(encoding="utf-8")) if csv_file.exists() else []
        if any(r.get("probe_id") == row["probe_id"] for r in rows):
            print(f"probe_id {row['probe_id']} уже в CSV — строка не дублируется")
            return False
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        new_file = not csv_file.exists()
        with open(csv_file, "a", newline="", encoding="utf-8") as file:
            # lineterminator — общее место правды с rows_to_csv: иначе append-строки
            # получаются CRLF, а файл с main — LF.
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, lineterminator="\n")
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        git(*COMMIT_IDENTITY, "add", CSV_PATH, cwd=workdir)
        git(*COMMIT_IDENTITY, "commit", "--quiet", "-m", commit_message, cwd=workdir)
        if git("push", "--quiet", "origin", f"HEAD:{DATA_BRANCH}", cwd=workdir,
               check=False).returncode == 0:
            return True
        # Кто-то успел раньше: сбрасываемся на его версию и пробуем снова.
        git("rebase", "--abort", cwd=workdir, check=False)
        git("fetch", "--quiet", "origin", DATA_BRANCH, cwd=workdir, check=False)
        git("checkout", "--quiet", "-B", DATA_BRANCH, f"origin/{DATA_BRANCH}", cwd=workdir,
            check=False)
    raise RuntimeError(f"не удалось записать строку {row['probe_id']} на {DATA_BRANCH} "
                       "за 5 попыток")


# ── Команды ───────────────────────────────────────────────────────────────────────


def cmd_dispatch(_args: argparse.Namespace) -> int:
    gh = Github(os.environ.get("GH_PIPELINE_PAT", ""), required_env("GITHUB_REPOSITORY"))
    workdir = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "dispatch-tail")

    # Гвардия «204 — не доказательство запуска»: workflow-файл обязан быть на main
    # (docs/research/21-github-actions.md, ловушка default-branch).
    if gh.contents(WORKFLOW_FILE, "main") is None:
        print(f"::error::{WORKFLOW_FILE} отсутствует на main — dispatch вернул бы 204 "
              "без запуска. Кампания неактивна до мержа.")
        return 1

    cov = coverage(read_rows(gh.contents(CSV_PATH, DATA_BRANCH) or ""),
                   max_days=campaign_max_days())
    print(coverage_markdown(cov))
    if cov["met"] or cov["overdue"]:
        # Предохранитель обязан работать с обеих сторон: если record-путь мёртв
        # (PAT истёк, инцидент Actions), тик всё равно финализирует по лимиту дней —
        # иначе кампания жжёт раны до 60-дневной смерти cron.
        print("Покрытие достигнуто или истёк лимит дней — финализирую (самозапирание).")
        return cmd_finalize(argparse.Namespace())

    probe_id = str(uuid.uuid4())
    sent_at = int(time.time() * 1000)
    try:
        gh.request("POST", f"/repos/{gh.repo}/dispatches", {
            "event_type": DISPATCH_EVENT_TYPE,
            "client_payload": {"probe_id": probe_id, "sent_at": sent_at},
        })
    except RuntimeError as error:
        # Транспорт сломан (права, лимит, сеть) — это не молчание: красный тик + строка.
        note = str(error)[:200]
        clone_data_branch(workdir)
        append_and_push(workdir, dispatch_failed_row(probe_id, sent_at, note),
                        f"probe {probe_id}: dispatch_failed")
        print(f"::error::dispatch отклонён — строка dispatch_failed записана: {note}")
        return 1

    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        text = gh.contents(CSV_PATH, DATA_BRANCH)
        if text and any(r.get("probe_id") == probe_id for r in read_rows(text)):
            waited = TIMEOUT_S - int(deadline - time.monotonic())
            print(f"probe {probe_id}: job стартовал и записал себя (ожидал {waited} с)")
            return 0

    # Job не стартовал и не записался за TIMEOUT_S — таймаут-строка от диспетчера.
    clone_data_branch(workdir)
    if append_and_push(workdir, timeout_row(probe_id, sent_at),
                       f"probe {probe_id}: timeout {TIMEOUT_S}s"):
        print(f"probe {probe_id}: ТАЙМАУТ {TIMEOUT_S} с — хвост пойман, строка записана")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    probe_id = required_env("PROBE_ID")
    sent_at = int(required_env("SENT_AT_MS"))
    run_id = required_env("GITHUB_RUN_ID")
    gh = Github(os.environ.get("GH_PIPELINE_PAT", ""), required_env("GITHUB_REPOSITORY"))
    workdir = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "dispatch-tail-data")

    run = gh.run(run_id)
    if not run.get("run_started_at"):
        print(f"::error::run {run_id} без run_started_at — метрику считать нечем")
        return 1
    row = compute_row(probe_id, sent_at, run)
    print(f"probe {probe_id}: latency {int(row['latency_ms']) / 1000:.1f} с, "
          f"queue {int(row['queue_ms']) / 1000:.1f} с, run {row['run_url']}")

    clone_data_branch(workdir)
    wrote = append_and_push(workdir, row, f"probe {probe_id}: {row['latency_ms']} ms")
    ensure_draft_pr(gh)

    cov = coverage(read_rows(gh.contents(CSV_PATH, DATA_BRANCH) or ""),
                   max_days=campaign_max_days())
    print(coverage_markdown(cov))
    if wrote and (cov["met"] or cov["overdue"]) and not args.no_finalize:
        return cmd_finalize(argparse.Namespace())
    return 0


def cmd_chain(_args: argparse.Namespace) -> int:
    """Следующая ступень цепочки тиков. Всегда()-шаг тика: живёт и после сбоя
    тика (один сбой не должен ронять кампанию до ближайшего cron-страхового),
    но не зацикливает красное: при неактивной кампании громко отказывается и
    выходит 0. Ограничитель темпа — CHAIN_DELAY_S и сериализация тиков."""
    gh = Github(os.environ.get("GH_DISPATCH_TOKEN", ""), required_env("GITHUB_REPOSITORY"))
    workflow_on_main = gh.contents(WORKFLOW_FILE, "main") is not None
    cov = coverage(read_rows(gh.contents(CSV_PATH, DATA_BRANCH) or ""),
                   max_days=campaign_max_days())
    reason = should_chain(cov, workflow_on_main)
    if reason:
        print(reason)
        return 0
    time.sleep(CHAIN_DELAY_S)
    gh.request("POST", f"/repos/{gh.repo}/actions/workflows/"
                       f"{WORKFLOW_FILE.split('/')[-1]}/dispatches", {"ref": "main"})
    print(f"следующая ступень цепочки задиспатчена (пауза {CHAIN_DELAY_S} с)")
    return 0


def cmd_tick_failed(_args: argparse.Namespace) -> int:
    """След тика, умершего до POST /dispatches: строка dispatch_failed с note.
    Доставку schedule это не ловит (ран не создан — ловить нечем, см. замер
    schedule в 21-github-actions.md), ловит смерть начавшегося тика. Лучшее
    усилие: без токена строка не запишется — останется красный job (класс
    «молча деградировало до только ok» держит гвардия-тест на GH_TOKEN)."""
    run_id = required_env("GITHUB_RUN_ID")
    sent_at = int(time.time() * 1000)
    gh = Github(os.environ.get("GH_DISPATCH_TOKEN", ""), required_env("GITHUB_REPOSITORY"))
    workdir = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "dispatch-tail-tickfail")
    note = (f"тик умер до диспатча: run {run_id}, "
            f"{os.environ.get('TICK_NOTE', 'шаг тика завершился ошибкой')}")
    clone_data_branch(workdir)
    wrote = append_and_push(workdir, dispatch_failed_row(f"tick-{run_id}", sent_at, note),
                            f"tick {run_id}: dispatch_failed (умер до POST /dispatches)")
    print(f"след тика {run_id} записан: {wrote}")
    return 0


def ensure_draft_pr(gh: Github) -> None:
    """Живой черновик-PR на ветке данных: держит задачу #4 «в работе» в протоколе
    (назначение не протухает за 24 ч) и виден как дашборд кампании. Идемпотентно."""
    if gh.open_pulls_on_branch():
        return
    body = (
        "#4\n\n"
        "## Что здесь\n\nДанные кампании замера хвоста задержки `repository_dispatch → "
        "старт job'а`. Черновик живёт, пока кампания набирает покрытие: каждый замер — "
        "коммит в CSV. При достижении критерия (100+ замеров, включая американский "
        "рабочий день, разброс ≥24 ч) кампания сама допишет сводку в "
        "`docs/research/99-open-questions.md`, переведёт PR в готовность и оставит "
        "комментарий в задаче.\n\nИнфраструктура кампании — отдельный уже смерженный PR; "
        "этот PR содержит только данные и сводку."
    )
    gh.request("POST", f"/repos/{gh.repo}/pulls", {
        "title": PR_TITLE, "head": DATA_BRANCH, "base": "main",
        "body": body, "draft": True, "maintainer_can_modify": False,
    })
    print(f"черновик-PR на {DATA_BRANCH} создан")


def cmd_finalize(_args: argparse.Namespace) -> int:
    """Финал кампании: сводка в доку, PR в готовность, комментарий в задачу,
    workflow выключен. Вызывается автоматически при покрытии или по лимиту дней."""
    gh = Github(os.environ.get("GH_PIPELINE_PAT", ""), required_env("GITHUB_REPOSITORY"))
    workdir = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "dispatch-tail-final")
    clone_data_branch(workdir)

    rows = read_rows((Path(workdir) / CSV_PATH).read_text(encoding="utf-8"))
    cov = coverage(rows, max_days=campaign_max_days())
    outcome = finalize_outcome(cov)
    block = (summarize(rows) + "\n\n" + coverage_markdown(cov)
             + (f". ⚠️ {outcome['verdict']}" if outcome["verdict"] else ""))

    doc_path = Path(workdir) / "docs/research/99-open-questions.md"
    doc_path.write_text(splice_summary(doc_path.read_text(encoding="utf-8"), block),
                        encoding="utf-8")
    git(*COMMIT_IDENTITY, "add", "docs/research/99-open-questions.md", cwd=workdir)
    # Идемпотентность: гонка двух финализаций (record-путь и тик) даёт одинаковый
    # блок — пустой коммит не ошибка, а признак «сводка уже записана». Остальной
    # отказ git'а — громкий сбой, не тишина.
    committed = git(*COMMIT_IDENTITY, "commit", "--quiet", "-m",
                    f"dispatch-tail: сводка кампании ({cov['ok_rows']} замеров, задача #4)",
                    cwd=workdir, check=False)
    if committed.returncode != 0 and "nothing to commit" not in (
            committed.stdout + committed.stderr).lower():
        raise RuntimeError(f"git commit сводки: {committed.stderr.strip()[:300]}")
    git("fetch", "--quiet", "origin", "main", DATA_BRANCH, cwd=workdir)
    git("rebase", "--autostash", "origin/main", cwd=workdir)
    # Редкая гонка двух финализаций: чужой коммит на ветке данных не должен
    # ронять наш — перебазируемся и пушим снова (до 3 раз), дальше громкий отказ.
    for _ in range(3):
        if git("push", "--quiet", "origin", f"HEAD:{DATA_BRANCH}", cwd=workdir,
               check=False).returncode == 0:
            break
        git("rebase", "--autostash", f"origin/{DATA_BRANCH}", cwd=workdir, check=False)
        git("fetch", "--quiet", "origin", DATA_BRANCH, cwd=workdir, check=False)
    else:
        raise RuntimeError("push финального коммита на ветку данных не прошёл за 3 попытки")

    for pull in gh.open_pulls_on_branch():
        if pull.get("draft") and outcome["ready"]:
            ready = subprocess.run(["gh", "pr", "ready", str(pull["number"])],
                                   capture_output=True, text=True)
            if ready.returncode != 0:
                print(f"::warning::gh pr ready упал: {ready.stderr.strip()[:200]} — "
                      "переведи PR в готовность руками")
        elif not outcome["ready"]:
            print(f"::warning::PR {pull['number']} остаётся черновиком: "
                  f"{outcome['verdict']}")
        comment = (f"🤖 Кампания замера хвоста завершена: {coverage_markdown(cov)}\n\n"
                   f"{summarize(rows)}\n\nДанные: `{CSV_PATH}` на ветке `{DATA_BRANCH}` "
                   f"(PR {pull['html_url']}). {outcome['closing']}")
        gh.request("POST", f"/repos/{gh.repo}/issues/{ISSUE_NUMBER}/comments",
                   {"body": comment})
        break
    else:
        print("::warning::открытого PR на ветке данных нет — комментарий не оставлен; "
              "открой PR из ветки данных руками")

    gh.request("PUT", f"/repos/{gh.repo}/actions/workflows/"
                      f"{WORKFLOW_FILE.split('/')[-1]}/disable")
    print("Кампания финализирована, workflow отключён (включить: gh workflow enable).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rows = read_rows(Path(args.csv).read_text(encoding="utf-8"))
    print(coverage_markdown(coverage(rows)))
    if rows:
        print()
        print(summarize(rows))
    return 0


# ── Мелкая обвязка ────────────────────────────────────────────────────────────────

DISPATCH_EVENT_TYPE = "dispatch-latency-probe"


def origin_url() -> str:
    return f"https://github.com/{os.environ['GITHUB_REPOSITORY']}.git"


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"env {name} обязателен для этой команды")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("dispatch", help="тик кампании: отправить замер и посторожить таймаут")
    rec = sub.add_parser("record", help="записать строку своего run'а (сторона probe job'а)")
    rec.add_argument("--no-finalize", action="store_true",
                     help="не финализировать кампанию при достижении покрытия")
    sub.add_parser("chain", help="диспатчнуть следующую ступень цепочки тиков (всегда()-шаг)")
    sub.add_parser("tick_failed", help="записать след тика, умершего до диспатча (failure()-шаг)")
    sub.add_parser("finalize", help="завершить кампанию: сводка, PR, комментарий")
    sta = sub.add_parser("status", help="покрытие и сводка по локальному CSV")
    sta.add_argument("--csv", default=CSV_PATH, help="путь до CSV (по умолчанию — кампания)")
    args = parser.parse_args()
    return {
        "dispatch": cmd_dispatch, "record": cmd_record, "chain": cmd_chain,
        "tick_failed": cmd_tick_failed, "finalize": cmd_finalize, "status": cmd_status,
    }[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
