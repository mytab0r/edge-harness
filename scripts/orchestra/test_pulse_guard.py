#!/usr/bin/env python3
"""Тесты предохранителя конвейера и пульса orchestra (scripts/orchestra/pulse_guard.py, #120).

Кормятся прод-формой: таймстампы и payloads — как их реально отдаёт GitHub API
(conclusion-строки, created_at с Z/.000Z/+00:00, workflow_runs/jobs). Проводка
conveyor_gate/heartbeat_check проверяется на моке gh — сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_pulse_guard.py -q
"""

import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("pulse_guard.py")
spec = importlib.util.spec_from_file_location("pulse_guard", SCRIPT)
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)  # type: ignore[union-attr]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def run(conclusion, created_at="2026-08-31T10:00:00Z", run_id=1, title="worker run", event="workflow_dispatch", updated_at=None, actor=None):
    """Прод-форма элемента workflow_runs (поля, которые читает модуль).
    event по умолчанию — workflow_dispatch (реальный тик), чтобы существующие
    тесты, не заботящиеся о фильтре real_orchestra_ticks, не начали молчать.
    updated_at по умолчанию = created_at: у короткого прогона моменты очереди
    и завершения совпадают; долгий прогон задаётся явно (см. регресс-тест
    окна свежести на находку ревью PR #488, раунд 3). actor — login
    triggering_actor (прод-форма Actions API, снято живым `gh api
    .../orchestra.yml/runs` 2026-09-07, issue #689); по умолчанию None —
    существующие тесты, которым различие каналов безразлично, не несут
    лишнего поля."""
    payload = {
        "id": run_id,
        "conclusion": conclusion,
        "created_at": created_at,
        "updated_at": updated_at or created_at,
        "html_url": f"https://github.com/mytab0r/edge-harness/actions/runs/{run_id}",
        "display_title": title,
        "event": event,
    }
    if actor is not None:
        payload["triggering_actor"] = {"login": actor}
    return payload


# ── Серия красных: подсчёт подряд ────────────────────────────────────────────────


@pytest.mark.parametrize("conclusions,expected", [
    ([], 0),
    (["success"], 0),
    (["failure"], 1),
    (["failure", "failure", "failure"], 3),
    (["failure", "cancelled", "failure"], 3),   # отмена — тоже не успех
    (["failure", "failure", "success", "failure"], 2),  # success сбрасывает серию
    (["success", "failure", "failure"], 0),     # самый новый зелёный — серия закрыта
    ([None, "failure", "failure"], 0),          # незавершённый прогон — решать рано
    (["failure", None, "failure"], 1),          # серия прервана незавершённым
])
def test_count_consecutive_failures(conclusions, expected):
    assert pg.count_consecutive_failures(conclusions) == expected


@pytest.mark.parametrize("failures,allowed", [
    (0, True), (2, True),
    (3, False), (4, False), (10, False),
])
def test_decide_dispatch_threshold_is_single_constant(failures, allowed):
    assert pg.decide_dispatch(failures) is allowed
    # порог — аргумент по умолчанию из одной константы: сменили константу —
    # сменилось решение, второй копии порога в коде быть не должно
    assert pg.decide_dispatch(pg.WORKER_FAILURE_PAUSE_AFTER - 1) is True
    assert pg.decide_dispatch(pg.WORKER_FAILURE_PAUSE_AFTER) is False


# ── «Один сигнал на серию»: маркер живёт до следующего success ───────────────────


M = lambda h: utc(2026, 8, 31, h)  # noqa: E731


def test_notification_pending_when_no_marker_yet():
    assert pg.pause_notification_pending([], utc(2026, 8, 31, 9)) is True


def test_notification_silent_while_marker_newer_than_success():
    # маркер (10:00) оставлен позже последнего success (09:00) — серия та же
    assert pg.pause_notification_pending([M(10)], M(9)) is False
    assert pg.pause_notification_pending([M(8), M(10)], M(9)) is False


def test_notification_fires_again_after_success_resets_series():
    # пульсы успели восстановиться (success 12:00 новее маркера 10:00) и снова
    # упали — сигнал обязан прозвучать заново
    assert pg.pause_notification_pending([M(10)], M(12)) is True


def test_notification_silent_without_any_success_once_marker_exists():
    # успехов нет вовсе: серия бесконечна, живого маркера достаточно
    assert pg.pause_notification_pending([M(10)], None) is False


# ── Пульс orchestra: возраст против порога, прод-формы таймстампов ───────────────


def test_parse_prod_timestamp_forms():
    for raw in ("2026-08-31T11:00:00Z", "2026-08-31T11:00:00.000Z", "2026-08-31T11:00:00+00:00"):
        assert pg.heartbeat_age_minutes(raw, utc(2026, 8, 31, 11, 30)) == 30.0


def test_decide_heartbeat_within_threshold():
    # ровно 45 минут — ещё не «старше»; пульс в норме
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 45)) == "ok"
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 15)) == "ok"


def test_decide_heartbeat_stale_beyond_three_intervals():
    # 46 минут > 45 = 3 интервала по 15 — пульсы пропадали
    assert pg.decide_heartbeat("2026-08-31T11:00:00Z", utc(2026, 8, 31, 11, 46)) == "stale"
    assert pg.decide_heartbeat(utc(2026, 8, 31, 10, 0), utc(2026, 8, 31, 11, 0)) == "stale"


# ── Тексты сигналов: маркеры, улики, путь возобновления ──────────────────────────


def test_pause_alert_carries_marker_evidence_and_resume():
    text = pg.pause_alert_text(3, run("failure", run_id=42, title="worker: задача"),
                               "task — шаги: Задача через DSH headless")
    assert pg.PAUSE_MARKER in text                       # маркер, по которому ищется «уже оповещено»
    assert "3 красных прогонов" in text
    assert str(pg.WORKER_FAILURE_PAUSE_AFTER) in text    # порог назван в тексте
    assert "actions/runs/42" in text                     # улика — ссылка на прогон
    assert "Задача через DSH headless" in text           # последняя ошибка
    assert "сбрасывает счётчик" in text                  # как снять паузу


def test_heartbeat_alert_carries_marker_evidence_and_threshold():
    text = pg.heartbeat_alert_text(61.0, run("success", run_id=7))
    assert pg.HEARTBEAT_MARKER in text
    assert "61 мин" in text
    assert str(pg.HEARTBEAT_MAX_AGE_MINUTES) in text
    assert "actions/runs/7" in text
    assert "внешний монитор" in text  # честный пробел: полный охват отложен


# ── Telegram-HTML (#170): parse_mode всегда, кликабельные номера, экранирование ──


def test_send_telegram_always_sends_parse_mode_and_escapes_plain(monkeypatch):
    # Класс #170: parse_mode не передавался нигде — Telegram рендерил plain text
    # и кликабельных ссылок не бывало. Гвардия держит САМ вызов curl: снять
    # "--data-urlencode parse_mode=HTML" или экранирование — тест краснеет.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    # Дефект «прод-запись только внутри GitHub Actions» (2026-09-11): вне CI
    # send_telegram теперь дефолтно DRY-RUN — эти тесты проверяют транспорт
    # (curl/HTML-экранирование), не режим записи, поэтому явно поднимают флаг.
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    calls = []
    monkeypatch.setattr(pg, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
                            returncode=0, stderr="")))
    assert pg.send_telegram('причина: <упало> & "вышло"') is True
    assert len(calls) == 1
    argv = calls[0][0]
    joined = " ".join(argv)
    assert "parse_mode=HTML" in joined
    # plain по умолчанию экранируется: случайные < и & не разваливают доставку
    assert "text=причина: &lt;упало&gt; &amp; \"вышло\"" in joined


def test_send_telegram_as_html_passes_markup_verbatim(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    # Дефект «прод-запись только внутри GitHub Actions» (2026-09-11): вне CI
    # send_telegram теперь дефолтно DRY-RUN — эти тесты проверяют транспорт
    # (curl/HTML-экранирование), не режим записи, поэтому явно поднимают флаг.
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    calls = []
    monkeypatch.setattr(pg, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
                            returncode=0, stderr="")))
    markup = '<a href="https://github.com/o/r/issues/7">#7</a>'
    assert pg.send_telegram(markup, as_html=True) is True
    assert f"text={markup}" in " ".join(calls[0][0])  # повторное экранирование убило бы ссылку


def test_send_telegram_with_reply_markup_passes_json_keyboard(monkeypatch):
    # #254: reply_markup — необязательный параметр, обычные вызовы (тесты выше)
    # его не передают и не ломаются им; здесь проверяется, что переданный
    # действительно уходит в тот же curl-запрос сериализованным JSON.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    # Дефект «прод-запись только внутри GitHub Actions» (2026-09-11): вне CI
    # send_telegram теперь дефолтно DRY-RUN — эти тесты проверяют транспорт
    # (curl/HTML-экранирование), не режим записи, поэтому явно поднимают флаг.
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    calls = []
    monkeypatch.setattr(pg, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
                            returncode=0, stderr="")))
    keyboard = pg.build_decision_keyboard(471, ["Вариант А", "Вариант Б"])
    assert pg.send_telegram("Нужно решение", reply_markup=keyboard) is True
    joined = " ".join(calls[0][0])
    assert f"reply_markup={json.dumps(keyboard)}" in joined


def test_send_telegram_without_reply_markup_does_not_add_the_flag(monkeypatch):
    # Мутация «reply_markup=None всегда сериализуется» красит этот тест: старые
    # вызовы (escalate без options) не должны нести пустой/None reply_markup.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    # Дефект «прод-запись только внутри GitHub Actions» (2026-09-11): вне CI
    # send_telegram теперь дефолтно DRY-RUN — эти тесты проверяют транспорт
    # (curl/HTML-экранирование), не режим записи, поэтому явно поднимают флаг.
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    calls = []
    monkeypatch.setattr(pg, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
                            returncode=0, stderr="")))
    assert pg.send_telegram("обычный алерт") is True
    assert "reply_markup" not in " ".join(calls[0][0])


# ══════════════════════════════════════════════════════════════════════════
# Прод-запись только внутри GitHub Actions (2026-09-11, находка владельца)
# ══════════════════════════════════════════════════════════════════════════
#
# Замер: watchdog-issue #120 несёт 112 из 573 комментариев (19.5%) от логина
# mytab0r, не github-actions[bot] — единственный документированный путь
# вызова scheduler.py (orchestra.yml) держит GH_TOKEN=github.token весь
# процесс, который ВСЕГДА атрибутируется как github-actions[bot]. Комментарий
# от mytab0r значит: чей-то `python scheduler.py` запущен вне этого workflow,
# личным токеном владельца, способным слить PR/задиспетчить воркера в обход
# `concurrency: group: orchestra`. Живая гонка: в 10:57:29Z 2026-09-11 шёл
# настоящий прогон orchestra (run 34591704645, actor github-actions[bot]), и
# в 10:57:56Z — пока он ЕЩЁ ВЫПОЛНЯЛСЯ — инстанс от mytab0r написал маркер
# закрытия WIP-эпизода.
#
# Тесты ниже зовут РЕАЛЬНЫЕ pg.gh/pg.send_telegram (не FakeGh-подмену) — это
# единственный способ проверить сам гейт, а не логику решений поверх него
# (для той логики FakeGh уже используется везде в остальном файле).


def _clear_ci_env(monkeypatch):
    """Явно снимает ВСЕ три сигнала независимо от окружения, в котором
    реально гоняются тесты (в т.ч. когда pytest сам запущен ВНУТРИ
    GitHub Actions, repo-ci.yml — тогда GITHUB_ACTIONS/GITHUB_RUN_ID УЖЕ
    стоят в окружении раннера, и тест без явного delenv тестировал бы не то,
    что заявляет)."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv(pg.ALLOW_PROD_WRITES_ENV, raising=False)


def test_in_github_actions_requires_both_signals(monkeypatch):
    _clear_ci_env(monkeypatch)
    assert pg.in_github_actions() is False
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert pg.in_github_actions() is False  # один признак недостаточен (см. докстринг)
    monkeypatch.setenv("GITHUB_RUN_ID", "123456")
    assert pg.in_github_actions() is True
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert pg.in_github_actions() is False  # GITHUB_RUN_ID один тоже недостаточен


def test_prod_writes_allowed_matrix(monkeypatch):
    _clear_ci_env(monkeypatch)
    assert pg.prod_writes_allowed() is False  # вне CI, без явного флага — заблокировано
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    assert pg.prod_writes_allowed() is True  # явный локальный прогон
    monkeypatch.delenv(pg.ALLOW_PROD_WRITES_ENV, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    assert pg.prod_writes_allowed() is True  # легитимный CI-прогон (событие ИЛИ DO-диспатч)


def test_gh_dry_run_skips_write_call_outside_ci(monkeypatch):
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ (часть 1 — «без газа не уходит»): мок
    HTTP-слоя (subprocess.run) не получает НИ ОДНОГО вызова — изменяющий
    `gh()` вне CI не касается сети вовсе, не просто «возвращает пусто» после
    настоящего запроса.

    До находки ревью PR #950 (третий проход) отказ гейта возвращал None —
    неотличимо от честного пустого ответа сервера, поэтому escalate()/
    post_issue_comment() рапортовали успех, ничего не отправив. Теперь отказ
    наблюдаем: WriteGateSkipped, не немой None."""
    _clear_ci_env(monkeypatch)
    calls = []
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
            returncode=0, stdout="{}", stderr="")))
    with pytest.raises(pg.WriteGateSkipped):
        pg.gh("-X", "POST", "repos/o/r/issues/1/comments", "-f", "body=x")
    assert calls == []  # subprocess.run НЕ вызван вовсе


