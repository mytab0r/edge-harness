#!/usr/bin/env python3
"""Тесты дедуп-эскалации и автозаведения задачи на переходе через порог
квоты (scripts/measure/quota_alert.py, #605).

Сетевые вызовы (pulse_guard.gh/escalate/post_issue_comment, subprocess.run
для scripts/gh/issue-create) подменяются monkeypatch — тот же приём, что
scripts/orchestra/test_pulse_guard.py/scripts/measure/test_quotas.py.

Запуск: python -m pytest scripts/measure/test_quota_alert.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quota_alert.py")
spec = importlib.util.spec_from_file_location("quota_alert", SCRIPT)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"


def _comment(body: str, created_at: str = "2026-09-06T18:00:00Z") -> dict:
    return {"body": body, "created_at": created_at}


# ── last_state: разбор маркера ────────────────────────────────────────────


def test_last_state_none_when_no_marker(monkeypatch):
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)


def test_last_state_reads_only_fresh_page_of_watchdog_history(monkeypatch):
    """Тот же «хвост», что PR закрыл для stale-маркеров простоя (found: ревью
    PR #607, некритичное замечание): дедуп состояния ресурса тикает на каждый
    прогон сторожа и читает только СВЕЖУЮ страницу #120 (551+ комментариев и
    растёт). Гвардия: запрос страницы 2 — громкое падение. Сними
    `max_pages=` из last_state — тест краснеет."""
    requested = []

    def fake_gh(*args):
        endpoint = args[0]
        assert isinstance(endpoint, str) and "/comments?" in endpoint, endpoint
        page = int(endpoint.split("&page=")[1])
        requested.append(page)
        assert page == 1, f"дедуп состояния читает только свежую страницу, запрошена {page}"
        # Полная страница БЕЗ маркеров состояния — дедуп честно отвечает
        # «состояние не известно», не обходя всю историю задачи.
        return [{"id": i, "body": f"comment {i}", "created_at": "2026-09-10T00:00:00Z"}
                for i in range(100)]

    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)
    assert requested == [1]


def test_last_state_reads_breach_with_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "breach", 999)
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [_comment(f"текст\n{marker}")])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("breach", 999)


def test_last_state_reads_ok_without_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "ok", None)
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [_comment(f"текст\n{marker}")])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_picks_the_latest_marker_not_the_first(monkeypatch):
    """Два маркера одного ресурса — состояние решает САМЫЙ СВЕЖИЙ по времени,
    не первый в списке (порядок ответа GitHub не гарантирован хронологией)."""
    older = _comment(qa.state_marker("cf_do_rows_read_day", "breach", 1), "2026-09-06T10:00:00Z")
    newer = _comment(qa.state_marker("cf_do_rows_read_day", "ok", None), "2026-09-06T20:00:00Z")
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [older, newer])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_ignores_marker_of_a_different_resource(monkeypatch):
    """Маркер другого ресурса не должен путаться с искомым — иначе состояние
    одного ресурса решалось бы по эскалации совсем другого (независимость
    дедупа по ресурсам, см. докстринг модуля)."""
    other = _comment(qa.state_marker("cf_workers_requests_day", "breach", 1))
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: [other])
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)


# ── check_and_alert: дедуп по переходу ────────────────────────────────────


def _no_prior_state(monkeypatch):
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (None, None))


def test_first_observation_breach_alerts_and_creates_task(monkeypatch):
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (1234, "задача заведена"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append(text) or "Telegram: доставлен; след в #120: оставлен")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_487_640, 5_000_000, 149.8)

    assert len(escalated) == 1
    assert "#1234" in escalated[0]
    assert "breach" in escalated[0].lower() or "перевалила" in escalated[0]
    assert "issue=#1234" in escalated[0]
    assert "breach" in result


def test_mutation_guard_breach_boundary_is_inclusive(monkeypatch):
    """Мутационная проверка (по образцу test_quotas.py): ровно на границе
    порога сигнал обязан сработать (>=, не >)."""
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (1, "ok"))
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: "escalated")
    result = qa.check_and_alert(REPO, "k", "метка", 80, 100, 80.0, threshold=80.0)
    assert "breach" in result


def test_no_change_does_not_alert_again(monkeypatch):
    """Порог держится (breach→breach) — второй алерт НЕ уходит: дедуп по
    переходу, не по каждому прогону."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", 1234))
    calls = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: calls.append(text) or "x")
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (None, "x"))

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_600_000, 5_000_000, 152.0)

    assert calls == []
    assert create_calls == []
    assert "без изменений" in result


