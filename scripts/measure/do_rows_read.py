#!/usr/bin/env python3
"""Факт rows_read Durable Objects из Cloudflare GraphQL Analytics API (задача #320).

Зачем: гипотеза «#status() (cf-worker/src/harness.ts) делает два полных скана
таблицы tasks на каждый heartbeat» объясняет ПРИЧИНУ исчерпания суточной квоты
5 000 000 rows_read (docs/research/20-cloudflare-free.md), но оценка «десятки-
сотни тысяч на job» в #320 не была подтверждена числом. Без факта нельзя ни
доказать, что фикс помог, ни понять, хватит ли его.

Cloudflare не документирует эту метрику по конкретному GraphQL-имени в текстовой
доке (developers.cloudflare.com/durable-objects/observability/metrics-and-analytics/
называет только 4 датасета-кандидата и отсылает к самостоятельной интроспекции:
`durableObjectsInvocationsAdaptiveGroups`, `durableObjectsPeriodicGroups`,
`durableObjectsStorageGroups`, `durableObjectsSubrequestsAdaptiveGroups`).
Поэтому этот скрипт НЕ угадывает имя поля: он идёт по живой схеме (introspection)
от корня (`Query.viewer.accounts.<датасет>`), пробует датасеты-кандидаты по
очереди и берёт первый, у которого объект `sum` реально содержит поле `rowsRead`.
Если такого нет ни у одного кандидата — это тоже результат, и скрипт говорит
об этом прямо, а не подставляет наугад.

Схема запроса подбирается по факту, а не по предположению: если у поля датасета
есть аргумент фильтра с ключами диапазона (`date_geq`/`date_leq`) и в объекте
`dimensions` есть поле `date` — берём один запрос с группировкой по дню;
иначе — по одному запросу на день с фильтром `{date: <день>}` и группировкой
по часу (`datetimeHour`), суточный итог — сумма по часовым строкам.

Использование:
    python scripts/measure/do_rows_read.py [--days N]

Среда: CLOUDFLARE_API_TOKEN (нужно право Account Analytics:Read — выдаётся в
Cloudflare dashboard → My Profile → API Tokens → права токена), CLOUDFLARE_ACCOUNT_ID.
Секреты читаются только из env и никогда не печатаются: в лог идут только числа
и текст ошибок GraphQL (в них токена нет).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.cloudflare.com/client/v4/graphql"
DAILY_LIMIT = 5_000_000

# Факт и его источник — docs/research/20-cloudflare-free.md, раздел «Замер
# факта: rows_read в проде» (не подтверждено официальной докой Cloudflare,
# эмпирическое наблюдение). Одно место правды: используется и в запросе
# (build_data_query), и в проверке (check_not_truncated) — раздельные
# литералы уже расходились бы незаметно.
GRAPHQL_ROW_LIMIT = 10000

# Порядок проверки — по правдоподобию (docs/research/20-cloudflare-free.md,
# раздел «Durable Objects на Free»: rows_read/rows_written — метрики хранилища
# SQLite, поэтому storage-датасет проверяется тоже, но после periodic).
CANDIDATE_DATASETS = ["durableObjectsPeriodicGroups", "durableObjectsStorageGroups"]

# Найдено живым прогоном (#320, 2026-09-05): поля вида `[X!]!` заворачивают тип
# в NON_NULL(LIST(NON_NULL(X))) — три уровня `ofType` до именованного типа, не
# два. Запрашиваем на один уровень глубже, чем казалось бы достаточно.
TYPE_REF_FRAGMENT = "name kind ofType { name kind ofType { name kind ofType { name kind } } }"

INTROSPECT_TYPE_QUERY = f"""
query IntrospectType($name: String!) {{
  __type(name: $name) {{
    name
    kind
    fields {{
      name
      type {{ {TYPE_REF_FRAGMENT} }}
      args {{ name type {{ {TYPE_REF_FRAGMENT} }} }}
    }}
    inputFields {{
      name
      type {{ {TYPE_REF_FRAGMENT} }}
    }}
  }}
}}
"""

SCHEMA_ROOT_QUERY = "query { __schema { queryType { name } } }"


# ── Чистая логика (тестируется без сети) ──────────────────────────────────────────


def unwrap_type_name(type_ref: dict | None) -> str | None:
    """GraphQL заворачивает тип в NON_NULL/LIST — разворачиваем до named type.

    Находка на живой схеме Cloudflare (2026-09-05, прогон #320): для NON_NULL/LIST
    поле `name` приходит не как `null` (как того требует спецификация GraphQL
    introspection), а как пустая строка `""`. Проверка `is None` эту обёртку не
    ловила и разворот останавливался на первом уровне — отсюда `тип '' не найден`.
    Значение считается «нет имени» при falsy (`None` ИЛИ `""`), не только `None`.
    """
    while type_ref is not None and not type_ref.get("name"):
        type_ref = type_ref.get("ofType")
    return (type_ref.get("name") or None) if type_ref else None


def find_field(type_obj: dict, field_name: str) -> dict:
    for f in type_obj.get("fields") or []:
        if f["name"] == field_name:
            return f
    raise RuntimeError(f"поле {field_name!r} не найдено в типе {type_obj.get('name')!r} "
                       "(живая схема отличается от ожидания — не угадка, а факт)")


def find_arg(field_obj: dict, arg_name: str) -> dict | None:
    for a in field_obj.get("args") or []:
        if a["name"] == arg_name:
            return a
    return None


def choose_query_shape(filter_field_names: set[str], dim_field_names: set[str]) -> str:
    """'range' — один запрос с date_geq/date_leq и группировкой по дню;
    'per_day' — по одному запросу на день с фильтром {date: ...} и группировкой
    по часу. Выбор по факту наличия полей, не по предположению."""
    if {"date_geq", "date_leq"} <= filter_field_names and "date" in dim_field_names:
        return "range"
    if "date" in filter_field_names:
        return "per_day"
    raise RuntimeError(f"датасет не поддерживает ни диапазон, ни точную дату: "
                       f"доступные ключи фильтра {sorted(filter_field_names)}")


def check_not_truncated(rows: list[dict], limit: int = GRAPHQL_ROW_LIMIT) -> None:
    """Cloudflare режет range-ответ на `limit` строк без явного маркера обрезки.
    Если пришло ровно `limit` строк — это неотличимо от «ровно столько и было»,
    поэтому считаем труднее: падаем громко, а не тихо занижаем суточный итог."""
    if len(rows) >= limit:
        raise RuntimeError(
            f"ответ GraphQL содержит {len(rows)} строк (>= лимита {limit}) — "
            "похоже на молчаливую обрезку диапазона; уменьши --days или добавь "
            "пагинацию вместо доверия этому числу")


def group_rows_by_day(rows: list[dict]) -> dict[date, list[dict]]:
    """Раскладка часовых/дневных строк range-ответа по суткам (`dimensions.date`).
    Единственное место, где сутки можно тихо перепутать при склейке — поэтому
    вынесено в чистую функцию и покрыто тестом на стыке двух дней."""
    by_day: dict[date, list[dict]] = {}
    for r in rows:
        d = date.fromisoformat(r["dimensions"]["date"])
        by_day.setdefault(d, []).append(r)
    return by_day


def daily_totals_from_rows(rows: list[dict], sum_has: set[str], dim_has: set[str]) -> dict:
    """Суточный итог из списка строк (часовых или дневных) одного дня."""
    total_read = sum(r["sum"].get("rowsRead", 0) or 0 for r in rows)
    total_written = sum(r["sum"].get("rowsWritten", 0) or 0 for r in rows) if "rowsWritten" in sum_has else 0
    peak = max(rows, key=lambda r: r["sum"].get("rowsRead", 0) or 0, default=None)
    by_namespace: dict[str, int] = {}
    if "namespaceId" in dim_has:
        for r in rows:
            ns = r["dimensions"].get("namespaceId") or "?"
            by_namespace[ns] = by_namespace.get(ns, 0) + (r["sum"].get("rowsRead", 0) or 0)
    peak_label = None
    if peak is not None:
        peak_label = peak["dimensions"].get("datetimeHour") or peak["dimensions"].get("date")
    return {
        "rows_read": total_read,
        "rows_written": total_written,
        "peak_label": peak_label,
        "peak_rows_read": (peak["sum"].get("rowsRead", 0) or 0) if peak is not None else 0,
        "by_namespace": by_namespace,
    }


def format_table(days_summary: list[tuple[date, dict]]) -> str:
    lines = [
        "дата (UTC) | rows_read | % от лимита 5 000 000 | пиковый час (rows_read) | rows_written",
        "---|---|---|---|---",
    ]
    for day, summary in days_summary:
        pct = summary["rows_read"] / DAILY_LIMIT * 100
        peak = f"{summary['peak_label']} ({summary['peak_rows_read']:,})" if summary["peak_label"] else "—"
        lines.append(
            f"{day.isoformat()} | {summary['rows_read']:,} | {pct:.1f}% | {peak} | "
            f"{summary['rows_written']:,}"
        )
    return "\n".join(lines)


def format_namespace_breakdown(days_summary: list[tuple[date, dict]]) -> str:
    totals: dict[str, int] = {}
    for _, summary in days_summary:
        for ns, val in summary["by_namespace"].items():
            totals[ns] = totals.get(ns, 0) + val
    if not totals:
        return "разбивка по namespaceId недоступна в этом датасете"
    lines = ["namespaceId | rows_read (сумма по всем снятым дням)", "---|---"]
    for ns, val in sorted(totals.items(), key=lambda kv: -kv[1]):
        lines.append(f"{ns} | {val:,}")
    return "\n".join(lines)


# ── GraphQL-транспорт (тонкая обёртка, только сеть) ────────────────────────────────


def graphql(token: str, query: str, variables: dict) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # Тело ответа Cloudflare при ошибке — не токен, печатать безопасно.
        detail = error.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], ensure_ascii=False)[:800])
    return payload["data"]


def introspect(token: str, type_name: str, step: str = "") -> dict:
    data = graphql(token, INTROSPECT_TYPE_QUERY, {"name": type_name})
    t = data["__type"]
    if t is None:
        where = f" (шаг: {step})" if step else ""
        raise RuntimeError(f"тип {type_name!r} не найден в живой схеме Cloudflare GraphQL API{where}")
    return t


def discover_dataset(token: str) -> dict:
    """Идёт по живой схеме от корня и находит первый датасет-кандидат, где
    `sum` реально содержит `rowsRead`. Возвращает всё нужное для построения
    основного запроса — без единой догадки об именах полей."""
    root = graphql(token, SCHEMA_ROOT_QUERY, {})
    query_type_name = root["__schema"]["queryType"]["name"]
    print(f"[introspection] queryType = {query_type_name!r}")

    query_type = introspect(token, query_type_name, step="Query.viewer")
    viewer_field = find_field(query_type, "viewer")
    viewer_type_name = unwrap_type_name(viewer_field["type"])
    print(f"[introspection] viewer field type = {viewer_type_name!r} (raw: {viewer_field['type']})")

    viewer_type = introspect(token, viewer_type_name, step="Viewer.accounts")
    accounts_field = find_field(viewer_type, "accounts")
    accounts_type_name = unwrap_type_name(accounts_field["type"])
    print(f"[introspection] accounts field type = {accounts_type_name!r} (raw: {accounts_field['type']})")
    accounts_type = introspect(token, accounts_type_name, step="Accounts.<dataset>")

    tried: dict[str, str] = {}
    for dataset in CANDIDATE_DATASETS:
        try:
            ds_field = find_field(accounts_type, dataset)
        except RuntimeError as error:
            tried[dataset] = str(error)
            continue
        ds_type_name = unwrap_type_name(ds_field["type"])
        print(f"[introspection] {dataset} field type = {ds_type_name!r}")
        ds_type = introspect(token, ds_type_name, step=f"{dataset}.sum/dimensions")

        sum_type_name = unwrap_type_name(find_field(ds_type, "sum")["type"])
        sum_type = introspect(token, sum_type_name, step=f"{dataset}.sum fields")
        sum_field_names = {f["name"] for f in sum_type.get("fields") or []}
        print(f"[introspection] {dataset}.sum fields = {sorted(sum_field_names)}")
        if "rowsRead" not in sum_field_names:
            tried[dataset] = f"sum не содержит rowsRead (доступно: {sorted(sum_field_names)})"
            continue

        dim_type_name = unwrap_type_name(find_field(ds_type, "dimensions")["type"])
        dim_type = introspect(token, dim_type_name, step=f"{dataset}.dimensions fields")
        dim_field_names = {f["name"] for f in dim_type.get("fields") or []}
        print(f"[introspection] {dataset}.dimensions fields = {sorted(dim_field_names)}")

        filter_arg = find_arg(ds_field, "filter")
        filter_field_names: set[str] = set()
        if filter_arg is not None:
            filter_type_name = unwrap_type_name(filter_arg["type"])
            filter_type = introspect(token, filter_type_name, step=f"{dataset}.filter fields")
            filter_field_names = {f["name"] for f in filter_type.get("inputFields") or []}
            print(f"[introspection] {dataset}.filter fields = {sorted(filter_field_names)}")

        return {
            "dataset": dataset,
            "sum_field_names": sum_field_names,
            "dim_field_names": dim_field_names,
            "filter_field_names": filter_field_names,
        }

    raise RuntimeError(
        "ни один из известных датасетов-кандидатов не содержит rowsRead в sum: "
        f"{json.dumps(tried, ensure_ascii=False)}. Метрика rows_read либо живёт под "
        "другим именем (проверь introspection.md на developers.cloudflare.com вручную), "
        "либо недоступна этому токену/плану."
    )


def build_data_query(dataset: str, shape: str, sum_fields: list[str], dim_fields: list[str]) -> str:
    sum_sel = " ".join(sum_fields)
    dim_sel = " ".join(dim_fields)
    if shape == "range":
        return f"""
query DoRowsReadRange($accountTag: string!, $start: Date, $end: Date) {{
  viewer {{
    accounts(filter: {{ accountTag: $accountTag }}) {{
      rows: {dataset}(
        filter: {{ date_geq: $start, date_leq: $end }}
        limit: {GRAPHQL_ROW_LIMIT}
        orderBy: [date_ASC]
      ) {{
        dimensions {{ {dim_sel} }}
        sum {{ {sum_sel} }}
      }}
    }}
  }}
}}
"""
    return f"""
query DoRowsReadDay($accountTag: string!, $date: Date) {{
  viewer {{
    accounts(filter: {{ accountTag: $accountTag }}) {{
      rows: {dataset}(
        filter: {{ date: $date }}
        limit: {GRAPHQL_ROW_LIMIT}
        orderBy: [datetimeHour_ASC]
      ) {{
        dimensions {{ {dim_sel} }}
        sum {{ {sum_sel} }}
      }}
    }}
  }}
}}
"""


def fetch_rows(token: str, account_id: str, query: str, shape: str, day: date,
                start: date, end: date) -> list[dict]:
    variables = ({"accountTag": account_id, "start": start.isoformat(), "end": end.isoformat()}
                 if shape == "range"
                 else {"accountTag": account_id, "date": day.isoformat()})
    data = graphql(token, query, variables)
    accounts = data["viewer"]["accounts"]
    if not accounts:
        raise RuntimeError(
            "accounts пуст — CLOUDFLARE_ACCOUNT_ID не совпадает с аккаунтом токена "
            "или у токена нет доступа к этому аккаунту")
    rows = accounts[0]["rows"]
    check_not_truncated(rows)
    return rows


# ── Оркестрация ─────────────────────────────────────────────────────────────────


def run(token: str, account_id: str, days: int) -> str:
    info = discover_dataset(token)
    dataset = info["dataset"]
    sum_fields = sorted({"rowsRead", "rowsWritten"} & info["sum_field_names"])
    dim_fields = sorted({"date", "datetimeHour", "namespaceId"} & info["dim_field_names"])
    shape = choose_query_shape(info["filter_field_names"], info["dim_field_names"])
    query = build_data_query(dataset, shape, sum_fields, dim_fields)

    today = datetime.now(timezone.utc).date()
    per_day: list[tuple[date, dict]] = []
    if shape == "range":
        start = today - timedelta(days=days - 1)
        rows = fetch_rows(token, account_id, query, shape, today, start, today)
        by_day = group_rows_by_day(rows)
        for offset in range(days):
            d = today - timedelta(days=offset)
            per_day.append((d, daily_totals_from_rows(by_day.get(d, []), info["sum_field_names"],
                                                       info["dim_field_names"])))
    else:
        for offset in range(days):
            d = today - timedelta(days=offset)
            rows = fetch_rows(token, account_id, query, shape, d, d, d)
            per_day.append((d, daily_totals_from_rows(rows, info["sum_field_names"],
                                                       info["dim_field_names"])))

    per_day.sort(key=lambda item: item[0])
    lines = [
        f"Датасет: `{dataset}` (найден интроспекцией живой схемы, не угадан).",
        "",
        format_table(per_day),
        "",
        format_namespace_breakdown(per_day),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=7, help="сколько последних суток UTC снять")
    args = parser.parse_args()
    if args.days < 1:
        parser.error(f"--days должен быть >= 1, получено {args.days} — "
                     "0 или отрицательное число даёт пустой отчёт с exit 0, "
                     "неотличимый от «данных нет» при том, что замера не было")

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not token or not account_id:
        print("::error::CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — замер невозможен")
        return 1

    try:
        report = run(token, account_id, args.days)
    except RuntimeError as error:
        msg = str(error)
        low = msg.lower()
        if "authentication" in low or "not authorized" in low or "authorization" in low:
            print(f"::error::Токену не хватает прав на Analytics: {msg}")
            print("::error::Нужно право Account Analytics:Read — выдать в Cloudflare "
                  "dashboard → My Profile → API Tokens → правки токена CLOUDFLARE_API_TOKEN.")
        else:
            print(f"::error::{msg}")
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