def test_gh_executes_write_call_inside_ci(monkeypatch):
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ (часть 2 — «с газом уходит»): тот же вызов
    внутри GitHub Actions реально доходит до subprocess.run."""
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "34591704645")
    calls = []
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
            returncode=0, stdout="{}", stderr="")))
    result = pg.gh("-X", "POST", "repos/o/r/issues/1/comments", "-f", "body=x")
    assert len(calls) == 1
    assert result == {}


def test_gh_executes_write_call_with_explicit_local_override(monkeypatch):
    """ДОКАЗАТЕЛЬСТВО МУТАЦИЕЙ (часть 3 — «явный флаг-исключение»): вне CI,
    но с ALLOW_PROD_WRITES_ENV=1, вызов уходит И печатает предупреждение о
    намеренном локальном прогоне (не молчит о своём режиме)."""
    _clear_ci_env(monkeypatch)
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    calls = []
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
            returncode=0, stdout="{}", stderr="")))
    result = pg.gh("-X", "POST", "repos/o/r/issues/1/comments", "-f", "body=x")
    assert len(calls) == 1
    assert result == {}
    assert "ВНЕ GitHub Actions" in pg.announce_write_mode()
    assert pg.ALLOW_PROD_WRITES_ENV in pg.announce_write_mode()


def test_gh_read_call_never_gated_even_outside_ci(monkeypatch):
    """Чтение (без `-X` МЕТОД-записи) не тормозится вовсе — иначе локальная
    диагностика планировщика (`gh()` для отчёта) стала бы невозможна без
    прод-флага, хотя GET ничего не мутирует."""
    _clear_ci_env(monkeypatch)
    calls = []
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
            returncode=0, stdout="[]", stderr="")))
    result = pg.gh("repos/o/r/pulls?state=open&per_page=100")
    assert len(calls) == 1
    assert result == []


def test_gh_explicit_get_method_is_not_a_write(monkeypatch):
    """`-X GET` (используется в паре мест репозитория, например поиск issues)
    остаётся чтением — не должен матчить _gh_call_is_write."""
    assert pg._gh_call_is_write(("-X", "GET", "search/issues")) is False
    assert pg._gh_call_is_write(("-X", "POST", "repos/o/r/issues/1/comments")) is True
    assert pg._gh_call_is_write(("repos/o/r/pulls",)) is False


def test_announce_write_mode_names_all_three_states(monkeypatch):
    _clear_ci_env(monkeypatch)
    assert "DRY-RUN" in pg.announce_write_mode()
    monkeypatch.setenv(pg.ALLOW_PROD_WRITES_ENV, "1")
    assert "ЯВНЫМ решением" in pg.announce_write_mode()
    monkeypatch.delenv(pg.ALLOW_PROD_WRITES_ENV, raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_RUN_ID", "1")
    assert "GitHub Actions" in pg.announce_write_mode() and "DRY-RUN" not in pg.announce_write_mode()


def test_build_decision_keyboard_callback_data_matches_ts_format_and_byte_limit():
    keyboard = pg.build_decision_keyboard(471, ["Вариант А", "Вариант Б", "Обсудить"])
    rows = keyboard["inline_keyboard"]
    assert rows == [
        [{"text": "Вариант А", "callback_data": "wo:471:1"}],
        [{"text": "Вариант Б", "callback_data": "wo:471:2"}],
        [{"text": "Обсудить", "callback_data": "wo:471:3"}],
    ]
    for row in rows:
        assert len(row[0]["callback_data"].encode("utf-8")) <= 64


def test_build_decision_keyboard_rejects_empty_options():
    with pytest.raises(ValueError):
        pg.build_decision_keyboard(471, [])


def test_escalate_without_options_keeps_old_signature_behaviour(monkeypatch):
    posted = []
    sent = []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text, reply_markup=None: sent.append(reply_markup) or True)
    result = pg.escalate("o/r", 120, "обычная эскалация")
    assert sent == [None]  # старые вызовы (без options) не порождают клавиатуру
    assert "доставлен" in result


def test_escalate_with_options_sends_decision_keyboard(monkeypatch):
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: None)
    sent = []
    monkeypatch.setattr(pg, "send_telegram", lambda text, reply_markup=None: sent.append(reply_markup) or True)
    pg.escalate("o/r", 471, "Нужно решение владельца", options=["Вариант А", "Вариант Б"])
    assert sent[0] == pg.build_decision_keyboard(471, ["Вариант А", "Вариант Б"])


def test_escalate_reports_comment_skipped_not_left_when_write_gated(monkeypatch):
    """Находка ревью PR #950 (третий проход): escalate() рапортовал «след в
    #N: оставлен», хотя `post_issue_comment` внутри gh() тихо не отправил
    ничего (гейт DRY-RUN вернул None). Мутация: замени `except
    WriteGateSkipped` на общий `except RuntimeError` без различения —
    сообщение потеряет слово «пропущен», станет неотличимо от реального
    сетевого сбоя (тоже нарушение, но другого класса)."""
    _clear_ci_env(monkeypatch)
    monkeypatch.setattr(pg, "subprocess", SimpleNamespace(
        run=lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run не должен вызываться"))))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: False)
    result = pg.escalate("o/r", 120, "текст эскалации")
    assert result == "Telegram: НЕ доставлен; след в #120: пропущен (DRY-RUN)"


def test_escalation_channel_failed_is_the_single_verdict_of_both_channels_silent():
    """«Оба канала молчат» — критерий живёт рядом с escalate (found: ревью
    PR #607, некритичное замечание: у критерия были вторые копии в
    quotas.py::main и quota_watch._channel_failed). Мутация: переименуй или
    измени формат возврата escalate так, что «НЕ доставлен»/«НЕ оставлен»
    разошлись с предикатом — тест краснеет вместе с вызывающими."""
    assert pg.escalation_channel_failed("Telegram: НЕ доставлен; след в #120: НЕ оставлен") is True
    assert pg.escalation_channel_failed("Telegram: доставлен; след в #120: НЕ оставлен") is False
    assert pg.escalation_channel_failed("Telegram: НЕ доставлен; след в #120: оставлен") is False


def test_escalation_dedup_carrier_failed_is_the_single_verdict_of_dedup_carrier_lost():
    """«Сигнал ушёл, а носитель дедупа нет» — пара к escalation_channel_failed
    (found: ревью PR #607, head 5824e75: гейт сторожа квот выбрасывал вердикт
    доставки stale_alert — при живом Telegram и персистентно падающем следе в
    #120 страница повторялась бы каждый тик, и ни один прогон не краснел).
    Мутация: переименуй предикат или разойди его с форматом возврата escalate
    — тест краснеет вместе с вызывающими."""
    assert pg.escalation_dedup_carrier_failed("Telegram: доставлен; след в #120: НЕ оставлен") is True
    assert pg.escalation_dedup_carrier_failed("Telegram: НЕ доставлен; след в #120: НЕ оставлен") is False
    assert pg.escalation_dedup_carrier_failed("Telegram: доставлен; след в #120: оставлен") is False
    assert pg.escalation_dedup_carrier_failed("Telegram: НЕ доставлен; след в #120: оставлен") is False


def test_carrier_write_verdict_and_its_predicate_visibility():
    """Тихая запись носителя дедупа (первое наблюдение ресурса в норме:
    маркер без эскалации, quota_alert.check_and_alert) возвращает вердикт
    той же лексики, что escalate, и литералы рождаются в pulse_guard рядом
    с предикатами (found: ревью PR #607, head 345a64f — раньше отказ тихой
    записи возвращался строкой «маркер НЕ записан», которую вызывающий не
    прогонял ни через один предикат, и носитель дедупа мог стоять сломанным
    неограниченно долго при зелёных прогонах). Мутации: (1) измени формат
    carrier_write_verdict так, что «НЕ оставлен» разошёлся с предикатом —
    тест краснеет вместе с quota_alert; (2) верни в quota_alert
    немаркированную строку отказа — test_first_observation_marker_write_
    failure_is_predicate_visible краснеет."""
    ok = pg.carrier_write_verdict(120, True)
    assert "не требовалось" in ok          # Telegram-плеча у тихой записи нет вовсе
    assert "оставлен" in ok and "НЕ оставлен" not in ok
    assert pg.escalation_dedup_carrier_failed(ok) is False   # успех — зелёный
    assert pg.escalation_channel_failed(ok) is False

    failed = pg.carrier_write_verdict(120, False, "HTTP 403: закрытая задача")
    assert "НЕ оставлен" in failed
    assert "HTTP 403: закрытая задача" in failed  # причина дословно, не гипотеза
    assert pg.escalation_dedup_carrier_failed(failed) is True
    assert pg.escalation_channel_failed(failed) is False     # «не требовалось» ≠ «НЕ доставлен»


def test_merge_telegram_text_is_short_clickable_and_escaped():
    text = pg.merge_telegram_text("mytab0r/edge-harness", 405, 170,
                                  "Telegram: «задача выполнена» на открытый PR — врёт, а про слияние в main не сообщает")
    assert "слито в main" in text                       # факт мержа назван своими словами
    assert '<a href="https://github.com/mytab0r/edge-harness/issues/170">#170</a>' in text
    assert '<a href="https://github.com/mytab0r/edge-harness/pull/405">#405</a>' in text
    # «Короткое» — это то, что ВИДНО в чате: Telegram рендерит <a>-ссылки как
    # #170/#405, пряча URL. Гвардия держит видимый размер, не байты разметки.
    visible = re.sub(r"<[^>]+>", "", text)
    assert len(visible) < 120, f"сообщение разрослось: {visible!r}"
    assert pg.short_title("один два три четыре пять шесть семь восемь") == "один два три четыре пять шесть"


def test_merge_telegram_text_escapes_hostile_title():
    text = pg.merge_telegram_text("o/r", 1, 2, "a & b <c>")
    assert "a &amp; b &lt;c&gt;" in text               # &/< от задачи не разваливают доставку
    assert '<a href="https://github.com/o/r/issues/2">' in text


def test_worker_telegram_report_sends_parse_mode_too():
    # Второй отправитель репозитория (#170) — bash-овский telegram_report в
    # task.sh: гвардия по исходнику, что он шлёт тот же parse_mode=HTML.
    # Поведенческая проверка Python-отправителя — выше, send_telegram.
    source = (Path(__file__).parent.parent / "worker" / "task.sh").read_text(encoding="utf-8")
    assert '--data-urlencode "parse_mode=HTML"' in source
    assert "выполнена, PR открыт" not in source  # слова «выполнена» в отчёте о PR больше нет


# ── Причина последнего красного: прод-форма jobs ─────────────────────────────────


JOBS_PAYLOAD = {
    "total_count": 2,
    "jobs": [
        {"name": "worker", "conclusion": "success", "steps": []},
        {"name": "task", "conclusion": "failure", "steps": [
            {"name": "Git-авторизация", "conclusion": "success"},
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ],
}


def test_last_failure_error_names_failed_job_and_steps(monkeypatch):
    monkeypatch.setattr(pg, "gh", lambda *a: JOBS_PAYLOAD)
    text = pg.last_failure_error("o/r", {"id": 42, "conclusion": "failure"})
    assert "task" in text and "Задача через DSH headless" in text
    assert "Git-авторизация" not in text  # зелёные шаги не шумят


def test_last_failure_error_loud_when_details_unavailable(monkeypatch):
    def broken(*a):
        raise RuntimeError("gh api: 502")
    monkeypatch.setattr(pg, "gh", broken)
    assert "детали недоступны" in pg.last_failure_error("o/r", {"id": 1})


def test_last_failure_error_shares_failing_jobs_not_second_copy():
    # Чеклист ревью PR #488: докстринг называл failing_jobs общим источником
    # с last_failure_error, а запрос жил в двух копиях. Свод настоящий —
    # гвардия по исходнику: last_failure_error не делает собственного
    # gh-вызова, читает job'ы только через failing_jobs.
    import inspect
    source = inspect.getsource(pg.last_failure_error)
    assert "failing_jobs(repo, run)" in source
    assert 'gh(f"repos' not in source


def test_failing_jobs_conclusions_param_and_loud_failure(monkeypatch):
    # Параметр выводов: timed_out-job «упавший» для failure_watch
    # (FAILURE_WATCH_RUN_CONCLUSIONS) и «не упавший» для дефолтного набора
    # предохранителя — один список job'ов, два вопроса. Сбой запроса —
    # RuntimeError наверх, а не пустой список: «сбой» и «пусто» различаются.
    payload = {"jobs": [
        {"id": 1, "name": "task", "conclusion": "timed_out", "steps": []},
        {"id": 2, "name": "dsh-task", "conclusion": "failure", "steps": []},
    ]}
    monkeypatch.setattr(pg, "gh", lambda *a: payload)
    assert [j["id"] for j in pg.failing_jobs("o/r", {"id": 7}, pg.FAILURE_WATCH_RUN_CONCLUSIONS)] == [1, 2]
    assert [j["id"] for j in pg.failing_jobs("o/r", {"id": 7})] == [2]  # дефолт: timed_out не красный

    def broken(*a):
        raise RuntimeError("gh api: 502")
    monkeypatch.setattr(pg, "gh", broken)
    try:
        pg.failing_jobs("o/r", {"id": 7})
    except RuntimeError:
        pass
    else:
        pytest.fail("сбой запроса job'ов обязан быть громким (RuntimeError), не пустым списком")


# ── Проводка: gate останавливает диспатч, heartbeat кричит ───────────────────────


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов пишется."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")


NOW = utc(2026, 8, 31, 12, 0)
RECENT_FAILURES = {"workflow_runs": [
    run("failure", "2026-08-31T11:50:00Z", 3),
    run("failure", "2026-08-31T11:35:00Z", 2),
    run("failure", "2026-08-31T11:20:00Z", 1),
]}
RECENT_OK = {"workflow_runs": [
    run("success", "2026-08-31T11:50:00Z", 9),
    run("failure", "2026-08-31T11:35:00Z", 3),
    run("failure", "2026-08-31T11:20:00Z", 2),
]}


# ── issue_marker_times: маркер с номером PR — не подстрока чужого номера ────
# (находка ревью #439): "... #16" — подстрока "... #163", голая проверка
# `marker in body` считала эскалацию по PR #16 "уже случившейся" внутри
# комментария про совсем другой PR #163.


def test_issue_marker_times_does_not_match_longer_pr_number(monkeypatch):
    marker = "[ai-review: автоповторы исчерпаны] #16"
    fake = FakeGh({
        "issues/120/comments": [
            {"created_at": "2026-09-06T10:00:00Z",
             "body": "🚨 edge-harness: [ai-review: автоповторы исчерпаны] #163\nдругой PR"},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    assert pg.issue_marker_times("mytab0r/edge-harness", 120, marker) == []


def test_issue_marker_times_matches_exact_pr_number(monkeypatch):
    marker = "[ai-review: автоповторы исчерпаны] #163"
    fake = FakeGh({
        "issues/120/comments": [
            {"created_at": "2026-09-06T10:00:00Z",
             "body": "🚨 edge-harness: [ai-review: автоповторы исчерпаны] #163\nтот самый PR"},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    times = pg.issue_marker_times("mytab0r/edge-harness", 120, marker)
    assert times == [utc(2026, 9, 6, 10, 0)]


def test_gate_blocks_dispatch_after_streak_and_notifies_once(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [],          # маркеров ещё нет
    })
    posted = []
    sent = []
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)

    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False                # диспатч остановлен
    assert any("паузе" in line for line in actions)  # первая тревога серии — действие
    assert len(posted) == 1 and len(sent) == 1
    assert pg.PAUSE_MARKER in posted[0] and "actions/runs/3" in sent[0]

    # второй пульс той же серии: молчит (не спамит), диспатч всё ещё закрыт
    # (формат ответа issues/{N}/comments — голый массив, как у GitHub API)
    fake.routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:59:00Z", "body": f"x {pg.PAUSE_MARKER}"}]
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: posted.append("spam"))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append("spam") or True)
    _, _, allowed2 = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed2 is False
    assert posted == [posted[0]] and sent == [sent[0]]


def test_gate_allows_dispatch_when_series_reset_by_success(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_OK,
        "issues/120/comments": [
            {"created_at": "2026-08-31T10:00:00Z", "body": pg.PAUSE_MARKER}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    # closed-состояние ничего не меняет — это наблюдение (#456), не действие
    assert any("разрешён" in line for line in observations)
    assert actions == []


# ── Полуоткрытое состояние (#205): чистые решения ─────────────────────────────────


@pytest.mark.parametrize("attempt,expected", [
    (1, 15), (2, 30), (3, 60), (4, 120), (5, 240), (6, 240), (100, 240),
])
def test_probe_backoff_grows_exponentially_and_caps(attempt, expected):
    # мутация-гвардия: если убрать min(..., cap) — на большом attempt выдержка
    # улетит за потолок, тест обязан покраснеть (see test_probe_backoff_cap_is_enforced)
    assert pg.probe_backoff_minutes(attempt) == expected


def test_probe_backoff_cap_is_enforced():
    # доказательство мутацией: без потолка probe_backoff_minutes(10) была бы
    # 15 * 2**9 = 7680 мин — гвардия обязана держать 240
    uncapped = pg.PROBE_BACKOFF_BASE_MINUTES * (2 ** 9)
    assert uncapped > pg.PROBE_BACKOFF_MAX_MINUTES
    assert pg.probe_backoff_minutes(10) == pg.PROBE_BACKOFF_MAX_MINUTES


@pytest.mark.parametrize("body,expected", [
    ("прочий текст без маркера", 0),
    (f"{pg.PROBE_MARKER} 1]", 1),
    (f"🔎 edge-harness: {pg.PAUSE_MARKER}\n{pg.PROBE_MARKER} 3]\nостальное", 3),
])
def test_probe_marker_attempts_parses_number(body, expected):
    assert pg.probe_marker_attempts([(utc(2026, 8, 31, 10), body)]) == expected


def test_probe_marker_attempts_takes_max_across_series():
    markers = [
        (utc(2026, 8, 31, 10), f"{pg.PROBE_MARKER} 1]"),
        (utc(2026, 8, 31, 11), f"{pg.PROBE_MARKER} 2]"),
    ]
    assert pg.probe_marker_attempts(markers) == 2


def test_decide_gate_state_closed_when_below_threshold():
    assert pg.decide_gate_state(0, 0, None, NOW) == "closed"
    assert pg.decide_gate_state(2, 0, None, NOW) == "closed"


def test_decide_gate_state_first_entry_has_no_marker_yet():
    # серия только что стала красной — маркера ещё нет, ставим первый (не пробуем)
    assert pg.decide_gate_state(3, 0, None, NOW) == "first"


def test_decide_gate_state_open_before_backoff_elapses():
    marker_at = utc(2026, 8, 31, 11, 50)  # 10 минут назад, выдержка попытки 1 = 15
    assert pg.decide_gate_state(3, 0, marker_at, NOW) == "open"


def test_decide_gate_state_probe_after_backoff_elapses():
    marker_at = utc(2026, 8, 31, 11, 45)  # ровно 15 минут назад — выдержка истекла
    assert pg.decide_gate_state(3, 0, marker_at, NOW) == "probe"


def test_decide_gate_state_open_backoff_grows_with_attempts():
    # после одной красной пробы (probe_attempts=1) выдержка следующей — 30 мин;
    # 15 минут с последнего маркера уже недостаточно
    marker_at = utc(2026, 8, 31, 11, 45)
    assert pg.decide_gate_state(3, 1, marker_at, NOW) == "open"
    assert pg.decide_gate_state(3, 1, utc(2026, 8, 31, 11, 30), NOW) == "probe"


def test_decide_gate_state_mutation_guard_no_backoff_growth():
    # доказательство мутацией: если бы выдержка не росла с attempts (баг —
    # всегда брать первую попытку), 15 минут хватило бы и после красной пробы —
    # это и есть дефект «предохранитель превращается в генератор запусков»
    marker_at = utc(2026, 8, 31, 11, 45)
    broken_backoff = pg.probe_backoff_minutes(1)  # как будто attempts не растут
    assert pg.minutes_between(marker_at, NOW) >= broken_backoff  # баг разрешил бы пробу
    assert pg.decide_gate_state(3, 1, marker_at, NOW) == "open"   # гвардия — не разрешает


def test_heartbeat_ok_is_quiet_and_stale_cries(monkeypatch):
    ok_runs = {"workflow_runs": [run("success", "2026-08-31T11:50:00Z", 5)]}
    fake = FakeGh({"workflows/orchestra.yml/runs": ok_runs, "issues/120/comments": []})
    monkeypatch.setattr(pg, "gh", fake)
    sent = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)
    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 8, 31, 12, 0))
    assert sent == [] and any("в норме" in line for line in lines)

    # последний успех 60 мин назад (порог 45) — опоздавший запуск кричит
    fake.routes["workflows/orchestra.yml/runs"] = {
        "workflow_runs": [run("failure", "2026-08-31T11:59:00Z", 6),
                          run("success", "2026-08-31T11:00:00Z", 5)]}
    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 8, 31, 12, 1))
    assert len(sent) == 1 and "пропадал" in sent[0]
    assert any("пропадал" in line for line in lines)


# ── #133: contract (pull_request) не маскирует пропавший пульс orchestra ────────


def test_real_orchestra_ticks_drops_pull_request_runs():
    runs = [
        run("success", "2026-09-05T14:19:00Z", 1, event="pull_request"),
        run("success", "2026-09-05T13:32:00Z", 2, event="schedule"),
        run("success", "2026-09-05T04:35:00Z", 3, event="workflow_dispatch"),
    ]
    ticks = pg.real_orchestra_ticks(runs)
    assert [t["id"] for t in ticks] == [2, 3]


def test_heartbeat_check_blind_to_pull_request_contract_runs_mutation_guard(monkeypatch):
    # Живая форма #133 (замер 2026-09-05): job orchestra (workflow_dispatch)
    # не бежал ~9ч49м, но между ним и «сейчас» — россыпь зелёных pull_request
    # прогонов job'а contract. Без фильтра last_ok подхватил бы самый свежий
    # pull_request-прогон и heartbeat_check остался бы тихим — ровно тот
    # силент-баг, который держал наблюдателя слепым в проде.
    runs = {"workflow_runs": [
        run("success", "2026-09-05T14:19:02Z", 10, event="pull_request"),
        run("success", "2026-09-05T14:01:22Z", 9, event="pull_request"),
        run("success", "2026-09-05T13:51:26Z", 8, event="pull_request"),
        run("success", "2026-09-05T04:35:49Z", 1, event="workflow_dispatch"),
    ]}
    fake = FakeGh({"workflows/orchestra.yml/runs": runs, "issues/120/comments": []})
    monkeypatch.setattr(pg, "gh", fake)
    sent = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)

    now = utc(2026, 9, 5, 14, 24, 0)
    lines = pg.heartbeat_check("mytab0r/edge-harness", now)
    assert len(sent) == 1 and "пропадал" in sent[0]
    assert any("пропадал" in line for line in lines)

    # Мутация: без фильтра (как раньше) last_ok — самый свежий success ЛЮБОГО
    # события, то есть pull_request-прогон 14:19:02 — 5 минут назад, ниже
    # порога HEARTBEAT_MAX_AGE_MINUTES=45 — heartbeat_check красит тест выше
    # молчанием. Доказываем это явно на тех же данных без фильтра.
    unfiltered_last_ok = next(r for r in runs["workflow_runs"] if r["conclusion"] == "success")
    assert pg.decide_heartbeat(unfiltered_last_ok["created_at"], now) == "ok"


# ── Находка ревью PR #318: клиентский фильтр на одной сырой странице не ─────────
# ── спасает от труncации ≥100 contract-прогонов — нужен серверный ?event=... ────


def test_orchestra_tick_runs_queries_each_legit_event_separately_no_pull_request(monkeypatch):
    """orchestra_tick_runs делает ОТДЕЛЬНЫЙ запрос на каждое легитимное
    событие (?event=schedule, ?event=workflow_dispatch) и ни разу не просит
    event=pull_request — contract, сколько бы его ни было между тиками, на
    эти страницы в принципе не попадает (сервер фильтрует до пагинации)."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": [
            run("success", "2026-09-05T13:32:00Z", 501, event="schedule"),
        ]},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": [
            run("success", "2026-09-05T04:35:00Z", 502, event="workflow_dispatch"),
        ]},
    })
    monkeypatch.setattr(pg, "gh", fake)
    ticks = pg.orchestra_tick_runs("mytab0r/edge-harness")
    assert [t["id"] for t in ticks] == [501, 502]
    assert len(fake.calls) == 2
    assert all("event=pull_request" not in c for c in fake.calls)


