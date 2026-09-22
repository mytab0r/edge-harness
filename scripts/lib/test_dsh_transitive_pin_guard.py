#!/usr/bin/env python3
"""Гвардия транзитивного пина dsh под pytest (#1467).

Зачем обёртка, если проверка уже написана на bash. Гейт тела PR
(`pr_mutation_claim_check.py`, ADR 0026) исполняет заявление о мутации ровно
одной формой команды — `python -m pytest <путь> -q`, и это правильно: одна
форма запуска означает один разбор вывода, а не парсер под каждый язык
стенда. Сама проверка остаётся там, где ей место (bash рядом с bash-кодом,
который она проверяет), а здесь — только мост.

Дублирования логики нет намеренно: этот файл НЕ повторяет ни одного сценария
гвардии, он запускает её целиком и требует нулевого кода возврата. Копия
сценариев на Python разошлась бы с оригиналом на первой же правке — ровно тот
рецидив, против которого правило «Одно место правды».

Запуск: python -m pytest scripts/lib/test_dsh_transitive_pin_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GUARD = REPO_ROOT / "scripts" / "lib" / "test" / "dsh-transitive-pin.guard.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")
def test_transitive_pin_guard_is_green():
    """Гвардия проходит целиком — все её сценарии, а не выборка.

    Вывод гвардии печатается в отчёт при отказе: без него «тест упал» не
    отличается от «упал какой именно сценарий», а лечатся они по-разному."""
    result = subprocess.run(
        ["bash", str(GUARD)], cwd=REPO_ROOT,
        capture_output=True, text=True, encoding="utf-8")

    assert result.returncode == 0, (
        f"гвардия транзитивного пина красная (rc={result.returncode}).\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert "гвардия зелёная" in result.stdout, (
        "гвардия завершилась нулём, но не напечатала свой итог — "
        f"проверять по коду возврата в одиночку нельзя:\n{result.stdout}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