def test_recovery_transition_alerts_without_creating_task(monkeypatch):
    """breach→ok: уведомление о восстановлении уходит, но НОВАЯ задача не
    заводится (см. докстринг модуля, «не закрывается автоматически»)."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", 1234))
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (999, "не должно вызываться"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "ok")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert create_calls == []
    assert len(escalated) == 1
    assert "#1234" in escalated[0]
    assert "issue=#1234" in escalated[0]
    assert "восстановилась" in escalated[0].lower() or "вернулась" in escalated[0].lower()
    assert "recovery" in result


def test_recovery_without_prior_issue_number_still_alerts(monkeypatch):
    monkeypatch.setattr(qa, "last_state", lambda repo, key: ("breach", None))
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: "ok")
    result = qa.check_and_alert(REPO, "k", "метка", 10, 100, 10.0)
    assert "recovery" in result


def test_first_observation_already_ok_does_not_send_false_recovery(monkeypatch):
    """Ресурс впервые увиден в норме (prev_state=None, new_state=ok) — это НЕ
    переход breach→ok, эскалации (Telegram + текст «квота вернулась ниже
    порога») быть не должно, только тихий маркер (found: ревью PR #607)."""
    _no_prior_state(monkeypatch)
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append(text))

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert escalated == []
    assert len(posted) == 1
    assert "= ok" in posted[0]
    assert "первое наблюдение" in result


def test_first_observation_marker_write_failure_is_predicate_visible(monkeypatch):
    """БЛОКЕР ревью PR #607 (head 345a64f), второе место: отказ тихой записи
    носителя дедупа обязан быть различим вызывающему ПРЕДИКАТОМ, а не только
    строкой лога — вердикт строит pulse_guard.carrier_write_verdict, строку
    отказа ловит pulse_guard.escalation_dedup_carrier_failed, и
    measure_main краснит по ней прогон. Иначе носитель дедупа мог стоять
    сломанным неограниченно долго при зелёных прогонах (последующий breach
    при сломанном носителе повторял бы Telegram-страницу каждый тик, и ни
    один прогон не говорил бы об этом заранее). Мутация: верни в
    check_and_alert немаркированную строку «маркер НЕ записан: …» — тест
    краснеет."""
    _no_prior_state(monkeypatch)
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    def broken_post(repo, issue, text):
        raise RuntimeError("HTTP 403: Not Have Write Access To Repository")
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", broken_post)

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert escalated == []                                   # тихая ветка: эскалации нет
    assert "первое наблюдение" in result
    assert "HTTP 403" in result                              # причина дословно, не гипотеза
    assert qa.pulse_guard.escalation_dedup_carrier_failed(result) is True
    assert qa.pulse_guard.escalation_channel_failed(result) is False


def test_first_observation_marker_write_success_does_not_trip_predicates(monkeypatch):
    """Здоровая тихая запись — зелёная: предикат носителя дедупа не должен
    принимать успешный вердикт за отказ (иначе каждый первый тик нового
    ресурса красил бы прогон ложным разбором)."""
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", lambda repo, issue, text: None)

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert qa.pulse_guard.escalation_dedup_carrier_failed(result) is False
    assert qa.pulse_guard.escalation_channel_failed(result) is False


