#!/usr/bin/env python3
"""Тесты дедуп-эскалации и автозаведения задачи на переходе через порог
квоты (scripts/measure/quota_alert.py, #605).

Сетевые вызовы (pulse_guard.gh/escalate/post_issue_comment, subprocess.run
для scripts/gh/issue-create) подменяются monkeypatch — тот же приём, что
scripts/orchestra/test_pulse_guard.py/scripts/measure/test_quotas.py.

Запуск: python -m pytest scripts/measure/test_quota_alert.py -q
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("quota_alert.py")
spec = importlib.util.spec_from_file_location("quota_alert", SCRIPT)
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)  # type: ignore[union-attr]

REPO = "mytab0r/edge-harness"

# Ссылки на РЕАЛЬНЫЕ функции, захваченные ДО того, как автоюз-фикстура ниже
# подменит qa.last_reading/qa.record_reading на нейтральные заглушки — тесты,
# которые проверяют last_reading/record_reading НАПРЯМУЮ, зовут именно эти
# ссылки, а не атрибут qa.last_reading (тот уже не настоящая функция к
# моменту, когда выполняется тело теста).
_REAL_LAST_READING = qa.last_reading
_REAL_RECORD_READING = qa.record_reading


def _comment(body: str, created_at: str = "2026-09-06T18:00:00Z") -> dict:
    return {"body": body, "created_at": created_at}


@pytest.fixture(autouse=True)
def _no_trend_bookkeeping_by_default(monkeypatch):
    """check_and_alert (#1100) теперь ВСЕГДА читает/пишет числовое показание
    тренда (last_reading/record_reading) — оба бьют по сети без мока. Тесты
    ниже, которым тренд не важен, получают нейтральный дефолт («показаний
    ещё не было», запись — успешный no-op); тесты, которым тренд нужен,
    переопределяют оба явно (тот же monkeypatch-инстанс, override работает)."""
    monkeypatch.setattr(qa, "last_reading", lambda repo, key: None)
    monkeypatch.setattr(qa, "record_reading", lambda *a, **k: None)


# ── last_state: разбор маркера ────────────────────────────────────────────


ISSUE_METADATA_URL = f"repos/{REPO}/issues/120"


def _fake_gh_over_history(total_comments: int, last_page_body: list[dict]):
    """Фейковый pulse_guard.gh для истории #120: первый вызов — метаданные
    issue (число комментариев), второй — СВЕЖАЯ страница (вычисленная от
    total_comments, не первая), которую all_issue_comments обязан запросить
    напрямую (found #1100 — см. all_issue_comments)."""
    last_page = (total_comments + 99) // 100 if total_comments else 0
    expected_page_url = f"repos/{REPO}/issues/120/comments?per_page=100&page={last_page}"
    requested = []

    def fake_gh(*args):
        endpoint = args[0]
        requested.append(endpoint)
        if endpoint == ISSUE_METADATA_URL:
            return {"comments": total_comments}
        assert endpoint == expected_page_url, (
            f"ожидалась СВЕЖАЯ (последняя) страница {expected_page_url!r}, "
            f"запрошено {endpoint!r}"
        )
        return last_page_body

    return fake_gh, requested


def test_last_state_none_when_no_marker(monkeypatch):
    fake_gh, _ = _fake_gh_over_history(0, [])
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)


def test_last_state_reads_only_fresh_page_of_watchdog_history(monkeypatch):
    """Найдено ревью PR #607 некритичным замечанием, затем — корнем #1100
    (находка F4 прочёса #1096): эндпоинт GitHub «List issue comments» не
    поддерживает sort/direction — страница 1 растущей истории #120 (900+
    комментариев) ВСЕГДА самая старая, не самая свежая. last_state обязан
    прочитать вычисленную ПОСЛЕДНЮЮ страницу (per_page=100), не буквальную
    page=1. Гвардия: запрос НЕ последней страницы — громкое падение (см.
    _fake_gh_over_history — assert внутри)."""
    # 950 комментариев → last_page = 10, полная страница БЕЗ маркеров
    # состояния — дедуп честно отвечает «состояние не известно».
    fake_gh, requested = _fake_gh_over_history(
        950, [{"id": i, "body": f"comment {i}", "created_at": "2026-09-13T00:00:00Z"} for i in range(50)],
    )
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == (None, None)
    assert requested == [ISSUE_METADATA_URL, f"repos/{REPO}/issues/120/comments?per_page=100&page=10"]


def test_last_state_reads_breach_with_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "breach", 999)
    fake_gh, _ = _fake_gh_over_history(1, [_comment(f"текст\n{marker}")])
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("breach", 999)