def test_heartbeat_check_finds_real_tick_past_page_full_of_contract_runs(monkeypatch):
    """Граничный случай, явно запрошенный ревью: ≥100 pull_request-прогонов
    contract между двумя настоящими тиками. Раньше (клиентский фильтр на
    ОДНОЙ сырой странице) эта масса contract-прогонов ЗАНИМАЛА всю страницу
    per_page=100 и настоящий success 04:35 терялся за ней — heartbeat_check
    молчал бы «не найдено». Серверный фильтр по событию делает вопрос
    неприменимым: страница event=workflow_dispatch физически не может
    содержать ни одного прогона contract, сколько бы их ни было."""
    # Живая форма находки: 150 (>per_page=100) прогонов contract на event=
    # pull_request «существуют», но код НЕ ИМЕЕТ ПРАВА их запрашивать — в
    # FakeGh для event=pull_request нарочно нет маршрута, любой такой запрос
    # упадёт громким AssertionError вместо тихой подмены страницы.
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": []},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": [
            run("success", "2026-09-05T04:35:49Z", 1, event="workflow_dispatch"),
        ]},
        "issues/120/comments": [],
    })
    monkeypatch.setattr(pg, "gh", fake)
    sent = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)

    now = utc(2026, 9, 5, 14, 24, 0)
    lines = pg.heartbeat_check("mytab0r/edge-harness", now)
    assert len(sent) == 1 and "пропадал" in sent[0]
    assert any("пропадал" in line for line in lines)


def test_heartbeat_check_loud_when_zero_ticks_found_after_server_filter(monkeypatch):
    """Пустой результат ПОСЛЕ серверного фильтра (оба легитимных события
    отдали 0 success) — не «выборка коротка» (см. docstring heartbeat_check),
    а реальный тревожный случай: находка ревью #318 — прежняя версия молчала
    ℹ️ без Telegram и без следа в задаче; теперь кричит 🚨 обоими каналами."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": []},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [],
    })
    monkeypatch.setattr(pg, "gh", fake)
    sent, posted = [], []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 24))
    assert lines and lines[0].startswith("🚨")
    assert len(sent) == 1 and pg.HEARTBEAT_NO_TICKS_MARKER in sent[0]
    assert len(posted) == 1 and pg.HEARTBEAT_NO_TICKS_MARKER in posted[0]


def test_heartbeat_check_no_ticks_report_honest_when_comment_post_fails(monkeypatch):
    """Находка AI-ревью PR #318 (третий раунд): строка отчёта раньше
    безусловно утверждала «след в #120», даже если post_issue_comment упал
    (RuntimeError уходил только в stderr-warning) — именно в сценарии
    «тиков нет» канал задачи может быть сломан по той же причине, что и
    пульс. Мутация: убери условный `trace` и верни жёсткое «след в #120)» —
    этот тест покраснеет."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": []},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)

    def failing_post(repo, n, text):
        raise RuntimeError("HTTP 500: transient")
    monkeypatch.setattr(pg, "post_issue_comment", failing_post)

    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 24))
    assert lines and "НЕ оставлен" in lines[0]
    assert "след в #120: оставлен)" not in lines[0]


def test_heartbeat_check_no_ticks_marker_suppresses_repeat_comment_not_telegram(monkeypatch):
    """Один след в задаче на эпизод (маркер HEARTBEAT_NO_TICKS уже стоит) —
    повторный комментарий не плодится, но Telegram кричит на каждый прогон
    (тот же приём, что у heartbeat_alert_text/PAUSE_MARKER)."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": []},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [
            {"created_at": "2026-09-05T14:00:00Z",
             "body": f"🚨 edge-harness: {pg.HEARTBEAT_NO_TICKS_MARKER}\nтекст"}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    sent, posted = [], []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 24))
    assert lines and lines[0].startswith("🚨")
    assert len(sent) == 1
    assert posted == []


def test_heartbeat_check_no_ticks_episode_reopens_after_close_marker(monkeypatch):
    """Находка ревью PR #318, п.1: старый код гасил канал навсегда после первого
    же «тиков нет» — issue_marker_times ищет подстроку по ВСЕЙ истории #120.
    Закрывающий маркер (тики вернулись) новее старого открывающего — новый
    эпизод объявляется заново, а не подавляется старым следом."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": []},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [
            {"created_at": "2026-09-01T00:00:00Z",
             "body": f"🚨 edge-harness: {pg.HEARTBEAT_NO_TICKS_MARKER}\nстарый эпизод"},
            {"created_at": "2026-09-03T00:00:00Z",
             "body": f"✅ edge-harness: {pg.HEARTBEAT_TICKS_RESUMED_MARKER}\nзакрыт"}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    posted = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    lines = pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 24))
    assert lines and lines[0].startswith("🚨")
    assert len(posted) == 1 and pg.HEARTBEAT_NO_TICKS_MARKER in posted[0]


