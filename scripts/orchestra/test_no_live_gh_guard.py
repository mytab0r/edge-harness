#!/usr/bin/env python3
"""Гвардия на гвардию: живой `gh` из теста действительно невозможен (#1438).

Тест поведенческий и герметичный одновременно: он ПЫТАЕТСЯ сделать ровно
тот вызов, который утекал в сеть, и проверяет, что фикстура из
`conftest.py` останавливает его ДО обращения к сети. Настоящий `gh` при
этом не запускается — в том и смысл.

Почему такой файл вообще нужен. Фикстура `forbid_live_gh` — autouse, то
есть её работа не видна ни в одном отдельном тесте: набор просто зелёный.
Зелёный набор одинаково выглядит и когда фикстура работает, и когда её
вырезали. Это ровно тот ложно-зелёный, о котором AGENTS.md («поведенческий
тест находит то, чего структурный не видит»): пока отсутствие фикстуры
ничего не красит, её удаление пройдёт молча.

Запуск: python -m pytest scripts/orchestra/test_no_live_gh_guard.py -q
"""

import subprocess

import pytest


def test_live_gh_from_a_test_is_refused_loudly():
    """Имя этого теста НЕ в ALLOWED_LIVE_GH, значит фикстура обязана его
    остановить. Команда выбрана безобидной (`gh --version`): если гвардия
    вдруг не сработает, в сеть всё равно ничего не уйдёт, а тест упадёт на
    отсутствии исключения — то есть отказ гвардии виден, но не оплачен
    побочным эффектом."""
    with pytest.raises(AssertionError) as caught:
        subprocess.run(["gh", "--version"], capture_output=True)

    message = str(caught.value)
    # Сообщение обязано назвать ФАКТ (какой именно вызов) — иначе автор
    # получает «что-то запрещено» и идёт гадать (AGENTS.md, «Алерт не гадает»).
    assert "ЖИВОЙ GitHub" in message, message
    assert "gh --version" in message, message
    # И назвать газ: чем это лечится.
    assert "ALLOWED_LIVE_GH" in message, message


def test_live_gh_through_check_output_is_refused_too():
    """Находка ai-review PR #1447, и она про «гарантия шире механизма».

    Первая редакция фикстуры патчила только `subprocess.run`, а
    `check_output`/`check_call`/`call` CPython строит НАПРЯМУЮ через `Popen`,
    минуя `run`. То есть тест с `check_output(["gh", …])` уходил бы в сеть
    молча — под вывеской «живой gh невозможен». Точка подмены перенесена на
    `Popen`; этот тест держит её там."""
    with pytest.raises(AssertionError) as caught:
        subprocess.check_output(["gh", "api", "repos/o/r"])
    assert "ЖИВОЙ GitHub" in str(caught.value)


def test_live_gh_wrapped_in_a_shell_is_refused_too():
    """Вторая половина той же находки: `bash -c "gh api …"` — обход в один
    шаг, если гвардия смотрит только на нулевой элемент команды."""
    with pytest.raises(AssertionError) as caught:
        subprocess.run(["bash", "-c", "gh api repos/o/r"], capture_output=True)
    assert "ЖИВОЙ GitHub" in str(caught.value)


def test_a_non_gh_subprocess_is_not_touched():
    """Обратная сторона, и она важнее: гвардия узкая. Тесты этого
    репозитория реально зовут `git`, `python`, `bash` — если бы фикстура
    ловила любой subprocess, её бы выключили в первый же день, и класс
    вернулся бы целиком."""
    # encoding обязателен рядом с text=True — дефолт CPython на Windows это
    # ANSI-кодовая страница локали, и кириллица в выводе валит decode (#723).
    # Поймано гвардией scripts/lib/test_console_utf8_guard.py на этом же файле.
    result = subprocess.run(
        ["python3", "-c", "print('ок')"], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0
    assert "ок" in result.stdout


def test_the_debt_list_names_a_reason_for_every_entry():
    """Тормоз без газа не принимается (AGENTS.md). Запись в списке долга
    без причины — это «потом разберёмся», а не осознанное исключение."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "orchestra_conftest", Path(__file__).with_name("conftest.py"))
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)

    assert conftest.ALLOWED_LIVE_GH, "список долга пуст — либо всё починено (тогда убери проверку), либо обнаружение сломалось"
    for name, reason in conftest.ALLOWED_LIVE_GH.items():
        assert reason.strip(), f"{name}: исключение без причины"
        assert "#1438" in reason, f"{name}: причина без адреса задачи — читатель не найдёт, чем это лечится"
