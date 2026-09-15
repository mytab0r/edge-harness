#!/usr/bin/env python3
"""Тесты повтора/классификации транзиентного отказа gh api (issue #770).

Прод-форма: сообщения об ошибке — ДОСЛОВНЫЕ строки из живых прогонов (см.
докстринг gh_retry.py) и уже лежащей в репозитории заглушки
scripts/git/test/task-branch-lease.test.sh:133, не пересказ формата.

Запуск: python -m pytest scripts/lib/test_gh_retry.py -q
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("gh_retry.py")
spec = importlib.util.spec_from_file_location("gh_retry", SCRIPT)
gr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gr)  # type: ignore[union-attr]


def _result(returncode, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── Классификация (критерий 1: по структурному факту, не по тексту) ──────────

def test_classify_transient_transport_cutoff_verbatim_prod_form():
    # Живой инцидент issue #770: run 34950741729, шаг verdict PR #1290 —
    # ДОСЛОВНАЯ строка stderr, не пересказ.
    assert gr.classify_gh_error(1, "unexpected end of JSON input") == gr.TRANSIENT


def test_classify_transient_5xx_stub_form_from_repo():
    # scripts/git/test/task-branch-lease.test.sh:133 — уже лежащая в
    # репозитории заглушка, ДОСЛОВНО.
    assert gr.classify_gh_error(1, "gh: HTTP 502: upstream connect error (заглушка поломки)") == gr.TRANSIENT


def test_classify_transient_429_rate_limit():
    assert gr.classify_gh_error(1, "gh: API rate limit exceeded (HTTP 429)") == gr.TRANSIENT


def test_classify_fatal_404_not_found_live_form():
    # Живой вызов 2026-09-15: gh api repos/mytab0r/edge-harness/pulls/999999999
    assert gr.classify_gh_error(1, "gh: Not Found (HTTP 404)") == gr.FATAL


def test_classify_fatal_422_contract_violation():
    assert gr.classify_gh_error(1, "gh: Validation Failed (HTTP 422)") == gr.FATAL


def test_classify_fatal_auth_required_exit_code_4():
    # gh help exit-codes: rc=4 — «требует аутентификации», без HTTP-статуса
    # вовсе (токен не появится сам между попытками).
    assert gr.classify_gh_error(4, "gh: To use GitHub CLI in a GitHub Actions workflow, "
                                    "set the GH_TOKEN environment variable.") == gr.FATAL


def test_classify_unknown_cancelled_exit_code_2_not_treated_as_transient():
    # Третье состояние: rc=2 (cancelled) — не подтверждено, было ли отменой
    # процесса или обрывом по таймауту; НЕ ретраится молча как transient.
    assert gr.classify_gh_error(2, "") == gr.UNKNOWN


def test_classify_mutation_substring_match_would_pass_paraphrase():
    # Мутационная гвардия (issue #770, критерий 1): классификатор по
    # структурному факту не должен спутать 404 (фатально) с транзиентным
    # только потому, что где-то в тексте есть слово "error" — подмена на
    # "по подстроке 'error' → transient" покрасила бы этот тест.
    assert gr.classify_gh_error(1, "gh: Not Found (HTTP 404) — some error context") == gr.FATAL


# ── Повтор: выживает N-1 транзиентных отказов из N попыток ───────────────────

def test_retry_survives_one_transient_blip_then_succeeds():
    calls = []
    sleeps = []

    def fake_run(argv):
        calls.append(list(argv))
        if len(calls) == 1:
            return _result(1, stderr="unexpected end of JSON input")
        return _result(0, stdout='{"ok": true}')

    result = gr.call_gh_with_retry(
        ["gh", "api", "repos/o/r/pulls/1"], run=fake_run, sleep=sleeps.append,
        max_attempts=4, base_delay=0.01, cap_delay=0.02,
    )
    assert result.returncode == 0
    assert len(calls) == 2, "второй попытки не было — обрыв не пережит"
    assert len(sleeps) == 1, "перед повтором обязана быть выдержка"


def test_retry_fatal_fails_immediately_no_retry():
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return _result(1, stderr="gh: Not Found (HTTP 404)")

    with pytest.raises(RuntimeError) as exc:
        gr.call_gh_with_retry(["gh", "api", "repos/o/r/pulls/999"], run=fake_run,
                               sleep=lambda s: pytest.fail("fatal-класс не должен спать в ожидании повтора"))
    assert len(calls) == 1, "содержательный отказ повторяться не должен"
    assert not isinstance(exc.value, gr.GhCallExhausted)
    assert "HTTP 404" in str(exc.value)


def test_retry_unknown_fails_immediately_no_retry():
    calls = []

    def fake_run(argv):
        calls.append(list(argv))
        return _result(2, stderr="")

    with pytest.raises(RuntimeError) as exc:
        gr.call_gh_with_retry(["gh", "api", "repos/o/r/pulls/1"], run=fake_run,
                               sleep=lambda s: pytest.fail("unknown-класс не должен ретраиться"))
    assert len(calls) == 1
    assert not isinstance(exc.value, gr.GhCallExhausted)


# ── Исчерпание бюджета (критерий 3): четыре части в сообщении ────────────────

def test_retry_exhaustion_message_carries_all_four_parts():
    def fake_run(argv):
        return _result(1, stderr="unexpected end of JSON input")

    sleeps = []
    with pytest.raises(gr.GhCallExhausted) as exc:
        gr.call_gh_with_retry(
            ["gh", "api", "-X", "POST", "repos/o/r/issues/612/comments"],
            run=fake_run, sleep=sleeps.append, max_attempts=3,
            base_delay=0.01, cap_delay=0.02,
        )
    error = exc.value
    text = str(error)
    # 1) сам вызов (метод+путь)
    assert "gh api -X" in text
    # 2) класс
    assert "транзиент" in text
    # 3) сколько попыток за сколько времени
    assert error.attempts == 3
    assert "3 попыт" in text
    assert "с)" in text  # секунды напечатаны
    # 4) что делать дальше
    assert "повторить" in text.lower()
    # исчерпание — ровно после max_attempts попыток, не больше и не меньше
    assert len(sleeps) == 2  # выдержка перед 2-й и 3-й, не перед несостоявшейся 4-й


def test_retry_exhaustion_carries_prod_form_stderr_verbatim():
    prod_stderr = "unexpected end of JSON input"

    def fake_run(argv):
        return _result(1, stderr=prod_stderr)

    with pytest.raises(gr.GhCallExhausted) as exc:
        gr.call_gh_with_retry(["gh", "api", "-X", "POST", "repos/o/r/issues/612/comments"],
                               run=fake_run, sleep=lambda s: None, max_attempts=2,
                               base_delay=0.01, cap_delay=0.02)
    assert exc.value.last_stderr == prod_stderr
    assert prod_stderr in str(exc.value)


# ── Мутация: без различения классов transient/fatal ретрай зажигает лишний
# провайдерский бюджет ИЛИ наоборот глотает фатальный отказ молча (issue
# #770, «доказательство мутацией») — эта пара тестов сама и есть рецепт:
# снять ветку `if cls != TRANSIENT: raise RuntimeError(...)` в
# call_gh_with_retry (мутация: всегда ретраить) — test_retry_fatal_fails_
# immediately_no_retry краснеет (fake_run вызвался бы max_attempts раз и
# бросил бы GhCallExhausted вместо RuntimeError на первой попытке).


def test_retry_delay_grows_exponentially_and_caps():
    assert gr.retry_delay_secs(1, base=2.0, cap=20.0) == 2.0
    assert gr.retry_delay_secs(2, base=2.0, cap=20.0) == 4.0
    assert gr.retry_delay_secs(3, base=2.0, cap=20.0) == 8.0
    assert gr.retry_delay_secs(10, base=2.0, cap=20.0) == 20.0, "потолок обязан удержать выдержку"
