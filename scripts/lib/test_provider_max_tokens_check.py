#!/usr/bin/env python3
"""Гвардия scripts/lib/provider_max_tokens_check.py (#1062, живой инцидент —
прогон worker.yml 34730173870): config/provider-usage.json несёт
max_output_tokens, который не превышает ПОДТВЕРЖДЁННЫЙ потолок ответа модели
(scripts/lib/confirmed-provider-models.json::confirmed_max_output_tokens).

Три случая:
  1. Реальный манифест репозитория — регрессионный свидетель: ноль нарушений
     СЕЙЧАС (после фикса #1062). Ловит будущий откат правки (кто-то вернёт
     131072 копипастой — тот же живой класс, что уже случился).
  2. Синтетическая мутация: запись со значением ВЫШЕ подтверждённого потолка
     — обязана попасть в список нарушений с именем провайдера/модели/чисел.
  3. Запись БЕЗ подтверждённого потолка (модель не в реестре) — не
     нарушение, тишина, а не фейл-открыто по умолчанию.

Запуск: python -m pytest scripts/lib/test_provider_max_tokens_check.py -q
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
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_max_tokens_check as check  # noqa: E402


def _hash(model: str) -> str:
    return hashlib.sha256(model.encode("utf-8")).hexdigest()


def test_real_manifest_has_zero_violations():
    """Регрессионный свидетель фикса #1062 — ловит будущий откат значения."""
    violations = check.check_max_tokens()
    assert violations == [], (
        "config/provider-usage.json несёт max_output_tokens выше "
        f"подтверждённого потолка: {violations}"
    )


def test_over_ceiling_entry_is_flagged(tmp_path):
    confirmed = tmp_path / "confirmed.json"
    confirmed.write_text(
        json.dumps(
            [
                {
                    "name": "TEST-MODEL",
                    "model_sha256": _hash("test-model"),
                    "confirmed_at": "2026-09-13",
                    "confirmed_max_output_tokens": 65536,
                    "evidence": "fixture",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "chains": {
                    "test-chain": [
                        {
                            "name": "OVER",
                            "base_url": "https://over.test/v1",
                            "model": "test-model",
                            "secret_env": "OVER_KEY",
                            "max_output_tokens": 131072,
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    violations = check.check_max_tokens(manifest_path=manifest, confirmed_path=confirmed)
    assert len(violations) == 1, f"ожидалось ровно одно нарушение, получено: {violations}"
    assert "OVER" in violations[0]
    assert "test-model" in violations[0]
    assert "131072" in violations[0]
    assert "65536" in violations[0]


def test_unconfirmed_model_is_not_flagged(tmp_path):
    """Модель без confirmed_max_output_tokens — не нарушение (не подтверждено,
    не фейл-открыто по умолчанию)."""
    confirmed = tmp_path / "confirmed.json"
    confirmed.write_text(json.dumps([]), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "chains": {
                    "test-chain": [
                        {
                            "name": "UNKNOWN",
                            "base_url": "https://unknown.test/v1",
                            "model": "unknown-model",
                            "secret_env": "UNKNOWN_KEY",
                            "max_output_tokens": 999999999,
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    violations = check.check_max_tokens(manifest_path=manifest, confirmed_path=confirmed)
    assert violations == []
