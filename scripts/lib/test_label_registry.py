#!/usr/bin/env python3
"""Гвардия реестра меток (задача #207): docs/agents/LABELS.md не протухает.

Класс проблемы: сплошной прочёс 2026-09-02 нашёл пять односторонних тормозов
конвейера из шести без объявленного газа (#205, #204, #196) — ни один пропуск
не был виден, пока логи не прочли целиком. Реестр меток фиксирует «у тормоза
X есть газ Y» текстом, а без гвардии сам реестр отстанет от кода так же, как
отстал факт про газ.

Три правила, три мутации, каждая красит CI отдельно:
  1. Метка встречается в коде (scripts/lib/collect_labels.py), но строки в
     реестре нет — новый тормоз добавлен молча, без объявленного газа.
  2. Строка в реестре есть, но колонка «Газ» пустая — тормоз назван, газ нет.
  3. Строка в реестре есть, а метки в коде больше нет — реестр обрастает
     мёртвыми записями (не отражает того, что реально исполняется).

Честный потолок этой гвардии назван в самом docs/agents/LABELS.md: она видит
только тормоза, выраженные меткой. Тормоз, зашитый условием в коде
(pulse_guard.py), сканированием не ловится — это дисциплина AGENTS.md и
вопрос в PR-шаблоне, не эта проверка.

Запуск: python -m pytest scripts/lib/test_label_registry.py -q
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS_MD = REPO_ROOT / "docs" / "agents" / "LABELS.md"

_spec = importlib.util.spec_from_file_location(
    "collect_labels", Path(__file__).resolve().parent / "collect_labels.py"
)
collect_labels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collect_labels)

TABLE_HEADER = "| Метка |"
COLUMN_COUNT = 6  # Метка | Кто ставит | Когда | Что блокирует | Газ | Порог


def parse_registry() -> list[dict[str, str]]:
    """Строки таблицы реестра. Парсинг — плоский сплит по `|`: в репозитории
    нет экранированных пайпов внутри ячеек (markdown-ссылки `[t](u)` их не
    содержат), усложнять до полного markdown-парсера незачем."""
    text = LABELS_MD.read_text(encoding="utf-8")
    rows: list[dict[str, str]] = []
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
            break  # таблица кончилась
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert len(cells) == COLUMN_COUNT, (
            f"строка реестра с {len(cells)} колонками вместо {COLUMN_COUNT}: {line!r}"
        )
        label = cells[0].strip("`")
        rows.append({
            "label": label,
            "who_sets": cells[1],
            "when": cells[2],
            "what_blocks": cells[3],
            "gas": cells[4],
            "threshold": cells[5],
        })
    return rows


def test_labels_md_has_registry_table():
    assert LABELS_MD.exists(), f"{LABELS_MD} не найден — реестр меток отсутствует (задача #207)"
    rows = parse_registry()
    assert rows, f"{LABELS_MD}: таблица реестра пуста или не распозналась"


def test_every_label_in_code_is_registered():
    """Мутация (а): литерал метки в коде без строки в реестре — красный."""
    code_labels = collect_labels.collect_labels()
    registry_labels = {row["label"] for row in parse_registry()}
    missing = sorted(code_labels - registry_labels)
    assert not missing, (
        f"метки используются в коде, но нет строки в {LABELS_MD}: {missing} — "
        "у каждого тормоза-метки обязан быть объявлен газ (AGENTS.md, задача #207)"
    )


def test_no_dead_registry_rows():
    """Мутация (в): строка реестра для метки, которой в коде больше нет — красный."""
    code_labels = collect_labels.collect_labels()
    registry_labels = {row["label"] for row in parse_registry()}
    dead = sorted(registry_labels - code_labels)
    assert not dead, (
        f"{LABELS_MD}: строки для меток, которых в коде больше нет: {dead} — "
        "удали запись или верни метку в код, реестр не должен обрастать мёртвыми строками"
    )


def test_every_row_has_gas():
    """Мутация (б): пустая колонка «Газ» — красный."""
    empty = [row["label"] for row in parse_registry() if not row["gas"]]
    assert not empty, (
        f"{LABELS_MD}: пустая колонка «Газ» у меток {empty} — ручной газ допустим, "
        "но должен быть объявлен явно («вручную: роль, по сигналу X»), не пропущен"
    )
