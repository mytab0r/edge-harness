#!/usr/bin/env python3
"""Тесты apply_owner_decision.py (#254) — job, применяющий нажатие кнопки в
Telegram тем же артефактом, что и ручной ответ владельца (#470/#471).

Запуск: python -m pytest scripts/orchestra/test_apply_owner_decision.py -q
"""

import importlib.util
import os
import subprocess
import sys
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
    # Причина различается МАШИННЫМ кодом (#1398), а не только прозой: по нему
    # дедуплицируется след в задаче и по нему же читатель отличает «секрета
    # нет у job'а» от «подписи нет в payload'е».
    with pytest.raises(aod.DecisionRefused, match="пустой подписью") as caught:
        aod.verify_signature("sekret", 471, 2, None)
    assert caught.value.reason == "signature_missing"


def test_verify_signature_empty_string_treated_as_missing():
    with pytest.raises(aod.DecisionRefused, match="пустой подписью") as caught:
        aod.verify_signature("sekret", 471, 2, "")
    assert caught.value.reason == "signature_missing"


def test_signature_missing_alert_states_a_fact_and_does_not_offer_guesses():
    """AGENTS.md, «алерт не гадает». Прежняя редакция этого отказа предлагала
    читателю ВЫБРАТЬ между «переходное окно деплоя, самоустраняется» и
    «вызван напрямую в обход кнопки» — и первая гипотеза успокаивающая: если
    секрета в воркере просто нет, самоустранения не будет никогда, а читатель
    уже успокоен. Данных различить причины у job'а нет (он не читает ни
    секреты Cloudflare, ни версию воркера) — значит он обязан сказать это
    прямо, а не подсовывать угадайку вместо честного пробела."""
    with pytest.raises(aod.DecisionRefused) as caught:
        aod.verify_signature("sekret", 471, 2, "")
    message = str(caught.value)
    assert "установить" in message and "НЕЛЬЗЯ" in message, message
    for guess in ("самоустраняется", "переходное окно", "либо вызван"):
        assert guess not in message, f"гадание осталось в тексте отказа: {message}"


def test_every_refusal_names_its_gas(monkeypatch):
    """AGENTS.md, «Тормоз без газа не принимается»: отказ останавливает работу,
    значит обязан назвать, что возвращает движение. Мутация «убрать gas у
    одного из отказов» красит этот тест — и именно этого не было до #1398:
    нажатие отвергалось, и дальше не происходило ничего."""
    monkeypatch.setattr(aod, "issue_still_waiting", lambda repo, issue: False)
    refusals = []
    for call in (
        lambda: aod.verify_signature(None, 1, 1, "x"),
        lambda: aod.verify_signature("s", 1, 1, None),
        lambda: aod.verify_signature("s", 1, 1, _valid_signature("s", 1, 2)),
        lambda: aod._check_issue_still_waiting("o/r", 1),  # задача больше не ждёт
    ):
        try:
            call()
        except aod.DecisionRefused as refusal:
            refusals.append(refusal)
    assert len(refusals) == 4, "ожидались четыре разных отказа"
    assert len({r.reason for r in refusals}) == 4, "коды причин обязаны различаться"
    for refusal in refusals:
        assert refusal.gas and len(refusal.gas) > 20, f"{refusal.reason}: газ не назван"


def test_verify_signature_mismatch_distinct_message():
    wrong = _valid_signature("sekret", 471, 1)  # подпись для ДРУГОГО варианта
    with pytest.raises(RuntimeError, match="не совпадает"):
        aod.verify_signature("sekret", 471, 2, wrong)


def test_verify_signature_non_ascii_is_mismatch_not_typeerror():
    # Находка ревью PR #1254: строковый hmac.compare_digest бросает TypeError
    # на не-ASCII — подделка кириллицей падала бы четвёртым,
    # не каталогизированным отказом (сырой трейсбек мимо ::error::).
    # Байтовое сравнение даёт обычный SignatureMismatch.
    with pytest.raises(RuntimeError, match="не совпадает"):
        aod.verify_signature("sekret", 471, 2, "подпись")


def test_main_non_ascii_signature_cli_fails_classified_not_traceback():
    """Прод-форма (находка ревью PR #1254): РЕАЛЬНЫЙ вызов строкой команды с
    не-ASCII подписью обязан дать каталогизированный отказ — exit 1,
    ::error:: и текст mismatch в stderr, без сырого TypeError-трейсбека."""
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--repo", "o/r", "--issue", "471", "--option", "2",
            "--signature", "подпись",
        ],
        env={**os.environ, aod.SIGNATURE_SECRET_ENV_VAR: "sekret"},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 1, proc.stderr
    assert "::error::" in proc.stderr
    assert "не совпадает" in proc.stderr
    assert "TypeError" not in proc.stderr
    assert "Traceback" not in proc.stderr


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