def test_heartbeat_check_closes_no_ticks_episode_when_ticks_return(monkeypatch):
    """Тики снова нашлись, открытый эпизод HEARTBEAT_NO_TICKS ещё не закрыт —
    heartbeat_check публикует закрывающий маркер (иначе episode_reopened
    никогда не увидит момент восстановления)."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": [
            run("success", "2026-09-05T14:00:00Z", 1, event="schedule"),
        ]},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [
            {"created_at": "2026-09-01T00:00:00Z",
             "body": f"🚨 edge-harness: {pg.HEARTBEAT_NO_TICKS_MARKER}\nстарый эпизод"}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    posted = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 10))
    assert len(posted) == 1 and pg.HEARTBEAT_TICKS_RESUMED_MARKER in posted[0]


def test_heartbeat_check_does_not_reclose_already_closed_no_ticks_episode(monkeypatch):
    """Эпизод уже закрыт (закрывающий маркер новее открывающего) — тики есть —
    heartbeat_check не плодит второй закрывающий комментарий."""
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=schedule": {"workflow_runs": [
            run("success", "2026-09-05T14:00:00Z", 1, event="schedule"),
        ]},
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {"workflow_runs": []},
        "issues/120/comments": [
            {"created_at": "2026-09-01T00:00:00Z",
             "body": f"🚨 edge-harness: {pg.HEARTBEAT_NO_TICKS_MARKER}\nстарый эпизод"},
            {"created_at": "2026-09-02T00:00:00Z",
             "body": f"✅ edge-harness: {pg.HEARTBEAT_TICKS_RESUMED_MARKER}\nзакрыт"}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    posted = []
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    pg.heartbeat_check("mytab0r/edge-harness", utc(2026, 9, 5, 14, 10))
    assert posted == []


# ── Независимый DO-пульс (#689): различитель triggering_actor.login ──────────────
#
# Фикстуры ниже — реальные workflow_runs `orchestra.yml` с event=workflow_dispatch
# (снято живым `gh api repos/mytab0r/edge-harness/actions/workflows/orchestra.yml/
# runs?event=workflow_dispatch&per_page=100` 2026-09-07, issue #689): событийный
# будильник (scripts/gh/wake_orchestra.sh, GH_TOKEN=github.token) даёт
# triggering_actor.login=github-actions[bot]; независимый DO-пульс (cf-worker
# alarm()::attemptOrchestraDispatch, GH_DISPATCH_TOKEN) — login владельца
# токена. В это утро независимый канал не тикал с 04:51:45 до как минимум
# 08:53:29 (событийный канал при этом тикал каждые 5-15 минут) — реальный,
# не выдуманный эпизод direction (b) из issue #689.

EVENT_RUNS_MORNING = [
    run("success", "2026-09-07T08:53:29Z", 101, event="workflow_dispatch", actor="github-actions[bot]"),
    run("success", "2026-09-07T08:44:42Z", 102, event="workflow_dispatch", actor="github-actions[bot]"),
    run("success", "2026-09-07T08:26:16Z", 103, event="workflow_dispatch", actor="github-actions[bot]"),
    run("success", "2026-09-07T08:16:15Z", 104, event="workflow_dispatch", actor="github-actions[bot]"),
    run("success", "2026-09-07T04:26:32Z", 105, event="workflow_dispatch", actor="github-actions[bot]"),
]
INDEPENDENT_RUN_MORNING = run(
    "success", "2026-09-07T04:51:45Z", 90, event="workflow_dispatch", actor="mytab0r")


def test_decide_independent_pulse_not_applicable_when_event_channel_itself_silent():
    # Событийный канал сам не тикал недавно — нечего сравнивать (честная
    # граница ложного срабатывания из issue #689: «оркестратор вообще не
    # запускался, некому и мерить»).
    old_event = run("success", "2026-09-06T10:00:00Z", 1, actor="github-actions[bot]")
    state, anchor, exact = pg.decide_independent_pulse([old_event], utc(2026, 9, 7, 9, 0))
    assert state == "not_applicable" and anchor is None


def test_decide_independent_pulse_ok_when_independent_tick_recent():
    # 2026-09-07T04:55 — 04:51:45 (независимый) 3.25 мин назад, 04:26:32
    # (событийный) 28.5 мин назад: оба в пределах порога — здоров.
    runs = [EVENT_RUNS_MORNING[4], INDEPENDENT_RUN_MORNING]
    state, anchor, exact = pg.decide_independent_pulse(runs, utc(2026, 9, 7, 4, 55, 0))
    assert state == "ok" and exact is True
    assert anchor.isoformat() == "2026-09-07T04:51:45+00:00"


def test_decide_independent_pulse_stale_on_real_incident_morning_gap(monkeypatch=None):
    # Прод-форма реального разрыва: событийный канал тикал в 08:16-08:53,
    # независимый — молчал с 04:51:45 (4 ч 8 мин, порог 60 мин).
    runs = EVENT_RUNS_MORNING + [INDEPENDENT_RUN_MORNING]
    state, anchor, exact = pg.decide_independent_pulse(runs, utc(2026, 9, 7, 9, 0, 0))
    assert state == "stale" and exact is True
    assert anchor.isoformat() == "2026-09-07T04:51:45+00:00"
    age = pg.minutes_between(anchor, utc(2026, 9, 7, 9, 0, 0))
    assert age > pg.INDEPENDENT_PULSE_STALE_AFTER_MINUTES
    assert round(age / 60, 1) == 4.1


def test_decide_independent_pulse_mutation_guard_threshold_is_single_constant():
    # Порог — аргумент по умолчанию из одной константы (тот же приём, что у
    # test_decide_dispatch_threshold_is_single_constant): изменили константу —
    # изменилось решение, второй копии порога в коде нет.
    runs = [run("success", "2026-09-07T08:00:00Z", 1, actor="github-actions[bot]"),
            run("success", "2026-09-07T07:00:00Z", 2, actor="mytab0r")]
    now = utc(2026, 9, 7, 8, 0, 0)
    assert pg.decide_independent_pulse(runs, now, stale_after_minutes=61)[0] == "ok"
    assert pg.decide_independent_pulse(runs, now, stale_after_minutes=59)[0] == "stale"


def test_decide_independent_pulse_stale_lower_bound_when_no_independent_in_sample():
    # Ни одного независимого тика во всей изученной выборке — anchor берётся
    # от самого старого прогона выборки (нижняя граница, exact=False):
    # алерт не имеет права утверждать точное число часов, которого не измерял.
    runs = EVENT_RUNS_MORNING
    state, anchor, exact = pg.decide_independent_pulse(runs, utc(2026, 9, 7, 9, 0, 0))
    assert state == "stale" and exact is False
    assert anchor.isoformat() == "2026-09-07T04:26:32+00:00"


def test_independent_pulse_alert_text_names_consequence_not_internal_state():
    text = pg.independent_pulse_alert_text(248.25, exact=True)
    assert "4.1 ч" in text
    assert "конвейер держится только на событийном канале" in text
    assert "встанет, как только прекратится активность PR" in text
    assert "pulse_healthy" not in text  # алерт не гадает внутренним именем поля


def test_independent_pulse_alert_text_marks_lower_bound_when_inexact():
    text = pg.independent_pulse_alert_text(248.25, exact=False)
    assert "как минимум" in text


def _fake_gh_for_morning_gap(extra_comments=None):
    return FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {
            "workflow_runs": EVENT_RUNS_MORNING + [INDEPENDENT_RUN_MORNING]},
        "issues/120/comments": extra_comments or [],
    })


def test_independent_pulse_check_quiet_when_independent_tick_recent(monkeypatch):
    # direction (a): независимый тик недавний — сигнала нет вовсе.
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {
            "workflow_runs": [EVENT_RUNS_MORNING[4], INDEPENDENT_RUN_MORNING]},
        "issues/120/comments": [],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: None)  # закрытие эпизода (нет открытого — не пишет)
    lines = pg.independent_pulse_check("mytab0r/edge-harness", utc(2026, 9, 7, 4, 55, 0))
    assert any("в норме" in line for line in lines)


def test_independent_pulse_check_escalates_on_real_incident_and_names_hours(monkeypatch):
    # direction (b): независимый канал молчал 4+ часа, событийный жив —
    # сигнал уходит, текст называет факт числом часов.
    fake = _fake_gh_for_morning_gap()
    monkeypatch.setattr(pg, "gh", fake)
    sent, posted = [], []
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    lines = pg.independent_pulse_check("mytab0r/edge-harness", utc(2026, 9, 7, 9, 0, 0))

    assert lines and lines[0].startswith("🚨")
    assert len(sent) == 1 and len(posted) == 1
    assert pg.DO_PULSE_MARKER in sent[0] and "4.1 ч" in sent[0]
    assert "встанет, как только прекратится активность PR" in sent[0]


def test_independent_pulse_check_dedups_same_episode(monkeypatch):
    # direction (c): маркер уже стоит для этого эпизода — повторный сигнал
    # не плодится (тот же приём, что у event_wake/heartbeat episode_reopened).
    fake = _fake_gh_for_morning_gap(extra_comments=[
        {"created_at": "2026-09-07T08:55:00Z",
         "body": f"🚨 edge-harness: {pg.DO_PULSE_MARKER}\nранее"}])
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать повторно"))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать повторно"))

    lines = pg.independent_pulse_check("mytab0r/edge-harness", utc(2026, 9, 7, 9, 0, 0))
    assert lines and "эпизод уже оповещён" in lines[0]


def test_independent_pulse_check_closes_episode_when_ticks_resume(monkeypatch):
    # direction (d): DO-дисптачи возобновились (реальный run 20:23:40Z того
    # же дня) — эпизод закрывается сам, без внешнего вмешательства.
    resumed_run = run("success", "2026-09-07T20:23:40Z", 200, actor="mytab0r")
    # Событийный канал обязан быть подтверждён свежим и на этот якорь тоже
    # (иначе not_applicable) — реальный тик github-actions[bot] того же вечера.
    evening_event_run = run("success", "2026-09-07T19:48:04Z", 199, actor="github-actions[bot]")
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {
            "workflow_runs": EVENT_RUNS_MORNING + [resumed_run, evening_event_run]},
        "issues/120/comments": [
            {"created_at": "2026-09-07T09:00:00Z",
             "body": f"🚨 edge-harness: {pg.DO_PULSE_MARKER}\nстарый эпизод"}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    posted = []
    monkeypatch.setattr(pg, "send_telegram", lambda *a: True)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    lines = pg.independent_pulse_check("mytab0r/edge-harness", utc(2026, 9, 7, 20, 30, 0))

    assert any("в норме" in line for line in lines)
    assert len(posted) == 1 and pg.DO_PULSE_RESUMED_MARKER in posted[0]


def test_independent_pulse_check_no_calls_beyond_recent_runs_when_not_applicable(monkeypatch):
    # Холостой ход (событийный канал сам не подтверждён свежим) — ровно один
    # вызов gh (recent_runs), ни маркеров, ни Telegram, ни комментария.
    fake = FakeGh({
        "workflows/orchestra.yml/runs?per_page=100&event=workflow_dispatch": {
            "workflow_runs": [run("success", "2026-09-05T10:00:00Z", 1, actor="github-actions[bot]")]},
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    lines = pg.independent_pulse_check("mytab0r/edge-harness", utc(2026, 9, 7, 9, 0, 0))
    assert lines == []
    assert len(fake.calls) == 1


# ── Полуоткрытое состояние (#205): проводка conveyor_gate ─────────────────────────


def test_gate_first_entry_posts_pause_marker_and_blocks(monkeypatch):
    # серия только что стала красной — маркера ещё нет: ставим PAUSE_MARKER,
    # диспатч НЕ даём в этом же пульсе (первая проба — не раньше следующего)
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [],
    })
    posted, sent = [], []
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)

    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False
    assert len(posted) == 1
    assert pg.PAUSE_MARKER in posted[0] and pg.PROBE_MARKER not in posted[0]
    assert any("паузе" in line for line in actions)  # первая тревога — действие


def test_gate_stays_open_before_backoff_then_probes_after(monkeypatch):
    # маркер паузы стоит 10 минут (выдержка первой попытки — 15): открыт, тихо
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:50:00Z", "body": pg.PAUSE_MARKER}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False
    # state=="open" (выдержка не истекла, уже оповещено) ничего не меняет — наблюдение
    assert any("паузе" in line for line in observations)
    assert actions == []

    # ровно 15 минут прошло — выдержка истекла: ровно одна проба, диспатч разрешён
    fake.routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:45:00Z", "body": pg.PAUSE_MARKER}]
    posted, sent = [], []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert len(posted) == 1 and f"{pg.PROBE_MARKER} 1]" in posted[0]
    # проба отличима отдельной строкой в отчёте — иначе её не отладить (действие)
    assert any("пробный диспатч после паузы" in line for line in actions)


def probe_body(attempt: int) -> str:
    """Прод-форма тела маркера пробы: probe_alert_text содержит и PAUSE_MARKER
    (чтобы issue_markers_any находил его как часть той же серии), и PROBE_MARKER
    с номером попытки — фикстуры собираются функцией кода, а не пересказом."""
    return pg.probe_alert_text(attempt, pg.probe_backoff_minutes(attempt), None, "err")


def test_gate_probe_success_closes_breaker_via_reset_streak(monkeypatch):
    # проба зелёная => следующий прогон worker.yml — success => серия сброшена,
    # count_consecutive_failures вернёт 0 => decide_dispatch снова True
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_OK,  # самый новый прогон — success
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:00:00Z", "body": probe_body(1)}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert any("разрешён" in line for line in observations)
    assert actions == []


def test_gate_probe_failure_grows_backoff_and_blocks_next_probe(monkeypatch):
    # проба #1 была красной (маркер "проба 1]" новее последнего success) —
    # следующая выдержка теперь 30 минут, не 15
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:45:00Z", "body": pg.PAUSE_MARKER},
            {"created_at": "2026-08-31T11:46:00Z", "body": probe_body(1)}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    # 14 минут с последней пробы (11:46 -> 12:00) — меньше выдержки попытки 2 (30 мин)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False
    # state=="open" (уже оповещено) — наблюдение, не действие
    assert any("паузе" in line for line in observations)
    assert actions == []

    # доказательство мутацией: без роста выдержки (attempt всегда 1) те же
    # 20 минут с последней пробы были бы >= 15 и пропустили бы вторую пробу —
    # exp-выдержка (30 мин после первой красной пробы) обязана держать закрытым
    fake.routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:45:00Z", "body": pg.PAUSE_MARKER},
        {"created_at": "2026-08-31T11:40:00Z", "body": probe_body(1)}]
    _, _, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)  # 20 минут прошло
    assert allowed is False  # exp-выдержка (30 мин) ещё не истекла
    broken_backoff_would_allow = pg.minutes_between(
        utc(2026, 8, 31, 11, 40), NOW) >= pg.probe_backoff_minutes(1)
    assert broken_backoff_would_allow is True  # без роста выдержки проба бы прошла


def test_gate_in_progress_probe_does_not_falsely_reopen_dispatch(monkeypatch):
    """Регрессия на реальный баг (#206, ревью): проба ушла (маркер проба 1
    стоит), но её workflow_run ещё in_progress (conclusion=None) — самый
    свежий прогон в списке. count_consecutive_failures останавливается на
    None и вернёт 0, но это НЕ значит «серия закрылась»: маркер активной
    серии обязан удержать gate закрытым, пока выдержка следующей попытки не
    истекла — иначе оркестратор на следующем пульсе решит, что диспатч снова
    разрешён, пока прошлая проба ещё выполняется."""
    fake = FakeGh({
        "workflows/worker.yml/runs": {"workflow_runs": [
            run(None, "2026-08-31T11:46:00Z", 4),          # проба ещё бежит
            run("failure", "2026-08-31T11:35:00Z", 2),
            run("failure", "2026-08-31T11:20:00Z", 1),
        ]},
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:45:00Z", "body": pg.PAUSE_MARKER},
            {"created_at": "2026-08-31T11:46:00Z", "body": probe_body(1)}],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))

    # доказательство мутацией: без фикса (не читая маркеры до decide_dispatch)
    # count_consecutive_failures([None, "failure", "failure"]) == 0 и
    # decide_dispatch(0) вернёт True — gate бы соврал "разрешён".
    assert pg.count_consecutive_failures([None, "failure", "failure"]) == 0
    assert pg.decide_dispatch(0) is True

    # 14 минут с последней пробы (11:46 -> 12:00) — меньше выдержки попытки 2 (30 мин)
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False
    assert any("паузе" in line for line in observations)
    assert actions == []


def test_gate_probe_rate_is_bounded_by_backoff_within_an_hour(monkeypatch):
    """Обратная проверка из критерия приёмки: за час пауза не должна породить
    больше проб, чем предусмотрено выдержкой. Симулируем час пульсов оркестратора
    каждые 15 минут (как в проде, cron orchestra.yml) при неизменно красной серии
    и считаем реальное число проб — оно обязано совпасть с числом проб, которое
    даёт экспоненциальный ряд выдержек, а не с числом пульсов (4 за час)."""
    comments = []

    def fake_issue_comments(repo, n, text):
        comments.append({"created_at": current_now[0].isoformat().replace("+00:00", "Z"), "body": text})

    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", fake_issue_comments)
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)

    start = utc(2026, 8, 31, 12, 0)
    current_now = [start]
    probes = 0
    # час пульсов каждые 15 минут — ровно как cron orchestra.yml в проде
    for minute_offset in range(0, 61, 15):
        current_now[0] = utc(2026, 8, 31, 12, 0)
        from datetime import timedelta
        current_now[0] = start + timedelta(minutes=minute_offset)
        fake.routes["issues/120/comments"] = list(comments)
        _, _, allowed = pg.conveyor_gate("mytab0r/edge-harness", current_now[0])
        if allowed:
            probes += 1

    # выдержки: 15 (первая проба) -> красная -> 30 -> красная -> ждём до 240;
    # за 60 минут с начала серии укладываются только пробы на 15 и 45 минутах
    # (30-минутная выдержка после первой красной пробы на 15-й минуте истекает
    # на 45-й) — итого РОВНО 2 пробы, не 5 (столько дал бы пульс без выдержки)
    assert probes == 2, f"гвардия частоты нарушена: проб за час {probes}, ожидалось 2"


# ── Гонка created_at/completion в анкере серии (issue #899) ──────────────────────


def test_runs_after_uses_completion_time_not_start_time():
    """#899: раньше runs_after сравнивал по created_at (старт) — долгий
    прогон, стартовавший ДО якоря, но завершившийся ПОСЛЕ него, молча выпадал
    бы из серии (свежий провал терялся). Сравнение — по updated_at
    (завершению), тем же основанием, что и у якоря (series_anchor)."""
    long_run = run("failure", created_at="2026-08-31T09:00:00Z", run_id=1,
                    updated_at="2026-08-31T12:30:00Z")
    anchor = utc(2026, 8, 31, 12, 0)
    assert pg.runs_after([long_run], anchor) == [long_run]
    # мутация: сравнение по created_at исключило бы этот прогон (09:00 < 12:00)
    assert pg.parse_time(long_run["created_at"]) < anchor


def test_gate_completion_time_anchor_fixes_stale_marker_race(monkeypatch):
    """Мутационное доказательство (issue #899, воспроизводит живую цитату):
    успешный прогон worker.yml 34498185823 стартовал 2026-09-10T15:49:42Z,
    посреди него (17:06:17Z, T0+80мин) поставлен маркер «проба 4», а сам
    прогон завершился успехом только в 18:53:38Z (T0+180мин).

    ДО фикса анкер серии брался по created_at успеха (T0) — маркер (T0+80)
    «новее» анкера, остаётся в серии, backoff отсчитывается от него, и
    диспатч остаётся заперт ДАЖЕ ПОСЛЕ того, как та же серия закрылась
    успехом. ПОСЛЕ фикса анкер — completion успеха (T0+180) — маркер (T0+80)
    старше анкера и вычёркивается, серия закрыта сразу после успеха."""
    from datetime import timedelta
    T0 = utc(2026, 9, 10, 15, 49)
    completed_at = T0 + timedelta(minutes=180)
    marker_at = T0 + timedelta(minutes=80)
    now = completed_at + timedelta(minutes=5)

    def iso(dt):
        return dt.isoformat().replace("+00:00", "Z")

    fake = FakeGh({
        "workflows/worker.yml/runs": {"workflow_runs": [
            run("success", created_at=iso(T0), run_id=9, updated_at=iso(completed_at)),
            run("failure", "2026-09-10T14:00:00Z", 8),
            run("failure", "2026-09-10T13:00:00Z", 7),
            run("failure", "2026-09-10T12:00:00Z", 6),
        ]},
        "issues/120/comments": [
            {"created_at": iso(marker_at),
             "body": pg.probe_alert_text(4, pg.probe_backoff_minutes(4), None, "err")},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment",
                         lambda *a: pytest.fail("серия закрыта success'ом — новых сигналов быть не должно"))
    monkeypatch.setattr(pg, "send_telegram",
                         lambda *a: pytest.fail("серия закрыта success'ом — новых сигналов быть не должно"))

    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", now)
    assert allowed is True, (
        "ПОСЛЕ фикса (анкер = updated_at успеха): маркер посреди прогона старше "
        "анкера, не держит гейт — диспатч разрешён сразу после успеха")
    assert any("разрешён" in line for line in observations)
    assert actions == []

    # Доказательство мутацией (для отчёта — что именно сломалось бы): по
    # created_at (старая логика) маркер оказывается НОВЕЕ анкера.
    broken_anchor = T0
    assert marker_at > broken_anchor, (
        "ДО фикса анкер = created_at успеха — маркер посреди прогона «новее» "
        "анкера, остаётся в серии, backoff держит gate открытым")


# ── Прогон ещё идёт при активной серии (issue #899, п.2) ─────────────────────────


def test_gate_does_not_stack_new_probe_while_previous_probe_still_running(monkeypatch):
    """#899, п.2: backoff после «проба 1» (30 мин) уже истёк, но сам прогон
    этой пробы всё ещё in_progress (conclusion=None) — заводить пробу 2
    нельзя: это второй одновременный workflow_dispatch воркера на одну и ту
    же паузу, а последующий маркер описал бы ещё бегущий прогон как
    «упавшие job'ы не найдены (conclusion прогона: None)», будто это и есть
    причина (живая цитата инцидента — маркер «проба 4» в #120, 17:06:17Z,
    посреди прогона 34498185823)."""
    fake = FakeGh({
        "workflows/worker.yml/runs": {"workflow_runs": [
            run(None, "2026-08-31T11:10:00Z", 4),          # проба 1 всё ещё бежит
            run("failure", "2026-08-31T10:00:00Z", 2),
            run("failure", "2026-08-31T09:45:00Z", 1),
        ]},
        "issues/120/comments": [
            {"created_at": "2026-08-31T10:15:00Z", "body": pg.PAUSE_MARKER},
            {"created_at": "2026-08-31T11:10:00Z", "body": probe_body(1)},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment",
                         lambda *a: pytest.fail("не должен писать вторую пробу поверх ещё бегущей"))
    monkeypatch.setattr(pg, "send_telegram",
                         lambda *a: pytest.fail("не должен слать вторую пробу поверх ещё бегущей"))

    # 40 минут с маркера пробы 1 (11:10 -> 11:50) — больше выдержки попытки 2 (30 мин)
    later = utc(2026, 8, 31, 11, 50)
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", later)
    assert allowed is False
    assert any("ещё выполняется" in line for line in observations)
    assert actions == []

    # доказательство мутацией: без этой проверки backoff действительно истёк бы
    # к этому моменту, и decide_gate_state вернул бы "probe" — вторую пробу
    would_have_probed = pg.minutes_between(utc(2026, 8, 31, 11, 10), later) >= pg.probe_backoff_minutes(2)
    assert would_have_probed is True


# ── Честный текст «на паузе» (issue #899, п.3) ────────────────────────────────────


def test_gate_open_text_names_effective_failures_not_raw_zero(monkeypatch):
    """#899, п.3: голова списка завершилась НЕ провалом и НЕ успехом
    (например 'skipped') — count_consecutive_failures останавливается на ней
    и вернёт 0, но маркер серии уже доказывает, что порог был достигнут
    раньше. Текст обязан называть ЧИСЛО, которым реально принято решение
    (effective_failures), а не самоопровергающееся «0 красных подряд —
    диспатч остановлен» (живая цитата инцидента, issue #899)."""
    fake = FakeGh({
        "workflows/worker.yml/runs": {"workflow_runs": [
            run("skipped", "2026-08-31T11:55:00Z", 5),
            run("failure", "2026-08-31T11:35:00Z", 2),
            run("failure", "2026-08-31T11:20:00Z", 1),
        ]},
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:50:00Z", "body": pg.PAUSE_MARKER},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать — throttle не истёк"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать — throttle не истёк"))

    # 10 минут с маркера паузы — меньше выдержки первой попытки (15 мин): open
    now = utc(2026, 8, 31, 12, 0)
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", now)
    assert allowed is False
    assert any("3 красных прогонов" in line for line in observations), observations
    assert not any("0 красных прогонов" in line for line in observations), (
        "самоопровергающийся текст: «0 красных подряд — диспатч остановлен» "
        "(живая цитата инцидента)")


# ── Видимость длящейся паузы (issue #899, п.4) ────────────────────────────────────


@pytest.mark.parametrize("elapsed_minutes,expected", [(0, False), (59, False), (60, True), (120, True)])
def test_pause_reminder_due_throttles_by_interval(elapsed_minutes, expected):
    from datetime import timedelta
    last = utc(2026, 8, 31, 10, 0)
    now = last + timedelta(minutes=elapsed_minutes)
    assert pg.pause_reminder_due(last, now) is expected


def test_pause_reminder_due_without_prior_signal_is_immediate():
    assert pg.pause_reminder_due(None, NOW) is True


def test_pause_reminder_marker_is_excluded_from_series_markers(monkeypatch):
    """#899, п.4: напоминание — НЕ маркер серии. issue_markers_any(PAUSE_MARKER,
    RESUME_MARKER) не должен его подхватывать, иначе last_marker_at сдвигался
    бы каждым напоминанием и откладывал бы реальную пробу навсегда."""
    reminder = pg.pause_reminder_text(3, 42.0, None)
    assert pg.PAUSE_MARKER not in reminder
    assert pg.RESUME_MARKER not in reminder
    assert pg.PROBE_MARKER not in reminder
    fake = FakeGh({"issues/120/comments": [
        {"created_at": "2026-08-31T10:00:00Z", "body": pg.PAUSE_MARKER},
        {"created_at": "2026-08-31T13:00:00Z", "body": reminder},
    ]})
    monkeypatch.setattr(pg, "gh", fake)
    markers = pg.issue_markers_any("mytab0r/edge-harness", 120, (pg.PAUSE_MARKER, pg.RESUME_MARKER))
    assert markers == [(utc(2026, 8, 31, 10, 0), pg.PAUSE_MARKER)]


def test_gate_open_state_posts_throttled_reminder_after_interval(monkeypatch):
    """#899, п.4: до этой правки state=='open' молчал в #120 сколько угодно
    долго (backoff-потолок — 240 минут) — живой случай: 4 часа простоя, ни
    одного нового комментария. Напоминание срабатывает раз в
    PAUSE_REMINDER_INTERVAL_MINUTES, не на каждый 15-минутный пульс."""
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T10:00:00Z", "body": pg.PAUSE_MARKER},
            {"created_at": "2026-08-31T10:01:00Z", "body": probe_body(4)},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    posted, sent = [], []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)

    # +30 минут с последнего маркера — меньше и throttle (60), и backoff (240): тихо
    _, actions1, allowed1 = pg.conveyor_gate("mytab0r/edge-harness", utc(2026, 8, 31, 10, 31))
    assert allowed1 is False and posted == [] and sent == [] and actions1 == []

    # +90 минут — throttle истёк, backoff ещё нет: одно напоминание, не серийный маркер
    _, actions2, allowed2 = pg.conveyor_gate("mytab0r/edge-harness", utc(2026, 8, 31, 11, 31))
    assert allowed2 is False
    assert len(posted) == 1 and len(sent) == 1
    assert pg.PAUSE_REMINDER_MARKER in posted[0]
    assert pg.PAUSE_MARKER not in posted[0] and pg.PROBE_MARKER not in posted[0]
    assert any("напоминание" in line for line in actions2)

    # тот же пульс ещё раз почти сразу (напоминание уже отправлено) — тихо
    fake.routes["issues/120/comments"] = fake.routes["issues/120/comments"] + [
        {"created_at": "2026-08-31T11:31:00Z", "body": posted[0]}]
    posted.clear()
    sent.clear()
    _, actions3, allowed3 = pg.conveyor_gate("mytab0r/edge-harness", utc(2026, 8, 31, 11, 35))
    assert allowed3 is False and posted == [] and sent == [] and actions3 == []


# ── Авто-возобновление по мержу (#220): success-маркер — виртуальный success ──────


def resume_body(pr: int = 445, task: int = 205) -> str:
    """Прод-форма тела маркера возобновления: собирается той же функцией кода,
    что пишет его в проде (resume_alert_text) — не пересказом."""
    return pg.resume_alert_text(pr, task, None)


def test_series_anchor_takes_latest_of_success_and_resume():
    assert pg.series_anchor(None, None) is None
    assert pg.series_anchor(M(9), None) == M(9)
    assert pg.series_anchor(None, M(11)) == M(11)
    assert pg.series_anchor(M(9), M(11)) == M(11)
    assert pg.series_anchor(M(12), M(11)) == M(12)  # зелёный прогон новее сброса


def test_runs_after_keeps_only_runs_newer_than_anchor():
    runs = [run("failure", "2026-08-31T11:50:00Z", 3),
            run("failure", "2026-08-31T11:35:00Z", 2),
            run("failure", "2026-08-31T11:20:00Z", 1)]
    assert pg.runs_after(runs, None) == runs  # без якоря — серия бесконечна
    assert pg.runs_after(runs, utc(2026, 8, 31, 11, 35)) == runs[:1]
    assert pg.runs_after(runs, utc(2026, 8, 31, 11, 55)) == []


def test_gate_resume_marker_reopens_dispatch_without_probe(monkeypatch):
    """Ключевой сценарий #220: серия красная, пауза стоит, но слит PR задачи
    последнего красного прогона (маркер возобновления 11:55 новее маркера
    паузы 11:45) — диспатч разрешён СРАЗУ, без выдержки и без комментария
    (новых сигналов серия не породила). Мутации: (а) не читать RESUME_MARKER
    — гейт ушёл бы в пробу с новым комментарием; (б) не отфильтровать маркер
    возобновления из серийных — гейт ушёл бы в open, 5 минут выдержки."""
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:45:00Z", "body": pg.pause_alert_text(3, None, "err")},
            {"created_at": "2026-08-31T11:55:00Z", "body": resume_body()},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("серия сброшена мержем — новых сигналов быть не должно"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("серия сброшена мержем — новых сигналов быть не должно"))

    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert actions == [], "сброс мержем — новых сигналов серия не порождает"
    assert any("сброшена мержем" in line for line in observations), \
        "сброс именно мержем (зелёного прогона не было) обязан быть назван в отчёте"


def test_gate_resume_resets_probe_attempt_numbering(monkeypatch):
    """Сброс мержем закрывает и счётчик попыток пробы: маркеры «пауза»/«проба 1»
    старше маркера возобновления — гейт обязан вернуться в closed, а не считать
    выдержку попытки 2 (30 мин) от чужого маркера. Мутация: оставить маркеры
    в серии — allowed False (open)."""
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_FAILURES,
        "runs/3/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:45:00Z", "body": pg.PAUSE_MARKER},
            {"created_at": "2026-08-31T11:46:00Z", "body": probe_body(1)},
            {"created_at": "2026-08-31T11:55:00Z", "body": resume_body()},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))

    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert actions == []
    assert not any("пробный диспатч" in line for line in observations)


