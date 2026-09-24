#!/usr/bin/env python3
"""Гвардия: 404 не засчитывается доказательством мёртвого id модели (#1494).

Класс одной фразой: **`HTTP_404` лежал в одном `grep` с `HTTP_410`/`UNKNOWN_MODEL`,
то есть считался ответом провайдера ПРО ID МОДЕЛИ, хотя 404 не различает «такой
модели нет» и «такого маршрута нет».**

Цена замерена (`docs/research/28-provider-chain-truth-table.md`): GLM отдал
`HTTP_404` через цепочку и HTTP 200 прямым вызовом того же `base_url` тем же
секретом в тот же день. Вердикт `dead_model` печатал действие «починить
конфигурацию … Узнать точный id модели» — то есть отправлял следующего агента
чинить исправную запись цепочки.

Гвардия ПОВЕДЕНЧЕСКАЯ, не текстовая: она подгружает настоящий
`scripts/lib/dsh-ci.sh` настоящим bash и зовёт настоящие функции на прод-формах
stderr — дословных, из живых прогонов. Структурный `grep` по исходнику здесь не
годится дважды: он прошёл бы на переименованной функции и не заметил бы
возвращённого `HTTP_404:` внутри другой ветки (AGENTS.md, «Поведенческий тест
находит то, чего структурный не видит»).

Запуск: python -m pytest scripts/lib/test_provider_failure_class_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DSH_CI = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"

#: Прод-формы stderr — дословно из живых прогонов, не пересказ (AGENTS.md,
#: «Тест кормит прод-форму данных, а не пересказ»). Номер прогона рядом с каждой.
PROVEN_MODEL_SCOPED = [
    ("dsh: HTTP_410: DeepSeek API error (HTTP 410)", "worker.yml 35010410097"),
    ("dsh: UNKNOWN_MODEL: model not in catalog", "worker.yml 34753001158, #1130"),
    ("dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output "
     "tokens (65536) for model nemotron-3-ultra", "worker.yml 34730173870"),
]

#: 404 в двух живых формах: та, из-за которой он когда-то попал в список
#: модельных, и та, что пришла от GLM в прогоне, ставшем поводом для #1494.
AMBIGUOUS_404 = [
    ("dsh: HTTP_404: modelCode does not exist", "прогон 33572445063, PR #190"),
    ("dsh: HTTP_404: DeepSeek Messages request failed (404)", "ai-review 35815981434"),
]


def _call(func: str, stderr_text: str) -> bool:
    """Настоящий bash, настоящий `source` прод-файла, настоящий файл stderr.
    Возвращает True, если функция посчитала отказ своим."""
    script = f'set -euo pipefail\nsource "{DSH_CI}"\n{func} "$1" && echo YES || echo NO\n'
    with __import__("tempfile").NamedTemporaryFile("w", suffix=".err", delete=False,
                                                   encoding="utf-8") as handle:
        handle.write(stderr_text + "\n")
        err_path = handle.name
    try:
        result = subprocess.run(["bash", "-c", script, "bash", err_path],
                                capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(err_path).unlink(missing_ok=True)
    assert result.returncode == 0, (
        f"стенд не поднялся: rc={result.returncode}, stderr={result.stderr[:400]}")
    return result.stdout.strip().endswith("YES")


@pytest.mark.parametrize("stderr_text,origin", PROVEN_MODEL_SCOPED,
                         ids=[o for _, o in PROVEN_MODEL_SCOPED])
def test_proven_model_scoped_stays_model_scoped(stderr_text: str, origin: str):
    """Три доказанные формы остаются модельными — #1494 сузил список, но не
    сломал его. Каждая сама различает «эта модель» от «этот аккаунт»."""
    assert _call("_dsh_failure_is_model_scoped", stderr_text), (
        f"форма из {origin} перестала считаться модельной — фолбэк по моделям "
        "внутри аккаунта (#1309) сломан")


@pytest.mark.parametrize("stderr_text,origin", AMBIGUOUS_404,
                         ids=[o for _, o in AMBIGUOUS_404])
def test_404_is_not_proof_of_a_dead_model(stderr_text: str, origin: str):
    """404 НЕ доказывает мёртвый id — это и есть #1494.

    Живой контрпример: тот же id, тот же секрет, HTTP 200 прямым вызовом
    (`docs/research/28-provider-chain-truth-table.md`). Раз контрпример
    существует, 404 не может быть доказательством."""
    assert not _call("_dsh_failure_is_model_scoped", stderr_text), (
        f"404 (форма из {origin}) снова засчитан доказательством мёртвого id — "
        "вердикт dead_model отправит следующего агента чинить исправную запись "
        "цепочки (#1494)")


@pytest.mark.parametrize("stderr_text,origin", AMBIGUOUS_404,
                         ids=[o for _, o in AMBIGUOUS_404])
def test_404_is_recognised_as_ambiguous(stderr_text: str, origin: str):
    """Обратная сторона: 404 обязан быть УЗНАН как неоднозначный, а не просто
    выпасть из модельных.

    Без этой половины фикс выродился бы в «404 больше не модельный» — и тогда
    следующая модель того же аккаунта перестала бы пробоваться вовсе, то есть
    поведение изменилось бы молча. Обе проверки нужны: первая запрещает
    утверждать недоказанное, вторая сохраняет полезное поведение."""
    assert _call("_dsh_failure_is_model_ambiguous", stderr_text), (
        f"404 (форма из {origin}) не узнан как неоднозначный — следующая модель "
        "того же аккаунта не будет опробована, поведение #1309 потеряно")


def test_the_two_detectors_do_not_overlap():
    """Ни одна форма не принадлежит обоим классам сразу: иначе порядок веток
    `if/elif` решал бы вердикт молча, и перестановка строк меняла бы смысл."""
    overlap = [text for text, _ in PROVEN_MODEL_SCOPED + AMBIGUOUS_404
               if _call("_dsh_failure_is_model_scoped", text)
               and _call("_dsh_failure_is_model_ambiguous", text)]
    assert overlap == [], (
        f"формы попали в оба класса сразу: {overlap} — вердикт стал зависеть от "
        "порядка веток, а не от содержания отказа")
