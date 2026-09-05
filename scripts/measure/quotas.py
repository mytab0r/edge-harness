#!/usr/bin/env python3
"""Срез квот и лимитов, на которых стоит харнес (issue #324).

Повод: 2026-09-03 систему молча остановил дневной лимит Durable Objects
(5 000 000 rows_read) — факт не был даже задокументирован (см.
docs/research/20-cloudflare-free.md и #320 — тот чинит горячий путь одного
full-scan, не сборщик метрик). Это — общий сборщик: печатает таблицу
«ресурс — значение — лимит — % — сброс» по ВСЕМ источникам, чтобы решения
принимались по числу, а не по гаданию между версиями.

Источники: Cloudflare GraphQL Analytics (Workers requests, DO rows_read/
written, DO storage; CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID), GitHub REST
(rate_limit + приближения к 500/час и concurrency 20; GH_TOKEN), LLM-провайдер
(z.ai/GLM — см. NO_PROVIDER_QUOTA_API: нет данных и почему).

Имя GraphQL-поля для DO rows_read/rows_written Cloudflare нигде не публикует
(в отличие от D1, где есть таблица «GraphQL Field Name») — сама документация
DO отсылает к интроспекции для таких случаев. Инструмент СНАЧАЛА
интроспектирует схему и ищет поле сам, не находит → «нет данных» с причиной,
а не тихий пропуск и не выдумка.

Лимиты — одно место правды здесь (LIMITS), значения совпадают с
docs/research/20-cloudflare-free.md и docs/research/21-github-actions.md.

Запуск: python scripts/measure/quotas.py
Тесты:  python -m pytest scripts/measure/test_quotas.py -q
"""

import importlib.util
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# escalate/WATCHDOG_ISSUE — общий канал «поломка/порог → задача-статус +
# Telegram» (#120/#174, см. scripts/orchestra/pulse_guard.py). Не заводим
# второй канал сигнала для того же класса «метрика перевалила за порог».
_PG_PATH = Path(__file__).resolve().parents[1] / "orchestra" / "pulse_guard.py"
_pg_spec = importlib.util.spec_from_file_location("pulse_guard", _PG_PATH)
pulse_guard = importlib.util.module_from_spec(_pg_spec)
_pg_spec.loader.exec_module(pulse_guard)  # type: ignore[union-attr]

CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
THRESHOLD_PCT = 80.0

# Один источник правды на числа лимитов — цитаты и даты проверки живут в
# docs/research/20-cloudflare-free.md («Durable Objects на Free», «Общие
# лимиты Workers») и docs/research/21-github-actions.md («API-лимиты»).
LIMITS = {
    "cf_workers_requests_day": 100_000,        # Workers Free: requests/day
    "cf_do_rows_read_day": 5_000_000,          # DO Free: rows_read/day — тот самый инцидент
    "cf_do_rows_written_day": 100_000,         # DO Free: rows_written/day
    "cf_do_storage_account_bytes": 5 * 1024 ** 3,   # DO Free: 5 GB на аккаунт
    "gh_dispatch_hour": 500,                   # вторичный лимит content-generating/час
    "gh_concurrent_jobs": 20,                  # Free: одновременные jobs (аккаунт, не репо)
}


@dataclass
class Row:
    resource: str
    source: str
    current: float | None
    limit: float | None
    unit: str
    reset: str
    status: str          # "ok" | "no-data"
    note: str = ""

    @property
    def pct(self) -> float | None:
        if self.status != "ok" or self.current is None or not self.limit:
            return None
        return round(100.0 * self.current / self.limit, 1)


def no_data(resource: str, source: str, limit: float | None, unit: str, reason: str) -> Row:
    return Row(resource, source, None, limit, unit, "-", "no-data", reason)


# ── Cloudflare GraphQL: клиент + интроспекция ────────────────────────────────


def cf_query(token: str, query: str, variables: dict | None = None) -> dict:
    """POST к GraphQL Analytics API. Бросает RuntimeError на транспортную
    ошибку и на errors в теле — вызывающий код решает, как это показать."""
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        CF_GRAPHQL_URL, data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as error:
        raise RuntimeError(f"сеть недоступна: {error}")
    if body.get("errors"):
        raise RuntimeError("; ".join(e.get("message", str(e)) for e in body["errors"]))
    return body["data"]


