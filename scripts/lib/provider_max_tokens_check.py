#!/usr/bin/env python3
"""Проверка по факту (#1062): max_output_tokens в config/provider-usage.json
не превышает ПОДТВЕРЖДЁННЫЙ потолок ответа модели — там, где потолок вообще
подтверждён (scripts/lib/confirmed-provider-models.json::
confirmed_max_output_tokens, необязательное поле рядом с уже существующим
model_sha256).

Класс живого инцидента (issue #1062, полный разбор там): все девять записей
default-chain несли один и тот же непроверенный max_output_tokens, а для
одной модели Ollama Cloud реальный потолок ответа оказался вдвое ниже —
живая ошибка провайдера (прогон worker.yml 34730173870) это подтвердила.
Проверка здесь — по значению ХЭША
id модели (та же схема, что dsh_model_confirmed в scripts/lib/dsh-ci.sh), не
по имени провайдера в цепочке: НОВАЯ запись, ссылающаяся на ту же модель
(другой ключ/аккаунт), унаследует ту же защиту без правки этого файла.

Запись БЕЗ confirmed_max_output_tokens (потолок не подтверждён) — не
нарушение: тишина здесь совпадает с политикой AGENTS.md «не знаешь — пиши
не подтверждено», не с фейл-открыто по умолчанию (# это не она) — просто
здесь нечего проверять.
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import hashlib
import json

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "config" / "provider-usage.json"
DEFAULT_CONFIRMED = REPO_ROOT / "scripts" / "lib" / "confirmed-provider-models.json"


def _model_hash(model: str) -> str:
    return hashlib.sha256(model.encode("utf-8")).hexdigest()


def _confirmed_ceilings(confirmed_path: Path) -> dict[str, int]:
    """hash модели -> confirmed_max_output_tokens, только для записей,
    несущих это поле."""
    data = json.loads(confirmed_path.read_text(encoding="utf-8"))
    ceilings: dict[str, int] = {}
    for entry in data:
        ceiling = entry.get("confirmed_max_output_tokens")
        if ceiling is None:
            continue
        ceilings[entry["model_sha256"]] = int(ceiling)
    return ceilings


def check_max_tokens(
    manifest_path: Path = DEFAULT_MANIFEST,
    confirmed_path: Path = DEFAULT_CONFIRMED,
) -> list[str]:
    """Возвращает список нарушений (пусто — всё в порядке). Каждое нарушение
    называет цепочку, имя провайдера, модель, сконфигурированное значение и
    подтверждённый потолок — правило AGENTS.md «Алерт не гадает»."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ceilings = _confirmed_ceilings(confirmed_path)
    violations: list[str] = []
    for chain_name, entries in manifest.get("chains", {}).items():
        for entry in entries:
            model = entry.get("model", "")
            ceiling = ceilings.get(_model_hash(model))
            if ceiling is None:
                continue
            configured = entry.get("max_output_tokens")
            if configured is None:
                continue
            if int(configured) > ceiling:
                violations.append(
                    f"цепочка '{chain_name}', провайдер '{entry.get('name', '?')}' "
                    f"(модель '{model}'): max_output_tokens={configured} превышает "
                    f"подтверждённый потолок {ceiling} "
                    f"({confirmed_path.name})"
                )
    return violations


if __name__ == "__main__":
    found = check_max_tokens()
    if found:
        for line in found:
            print(f"::error::{line}")
        raise SystemExit(1)
    print("provider_max_tokens_check: все проверяемые записи в пределах подтверждённого потолка")
