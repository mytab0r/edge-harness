#!/usr/bin/env python3
"""Персистентное состояние квоты LLM-провайдеров цепочки (#857,
openspec/changes/provider-quota-gating) — {provider_name: reset_iso},
живущее в репо-переменной `vars.DSH_PROVIDER_QUOTA_UNTIL`.

Носитель и обоснование — design.md этого change, «Носитель состояния».
Коротко: `vars.*` читается контекстом workflow (`${{ vars.X }}`) БЕЗ
токена и БЕЗ сетевого вызова — тот же путь, что уже несёт
`vars.DSH_PROVIDER_CHAIN` во всех трёх consumer'ах (`ai-review.yml`,
`worker.yml`, `hands.yml`), включая недоверенный DSH-шаг `ai-review.yml`
(#18), у которого нет GitHub-токена вовсе. Значит чтение состояния не
расширяет границу доверия ни на бит. Пишет только пульс оркестратора
(`scheduler.py::sync_provider_quota_state`, у него уже есть
`GH_PIPELINE_PAT`/`ORCHESTRA_PAT`) — гвардия границы в
`scripts/lib/test_provider_quota_state_guard.py`.

Этот модуль — только логика (чистые функции + два IO-метода, принимающие
`gh_func` инъекцией, как остальные модули scripts/orchestra/scripts/lib
этого репозитория): не читает окружение, не решает, КОГДА синхронизировать
— это делает вызывающий (`scheduler.py`).
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
from datetime import datetime, timezone

# Одно место правды на имя переменной — bash-сторона (scripts/lib/dsh-ci.sh)
# читает её буквальным именем `vars.DSH_PROVIDER_QUOTA_UNTIL` в трёх
# workflow-файлах; гвардия test_provider_quota_state_guard.py сверяет, что
# буквальная строка везде совпадает с этой константой.
QUOTA_VAR_NAME = "DSH_PROVIDER_QUOTA_UNTIL"

# Формат хранения даты в самой переменной — канонический ISO с Z (тот же,
# что уже печатает dsh-ci.sh::dsh_extract_reset_hint в прод-форме ISO), не
# зависит от того, в каком из двух форматов провайдер прислал дату в
# stderr (см. parse_reset_hint_pairs ниже).
_STORAGE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_reset_hint_pairs(reset_hint: str) -> dict[str, datetime]:
    """«Name: date; Name2: date2» (DSH_CHAIN_RESET_HINT, scripts/lib/dsh-ci.sh)
    -> {name: datetime UTC}. Прод-форма встречается в ДВУХ видах дат — ISO
    («2026-09-10T00:00:00Z») и «YYYY-MM-DD HH:MM:SS» без зоны (живой
    прогон 34176910458, читается как UTC — dsh зону не называет, это
    ближайшее разумное допущение, не факт, см. docs/runbooks/
    switch-llm-provider.md). Единственное место, разбирающее эту строку —
    scheduler.py::parse_reset_hint_dates делегирует сюда (одно место
    правды на формат, класс AGENTS.md).

    Чанк, не разобравшийся ни одним форматом, или без имени до двоеточия,
    пропускается молча — вызывающий трактует отсутствие ключа как «дата
    неизвестна», не как «дата в прошлом»."""
    pairs: dict[str, datetime] = {}
    for chunk in (reset_hint or "").split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, _, raw = chunk.partition(":")
        name = name.strip()
        raw = raw.strip()
        if not name:
            continue
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                pairs[name] = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    return pairs


def merge_reset_hints(
    state: dict[str, str], reset_hint: str, now: datetime
) -> tuple[dict[str, str], bool]:
    """Мержит свежий reset-хинт в состояние. Пишет ТОЛЬКО даты строго в
    будущем относительно `now` (уже прошедшая дата не несёт пользы гейту —
    dsh_provider_quota_gate_skip и так пропустит её при чтении, но не
    засорять состояние старьём). Возвращает (новое_состояние, changed) —
    changed=False, если ничего реально не изменилось (та же дата уже
    записана), чтобы вызывающий не тратил PATCH впустую."""
    pairs = parse_reset_hint_pairs(reset_hint)
    new_state = dict(state)
    changed = False
    for name, reset_dt in pairs.items():
        if reset_dt <= now:
            continue
        iso = reset_dt.strftime(_STORAGE_FORMAT)
        if new_state.get(name) != iso:
            new_state[name] = iso
            changed = True
    return new_state, changed


def expire_stale(state: dict[str, str], now: datetime) -> tuple[dict[str, str], list[str]]:
    """Снимает записи, чей срок сброса уже прошёл, или которые не
    разбираются как дата вовсе (мусор — лучше снять, чем плодить). Это и
    есть газ «срок прошёл» из AGENTS.md «Тормоз без газа не принимается».
    Возвращает (новое_состояние, имена_снятых) — пустой список snapshot
    имён означает «менять нечего», вызывающий трактует это как
    changed=False."""
    new_state: dict[str, str] = {}
    expired: list[str] = []
    for name, iso in state.items():
        try:
            reset_dt = datetime.strptime(iso, _STORAGE_FORMAT).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            expired.append(name)
            continue
        if reset_dt <= now:
            expired.append(name)
            continue
        new_state[name] = iso
    return new_state, expired


def load_quota_state(repo: str, gh_func) -> dict[str, str]:
    """Читает `vars.DSH_PROVIDER_QUOTA_UNTIL`. Переменная не заведена
    (404) -> {} — нормальное состояние «квот ещё не помним», НЕ ошибка
    (первый запуск этого механизма, до первой записи пульсом). Значение
    есть, но не JSON-объект {provider: reset_iso} -> RuntimeError —
    вызывающий обязан поймать её и не ронять остальной тик пульса
    (второстепенный механизм, см. scheduler.py::sync_provider_quota_state)."""
    try:
        resp = gh_func(f"repos/{repo}/actions/variables/{QUOTA_VAR_NAME}")
    except RuntimeError as error:
        if "404" in str(error):
            return {}
        raise
    raw = (resp or {}).get("value", "") if isinstance(resp, dict) else ""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"vars.{QUOTA_VAR_NAME}: невалидный JSON ({error})") from error
    if not isinstance(data, dict):
        raise RuntimeError(
            f"vars.{QUOTA_VAR_NAME}: ожидался JSON-объект {{provider: reset_iso}}, "
            f"получено {type(data).__name__}"
        )
    return {str(key): str(value) for key, value in data.items()}


def save_quota_state(repo: str, state: dict[str, str], gh_func) -> None:
    """Пишет состояние. PATCH обновляет существующую переменную; если её
    ещё не существует (404 — самая первая запись механизма), падает на
    POST, создающий её."""
    body = json.dumps(state, ensure_ascii=False, sort_keys=True)
    try:
        gh_func(
            "-X", "PATCH", f"repos/{repo}/actions/variables/{QUOTA_VAR_NAME}",
            "-f", f"name={QUOTA_VAR_NAME}", "-f", f"value={body}",
        )
    except RuntimeError as error:
        if "404" not in str(error):
            raise
        gh_func(
            "-X", "POST", f"repos/{repo}/actions/variables",
            "-f", f"name={QUOTA_VAR_NAME}", "-f", f"value={body}",
        )