def cf_type_names(token: str) -> list[str]:
    """Имена всех типов схемы — один недорогой запрос (только имена, без
    полей), выполняется один раз и переиспользуется для поиска нужных типов."""
    data = cf_query(token, "{ __schema { types { name } } }")
    return [t["name"] for t in data["__schema"]["types"]]


def cf_type_fields(token: str, type_name: str) -> list[str]:
    data = cf_query(token, f'{{ __type(name: "{type_name}") {{ fields {{ name }} }} }}')
    t = data.get("__type")
    return [f["name"] for f in t["fields"]] if t else []


def find_row_metric(token: str, type_names: list[str], keyword: str) -> tuple[str, str] | None:
    """Ищет поле, содержащее keyword (например 'rowsread'), среди Sum/Max
    типов датасетов Durable Objects — без предположения, в каком именно из
    четырёх датасетов (durableObjectsInvocationsAdaptiveGroups/PeriodicGroups/
    StorageGroups/SubrequestsAdaptiveGroups) оно живёт."""
    candidates = [
        n for n in type_names
        if "durableobjects" in n.lower() and n.lower().endswith(("sum", "max"))
    ]
    for type_name in candidates:
        fields = cf_type_fields(token, type_name)
        for f in fields:
            if keyword in f.lower():
                return type_name, f
    return None


def utc_day_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