def test_lost_evidence_note_is_not_mistaken_for_broken_dedup_carrier(monkeypatch, capsys):
    """Потерянная улика (комментарий в найденную задачу не добавлен) при
    записанном маркере и доставленном Telegram — НЕ отказ носителя дедупа:
    предикат не должен давать ложного красного (маркер записан, повторной
    страницы не будет). Формулировка note нарочно без литералов вердикта
    escalate — литералы рождаются только в pulse_guard (source-гвардия
    test_channel_failed_criterion_single_source). Потеря при этом не должна
    пройти невидимой: улика добавляется ОДИН раз на переход — носитель
    видимости ::warning::-аннотация, а не только строка лога (чеклист ревью
    PR #607, head 345a64f)."""
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task",
                         lambda *a: (1234, "задача #1234 уже открыта, комментарий с уликой не добавлен: сеть"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append(text)
                         or "Telegram: доставлен; след в #120: оставлен")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_487_640, 5_000_000, 149.8)

    assert "комментарий с уликой не добавлен" in result
    assert qa.pulse_guard.escalation_dedup_carrier_failed(result) is False
    assert qa.pulse_guard.escalation_channel_failed(result) is False


def test_lost_evidence_annotation_is_emitted_by_create_or_note_task(monkeypatch, capsys):
    """::warning::-аннотация — носитель видимости потерянной улики вне сырого
    лога (чеклист ревью PR #607, head 345a64f): улика добавляется ОДИН раз
    на переход, маркер уже записан — повторной попытки не будет, потеря не
    должна пройти невидимой; GitHub показывает аннотации в списке аннотаций
    проверки. Мутация: сними print("::warning::улика...") в
    create_or_note_task — тест краснеет."""
    candidates = ("похожие ОТКРЫТЫЕ задачи пула уже есть:\n"
                  "  #77 (score 0.9): Квота харнеса перевалила за 80.0%: DO rows_read/сутки — "
                  "https://github.com/mytab0r/edge-harness/issues/77\n")
    monkeypatch.setattr(qa.subprocess, "run",
                         lambda *a, **k: _FakeResult(1, stderr=candidates))

    def broken_post(repo, issue, text):
        raise RuntimeError("сеть")
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", broken_post)

    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_487_640, 5_000_000, 149.8, 80.0)

    assert number == 77
    assert "комментарий с уликой не добавлен" in note
    err = capsys.readouterr().err
    assert "::warning::" in err and "улика" in err and "#77" in err


def test_breach_without_created_task_does_not_write_state_marker(monkeypatch):
    """create_or_note_task не смог завести/найти задачу (issue_number is None)
    — маркер breach НЕ пишется, иначе следующий прогон увидел бы «без
    изменений» и не повторил бы попытку заведения задачи (silent-wrong,
    found: ревью PR #607)."""
    _no_prior_state(monkeypatch)
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (None, "issue-create отказал: сеть"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                 7_487_640, 5_000_000, 149.8)

    assert len(escalated) == 1
    assert "[quota: состояние" not in escalated[0]
    assert "маркер состояния НЕ записан" in result

    # Следующий прогон при той же метрике должен снова увидеть переход
    # (никакого маркера не было записано, prev_state остаётся None) и
    # повторить попытку заведения задачи.
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (1234, "задача заведена"))
    result2 = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки",
                                  7_600_000, 5_000_000, 152.0)
    assert create_calls == [1]
    assert "breach" in result2


# ── create_or_note_task: проводка на scripts/gh/issue-create ─────────────


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_create_task_parses_issue_number_from_url(monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run",
                         lambda *a, **k: _FakeResult(0, "https://github.com/mytab0r/edge-harness/issues/4242\n"))
    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_487_640, 5_000_000, 149.8, 80.0)
    assert number == 4242
    assert "заведена" in note


