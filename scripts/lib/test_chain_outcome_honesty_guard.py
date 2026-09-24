#!/usr/bin/env python3
"""Гвардия: «класс не распознан» не выдаётся за «повтор поможет» (#1500).

Кормится ПРОД-ФОРМОЙ stderr — строками, скопированными из живых прогонов, а не
пересказом того, как мог бы выглядеть отказ. Тест, построенный на пересказе
чужого формата, зелёный и бесполезный (AGENTS.md).

Гоняется настоящий bash и настоящие функции `dsh-ci.sh`: классификация класса
отказа (`dsh_chain_should_advance`) и сборка сводки
(`_dsh_chain_report_exhausted`). Структурная проверка по исходнику осталась бы
зелёной при вырезанном теле ветки.

Запуск: python -m pytest scripts/lib/test_chain_outcome_honesty_guard.py -q
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

REPO = Path(__file__).resolve().parent.parent.parent
DSH_CI = REPO / "scripts" / "lib" / "dsh-ci.sh"

#: Дословные строки stderr из живых прогонов и класс, который обязан выйти.
#: Каждая запись несёт адрес замера — иначе «прод-форма» ничем не отличается от
#: выдумки, похожей на правду.
LIVE_STDERR = [
    ("dsh: AUTH: Unauthorized", "dead_credential",
     "Ollama, зонд #1520 прогон 35998654827: HTTP 401 authentication_error "
     "на всех трёх ключах, на всех телах и потолках"),
    ("dsh: INVALID_REQUEST: Invalid Anthropic Messages API request", "bad_request",
     "OpenRouter, перехват #1525 прогон 36010507733: провайдер назвал поле "
     "thinking.budget_tokens дословно"),
    ("dsh: HTTP_404: DeepSeek Messages request failed (404)", "http_404",
     "GLM/NVIDIA, ai-review 35994901504"),
    ("dsh: STREAM_CLOSED: DeepSeek Messages stream ended before message_stop",
     "stream_closed", "#1084, worker.yml 2026-09-13T09:39Z"),
    ("dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output"
     " tokens (65536) for model nemotron-3-ultra", "max_tokens_over_model",
     "#1062, прогон 34730173870 — частный случай, обязан выигрывать у общего "
     "bad_request, иначе конфиг одной записи объявят общей поломкой формы"),
    ("dsh: ЧТО-ТО СОВЕРШЕННО НОВОЕ: провайдер придумал свой текст",
     "unrecognised", "будущий неизвестный текст — умолчание"),
]


def classify(stderr_text: str) -> str:
    """Прогнать настоящую `dsh_chain_should_advance` и вернуть класс."""
    script = (
        f'source "{DSH_CI}" >/dev/null 2>&1; '
        'err=$(mktemp); cat >"$err" <<\'STDERR_EOF\'\n'
        f'{stderr_text}\n'
        'STDERR_EOF\n'
        'dsh_chain_should_advance "$err" "" 1 >/dev/null 2>&1; '
        'printf "CLASS:%s\\n" "$DSH_CHAIN_CLASS_ID"')
    result = subprocess.run(["bash", "-c", script], cwd=REPO, capture_output=True,
                            text=True, encoding="utf-8")
    line = next((l for l in result.stdout.splitlines() if l.startswith("CLASS:")), "")
    return line[len("CLASS:"):]


@pytest.mark.parametrize("stderr_text,expected,why", LIVE_STDERR,
                         ids=[c[1] for c in LIVE_STDERR])
def test_live_stderr_gets_its_named_class(stderr_text, expected, why):
    assert classify(stderr_text) == expected, why


def summarize(outcomes: list[tuple[str, str]]) -> tuple[str, str, str]:
    """Прогнать настоящую `_dsh_chain_report_exhausted` на готовых исходах.

    Возвращает (сводка, действие, DSH_CHAIN_RETRY_USEFUL) — ровно то, что
    читает человек и что читает код."""
    rows = "\\n".join(f"{name}\\t{cls}\\t" for name, cls in outcomes)
    script = (
        f'source "{DSH_CI}" >/dev/null 2>&1; '
        f'DSH_CHAIN_OUTCOMES=$(printf "{rows}\\n"); '
        'DSH_CHAIN_TRIED="прогон"; '
        f'_dsh_chain_report_exhausted {len(outcomes)} 2>"$0.err"; '
        'printf "SUMMARY:%s\\n" "$DSH_CHAIN_OUTCOME_SUMMARY"; '
        'printf "RETRY:%s\\n" "$DSH_CHAIN_RETRY_USEFUL"; '
        'printf "ACTION:%s\\n" "$(cat "$0.err" | tr "\\n" " ")"')
    result = subprocess.run(["bash", "-c", script, str(REPO / "chain-report")],
                            cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    out = {}
    for key in ("SUMMARY", "RETRY", "ACTION"):
        line = next((l for l in result.stdout.splitlines() if l.startswith(key + ":")), "")
        out[key] = line[len(key) + 1:]
    Path(str(REPO / "chain-report") + ".err").unlink(missing_ok=True)
    return out["SUMMARY"], out["ACTION"], out["RETRY"]


def test_dead_credentials_do_not_advise_a_retry():
    """Мёртвый ключ — не повод повторять прогон.

    Живой случай, ради которого написано: три записи Ollama отвечали 401, и
    сводка прогон за прогоном советовала «повторить прогон». Повтор с тем же
    ключом даёт тот же 401 — совет был не просто бесполезен, он уводил от
    единственного лечения (ротации)."""
    summary, action, retry = summarize([("Ollama-1", "dead_credential"),
                                        ("Ollama-2", "dead_credential")])

    assert retry == "0", f"советуется повтор там, где он не может помочь: {summary}"
    assert "ключ отвергнут провайдером: 2" in summary
    assert "ротировать ключи" in action, "газ обязан назвать, ЧЕМ это лечится"
    assert "повторить прогон" not in action


def test_rejected_request_form_does_not_advise_a_retry():
    """Отказ по форме запроса детерминирован: тот же запрос даст тот же ответ."""
    summary, action, retry = summarize([("OpenRouter-1", "bad_request")])

    assert retry == "0"
    assert "форма запроса отвергнута: 1" in summary
    assert "починить форму запроса" in action


def test_unrecognised_class_is_an_honest_gap_not_a_transient():
    """«Класс не распознан» и «повтор поможет» — разные утверждения.

    Умолчание не имеет права утверждать второе: следующий нераспознанный текст
    провайдера не обязан быть повторяемым, и объявлять его таким — гадание
    (AGENTS.md, «алерт не гадает»)."""
    summary, action, retry = summarize([("НЕКТО", "cause_unknown")])

    assert retry == "0", "нераспознанный класс объявлен повторяемым"
    assert "класс отказа не распознан" in summary
    assert "НЕ объявляется ни полезным, ни бесполезным" in action, (
        "честный пробел обязан звучать пробелом, а не вердиктом")
    assert "dsh_request_capture" in action, (
        "газ обязан назвать инструмент, которым пробел закрывается (#1525)")


def test_measured_transients_still_advise_a_retry():
    """Обратная сторона: то, что ЗАМЕРЕНО транзиентным, повтор советовать
    обязано — иначе правка просто выключила бы полезный совет."""
    summary, action, retry = summarize([("GLM", "transient")])

    assert retry == "1"
    assert "повторить прогон" in action


def test_mixed_chain_counts_each_bucket_separately():
    """Смешанная цепочка — каждый исход в свою корзину.

    Это и есть форма боевого прогона: у нас одновременно мёртвые ключи,
    отвергнутая форма и живой транзиент. Сводка обязана различать их, иначе
    читатель снова получит одно слово на три разные болезни."""
    summary, action, retry = summarize([
        ("Ollama-1", "dead_credential"), ("OpenRouter-1", "bad_request"),
        ("GLM", "transient"), ("НЕКТО", "cause_unknown")])

    assert "ключ отвергнут провайдером: 1" in summary
    assert "форма запроса отвергнута: 1" in summary
    assert "класс отказа не распознан, #1500): 1" in summary
    assert "транзиентных отказов: 1" in summary
    assert retry == "1", "один настоящий транзиент повтор всё же оправдывает"


def outcome_for(class_id: str) -> str:
    """Прогнать настоящее отображение «класс отказа -> исход провайдера»."""
    result = subprocess.run(
        ["bash", "-c",
         f'source "{DSH_CI}" >/dev/null 2>&1; dsh_chain_outcome_for_class "{class_id}"'],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.strip()


#: Класс отказа и исход, который он обязан дать. Это ядро задачи #1500, и
#: проверять его надо напрямую: первая версия этой гвардии кормила сводку
#: готовыми исходами и пережила мутацию «вернуть transient», ничего не заметив.
CLASS_TO_OUTCOME = [
    ("dead_credential", "dead_credential", "ключ мёртв — лечится ротацией"),
    ("bad_request", "bad_request", "форма запроса — лечится правкой запроса"),
    ("stream_closed", "transient", "обрыв SSE замерен у разных провайдеров (#1084)"),
    ("our_timeout", "transient", "наш нож по времени (#880)"),
    ("max_tokens_over_model", "transient",
     "потолок ОДНОЙ записи — следующая может быть верной (#1062)"),
    ("unrecognised", "cause_unknown", "умолчание обязано быть пробелом, не вердиктом"),
    ("", "cause_unknown", "класс не выставлен вовсе — тот же честный пробел"),
    ("какой-то-новый-класс", "cause_unknown",
     "класс, которого ещё нет в списке, не объявляется повторяемым"),
]


@pytest.mark.parametrize("class_id,expected,why", CLASS_TO_OUTCOME,
                         ids=[c[0] or "(пусто)" for c in CLASS_TO_OUTCOME])
def test_class_maps_to_an_honest_outcome(class_id, expected, why):
    assert outcome_for(class_id) == expected, why


def test_no_class_maps_to_transient_by_default():
    """Ни один нераспознанный класс не даёт `transient`.

    Это ровно то утверждение, которое чинит задача, и оно проверяется
    перебором — не одной строкой: подмена умолчания на `transient` обязана
    краснить гвардию, какой бы класс ни подставили."""
    for made_up in ("HTTP_500", "провайдер_придумал_новое", "AUTH_LIKE", "42"):
        assert outcome_for(made_up) == "cause_unknown", (
            f"«{made_up}» объявлен повторяемым, хотя класс не распознан")
