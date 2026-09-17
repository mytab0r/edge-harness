#!/usr/bin/env python3
"""Тесты apply_owner_decision.py (#254) — job, применяющий нажатие кнопки в
Telegram тем же артефактом, что и ручной ответ владельца (#470/#471).

Запуск: python -m pytest scripts/orchestra/test_apply_owner_decision.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("apply_owner_decision.py")
spec = importlib.util.spec_from_file_location("apply_owner_decision", SCRIPT)
aod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aod)  # type: ignore[union-attr]


def test_decision_comment_first_line_matches_format_470_471():
    # Первая строка — единственный формат, который понимает
    # waiting_owner_guard.py (#470/#471): «РЕШЕНИЕ: N». Мутация «изменить
    # префикс/порядок» красит и этот тест, и симметричный тест на стороне TS
    # (parseOwnerDecisionCallback читает тот же формат в обратную сторону).
    text = aod.decision_comment(2)
    first_line = text.splitlines()[0]
    assert first_line == "РЕШЕНИЕ: 2"


def _valid_signature(secret: str, issue: int, option: int) -> str:
    return aod.compute_signature(secret, issue, option)


def test_main_posts_comment_via_post_issue_comment_not_a_second_path(monkeypatch):
    calls = []
    monkeypatch.setenv(aod.SIGNATURE_SECRET_ENV_VAR, "sekret")
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: True)
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: calls.append((repo, issue, text)))
    sig = _valid_signature("sekret", 471, 2)
    rc = aod.main(["--repo", "o/r", "--issue", "471", "--option", "2", "--signature", sig])
    assert rc == 0
    assert calls == [("o/r", 471, aod.decision_comment(2))]


def test_main_propagates_post_issue_comment_failure_loudly(monkeypatch):
    # Мутация «проглотить RuntimeError» красит этот тест: провал записи решения
    # обязан покрасить job (exit 1 в __main__), а не тихо доложиться success.
    def boom(repo, issue, text):
        raise RuntimeError("gh api упал")

    monkeypatch.setenv(aod.SIGNATURE_SECRET_ENV_VAR, "sekret")
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: True)
    monkeypatch.setattr(aod, "post_issue_comment", boom)
    sig = _valid_signature("sekret", 471, 1)
    with pytest.raises(RuntimeError):
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "1", "--signature", sig])


# ── Подпись client_payload (#1251) — три РАЗНЫХ отказа, каждый обрывает main
# ДО post_issue_comment (прод-форма payload: то, что реально шлёт worker,
# см. cf-worker/src/harness.ts #dispatchOwnerDecision/#hmac). ──────────────


def test_compute_signature_matches_worker_hmac_format():
    # Прод-форма: HMAC-SHA256(secret, "issue:option") hex — тот же формат,
    # что #hmac(`${issueNumber}:${option}`, secret) в harness.ts. Проверяем
    # ЗНАЧЕНИЕ известного тестового вектора, не структуру вызова — если
    # формат payload'а разойдётся, подпись живого воркера перестанет
    # совпадать молча, этот тест обязан поймать это первым.
    import hashlib
    import hmac as hmac_module

    expected = hmac_module.new(b"test-webhook-secret", b"471:2", hashlib.sha256).hexdigest()
    assert aod.compute_signature("test-webhook-secret", 471, 2) == expected
    # Замороженный литеральный вектор (находка ревью PR #1254, #1251) —
    # ЗНАЧЕНИЕ HMAC, НЕ пересчитанное тем же модулем: пересчёт выше ловит
    # только «изменился ли вывод compute_signature», литерал ловит сговор
    # обеих сторон — изменение формата одновременно в harness.ts и в этом
    # тесте иначе обе сюиты пропустили бы зелёными. Значение независимо
    # вычислено: HMAC-SHA256(b"test-webhook-secret", b"471:2").hexdigest().
    assert aod.compute_signature("test-webhook-secret", 471, 2) == (
        "63ed810b997707d5af406d1ad0596ec82b66a259c7cd9cc6b97a1c14b62d7d0e"
    )


def test_verify_signature_ok_does_not_raise():
    sig = _valid_signature("sekret", 471, 2)
    aod.verify_signature("sekret", 471, 2, sig)  # не должно кинуть


def test_verify_signature_secret_not_configured_distinct_message():
    with pytest.raises(RuntimeError, match="не настроен"):
        aod.verify_signature(None, 471, 2, "irrelevant")


def test_verify_signature_secret_empty_string_treated_as_not_configured():
    with pytest.raises(RuntimeError, match="не настроен"):
        aod.verify_signature("", 471, 2, "irrelevant")


def test_verify_signature_missing_distinct_message():
    with pytest.raises(RuntimeError, match="не несёт подписи"):
        aod.verify_signature("sekret", 471, 2, None)


def test_verify_signature_empty_string_treated_as_missing():
    with pytest.raises(RuntimeError, match="не несёт подписи"):
        aod.verify_signature("sekret", 471, 2, "")


def test_verify_signature_mismatch_distinct_message():
    wrong = _valid_signature("sekret", 471, 1)  # подпись для ДРУГОГО варианта
    with pytest.raises(RuntimeError, match="не совпадает"):
        aod.verify_signature("sekret", 471, 2, wrong)


def test_verify_signature_three_failure_messages_are_distinct():
    # Класс #1096: три исхода не должны схлопнуться в одну строку — иначе
    # читатель лога не отличит "секрета нет" от "подпись подделана".
    msgs = set()
    for call in (
        lambda: aod.verify_signature(None, 1, 1, "x"),
        lambda: aod.verify_signature("s", 1, 1, None),
        lambda: aod.verify_signature("s", 1, 1, _valid_signature("s", 1, 2)),
    ):
        try:
            call()
        except RuntimeError as error:
            msgs.add(str(error))
    assert len(msgs) == 3


def test_verify_signature_replayed_for_different_issue_rejected():
    # Подпись привязана к ПАРЕ issue+option — подпись, выданная для #471,
    # не годится для #999 с тем же option (перенос подписи на другую задачу).
    sig = _valid_signature("sekret", 471, 2)
    with pytest.raises(RuntimeError, match="не совпадает"):
        aod.verify_signature("sekret", 999, 2, sig)


def test_main_stops_before_issue_still_waiting_check_on_bad_signature(monkeypatch):
    # Подпись проверяется РАНЬШЕ обращения к GitHub API за состоянием
    # issue — неаутентифицированный вызов не должен даже дойти до сети.
    called = []
    monkeypatch.setenv(aod.SIGNATURE_SECRET_ENV_VAR, "sekret")
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: called.append(1) or True)
    monkeypatch.setattr(aod, "post_issue_comment", lambda *a: called.append("posted"))
    with pytest.raises(RuntimeError, match="не совпадает"):
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "2", "--signature", "forged"])
    assert called == []


def test_main_without_secret_env_refuses_even_with_a_valid_looking_signature(monkeypatch):
    # Секрет job'а отсутствует — job НЕ пытается сверять (нечем), и уж тем
    # более не принимает произвольную строку как «валидную».
    monkeypatch.delenv(aod.SIGNATURE_SECRET_ENV_VAR, raising=False)
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: True)
    monkeypatch.setattr(aod, "post_issue_comment", lambda *a: pytest.fail("не должен писать комментарий"))
    with pytest.raises(RuntimeError, match="не настроен"):
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "1", "--signature", "anything"])


# ── issue_still_waiting / отказ на протухшей кнопке (находка ревью PR #486,
# четвёртый заход) ────────────────────────────────────────────────────────


def test_issue_still_waiting_true_when_open_and_labeled(monkeypatch):
    monkeypatch.setattr(
        aod, "gh",
        lambda *a: {"state": "open", "labels": [{"name": "task"}, {"name": "waiting:owner"}]},
    )
    assert aod.issue_still_waiting("o/r", 471) is True


def test_issue_still_waiting_false_when_closed(monkeypatch):
    monkeypatch.setattr(
        aod, "gh",
        lambda *a: {"state": "closed", "labels": [{"name": "waiting:owner"}]},
    )
    assert aod.issue_still_waiting("o/r", 471) is False


def test_issue_still_waiting_false_when_label_removed(monkeypatch):
    # Протухшая кнопка прошлой эскалации: задачу уже разрешили (метка снята
    # гвардией) или переоткрыли под новый раунд без waiting:owner.
    monkeypatch.setattr(aod, "gh", lambda *a: {"state": "open", "labels": [{"name": "task"}]})
    assert aod.issue_still_waiting("o/r", 471) is False


def test_main_refuses_stale_button_loudly_without_posting_comment(monkeypatch):
    """Мутация «применить решение без проверки» красит этот тест: нажатие
    кнопки протухшей эскалации не должно писать «РЕШЕНИЕ: N» в задачу,
    которая больше не ждёт — RuntimeError, не тихий success. Подпись здесь
    валидная (#1251) — тест проверяет ИМЕННО стадию issue_still_waiting,
    которая идёт ПОСЛЕ verify_signature, не смешивает два разных отказа."""
    posted = []
    monkeypatch.setenv(aod.SIGNATURE_SECRET_ENV_VAR, "sekret")
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: False)
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: posted.append(text))
    sig = _valid_signature("sekret", 471, 1)
    with pytest.raises(RuntimeError, match="waiting:owner"):
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "1", "--signature", sig])
    assert posted == []