def test_last_state_reads_ok_without_issue_number(monkeypatch):
    marker = qa.state_marker("cf_do_rows_read_day", "ok", None)
    fake_gh, _ = _fake_gh_over_history(1, [_comment(f"текст\n{marker}")])
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_picks_the_latest_marker_not_the_first(monkeypatch):
    """Два маркера одного ресурса — состояние решает САМЫЙ СВЕЖИЙ по времени,
    не первый в списке (порядок ответа GitHub не гарантирован хронологией)."""
    older = _comment(qa.state_marker("cf_do_rows_read_day", "breach", 1), "2026-09-06T10:00:00Z")
    newer = _comment(qa.state_marker("cf_do_rows_read_day", "ok", None), "2026-09-06T20:00:00Z")
    fake_gh, _ = _fake_gh_over_history(2, [older, newer])
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert qa.last_state(REPO, "cf_do_rows_read_day") == ("ok", None)


def test_last_state_ignores_marker_of_a_different_resource(monkeypatch):
    """Маркер другого ресурса не должен путаться с искомым — иначе состояние
    одного ресурса решалось бы по эскалации совсем другого (независимость
    дедупа по ресурсам, см. докстринг модуля)."""
    other = _comment(qa.state_marker("cf_workers_requests_day", "breach", 1))
    fake_gh, _ = _fake_gh_over_history(1, [other])
    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
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


# ── Тренд (#1100): reading-маркер, троттлинг чисел, третье состояние ─────────


def test_reading_marker_roundtrip():
    when = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    marker = qa.reading_marker("gh_rest_rate_limit_hour", 6.1, when)
    assert marker == "[quota: замер gh_rest_rate_limit_hour = 6.1% at 2026-09-13T07:03:00+00:00]"


def test_last_reading_none_when_no_marker(monkeypatch):
    monkeypatch.setattr(qa.pulse_guard, "gh", lambda *a: {"comments": 0} if a[0].endswith("/120") else [])
    assert _REAL_LAST_READING(REPO, "gh_rest_rate_limit_hour") is None


def test_last_reading_parses_pct_time_and_comment_id(monkeypatch):
    when = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    marker = qa.reading_marker("gh_rest_rate_limit_hour", 6.1, when)

    def fake_gh(*args):
        endpoint = args[0]
        if endpoint.endswith("/issues/120"):
            return {"comments": 1}
        return [{"id": 555, "body": marker, "created_at": "2026-09-13T07:03:05Z"}]

    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    pct, ts, comment_id = _REAL_LAST_READING(REPO, "gh_rest_rate_limit_hour")
    assert pct == 6.1
    assert ts == when
    assert comment_id == 555


def test_last_reading_ignores_marker_of_a_different_resource(monkeypatch):
    other = qa.reading_marker("cf_workers_requests_day", 50.0, datetime(2026, 9, 13, tzinfo=timezone.utc))

    def fake_gh(*args):
        if args[0].endswith("/issues/120"):
            return {"comments": 1}
        return [{"id": 1, "body": other, "created_at": "2026-09-13T00:00:00Z"}]

    monkeypatch.setattr(qa.pulse_guard, "gh", fake_gh)
    assert _REAL_LAST_READING(REPO, "gh_rest_rate_limit_hour") is None


def test_record_reading_edits_existing_carrier_not_a_new_comment(monkeypatch):
    """Второй и далее тик — PATCH на тот же comment_id, НЕ новый POST (иначе
    ровно тот класс, что дал 114 дублей в инциденте #1100)."""
    edited = []
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "edit_issue_comment", lambda repo, cid, text: edited.append((cid, text)))
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", lambda repo, issue, text: posted.append(text))

    when = datetime(2026, 9, 13, 7, 18, 0, tzinfo=timezone.utc)
    _REAL_RECORD_READING(REPO, "gh_rest_rate_limit_hour", 23.07, when, 555)

    assert edited == [(555, qa.reading_marker("gh_rest_rate_limit_hour", 23.07, when))]
    assert posted == []


def test_record_reading_posts_new_carrier_on_first_reading(monkeypatch):
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "edit_issue_comment",
                         lambda *a: (_ for _ in ()).throw(AssertionError("не должно править — носителя ещё нет")))
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", lambda repo, issue, text: posted.append((issue, text)))

    when = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    _REAL_RECORD_READING(REPO, "gh_rest_rate_limit_hour", 6.1, when, None)

    assert posted == [(qa.WATCHDOG_ISSUE, qa.reading_marker("gh_rest_rate_limit_hour", 6.1, when))]