def collect_cloudflare(account_id: str, token: str) -> list[Row]:
    source = "Cloudflare GraphQL Analytics"
    reset = "00:00 UTC (следующие сутки)"
    day_start = utc_day_start()

    try:
        type_names = cf_type_names(token)
    except RuntimeError as error:
        reason = f"интроспекция схемы не удалась: {error}"
        return [
            no_data("Workers requests/сутки", source, LIMITS["cf_workers_requests_day"], "requests", reason),
            no_data("DO rows_read/сутки", source, LIMITS["cf_do_rows_read_day"], "rows", reason),
            no_data("DO rows_written/сутки", source, LIMITS["cf_do_rows_written_day"], "rows", reason),
            no_data("DO storage/аккаунт", source, LIMITS["cf_do_storage_account_bytes"], "bytes", reason),
        ]

    rows: list[Row] = []

    # Workers requests — поля подтверждены доком дословно (workersInvocationsAdaptive
    # sum{requests,errors,subrequests}), интроспекция не нужна.
    try:
        data = cf_query(
            token,
            """query($accountTag: string, $start: string) {
                viewer { accounts(filter: {accountTag: $accountTag}) {
                    workersInvocationsAdaptive(limit: 1, filter: {datetime_geq: $start}) {
                        sum { requests }
                    }
                } }
            }""",
            {"accountTag": account_id, "start": day_start},
        )
        accounts = data["viewer"]["accounts"]
        total = sum(item["sum"]["requests"] for acc in accounts for item in acc["workersInvocationsAdaptive"])
        rows.append(Row("Workers requests/сутки", source, total, LIMITS["cf_workers_requests_day"], "requests", reset, "ok"))
    except (RuntimeError, KeyError, TypeError) as error:
        rows.append(no_data("Workers requests/сутки", source, LIMITS["cf_workers_requests_day"], "requests", str(error)))

    # DO storage — поле storedBytes подтверждено доком дословно.
    try:
        data = cf_query(
            token,
            """query($accountTag: string) {
                viewer { accounts(filter: {accountTag: $accountTag}) {
                    durableObjectsStorageGroups(limit: 1, orderBy: [date_DESC]) {
                        max { storedBytes }
                    }
                } }
            }""",
            {"accountTag": account_id},
        )
        accounts = data["viewer"]["accounts"]
        values = [item["max"]["storedBytes"] for acc in accounts for item in acc["durableObjectsStorageGroups"]]
        current = max(values) if values else 0
        rows.append(Row("DO storage/аккаунт", source, current, LIMITS["cf_do_storage_account_bytes"], "bytes", "-", "ok"))
    except (RuntimeError, KeyError, TypeError) as error:
        rows.append(no_data("DO storage/аккаунт", source, LIMITS["cf_do_storage_account_bytes"], "bytes", str(error)))

    # DO rows_read/rows_written — имя поля не задокументировано, ищем интроспекцией.
    for label, limit_key, keyword in (
        ("DO rows_read/сутки", "cf_do_rows_read_day", "rowsread"),
        ("DO rows_written/сутки", "cf_do_rows_written_day", "rowswritten"),
    ):
        found = find_row_metric(token, type_names, keyword)
        if found is None:
            rows.append(no_data(
                label, source, LIMITS[limit_key], "rows",
                f"поле, содержащее '{keyword}', не найдено ни в одном Sum/Max-типе "
                "датасетов Durable Objects через интроспекцию схемы — метрика, "
                "возможно, не экспонируется через GraphQL Analytics (инцидент "
                "#320 был замечен по письму Cloudflare, не по дашборду/API)",
            ))
            continue
        type_name, field_name = found
        try:
            group_field = next(
                n for n in ("durableObjectsInvocationsAdaptiveGroups", "durableObjectsPeriodicGroups",
                             "durableObjectsStorageGroups", "durableObjectsSubrequestsAdaptiveGroups")
                if n.lower() in type_name.lower()
            )
            data = cf_query(
                token,
                f"""query($accountTag: string, $start: string) {{
                    viewer {{ accounts(filter: {{accountTag: $accountTag}}) {{
                        {group_field}(limit: 1000, filter: {{date_geq: $start}}) {{
                            sum {{ {field_name} }}
                        }}
                    }} }}
                }}""",
                {"accountTag": account_id, "start": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            )
            accounts = data["viewer"]["accounts"]
            total = sum(item["sum"][field_name] for acc in accounts for item in acc[group_field])
            rows.append(Row(label, source, total, LIMITS[limit_key], "rows", reset, "ok", f"поле {field_name} в {type_name}"))
        except (RuntimeError, KeyError, TypeError) as error:
            rows.append(no_data(label, source, LIMITS[limit_key], "rows",
                                 f"поле {field_name} найдено интроспекцией ({type_name}), "
                                 f"но запрос данных упал: {error}"))

    return rows


# ── GitHub REST ──────────────────────────────────────────────────────────────


def gh_api(*args: str) -> dict | list | None:
    # encoding явный: иначе Windows читает вывод в кодировке консоли, а
    # реальные ответы GitHub несут кириллицу (найдено живым прогоном 2026-09-05).
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True,
                             encoding="utf-8", env={**os.environ, "NO_COLOR": "1"})
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api {args} завершился с кодом {result.returncode}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def collect_github(repo: str) -> list[Row]:
    source = "GitHub REST"
    rows: list[Row] = []

    try:
        data = gh_api("rate_limit")
        core = data["resources"]["core"]
        rows.append(Row("GitHub REST rate limit (PAT/GITHUB_TOKEN)", source,
                         core["used"], core["limit"], "requests/час",
                         datetime.fromtimestamp(core["reset"], tz=timezone.utc).isoformat(), "ok"))
        graphql = data["resources"]["graphql"]
        rows.append(Row("GitHub GraphQL rate limit", source,
                         graphql["used"], graphql["limit"], "points/час",
                         datetime.fromtimestamp(graphql["reset"], tz=timezone.utc).isoformat(), "ok"))
    except (RuntimeError, KeyError) as error:
        rows.append(no_data("GitHub REST rate limit (PAT/GITHUB_TOKEN)", source, None, "requests/час", str(error)))
        rows.append(no_data("GitHub GraphQL rate limit", source, None, "points/час", str(error)))

    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dispatch_count = 0
        for event in ("repository_dispatch", "workflow_dispatch"):
            # --method GET обязателен: с -f полями gh api иначе уходит в POST → 404.
            data = gh_api("--method", "GET", f"repos/{repo}/actions/runs", "-f", f"event={event}",
                          "-f", f"created=>={since}", "-f", "per_page=100")
            dispatch_count += data.get("total_count", 0)
        rows.append(Row(
            "Диспатчи этого репо/час (приближение к вторичному лимиту 500/час, аккаунт-wide)",
            source, dispatch_count, LIMITS["gh_dispatch_hour"], "runs/час", "скользящее окно", "ok",
            "приближение по repository_dispatch+workflow_dispatch ЭТОГО репозитория за час; "
            "настоящий лимит 500/час общий на аккаунт/приложение (docs/research/21-github-actions.md)",
        ))
    except RuntimeError as error:
        rows.append(no_data("Диспатчи этого репо/час", source, LIMITS["gh_dispatch_hour"], "runs/час", str(error)))

    try:
        data = gh_api("--method", "GET", f"repos/{repo}/actions/runs", "-f", "status=in_progress", "-f", "per_page=100")
        in_progress = data.get("total_count", 0)
        rows.append(Row(
            "In-progress workflow runs этого репо (приближение к concurrency 20, аккаунт-wide)",
            source, in_progress, LIMITS["gh_concurrent_jobs"], "jobs", "-", "ok",
            "приближение по одному репозиторию; лимит 20 общий на весь аккаунт",
        ))
    except RuntimeError as error:
        rows.append(no_data("In-progress workflow runs этого репо", source, LIMITS["gh_concurrent_jobs"], "jobs", str(error)))

    rows.append(no_data(
        "Actions минуты (billing)", source, None, "минут/мес",
        "public repo + standard runners = бесплатно и безлимитно "
        "(docs/research/21-github-actions.md, «Бесплатность»); учитывать нечего",
    ))

    return rows


