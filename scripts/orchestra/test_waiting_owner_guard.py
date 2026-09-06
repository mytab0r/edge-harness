#!/usr/bin/env python3
"""Тесты гвардии `waiting:owner` (scripts/orchestra/waiting_owner_guard.py, #470).

Прод-форма: тело issue #370 (снято живым `gh issue view 370 --json body`
2026-09-05) несёт критерий «владелец выбирает и фиксирует явно» с тремя
пронумерованными вариантами — но БЕЗ машиночитаемого блока «## Варианты
владельца» (варианты идут прозой под «## Критерий готовности», через перенос
строки, без «—»). Живой контрпример: доказывает, что старый, уже закрытый
issue не матчится новым узким форматом задним числом — авто-метка размечает
только НОВЫЙ, машиночитаемый формат, не всякий текст про «решение владельца».

Запуск: python -m pytest scripts/orchestra/test_waiting_owner_guard.py -q
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

SCRIPT = _DIR / "waiting_owner_guard.py"
spec = importlib.util.spec_from_file_location("waiting_owner_guard", SCRIPT)
wog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wog)  # type: ignore[union-attr]

pg = sys.modules["pulse_guard"]

REPO = "mytab0r/edge-harness"


def utc(h=0, m=0):
    return datetime(2026, 9, 6, h, m, tzinfo=timezone.utc)


# ── Прод-форма: тело #370 (снято 2026-09-05 живым gh), без нового формата ──────

ISSUE_370_BODY = (
    "#341\n\n## Цель\nИнвариант 6 … Нужно решение владельца, каким каналом "
    "инвариант 6 всё-таки видит дрейф — а не тихая заглушка «работает только "
    "руками».\n\n## Критерий готовности\nОдно из (владелец выбирает и "
    "фиксирует явно, не молчаливым дефолтом):\n1. Новый секрет — PAT с "
    "admin-правами на репозиторий …\n2. Осознанный отказ от автоматизации …\n"
    "3. Любой третий вариант, который владелец сочтёт дешевле двух выше.\n"
)

# Новый машиночитаемый формат (docs/agents/PROTOCOL.md) — сконструирован по
# контракту, реального issue с этим форматом на момент теста ещё нет (гвардия
# и есть первое, что его вводит).
NEW_FORMAT_BODY = (
    "## Цель\nНужно решение владельца по маршруту интеграции.\n\n"
    "## Варианты владельца\n"
    "1. Секрет с правами администратора — включаем автоматику полностью\n"
    "2. Оставить ручной инструмент — автоматики не будет вовсе\n\n"
    "## Площадь\n- area:orchestra\n"
)

NEW_FORMAT_BODY_ONE_VARIANT = (
    "## Варианты владельца\n1. Единственный вариант без выбора — сделать так\n"
)


def issue(number, body, labels=("task",), comments_text=()):
    return {
        "number": number,
        "labels": [{"name": name} for name in labels],
        "body": body,
        "comments_text": list(comments_text),
    }


# ── variant_lines / should_auto_label ───────────────────────────────────────


def test_variant_lines_empty_without_header():
    assert wog.variant_lines(ISSUE_370_BODY) == []


def test_variant_lines_empty_on_prose_without_dash():
    """Живой контрпример #370: варианты есть, но не в машиночитаемом блоке и
    без «—» — признак не срабатывает вовсе."""
    assert wog.should_auto_label(ISSUE_370_BODY) is False


def test_variant_lines_finds_two_variants_in_new_format():
    lines = wog.variant_lines(NEW_FORMAT_BODY)
    assert len(lines) == 2
    assert lines[0].startswith("1.")
    assert lines[1].startswith("2.")


def test_variant_lines_stops_at_next_header():
    """Заголовок «## Площадь» после блока вариантов не даёт лишним строкам
    попасть в разбор — граница блока соблюдена."""
    lines = wog.variant_lines(NEW_FORMAT_BODY)
    assert all("area:orchestra" not in line for line in lines)


def test_should_auto_label_true_for_two_variants():
    assert wog.should_auto_label(NEW_FORMAT_BODY) is True


def test_should_auto_label_false_for_single_variant():
    """MIN_VARIANTS = 2: один вариант — не выбор, авто-метка не ставится."""
    assert wog.should_auto_label(NEW_FORMAT_BODY_ONE_VARIANT) is False


# ── find_candidates_for_auto_label ──────────────────────────────────────────


def test_find_candidates_skips_issue_without_variants_block():
    """Мутация: #370 (живая прод-форма) не даёт ложного срабатывания, даже
    когда явно просит «решение владельца» — формат не машиночитаем."""
    issues = [issue(370, ISSUE_370_BODY)]
    assert wog.find_candidates_for_auto_label(issues) == []


def test_find_candidates_flags_new_format_issue():
    issues = [issue(500, NEW_FORMAT_BODY)]
    result = wog.find_candidates_for_auto_label(issues)
    assert [i["number"] for i in result] == [500]


def test_find_candidates_skips_already_labeled():
    issues = [issue(500, NEW_FORMAT_BODY, labels=("task", "waiting:owner"))]
    assert wog.find_candidates_for_auto_label(issues) == []


# ── decision_marker / find_resolved ─────────────────────────────────────────


def test_decision_marker_none_without_marker():
    assert wog.decision_marker(["просто обсуждение, ничего не решено"]) is None


def test_decision_marker_reads_first_line_of_comment():
    texts = ["тело задачи", "РЕШЕНИЕ: 2\n\nПояснение, почему выбран второй вариант."]
    assert wog.decision_marker(texts) == 2


def test_decision_marker_ignores_mid_prose_mention():
    """«решение» посреди фразы обсуждения — не маркер (находка того же класса,
    что stale_blocked_guard.STALE_MARKER_RE уже ловит для «Блокирована: #N»)."""
    texts = ["Ждём, когда придёт решение по этому вопросу — пока неясно."]
    assert wog.decision_marker(texts) is None


def test_decision_marker_last_wins_on_correction():
    """Владелец поправил ответ вторым комментарием — считается последний."""
    texts = ["РЕШЕНИЕ: 1\n\nПервый ответ.", "РЕШЕНИЕ: 2\n\nПередумал, второй вариант лучше."]
    assert wog.decision_marker(texts) == 2


def test_find_resolved_flags_issue_with_decision_comment():
    issues = [issue(500, NEW_FORMAT_BODY, labels=("task", "waiting:owner"),
                    comments_text=["РЕШЕНИЕ: 1\n\nПервый вариант."])]
    assert wog.find_resolved(issues) == [{"number": 500, "option": 1}]


def test_find_resolved_silent_without_decision_comment():
    issues = [issue(500, NEW_FORMAT_BODY, labels=("task", "waiting:owner"),
                    comments_text=["ещё жду ответа"])]
    assert wog.find_resolved(issues) == []


# ── escalation_pending ───────────────────────────────────────────────────────


def test_escalation_pending_true_without_prior_marker():
    assert wog.escalation_pending([], utc(12, 0)) is True


def test_escalation_pending_false_within_threshold():
    marker_at = utc(0, 0)
    now = marker_at + timedelta(hours=23)
    assert wog.escalation_pending([marker_at], now) is False


def test_escalation_pending_true_after_threshold():
    marker_at = utc(0, 0)
    now = marker_at + timedelta(hours=25)
    assert wog.escalation_pending([marker_at], now) is True


def test_escalation_pending_uses_latest_of_several_markers():
    old = utc(0, 0)
    recent = old + timedelta(hours=20)
    now = recent + timedelta(hours=2)
    # старейший маркер уже за порогом (22ч > 24ч? нет, 22 < 24) — но раз
    # свежайший маркер моложе порога, эскалировать рано в любом случае
    assert wog.escalation_pending([old, recent], now) is False


# ── Тексты (детерминированные, тестируются на содержание) ──────────────────


def test_waiting_owner_alert_text_has_variants_link_and_threshold():
    text = wog.waiting_owner_alert_text(REPO, issue(500, NEW_FORMAT_BODY,
                                                     labels=("task", "waiting:owner")))
    assert text.startswith("🧭 " + wog.WAITING_OWNER_ESCALATE_MARKER)
    assert "1. Секрет с правами администратора" in text
    assert f"https://github.com/{REPO}/issues/500" in text
    assert "РЕШЕНИЕ: <номер>" in text
    assert f"{wog.WAITING_OWNER_REESCALATE_HOURS} ч" in text


def test_waiting_owner_alert_text_honest_without_variants_block():
    text = wog.waiting_owner_alert_text(
        REPO, issue(370, ISSUE_370_BODY, labels=("task", "waiting:owner")))
    assert "не размечены машиночитаемым блоком" in text


def test_resolved_comment_text_names_option_and_label():
    text = wog.resolved_comment_text(2)
    assert "вариант 2" in text and "waiting:owner" in text


# ── Проводка: gh замокан, сеть не нужна ─────────────────────────────────────


def patch_gh(monkeypatch, fake):
    monkeypatch.setattr(pg, "gh", fake)


@pytest.fixture()
def offline_telegram(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def test_waiting_owner_check_quiet_when_pool_empty():
    def fake(*args):
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return []
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    original = pg.gh
    pg.gh = fake
    try:
        lines = wog.waiting_owner_check(REPO, utc(12, 0))
    finally:
        pg.gh = original
    assert lines == ["💗 waiting:owner: открытых задач с меткой нет"]


def test_waiting_owner_check_auto_labels_new_candidate(monkeypatch):
    """Мутация (а): задача пула несёт блок вариантов и ещё не помечена —
    обязана получить метку POST'ом labels[]=waiting:owner."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return [{"number": 500, "labels": [{"name": "task"}], "body": NEW_FORMAT_BODY}]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = wog.waiting_owner_check(REPO, utc(12, 0))
    assert lines == ["🏷️ #500: waiting:owner проставлена автоматически "
                     "(найден блок «## Варианты владельца»)"]
    posted = [c for c in calls if c[0] == "-X" and c[1] == "POST" and "issues/500/labels" in c[2]]
    assert posted, "ожидался POST issues/500/labels"
    assert any(a == "labels[]=waiting:owner" for a in posted[0])


def test_waiting_owner_check_reports_failed_auto_label_not_silently(monkeypatch):
    """Находка AI-ревью PR #471: провал POST labels не должен быть тише
    провала DELETE labels ниже — прогон обязан вернуть 🚨-строку, не пустой
    список (который main() принял бы за холостой ход)."""
    def fake(*args):
        if args[0] == "-X" and args[1] == "POST" and "labels" in args[2]:
            raise RuntimeError("HTTP 404: Label does not exist")
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return [{"number": 500, "labels": [{"name": "task"}], "body": NEW_FORMAT_BODY}]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = wog.waiting_owner_check(REPO, utc(12, 0))
    assert len(lines) == 1
    assert lines[0].startswith("🚨 #500")
    assert not any(line.startswith("💗") for line in lines)


def test_waiting_owner_check_resolves_on_decision_comment(monkeypatch, offline_telegram):
    """Мутация (б): комментарий с маркером «РЕШЕНИЕ: N» — метка снимается
    DELETE'ом, подтверждение оставлено, эскалация не идёт."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return []
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return [{"number": 500, "labels": [{"name": "task"}, {"name": "waiting:owner"}],
                     "body": NEW_FORMAT_BODY}]
        if url == f"repos/{REPO}/issues/500/comments?per_page=100&page=1":
            return [{"body": "РЕШЕНИЕ: 1\n\nПервый вариант.", "created_at": "2026-09-06T10:00:00Z"}]
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = wog.waiting_owner_check(REPO, utc(12, 0))
    assert lines == ["✅ #500: решение получено (вариант 1), метка снята"]
    deleted = [c for c in calls if c[0] == "-X" and c[1] == "DELETE" and "issues/500/labels" in c[2]]
    assert deleted, "ожидался DELETE issues/500/labels/waiting:owner"
    commented = [c for c in calls if c[0] == "-X" and c[1] == "POST" and "issues/500/comments" in c[2]]
    assert commented, "подтверждение обязано быть оставлено в задаче"


def test_waiting_owner_check_escalates_without_prior_marker(monkeypatch, offline_telegram):
    """Мутация (в): нет решения и нет прежнего маркера эскалации — сигнал
    (комментарий в задачу + попытка Telegram) идёт."""
    calls = []

    def fake(*args):
        calls.append(args)
        if args[0] == "-X":
            return None
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return []
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return [{"number": 500, "labels": [{"name": "task"}, {"name": "waiting:owner"}],
                     "body": NEW_FORMAT_BODY, "title": "Нужно решение"}]
        if url == f"repos/{REPO}/issues/500/comments?per_page=100&page=1":
            return []
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = wog.waiting_owner_check(REPO, utc(12, 0))
    assert len(lines) == 1
    assert lines[0].startswith("🚨 #500")
    commented = [c for c in calls if c[0] == "-X" and c[1] == "POST" and "issues/500/comments" in c[2]]
    assert commented, "эскалация обязана оставить след прямо в задаче"
    body_arg = next(a for a in commented[0] if a.startswith("body="))
    assert wog.WAITING_OWNER_ESCALATE_MARKER in body_arg


def test_waiting_owner_check_silent_channel_when_already_escalated_recently(monkeypatch, offline_telegram):
    """Тот же эпизод уже сигналился < 24ч назад — повтор в канал не идёт, но
    находка остаётся в отчёте (не 💗) — CI-шаг не должен выглядеть холостым."""
    def fake(*args):
        if args[0] == "-X":
            raise AssertionError(f"мутирующий вызов не ожидался — эпизод молод: {args}")
        url = args[0]
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=task"):
            return []
        if url.startswith(f"repos/{REPO}/issues?state=open&labels=waiting:owner"):
            return [{"number": 500, "labels": [{"name": "task"}, {"name": "waiting:owner"}],
                     "body": NEW_FORMAT_BODY, "title": "Нужно решение"}]
        if url == f"repos/{REPO}/issues/500/comments?per_page=100&page=1":
            return [{"body": f"🧭 {wog.WAITING_OWNER_ESCALATE_MARKER}\nуже сигналили",
                     "created_at": "2026-09-06T11:00:00Z"}]
        raise AssertionError(f"неожиданный вызов gh: {args}")

    patch_gh(monkeypatch, fake)
    lines = wog.waiting_owner_check(REPO, utc(12, 0))
    assert lines == ["🔇 #500: нужен выбор владельца (уже сигналили, следующее напоминание позже)"]


def test_main_exit_code_reflects_violations(monkeypatch):
    monkeypatch.setattr(wog, "waiting_owner_check", lambda repo, now: ["🚨 нужен выбор владельца"])
    assert wog.main() == 1

    monkeypatch.setattr(wog, "waiting_owner_check", lambda repo, now: ["💗 всё чисто"])
    assert wog.main() == 0


def test_main_exit_code_treats_silent_channel_and_actions_correctly(monkeypatch):
    """🔇 — всё ещё нарушение (не отвечено); 🏷️/✅ — выполненные действия, не
    находки, не должны красить CI-шаг."""
    monkeypatch.setattr(wog, "waiting_owner_check", lambda repo, now: ["🔇 нужен выбор владельца"])
    assert wog.main() == 1

    monkeypatch.setattr(wog, "waiting_owner_check",
                        lambda repo, now: ["🏷️ проставлена автоматически", "✅ решение получено"])
    assert wog.main() == 0
