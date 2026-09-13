#!/usr/bin/env python3
"""Носитель третьего состояния для наблюдателей репозитория (issue #1096).

## Класс дефекта

У большинства проверок здоровья репозитория («инвариант нарушен» /
«инвариант чист») на деле ДВА состояния вместо трёх — не хватает «не смог
посмотреть». Когда `except RuntimeError: return []` или `if anchor is None:
continue` молча превращают «транспорт отказал»/«данных нет» в тот же самый
пустой список, что и настоящее «нарушений нет», — читатель отчёта (человек
или газ-механизм вроде эскалации) не может отличить «система здорова» от
«наблюдатель ослеп». Живой замер (issue #1096): из 16 строк
`repo_invariants.build_report` 12 остаются 💚, даже когда каждый сетевой
вызов внутри инвариантов, читающих сеть, кидает RuntimeError.

Это тот же класс, что уже нашли и закрыли ПОРОЗНЬ — `quota_watch.
MeasurementScan.api_ok` (см. `scripts/measure/quota_watch.py`, докстринг
«Два возраста одного скана») и `status="unchecked"` инварианта 15
(`repo_invariants.py::check_merge_reaction_gaps` +
`build_report`, найдено ревью PR #956) — оба места сами по себе НЕ заводят
второй копии одной и той же идеи, а этот модуль обобщает её в один тип,
переиспользуемый следующими миграциями (issue #1096, шаг 2).

## Три исхода, не два

- `STATUS_OK` — проверка состоялась, нарушений не найдено.
- `STATUS_VIOLATION` — проверка состоялась, нарушения есть (`violations`
  непустой).
- `STATUS_UNKNOWN` — проверка НЕ СОСТОЯЛАСЬ (транспорт недоступен, данные
  неожиданной формы, время/бюджет исчерпаны на середине) — `reason` ОБЯЗАН
  назвать, что именно и почему не удалось (AGENTS.md, «Алерт не гадает»):
  не пустая строка, не подстановка «наверное сеть».

`CheckResult` — NamedTuple, не dataclass: неизменяемый, сравнимый по
значению (тесты сравнивают результат целиком), не требует `@dataclass`
поверх stdlib. Конструкторы `ok()`/`violation()`/`unknown()` — единственный
способ создать валидный `CheckResult`: оба неверных входа (пустой список
нарушений в `violation()`, пустая причина в `unknown()`) кидают `ValueError`
на месте создания — раньше самого возврата из `check_*`, где ошибку было бы
труднее найти.

## Гвардия регресса — почему поведенческая, не только структурная

AGENTS.md («Поведенческий тест находит то, чего структурный не видит»,
#891/#893): проверка по одному только исходнику (AST) ловит форму кода
(«есть `except`, нет вызова `unknown(`»), но не ловит, скажем, `unknown(...)`
внутри `except`, тело которого реально ничего не возвращает вызывающему
(баг в другом месте функции). Этот модуль даёт ОБЕ гвардии:

- `scan_silent_except(source, function_names)` — AST-скан по исходнику,
  дешёвый (без импорта модуля, без сети), находит `except (RuntimeError|
  Exception ...): <тело без вызова unknown(...)>` внутри перечисленных
  функций — узкая, конкретная форма из issue #1096, не общий линт (общий
  линт по ЛЮБОМУ `except: return []` покрасил бы CI сразу для всех ещё НЕ
  мигрированных инвариантов — это работа шага 2, отдельная задача).
- Поведенческий тест — обязанность модуля, МИГРИРУЮЩЕГО конкретную функцию
  (`test_repo_invariants.py`/`test_pulse_guard.py`): падающий транспорт
  (`gh` кидает RuntimeError) реально вызывает мигрированную функцию и
  проверяет `result.status == STATUS_UNKNOWN` — это модуль сам за
  потребителя не подделывает (см. `scripts/lib/test_check_result.py` для
  примера на синтетическом коде).

Запуск тестов: python -m pytest scripts/lib/test_check_result.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import ast
from typing import NamedTuple

STATUS_OK = "ok"
STATUS_VIOLATION = "violation"
STATUS_UNKNOWN = "unknown"

# Значки отчёта — одно место правды на всё, что рендерит CheckResult построчно
# (issue #1096, критерий 2): ❓ обязан быть визуально ТРЕВОЖНЫМ, не 💚 и не
# нейтральным ⏭️ (тот уже занят «сознательно не проверяем на этом пульсе»,
# см. build_report инварианты 6/9 — другая семантика: там решение «не
# запускать», здесь — «попытались и не смогли»).
STATUS_EMOJI = {
    STATUS_OK: "💚",
    STATUS_VIOLATION: "🚨",
    STATUS_UNKNOWN: "❓",
}


class CheckResult(NamedTuple):
    """Трёхзначный исход одной проверки (issue #1096). Создавайте только
    через ok()/violation()/unknown() — конструкторы проверяют инвариант
    формы на месте, сам NamedTuple их не форсирует (иначе `CheckResult(
    STATUS_UNKNOWN, [], None)` собрался бы молча и снова слил ❓ с 💚 по
    смыслу — пустая причина у unknown ничем не лучше отсутствия причины)."""

    status: str
    violations: list
    reason: str | None


def ok() -> CheckResult:
    """Проверка состоялась, нарушений нет."""
    return CheckResult(STATUS_OK, [], None)


def violation(items: list) -> CheckResult:
    """Проверка состоялась, нарушения есть. `items` обязан быть непустым —
    пустой список нарушений это ok(), а не «violation с нулём находок» (та
    же путаница видом наоборот: два разных факта в одной форме)."""
    if not items:
        raise ValueError(
            "check_result.violation() требует непустой список находок — "
            "пустой список означает 'нарушений нет', это check_result.ok()")
    return CheckResult(STATUS_VIOLATION, list(items), None)


def unknown(reason: str) -> CheckResult:
    """Проверка НЕ состоялась. `reason` обязана быть непустой строкой,
    называющей ФАКТ, а не гипотезу (AGENTS.md «Алерт не гадает») — что
    именно не удалось посмотреть и почему (транспорт/форма ответа/бюджет
    времени), а не «что-то пошло не так»."""
    if not reason or not reason.strip():
        raise ValueError(
            "check_result.unknown() требует непустую человекочитаемую причину — "
            "иначе читатель не может отличить 'не смог посмотреть' от гипотезы")
    return CheckResult(STATUS_UNKNOWN, [], reason)


def from_violations(items: list) -> CheckResult:
    """Конструктор по факту количества находок: пустой список -> ok(),
    непустой -> violation(). Для проверок, у которых по построению не
    бывает третьего состояния (чистая функция без сетевого IO) — не
    заставляет звать if/else в каждом таком месте."""
    return violation(items) if items else ok()


def status_emoji(status: str) -> str:
    try:
        return STATUS_EMOJI[status]
    except KeyError:
        raise ValueError(f"check_result: неизвестный статус {status!r} — "
                          "ожидался один из STATUS_OK/STATUS_VIOLATION/STATUS_UNKNOWN") from None


# ══════════════════════════════════════════════════════════════════════════
# Гвардия регресса (issue #1096, критерий готовности 2): AST-скан по
# исходнику, УЗКАЯ форма — только `except (RuntimeError|Exception ...):`
# внутри перечисленных функций, чьё тело не зовёт unknown(...)/
# check_result.unknown(...) ни разу. Не линт «любой except в check_*/fetch_*»
# — тот покрасил бы ещё НЕ мигрированные инварианты (16 функций, из них
# сетевых 7 по замеру issue #1096) до их миграции шагом 2; список функций,
# за которыми эта гвардия следит, называется явно самим вызывающим тестом
# (реестр — данные теста, не константа этого модуля: у каждого модуля,
# использующего check_result, свой реестр мигрированных функций).
# ══════════════════════════════════════════════════════════════════════════

def _handler_calls_unknown(handler: ast.ExceptHandler) -> bool:
    """True, если где-то в теле except-блока встречается вызов вида
    `unknown(...)`/`check_result.unknown(...)`/`<алиас>.unknown(...)` —
    смотрим на ИМЯ вызываемого (последний атрибут), не на модуль импорта:
    вызывающий код мог импортировать этот модуль под любым псевдонимом,
    привязка к буквальному "check_result" была бы второй копией той же
    хрупкости, которую chr(#332)/#891 уже наказали в этом репозитории."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None)
            if name == "unknown":
                return True
    return False


def _is_falsy_empty_return(node: ast.Return) -> bool:
    """True для `return`/`return None`/`return []`/`return ()`/`return
    ok(...)`-подобного вызова — ровно та форма, которую issue #1096
    запрещает без сопровождающего unknown(): обработчик молча превращает
    отказ в «нарушений нет». `return check_result.violation(...)`/`return
    some_variable` — не наш случай, здесь тело обработчика явно решает
    что-то другое, не просто гасит исключение в пустоту.

    `return check_result.ok()` (находка ai-review PR #1110) — тот же класс,
    что голый `return []`: ok() ЖЁСТКО означает «нарушений нет», не «не
    знаю» — регресс, который заменяет литерал на конструктор с тем же
    смыслом, обязан ловиться так же."""
    value = node.value
    if value is None:
        return True  # bare `return`
    if isinstance(value, ast.Constant) and value.value is None:
        return True  # `return None`
    if isinstance(value, (ast.List, ast.Tuple)) and not value.elts:
        return True  # `return []`/`return ()`
    if isinstance(value, ast.Call):
        func = value.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name == "ok":
            return True  # `return ok()`/`return check_result.ok()`
    return False


def _handler_silently_discards(handler: ast.ExceptHandler) -> bool:
    """True, если тело обработчика содержит `return`, отдающий пустое/None
    значение НАПРЯМУЮ (см. `_is_falsy_empty_return`). Обработчик, который
    только накапливает состояние (`continue`, счётчик) и решает дальше по
    ходу функции — НЕ этот класс: он ничего не роняет молча ЗДЕСЬ, а
    откладывает решение (см. `check_worker_false_success_comment`,
    ветка per-кандидатской сверки — общий unknown() строится ПОСЛЕ цикла
    по накопленному счётчику, не внутри каждого отдельного except)."""
    for node in ast.walk(handler):
        if isinstance(node, ast.Return) and _is_falsy_empty_return(node):
            return True
    return False


_SWALLOWING_EXCEPTION_NAMES = {"RuntimeError", "Exception", "BaseException"}


def _exception_names(handler: ast.ExceptHandler) -> set[str]:
    node = handler.type
    if node is None:
        return {"BaseException"}  # bare except
    names: set[str] = set()
    candidates = node.elts if isinstance(node, ast.Tuple) else [node]
    for candidate in candidates:
        if isinstance(candidate, ast.Name):
            names.add(candidate.id)
        elif isinstance(candidate, ast.Attribute):
            names.add(candidate.attr)
    return names


def scan_silent_except(source: str, function_names: set[str]) -> list[dict]:
    """Возвращает список находок `{"function": имя, "line": номер}` — каждая
    находка это `except RuntimeError`-подобный обработчик внутри одной из
    `function_names`, чьё тело НАПРЯМУЮ возвращает пустое/None значение
    (`_is_falsy_empty_return`) и нигде не зовёт `unknown(...)`. Пустой
    список — гвардия чиста (для перечисленных функций регресс невозможен
    статически).

    Обработчик, который просто накапливает состояние (счётчик/список) и
    решает дальше по ходу функции, — НЕ находка (см. `_handler_silently_
    discards`): он не роняет отказ молча ЗДЕСЬ, а откладывает решение.

    Мутация, которую эта гвардия обязана ловить (issue #1096, критерий 2):
    снять вызов `unknown(...)` внутри `except RuntimeError:` мигрированной
    функции, оставив `return []`/`return None`/пустой `return`, — находка
    появляется немедленно, без запуска самого кода."""
    tree = ast.parse(source)
    findings: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in function_names:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.ExceptHandler):
                continue
            if not (_exception_names(inner) & _SWALLOWING_EXCEPTION_NAMES):
                continue
            if not _handler_silently_discards(inner):
                continue
            if _handler_calls_unknown(inner):
                continue
            findings.append({"function": node.name, "line": inner.lineno})
    return findings