def test_gate_reds_after_resume_form_a_fresh_series(monkeypatch):
    """Сброс не анестезия: красные прогоны ПОСЛЕ маркера возобновления — новая
    серия, считаются с нуля. Два красных после сброса (порог 3) — диспатч
    разрешён; мутация без runs_after: failures=4 по старым красным — пауза."""
    fake = FakeGh({
        "workflows/worker.yml/runs": {"workflow_runs": [
            run("failure", "2026-08-31T11:50:00Z", 4),
            run("failure", "2026-08-31T11:45:00Z", 3),
            run("failure", "2026-08-31T11:20:00Z", 2),
            run("failure", "2026-08-31T11:10:00Z", 1),
        ]},
        "runs/4/jobs": JOBS_PAYLOAD,
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:40:00Z", "body": resume_body()},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True

    # три красных после сброса — предохранитель срабатывает заново (first):
    # гвардия, что сброс не отменяет сам механизм паузы
    fake.routes["workflows/worker.yml/runs"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 5),
        run("failure", "2026-08-31T11:45:00Z", 4),
        run("failure", "2026-08-31T11:42:00Z", 3),
        run("failure", "2026-08-31T11:20:00Z", 2),
        run("failure", "2026-08-31T11:10:00Z", 1),
    ]}
    fake.routes["runs/5/jobs"] = JOBS_PAYLOAD
    posted, sent = [], []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))
    monkeypatch.setattr(pg, "send_telegram", lambda text: sent.append(text) or True)
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is False
    assert len(posted) == 1 and pg.PAUSE_MARKER in posted[0]
    assert len(actions) == 1 and "паузе" in actions[0]  # постинг паузы — действие


