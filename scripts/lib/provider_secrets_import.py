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
  - значения не проходят через argv — gh кормится через stdin (--body-file -);
  - значения никогда не печатаются (целиком/частично/как подстрока);
  - никакой записи значений в файлы;
  - файл-экспорт внутри рабочего дерева репозитория — громкий отказ;
  - дефолт — сухой прогон, запись только по --apply;
  - существующий секрет/переменная перезаписывается только явным флагом.

Использование:
  python3 scripts/lib/provider_secrets_import.py --export-file <путь> [--apply]
"""

from __future__ import annotations

import argparse
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

_TEST_STATUS_RANK = {"active": 0, "error": 1, "unavailable": 2}
_PROBE_TIMEOUT_SECONDS = 10


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
    api_key: str | None
    access_token: str | None


def accounts_from_export(data: dict) -> list[Account]:
    accounts: list[Account] = []
    for raw in data.get(REQUIRED_EXPORT_KEY, []):
        if not isinstance(raw, dict):
            continue
        priority_raw = raw.get("priority")
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
                api_key=(raw.get("apiKey") or None),
                access_token=(raw.get("accessToken") or None),
            )
        )
    return accounts


def secret_value(account: Account) -> str | None:
    """apikey-учётки несут значение в apiKey, oauth — в accessToken. Ни то, ни
    другое никогда не возвращается наружу иначе как в stdin gh secret set."""
    return account.api_key or account.access_token or None


def _rank_key(account: Account) -> tuple:
    """Правило выбора слота (задача #733, критерий 7): isActive, затем
    testStatus (active лучше error лучше unavailable), затем priority
    (меньше — лучше), затем id для стабильности сортировки."""
    return (
        0 if account.is_active else 1,
        _TEST_STATUS_RANK.get(account.test_status, 3),
        account.priority,
        account.id,
    )


# ── Сопоставление слотов ──────────────────────────────────────────────────


@dataclass
class SlotAssignment:
    route: SuiteRoute
    account: Account | None


@dataclass
class SelectionResult:
    assignments: list[SlotAssignment]
    overflow: list[Account]
    out_of_scope: list[Account]


def select_accounts(accounts: list[Account], routes: list[SuiteRoute]) -> SelectionResult:
    routes_by_family: dict[str, list[SuiteRoute]] = {}
    for route in routes:
        routes_by_family.setdefault(route.family, []).append(route)
    for family_routes in routes_by_family.values():
        family_routes.sort(key=lambda r: r.slot)

    candidates_by_family: dict[str, list[Account]] = {}
    out_of_scope: list[Account] = []
    for account in accounts:
        family = KROUTER_PROVIDER_TO_SUITE_FAMILY.get(account.provider)
        if family is None or family not in routes_by_family:
            out_of_scope.append(account)
            continue
        candidates_by_family.setdefault(family, []).append(account)

    assignments: list[SlotAssignment] = []
    overflow: list[Account] = []
    for family, family_routes in routes_by_family.items():
        candidates = sorted(candidates_by_family.get(family, []), key=_rank_key)
        for index, route in enumerate(family_routes):
            account = candidates[index] if index < len(candidates) else None
            assignments.append(SlotAssignment(route=route, account=account))
        overflow.extend(candidates[len(family_routes):])

    assignments.sort(key=lambda a: (a.route.family, a.route.slot))
    return SelectionResult(assignments=assignments, overflow=overflow, out_of_scope=out_of_scope)


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
    """Значение — ТОЛЬКО через stdin (--body-file -), никогда через argv."""
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo, "--body-file", "-"],
        input=value, text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret set {name} упал: {result.stderr.strip()}")


def set_variable(repo: str, name: str, value: str) -> None:
    result = subprocess.run(
        ["gh", "variable", "set", name, "--repo", repo, "--body-file", "-"],
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
    probe_results: dict[str, str],
    suite_status: str,
    apply: bool,
) -> str:
    lines: list[str] = []
    lines.append("## Слоты combo-router (PR #732)\n")
    lines.append("| секрет | провайдер | email | priority | isActive | testStatus | статус | проба |")
    lines.append("|---|---|---|---|---|---|---|---|")
    assigned = 0
    empty = 0
    for slot in selection.assignments:
        name = slot.route.secret_env
        if slot.account is None:
            lines.append(f"| {name} | — | — | — | — | — | нет кандидата | — |")
            empty += 1
            continue
        assigned += 1
        account = slot.account
        status = secret_status.get(name, "?")
        probe = probe_results.get(name, "не проверялась" if not apply else "—")
        lines.append(
            f"| {name} | {account.provider} | {account.email} | {account.priority} | "
            f"{account.is_active} | {account.test_status} | {status} | {probe} |"
        )
    lines.append("")
    lines.append(f"Слотов с кандидатом: {assigned}; слотов без кандидата: {empty}.")
    lines.append("")
    if selection.overflow:
        lines.append("## Не влезли в слоты (тот же провайдер, слотов не хватило)\n")
        for account in selection.overflow:
            lines.append(
                f"- {account.provider} — {account.email} (priority={account.priority}, "
                f"isActive={account.is_active}, testStatus={account.test_status})"
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
    parser.add_argument("--no-probe", action="store_true", help="не делать живые HTTP-запросы к эндпоинтам провайдеров")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.export_file:
        print("::error::не передан файл-экспорт (--export-file или PROVIDER_EXPORT_FILE)", file=sys.stderr)
        return 2

    try:
        data = load_export(args.export_file)
        routes = parse_suite_routes(Path(args.dsh_ci_path))
        accounts = accounts_from_export(data)
        selection = select_accounts(accounts, routes)
        repo = gh_repo(args.repo)
        existing_secrets = existing_secret_names(repo)
        existing_vars = existing_variable_names(repo)
    except LoudError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2

    secret_status: dict[str, str] = {}
    probe_results: dict[str, str] = {}

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

        if args.apply and do_write and not args.no_probe:
            probe_results[name] = probe_provider(slot.route.base_url, value)

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

    print(render_report(selection, secret_status, probe_results, suite_status, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
