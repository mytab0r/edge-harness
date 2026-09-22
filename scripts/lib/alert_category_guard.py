#!/usr/bin/env python3
"""Ни один сигнал владельцу не уходит без категории (#1461).

Класс одной фразой: **точка отправки, забывшая категорию, возвращает ровно то
состояние, ради ухода из которого категория и заводилась** — всё одним
потоком, решение владельца вперемешку с «деплой откатился».

Обязательный именованный аргумент `send_telegram(..., category=...)` ловит это
в рантайме — но только если строка исполнилась. Сигнал уходит из редких веток
(пульс не тикает, предохранитель сработал), и красный прогон случился бы у
владельца в чате, а не в CI. Поэтому проверка статическая, по исходнику.

Разбор — AST, не регулярка. Урок оплачен в этой же сессии (#1438): текстовый
поиск `send_telegram(` не видит многострочный вызов, где `category=` стоит
ниже по строкам, и красит зелёным ровно то, что обязан ловить. AST видит
вызов целиком независимо от переносов.

Газ (AGENTS.md, «тормоз без газа»): нарушение снимается добавлением
`category=` из реестра `scripts/lib/alert_category.py` — сообщение называет
файл, строку и список известных категорий. Реестра исключений здесь НЕТ
намеренно: «отправить без категории» не имеет законного случая, в отличие от
шагов workflow, где долг измерен и назван (api_quota_gate_guard).

Запуск: python scripts/lib/alert_category_guard.py
Тесты:  python -m pytest scripts/lib/test_alert_category_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNED_DIRS = ("scripts",)
SENDER_NAME = "send_telegram"
CATEGORY_KEYWORD = "category"


_REGISTRY_MODULE = "alert_category"


def _category_value_problem(node: ast.AST) -> str | None:
    """Почему значение `category=` не годится, или None — годится.

    Проверять НАЛИЧИЕ аргумента мало, и это выяснилось на живой ошибке автора
    в этом же PR: я написал `category=pulse_guard.alert_category.OWNER_DECISION`
    в модуле, куда имя `pulse_guard` не импортировано. Аргумент был на месте,
    гвардия молчала — упало в рантайме NameError'ом на тестах. Тот же класс,
    что «имя есть, проводки нет», за который репозиторий платил не раз.

    Годятся ровно две формы:
      * `alert_category.<ИМЯ>` — константа реестра (цепочка атрибутов длиннее
        одной ступени запрещена: `x.alert_category.Y` и есть та ошибка);
      * строковый литерал, который реестр знает.
    Всё остальное (переменная, вызов, f-строка) статически не разрешимо —
    честнее назвать это нарушением, чем пропустить молча."""
    if isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name) and base.id == _REGISTRY_MODULE:
            return None
        return (f"значение `category=` ссылается не на реестр {_REGISTRY_MODULE} напрямую — "
                f"статически не проверить, что имя вообще разрешится (живой случай: "
                f"`pulse_guard.alert_category.X` в модуле без импорта pulse_guard)")
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        import importlib.util as _il
        spec = _il.spec_from_file_location(
            _REGISTRY_MODULE, Path(__file__).resolve().parent / "alert_category.py")
        registry = _il.module_from_spec(spec)
        spec.loader.exec_module(registry)
        if node.value in registry.CATEGORIES:
            return None
        return f"категория {node.value!r} не объявлена в {_REGISTRY_MODULE}.CATEGORIES"
    return ("значение `category=` не разрешается статически — ожидается "
            f"{_REGISTRY_MODULE}.<КОНСТАНТА> или строка из реестра")


def _calls_without_category(source: str) -> list[tuple[int, str]]:
    """(строка, причина) для вызовов `send_telegram`, чья категория не годится.

    Определение самого отправителя (`def send_telegram`) вызовом не является и
    в список не попадает: иначе гвардия штрафовала бы файл, который эту
    возможность и предоставляет."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != SENDER_NAME:
            continue
        by_name = {kw.arg: kw.value for kw in node.keywords}
        # `**kwargs` даёт arg=None: считать такой вызов закрытым нельзя —
        # что там внутри, статически не видно, и это честнее назвать
        # нарушением, чем пропустить молча.
        if CATEGORY_KEYWORD not in by_name:
            offenders.append((node.lineno, f"нет `{CATEGORY_KEYWORD}=`"))
            continue
        problem = _category_value_problem(by_name[CATEGORY_KEYWORD])
        if problem:
            offenders.append((node.lineno, problem))
    return offenders


def check(repo_root: Path = REPO_ROOT) -> list[str]:
    problems: list[str] = []
    for directory in SCANNED_DIRS:
        for path in sorted((repo_root / directory).rglob("*.py")):
            if path.name.startswith("test_"):
                continue  # тесты зовут отправитель со стендами, а не шлют владельцу
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if SENDER_NAME not in source:
                continue
            for lineno, reason in _calls_without_category(source):
                rel = path.relative_to(repo_root)
                problems.append(
                    f"{rel}:{lineno}: {SENDER_NAME}(...) — {reason}. Сигнал уйдёт "
                    f"владельцу в общий поток или упадёт в рантайме. Категория берётся "
                    f"из scripts/lib/alert_category.py (#1461)")
    return problems


def main() -> int:
    problems = check()
    if problems:
        for problem in problems:
            print(f"::error::alert-category: {problem}")
        return 1
    print("alert-category: каждый вызов отправителя несёт категорию")
    return 0


if __name__ == "__main__":
    sys.exit(main())