def test_stale_resume_marker_does_not_shadow_real_success(monkeypatch):
    """Возобновление старше последнего зелёного прогона — история, не якорь:
    серия после зелёного считается по зелёному, и в отчёте нет слов про мерж."""
    fake = FakeGh({
        "workflows/worker.yml/runs": RECENT_OK,  # success 11:50 — новее сброса 11:30
        "issues/120/comments": [
            {"created_at": "2026-08-31T11:30:00Z", "body": resume_body()},
        ],
    })
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a: pytest.fail("не должен слать"))
    observations, actions, allowed = pg.conveyor_gate("mytab0r/edge-harness", NOW)
    assert allowed is True
    assert not any("мержем" in line for line in observations)


def test_resume_alert_text_carries_marker_evidence():
    text = resume_body(pr=445, task=205)
    assert f"{pg.RESUME_MARKER} #445]" in text   # токен, по которому gate и дедуп читают сброс
    assert "#205" in text                        # задача последнего красного прогона
    assert "без ожидания пробы" in text          # путь возобновления назван
    assert pg.PAUSE_MARKER not in text           # маркер серии — не сброс: их нельзя смешивать


# ── Гвардия непрочитанных провалов ключевых workflow (#477) ─────────────────────


@pytest.mark.parametrize("text,expected", [
    ("dsh: RATE_LIMIT: Rate limit reached for requests", "infra"),
    ("dial tcp: lookup api.github.com: no such host", "infra"),
    ("gh: 502 Bad Gateway", "infra"),
    ("ОШИБКА: main уехал вперёд (оркестратор слил PR-ы) — base протух.", "stale_base"),
    # Находка ревью PR #488: "protected branch hook declined" — НЕ признак
    # протухшей базы (это отказ серверного хука по другой причине, обычно
    # наш дефект workflow), сигнатуру убрали из STALE_BASE_SIGNATURES —
    # такой текст обязан классифицироваться как defect, не тихо прощаться.
    ("! [remote rejected] agent/1-x -> agent/1-x (protected branch hook declined)", "defect"),
    ("scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory", "defect"),
    ("AssertionError: expected 3 got 2", "defect"),
    ("что угодно нераспознанное", "defect"),  # fail loud: непонятное — дефект, не прощаем молча
])
def test_classify_failure_cause(text, expected):
    assert pg.classify_failure_cause(text) == expected


def test_failure_fingerprint_stable_across_run_specific_noise():
    # Один и тот же баг на РАЗНЫХ прогонах (разные run id/номера строк/hex) —
    # обязан схлопнуться в один отпечаток: иначе «не спамить» не работает.
    a = pg.failure_fingerprint(
        "worker.yml", "task",
        "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")
    b = pg.failure_fingerprint(
        "worker.yml", "task",
        "scripts/worker/task.sh: line 402: .../infra_digest.sh: No such file or directory")
    assert a == b


def test_failure_fingerprint_distinguishes_workflow_and_job():
    base = pg.failure_fingerprint("worker.yml", "task", "No such file or directory")
    other_workflow = pg.failure_fingerprint("hands.yml", "task", "No such file or directory")
    other_job = pg.failure_fingerprint("worker.yml", "dsh-task", "No such file or directory")
    other_text = pg.failure_fingerprint("worker.yml", "task", "AssertionError: boom")
    assert len({base, other_workflow, other_job, other_text}) == 4


FAILURE_WATCH_QUIET_ROUTES = {
    f"workflows/{wf}/runs?status=completed": {"workflow_runs": []}
    for wf in pg.WATCHED_WORKFLOWS
}
# Счётчик суточного потолка (FAILURE_WATCH_DAILY_CAP) — по умолчанию пусто
# (потолок никогда не исчерпан), тесты, проверяющие сам потолок, переопределяют
# этот маршрут явно.
FAILURE_WATCH_QUIET_ROUTES["issues?state=all&labels=ci-failure"] = []


def _stdout_with_error(line: str):
    return SimpleNamespace(returncode=0, stdout=f"2026-09-06T10:18:00.0000000Z {line}\n")


def test_last_error_log_line_skips_runner_boilerplate_finds_real_cause(monkeypatch):
    # Ветка 1 (предпочтительная): причина аннотирована ::error::-строкой
    # (наши die()/echo "::error::" в task.sh), boilerplate раннера
    # («Process completed with exit code N», «Cleaning up») пропускается.
    log = (
        "2026-09-06T10:17:58.0000000Z ##[error]Нет доступа к морде dsh-edge — "
        "job красный (#119)\n"
        "2026-09-06T10:17:59.0000000Z Cleaning up orphan processes\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "dsh-edge" in line
    assert "exit code" not in line


def test_last_error_log_line_plain_stderr_tail_when_no_error_annotation(monkeypatch):
    # Находка ревью PR #488 (раунд 6, блокирующая): содержательные причины
    # почти везде идут ПРОСТОЙ stderr-строкой без ##[error] — живой замер на
    # прогоне 34027035455: bash печатает «line N: ... No such file or
    # directory» plain-строкой, аннотированной в логе только boilerplate
    # раннера. Только-##[error]-канал делал факт недостижимым (orchestra.yml
    # не содержит ни одного ::error вовсе). Фикстура — прод-форма, не пересказ.
    log = (
        "2026-09-06T10:17:58.0000000Z ##[group]Run bash scripts/worker/task.sh\n"
        "2026-09-06T10:17:58.5000000Z source: /home/runner/work/edge-harness/edge-harness/scripts/gh/infra_digest.sh: No such file or directory\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "No such file or directory" in line
    assert "##[" not in line and "exit code" not in line


def test_last_error_log_line_worker_wrapper_is_not_a_fact(monkeypatch):
    # Находка ревью PR #488 (раунд 6, блокирующая): «Воркер не справился:
    # dsh завершился с кодом N без открытого PR» (die() в task.sh) — факт
    # падения ОБЁРТКИ, не причина; нормализация цифр делала из него один
    # отпечаток на любую причину агентского провала. Причина живёт в
    # plain-хвосте (вывод dsh) — факт берётся оттуда.
    log = (
        "2026-09-06T10:17:57.0000000Z dsh: AssertionError: пул пуст, а задача назначена\n"
        "2026-09-06T10:17:58.0000000Z ##[error]Воркер не справился: dsh завершился с кодом 1 без открытого PR\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "пул пуст" in line
    assert "Воркер не справился" not in line


def test_last_error_log_line_guard_catalog_wrapper_is_not_a_fact(monkeypatch):
    # PR #771 ревью, блокирующая 1: `scripts/ci/run_guards.sh:42` эмитит
    # «::error::гвардия каталога '<имя>' … провалилась» как последняя
    # аннотированная строка перед «Process completed» — то же самое, что и
    # «Воркер не справился» выше: факт падения ОБЁРТКИ переборщика, а не
    # причины упавшей гвардии. Прод-форма: причина живёт как WARNING pip
    # retry в plain-хвосте лога.
    log = (
        "2026-09-06T10:17:55.0000000Z WARNING: Retrying (Retry(total=4, connect=None, "
        "read=None, redirect=None, status=None)) after connection reset by peer: "
        "'https://pypi.org/simple/pyyaml/'\n"
        "2026-09-06T10:17:58.0000000Z ##[error]гвардия каталога 'stale-blocked-guard' "
        "(scripts/ci/guards/stale-blocked-guard.sh) провалилась\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "pypi.org" in line
    assert "гвардия каталога" not in line


def test_last_error_log_line_wrapper_without_tail_returns_none(monkeypatch):
    # Обёртка без содержательного хвоста — не превращается в «факт с ложной
    # точностью»: нет строки → вызывающий уходит в громкое наблюдение, задачу
    # не заводит (тот же контракт, что у only-boilerplate выше).
    log = (
        "2026-09-06T10:17:58.0000000Z ##[error]Воркер не справился: dsh завершился с кодом 1 без открытого PR\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
        "2026-09-06T10:18:01.0000000Z Cleaning up orphan processes\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    assert pg.last_error_log_line("mytab0r/edge-harness", 999) is None


def test_last_error_log_line_only_boilerplate_returns_none(monkeypatch):
    # Без содержательной строки — None, вызывающий откатывается на имена шагов
    # (существующая ветка `fact = error_line or f"шаги: {step_names}"`).
    log = "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    assert pg.last_error_log_line("mytab0r/edge-harness", 999) is None


def test_last_error_log_line_passes_allow_escape_sequences():
    # Живая проверка PR #488: сырой лог job'а несёт ANSI-escape, и gh (замер
    # 2.98.0, 2026-09-06) отвечает ОТКАЗОМ exit 1 без --allow-escape-sequences
    # («the response contains terminal escape sequences») — без флага факт
    # недостижим в проде вообще, при зелёных тестах на моках. Гвардия по
    # исходнику: мок subprocess принимает любые аргументы и этого не ловит.
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"gh", "api", "--allow-escape-sequences"' in source, (
        "last_error_log_line обязан звать gh api с --allow-escape-sequences")


def test_last_error_log_line_ignores_post_job_cleanup_teardown(monkeypatch):
    # Находка #610 — фикстура дословно повторяет реальный лог job'а orchestra
    # (run 34069761104): checkout печатает секцию "Post job cleanup." (чистка
    # ssh/http/credentials config) ПОСЛЕ настоящего последнего вывода job'а.
    # "Removing credentials config '<UUID>.config'" содержит буквы и цифры и
    # раньше проходила все фильтры как «факт» — реальный последний вывод
    # («прогон окрашен красным») терялся за teardown-секцией.
    log = (
        "2026-09-07T00:27:52.8096896Z ### Детектор простоя (#201)\n"
        "2026-09-07T00:27:52.8097925Z 🚨 потолок автозаведённых задач в сутки исчерпан "
        "(5/5) — отпечаток gate:pipeline-paused НЕ заведён, нужен человек\n"
        "2026-09-07T00:27:52.8101423Z 🚨 прогон окрашен красным (Telegram: доставлен; "
        "след в #120: оставлен)\n"
        "2026-09-07T00:27:52.8187273Z ##[error]Process completed with exit code 1.\n"
        "2026-09-07T00:27:52.8369619Z Post job cleanup.\n"
        "2026-09-07T00:27:52.9162261Z Temporarily overriding HOME='/home/runner/work/_temp/"
        "cfa9d952-f45c-49c0-9dae-0a4c5348684d' before making global git config changes\n"
        "2026-09-07T00:27:53.0000000Z Removing credentials config "
        "'/home/runner/work/_temp/git-credentials-8a8eb9a3-bd3f-4668-8fdb-b30a4b91f216.config'\n"
        "2026-09-07T00:27:53.0100000Z Cleaning up orphan processes\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "прогон окрашен красным" in line
    assert "credentials" not in line and "Post job cleanup" not in line


def test_last_error_log_line_two_runs_same_cause_give_same_fingerprint_after_fix(monkeypatch):
    # Мутационная проверка класса #610: два прогона с ОДНОЙ и той же причиной,
    # но РАЗНЫМ UUID teardown-секции, обязаны дать ОДИН и тот же fingerprint —
    # до фикса last_error_log_line подхватывал разный "Removing credentials
    # config '<UUID>.config'" на каждый прогон и давал разные fingerprint при
    # одинаковом заголовке issue (живой случай #578/#580/#589/#592/#598).
    def log_with_uuid(uuid: str) -> str:
        return (
            "2026-09-06T21:40:24.4843425Z 🚨 #120: архив сессии не удался "
            "(возможность сломана): HTTP Error 500: Internal Server Error\n"
            "2026-09-06T21:40:24.4937977Z ##[error]Process completed with exit code 1.\n"
            "2026-09-06T21:40:24.5134867Z Post job cleanup.\n"
            f"2026-09-06T21:40:24.7336136Z Removing credentials config '/home/runner/work/_temp/git-credentials-{uuid}.config'\n"
            "2026-09-06T21:40:24.7482979Z Cleaning up orphan processes\n"
        )
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log_with_uuid("8a8eb9a3-bd3f-4668-8fdb-b30a4b91f216"))))
    line1 = pg.last_error_log_line("mytab0r/edge-harness", 1)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log_with_uuid("f42e2338-f7cb-4e6f-8022-328e66ce91b0"))))
    line2 = pg.last_error_log_line("mytab0r/edge-harness", 2)
    assert line1 == line2
    fp1 = pg.failure_fingerprint("orchestra.yml", "orchestra", line1)
    fp2 = pg.failure_fingerprint("orchestra.yml", "orchestra", line2)
    assert fp1 == fp2


def test_last_error_log_line_strips_ansi_escapes_from_fact(monkeypatch):
    # Факт уходит в тело задачи и след #120/Telegram — управляющие коды сырого
    # лога не должны попадать в текст сигнала.
    log = (
        "\x1b[31m2026-09-06T10:17:58.0000000Z ##[error]scripts/worker/task.sh: "
        "infra_digest.sh: No such file or directory\x1b[0m\n"
        "2026-09-06T10:18:00.0000000Z ##[error]Process completed with exit code 1.\n"
    )
    monkeypatch.setattr(
        pg, "subprocess", SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=log)))
    line = pg.last_error_log_line("mytab0r/edge-harness", 999)
    assert line is not None and "infra_digest.sh" in line
    assert "\x1b" not in line


def test_failure_watch_quiet_when_no_failed_runs(monkeypatch):
    fake = FakeGh(dict(FAILURE_WATCH_QUIET_ROUTES))
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert observations == [] and actions == []


def test_failure_watch_defect_files_task_once_then_dedupes(monkeypatch):
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34027035455),
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []  # пока пусто — класса ещё нет
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert len(created) == 1
    joined = " ".join(created[0])
    assert "labels[]=task" in joined and "labels[]=ci-failure" in joined
    assert "infra_digest.sh" in joined
    assert any("заведена задача" in line for line in actions)

    # второй пульс: тот же класс уже в открытых issues (метка ci-failure) —
    # не заводим вторую задачу на тот же баг.
    fp = pg.failure_fingerprint("worker.yml", "task",
                                 "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")
    routes["issues?state=open&labels=ci-failure"] = [
        {"body": f"...<!-- failure-fingerprint: {fp} -->\n"},
    ]
    observations2, actions2 = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions2 == []
    assert any("не дублируем" in line for line in observations2)
    assert len(created) == 1  # вторая задача не заведена


