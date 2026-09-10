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

СЕМАНТИКА isActive/testStatus — состояние ПРЕДОХРАНИТЕЛЯ, не факт о ключе
(задача #777): роутер учёток (krouter) сам гасит учётку (isActive=False,
testStatus=unavailable) при исчерпании квоты и включает её обратно, когда та
восстановится — файл-экспорт лишь снимок на дату выгрузки (см. --export-file,
дата печатается в отчёте). Ранжирование поэтому различает ТРИ РАЗНЫЕ оси, а
не одну: «квота временно исчерпана» (429/backoff — нормальное состояние
ротации, НЕ дисквалификация); «доступ запрещён» (403 — неоднозначный сигнал,
может быть гео-блок/WAF/лимит плана при годном ключе, ранг хуже квоты, но БЕЗ
выброса из слота — 403 не одноразово надёжен, задача #777 major 5 гейта
PR #778); «ключ неверен» (401 — единственный код настоящей дисквалификации,
выброс из слота, и то лишь когда источник факта — живая проба, не снимок).
Источник факта для ранга — живая проба (--no-probe отключает), она пробует
ТОЛЬКО кандидатов на слоты suite (не весь файл — из непричастных к suite
учёток пробовать нечего, у них нет маршрута); когда пробы нет (сеть
недоступна или --no-probe) — падаем на классификацию снимка по
errorCode/backoffLevel. Источник ранга (проба/снимок) виден в отчёте
отдельной пометкой у каждой строки.

Использование:
  python3 scripts/lib/provider_secrets_import.py --export-file <путь> [--apply]
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

# Бутстрап выше настраивает stdout/stderr ОДИН раз, в момент импорта этого
# файла — для обычного запуска этого достаточно (sys.stdout уже финальный к
# моменту старта процесса). Но main() держит явный, повторный доступ к тому
# же ensure_utf8_stdio() (не вторую реализацию — тот же _console_utf8_spec,
# просто новый экземпляр модуля) и зовёт его ещё раз ПЕРВОЙ строкой main():
# нужно для случая, когда sys.stdout подменяют ПОСЛЕ импорта (issue #791,
# тест test_print_report_reconfigures_stdout_to_utf8_on_cp1251_console —
# прод-форма перехваченного stdout собирается уже после того, как модуль
# импортирован). Раньше это делала приватная _ensure_utf8_stdout() — теперь
# тот же эффект даёт повторный вызов канонического хелпера, без второго
# источника правды на сам механизм reconfigure.
console_utf8 = importlib.util.module_from_spec(_console_utf8_spec)
_console_utf8_spec.loader.exec_module(console_utf8)

import argparse
import datetime
import http.client
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

# Тир ранга (меньше — лучше слоту). ТРИ РАЗНЫЕ оси, не смешиваются (задача
# #777, major 5 гейта PR #778): TIER_TEMPORARY (квота/backoff) — нормальное
# состояние ротации, оно ЛУЧШЕ TIER_SUSPECT и TIER_DISQUALIFIED при любом
# источнике факта. TIER_SUSPECT (HTTP 403) хуже TIER_UNKNOWN, но НЕ выбрасывает
# кандидата из слота — только TIER_DISQUALIFIED (HTTP 401) это делает, и то
# лишь когда источник факта — живая проба (см. select_accounts).
_TIER_HEALTHY = 0
_TIER_TEMPORARY = 1
_TIER_UNKNOWN = 2
_TIER_SUSPECT = 3
_TIER_DISQUALIFIED = 4

# HTTP 401 — единственный код, которым и живая проба (probe_provider), и
# снимок (errorCode) выбрасывают кандидата из слота ("ключ неверен",
# настоящая дисквалификация). HTTP 403 у OpenAI-совместимых шлюзов — НЕ то же
# самое: это ещё и гео-блок/WAF/политика организации/лимит плана при годном
# ключе. Поэтому 403 — ХУЖЕ ранг (TIER_SUSPECT), но НЕ выброс из слота; выброс
# остаётся только за подтверждённым 401.
#
# НЕ ПОДТВЕРЖДЕНО (минор 5 гейта PR #781): для самого HTTP 403 отдельного
# замера нестабильности нет. Единственное живое наблюдение задачи #777 (один
# и тот же шлюз вернул 451 автору находки и 200 гейту минутами позже, проба
# одноразовая, повторов нет) относится к HTTP 451, который этот код
# классифицирует как PROBE_UNKNOWN (см. probe_provider), а не
# PROBE_SUSPECT_FORBIDDEN — оно доказывает нестабильность ответа шлюза
# ВООБЩЕ, но не обосновывает политику именно для кода 403. Осторожность
# (ранжировать хуже, но не выбрасывать) принята по общей репутации
# гео-блок/WAF/лимит плана у OpenAI-совместимых шлюзов, а не по измерению
# для 403 конкретно.
_AUTH_INVALID_HTTP_CODES = (401,)
_AUTH_SUSPECT_HTTP_CODES = (403,)

