#!/usr/bin/env python3
"""Крах инструмента не выдаётся за отказ провайдеров (#1470).

Класс одной фразой: **один локальный отказ, отпечатанный по числу
провайдеров, считался N отказами провайдеров, и итог советовал повтор,
который не может помочь.**

Класс стоил репозиторию дважды, и оба раза совет был заведомо пустым:

  #1315 — промпт не влез в аргумент (`E2BIG`), восемь «отказов» за 78 секунд
          без единого обращения к сети; починили СЛУЧАЙ, класс остался;
  #1467 — транзитивная зависимость dsh обновилась в чужом реестре, dsh стал
          падать на старте за 0–1 секунду; итог снова «повторить прогон», и я
          лично перезапустил три прогона, прежде чем прочитал стек.

Стенд зовёт НАСТОЯЩУЮ функцию сводки из `dsh-ci.sh` на прод-форме данных:
строки `DSH_CHAIN_OUTCOMES` ровно того вида, что пишет
`_dsh_chain_record_outcome`, с дословной выдержкой stderr из прогона
`35752743682`. Текстовый разбор исходника здесь бесполезен: вырезанное тело
проверки прошло бы его молча (AGENTS.md, «Поведенческий тест находит то, чего
структурный не видит»).

Запуск: python -m pytest scripts/lib/test_dsh_tool_crash_class.py -q
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
DSH_CI = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"

# Дословная выдержка из живого прогона ai-review 35752743682 (#1467) —
# прод-форма, а не наш пересказ того, как это могло бы выглядеть.
CRASH_NOTE = (
    "класс не распознан (file:///opt/hostedtoolcache/node/24.20.0/x64/lib/node_modules/"
    "@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-app-boot/lib/index.js:764 "
    "\tif (hmr === void 0) throw new Error(`${binName}: user patch-laye)"
)

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash недоступен")


def _report(outcomes: str, total: int) -> str:
    """Зовёт настоящую `_dsh_chain_report_exhausted` и отдаёт весь её вывод
    плюс два решающих флага — их читает вызывающий конвейер."""
    # Строки идут через ОКРУЖЕНИЕ, а не подстановкой в текст скрипта: табы —
    # разделитель полей `DSH_CHAIN_OUTCOMES`, и в одинарных кавычках bash их
    # не разворачивает. Первая версия стенда подставляла repr() и получала
    # литерал «\t» вместо таба — разбор молча давал ноль transient, а стенд
    # краснел на исправном коде. Поймано исполнением.
    script = (
        f'source "{DSH_CI}"\n'
        'DSH_CHAIN_OUTCOMES="$STAND_OUTCOMES"\n'
        'DSH_CHAIN_TRIED="GLM, OpenRouter-2"\n'
        'DSH_CHAIN_RESET_HINT=""\n'
        f'_dsh_chain_report_exhausted {total} 2>&1\n'
        'printf "RETRY_USEFUL=%s\\n" "${DSH_CHAIN_RETRY_USEFUL:-?}"\n'
        'printf "REASON=%s\\n" "${DSH_RUN_FAILURE_REASON:-}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "STAND_OUTCOMES": outcomes},
        capture_output=True, text=True, encoding="utf-8")
    return result.stdout + result.stderr


def _outcomes(*rows: tuple[str, str, str]) -> str:
    return "".join(f"{name}\t{cls}\t{note}\n" for name, cls, note in rows)


@needs_bash
def test_identical_local_failure_is_not_eight_provider_failures():
    """Живой случай #1467: все опробованные отказали ОДИНАКОВО."""
    out = _report(_outcomes(("GLM", "transient", CRASH_NOTE),
                            ("OpenRouter-2", "transient", CRASH_NOTE)), 2)

    assert "инструмент не стартовал" in out, out
    assert "RETRY_USEFUL=0" in out, "флаг повтора остался поднятым — конвейер будет перезапускать вечно"
    assert "REASON=tool_did_not_start" in out, "причина не машиночитаема"
    assert "повторить прогон" not in out, "совет, который не может помочь, остался"


@needs_bash
def test_different_provider_failures_keep_the_old_path():
    """Настоящие разные отказы — повтор осмыслен, семантика прежняя."""
    out = _report(_outcomes(("GLM", "transient", "HTTP_502 от api.z.ai"),
                            ("OpenRouter-2", "transient", "таймаут соединения")), 2)

    assert "повторить прогон" in out, out
    assert "RETRY_USEFUL=1" in out
    assert "инструмент не стартовал" not in out


@needs_bash
def test_single_provider_is_not_enough_evidence():
    """Один отказ не отличает «наш» от «их». Вердикт по одной точке был бы
    гаданием — ровно тем, что правило «Алерт не гадает» запрещает."""
    out = _report(_outcomes(("GLM", "transient", CRASH_NOTE)), 1)

    assert "инструмент не стартовал" not in out, out


@needs_bash
def test_mixed_classes_mean_providers_did_answer():
    """Есть класс кроме transient — значит провайдеры отвечали, и общий крах
    инструмента исключён."""
    out = _report(_outcomes(("GLM", "quota", "сброс завтра"),
                            ("OpenRouter-2", "transient", CRASH_NOTE)), 2)

    assert "инструмент не стартовал" not in out, out


@needs_bash
def test_empty_notes_prove_nothing():
    """Совпадение пустот — не признак."""
    out = _report(_outcomes(("GLM", "transient", ""),
                            ("OpenRouter-2", "transient", "")), 2)

    assert "инструмент не стартовал" not in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