def test_reading_carrier_falling_off_fresh_page_self_heals_next_tick(monkeypatch):
    """found: ревью PR #1112, подтверждённая деградация — редактирование НА
    МЕСТЕ не двигает `created_at` комментария (GitHub меняет только
    `updated_at`), а `all_issue_comments` листает страницы ПО ПОРЯДКУ
    СОЗДАНИЯ — значит носитель тренда, созданный один раз и вечно
    редактируемый, рано или поздно физически съезжает за окно
    MARKER_SCAN_PAGES по мере роста #120, независимо от того, как недавно
    его РЕДАКТИРОВАЛИ. Это ТОТ ЖЕ класс, что и сам инцидент #1100 (амнезия
    дедупа), только для числового носителя, не для маркера состояния.

    Поведение, которое этот тест закрепляет: тик, заставший `last_reading`
    вернувшим None (носитель уже существовал, но не найден на свежей
    странице), обрабатывается ТЕМ ЖЕ путём, что «первый тик вообще» —
    теряет ровно один сэмпл тренда в этом тике (projected_minutes
    посчитать не из чего, classify_state падает обратно на чистый
    pct>=threshold), но `record_reading` получает comment_id=None и
    заводит НОВЫЙ носитель у хвоста истории — на СЛЕДУЮЩЕМ тике позиция
    снова свежая. Самоисцеление за один тик, не постоянная слепота."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (qa.STATE_OK, None))
    monkeypatch.setattr(qa, "last_reading", lambda repo, key: None)  # носитель "потерян"
    monkeypatch.setattr(qa, "record_reading", _REAL_RECORD_READING)  # автоюз-заглушку — назад на реальную
    posted = []
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment",
                         lambda repo, issue, text: posted.append(text))
    monkeypatch.setattr(qa.pulse_guard, "edit_issue_comment",
                         lambda *a: (_ for _ in ()).throw(
                             AssertionError("comment_id неизвестен — обязан быть POST, не PATCH")))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    # pct=57% растёт быстро, но без прежнего показания тренд не посчитать —
    # classify_state обязан упасть на чистый порог (57% < 80%), не упасть
    # с исключением и не притвориться, что видел прошлый тик.
    result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour", "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                 570, 1000, 57.0, threshold=80.0)

    assert "без изменений" in result  # ok→ok по чистому pct, тренд в этот тик недоступен
    assert escalated == []  # не эскалация, потеря сэмпла тихая (best-effort)
    assert len(posted) == 1  # новый носитель ушёл к хвосту истории — позиция обновлена
    assert "57.0%" in posted[0]


def test_trend_projection_computes_rate_and_minutes_to_threshold():
    t1 = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 13, 7, 18, 0, tzinfo=timezone.utc)
    rate, projected = qa._trend_projection(6.1, t1, 23.07, t2, 80.0)
    assert rate == pytest.approx(1.1313, abs=1e-3)
    assert projected == pytest.approx(50.33, abs=0.1)


def test_trend_projection_none_when_shrinking():
    t1 = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 13, 7, 18, 0, tzinfo=timezone.utc)
    rate, projected = qa._trend_projection(50.0, t1, 10.0, t2, 80.0)
    assert rate < 0
    assert projected is None


def test_trend_projection_none_when_non_positive_time_delta():
    t1 = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    rate, projected = qa._trend_projection(6.1, t1, 23.07, t1, 80.0)
    assert rate is None and projected is None


def test_classify_state_breach_by_pct_alone():
    assert qa.classify_state(85.0, 80.0, None, 45.0) == qa.STATE_BREACH


def test_classify_state_approaching_by_projection_even_when_pct_low():
    """Ядро #1100: pct всё ещё далеко от порога, но скорость роста такая, что
    порог будет пробит внутри горизонта — approaching, не ok."""
    assert qa.classify_state(40.04, 80.0, 35.34, 45.0) == qa.STATE_APPROACHING


def test_classify_state_ok_when_projection_beyond_horizon():
    assert qa.classify_state(23.07, 80.0, 50.33, 45.0) == qa.STATE_OK


def test_classify_state_ok_when_no_projection():
    assert qa.classify_state(6.1, 80.0, None, 45.0) == qa.STATE_OK


def test_approaching_transition_escalates_without_creating_task(monkeypatch):
    """approaching — Telegram + след в #120, БЕЗ автозаведения задачи (см.
    докстринг check_and_alert): в отличие от breach, тренд может развернуться
    сам, не дойдя до порога."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (qa.STATE_OK, None))
    t1 = datetime(2026, 9, 13, 7, 18, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 13, 7, 33, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(qa, "last_reading", lambda repo, key: (23.07, t1, 1))
    create_calls = []
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: create_calls.append(1) or (999, "не должно вызываться"))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append(text) or "Telegram: доставлен; след в #120: оставлен")

    result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour", "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                 400, 1000, 40.04, threshold=80.0, now=t2)

    assert create_calls == []
    assert len(escalated) == 1
    assert "приближается" in escalated[0]
    assert "порог 80.0% будет пробит через" in escalated[0]
    assert "approaching" in escalated[0]
    assert "approaching" in result