def test_failure_watch_same_title_different_fingerprint_comments_not_duplicates(monkeypatch):
    # Мутационная проверка класса #610 (живой случай #578/#580/#589/#592/#598):
    # заголовок failure_watch константный по (workflow, job_name) — «CI:
    # orchestra.yml падает — orchestra» для ЛЮБОГО факта этого job'а. Открытая
    # issue того же заголовка, но с ДРУГИМ fingerprint в теле (например,
    # прошлый прогон поймал другой вариант того же боилерплейта) обязана
    # остановить создание ВТОРОЙ issue — комментарий на существующую, не дубль.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/orchestra.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34063041667, event="schedule"),
    ]}
    routes["runs/34063041667/jobs"] = {"jobs": [
        {"id": 1, "name": "orchestra", "conclusion": "failure", "steps": [
            {"name": "Обход пула и очередь слияний", "conclusion": "failure"},
        ]},
    ]}
    # Реальные UUID двух живых дублей (#578/#589) — сегменты короче 6 hex
    # символов (например «bd3f», «4668») переживают нормализацию, поэтому эти
    # два факта дают РАЗНЫЙ fingerprint при ОДИНАКОВОМ заголовке (та же
    # причина, что и у живого случая — регресс без обрезки #578/#589 остался бы
    # зелёным по этому тесту, если бы совпал и fingerprint).
    existing_fp = pg.failure_fingerprint(
        "orchestra.yml", "orchestra",
        "Removing credentials config '/home/runner/work/_temp/git-credentials-8a8eb9a3-bd3f-4668-8fdb-b30a4b91f216.config'")
    routes["issues?state=open&labels=ci-failure"] = [
        {"number": 578, "title": "CI: orchestra.yml падает — orchestra",
         "html_url": "https://github.com/mytab0r/edge-harness/issues/578",
         "body": f"...<!-- failure-fingerprint: {existing_fp} -->\n"},
    ]
    routes["issues/578/comments"] = []  # маркера этого класса на #578 ещё нет
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "Removing credentials config '/home/runner/work/_temp/git-credentials-f42e2338-f7cb-4e6f-8022-328e66ce91b0.config'")))
    new_fp = pg.failure_fingerprint(
        "orchestra.yml", "orchestra",
        "Removing credentials config '/home/runner/work/_temp/git-credentials-f42e2338-f7cb-4e6f-8022-328e66ce91b0.config'")
    assert new_fp != existing_fp  # предпосылка теста: РАЗНЫЙ fingerprint, ОДИНАКОВЫЙ заголовок
    created = []
    commented = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: commented.append((n, text)))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert created == []  # НЕ заведена вторая issue с тем же заголовком
    assert actions == []  # это не «новая задача», см. #456
    assert len(commented) == 1 and commented[0][0] == 578
    assert any("тем же заголовком" in line and "#578" in line for line in observations)


def test_failure_watch_repeat_pulse_of_same_run_does_not_double_comment(monkeypatch):
    # Мутационная проверка (находка ревью PR #612, живой прогон 34063041667 —
    # реальные id job'а/шага и реальный хвост лога сняты `gh api` с прод): окно
    # свежести FAILURE_WATCH_WINDOW_MINUTES=30 при пульсе раз в 15 мин держит
    # ОДИН И ТОТ ЖЕ красный прогон «свежим» до трёх пульсов подряд. Без дедупа
    # по комментариям (issue_marker_times — приём stall_detector.py, #248)
    # каждый такой пульс писал бы БАЙТ-В-БАЙТ дубль на #578: тело issue не
    # меняется (ci_failure_fingerprints парсит только тела), значит только
    # чтение уже оставленных КОММЕНТАРИЕВ этой issue отличает «уже сигналили»
    # от «новый класс причины».
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/orchestra.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-09-06T22:08:03Z", 34063041667, event="schedule"),
    ]}
    # Реальные id job'а и шага прогона 34063041667 (`gh api
    # repos/mytab0r/edge-harness/actions/runs/34063041667/jobs`).
    routes["runs/34063041667/jobs"] = {"jobs": [
        {"id": 101566876955, "name": "orchestra", "conclusion": "failure", "steps": [
            {"name": "Обход пула и очередь слияний", "conclusion": "failure"},
        ]},
    ]}
    existing_fp = pg.failure_fingerprint(
        "orchestra.yml", "orchestra",
        "Removing credentials config '/home/runner/work/_temp/git-credentials-8a8eb9a3-bd3f-4668-8fdb-b30a4b91f216.config'")
    routes["issues?state=open&labels=ci-failure"] = [
        {"number": 578, "title": "CI: orchestra.yml падает — orchestra",
         "html_url": "https://github.com/mytab0r/edge-harness/issues/578",
         "body": f"...<!-- failure-fingerprint: {existing_fp} -->\n"},
    ]
    # Реальный хвост лога job'а 101566876955 (`gh api .../jobs/101566876955/logs`,
    # снят 2026-09-08): «Removing credentials config '<UUID>.config'» —
    # ровно боилерплейт teardown checkout, живая причина класса #578.
    same_run_error = (
        "Removing credentials config '/home/runner/work/_temp/"
        "git-credentials-f42e2338-f7cb-4e6f-8022-328e66ce91b0.config'")
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(same_run_error)))
    same_run_fp = pg.failure_fingerprint("orchestra.yml", "orchestra", same_run_error)
    assert same_run_fp != existing_fp  # предпосылка: класс новый для тела #578

    fake = FakeGh(routes)
    posted_comments: list[dict] = []  # эмулирует РЕАЛЬНУЮ issue #578 — комментарии копятся между пульсами

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues/578/comments":
            body = args[-1].split("=", 1)[1]
            posted_comments.append({"created_at": "2026-09-06T22:10:00Z", "body": body})
            return None
        joined = " ".join(args)
        if "issues/578/comments" in joined and args[:2] != ("-X", "POST"):
            return list(posted_comments)
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    # (б) Пульс 1: комментарий пишется (маркера этого класса на #578 ещё нет).
    observations1, actions1 = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions1 == []
    assert len(posted_comments) == 1
    assert any("уже открыта (#578)" in line for line in observations1)
    # (г) текст не врёт: не называет прогон новым (он тот же, что и всегда был).
    assert "Новый прогон" not in posted_comments[0]["body"]

    # (а) Пульс 2 — ТОТ ЖЕ прогон и факт (следующий тик оркестратора, тот же
    # прогон всё ещё «свежий» в окне FAILURE_WATCH_WINDOW_MINUTES): комментарий
    # НЕ дублируется.
    observations2, actions2 = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions2 == []
    assert len(posted_comments) == 1  # не выросло — снят фикс, здесь стало бы 2
    assert any("уже прокомментирована" in line and "молчу" in line for line in observations2)

    # (в) Пульс 3 — ДРУГОЙ прогон/причина (другой отпечаток факта под тем же
    # заголовком): дедуп не склеивает разные причины — комментарий пишется.
    different_run_error = "HTTP Error 500: Internal Server Error (archive session, #119/#575)"
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(different_run_error)))
    different_fp = pg.failure_fingerprint("orchestra.yml", "orchestra", different_run_error)
    assert different_fp not in (existing_fp, same_run_fp)
    observations3, actions3 = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions3 == []
    assert len(posted_comments) == 2  # новый класс — второй, НЕ дублирующий комментарий
    assert any("уже открыта (#578)" in line for line in observations3)
    assert "Новый прогон" not in posted_comments[1]["body"]


# ── Суточный потолок автозаведения ci-failure (FAILURE_WATCH_DAILY_CAP) ──────────


def _ci_failure_issue(number: int, created_at: str) -> dict:
    return {
        "number": number,
        "title": f"CI: worker.yml падает — job-{number}",
        "created_at": created_at,
        "body": f"...<!-- failure-fingerprint: whatever-{number} -->\n",
    }


def test_failure_watch_daily_cap_blocks_new_class_signals_once_and_lists_skipped(monkeypatch):
    # Мутационная проверка потолка: квота дня уже сожжена (FAILURE_WATCH_DAILY_CAP
    # задач за последние 24ч) — новый, ещё НЕ заведённый класс НЕ становится
    # задачей, а видимый сигнал (комментарий #120 + Telegram) уходит РОВНО один
    # раз, при этом отсечённый класс всё равно перечислен (тихий след-комментарий).
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34027035455),
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []  # класса ещё нет в пуле
    routes["issues?state=all&labels=ci-failure"] = [
        _ci_failure_issue(100 + i, "2026-08-31T06:00:00Z")  # < 24ч до NOW
        for i in range(pg.FAILURE_WATCH_DAILY_CAP)
    ]
    routes["issues/120/comments"] = []  # ни один маркер сегодня ещё не стоит
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)
    posted = []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: True)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)

    assert created == []  # потолок исчерпан — новая задача НЕ заведена
    fp = pg.failure_fingerprint(
        "worker.yml", "task",
        "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")
    assert any("потолок" in line and "исчерпан" in line and fp in line for line in observations)
    # Ровно один видимый сигнал (эскалация #120+Telegram) — не второй канал,
    # тот же escalate(), что и остальные тревоги этого модуля.
    escalation_posts = [text for _, text in posted if pg.FAILURE_WATCH_CAP_MARKER in text]
    assert len(escalation_posts) == 1
    assert NOW.date().isoformat() in escalation_posts[0]
    assert fp in escalation_posts[0]  # отсечённый класс перечислен в самом сигнале
    assert any("эскалация потолка" in line for line in actions)
    # Плюс тихий след-комментарий с отпечатком отсечённого класса — не теряется
    # (отличаем от эскалации: та тоже упоминает префикс маркера в тексте «что
    # дальше», поэтому фильтруем именно комментарии БЕЗ FAILURE_WATCH_CAP_MARKER).
    skip_posts = [
        text for _, text in posted
        if pg.FAILURE_WATCH_CAP_SKIP_MARKER_PREFIX in text and pg.FAILURE_WATCH_CAP_MARKER not in text
    ]
    assert len(skip_posts) == 1 and fp in skip_posts[0]


def test_failure_watch_daily_cap_escalation_deduped_once_per_day(monkeypatch):
    # Второй пульс того же дня, потолок всё ещё исчерпан, класс тот же самый:
    # ни повторной эскалации (#120+Telegram), ни повторного тихого следа —
    # маркеры обоих уже стоят. Наблюдение о потолке при этом печатается каждый
    # пульс (видно в GITHUB_STEP_SUMMARY), это не то же самое, что тревога.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34027035455),
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    routes["issues?state=all&labels=ci-failure"] = [
        _ci_failure_issue(100 + i, "2026-08-31T06:00:00Z")
        for i in range(pg.FAILURE_WATCH_DAILY_CAP)
    ]
    fp = pg.failure_fingerprint(
        "worker.yml", "task",
        "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")
    cap_marker = f"{pg.FAILURE_WATCH_CAP_MARKER} {NOW.date().isoformat()}]"
    skip_marker = f"{pg.FAILURE_WATCH_CAP_SKIP_MARKER_PREFIX} {NOW.date().isoformat()} {fp}]"
    routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:00:00Z", "body": f"🚨 {cap_marker}\n..."},
        {"created_at": "2026-08-31T11:00:01Z", "body": f"{skip_marker}\n..."},
    ]
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("маркер уже стоит — повтор не пишем"))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: pytest.fail("эскалация уже была сегодня"))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)

    assert any("потолок" in line and "исчерпан" in line for line in observations)
    assert not any("эскалация потолка" in line for line in actions)


def test_failure_watch_daily_cap_never_raises(monkeypatch):
    # Прогон обязан остаться зелёным: исчерпание потолка — сигнал «нужен
    # человек», не отказ (см. докстринг FAILURE_WATCH_CAP_MARKER). Отправка в
    # Telegram/комментарий может упасть (сеть) — failure_watch не поднимает
    # исключение наверх ни при одном исходе.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34027035455),
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    routes["issues?state=all&labels=ci-failure"] = [
        _ci_failure_issue(100 + i, "2026-08-31T06:00:00Z")
        for i in range(pg.FAILURE_WATCH_DAILY_CAP)
    ]
    routes["issues/120/comments"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: False)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)  # не бросает исключение
    assert any("потолок" in line and "исчерпан" in line for line in observations)


def test_failure_watch_title_dedup_not_blocked_by_exhausted_daily_cap(monkeypatch):
    # Мутационная проверка порядка проверок при ребейзе поверх #610/PR #612
    # (комментарий по маркеру среди комментариев issue, не создание задачи):
    # суточный потолок ci-failure УЖЕ исчерпан (5/5 за последние 24ч, реальная
    # форма ответа gh api — те же поля, что и в остальных тестах потолка),
    # но у нового класса причины УЖЕ есть открытая issue с тем же
    # детерминированным заголовком (workflow+job_name, #578 — живой случай
    # #578/#580/#589/#592/#598). Комментарий на существующую issue НЕ заводит
    # задачу — сжигать суточную квоту тут нечего, а значит потолок не вправе
    # его блокировать. До этой правки (потолок проверялся ДО дедупа по
    # заголовку) один и тот же класс тут получал бы ложное «потолок исчерпан»
    # вместо тихого комментария на #578, хотя пул при этом не растёт вовсе.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/orchestra.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-09-06T22:08:03Z", 34063041667, event="schedule"),
    ]}
    # Реальные id job'а и шага прогона 34063041667 (`gh api
    # repos/mytab0r/edge-harness/actions/runs/34063041667/jobs`), как и в
    # test_failure_watch_repeat_pulse_of_same_run_does_not_double_comment.
    routes["runs/34063041667/jobs"] = {"jobs": [
        {"id": 101566876955, "name": "orchestra", "conclusion": "failure", "steps": [
            {"name": "Обход пула и очередь слияний", "conclusion": "failure"},
        ]},
    ]}
    existing_fp = pg.failure_fingerprint(
        "orchestra.yml", "orchestra",
        "Removing credentials config '/home/runner/work/_temp/git-credentials-8a8eb9a3-bd3f-4668-8fdb-b30a4b91f216.config'")
    routes["issues?state=open&labels=ci-failure"] = [
        {"number": 578, "title": "CI: orchestra.yml падает — orchestra",
         "html_url": "https://github.com/mytab0r/edge-harness/issues/578",
         "body": f"...<!-- failure-fingerprint: {existing_fp} -->\n"},
    ]
    # Потолок дня уже сожжён (state=all, реальная форма ответа gh api — та же,
    # что и у остальных тестов потолка выше).
    routes["issues?state=all&labels=ci-failure"] = [
        _ci_failure_issue(200 + i, "2026-09-06T06:00:00Z")
        for i in range(pg.FAILURE_WATCH_DAILY_CAP)
    ]
    routes["issues/578/comments"] = []  # маркера этого класса на #578 ещё нет
    # Реальный хвост лога job'а 101566876955 (`gh api .../jobs/101566876955/logs`,
    # снят 2026-09-08): «Removing credentials config '<UUID>.config'» — тот же
    # боилерплейт teardown, что и в соседних тестах, другой UUID → новый класс.
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "Removing credentials config '/home/runner/work/_temp/git-credentials-f42e2338-f7cb-4e6f-8022-328e66ce91b0.config'")))
    new_fp = pg.failure_fingerprint(
        "orchestra.yml", "orchestra",
        "Removing credentials config '/home/runner/work/_temp/git-credentials-f42e2338-f7cb-4e6f-8022-328e66ce91b0.config'")
    assert new_fp != existing_fp  # предпосылка: РАЗНЫЙ fingerprint, ОДИНАКОВЫЙ заголовок

    fake = FakeGh(routes)
    created = []
    commented = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: commented.append((n, text)))
    monkeypatch.setattr(pg, "send_telegram", lambda *a, **k: pytest.fail("потолок не исчерпан для этого класса — эскалации быть не должно"))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert created == []  # новая issue не заведена (ни потолком, ни дублем)
    assert len(commented) == 1 and commented[0][0] == 578  # комментарий на существующую issue
    assert not any("исчерпан" in line for line in observations)  # потолок тут не при чём
    assert any("тем же заголовком" in line and "#578" in line for line in observations)


