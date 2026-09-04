#!/usr/bin/env python3
"""Тесты извлечения номера задачи из текста (scripts/lib/task_ref.py, #187, #195).

Класс #187: `f"#{n}" in text` / `line.split("#")` матчат подстрокой — `#18`
совпадает с `#180`, `#181`, `#5180`. Живой прогон orchestra 33570081734:
контракт спутал PR #185 (задача #182) с задачей #18.

Класс #195 (второй экземпляр #187): даже с границами числа `references_task`
по всему телу чужого PR путает «упомянута в прозе» с «объявлена как задача
PR» — contract_check.py сравнивал декларацию своего PR (узко, строка,
начинающаяся с `#N`) с любым упоминанием в теле чужого (широко). Симметрия
восстановлена через task_ref.declared_tasks/declares_task.

Кейсы кормятся прод-формой тела PR, которая реально встречается в
репозитории (см. test_real_pr_body_form, test_real_pr_209_*).

Запуск: python -m pytest scripts/lib/test_task_ref.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("task_ref.py")
spec = importlib.util.spec_from_file_location("task_ref", SCRIPT)
task_ref = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_ref)  # type: ignore[union-attr]


def test_no_false_positive_on_longer_number_suffix():
    # #18 не должен матчить #180, #181, #184, #185 (сам баг 33570081734).
    assert task_ref.references_task("Открыт PR #180", 18) is False
    assert task_ref.references_task("Открыт PR #181", 18) is False
    assert task_ref.references_task("Открыт PR #182", 18) is False
    assert task_ref.references_task("Открыт PR #184", 18) is False
    assert task_ref.references_task("Открыт PR #185", 18) is False


def test_no_false_positive_on_longer_number_prefix():
    # #18 не должен матчить #5180 (граница слева — не только справа).
    assert task_ref.references_task("см. задачу #5180", 18) is False
    assert task_ref.references_task("#518", 18) is False


def test_true_positive_various_positions():
    assert task_ref.references_task("#18", 18) is True
    assert task_ref.references_task("Закрывает #18 в этом PR", 18) is True
    assert task_ref.references_task("см. #18, #182 и #185", 18) is True
    assert task_ref.references_task("(#18)", 18) is True
    assert task_ref.references_task("#18,#182", 18) is True


def test_extract_task_refs_multiple_with_boundaries():
    text = "Сначала #18, потом #180 и снова #18 рядом с #5180."
    assert task_ref.extract_task_refs(text) == [18, 180, 18, 5180]


def test_extract_task_refs_empty_text():
    assert task_ref.extract_task_refs("") == []
    assert task_ref.extract_task_refs(None) == []


def test_real_pr_body_form():
    # Прод-форма тела PR (см. контракт PR из AGENTS/скриптов): первая строка
    # ровно "#<N>", дальше пояснительный текст с другими номерами.
    body = (
        "#18\n\n"
        "Второй гейт ревью (AI). Связано с #180, #181, #185 — но задача одна: #18.\n"
    )
    assert task_ref.references_task(body, 18) is True
    assert task_ref.extract_task_refs(body) == [18, 180, 181, 185, 18]


def test_real_pr_body_no_relation_regression():
    # Регресс из 33570081734: PR #185 про задачу #182, контракт для #18 не
    # должен видеть его как конкурента.
    body = "#182\n\nАрхив сессий раннера падает 403 (см. #174).\n"
    assert task_ref.references_task(body, 18) is False
    assert task_ref.references_task(body, 182) is True


def test_declared_tasks_only_from_leading_hash_line():
    # #195: декларация — номер на строке, начинающейся с `#N`, не любое
    # упоминание в прозе (борда GitHub Projects упоминает вехи по номеру).
    body = (
        "#182\n\n"
        "## Что сделано\n"
        "Вехи привязаны к #18 и #20. Строки Зависимости добавлены в тела "
        "#147 и #134.\n"
    )
    assert task_ref.declared_tasks(body) == [182]
    assert task_ref.declares_task(body, 182) is True
    assert task_ref.declares_task(body, 18) is False
    assert task_ref.declares_task(body, 134) is False


def test_declared_tasks_empty_text():
    assert task_ref.declared_tasks("") == []
    assert task_ref.declared_tasks(None) == []


# #195: асимметрия contract_check.py — своя декларация PR уже была узкой
# (строка, начинающаяся с #N), а конфликт с чужими PR гонялся по всему телу
# (task_ref.references_task) вместо декларации (task_ref.declares_task).
# Кейсы ниже — реальные тела PR из живого прогона (не пересказ).

_PR_209_BODY = (
    "#207\n\n"
    "## Что сделано\n"
    "- Правило «Тормоз без газа не принимается» в AGENTS.md (раздел «Правила, "
    "оплаченные чужими ошибками»), с замером цены на пяти реальных тормозах "
    "(#205, #204, #196).\n"
    "- Реестр docs/agents/LABELS.md по всем 13 меткам.\n"
)

_PR_206_BODY = (
    "#205\n\n"
    "## Класс проблемы\n\n"
    "Предохранитель конвейера был двухсостоятельным без сброса: "
    "`conveyor_gate` останавливает диспатч `worker.yml`.\n"
)


def test_real_pr_209_does_not_declare_205_mentioned_in_prose():
    # #209 объявляет #207 первой строкой, #205 упомянут в прозе (замер цены
    # на пяти тормозах) — это не декларация задачи #205.
    assert task_ref.declared_tasks(_PR_209_BODY) == [207]
    assert task_ref.declares_task(_PR_209_BODY, 205) is False
    # references_task (широкая, по всему тексту) видит упоминание — так и
    # должно быть, это не баг references_task, а неверное место её вызова.
    assert task_ref.references_task(_PR_209_BODY, 205) is True


def test_real_pr_206_declares_205():
    assert task_ref.declared_tasks(_PR_206_BODY) == [205]
    assert task_ref.declares_task(_PR_206_BODY, 205) is True


def test_pr_209_and_pr_206_do_not_conflict_on_declared_task():
    # Живой ложный конфликт из #195: contract_check для #206 (декларация
    # #205) находил #209 как «уже открытый PR на задачу #205», хотя #209
    # объявляет #207. По декларации конфликта нет.
    declared_206 = task_ref.declared_tasks(_PR_206_BODY)[0]
    assert task_ref.declares_task(_PR_209_BODY, declared_206) is False


def test_two_prs_declaring_same_task_still_conflict():
    # Обратная проверка: если оба PR ОБЪЯВЛЯЮТ одну и ту же задачу первой
    # строкой — это настоящая гонка веток, конфликт обязан остаться.
    pr_a = "#42\n\nПервая реализация.\n"
    pr_b = "#42\n\nВторая попытка, другая ветка.\n"
    declared = task_ref.declared_tasks(pr_a)[0]
    assert task_ref.declares_task(pr_b, declared) is True