def test_approaching_dedup_no_repeat_while_still_approaching(monkeypatch):
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (qa.STATE_APPROACHING, None))
    monkeypatch.setattr(qa, "last_reading", lambda repo, key: (40.04, datetime(2026, 9, 13, 7, 33, tzinfo=timezone.utc), 1))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour", "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                 570, 1000, 57.01, threshold=80.0,
                                 now=datetime(2026, 9, 13, 7, 48, tzinfo=timezone.utc))

    assert escalated == []
    assert "без изменений" in result


def test_recovery_from_approaching_does_not_claim_threshold_was_crossed(monkeypatch):
    """found: ревью PR #1112 — «approaching→ok уходит текстом ложного
    восстановления». Разворот тренда из approaching (порог НИКОГДА не
    пробивался) обязан отличаться от разворота из breach: читатель #120,
    пропустивший ⚠️-запись, не должен увидеть «вернулась ниже порога» о
    пороге, которого квота не касалась."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (qa.STATE_APPROACHING, None))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour", "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                 300, 1000, 30.0, threshold=80.0)

    assert len(escalated) == 1
    text = escalated[0]
    assert "вернулась ниже" not in text  # НЕ текст breach→ok
    assert "порог 80.0% НЕ был достигнут" in text
    assert "recovery" in result


def test_recovery_from_breach_still_mentions_the_task(monkeypatch):
    """Контрольный тест к предыдущему: breach→ok НЕ меняет поведение —
    текст по-прежнему называет задачу на разбор."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (qa.STATE_BREACH, 4242))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qa.check_and_alert(REPO, "cf_do_rows_read_day", "DO rows_read/сутки", 100, 5_000_000, 2.0)

    assert len(escalated) == 1
    text = escalated[0]
    assert "вернулась ниже" in text
    assert "#4242" in text
    assert "recovery" in result


def test_first_observation_already_approaching_alerts(monkeypatch):
    """Симметрично уже существовавшему поведению для breach (found ревью
    PR #607): первое наблюдение, заставшее ресурс УЖЕ в approaching, — не
    «событие без содержания», как первое наблюдение в ok, а настоящая
    новость, которую стоит увидеть немедленно."""
    monkeypatch.setattr(qa, "last_state", lambda repo, key: (None, None))
    monkeypatch.setattr(qa, "last_reading", lambda repo, key: (23.07, datetime(2026, 9, 13, 7, 18, tzinfo=timezone.utc), 1))
    escalated = []
    monkeypatch.setattr(qa.pulse_guard, "escalate", lambda repo, issue, text: escalated.append(text) or "x")

    result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour", "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                 400, 1000, 40.04, threshold=80.0,
                                 now=datetime(2026, 9, 13, 7, 33, tzinfo=timezone.utc))

    assert len(escalated) == 1
    assert "approaching" in result


# ── Воспроизведение инцидента #1100 ──────────────────────────────────────────


