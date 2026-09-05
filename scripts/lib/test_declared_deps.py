#!/usr/bin/env python3
"""Тесты детектора рассинхрона «тело называет блокирующую issue — граф не
знает» (scripts/lib/declared_deps.py, задача #371).

Фрагменты тел — прод-форма: реальный текст открытых issues этого репозитория
на момент внедрения (#258 действительно называет #133 «Единственный
настоящий блокер», #243 упоминает #201 в перечислении «Поглощает часть
#227… и часть #201», не как зависимость — оба случая пойманы живым замером
при разработке #371, не выдуманы для теста).

Запуск: python -m pytest scripts/lib/test_declared_deps.py -q
"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("declared_deps.py")
spec = importlib.util.spec_from_file_location("declared_deps", SCRIPT)
dd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dd)  # type: ignore[union-attr]


# ── declared_candidates: формулировки зависимости ───────────────────────────


def test_declared_candidates_finds_blocker_noun_form():
    # #258, прод-текст: «блокер» существительным, не глагольной формой
    body = (
        "Единственный настоящий блокер — #133 (egress api.github.com из "
        "воркера морды даёт 403)."
    )
    assert dd.declared_candidates(body) == {133}


def test_declared_candidates_finds_zavisit_ot():
    assert dd.declared_candidates("Также зависит от #170 (форма сообщений).") == {170}


def test_declared_candidates_finds_zablokirovan_stem_forms():
    assert dd.declared_candidates("Заблокировано #80 (PoC плагинной системы).") == {80}
    assert dd.declared_candidates("Заблокирована #170 форма сообщений.") == {170}


def test_declared_candidates_ignores_posle_across_closed_paren():
    # #243, прод-текст: «после» относится к «слиянию» ВНУТРИ скобки, #201 —
    # следующий пункт перечисления за закрытой скобкой, не зависимость.
    body = (
        "Поглощает часть #227 (закрытие задач после слияния) и часть #201 "
        "(дедупликация по отпечатку)."
    )
    assert dd.declared_candidates(body) == set()


def test_declared_candidates_case_insensitive_trigger():
    assert dd.declared_candidates("ПОСЛЕ #99 закрытия конвейера.") == {99}


def test_declared_candidates_no_trigger_no_candidates():
    assert dd.declared_candidates("Обычный текст с номером #55 без формулировки.") == set()


# ── find_desync: тело называет открытую задачу, граф не знает ──────────────


def test_find_desync_flags_missing_native_edge():
    issues = [
        {"number": 258, "body": "Единственный настоящий блокер — #133.", "blocked_by_open": []},
        {"number": 133, "body": "Ничего не блокирует.", "blocked_by_open": []},
    ]
    findings = dd.find_desync(issues)
    assert findings == [{"issue": 258, "declared_blocking": 133}]


def test_find_desync_clean_when_native_edge_already_present():
    # Мутация: тот же текст, но связь УЖЕ проставлена в графе — находки нет.
    issues = [
        {"number": 258, "body": "Единственный настоящий блокер — #133.", "blocked_by_open": [133]},
        {"number": 133, "body": "Ничего не блокирует.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_reference_to_closed_issue_not_in_pool():
    # #105 упомянут («после #105»), но не входит в открытый пул (уже закрыт)
    # — не рассинхрон: закрытая ссылка не влияет на приоритет.
    issues = [
        {"number": 111, "body": "после #105 журнал станет доступен.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_false_positive_enumeration():
    issues = [
        {
            "number": 243,
            "body": (
                "Поглощает часть #227 (закрытие задач после слияния) и часть "
                "#201 (дедупликация по отпечатку)."
            ),
            "blocked_by_open": [],
        },
        {"number": 201, "body": "Дедупликация.", "blocked_by_open": []},
        {"number": 227, "body": "Закрытие после слияния.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []


def test_find_desync_ignores_self_reference():
    issues = [
        {"number": 5, "body": "После #5 ничего не меняется.", "blocked_by_open": []},
    ]
    assert dd.find_desync(issues) == []
