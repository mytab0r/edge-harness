#!/usr/bin/env python3
"""Гвардия входа `slot` в worker.yml (#827, находка ai-review PR #831).

Класс: workflow_dispatch-вход, объявленный `type: string` без ограничения
значений, принимает ЛЮБУЮ строку — включая `slot=3`, которая создаёт новую
concurrency-группу `worker-3`, не учитываемую scheduler.py (`WORKER_MAX_
CONCURRENCY=2`, диапазон слотов 1..2 в `free_worker_slot`). Это превышает
предполагаемый лимит параллельности и маскирует загрузку конвейера, при этом
ни один существующий тест не читал реальный YAML workflow — регресс на
`type: string` прошёл бы тесты сценариев dispatch (они не смотрят исходник
worker.yml) зелёными.

Требование: `slot` объявлен `type: choice` с опциями РОВНО `["1", "2"]` —
GitHub Actions отклоняет workflow_dispatch с недопустимым значением choice
как на ручном запуске, так и на API/CLI dispatch (`gh workflow run -f
inputs[slot]=`), поэтому невозможного значения не бывает уже на входе, не
постфактум.

Запуск: python -m pytest scripts/lib/test_worker_slot_input_guard.py -q
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_YML = REPO_ROOT / ".github" / "workflows" / "worker.yml"


def _slot_input() -> dict:
    doc = yaml.safe_load(WORKER_YML.read_text(encoding="utf-8"))
    # YAML грузит ключ `on:` как булев True (yaml 1.1) — тот же класс,
    # что уже учитывают другие гвардии workflow в этом каталоге.
    on_block = doc.get("on") or doc.get(True)
    return on_block["workflow_dispatch"]["inputs"]["slot"]


def test_slot_input_is_choice_type_not_free_string():
    slot = _slot_input()
    assert slot.get("type") == "choice", (
        "вход `slot` обязан быть `type: choice` — свободная `string` "
        "принимает недопустимые слоты (например 3), находка ai-review #831"
    )


def test_slot_input_options_are_exactly_the_two_valid_slots():
    slot = _slot_input()
    assert slot.get("options") == ["1", "2"], (
        "опции `slot` обязаны быть ровно ['1', '2'] — WORKER_MAX_CONCURRENCY=2 "
        "в scheduler.py (диапазон слотов 1..2), третьего слота не существует"
    )


def test_slot_input_default_is_a_valid_slot():
    slot = _slot_input()
    assert slot.get("default") in slot.get("options", []), (
        "default `slot` обязан быть одной из объявленных опций"
    )