def test_reproduction_1100_trend_fires_before_exhaustion(monkeypatch):
    """Критерий готовности #1100: на исторических данных 2026-09-13 (6.1% в
    07:03 → исчерпание в 08:26) механизм обязан дать сигнал МЕЖДУ этими
    точками.

    Реальные факты (дословно из issue #1100, тела маркеров/логов прогонов
    этого дня):
      - 07:03Z, прогон 34744247391: "gh_rest_rate_limit_hour: первое
        наблюдение (ok, 6.1%)".
      - 08:19Z: первый из шести прогонов orchestra.yml падает "API rate
        limit exceeded for installation (HTTP 403)".
      - 08:26Z: акцептанс-критерий issue #1100 называет это моментом
        исчерпания.
      - Точное число лимита installation-токена НЕ ПОДТВЕРЖДЕНО однозначно:
        документация GitHub называет 1000 запросов/час, а живой замер
        этого же репозитория (docs/research/21-github-actions.md,
        «Live-замер 2026-09-09», issue #812) — 5000/час; ниже используется
        1000 как иллюстративное число для получения тех же `current`,
        что дают наблюдаемые pct (6.1%, 100%) — сама механика теста работает
        с ПРОЦЕНТОМ, не с абсолютным числом, поэтому выбор 1000 против 5000
        не влияет на проверяемый результат. Форма ответа `gh api rate_limit`
        проверена живьём в этом PR:
        {"resources": {"core": {"limit":.., "used":..}}}.

    Честно про то, что НЕ факт: промежуточные показания (07:18, 07:33, ...)
    нигде не сохранились — сам корень #1100 в том, что `last_reading` до
    фикса возвращал (None, None) всегда, поэтому исторических чисел между
    07:03 и 08:26 не существует. Здесь они — ЛИНЕЙНАЯ интерполяция между
    двумя единственными документированными точками (6.1% → 100% за 83
    минуты, скорость 1.1313 п.п./мин, постоянная) — простейшая модель,
    не заявленная как факт."""
    T0 = datetime(2026, 9, 13, 7, 3, 0, tzinfo=timezone.utc)
    EXHAUSTION = datetime(2026, 9, 13, 8, 26, 0, tzinfo=timezone.utc)
    FIRST_FAILURE = datetime(2026, 9, 13, 8, 19, 0, tzinfo=timezone.utc)
    LIMIT = 1000.0
    START_PCT = 6.1
    total_minutes = (EXHAUSTION - T0).total_seconds() / 60.0
    rate_real = (100.0 - START_PCT) / total_minutes  # калибровка по двум документированным точкам

    def pct_at(t):
        minutes = (t - T0).total_seconds() / 60.0
        return round(min(START_PCT + rate_real * minutes, 100.0), 2)

    carrier = {"reading": None, "state": (None, None)}
    escalated = []

    monkeypatch.setattr(qa, "last_reading", lambda repo, key: carrier["reading"])
    monkeypatch.setattr(qa, "last_state", lambda repo, key: carrier["state"])
    monkeypatch.setattr(qa, "record_reading",
                         lambda repo, key, pct, when, cid: carrier.__setitem__("reading", (pct, when, 1)))
    monkeypatch.setattr(qa.pulse_guard, "post_issue_comment", lambda *a: None)
    monkeypatch.setattr(qa.pulse_guard, "escalate",
                         lambda repo, issue, text: escalated.append((text,)) or "Telegram: доставлен; след в #120: оставлен")
    monkeypatch.setattr(qa, "create_or_note_task", lambda *a: (0, "не должно вызываться в approaching"))

    fired_at = None
    t = T0
    tick = timedelta(minutes=15)  # CHECK_INTERVAL_MINUTES (quota_watch.py)
    while t <= EXHAUSTION:
        pct = pct_at(t)
        current = round(LIMIT * pct / 100.0)
        result = qa.check_and_alert(REPO, "gh_rest_rate_limit_hour",
                                     "GitHub REST rate limit (PAT/GITHUB_TOKEN)",
                                     current, LIMIT, pct, threshold=80.0, now=t)
        if "approaching —" in result:
            fired_at = t
            break
        # обновляем "персистентное" состояние тем же способом, каким его
        # обновил бы реальный state_marker в #120 (ok/breach фиксируются
        # через escalate/post_issue_comment, которые здесь замоканы —
        # без этого обновления дедуп следующего тика не увидит переход).
        if "первое наблюдение" in result or "без изменений" in result:
            carrier["state"] = (qa.STATE_OK, None)
        t += tick

    assert fired_at is not None, "тренд обязан был сработать до достижения EXHAUSTION"
    minutes_before_exhaustion = (EXHAUSTION - fired_at).total_seconds() / 60.0
    minutes_before_first_failure = (FIRST_FAILURE - fired_at).total_seconds() / 60.0
    assert minutes_before_exhaustion > 0, "сигнал обязан прийти ДО исчерпания"
    assert minutes_before_first_failure > 0, "сигнал обязан прийти ДО первого реального отказа оркестратора"
    assert fired_at == datetime(2026, 9, 13, 7, 33, 0, tzinfo=timezone.utc)
    assert minutes_before_exhaustion == pytest.approx(53.0, abs=0.01)
    assert minutes_before_first_failure == pytest.approx(46.0, abs=0.01)
    assert len(escalated) == 1
    assert "приближается к пределу" in escalated[0][0]

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
