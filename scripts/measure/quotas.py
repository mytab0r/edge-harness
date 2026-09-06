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
Исключение: rows_read/day берётся из do_rows_read.DAILY_LIMIT — то место
правды для этого числа уже существовало с #320, второй копией здесь не
заводим (находка AI-ревью PR #327).

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

# check_not_truncated — тот же класс труncации group-ответов Cloudflare, что
# уже задокументирован и огорожен в do_rows_read.py (research/20, «Cloudflare
# режет group-ответы на limit строк без явного маркера») — переиспользуем
# готовую гвардию, не заводим второе место правды про тот же класс отказа
# (находка AI-ревью PR #327, третий раунд).
_DRR_PATH = Path(__file__).with_name("do_rows_read.py")
_drr_spec = importlib.util.spec_from_file_location("do_rows_read", _DRR_PATH)
do_rows_read = importlib.util.module_from_spec(_drr_spec)
_drr_spec.loader.exec_module(do_rows_read)  # type: ignore[union-attr]

CF_GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
THRESHOLD_PCT = 80.0

# Один источник правды на числа лимитов — цитаты и даты проверки живут в
# docs/research/20-cloudflare-free.md («Durable Objects на Free», «Общие
# лимиты Workers») и docs/research/21-github-actions.md («API-лимиты»).
LIMITS = {
    "cf_workers_requests_day": 100_000,        # Workers Free: requests/day
    "cf_do_rows_read_day": do_rows_read.DAILY_LIMIT,  # DO Free: rows_read/day — тот самый инцидент (#320, одно место правды)
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
    # sum{requests,errors,subrequests}), интроспекция не нужна. Это adaptive-
    # groups датасет, каждая строка ответа — ОДНА группа измерений (дата/
    # минута, scriptName, status), limit режет группы, не строки данных
    # (находка ревью PR #327): limit: 1 брал бы запросы одной случайной группы
    # вместо всех суток — тихий undercount, порог 80% молча не сработал бы.
    # limit: 10000 — тот же порядок, что в цитируемом тут же туториале Cloudflare.
    try:
        data = cf_query(
            token,
            """query($accountTag: string, $start: string) {
                viewer { accounts(filter: {accountTag: $accountTag}) {
                    workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $start}) {
                        sum { requests }
                    }
                } }
            }""",
            {"accountTag": account_id, "start": day_start},
        )
        accounts = do_rows_read.require_accounts(data["viewer"]["accounts"])
        items = [item for acc in accounts for item in acc["workersInvocationsAdaptive"]]
        # Гвардия обрезки (находка AI-ревью PR #327, третий раунд): CF режет
        # group-ответы на limit БЕЗ маркера — see do_rows_read.check_not_truncated.
        do_rows_read.check_not_truncated(items, limit=10000)
        total = sum(item["sum"]["requests"] for item in items)
        rows.append(Row("Workers requests/сутки", source, total, LIMITS["cf_workers_requests_day"], "requests", reset, "ok"))
    except (RuntimeError, KeyError, TypeError) as error:
        rows.append(no_data("Workers requests/сутки", source, LIMITS["cf_workers_requests_day"], "requests", str(error)))

    # DO storage — поле storedBytes подтверждено доком дословно. limit: 1
    # (та же находка, что выше): storage-группы разбиты по namespaceId, на
    # аккаунте их несколько (#322) — limit: 1 брал бы один произвольный
    # namespace вместо аккаунта целиком. dimensions.date нужен, чтобы взять
    # ПОСЛЕДНЮЮ дату и просуммировать storedBytes по всем namespace именно
    # этой даты, а не смешать даты между собой.
    try:
        data = cf_query(
            token,
            """query($accountTag: string) {
                viewer { accounts(filter: {accountTag: $accountTag}) {
                    durableObjectsStorageGroups(limit: 10000, orderBy: [date_DESC]) {
                        dimensions { date }
                        max { storedBytes }
                    }
                } }
            }""",
            {"accountTag": account_id},
        )
        accounts = do_rows_read.require_accounts(data["viewer"]["accounts"])
        items = [item for acc in accounts for item in acc["durableObjectsStorageGroups"]]
        do_rows_read.check_not_truncated(items, limit=10000)
        if items:
            latest_date = items[0]["dimensions"]["date"]  # orderBy date_DESC — первые строки самые свежие
            current = sum(item["max"]["storedBytes"] for item in items if item["dimensions"]["date"] == latest_date)
        else:
            current = 0
        rows.append(Row("DO storage/аккаунт", source, current, LIMITS["cf_do_storage_account_bytes"], "bytes", "-", "ok"))
    except (RuntimeError, KeyError, TypeError) as error:
        rows.append(no_data("DO storage/аккаунт", source, LIMITS["cf_do_storage_account_bytes"], "bytes", str(error)))

    # DO rows_read/rows_written — имя поля не задокументировано, ищем интроспекцией.
    # Поиск (find_row_metric → cf_type_fields → cf_query) — тоже сетевой вызов и
    # тоже может упасть транзиентно; весь блок для строки в одном try, чтобы
    # сбой поиска одной метрики не убивал main() и уже собранные строки выше
    # (найдено исполнением: 500 на __type-запросе валил RuntimeError наружу).
    for label, limit_key, keyword in (
        ("DO rows_read/сутки", "cf_do_rows_read_day", "rowsread"),
        ("DO rows_written/сутки", "cf_do_rows_written_day", "rowswritten"),
    ):
        try:
            found = find_row_metric(token, type_names, keyword)
            if found is None:
                rows.append(no_data(
                    label, source, LIMITS[limit_key], "rows",
                    f"поле, содержащее '{keyword}', не найдено ни в одном Sum/Max-типе "
                    "датасетов Durable Objects через интроспекцию схемы В ЭТОМ ПРОГОНЕ "
                    "(находка AI-ревью PR #327, третий раунд: метрика экспонируется — "
                    "см. docs/research/20-cloudflare-free.md, «Замер факта: rows_read "
                    "в проде», найдена интроспекцией и снята живым прогоном 2026-09-05 — "
                    "если этот прогон её не находит, вероятнее временный сбой "
                    "интроспекции или дрейф схемы, а не отсутствие метрики)",
                ))
                continue
            type_name, field_name = found
            # Агрегация выбирается по фактическому суффиксу типа (Sum/Max), а не
            # захардкожена как "sum" — find_row_metric ищет и в Max-типах тоже,
            # и запрос с "sum" на Max-типе не пройдёт валидацию GraphQL-схемы.
            agg = "max" if type_name.lower().endswith("max") else "sum"
            group_field = next(
                n for n in ("durableObjectsInvocationsAdaptiveGroups", "durableObjectsPeriodicGroups",
                             "durableObjectsStorageGroups", "durableObjectsSubrequestsAdaptiveGroups")
                if n.lower() in type_name.lower()
            )
            data = cf_query(
                token,
                # $start: Date — не string (находка AI-ревью PR #327, третий
                # раунд): тот же фильтр date_geq, что и в живом проверенном
                # do_rows_read.py::DoRowsReadRange (verbatim `$start: Date`),
                # тип переменной там доказан живым прогоном, здесь — то же
                # семейство датасетов Durable Objects, не гадаем заново.
                f"""query($accountTag: string, $start: Date) {{
                    viewer {{ accounts(filter: {{accountTag: $accountTag}}) {{
                        {group_field}(limit: 1000, filter: {{date_geq: $start}}) {{
                            {agg} {{ {field_name} }}
                        }}
                    }} }}
                }}""",
                {"accountTag": account_id, "start": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            )
            accounts = do_rows_read.require_accounts(data["viewer"]["accounts"])
            items = [item for acc in accounts for item in acc[group_field]]
            # Гвардия обрезки (та же находка): limit: 1000 может обрезаться
            # молча так же, как limit: 10000 в остальных двух местах.
            do_rows_read.check_not_truncated(items, limit=1000)
            values = [item[agg][field_name] for item in items]
            total = max(values) if agg == "max" and values else sum(values)
            rows.append(Row(label, source, total, LIMITS[limit_key], "rows", reset, "ok", f"поле {field_name} в {type_name} (агрегат {agg})"))
        except (RuntimeError, KeyError, TypeError, StopIteration) as error:
            # StopIteration — находка AI-ревью PR #327: `next()` без дефолта
            # выше бросает её, когда интроспекция нашла Sum/Max-тип с полем
            # rowsread/rowswritten, но имя типа не содержит ни один из
            # четырёх захардкоженных групп-датасетов — ровно момент, когда
            # CF заведёт пятый DO-датасет и метрика появится в схеме, main()
            # не должен падать целиком именно тогда, когда появились новые
            # данные.
            rows.append(no_data(label, source, LIMITS[limit_key], "rows", f"интроспекция или запрос данных упали: {error}"))

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
    # GitHub Actions кладёт в env ПУСТУЮ строку для незаданного `vars.*`, не
    # отсутствие ключа — `.get(..., default)` тут не срабатывает никогда
    # (находка AI-ревью PR #327): без `or` причина печаталась бы как
    # «по vars.DEEPSEEK_BASE_URL ()» вместо честного «не задан в окружении».
    base_url = os.environ.get("DEEPSEEK_BASE_URL") or "не задан в окружении"
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
    # By-design no-data (находка AI-ревью PR #327, третий раунд): «Actions
    # минуты» и «LLM-провайдер квота» — «нет данных» ВСЕГДА, не находка, а
    # объявленный честный пробел (public repo безлимитен; провайдер не
    # публикует API остатка квоты) — предупреждение, горящее на КАЖДОМ
    # прогоне, перестаёт быть сигналом и маскирует реальный «CF недоступен».
    # Считаем только НЕОЖИДАННЫЕ пропуски.
    BY_DESIGN_NO_DATA = {"Actions минуты (billing)", "LLM-провайдер квота"}
    no_data_rows = [r for r in rows if r.status == "no-data" and r.resource not in BY_DESIGN_NO_DATA]
    if no_data_rows:
        print(f"::warning::{len(no_data_rows)} источник(ов) без данных — см. колонку «Заметка» выше")

    exit_code = 0
    if breached:
        text = "🚨 Квота харнеса перевалила за {}%:\n".format(THRESHOLD_PCT) + "\n".join(
            f"- {r.resource}: {r.current:,} / {r.limit:,} ({r.pct}%)".replace(",", " ") for r in breached
        )
        # ::warning:: — workflow-команда GitHub Actions, обрывается на первом
        # переводе строки: многострочный text как аннотация показал бы только
        # первую строку, остальные breached-ресурсы ушли бы в лог простым
        # текстом (находка ревью PR #327). Полный список — обычным print,
        # аннотация — однострочная сводка.
        print(text)
        print(f"::warning::квота харнеса перевалила за {THRESHOLD_PCT}%: "
              + ", ".join(r.resource for r in breached))
        result = pulse_guard.escalate(repo, pulse_guard.WATCHDOG_ISSUE, text)
        print(result)
        # Находка ревью PR #327: escalate() — best-effort по обоим каналам
        # (Telegram, след в issue), возврат печатался, но не проверялся, и
        # прогон всегда завершался 0. Порог пробит И сигнал не дошёл ни одним
        # каналом ("НЕ доставлен" + "НЕ оставлен") — прогон обязан красить
        # список workflow-раннов, а не жить одной строкой в логе, который
        # никто не читает между пультами (тот же принцип, что у
        # scheduler.py::accept_merged_tasks hard_failure).
        if "НЕ доставлен" in result and "НЕ оставлен" in result:
            print(f"::error::квота пробита, но сигнал не дошёл ни одним каналом: {result}")
            exit_code = 1
    else:
        print(f"Порог {THRESHOLD_PCT}% не превышен ни по одному измеренному ресурсу.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
