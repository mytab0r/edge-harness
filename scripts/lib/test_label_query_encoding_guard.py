#!/usr/bin/env python3
"""Гвардия класса «значение метки в query GitHub API не URL-кодировано» (#938).

Инцидент: `scripts/orchestra/waiting_owner_guard.py::open_waiting_owner_issues`
строил `f"repos/{repo}/issues?state=open&labels={WAITING_OWNER_LABEL}&per_page=100"`,
где `WAITING_OWNER_LABEL = "waiting:owner"`. Двоеточие не кодировалось —
GitHub интерпретирует его как часть URL-синтаксиса, не байт значения фильтра,
и молча отвечает пустым списком, а не ошибкой. Доказано напрямую API:

    $ gh api "repos/mytab0r/edge-harness/issues?state=open&labels=waiting:owner"
    []
    $ gh api "repos/mytab0r/edge-harness/issues?state=open&labels=waiting%3Aowner&per_page=100"
    [782, 133]

Следствие: детектор ответа владельца (`waiting_owner_check`) не видел ни одной
задачи с меткой `waiting:owner` — ответ владельца в #782 («РЕШЕНИЕ: 1»,
2026-09-09) двое суток не был обработан.

Класс, не точечный случай: любое место, подставляющее ПЕРЕМЕННОЕ значение
метки в query-параметр (`labels=`) f-строкой напрямую, ломается тем же
способом, как только значение получит двоеточие — а живые метки этого
репозитория (`review:ok`, `ai:failed`, `area:*`, …) двоеточие уже несут.
Одно место правды на кодирование — `review_labels.label_query_value`
(рядом с `review_labels.list_pages`, тем же местом правды на пагинацию,
#308) — эта гвардия по исходнику требует, чтобы КАЖДАЯ подстановка после
`labels=` шла именно через неё, а не проверяет отдельные файлы точечно.

Признак (AST, не текстовый regex — по образцу
`test_console_utf8_guard.py::_file_io_calls_missing_encoding`): в любом
f-string (`ast.JoinedStr`, включая неявную конкатенацию соседних строковых
литералов — Python склеивает их в один узел на этапе разбора, поэтому перенос
`f"...labels="` + `f"{label_query_value(...)}"` на две строки исходника не
уходит от проверки) константный кусок, заканчивающийся на `&labels=` или
`?labels=` (признак «это позиция query-параметра», не любое упоминание слова
«labels» — находка живой прогонки: `pool_issue.py` несёт `f"...: labels="` в
тексте ОШИБКИ, не в URL, и не должен считаться нарушением), обязан быть
немедленно продолжен `FormattedValue`, чьё выражение — вызов
`review_labels.label_query_value(...)` (или `label_query_value(...)`, если
модуль импортирован без префикса). Голый литерал `labels=task` (без `{}`
вовсе) — не мишень: тут нечего кодировать, значение не переменное.

Запуск: python -m pytest scripts/lib/test_label_query_encoding_guard.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _production_scripts() -> list[Path]:
    """Все .py в scripts/, кроме тестов и самого модуля-хелпера — тесты
    гоняют собственные фикстуры URL (моки в test_*.py), это не то же самое,
    что запрос в проде; review_labels.py — место, где живёт сама функция
    кодирования, ей нечего требовать от себя."""
    return [
        path for path in (REPO_ROOT / "scripts").rglob("*.py")
        if not path.name.startswith("test_") and path.name != "review_labels.py"
    ]


def _is_label_query_value_call(expr: ast.expr) -> bool:
    """True — выражение внутри `{...}` это вызов `label_query_value(...)`,
    голый или через атрибут (`review_labels.label_query_value(...)`)."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute):
        return func.attr == "label_query_value"
    if isinstance(func, ast.Name):
        return func.id == "label_query_value"
    return False


def _unencoded_labels_offenders(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        values = node.values
        for i, part in enumerate(values):
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str)):
                continue
            if not (part.value.endswith("&labels=") or part.value.endswith("?labels=")):
                continue
            # 'labels=' — последний кусок f-строки целиком (константа без
            # подстановки следом, например голый литерал 'labels=task') —
            # кодировать нечего.
            if i + 1 >= len(values):
                continue
            nxt = values[i + 1]
            if not isinstance(nxt, ast.FormattedValue):
                continue
            if not _is_label_query_value_call(nxt.value):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{part.lineno}")
    return offenders


def test_every_labels_query_substitution_is_url_encoded():
    offenders: list[str] = []
    for path in _production_scripts():
        offenders.extend(_unencoded_labels_offenders(path))
    assert offenders == [], (
        "Подстановка значения метки в query 'labels=' без "
        "review_labels.label_query_value(...) (класс #938 — двоеточие в "
        "имени метки типа 'waiting:owner'/'review:ok'/'ai:failed' ломает "
        "серверный фильтр GitHub молча, пустым списком, не ошибкой): "
        f"{offenders}"
    )


# ── Поведенческий тест на прод-форме значения ────────────────────────────────


def test_label_query_value_encodes_colon_like_prod_waiting_owner_label():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "review_labels_encoding_check", REPO_ROOT / "scripts" / "lib" / "review_labels.py")
    review_labels = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review_labels)  # type: ignore[union-attr]

    # Прод-форма — буквальное значение WAITING_OWNER_LABEL из
    # waiting_owner_guard.py, не пересказ (AGENTS.md, «тест кормит прод-форму
    # данных»).
    assert review_labels.label_query_value("waiting:owner") == "waiting%3Aowner"
    # Живые метки этого репозитория с тем же символом класса — та же
    # кодировка, не частный случай одной метки.
    assert review_labels.label_query_value("review:large-ok") == "review%3Alarge-ok"
    assert review_labels.label_query_value("ai:changes-requested") == "ai%3Achanges-requested"


# Мутация, которой доказана гвардия (#938): временно верни в
# waiting_owner_guard.py::open_waiting_owner_issues старую форму
# `f"...&labels={WAITING_OWNER_LABEL}&per_page=100"` (без
# review_labels.label_query_value) — test_every_labels_query_substitution_is_url_encoded
# краснеет, называя это же место offender'ом. Верни фикс — тест снова зелёный.