# ── Отказ больше не пропадает молча (#1398) ─────────────────────────────────


def _refusal(reason="signature_missing"):
    return aod.DecisionRefused(reason, "подписи нет", "проверить секрет воркера")


def test_refusal_comment_never_carries_the_decision_marker():
    """Худший исход этого файла: текст ОТКАЗА, несущий маркер решения,
    применил бы решение, в котором отказано — `waiting_owner_guard` читает
    маркер в любом комментарии от кого угодно и автора не смотрит вовсе
    (AGENTS.md, #1251). Проверка машинная тем же регулярным выражением, что
    читает гвардия."""
    for reason in ("github_secret_missing", "signature_missing",
                   "signature_mismatch", "not_waiting"):
        text = aod.refusal_comment(_refusal(reason), 471, 2)
        assert aod.DECISION_MARKER_RE.search(text) is None, text


def test_assert_no_decision_marker_actually_refuses_such_a_text():
    """Сама страховка обязана срабатывать, а не быть украшением: мутация
    «снять проверку» иначе прошла бы молча (класс #893 — гвардия, проверяющая
    наличие имени, а не поведение)."""
    with pytest.raises(RuntimeError, match="маркер решения"):
        aod.assert_no_decision_marker("РЕШЕНИЕ: 2\nэто не решение, а цитата")


def test_notify_refusal_answers_both_addressees_with_reason_and_gas(monkeypatch):
    """Два адресата, и оба обязательны по разным причинам: владелец нажал и
    должен узнать ответ тем же каналом; следующий агент читает задачу, а не
    чужой чат и не лог прогона, у которого читателя нет."""
    posted, sent = [], []
    monkeypatch.setattr(aod, "refusal_already_reported", lambda repo, issue, marker: False)
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: posted.append((repo, issue, text)))
    monkeypatch.setattr(aod, "send_telegram", lambda text: sent.append(text) or True)

    aod.notify_refusal("o/r", 471, 2, _refusal())

    assert len(posted) == 1 and len(sent) == 1
    for text in (posted[0][2], sent[0]):
        assert "подписи нет" in text, text
        assert "проверить секрет воркера" in text, text
    assert "https://github.com/o/r/issues/471" in sent[0], sent[0]


def test_notify_refusal_does_not_repeat_the_trace_but_still_answers_the_press(monkeypatch):
    """Повторное нажатие той же кнопки при той же неисправности не плодит
    копии следа (класс #1100), но ответ владельцу уходит КАЖДЫЙ раз: он нажал
    снова именно потому, что не получил ответа."""
    posted, sent = [], []
    monkeypatch.setattr(aod, "refusal_already_reported", lambda repo, issue, marker: True)
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: posted.append(text))
    monkeypatch.setattr(aod, "send_telegram", lambda text: sent.append(text) or True)

    aod.notify_refusal("o/r", 471, 2, _refusal())

    assert posted == []
    assert len(sent) == 1


def test_refusal_marker_separates_reasons_and_options():
    """Дедуп по ПАРЕ причина+вариант: другой вариант или другая неисправность —
    другое событие, и след у него свой, иначе первый же отказ заглушил бы все
    последующие по этой задаче."""
    markers = {
        aod.refusal_marker("signature_missing", 471, 1),
        aod.refusal_marker("signature_missing", 471, 2),
        aod.refusal_marker("signature_mismatch", 471, 1),
        aod.refusal_marker("signature_missing", 472, 1),
    }
    assert len(markers) == 4


def test_notify_refusal_says_loudly_when_a_channel_did_not_deliver(monkeypatch, capsys):
    """«Сигнал ушёл» и «сигнал не ушёл» лечатся по-разному (AGENTS.md, fail
    loud). Мутация «проглотить неудачу доставки» красит этот тест: иначе
    нажатие остаётся без ответа, а лог утверждает обратное."""
    monkeypatch.setattr(aod, "refusal_already_reported",
                        lambda repo, issue, marker: (_ for _ in ()).throw(RuntimeError("gh 403")))
    monkeypatch.setattr(aod, "send_telegram", lambda text: False)

    aod.notify_refusal("o/r", 471, 2, _refusal())

    err = capsys.readouterr().err
    assert "НЕ записан" in err, err
    assert "НЕ отправлено" in err, err