# Контракт между probe_provider и classify_probe — сентинел-константы, НЕ
# русская проза (minor 9 гейта PR #778): раньше classify_probe распознавал
# результат пробы по строковому совпадению/префиксу человеческого текста —
# правка формулировки в probe_provider молча меняла бы классификацию (пример
# в находке: детальный текст сетевой ошибки менялся, а префикс "неизвестно
# (сеть:" был единственным, что отличало "сеть недоступна" от "факт получен").
# ProbeResult.detail остаётся человеко-читаемым и безопасным (без ключа/тела
# ответа) только для отчёта; классификация читает исключительно .outcome.
PROBE_ALIVE = "alive"
PROBE_QUOTA = "quota"
PROBE_INVALID_KEY = "invalid_key"
PROBE_SUSPECT_FORBIDDEN = "suspect_forbidden"
PROBE_NETWORK_UNAVAILABLE = "network_unavailable"
PROBE_UNKNOWN = "unknown"


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
    основание ВЫБРОСА по снимку — errorCode 401 (ключ неверен). errorCode 403
    (доступ запрещён) — неоднозначный сигнал (может быть гео-блок/WAF/лимит
    плана при годном ключе, см. major 5 гейта PR #778) — ранжируется хуже
    квоты, но кандидата НЕ выбрасывает. errorCode 429 или backoffLevel>0 — та
    же ось, что и живая проба 429: квота, нормальное состояние ротации.

    Порядок проверок НАМЕРЕННЫЙ (minor 7 гейта PR #778): testStatus=active
    проверяется ПЕРВЫМ, раньше errorCode. Обоснование ограничено тем, что
    видно в файле-снимке владельца (2026-08-25, 47 учёток): у всех троих
    записей с одновременно isActive=True/testStatus=active И непустым
    errorCode код — 429 или 503 (квота/перегрузка), ни разу 401/403 — то есть
    на доступных данных конфликта "активна, но код говорит про неверный ключ"
    не наблюдалось. Обнуляет ли krouter errorCode при возврате
    учётки в строй — НЕ ПОДТВЕРЖДЕНО (внутренности krouter не в этой
    задаче); если такой конфликт когда-нибудь появится в реальном экспорте,
    testStatus=active сейчас победит errorCode=401/403 молча — это осознанно
    принятый риск, не оплошность, и первое, что проверить, если слот займёт
    учётка с застрявшим кодом дисквалификации."""
    if account.is_active and account.test_status == "active":
        return _TIER_HEALTHY, "активна по снимку (testStatus=active) [источник: снимок]"
    if account.error_code in _AUTH_INVALID_HTTP_CODES:
        return (
            _TIER_DISQUALIFIED,
            f"ключ неверен по снимку (errorCode={account.error_code}) [источник: снимок]",
        )
    if account.error_code in _AUTH_SUSPECT_HTTP_CODES:
        return (
            _TIER_SUSPECT,
            f"доступ запрещён по снимку (errorCode={account.error_code}) — неоднозначный "
            "сигнал (гео-блок/WAF/лимит плана, не обязательно неверный ключ), тир ниже "
            "квоты, слот не освобождается [источник: снимок]",
        )
    if account.error_code == 429 or account.backoff_level > 0:
        return (
            _TIER_TEMPORARY,
            "квота/backoff по снимку — временное состояние ротации, не дисквалификация "
            "[источник: снимок]",
        )
    if account.error_code is None:
        return (
            _TIER_UNKNOWN,
            "неопределённо по снимку (errorCode отсутствует) [источник: снимок]",
        )
    # last_error — сырое эхо ответа krouter, уже присутствует в файле-экспорте
    # (значит и так в руках оператора, не сетевой вызов) — до этой правки поле
    # не читал никто (minor 9 гейта PR #778): здесь единственное место, где
    # оно даёт контекст к "неопределённо", вместо того чтобы простаивать.
    detail = f", lastError={account.last_error!r}" if account.last_error else ""
    return (
        _TIER_UNKNOWN,
        f"неопределённо по снимку (errorCode={account.error_code}, не 401/403/429{detail}) "
        "[источник: снимок]",
    )


@dataclass(frozen=True)
class ProbeResult:
    """Возврат живой пробы (probe_provider/probe_provider_full) — см.
    контракт-константы PROBE_* выше. detail — только для отчёта человеку,
    никогда не несёт ключ/заголовок/тело ответа целиком (задача #777,
    критерий 6). models/models_error заполняются ТОЛЬКО когда
    outcome == PROBE_ALIVE и только если тело ответа /models разобралось
    (иначе models_error называет, что ожидалось и что пришло — по структуре,
    не по значениям; задача #783/PR #785 — импортёр раньше на неузнанной
    структуре ответа выбрасывал необработанный трейсбек с телом ответа
    сервера в выводе, вместо печати только id моделей)."""
    outcome: str
    detail: str
    models: tuple[str, ...] | None = None
    models_error: str | None = None


def classify_probe(result: ProbeResult) -> tuple[int, str] | None:
    """Ранг по РЕЗУЛЬТАТУ живой пробы (probe_provider). None — проба не
    получила ответа от сети (недоступна), вызывающий обязан упасть на
    classify_snapshot и пометить это в отчёте. Читает только result.outcome
    (сентинел-константа), не человеческий текст result.detail (minor 9
    гейта PR #778)."""
    if result.outcome == PROBE_NETWORK_UNAVAILABLE:
        return None
    if result.outcome == PROBE_ALIVE:
        return _TIER_HEALTHY, "жива [источник: проба]"
    if result.outcome == PROBE_QUOTA:
        return _TIER_TEMPORARY, "квота исчерпана [источник: проба] — не дисквалификация"
    if result.outcome == PROBE_INVALID_KEY:
        return _TIER_DISQUALIFIED, "ключ неверен [источник: проба]"
    if result.outcome == PROBE_SUSPECT_FORBIDDEN:
        return (
            _TIER_SUSPECT,
            "доступ запрещён по пробе (HTTP 403) — неоднозначный сигнал (гео-блок/WAF/"
            "лимит плана, не обязательно неверный ключ), тир ниже квоты, слот НЕ "
            "выбрасывается [источник: проба]",
        )
    return _TIER_UNKNOWN, f"неизвестно ({result.detail}) [источник: проба]"


def rank_account(account: Account, probe_result: ProbeResult | None) -> tuple[int, str, str]:
    """(tier, заметка, источник). Живая проба, если получила ответ по сети, —
    авторитетный источник факта; иначе (проба выключена/недоступна по сети) —
    снимок файла-экспорта."""
    if probe_result is not None:
        classified = classify_probe(probe_result)
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
) -> dict[str, ProbeResult]:
    """Живая проба ТОЛЬКО кандидатов на слоты suite (задача #777) — учётки вне
    списка маршрутов (out_of_scope) пробовать бессмысленно, у них нет слота,
    который проба могла бы переранжировать. На файле владельца (2026-08-25,
    47 учёток — тот же замер, что и в classify_snapshot выше) это 9
    кандидатов — пробовать все 47 значило бы тратить сеть на 38 учёток, чей
    результат пробы ни на что не влияет (минор 7 гейта PR #781: раньше
    здесь жил второй, разошедшийся замер — 46/37 — того же файла).

    Возвращает ProbeResult (классификация + разобранные id моделей, задача
    #783/PR #785 — probe_provider_full больше не выбрасывает тело ответа,
    а разбирает id моделей из него), не голый статус — вызывающий сам
    решает, что показывать в отчёте (см. main/render_report), ранжирование
    по-прежнему смотрит только на .outcome."""
    results: dict[str, ProbeResult] = {}
    for family, accounts in candidates_by_family.items():
        family_routes = routes_by_family.get(family)
        if not family_routes:
            continue
        base_url = family_routes[0].base_url
        for account in accounts:
            value = secret_value(account)
            if value is None:
                continue
            results[account.id] = probe_provider_full(base_url, value)
    return results


def select_accounts(
    accounts: list[Account],
    routes: list[SuiteRoute],
    probe_results: dict[str, ProbeResult] | None = None,
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

        # Minor 6 гейта PR #778: причина пустоты по пробе — не факт о семье
        # маршрутов целиком, а факт про КОНКРЕТНОЕ число пустых слотов.
        # 3 слота с 1 выброшенным пробой кандидатом и без других кандидатов
        # (пример: ollama, задача #777) — пустых 3, а живо-исключён 1: только
        # ПЕРВЫЙ пустой слот получает причину «исключён пробой», остальные —
        # «нет кандидата» (иначе читатель решит, что починка одного ключа
        # заполнит все три).
        empty_reason = None
        if dead_by_probe:
            names = ", ".join(account.email or account.id for account in dead_by_probe)
            empty_reason = (
                f"{len(dead_by_probe)} кандидат(ов) исключены живой пробой "
                f"(ключ неверен): {names}"
            )
        # Minor 3 гейта PR #781: код теперь делает то, что уже обещал
        # комментарий выше — ТОЛЬКО первый пустой слот несёт агрегатную
        # причину, остальные пустые слоты семьи получают «нет кандидата».
        # Раньше budget = len(dead_by_probe) отдавал ОДНУ И ТУ ЖЕ агрегатную
        # строку первым N пустым слотам (N = число выброшенных) — при 2
        # выброшенных и 3 слотах строка повторялась в двух слотах, и
        # читатель, складывающий числа из повторов, получал 4 исключённых
        # при фактических 2-х.
        empty_reason_budget = 1 if dead_by_probe else 0

        for index, route in enumerate(family_routes):
            account = viable[index] if index < len(viable) else None
            slot_empty_reason = None
            if account is None and empty_reason_budget > 0:
                slot_empty_reason = empty_reason
                empty_reason_budget -= 1
            assignments.append(
                SlotAssignment(
                    route=route,
                    account=account,
                    empty_reason=slot_empty_reason,
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
        capture_output=True, text=True, encoding="utf-8",
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
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret list упал: {result.stderr.strip()}")
    return {item["name"] for item in json.loads(result.stdout)}


def existing_variable_names(repo: str) -> set[str]:
    result = subprocess.run(
        ["gh", "variable", "list", "--repo", repo, "--json", "name"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise LoudError(f"gh variable list упал: {result.stderr.strip()}")
    return {item["name"] for item in json.loads(result.stdout)}


def set_secret(repo: str, name: str, value: str) -> None:
    """Значение — ТОЛЬКО через stdin (--body-file -), никогда через argv."""
    result = subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo, "--body-file", "-"],
        input=value, text=True, capture_output=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise LoudError(f"gh secret set {name} упал: {result.stderr.strip()}")


def set_variable(repo: str, name: str, value: str) -> None:
    result = subprocess.run(
        ["gh", "variable", "set", name, "--repo", repo, "--body-file", "-"],
        input=value, text=True, capture_output=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise LoudError(f"gh variable set {name} упал: {result.stderr.strip()}")


# ── Живая проба ───────────────────────────────────────────────────────────


class ModelListError(LoudError):
    """Ответ /models пришёл в структуре, которую парсер не понимает."""


def parse_model_ids(base_url: str, body: bytes) -> list[str]:
    """Разбор тела ответа /models — печатает ТОЛЬКО имена моделей (задача
    «импортёр печатает доступные id моделей», #783/PR #785), не всё тело.

    Формат проверен живым запросом ко всем четырём провайдерам suite
    (2026-09-09, задача) — у всех ОДИН И ТОТ ЖЕ top-level контракт,
    OpenAI-совместимый: dict с ключом "data" — список объектов со строковым
    полем "id". Различаются только ЛИШНИЕ поля элемента, которые эта функция
    не использует:
      - nvidia-nim, zai (GLM), ollama-cloud: created/id/object/owned_by;
      - openrouter: вдобавок architecture/pricing/context_length/… (431
        моделей на момент проверки — то же поле "id", просто больше шума).
    Ни разного формата, ни отличного от OpenAI top-level контракта среди
    четырёх проверенных провайдеров НЕ найдено — если он встретится у нового
    провайдера, эта функция обязана упасть громко (см. ниже), а не угадать.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise ModelListError(f"{base_url}: ответ /models не JSON: {error}") from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
        top_keys = sorted(parsed.keys()) if isinstance(parsed, dict) else None
        raise ModelListError(
            "неузнанная структура ответа /models: ожидался dict с ключом "
            "'data' (список объектов со строковым полем 'id'). Получено: "
            f"top-level {type(parsed).__name__}"
            + (f", ключи={top_keys}" if top_keys is not None else "")
            + f" ({base_url}). Значения тела не печатаются."
        )
    ids: list[str] = []
    for index, item in enumerate(parsed["data"]):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            item_keys = sorted(item.keys()) if isinstance(item, dict) else None
            raise ModelListError(
                f"неузнанная структура data[{index}] ответа /models: ожидался "
                "объект со строковым полем 'id'. Получено: "
                f"{type(item).__name__}"
                + (f", ключи={item_keys}" if item_keys is not None else "")
                + f" ({base_url}). Значения тела не печатаются."
            )
        ids.append(item["id"])
    return sorted(set(ids))


def probe_provider_full(base_url: str, api_key: str) -> ProbeResult:
    """ProbeResult(outcome, detail, models, models_error) — эвристика по
    HTTP-статусу общего для OpenAI-совместимых шлюзов эндпоинта /models.
    401 — ключ неверен (тир дисквалификации), 403 — доступ запрещён, но НЕ то
    же самое (гео-блок/WAF/лимит плана при годном ключе — major 5 гейта
    PR #778, отдельный тир БЕЗ выброса из слота), 429 — квота исчерпана
    (разное лечение, не смешиваем). outcome — сентинел-константа PROBE_*
    (контракт с classify_probe, minor 9 гейта PR #778), detail — только для
    отчёта человеку.

    На успехе (200) заодно разбирает список id моделей из уже полученного
    тела ответа (задача #783/PR #785 — раньше при неузнанной структуре тела
    падал необработанный трейсбек с телом ответа сервера в выводе): models
    заполняется только если тело разобралось, иначе models_error называет
    структуру ожидаемого/полученного, не значения. Один HTTP-запрос на оба
    факта (классификация + модели).

    Ни при какой ветке функция не печатает и не возвращает тело ответа/
    сообщение исключения целиком — только классификацию по коду/типу
    исключения (задача #777, критерий 6; блокирующая 2 гейта PR #778:
    печать str(error) или error.msg — тот же класс утечки, что печать
    значения ключа, если сервер эхом отражает что-то из запроса в тексте
    ошибки).

    Блокирующая 1 гейта PR #778: h.getresponse() внутри urlopen() может
    выбросить исключения, которые urllib НЕ оборачивает в URLError —
    ConnectionResetError/TimeoutError (оба OSError) и http.client.
    BadStatusLine и другие http.client.HTTPException (обрыв/таймаут/мусор
    вместо статус-строки). Раньше это падало необработанным трейсбеком и
    валило весь импортёр (probe_candidates — первый шаг main()) из-за
    ОДНОГО медленного/нестабильного шлюза среди девяти. Тот же тир, что и
    URLError, — «недоступна по сети», без данных для дисквалификации."""
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            if response.status == 200:
                body = response.read()
                try:
                    return ProbeResult(PROBE_ALIVE, "HTTP 200", models=tuple(parse_model_ids(base_url, body)))
                except ModelListError as error:
                    return ProbeResult(PROBE_ALIVE, "HTTP 200", models_error=str(error))
            return ProbeResult(PROBE_UNKNOWN, f"HTTP {response.status}")
    except urllib.error.HTTPError as error:
        if error.code == 401:
            return ProbeResult(PROBE_INVALID_KEY, "HTTP 401")
        if error.code == 403:
            return ProbeResult(PROBE_SUSPECT_FORBIDDEN, "HTTP 403")
        if error.code == 429:
            return ProbeResult(PROBE_QUOTA, "HTTP 429")
        return ProbeResult(PROBE_UNKNOWN, f"HTTP {error.code}")
    except urllib.error.URLError as error:
        return ProbeResult(PROBE_NETWORK_UNAVAILABLE, f"сеть: {error.reason}")
    except (OSError, http.client.HTTPException) as error:
        return ProbeResult(PROBE_NETWORK_UNAVAILABLE, f"сеть: {type(error).__name__}")


# Alias — оставлен ради обратной совместимости вызовов/тестов, которым нужна
# только классификация (probe_provider), без явного упоминания разбора
# моделей: probe_provider_full — единственная реализация живой пробы (задача
# #777/#781 не делит её на "статус" и "статус+модели", один HTTP-запрос даёт
# оба факта сразу — см. probe_provider_full).
probe_provider = probe_provider_full


# ── Отчёт ─────────────────────────────────────────────────────────────────


# Категории для агрегатной строки пробы (minor 8 гейта PR #778) — читают
# result.outcome (сентинел-константу), не текст, тот же контракт, что
# classify_probe (minor 9).
_PROBE_BUCKET_LABELS: dict[str, str] = {
    PROBE_ALIVE: "жива",
    PROBE_QUOTA: "квота",
    PROBE_INVALID_KEY: "ключ неверен",
    PROBE_SUSPECT_FORBIDDEN: "доступ запрещён (403)",
    PROBE_NETWORK_UNAVAILABLE: "сеть недоступна",
    PROBE_UNKNOWN: "неизвестно",
}


def _probe_bucket(result: ProbeResult) -> str:
    return _PROBE_BUCKET_LABELS.get(result.outcome, "неизвестно")


def render_report(
    selection: SelectionResult,
    secret_status: dict[str, str],
    suite_status: str,
    apply: bool,
    export_date: str,
    no_probe: bool,
    probe_results: dict[str, ProbeResult],
    existing_secrets: set[str],
    repo: str,
) -> str:
    lines: list[str] = []
    lines.append(f"Снимок экспорта датирован: {export_date}.")
    lines.append(
        "`isActive`/`testStatus` в таблице ниже — состояние ПРЕДОХРАНИТЕЛЯ роутера учёток "
        "на дату снимка (квота исчерпана → учётка гасится, квота вернулась → включается "
        "обратно), а НЕ факт о текущей пригодности ключа. Колонка «ранг» называет "
        "источник вывода: живая проба (текущий факт) либо снимок."
    )
    # Minor 8 гейта PR #778: режим пробы — это факт из аргументов CLI, а не
    # догадка по отсутствию результата у конкретной строки. Печатаем прямо,
    # плюс агрегат по всем пробованным кандидатам, чтобы числа из PR можно
    # было прочитать в самом выводе инструмента, а не пересчитывать руками.
    if no_probe:
        lines.append("Проба: выключена флагом `--no-probe` — весь ранг ниже взят из снимка.")
    else:
        buckets: dict[str, int] = {}
        for status in probe_results.values():
            bucket = _probe_bucket(status)
            buckets[bucket] = buckets.get(bucket, 0) + 1
        # Блокирующая 2 гейта PR #781: порядок берётся из _PROBE_BUCKET_LABELS
        # (единственное место правды, объявлено рядом с PROBE_*) — раньше
        # здесь жила ВТОРАЯ копия множества исходов литералом; исход,
        # присутствующий в buckets, но отсутствующий в этой копии, молча
        # выпадал из разбивки, хотя влиял на len(probe_results) в тотале.
        order = list(_PROBE_BUCKET_LABELS.values())
        parts = [f"{label} {buckets[label]}" for label in order if buckets.get(label)]
        assert sum(buckets.values()) == len(probe_results)
        lines.append(
            f"Проба: {len(probe_results)} кандидат(ов) пробовано — " + (", ".join(parts) if parts else "нет данных") + "."
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
            # Major 4 гейта PR #778: «пусто» неоднозначно, если секрет уже
            # существует — этот скрипт секреты не удаляет, значит маршрут в
            # suite остаётся ВКЛЮЧЁН с ключом, который проба/снимок только что
            # признали мёртвым/сомнительным. Различаем две ситуации явно.
            if name in existing_secrets:
                reason = (
                    f"{reason} — секрет {name} уже существует и НЕ будет удалён (скрипт "
                    "секреты не удаляет), маршрут в suite останется включён со старым "
                    # Minor 6 гейта PR #781: repo уже известен вызывающему (main())
                    # и уже передаётся в render_report для остального отчёта — команда
                    # обязана копипаститься без правки владельцем, не носить плейсхолдер.
                    f"ключом. Газ: `gh secret delete {name} --repo {repo}` вручную "
                    "либо замени ключ на живой в экспорте и повтори прогон с --apply"
                )
            lines.append(f"| {name} | — | — | — | — | — | пусто: {reason} | — |")
            empty += 1
            continue
        assigned += 1
        account = slot.account
        status = secret_status.get(name, "?")
        tier, note, _source = selection.rank_by_id.get(account.id, (None, "?", "?"))
        # Minor 4 гейта PR #781: TIER_SUSPECT (403) занимает слот, ключ будет
        # записан при --apply, маршрут включён в suite — раньше единственным
        # текстом был перечень гипотез (гео-блок/WAF/лимит плана), без ответа
        # на «что делать». Сравнимо с пустым слотом при существующем секрете
        # (:762 выше), где газ назван прямо.
        if tier == _TIER_SUSPECT:
            note = (
                f"{note}. Газ при устойчивом 403: если несколько прогонов подряд дают тот "
                "же 403 для этой учётки, считай ключ вероятно мёртвым и либо замени его в "
                "роутере учёток, либо погаси учётку вручную (isActive=False), прежде чем "
                "давать ей слот следующим прогоном"
            )
        lines.append(
            f"| {name} | {account.provider} | {account.email} | {account.priority} | "
            f"{account.is_active} | {account.test_status} | {note} | {status} |"
        )
    lines.append("")
    lines.append(f"Слотов с кандидатом: {assigned}; слотов без кандидата: {empty}.")
    lines.append("")
    seen_secrets: set[str] = set()
    model_slots = [
        slot for slot in selection.assignments
        if slot.account is not None and slot.account.id in probe_results
    ]
    if model_slots:
        lines.append("## Доступные id моделей провайдеров (живая проба, /models)\n")
        lines.append(
            "Тот же HTTP-запрос, что дал ранг выше — тело ответа больше не выбрасывается. "
            "Список полный, без фильтрации (см. обоснование в задаче: цель — точный id для "
            "`vars.DSH_PROVIDER_CHAIN`, а не сокращённая выборка на глаз); id ВСЁ РАВНО обязан "
            "пройти реестр подтверждённых моделей (`scripts/lib/confirmed-provider-models.json`, "
            "#737) до попадания в цепочку."
        )
        for slot in model_slots:
            name = slot.route.secret_env
            if name in seen_secrets:
                continue
            seen_secrets.add(name)
            result = probe_results[slot.account.id]
            lines.append(f"\n### `{name}` — base_url `{slot.route.base_url}`\n")
            if result.outcome != PROBE_ALIVE:
                lines.append(f"- проба: {_probe_bucket(result)} — список моделей недоступен.")
                continue
            if result.models_error:
                lines.append(f"- разбор тела ответа не удался: {result.models_error}")
                continue
            if not result.models:
                lines.append("- проба не запрашивала модели (внутренняя ошибка — models пуст на статусе «жива»).")
                continue
            lines.append(f"- {len(result.models)} моделей:")
            for model_id in result.models:
                lines.append(f"  - `{model_id}`")
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
    # Повторный явный вызов (issue #723/#791) — см. комментарий у bootstrap-блока
    # выше: подхватывает sys.stdout, даже если он был подменён ПОСЛЕ импорта.
    console_utf8.ensure_utf8_stdio()
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
        probe_results: dict[str, ProbeResult] = (
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

    report = render_report(
        selection, secret_status, suite_status, args.apply, export_date,
        no_probe=args.no_probe, probe_results=probe_results, existing_secrets=existing_secrets,
        repo=repo,
    )
    try:
        print(report)
    except UnicodeEncodeError as error:
        # Необратимые действия выше (set_secret/set_variable при --apply) УЖЕ
        # выполнены к этому моменту — сбой ИМЕННО печати отчёта не имеет права
        # превратить успешную запись в exit 1 (AGENTS.md, «Fail loud, не
        # silent-wrong»: «работа не выполнена» и «работа выполнена, но отчёт не
        # показан» — разные факты, лечатся по-разному, подменять один другим
        # нельзя). console_utf8.ensure_utf8_stdio() выше должен был предотвратить
        # это на практике (issue #723/#791) — этот except остаётся как гарантия
        # атомарности кода возврата на случай, если reconfigure недоступен/не
        # сработал (см. её докстринг), а не как основной путь.
        #
        # stderr в CPython по умолчанию errors="backslashreplace" (никогда не
        # падает на не-ASCII) — сюда уходит и громкое сообщение о сбое печати, и
        # сам текст отчёта, чтобы факт не потерялся молча.
        print(
            f"::error::отчёт не напечатан в stdout ({error}) — секреты/переменная "
            "выше уже записаны (если был --apply); текст отчёта ниже:",
            file=sys.stderr,
        )
        print(report, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
