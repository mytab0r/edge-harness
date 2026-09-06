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


def run(conclusion, created_at="2026-08-31T10:00:00Z", run_id=1, title="worker run", event="workflow_dispatch"):
    """Прод-форма элемента workflow_runs (поля, которые читает модуль).
    event по умолчанию — workflow_dispatch (реальный тик), чтобы существующие
    тесты, не заботящиеся о фильтре real_orchestra_ticks, не начали молчать."""
    return {
        "id": run_id,
        "conclusion": conclusion,
        "created_at": created_at,
        "html_url": f"https://github.com/mytab0r/edge-harness/actions/runs/{run_id}",
        "display_title": title,
        "event": event,
    }


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
    calls = []
    monkeypatch.setattr(pg, "subprocess",
                        SimpleNamespace(run=lambda *a, **k: calls.append(a) or SimpleNamespace(
                            returncode=0, stderr="")))
    assert pg.send_telegram("обычный алерт") is True
    assert "reply_markup" not in " ".join(calls[0][0])


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
