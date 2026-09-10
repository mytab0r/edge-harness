#!/usr/bin/env python3
"""Гвардия реестра использования LLM-провайдеров (задача #823,
openspec/changes/llm-provider-usage-manifest): docs/agents/LLM-PROVIDER-USAGE.md
не протухает относительно config/provider-usage.json — тот же класс проблемы
и та же форма гвардии, что уже закрыты для меток задачей #207
(scripts/lib/test_label_registry.py).

Три мутации, каждая красит CI отдельно:
  1. Сборщик печатает строку (потребитель из CONSUMERS + строка morda), а в
     реестре её нет — новое назначение задокументировано не полностью.
  2. Строка реестра есть, а сборщик её больше не печатает (потребитель ушёл
     из CONSUMERS или его строка сформирована иначе) — реестр обрастает
     мёртвой записью.
  3. Строка реестра ЕСТЬ, но её текст разошёлся с тем, что реально печатает
     сборщик по факту манифеста (например кто-то поправил число провайдеров
     руками, не перегенерировав таблицу) — реестр лжёт о содержимом
     config/provider-usage.json.

Честный потолок — тот же, что у LABELS.md (см. сам файл реестра): форма
записи, не то, что цепочка реально переживёт квоту в проде.

Запуск: python -m pytest scripts/lib/test_provider_usage_registry.py -q
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_MD = REPO_ROOT / "docs" / "agents" / "LLM-PROVIDER-USAGE.md"

_spec = importlib.util.spec_from_file_location(
    "collect_provider_usage", Path(__file__).resolve().parent / "collect_provider_usage.py"
)
collect_provider_usage = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect_provider_usage)

TABLE_HEADER = "| Потребитель |"


def parse_registry_rows() -> list[str]:
    """Строки таблицы РЕЕСТРА как их печатает сам сборщик (без хвостового
    перевода строки) — плоский построчный разбор, тот же приём, что
    test_label_registry.parse_registry (нет экранированных пайпов внутри ячеек
    в этом репозитории)."""
    text = REGISTRY_MD.read_text(encoding="utf-8")
    rows: list[str] = []
    in_table = False
    for line in text.splitlines():
        if line.startswith(TABLE_HEADER):
            in_table = True
            continue
        if not in_table:
            continue
        if line.startswith("|---"):
            continue
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def generated_rows() -> list[str]:
    rows = [collect_provider_usage.format_row(entry)
            for entry in collect_provider_usage.collect_provider_usage()]
    rows.append(
        f"| `morda` | (вне манифеста) | — | {collect_provider_usage.MORDA_VISIBILITY_NOTE} |"
    )
    return rows


def test_registry_has_table():
    assert REGISTRY_MD.exists(), f"{REGISTRY_MD} не найден — реестр использования провайдеров отсутствует (#823)"
    rows = parse_registry_rows()
    assert rows, f"{REGISTRY_MD}: таблица реестра пуста или не распозналась"


def test_registry_matches_generator_exactly():
    """Мутации 1+2+3 разом: реестр обязан быть побайтово тем, что печатает
    сборщик — расхождение в любую сторону (пропавшая строка, лишняя,
    исправленная руками) красит этот тест."""
    expected = generated_rows()
    actual = parse_registry_rows()
    assert actual == expected, (
        "docs/agents/LLM-PROVIDER-USAGE.md разошёлся с "
        "scripts/lib/collect_provider_usage.py — перегенерируй таблицу:\n"
        f"ожидалось:\n{chr(10).join(expected)}\n\nв файле:\n{chr(10).join(actual)}"
    )


def test_every_consumer_has_a_row(tmp_path):
    """Мутация 1, изолированно от реального манифеста: подсунуть манифест без
    записи потребителя — сборщик обязан честно напечатать «не назначена», а не
    молча пропустить строку (иначе гвардия реестра её тоже не заметит)."""
    manifest_path = tmp_path / "provider-usage.json"
    manifest_path.write_text('{"chains": {"c": [{"name": "X"}]}, "usage": {"worker": "c"}}',
                              encoding="utf-8")
    rows = collect_provider_usage.collect_provider_usage(manifest_path)
    by_consumer = {row["consumer"]: row for row in rows}
    assert set(by_consumer) == set(collect_provider_usage.CONSUMERS)
    assert by_consumer["worker"]["state"] == "ok"
    assert by_consumer["ai-review"]["state"] == "missing"
    assert by_consumer["hands"]["state"] == "missing"