# ── LLM-провайдер ─────────────────────────────────────────────────────────────
# Имя/URL текущего провайдера — одно место правды vars.DEEPSEEK_BASE_URL
# (гвардия #153, scripts/lib/test/provider-default.guard.sh), сюда не зашиваем:
# читаем из окружения, а не из строкового литерала.


def provider_no_quota_api_reason() -> str:
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "не задан в окружении")
    return (
        f"Провайдер по vars.DEEPSEEK_BASE_URL ({base_url}) не публикует "
        "документированный REST-эндпоинт остатка квоты (проверено 2026-09-05 "
        "для z.ai: docs.z.ai — SPA без серверной отдачи страниц API-reference, "
        "запрос по догадке .../usage не отвечает содержательно). Единственный "
        "подтверждённый сигнал — строка 'RATE_LIMIT: ... reset at <дата>' в "
        "stderr ответа модели, уже перехватывается в "
        "docs/runbooks/switch-llm-provider.md."
    )


def collect_provider() -> list[Row]:
    return [no_data("LLM-провайдер квота", "нет API", None, "-", provider_no_quota_api_reason())]


# ── Вывод и порог ─────────────────────────────────────────────────────────────


def format_table(rows: list[Row]) -> str:
    header = ("Ресурс", "Источник", "Значение", "Лимит", "%", "Сброс", "Заметка")
    lines = [header]
    for row in rows:
        current = "нет данных" if row.status == "no-data" else f"{row.current:,}".replace(",", " ")
        limit = "-" if row.limit is None else f"{row.limit:,}".replace(",", " ")
        pct = "-" if row.pct is None else f"{row.pct}%"
        lines.append((row.resource, row.source, current, limit, pct, row.reset, row.note))
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(header))]
    out = []
    for i, line in enumerate(lines):
        out.append(" | ".join(str(cell).ljust(widths[j]) for j, cell in enumerate(line)))
        if i == 0:
            out.append("-+-".join("-" * w for w in widths))
    return "\n".join(out)


def over_threshold(rows: list[Row], threshold: float = THRESHOLD_PCT) -> list[Row]:
    return [r for r in rows if r.pct is not None and r.pct >= threshold]


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "mytab0r/edge-harness")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    token = os.environ.get("CLOUDFLARE_API_TOKEN")

    if account_id and token:
        cf_rows = collect_cloudflare(account_id, token)
    else:
        reason = "CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы в окружении"
        cf_rows = [
            no_data("Workers requests/сутки", "Cloudflare GraphQL Analytics", LIMITS["cf_workers_requests_day"], "requests", reason),
            no_data("DO rows_read/сутки", "Cloudflare GraphQL Analytics", LIMITS["cf_do_rows_read_day"], "rows", reason),
            no_data("DO rows_written/сутки", "Cloudflare GraphQL Analytics", LIMITS["cf_do_rows_written_day"], "rows", reason),
            no_data("DO storage/аккаунт", "Cloudflare GraphQL Analytics", LIMITS["cf_do_storage_account_bytes"], "bytes", reason),
        ]

    rows = cf_rows + collect_github(repo) + collect_provider()

    print(format_table(rows))
    print()

    breached = over_threshold(rows)
    no_data_rows = [r for r in rows if r.status == "no-data"]
    if no_data_rows:
        print(f"::warning::{len(no_data_rows)} источник(ов) без данных — см. колонку «Заметка» выше")

    if breached:
        text = "🚨 Квота харнеса перевалила за {}%:\n".format(THRESHOLD_PCT) + "\n".join(
            f"- {r.resource}: {r.current:,} / {r.limit:,} ({r.pct}%)".replace(",", " ") for r in breached
        )
        print(f"::warning::{text}")
        result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
        print(result)
    else:
        print(f"Порог {THRESHOLD_PCT}% не превышен ни по одному измеренному ресурсу.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
