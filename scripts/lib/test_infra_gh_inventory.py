#!/usr/bin/env python3
"""Гвардия актуальности инвентаря docs/agents/INFRA-GH.md (находка ревью PR #326/#333).

Класс проблемы: таблица «Инвентарь workflow» — прод-форма факта («прочитаны
все, <дата>»), а не décor. Живой пример протухания, обнаруженный ревью:
`.github/workflows/worker-ci.yml` был удалён cleanup-коммитом и восстановлен
задачей #72 (#331) уже ПОСЛЕ прошлой сверки таблицы — строка не добавилась,
заголовок продолжал утверждать «прочитаны все». Без гвардии такой срез
протухает молча (тот же класс, что #326 уже закрыл для реестра меток,
scripts/lib/test_label_registry.py): новый или восстановленный workflow не
попадает в таблицу, и никто не обязан заметить пропуск, пока не читает файл
целиком построчно.

Единственное правило: каждое имя `.yml` из `.github/workflows/` обязано
встречаться в таблице инвентаря как отдельная ячейка `| `<file>` |`
(markdown-код в первой колонке), не просто где-то в тексте — упоминание в
прозе (как у samого worker-ci.yml в пункте про грабли) не считается строкой
инвентаря.

Честный потолок: гвардия проверяет только ПРИСУТСТВИЕ имени файла в таблице,
не корректность остальных колонок (триггер/секреты/vars) — это по-прежнему
дисциплина автора и ревьюера, как и у test_label_registry.py.

Запуск: python -m pytest scripts/lib/test_infra_gh_inventory.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
INFRA_GH_MD = REPO_ROOT / "docs" / "agents" / "INFRA-GH.md"

TABLE_HEADER = "| Файл | Триггер |"


def workflow_files() -> list[str]:
    return sorted(p.name for p in WORKFLOWS_DIR.glob("*.yml"))


def inventory_table_rows(text: str) -> list[str]:
    """Строки markdown-таблицы «Инвентарь workflow» между заголовком и первой
    строкой, не начинающейся с `|` (та же форма разбора, что
    test_label_registry.parse_registry — плоский сплит, без экранированных
    пайпов в этом файле)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(TABLE_HEADER)), None)
    assert start is not None, f"в {INFRA_GH_MD} не найден заголовок таблицы {TABLE_HEADER!r}"
    rows = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


def files_named_in_table(rows: list[str]) -> set[str]:
    """Имена `.yml`, названные первой колонкой каждой строки таблицы (после
    строки-разделителя `|---|`) как markdown-код `` `file.yml` ``."""
    names: set[str] = set()
    for row in rows:
        if row.startswith("|---"):
            continue
        first_cell = row.strip("|").split("|", 1)[0].strip()
        match = re.fullmatch(r"`([\w.-]+\.yml)`", first_cell)
        if match:
            names.add(match.group(1))
    return names


def test_every_workflow_file_has_an_inventory_row():
    text = INFRA_GH_MD.read_text(encoding="utf-8")
    rows = inventory_table_rows(text)
    named = files_named_in_table(rows)
    on_disk = set(workflow_files())
    missing = sorted(on_disk - named)
    assert not missing, (
        f"{INFRA_GH_MD} не называет {missing} в таблице «Инвентарь workflow» — "
        "новый/восстановленный workflow добавлен молча, таблица протухла (см. докстринг)"
    )


def test_inventory_has_no_stale_row_for_a_deleted_workflow():
    text = INFRA_GH_MD.read_text(encoding="utf-8")
    rows = inventory_table_rows(text)
    named = files_named_in_table(rows)
    on_disk = set(workflow_files())
    stale = sorted(named - on_disk)
    assert not stale, (
        f"{INFRA_GH_MD} называет {stale} в инвентаре, а файла(ов) в .github/workflows/ "
        "больше нет — мёртвая строка, не отражает того, что реально исполняется"
    )