def test_failure_watch_infra_cause_is_silent_after_first_marker(monkeypatch):
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/hands.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 7),
    ]}
    routes["runs/7/jobs"] = {"jobs": [
        {"id": 1, "name": "dsh-task", "conclusion": "failure", "steps": [
            {"name": "Прогон задачи через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues/120/comments"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "dsh: RATE_LIMIT: Rate limit reached for requests")))
    posted = []
    monkeypatch.setattr(pg, "post_issue_comment", lambda repo, n, text: posted.append(text))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert len(posted) == 1
    assert pg.FAILURE_WATCH_INFRA_MARKER in posted[0]
    assert actions == []  # инфраструктура — наблюдение, не действие пула

    # второй пульс: маркер уже стоит — молчим, не спамим
    fp = pg.failure_fingerprint("hands.yml", "dsh-task", "dsh: RATE_LIMIT: Rate limit reached for requests")
    routes["issues/120/comments"] = [
        {"created_at": "2026-08-31T11:55:00Z",
         "body": f"...{pg.FAILURE_WATCH_INFRA_MARKER} {fp}]..."},
    ]
    posted.clear()
    observations2, _ = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert posted == []
    assert any("уже сигналили" in line for line in observations2)


def test_failure_watch_ignores_run_older_than_freshness_window(monkeypatch):
    # Находка ревью PR #488: провал старше окна не заводит задачу заново на
    # уже неактуальную причину — `now` обязан использоваться, не просто
    # приниматься в сигнатуру. Прогон на 4 дня старше NOW заведомо за окном
    # FAILURE_WATCH_WINDOW_MINUTES (30 мин).
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-27T11:50:00Z", 34027035455,
            updated_at="2026-08-27T11:55:00Z"),
    ]}

    def boom(*a):
        pytest.fail("прогон вне окна свежести не должен запрашивать детали job'ов")

    monkeypatch.setattr(pg, "gh", FakeGh(routes))
    monkeypatch.setattr(pg, "failing_jobs", boom)
    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert observations == [] and actions == []


def test_failure_watch_window_anchor_is_failure_moment_not_queue_time(monkeypatch):
    # Находка ревью PR #488 (раунд 3, блокирующая): created_at у GitHub —
    # момент ПОСТАНОВКИ В ОЧЕРЕДЬ, не провала. Воркер по замыслу пашет десятки
    # минут (worker.yml — timeout-minutes: 280): прогон, поставленный в
    # очередь 200 минут назад и УПАВШИЙ 10 минут назад, по created_at лежал бы
    # вне окна на каждом пульсе — ни задачи, ни наблюдения, навсегда. Якорь
    # окна — updated_at (у завершённого красного прогона это момент провала).
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T08:40:00Z", 34027035455,  # очередь: 200 мин до NOW
            updated_at="2026-08-31T11:50:00Z"),              # провал: 10 мин до NOW
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert len(created) == 1, (
        "долгий прогон, упавший внутри окна, обязан разбираться — "
        "якорь окна created_at хоронит провалы долгих воркеров")
    assert any("заведена задача" in line for line in actions)


def test_failure_watch_treats_timed_out_as_failure_and_ignores_cancelled(monkeypatch):
    # Находка ревью PR #488 (раунд 3, блокирующая): серверный фильтр
    # status=failure не возвращает timed_out, а прогон, убитый собственным
    # капом (timeout-minutes: worker.yml = 280), — провал в смысле #477.
    # Опрос идёт по status=completed с клиентским фильтром
    # FAILURE_WATCH_RUN_CONCLUSIONS; cancelled там НЕТ — отмена это
    # осознанное действие, не сигнал о дефекте/инфраструктуре.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        # Прод-форма: список от нового к старому — отменённый прогон СТОИТ
        # ВПЕРЕДИ провального. Без клиентского фильтра вывода runs[0] — это
        # отменённый прогон, и разбор ушёл бы на него (мутация: снятие
        # фильтра красит этот тест — FakeGh громко требует маршрут jobs
        # для прогона 222, которого быть не должно).
        run("cancelled", "2026-08-31T11:52:00Z", 222,
            updated_at="2026-08-31T11:53:00Z"),
        run("timed_out", "2026-08-31T11:50:00Z", 111,
            updated_at="2026-08-31T11:51:00Z"),
    ]}
    # Маршрут jobs заведён ТОЛЬКО для timed_out-прогона 111: разбор
    # отменённого 222 уронил бы FakeGh AssertionError — громко, не молча.
    routes["runs/111/jobs"] = {"jobs": [
        {"id": 111, "name": "task", "conclusion": "timed_out", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory")))
    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert len(created) == 1  # задача — по timed_out-прогону; cancelled не разбирается
    assert "actions/runs/111" in " ".join(created[0])
    assert "actions/runs/222" not in " ".join(created[0])


def test_failure_watch_task_body_names_failed_steps(monkeypatch):
    # Чеклист ревью PR #488 (раунд 3): критерий #477 называет «шаг + последняя
    # ##[error]-строка» — тело заведённой задачи обязано нести упавшие шаги
    # job'а (имена уже прочитаны из job'а, второй запрос не нужен).
    body = pg.failure_watch_task_body(
        "worker.yml", "task",
        "##[error]scripts/worker/task.sh: line 375: .../infra_digest.sh: No such file or directory",
        "https://github.com/mytab0r/edge-harness/actions/runs/1",
        "ce6fa14c0089",
        "Задача через DSH headless")
    assert "Упавшие шаги job'а «task»: Задача через DSH headless" in body
    assert "Факт: ##[error]scripts/worker/task.sh" in body


def test_failure_watch_stale_base_neither_files_task_nor_signals(monkeypatch):
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 8),
    ]}
    routes["runs/8/jobs"] = {"jobs": [
        {"id": 2, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Ветка: новая от свежего origin/main", "conclusion": "failure"},
        ]},
    ]}
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "ОШИБКА: main уехал вперёд (оркестратор слил PR-ы) — base протух.")))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать — газ уже назван в #474"))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions == []
    assert any("устаревшая база" in line for line in observations)
    assert any("#474" in line for line in observations)


def test_failure_watch_skips_pull_request_runs_their_class_belongs_to_stall_detector(monkeypatch):
    # Находка ревью PR #488 (раунд 2, блокирующая): красный прогон PR-события —
    # это красный обязательный чек на PR; устойчивую причину того же класса
    # уже заводит автодетектор простоя (#201, отпечаток check:red:<имя>).
    # Вторая задача с меткой ci-failure на тот же дефект — тот спам, который
    # запрещает критерий #477. Прод-форма: PR-триггер среди отслеживаемых
    # workflow есть только у orchestra.yml (job contract), свежий красный
    # pull_request-прогон стоит в одной странице ПЕРЕД свежим schedule-прогоном.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/orchestra.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:55:00Z", 555, event="pull_request"),
        run("failure", "2026-08-31T11:50:00Z", 34027035455, event="schedule"),
    ]}
    # Маршрут jobs заведён ТОЛЬКО для schedule-прогона: разбор PR-прогона 555
    # уронил бы FakeGh AssertionError («нет маршрута») — громко, не молча.
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "orchestra", "conclusion": "failure", "steps": [
            {"name": "Планировщик", "conclusion": "failure"},
        ]},
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "##[error]AssertionError: пул пуст, а задача назначена")))
    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 999}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert len(created) == 1  # задача — по schedule-прогону, не по PR-прогону
    assert any("orchestra.yml" in line for line in actions)
    # В теле задачи — ссылка на schedule-прогон 34027035455, не на PR-прогон 555.
    assert "actions/runs/34027035455" in " ".join(created[0])
    assert "actions/runs/555" not in " ".join(created[0])


def test_failure_watch_infra_reports_trace_not_posted_when_comment_fails(monkeypatch):
    # Находка ревью PR #488 (раунд 2, блокирующая; тот же класс, что
    # heartbeat_check после PR #318): упавший post_issue_comment уходит в
    # warning — и отчёт не имеет права утверждать «след в #120 оставлен».
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/hands.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 7),
    ]}
    routes["runs/7/jobs"] = {"jobs": [
        {"id": 1, "name": "dsh-task", "conclusion": "failure", "steps": [
            {"name": "Прогон задачи через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    routes["issues/120/comments"] = []
    monkeypatch.setattr(pg, "gh", FakeGh(routes))
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "dsh: RATE_LIMIT: Rate limit reached for requests")))

    def broken_post(*a):
        raise RuntimeError("gh api: 502")
    monkeypatch.setattr(pg, "post_issue_comment", broken_post)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions == []  # инфраструктура — наблюдение, не действие пула
    line = next(l for l in observations if "инфраструктурная причина" in l)
    assert "НЕ оставлен" in line


def test_failure_watch_no_task_without_error_line(monkeypatch):
    # Чеклист ревью PR #488: фолбэк-факт «шаги: …» грубее отпечатка с
    # настоящей строкой — задача по нему мигает во вторую, когда лог на
    # следующем пульсе прочитается. Без строки ##[error] задачи нет — только
    # громкое наблюдение; устойчивый случай возьмёт автодетектор (#201, warn:).
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 34027035455),
    ]}
    routes["runs/34027035455/jobs"] = {"jobs": [
        {"id": 999, "name": "task", "conclusion": "failure", "steps": [
            {"name": "Задача через DSH headless", "conclusion": "failure"},
        ]},
    ]}
    # Маршрутов issues НЕТ нарочно: попытка дедупа/создания задачи уронило бы
    # FakeGh AssertionError — заведение задачи без факта красит тест громко.
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    # Лог содержит только boilerplate раннера — содержательной строки нет.
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "##[error]Process completed with exit code 1.")))

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions == []
    line = next(l for l in observations if "задачу не" in l and "заводим" in l)
    assert "Задача через DSH headless" in line


def test_failure_watch_parses_jobs_up_to_cap_and_names_the_rest(monkeypatch):
    # Чеклист ревью PR #488: разбираются ВСЕ упавшие job'ы (до потолка
    # FAILURE_WATCH_MAX_JOBS_PER_RUN логов на прогон), хвост назван поимённо —
    # не прячется молча (тот же класс «не прятать хвост», что #308).
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/hands.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 7),
    ]}
    routes["runs/7/jobs"] = {"jobs": [
        {"id": 1 + i, "name": f"job-{i}", "conclusion": "failure", "steps": [
            {"name": "шаг", "conclusion": "failure"},
        ]}
        for i in range(pg.FAILURE_WATCH_MAX_JOBS_PER_RUN + 1)
    ]}
    routes["issues?state=open&labels=ci-failure"] = []
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "subprocess",
        SimpleNamespace(run=lambda *a, **k: _stdout_with_error(
            "##[error]AssertionError: красный тест")))

    created = []

    def fake_gh_dispatch(*args):
        if args[:2] == ("-X", "POST") and args[2] == "repos/mytab0r/edge-harness/issues":
            created.append(args)
            return {"number": 900 + len(created)}
        return fake(*args)
    monkeypatch.setattr(pg, "gh", fake_gh_dispatch)

    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    # Разные job'ы — разные классы: по задаче на каждый разобранный job.
    assert len(created) == pg.FAILURE_WATCH_MAX_JOBS_PER_RUN
    assert len(actions) == pg.FAILURE_WATCH_MAX_JOBS_PER_RUN
    hidden_line = next(l for l in observations if "не разобраны" in l)
    assert f"job-{pg.FAILURE_WATCH_MAX_JOBS_PER_RUN}" in hidden_line


def test_failure_watch_single_page_100_for_every_watched_workflow(monkeypatch):
    # Находка ревью PR #488 (раунды 4–5): страница считается безотносительно
    # окна, а провал с самым старым created_at — timed_out долгого воркера
    # (кап 280 мин: создан ЗА ЧАСЫ до провала) — вытеснялся бы за страницу 20
    # молча. Размер ЕДИНЫЙ для всех отслеживаемых (FAILURE_WATCH_PER_PAGE=100,
    # прецедент — heartbeat_check), тот же ОДИН запрос на workflow.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    pg.failure_watch("mytab0r/edge-harness", NOW)
    poll_calls = [url for url in fake.calls if "runs?status=completed" in url]
    assert len(poll_calls) == len(pg.WATCHED_WORKFLOWS)  # по одному на workflow
    for url in poll_calls:
        assert "per_page=100" in url, url


def test_failure_watch_jobs_request_failure_is_named_observation(monkeypatch):
    # Свод источников (failing_jobs) сделал сбой запроса громким: «job'ы не
    # прочитаны» и «упавших job'ов нет» — разные факты, разные наблюдения;
    # сбой одного workflow не роняет обход остальных и сам пульс.
    routes = dict(FAILURE_WATCH_QUIET_ROUTES)
    routes["workflows/worker.yml/runs?status=completed"] = {"workflow_runs": [
        run("failure", "2026-08-31T11:50:00Z", 31337,
            updated_at="2026-08-31T11:51:00Z"),
    ]}
    # Маршрута runs/31337/jobs НЕТ: FakeGh бросает AssertionError, оборачиваем
    # в RuntimeError — прод-форма сетевого сбоя gh().
    fake = FakeGh(routes)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(
        pg, "failing_jobs",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh api: 502 (почти как в проде)")))
    monkeypatch.setattr(pg, "post_issue_comment", lambda *a: pytest.fail("не должен писать"))
    observations, actions = pg.failure_watch("mytab0r/edge-harness", NOW)
    assert actions == []
    line = next(l for l in observations if "не прочитаны" in l)
    assert "worker.yml" in line and "502" in line


def test_stale_base_signatures_no_dead_strings():
    # Каждая сигнатура обязанa быть достижимой в реальном логе: «checks
    # awaiting conflict resolution» была удалена ревью PR #488 (раунд 2) —
    # на конфликтном PR проверки не запускаются вовсе, строки в логе нет.
    # Гвардия против возврата мёртвых сигнатур: новая добавляется только с
    # указанием живого источника строки.
    assert all(
        "awaiting conflict" not in signature
        for signature in pg.STALE_BASE_SIGNATURES
    )


def test_issue_marker_times_from_comments_same_semantics_no_refetch():
    """Чистая половина issue_marker_times (находка ревью PR #883): фильтрует
    УЖЕ скачанные комментарии с ТОЙ ЖЕ семантикой совпадения (_marker_present,
    включая границу «#16 не совпадает внутри #163»), без единого сетевого
    вызова — читатель, которому один issue нужен по нескольким маркерам,
    вычитывает его один раз и фильтрует локально."""
    comments = [
        {"created_at": "2026-09-06T10:00:00Z",
         "body": "🚨 edge-harness: [conflict: эскалация] #701\nтот самый PR"},
        {"created_at": "2026-09-06T11:00:00Z",
         "body": "🚨 edge-harness: [conflict: эскалация] #70\nчужой номер — голая подстрока нашла бы"},
        {"created_at": "2026-09-06T12:00:00Z", "body": "обычный комментарий"},
    ]
    times = pg.issue_marker_times_from_comments(comments, "[conflict: эскалация] #701")
    assert times == [utc(2026, 9, 6, 10, 0)]