def test_main_attaches_press_context_to_the_refusal(monkeypatch):
    """Контекст нажатия едет В ИСКЛЮЧЕНИИ, а не во второй копии в env: repo,
    задача и вариант уже пришли аргументами, и вторая их копия расходилась бы
    молча (AGENTS.md, «одно место правды»)."""
    monkeypatch.setenv(aod.SIGNATURE_SECRET_ENV_VAR, "sekret")
    with pytest.raises(aod.DecisionRefused) as caught:
        aod.main(["--repo", "o/r", "--issue", "471", "--option", "2", "--signature", "мимо"])
    assert (caught.value.repo, caught.value.issue_number, caught.value.option) == ("o/r", 471, 2)


def test_entrypoint_refusal_is_loud_names_the_gas_and_admits_undelivered(monkeypatch, tmp_path):
    """Сквозной прогон настоящей точки входа, а не пересказ: скрипт
    запускается процессом с заведомо неверной подписью и БЕЗ единого рабочего
    канала доставки — `gh` убран из PATH (недоступность инструмента, а не
    подменённый инструмент), Telegram-переменных нет. Требование: код возврата
    1, причина и газ в логе, и честное признание, что ни один канал не
    доставил — вместо прежнего молчания, где красный прогон был единственным
    следом (#1398)."""
    env = dict(os.environ)
    env["PATH"] = str(tmp_path)  # каталог пустой: gh отсюда недостижим
    env[aod.SIGNATURE_SECRET_ENV_VAR] = "sekret"
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_ACTIONS"):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "o/r", "--issue", "471",
         "--option", "2", "--signature", "заведомо-не-та"],
        capture_output=True, text=True, encoding="utf-8", env=env)

    assert result.returncode == 1, result.stdout + result.stderr
    err = result.stderr
    assert "::error::" in err and "не совпадает" in err, err
    assert "газ —" in err, err
    assert "НЕ записан" in err, err
    assert "НЕ отправлено" in err, err


def test_notify_refusal_posts_nothing_if_the_text_would_apply_the_decision(monkeypatch):
    """Страховка проверяется ПОВЕДЕНИЕМ, а не наличием вызова: если текст
    отказа когда-нибудь начнёт нести маркер решения (правка формулировки,
    цитата варианта, перенос строки), комментарий не должен уйти ВООБЩЕ —
    иначе отказ применит решение, в котором отказывает. Красный job здесь
    дешевле молча применённого чужого решения."""
    posted = []
    monkeypatch.setattr(aod, "refusal_comment",
                        lambda refusal, issue, option: f"РЕШЕНИЕ: {option}\nотказ")
    monkeypatch.setattr(aod, "refusal_already_reported", lambda repo, issue, marker: False)
    monkeypatch.setattr(aod, "post_issue_comment", lambda repo, issue, text: posted.append(text))
    monkeypatch.setattr(aod, "send_telegram", lambda text: True)

    with pytest.raises(RuntimeError, match="маркер решения"):
        aod.notify_refusal("o/r", 471, 2, _refusal())
    assert posted == []


def test_refusal_comment_does_not_claim_a_label_that_is_gone():
    """Находка ai-ревью PR #1401: единый текст «задача остаётся с меткой
    waiting:owner» ложен ровно для причины `not_waiting` — там метки как раз
    НЕТ (снята или задача закрыта), и это и есть причина отказа. След,
    утверждающий обратное, обманывает следующего агента именно в том случае,
    ради которого проверка `issue_still_waiting` написана."""
    gone = aod.refusal_comment(_refusal("not_waiting"), 471, 2)
    assert "нет (снята или задача закрыта)" in gone, gone
    assert "остаётся с меткой" not in gone, gone

    still = aod.refusal_comment(_refusal("signature_missing"), 471, 2)
    assert "остаётся с меткой" in still, still


def test_refusal_trace_lookup_reads_the_whole_history_not_one_page(monkeypatch):
    """Класс #308/#276: эндпоинт комментариев отдаёт СТАРЕЙШИЕ вперёд и
    `sort`/`direction` молча игнорирует, поэтому на задаче длиннее ста
    комментариев первая страница свежего маркера не содержит вовсе — дедуп
    перестал бы находить собственный след и плодил бы копии при каждом
    повторном нажатии (класс #1100). Мутация «вернуть однократный gh с
    per_page=100» красит и этот тест, и механическую гвардию
    scripts/lib/test_pagination_guard.py."""
    marker = aod.refusal_marker("signature_missing", 471, 2)
    pages = {"calls": 0}

    def whole_history(repo, issue_number, max_pages=None):
        pages["calls"] += 1
        # Маркер лежит в ХВОСТЕ истории — там, где его и оставит свежий отказ.
        return [{"body": f"шум {i}"} for i in range(150)] + [{"body": marker}]

    monkeypatch.setattr(aod, "all_issue_comments", whole_history)
    assert aod.refusal_already_reported("o/r", 471, marker) is True
    assert pages["calls"] == 1
