#!/usr/bin/env python3
"""Тесты извлечения номера задачи из текста (scripts/lib/task_ref.py, #187).

Класс: `f"#{n}" in text` / `line.split("#")` матчат подстрокой — `#18` совпадает
с `#180`, `#181`, `#5180`. Живой прогон orchestra 33570081734: контракт спутал
PR #185 (задача #182) с задачей #18. Кейсы ниже кормятся прод-формой тела PR,
которая реально встречается в репозитории (см. test_real_pr_body_form).

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
