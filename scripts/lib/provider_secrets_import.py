#!/usr/bin/env python3
"""Массовая установка секретов/переменной combo-router (PR #732, задача #215)
из локального файла-экспорта роутера учёток владельца — задача #733.

Владелец отказался вставлять секреты руками в веб-форму GitHub. Этот скрипт
читает файл-экспорт (структура — top-level объект с ключом
"providerConnections": список учёток с полями provider/authType/email/
priority/isActive/testStatus/backoffLevel/lastError/apiKey|accessToken),
сопоставляет учётки со слотами combo-router и заводит секреты/переменную
через `gh secret set`/`gh variable set`.

Контракт имён секретов и провайдеров-кандидатов НЕ дублируется здесь литералом
— он единственный раз объявлен в PR #732 (scripts/lib/dsh-ci.sh::
PLUGINS_SUITE_CANDIDATE_ROUTES) и парсится оттуда (parse_suite_routes). Если
PR #732 ещё не смёржен в текущую ветку — громкий отказ с понятным сообщением
(см. parse_suite_routes), а не угаданный список.

Anthropic OAuth-пул (задача #216) — ДРУГОЙ механизм (импорт credentials-файла
через `dsh-anthropic-pool add`, не API-ключ) — этот скрипт его НЕ трогает.
Реестр провайдеров в морде (PR #453, задача #378, схема
`<ROUTE_UPPER>_API_KEY`, Cloudflare Workers secrets) — тоже другой потребитель,
этот скрипт заполняет только GitHub Actions secrets/vars ЭТОГО репозитория.

Требования безопасности (AGENTS.md, раздел «Секреты» — репозиторий публичный):
  - путь к файлу-экспорту — только --export-file/переменная окружения
    PROVIDER_EXPORT_FILE, никогда литерал в коде/тестах/докстрингах;
  - значения не проходят через argv — gh кормится через stdin (input=, без
    --body-file — задача #786: флаг непортируем между версиями gh);
  - значения никогда не печатаются (целиком/частично/как подстрока);
  - никакой записи значений в файлы;
  - файл-экспорт внутри рабочего дерева репозитория — громкий отказ;
  - дефолт — сухой прогон, запись только по --apply;
  - существующий секрет/переменная перезаписывается только явным флагом.

СЕМАНТИКА isActive/testStatus — состояние ПРЕДОХРАНИТЕЛЯ, не факт о ключе
(задача #777): роутер учёток (krouter) сам гасит учётку (isActive=False,
testStatus=unavailable) при исчерпании квоты и включает её обратно, когда та
восстановится — файл-экспорт лишь снимок на дату выгрузки (см. --export-file,
дата печатается в отчёте). Ранжирование поэтому различает ДВЕ РАЗНЫЕ оси, а
не одну: «квота временно исчерпана» (429/backoff — нормальное состояние
ротации, НЕ дисквалификация) и «ключ неверен/доступ запрещён» (401/403 —
настоящая дисквалификация). Источник факта для ранга — живая проба
(--no-probe отключает), она пробует ТОЛЬКО кандидатов на слоты suite (не весь
файл — из непричастных к suite учёток пробовать нечего, у них нет маршрута);
когда пробы нет (сеть недоступна или --no-probe) — падаем на классификацию
снимка по errorCode/backoffLevel. Источник ранга (проба/снимок) виден в
отчёте отдельной пометкой у каждой строки.

Использование:
  python3 scripts/lib/provider_secrets_import.py --export-file <путь> [--apply]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DSH_CI_DEFAULT = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"

SUITE_URL_VAR = "PLUGINS_SUITE_URL"
# Тег релиза suite (dsh-plugins-suite-v1) — не секрет, публичный тег GitHub
# Release этого репозитория (см. задачу #215, PR #732). Переопределим флагом
# --suite-tag, если owner когда-нибудь переопубликует suite под новым тегом —
# зашитый дефолт не должен становиться недостижимым газом.
DEFAULT_SUITE_TAG = "dsh-plugins-suite-v1"

REQUIRED_EXPORT_KEY = "providerConnections"

# Слаг провайдера в экспорте роутера учёток (krouter) -> семейство маршрутов
# combo-router (алиас без суффикса "-N" в PLUGINS_SUITE_CANDIDATE_ROUTES).
# Явная карта, а не угадывание: провайдер экспорта, которого здесь нет, —
# вне области этой задачи (другая схема, #731/#727, или не подключён вовсе),
# перечисляется в отчёте отдельно, не отбрасывается молча.
KROUTER_PROVIDER_TO_SUITE_FAMILY: dict[str, str] = {
    "openrouter": "openrouter",
    "ollama": "ollama-cloud",  # "ollama-local" НЕ маппится — другой механизм
    "nvidia": "nvidia-nim",
    "glm": "zai",  # GLM — тот же Z.ai coding plan, что маршрут zai-1 в PR #732
}

_PROBE_TIMEOUT_SECONDS = 10

# Тир ранга (меньше — лучше слоту). ДВЕ РАЗНЫЕ оси, не смешиваются (задача
# #777): TIER_TEMPORARY (квота/backoff) — нормальное состояние ротации, оно
# ЛУЧШЕ TIER_DISQUALIFIED (ключ неверен) при любом источнике факта.
_TIER_HEALTHY = 0
_TIER_TEMPORARY = 1
_TIER_UNKNOWN = 2
_TIER_DISQUALIFIED = 3

# HTTP-коды, которые и живая проба (probe_provider), и снимок (errorCode)
# трактуют одинаково — «ключ неверен/доступ запрещён», настоящая
# дисквалификация, не квота.
_AUTH_INVALID_HTTP_CODES = (401, 403)


class LoudError(RuntimeError):
    """Ошибка, которая обязана быть видна целиком оператору — без утечки значений."""


# ── Контракт имён секретов: парсинг PR #732, не дублирование ────────────────


@dataclass(frozen=True)
class SuiteRoute:
    alias: str
    family: str
    slot: int
    base_url: str
    secret_env: str
    display_name: str


_ROUTE_LINE_RE = re.compile(
    r'^\s*"([a-z0-9-]+)\|([^|]+)\|([A-Z0-9_]+)\|[^|]*\|[^|]*\|([^"]*)"\s*$'
)


def parse_suite_routes(dsh_ci_path: Path = DSH_CI_DEFAULT) -> list[SuiteRoute]:
    """Единственное место правды на имена секретов — PLUGINS_SUITE_CANDIDATE_ROUTES
    в scripts/lib/dsh-ci.sh (PR #732). Парсим файл, не переписываем список сюда."""
    if not dsh_ci_path.exists():
        raise LoudError(
            f"{dsh_ci_path} не найден — контракт имён секретов combo-router "
            "живёт в PR #732 (scripts/lib/dsh-ci.sh::PLUGINS_SUITE_CANDIDATE_ROUTES)."
        )
    text = dsh_ci_path.read_text(encoding="utf-8")
    marker = "PLUGINS_SUITE_CANDIDATE_ROUTES=("
    if marker not in text:
        raise LoudError(
            f"{dsh_ci_path} не содержит массив PLUGINS_SUITE_CANDIDATE_ROUTES — "
            "похоже, PR #732 ещё не смёржен в эту ветку/main."
        )
    block = text.split(marker, 1)[1].split(")", 1)[0]
    routes: list[SuiteRoute] = []
    for line in block.splitlines():
        match = _ROUTE_LINE_RE.match(line)
        if not match:
            continue
        alias, base_url, secret_env, display_name = match.groups()
        family_match = re.match(r"^(.*)-(\d+)$", alias)
        if not family_match:
            raise LoudError(
                f"алиас маршрута {alias!r} в {dsh_ci_path} не оканчивается на "
                "-N — формат массива изменился, парсер не понимает."
            )
        routes.append(
            SuiteRoute(
                alias=alias,
                family=family_match.group(1),
                slot=int(family_match.group(2)),
                base_url=base_url,
                secret_env=secret_env,
                display_name=display_name,
            )
        )
    if not routes:
        raise LoudError(
            f"в {dsh_ci_path} не нашлось ни одной строки маршрута между "
            f"{marker!r} и закрывающей скобкой — формат изменился?"
        )
    return routes


# ── Разбор файла-экспорта ─────────────────────────────────────────────────


def _ensure_outside_repo(path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved
    raise LoudError(
        f"файл-экспорт {resolved} лежит внутри рабочего дерева репозитория "
        f"({REPO_ROOT}) — рано или поздно это уедет в коммит. Держи экспорт "
        "вне репозитория и передай путь аргументом --export-file или "
        "переменной окружения PROVIDER_EXPORT_FILE."
    )


def load_export(path_str: str) -> dict:
    path = _ensure_outside_repo(Path(path_str))
    if not path.exists():
        raise LoudError(f"файл-экспорт не найден: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise LoudError(f"{path} — невалидный JSON: {error}") from error
    if (
        not isinstance(data, dict)
        or REQUIRED_EXPORT_KEY not in data
        or not isinstance(data[REQUIRED_EXPORT_KEY], list)
    ):
        top_keys = sorted(data.keys()) if isinstance(data, dict) else [f"<{type(data).__name__}>"]
        raise LoudError(
            "неузнанная структура файла-экспорта: ожидался объект с ключом "
            f"{REQUIRED_EXPORT_KEY!r} (список учёток). Верхнеуровневые ключи "
            f"файла: {top_keys}. Значения не печатаются."
        )
    return data


_FILENAME_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def export_snapshot_date(path: Path) -> str:
    """Дата снимка предохранителя (задача #777, критерий 4) — экспорт krouter
    не несёт дату содержимым JSON (проверено на реальном файле владельца:
    верхнеуровневые ключи не dict/list — settings/modelAliases/mitmAlias/
    pricing, ни одного поля-даты), поэтому берём её из имени файла
    (krouter-backup-<ISO-дата>...), а если экспортёр когда-нибудь переименует
    файл — из mtime, явно пометив, что это mtime файла, а не факт о
    содержимом."""
    match = _FILENAME_DATE_RE.search(path.name)
    if match:
        return f"{match.group(1)} (дата из имени файла)"
    mtime = path.stat().st_mtime
    return (
        datetime.datetime.fromtimestamp(mtime).date().isoformat()
        + " (по mtime файла — имя файла даты не содержит)"
    )


@dataclass
class Account:
    id: str
    provider: str
    email: str
    priority: int
    is_active: bool
    test_status: str
    backoff_level: int
    last_error: str
    error_code: int | None
    api_key: str | None
    access_token: str | None


def accounts_from_export(data: dict) -> list[Account]:
    accounts: list[Account] = []
    for raw in data.get(REQUIRED_EXPORT_KEY, []):
        if not isinstance(raw, dict):
            continue
        priority_raw = raw.get("priority")
        error_code_raw = raw.get("errorCode")
        accounts.append(
            Account(
                id=str(raw.get("id", "")),
                provider=str(raw.get("provider", "")),
                email=str(raw.get("email") or ""),
                priority=int(priority_raw) if isinstance(priority_raw, (int, float)) else 999999,
                is_active=bool(raw.get("isActive")),
                test_status=str(raw.get("testStatus") or ""),
                backoff_level=int(raw.get("backoffLevel") or 0),
                last_error=str(raw.get("lastError") or ""),
                error_code=int(error_code_raw) if isinstance(error_code_raw, (int, float)) else None,
                api_key=(raw.get("apiKey") or None),
                access_token=(raw.get("accessToken") or None),
            )
        )
    return accounts


def secret_value(account: Account) -> str | None:
    """apikey-учётки несут значение в apiKey, oauth — в accessToken. Ни то, ни
    другое никогда не возвращается наружу иначе как в stdin gh secret set."""
    return account.api_key or account.access_token or None


def classify_snapshot(account: Account) -> tuple[int, str]:
    """Ранг по СНИМКУ файла-экспорта — только когда живой пробы нет (задача
    #777). isActive/testStatus сами по себе НЕ дисквалифицируют (это
    предохранитель на дату выгрузки, см. докстринг модуля) — единственное
    основание дисквалификации по снимку — errorCode 401/403 (ключ
    неверен/доступ запрещён). errorCode 429 или backoffLevel>0 — та же ось,
    что и живая проба 429: квота, нормальное состояние ротации."""
    if account.is_active and account.test_status == "active":
        return _TIER_HEALTHY, "активна по снимку (testStatus=active) [источник: снимок]"
    if account.error_code in _AUTH_INVALID_HTTP_CODES:
        return (
            _TIER_DISQUALIFIED,
            f"ключ неверен по снимку (errorCode={account.error_code}) [источник: снимок]",
        )
    if account.error_code == 429 or account.backoff_level > 0:
        return (
            _TIER_TEMPORARY,
            "квота/backoff по снимку — временное состояние ротации, не дисквалификация "
            "[источник: снимок]",
        )
    return (
        _TIER_UNKNOWN,
        "неопределённо по снимку (нет явной ошибки авторизации) [источник: снимок]",
    )


def classify_probe(status: str) -> tuple[int, str] | None:
    """Ранг по РЕЗУЛЬТАТУ живой пробы (probe_provider). None — проба не
    получила ответа от сети (недоступна), вызывающий обязан упасть на
    classify_snapshot и пометить это в отчёте."""
    if status == "жива":
        return _TIER_HEALTHY, "жива [источник: проба]"
    if status == "квота исчерпана":
        return _TIER_TEMPORARY, "квота исчерпана [источник: проба] — не дисквалификация"
    if status == "ключ неверен":
        return _TIER_DISQUALIFIED, "ключ неверен [источник: проба]"
    if status.startswith("неизвестно (сеть:"):
        return None
    return _TIER_UNKNOWN, f"{status} [источник: проба]"


def rank_account(account: Account, probe_status: str | None) -> tuple[int, str, str]:
    """(tier, заметка, источник). Живая проба, если получила ответ по сети, —
    авторитетный источник факта; иначе (проба выключена/недоступна по сети) —
    снимок файла-экспорта."""
    if probe_status is not None:
        classified = classify_probe(probe_status)
        if classified is not None:
            tier, note = classified
            return tier, note, "проба"
        tier, note = classify_snapshot(account)
        return tier, f"{note} (проба недоступна по сети)", "снимок"
    tier, note = classify_snapshot(account)
    return tier, note, "снимок"


# ── Сопоставление слотов ──────────────────────────────────────────────────


@dataclass
class SlotAssignment:
    route: SuiteRoute
    account: Account | None
    empty_reason: str | None = None


@dataclass
class SelectionResult:
    assignments: list[SlotAssignment]
    overflow: list[Account]
    out_of_scope: list[Account]
    probe_excluded: list[Account]
    rank_by_id: dict[str, tuple[int, str, str]]


def group_routes_by_family(routes: list[SuiteRoute]) -> dict[str, list[SuiteRoute]]:
    routes_by_family: dict[str, list[SuiteRoute]] = {}
    for route in routes:
        routes_by_family.setdefault(route.family, []).append(route)
    for family_routes in routes_by_family.values():
        family_routes.sort(key=lambda r: r.slot)
    return routes_by_family


def group_candidates(
    accounts: list[Account], routes_by_family: dict[str, list[SuiteRoute]]
) -> tuple[dict[str, list[Account]], list[Account]]:
    candidates_by_family: dict[str, list[Account]] = {}
    out_of_scope: list[Account] = []
    for account in accounts:
        family = KROUTER_PROVIDER_TO_SUITE_FAMILY.get(account.provider)
        if family is None or family not in routes_by_family:
            out_of_scope.append(account)
            continue
        candidates_by_family.setdefault(family, []).append(account)
    return candidates_by_family, out_of_scope


def probe_candidates(
    candidates_by_family: dict[str, list[Account]],
    routes_by_family: dict[str, list[SuiteRoute]],
) -> dict[str, str]:
    """Живая проба ТОЛЬКО кандидатов на слоты suite (задача #777) — учётки вне
    списка маршрутов (out_of_scope) пробовать бессмысленно, у них нет слота,
    который проба могла бы переранжировать. На файле владельца это ~9
    кандидатов из 46 учёток экспорта — пробовать все 46 значило бы тратить
    сеть на 37 учёток, чей результат пробы ни на что не влияет."""
    results: dict[str, str] = {}
    for family, accounts in candidates_by_family.items():
        family_routes = routes_by_family.get(family)
        if not family_routes:
            continue
        base_url = family_routes[0].base_url
        for account in accounts:
            value = secret_value(account)
            if value is None:
                continue
            results[account.id] = probe_provider(base_url, value)
    return results


def select_accounts(
    accounts: list[Account],
    routes: list[SuiteRoute],
    probe_results: dict[str, str] | None = None,
) -> SelectionResult:
    probe_results = probe_results or {}
    routes_by_family = group_routes_by_family(routes)
    candidates_by_family, out_of_scope = group_candidates(accounts, routes_by_family)

    rank_by_id: dict[str, tuple[int, str, str]] = {}
    for family_accounts in candidates_by_family.values():
        for account in family_accounts:
            rank_by_id[account.id] = rank_account(account, probe_results.get(account.id))

    assignments: list[SlotAssignment] = []
    overflow: list[Account] = []
    probe_excluded: list[Account] = []
    for family, family_routes in routes_by_family.items():
        ranked = sorted(
            candidates_by_family.get(family, []),
            key=lambda a: (rank_by_id[a.id][0], a.priority, a.id),
        )
        # Живая проба, подтвердившая «ключ неверен» (401/403) — настоящая
        # дисквалификация (не снимок, который может быть двухнедельной
        # давности): такого кандидата НЕ ставим в слот, честнее оставить его
        # пустым с названной причиной (задача #777, критерий 5). Снимочная
        # дисквалификация (нет живой пробы) кандидата не выбрасывает — только
        # ранжирует его последним, он всё ещё может занять слот, если больше
        # некому.
        viable: list[Account] = []
        dead_by_probe: list[Account] = []
        for account in ranked:
            tier, _note, source = rank_by_id[account.id]
            if source == "проба" and tier == _TIER_DISQUALIFIED:
                dead_by_probe.append(account)
            else:
                viable.append(account)

        empty_reason = None
        if dead_by_probe:
            names = ", ".join(account.email or account.id for account in dead_by_probe)
            empty_reason = (
                f"{len(dead_by_probe)} кандидат(ов) исключены живой пробой "
                f"(ключ неверен): {names}"
            )

        for index, route in enumerate(family_routes):
            account = viable[index] if index < len(viable) else None
            assignments.append(
                SlotAssignment(
                    route=route,
                    account=account,
                    empty_reason=(empty_reason if account is None and dead_by_probe else None),
                )
            )
        overflow.extend(viable[len(family_routes):])
        probe_excluded.extend(dead_by_probe)

    assignments.sort(key=lambda a: (a.route.family, a.route.slot))
    return SelectionResult(
        assignments=assignments,
        overflow=overflow,
        out_of_scope=out_of_scope,
        probe_excluded=probe_excluded,
        rank_by_id=rank_by_id,
    )


# ── gh: секреты/переменные (значения только через stdin) ────────────────────


def gh_repo(explicit: str | None) -> str:
    repo = explicit or os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return repo
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise LoudError(
            "не удалось определить owner/repo (передай --repo или задай "
            "GITHUB_REPOSITORY): " + result.stderr.strip()
        )
    return result.stdout.strip()


def existing_secret_names(repo: str) -> set[str]:
    result = subprocess.run(
        ["gh", "secret", "list", "--repo", repo, "--json", "name"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret list упал: {result.stderr.strip()}")
    return {item["name"] for item in json.loads(result.stdout)}


def existing_variable_names(repo: str) -> set[str]:
    result = subprocess.run(
        ["gh", "variable", "list", "--repo", repo, "--json", "name"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise LoudError(f"gh variable list упал: {result.stderr.strip()}")
    return {item["name"] for item in json.loads(result.stdout)}


def set_secret(repo: str, name: str, value: str) -> None:
    """Значение — ТОЛЬКО через stdin, никогда через argv. Без --body/--body-file
    `gh secret set` сам читает значение из stdin — этого достаточно, флаг не
    нужен. `--body-file` НЕ добавляем: задача #786 — установленная версия gh
    (2.85.0) не знает этот флаг у `gh secret set`/`gh variable set` (`unknown
    flag: --body-file`), из-за чего ни один секрет не записывался, хотя
    подавать значение и без него можно тем же `input=`."""
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo],
        input=value, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret set {name} упал: {result.stderr.strip()}")


def set_variable(repo: str, name: str, value: str) -> None:
    """См. докстринг set_secret — тот же непортируемый --body-file (#786),
    та же замена: значение через input=, --body-file в argv не добавляем."""
    result = subprocess.run(
        ["gh", "variable", "set", name, "--repo", repo],
        input=value, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise LoudError(f"gh variable set {name} упал: {result.stderr.strip()}")


# ── Живая проба ───────────────────────────────────────────────────────────


def probe_provider(base_url: str, api_key: str) -> str:
    """жива / квота исчерпана / ключ неверен / неизвестно — эвристика по HTTP-
    статусу общего для OpenAI-совместимых шлюзов эндпоинта /models. 401/403 —
    ключ неверен, 429 — квота исчерпана (разное лечение, не смешиваем)."""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            if response.status == 200:
                return "жива"
            return f"неизвестно (HTTP {response.status})"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            return "ключ неверен"
        if error.code == 429:
            return "квота исчерпана"
        return f"неизвестно (HTTP {error.code})"
    except urllib.error.URLError as error:
        return f"неизвестно (сеть: {error.reason})"


# ── Отчёт ─────────────────────────────────────────────────────────────────


def render_report(
    selection: SelectionResult,
    secret_status: dict[str, str],
    suite_status: str,
    apply: bool,
    export_date: str,
) -> str:
    lines: list[str] = []
    lines.append(f"Снимок экспорта датирован: {export_date}.")
    lines.append(
        "`isActive`/`testStatus` в таблице ниже — состояние ПРЕДОХРАНИТЕЛЯ роутера учёток "
        "на дату снимка (квота исчерпана → учётка гасится, квота вернулась → включается "
        "обратно), а НЕ факт о текущей пригодности ключа. Колонка «ранг» называет "
        "источник вывода: живая проба (текущий факт) либо снимок (когда пробы нет — "
        "выключена флагом или недоступна по сети)."
    )
    lines.append("")
    lines.append("## Слоты combo-router (PR #732)\n")
    lines.append(
        "| секрет | провайдер | email | priority | isActive (снимок) | testStatus (снимок) | ранг | статус секрета |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    assigned = 0
    empty = 0
    for slot in selection.assignments:
        name = slot.route.secret_env
        if slot.account is None:
            reason = slot.empty_reason or "нет кандидата"
            lines.append(f"| {name} | — | — | — | — | — | пусто: {reason} | — |")
            empty += 1
            continue
        assigned += 1
        account = slot.account
        status = secret_status.get(name, "?")
        _tier, note, _source = selection.rank_by_id.get(account.id, (None, "?", "?"))
        lines.append(
            f"| {name} | {account.provider} | {account.email} | {account.priority} | "
            f"{account.is_active} | {account.test_status} | {note} | {status} |"
        )
    lines.append("")
    lines.append(f"Слотов с кандидатом: {assigned}; слотов без кандидата: {empty}.")
    lines.append("")
    if selection.probe_excluded:
        lines.append("## Исключены живой пробой (подтверждено «ключ неверен»)\n")
        for account in selection.probe_excluded:
            _tier, note, _source = selection.rank_by_id.get(account.id, (None, "?", "?"))
            lines.append(f"- {account.provider} — {account.email or account.id}: {note}")
        lines.append("")
    if selection.overflow:
        lines.append("## Не влезли в слоты (тот же провайдер, слотов не хватило)\n")
        for account in selection.overflow:
            _tier, note, _source = selection.rank_by_id.get(account.id, (None, "?", "?"))
            lines.append(
                f"- {account.provider} — {account.email} (priority={account.priority}, "
                f"isActive={account.is_active}, testStatus={account.test_status}, ранг: {note})"
            )
        lines.append("")
    if selection.out_of_scope:
        by_provider: dict[str, int] = {}
        for account in selection.out_of_scope:
            by_provider[account.provider] = by_provider.get(account.provider, 0) + 1
        lines.append(f"## Вне области этой задачи ({len(selection.out_of_scope)} учёток)\n")
        lines.append(
            "Провайдеры вне списка маршрутов combo-router (PR #732) — этот скрипт их не "
            "устанавливает. Для них — другая схема (задача #731, `<PROVIDER_SLUG>_API_KEY` "
            "+ `vars.DSH_PROVIDER_CHAIN`) либо ротация не подключена вовсе."
        )
        for provider, count in sorted(by_provider.items()):
            lines.append(f"- {provider}: {count}")
        lines.append("")
    lines.append(f"## Переменная `{SUITE_URL_VAR}`\n")
    lines.append(f"- {suite_status}")
    lines.append("")
    lines.append("## Anthropic OAuth-пул — вне области\n")
    lines.append(
        "Anthropic OAuth-токены (учётки владельца с authType=oauth, провайдер claude) — "
        "ДРУГОЙ механизм (импорт credentials-файла через `dsh-anthropic-pool add`, не "
        "API-ключ). Этот скрипт их не заводит — см. задачу #216."
    )
    lines.append("")
    lines.append(
        "Режим: " + ("ЗАПИСЬ (--apply)" if apply else "СУХОЙ ПРОГОН — ничего не изменено, добавь --apply")
    )
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--export-file",
        default=os.environ.get("PROVIDER_EXPORT_FILE"),
        help="путь к файлу-экспорту роутера учёток (или переменная окружения "
             "PROVIDER_EXPORT_FILE) — никогда не пиши реальный путь в код/тесты",
    )
    parser.add_argument("--apply", action="store_true", help="писать секреты/переменные (дефолт — сухой прогон)")
    parser.add_argument("--force-secrets", action="store_true", help="перезаписать уже существующие секреты")
    parser.add_argument("--force-vars", action="store_true", help="перезаписать существующую переменную PLUGINS_SUITE_URL")
    parser.add_argument("--suite-tag", default=DEFAULT_SUITE_TAG, help=f"значение vars.{SUITE_URL_VAR} (дефолт {DEFAULT_SUITE_TAG})")
    parser.add_argument("--dsh-ci-path", default=str(DSH_CI_DEFAULT), help="путь к dsh-ci.sh для парсинга контракта имён (для тестов)")
    parser.add_argument("--repo", default=None, help="owner/repo (по умолчанию — GITHUB_REPOSITORY/gh repo view)")
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="не делать живые HTTP-запросы к эндпоинтам провайдеров (дефолт — пробовать "
             "кандидатов на слоты suite ВСЕГДА, и в сухом прогоне тоже, задача #777: "
             "проба ничего не пишет, а сеть и так дёргается при --apply)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # Отчёт (render_report) несёт кириллицу и «→» — на cp1251-консоли Windows
    # обычный print(...) в это падал UnicodeEncodeError УЖЕ ПОСЛЕ записи
    # секретов, пряча видимый исход прогона (задача #786, класс «шаг не
    # сохранил результат» — AGENTS.md «Проверяй видимый результат, а не
    # шаг»). reconfigure появился в Python 3.7+, но безопаснее не полагаться
    # на его наличие у подменённого в тестах stdout/stderr — getattr с
    # проверкой на None, тихий пропуск, если метода нет.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")

    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.export_file:
        print("::error::не передан файл-экспорт (--export-file или PROVIDER_EXPORT_FILE)", file=sys.stderr)
        return 2

    try:
        data = load_export(args.export_file)
        routes = parse_suite_routes(Path(args.dsh_ci_path))
        accounts = accounts_from_export(data)
        routes_by_family = group_routes_by_family(routes)
        candidates_by_family, _out_of_scope = group_candidates(accounts, routes_by_family)
        probe_results = (
            {} if args.no_probe else probe_candidates(candidates_by_family, routes_by_family)
        )
        selection = select_accounts(accounts, routes, probe_results=probe_results)
        repo = gh_repo(args.repo)
        existing_secrets = existing_secret_names(repo)
        existing_vars = existing_variable_names(repo)
    except LoudError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2

    export_date = export_snapshot_date(Path(args.export_file))
    secret_status: dict[str, str] = {}

    for slot in selection.assignments:
        name = slot.route.secret_env
        if slot.account is None:
            continue
        value = secret_value(slot.account)
        if value is None:
            secret_status[name] = "нет значения ключа в экспорте — пропуск"
            continue
        already = name in existing_secrets
        do_write = args.apply and (not already or args.force_secrets)
        if not args.apply:
            if already:
                secret_status[name] = "уже существует — пропуск при --apply без --force-secrets [сухой прогон]"
            else:
                secret_status[name] = "будет создан [сухой прогон]"
        elif not do_write:
            secret_status[name] = "уже существует — пропущен (idempotent, нужен --force-secrets)"
        else:
            try:
                set_secret(repo, name, value)
            except LoudError as error:
                secret_status[name] = f"ОШИБКА записи: {error}"
                continue
            secret_status[name] = "перезаписан" if already else "создан"

    suite_already = SUITE_URL_VAR in existing_vars
    suite_do_write = args.apply and (not suite_already or args.force_vars)
    if not args.apply:
        suite_status = (
            "уже существует — пропуск при --apply без --force-vars [сухой прогон]"
            if suite_already else f"будет установлена в {args.suite_tag} [сухой прогон]"
        )
    elif not suite_do_write:
        suite_status = "уже существует — пропущена (idempotent, нужен --force-vars)"
    else:
        try:
            set_variable(repo, SUITE_URL_VAR, args.suite_tag)
            suite_status = ("перезаписана в " if suite_already else "установлена в ") + args.suite_tag
        except LoudError as error:
            suite_status = f"ОШИБКА записи: {error}"

    print(render_report(selection, secret_status, suite_status, args.apply, export_date))
    return 0


if __name__ == "__main__":
    sys.exit(main())
