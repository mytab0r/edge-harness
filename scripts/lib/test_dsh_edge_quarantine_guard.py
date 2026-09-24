#!/usr/bin/env python3
"""Мост к стенду карантина сессий dsh-edge (#1514).

Носитель гвардии — `dsh-edge/test/quarantine-unmigratable.test.mjs`: он
извлекает НАСТОЯЩИЕ методы из unified diff патча
`0007-quarantine-unmigratable-session.patch` и исполняет их как модуль. Это
единственное место правды, и переписывать его на Python значило бы завести
вторую копию стенда — ровно то, что запрещает AGENTS.md.

Зачем тогда этот файл: PR-гейт мутационных доказательств
(`mutation_claim.py::TEST_CMD_RE`) исполняет только `python -m pytest`. Без
моста доказательство мутации для правок этого патча оставалось бы прозой там,
где репозиторий умеет проверять машиной — тот же вывод, что уже сделан в
#1510. Мост ничего не проверяет сам: он зовёт тот же `node --test` и
пробрасывает его вывод в отказ.

Запуск: python -m pytest scripts/lib/test_dsh_edge_quarantine_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SUITE = REPO_ROOT / "dsh-edge" / "test" / "quarantine-unmigratable.test.mjs"


def test_quarantine_suite_passes():
    """Прогон настоящего стенда. Отказ печатает вывод node дословно: читателю
    нужен упавший сценарий, а не «мост сказал нет»."""
    assert SUITE.is_file(), f"стенд карантина не найден: {SUITE}"
    node = shutil.which("node")
    if node is None:
        # «Возможности нет» отделено от «проверка прошла» (AGENTS.md, fail
        # loud): молчаливый зелёный здесь скрыл бы отсутствие проверки вовсе.
        pytest.fail("node не найден в PATH — стенд карантина НЕ прогнан; "
                    "это «возможности нет», а не «сценарии целы»")
    result = subprocess.run([node, "--test", str(SUITE)], cwd=REPO_ROOT,
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=300)
    assert result.returncode == 0, (
        "стенд карантина сессий dsh-edge покраснел:\n"
        + (result.stdout or "")[-4000:] + (result.stderr or "")[-2000:])
