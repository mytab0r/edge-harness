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

Вторая половина контракта слота — `run-name`/`concurrency.group` (находка
ai-review PR #831, «доделай в этом PR»): оркестратор читает номер слота
готового прогона ТОЛЬКО из `run-name` (`_SLOT_PATTERN` в scheduler.py) — REST
не отдаёт workflow_dispatch inputs ни в одном поле готового run. Гвардия
выше защищает только блок `inputs`; переформулировка `run-name:` будущим PR
прошла бы её молча, а `_run_slot` тогда тихо возвращает `None` для всех
прогонов — диспатчи копятся очередью в «свободном» слоте 2 (GitHub
сериализует группу, двойной работы не будет), но воркер отчитывается
неверно. Тесты ниже требуют, чтобы `run-name` содержал форму `(slot ...)`,
которую парсит `_SLOT_PATTERN`, и чтобы `concurrency.group` зависел от
`inputs.slot`.

Запуск: python -m pytest scripts/lib/test_worker_slot_input_guard.py -q
"""

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_YML = REPO_ROOT / ".github" / "workflows" / "worker.yml"
SCHEDULER_PY = REPO_ROOT / "scripts" / "orchestra" / "scheduler.py"


def _worker_doc() -> dict:
    return yaml.safe_load(WORKER_YML.read_text(encoding="utf-8"))


def _slot_input() -> dict:
    doc = _worker_doc()
    # YAML грузит ключ `on:` как булев True (yaml 1.1) — тот же класс,
    # что уже учитывают другие гвардии workflow в этом каталоге.
    on_block = doc.get("on") or doc.get(True)
    return on_block["workflow_dispatch"]["inputs"]["slot"]


def _slot_pattern() -> re.Pattern:
    text = SCHEDULER_PY.read_text(encoding="utf-8")
    match = re.search(r"_SLOT_PATTERN\s*=\s*re\.compile\((.+?)\)\n", text)
    assert match, "scheduler.py обязан объявлять _SLOT_PATTERN — контракт run-name сверить не с чем"
    pattern_source = ast.literal_eval(match.group(1))
    return re.compile(pattern_source)


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


def test_run_name_carries_slot_in_a_form_scheduler_can_parse():
    doc = _worker_doc()
    run_name = doc.get("run-name", "")
    pattern = _slot_pattern()
    # Подставляем оба валидных значения слота вместо GitHub-выражения
    # `${{ inputs.slot || '1' }}` — сам YAML этого не вычисляет, но форма
    # `(slot N)` обязана остаться в буквальном тексте вокруг выражения.
    for candidate_slot in ("1", "2"):
        rendered = re.sub(r"\$\{\{[^}]*\}\}", candidate_slot, run_name, count=1)
        assert pattern.search(rendered), (
            f"run-name {run_name!r} не даёт форму, которую парсит _SLOT_PATTERN "
            f"scheduler.py, при подстановке slot={candidate_slot} — оркестратор "
            "прочитает прогон как слот None (находка ai-review PR #831)"
        )


def test_concurrency_group_depends_on_slot_input():
    doc = _worker_doc()
    group = doc.get("concurrency", {}).get("group", "")
    assert "inputs.slot" in group, (
        f"concurrency.group {group!r} обязан зависеть от `inputs.slot` — иначе "
        "два прогона в разных слотах сериализуются в одну группу, и второй слот "
        "перестаёт быть параллельным (находка ai-review PR #831)"
    )