def test_create_task_duplicate_guard_comments_existing_instead_of_new(monkeypatch):
    """Гвардия дублей (#566) нашла уже открытую задачу — модуль не считает
    это отказом: комментирует найденную вместо второй задачи того же класса."""
    stderr = (
        "::error::issue-create: похожие ОТКРЫТЫЕ задачи пула уже есть "
        "(класс #566, живой случай #518/#547/#564):\n"
        "  #777 (score 0.91): Квота харнеса перевалила за 80.0%: DO rows_read/сутки — https://...\n"
    )
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _FakeResult(1, "", stderr))
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append((issue, text)))

    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_600_000, 5_000_000, 152.0, 80.0)

    assert number == 777
    assert len(posted) == 1 and posted[0][0] == 777
    assert "уже открыта" in note


def test_create_task_unrelated_failure_reports_none(monkeypatch):
    monkeypatch.setattr(qa.subprocess, "run", lambda *a, **k: _FakeResult(1, "", "::error::issue заводится без метки task"))
    number, note = qa.create_or_note_task(REPO, "x", "k", 1, 2, 50.0, 80.0)
    assert number is None
    assert "отказал" in note


def test_create_task_other_resource_candidates_confirmed_not_duplicate(monkeypatch):
    """Блокер-замечание ревью PR #607 («гвардия дублей смешивает ресурсы
    квот»): похожие кандидаты — задачи ПРО ДРУГОЙ ресурс (storage вместо
    rows_read, jaccard шаблонных заголовков 0.38–0.50 при пороге 0.3).
    Улика чужой задачей не глотается: issue-create вызывается ПОВТОРНО с
    --confirm-not-duplicate, своя задача заводится. Мутация: верни прежнее
    «первый кандидат = получатель улики» — тест краснеет (второго вызова
    нет, задача не заведена)."""
    storage_stderr = (
        "::error::issue-create: похожие ОТКРЫТЫЕ задачи пула уже есть "
        "(класс #566, живой случай #518/#547/#564):\n"
        "  #778 (score 0.44): Квота харнеса перевалила за 80.0%: DO storage/аккаунт — https://...\n"
    )
    calls = []

    def fake_run(args, **_kw):
        calls.append(list(args))
        if "--confirm-not-duplicate" in args:
            assert calls[0] is not None
            return _FakeResult(0, "https://github.com/mytab0r/edge-harness/issues/4243\n")
        return _FakeResult(1, "", storage_stderr)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append((issue, text)))

    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_600_000, 5_000_000, 152.0, 80.0)

    assert number == 4243
    assert "заведена" in note and "--confirm-not-duplicate" in note and "другие ресурсы квоты" in note
    assert posted == []  # чужой задаче улика НЕ ушла
    assert len(calls) == 2
    assert "--confirm-not-duplicate" in calls[1]
    assert "DO rows_read/сутки" in calls[1][calls[1].index("--confirm-not-duplicate") + 1]


def test_create_task_mixed_candidates_prefers_same_resource(monkeypatch):
    """Среди похожих кандидатов есть задача ТОГО ЖЕ ресурса — улика уходит
    в неё (вторая задача не заводится), задача другого ресурса игнорируется."""
    mixed_stderr = (
        "::error::issue-create: похожие ОТКРЫТЫЕ задачи пула уже есть "
        "(класс #566, живой случай #518/#547/#564):\n"
        "  #778 (score 0.50): Квота харнеса перевалила за 80.0%: DO storage/аккаунт — https://...\n"
        "  #777 (score 0.91): Квота харнеса перевалила за 80.0%: DO rows_read/сутки — https://...\n"
    )

    def fake_run(args, **_kw):
        assert "--confirm-not-duplicate" not in args, "свой кандидат найден — повторного вызова быть не должно"
        return _FakeResult(1, "", mixed_stderr)

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append((issue, text)))

    number, note = qa.create_or_note_task(REPO, "DO rows_read/сутки", "cf_do_rows_read_day",
                                           7_600_000, 5_000_000, 152.0, 80.0)

    assert number == 777
    assert len(posted) == 1
    assert "тот же ресурс" in posted[0][1]  # улика называет, почему получатель — своя задача
