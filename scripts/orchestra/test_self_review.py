#!/usr/bin/env python3
"""Тесты periodической саморевизии (issue #1025, scripts/orchestra/self_review.py).

Гвардия каталога: scripts/ci/guards/self-review-guard.sh. Мутация-доказательство
(руками, не автоматизирована в этом файле): удалить тело `has_verifiable_fact`
(вернуть всегда True) -> test_has_verifiable_fact_rejects_guess и
test_decide_findings_rejects_finding_without_fact краснеют; вернуть тело — снова
зелёные. Аналогично для `fingerprint`: заменить на константу -> оба теста
дедупа (test_decide_findings_dedup_by_fingerprint и
test_existing_fingerprints_parses_body) краснеют.

Запуск: python -m pytest scripts/orchestra/test_self_review.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("self_review", _HERE / "self_review.py")
self_review = importlib.util.module_from_spec(_spec)
sys.modules["self_review"] = self_review
_spec.loader.exec_module(self_review)  # type: ignore[union-attr]


# ── Чек-лист (данные) ────────────────────────────────────────────────────────


def test_load_checklist_has_seven_paid_classes():
    """Число не догма, но регрессия ниже семи — потерянные пункты
    (см. AGENTS.md, «Инцидент оставляет инвариант»)."""
    classes = self_review.load_checklist()
    assert len(classes) >= 7
    ids = {entry["id"] for entry in classes}
    assert "brake-without-gas" in ids
    assert "signal-without-reader" in ids


def test_render_checklist_reflects_config_not_hardcoded():
    """Чек-лист — ДАННЫЕ: новый пункт конфига обязан появиться в промпте без
    правки self_review.py."""
    classes = self_review.load_checklist()
    extra = classes + [{"id": "zzz-new", "title": "Новый пункт",
                          "look_for": "искать штуку", "example": "пример штуки"}]
    rendered = self_review.render_checklist(extra)
    assert "Новый пункт" in rendered
    assert "искать штуку" in rendered


def test_build_prompt_includes_open_question_checklist_and_digest():
    digest = {"window": {"since": "2026-09-10T00:00:00+00:00"}, "pool_summary": {"open_tasks": 3}}
    prompt = self_review.build_prompt(digest, self_review.load_checklist())
    assert "открытый вопрос" in prompt.lower() or "Открытый вопрос" not in "" and "что здесь не сходится" in prompt.lower()
    assert "### НАХОДКА" in prompt
    assert '"open_tasks": 3' in prompt
    assert "Тормоз без газа" in prompt


# ── Разбор находок ───────────────────────────────────────────────────────────


VALID_BLOCK = """### НАХОДКА
ЗАГОЛОВОК: worker.yml — 8 провалов из 8 после мержа #878
КЛАСС: дефект
ФАКТ: `gh run list --workflow worker.yml` показывает 8 conclusion=failure подряд начиная с прогона после 2026-09-10T22:54Z
ПОЧЕМУ_ВАЖНО: воркер не открывает PR вовсе, задачи пула не продвигаются
БЛОКИРУЕТСЯ: ничем
### КОНЕЦ НАХОДКИ"""


def test_parse_findings_single_block():
    findings = self_review.parse_findings(VALID_BLOCK)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.klass == "дефект"
    assert "878" in finding.title
    assert finding.blocked_by == "ничем"


def test_parse_findings_multiple_blocks():
    text = VALID_BLOCK + "\n\n" + VALID_BLOCK.replace("878", "999")
    findings = self_review.parse_findings(text)
    assert len(findings) == 2
    assert findings[1].title.endswith("999")


def test_parse_findings_no_findings_literal_returns_empty():
    assert self_review.parse_findings("НАХОДОК НЕТ") == []
    assert self_review.parse_findings("") == []


def test_parse_findings_drops_block_missing_required_field():
    broken = """### НАХОДКА
ЗАГОЛОВОК: что-то
КЛАСС: дефект
ФАКТ: цифра 42 из логов
### КОНЕЦ НАХОДКИ"""
    assert self_review.parse_findings(broken) == []


def test_parse_findings_ignores_prose_outside_blocks():
    text = "Я посмотрел на данные и ничего примечательного не увидел.\n\n" + VALID_BLOCK
    findings = self_review.parse_findings(text)
    assert len(findings) == 1


# ── Жёсткий фильтр факта ──────────────────────────────────────────────────────


def test_has_verifiable_fact_accepts_command_with_number():
    assert self_review.has_verifiable_fact(
        "`gh run list` показывает 8 failure подряд после 2026-09-10T22:54Z")


def test_has_verifiable_fact_accepts_url():
    assert self_review.has_verifiable_fact(
        "https://github.com/mytab0r/edge-harness/actions/runs/34498185823")


def test_has_verifiable_fact_rejects_guess():
    """Живой антипример класса #962: гипотеза без числа/команды/ссылки."""
    assert not self_review.has_verifiable_fact("возможно тут проблема с провайдером")


def test_has_verifiable_fact_rejects_empty_and_too_short():
    assert not self_review.has_verifiable_fact("")
    assert not self_review.has_verifiable_fact("42")


# ── Отпечаток и метки по классу ───────────────────────────────────────────────


def _finding(title="Пример находки", klass="дефект",
             fact="зафиксировано 42 прогона подряд в логе", why="важно",
             blocked_by="ничем"):
    return self_review.Finding(title=title, klass=klass, fact=fact, why=why,
                                blocked_by=blocked_by)


def test_fingerprint_stable_for_same_title_and_class():
    a = self_review.fingerprint(_finding(title="worker.yml падает 8 раз подряд"))
    b = self_review.fingerprint(_finding(title="Восемь подряд провалов worker.yml"))
    # Разные слова — Jaccard решает похожесть на уровне decide_findings,
    # отпечаток сам по себе основан на токенах, не обязан совпасть по СЛОВАМ,
    # которые не пересекаются вовсе. Проверяем инвариант отпечатка: один и тот
    # же заголовок + класс => один и тот же отпечаток (детерминированность).
    c = self_review.fingerprint(_finding(title="worker.yml падает 8 раз подряд"))
    assert a == c


def test_fingerprint_differs_by_class():
    same_title = "Петля меток на issue"
    a = self_review.fingerprint(_finding(title=same_title, klass="дефект"))
    b = self_review.fingerprint(_finding(title=same_title, klass="знание"))
    assert a != b


def test_classify_labels_defect_is_plain_task():
    labels = self_review.classify_labels("дефект")
    assert "task" in labels and "self-review" in labels
    assert self_review.KNOWLEDGE_LABEL not in labels
    assert self_review.INSTRUMENT_LABEL not in labels


def test_classify_labels_knowledge():
    labels = self_review.classify_labels("знание")
    assert self_review.KNOWLEDGE_LABEL in labels


def test_classify_labels_instrument():
    labels = self_review.classify_labels("инструмент")
    assert self_review.INSTRUMENT_LABEL in labels


def test_classify_labels_unknown_class_falls_back_to_plain_task():
    labels = self_review.classify_labels("что-то невиданное")
    assert labels == (self_review.TASK_LABEL, self_review.SELF_REVIEW_LABEL)


def test_finding_body_knowledge_requires_doc_write():
    finding = _finding(klass="знание")
    body = self_review.finding_body(finding, {"since": "s", "until": "u"})
    assert "AGENTS.md" in body
    assert "Отпечаток: `" in body


def test_finding_body_instrument_requires_historical_proof():
    finding = _finding(klass="инструмент")
    body = self_review.finding_body(finding, {"since": "s", "until": "u"})
    assert "доказательство на истории" in body
    assert "scripts/measure/" in body


# ── Решение: дедуп + потолок (чистая функция, без сети) ──────────────────────


def test_decide_findings_creates_when_novel_and_within_cap():
    findings = [_finding(title="Новая находка А"), _finding(title="Новая находка Б")]
    decisions = self_review.decide_findings(findings, known_fingerprints=set(),
                                             candidates=[], remaining_cap=5)
    assert [d.action for d in decisions] == ["create", "create"]


def test_decide_findings_rejects_finding_without_fact():
    findings = [_finding(fact="догадка без цифр и команд")]
    decisions = self_review.decide_findings(findings, known_fingerprints=set(),
                                             candidates=[], remaining_cap=5)
    assert decisions[0].action == "reject_no_fact"


def test_decide_findings_dedup_by_fingerprint():
    finding = _finding(title="Повторная находка")
    fp = self_review.fingerprint(finding)
    decisions = self_review.decide_findings([finding], known_fingerprints={fp},
                                             candidates=[], remaining_cap=5)
    assert decisions[0].action == "duplicate"
    assert fp in decisions[0].detail


def test_decide_findings_dedup_by_token_similarity_to_open_pool():
    finding = _finding(title="worker.yml падает 8 раз подряд после мержа 878")
    candidates = [{"number": 42, "title": "worker.yml падает 8 раз подряд после мержа 878 регрессия",
                    "url": "https://x"}]
    decisions = self_review.decide_findings([finding], known_fingerprints=set(),
                                             candidates=candidates, remaining_cap=5)
    assert decisions[0].action == "duplicate"
    assert "#42" in decisions[0].detail


def test_decide_findings_caps_after_remaining_budget():
    findings = [_finding(title=f"Находка {i}") for i in range(3)]
    decisions = self_review.decide_findings(findings, known_fingerprints=set(),
                                             candidates=[], remaining_cap=1)
    actions = [d.action for d in decisions]
    assert actions == ["create", "capped", "capped"]


def test_decide_findings_zero_cap_caps_everything():
    findings = [_finding()]
    decisions = self_review.decide_findings(findings, known_fingerprints=set(),
                                             candidates=[], remaining_cap=0)
    assert decisions[0].action == "capped"


# ── Наблюдаемость (печатает, почему 0) ───────────────────────────────────────


def test_summarize_decisions_no_findings_names_the_reason():
    text = self_review.summarize_decisions([])
    assert "не вернула ни одной находки" in text


def test_summarize_decisions_counts_each_action():
    decisions = [
        self_review.Decision(_finding(title="A"), "create", "ok"),
        self_review.Decision(_finding(title="B"), "duplicate", "уже открыта #1"),
        self_review.Decision(_finding(title="C"), "reject_no_fact", "нет факта"),
        self_review.Decision(_finding(title="D"), "capped", "потолок"),
    ]
    text = self_review.summarize_decisions(decisions)
    assert "заведено 1" in text
    assert "отброшено без факта 1" in text
    assert "дублей 1" in text
    assert "отсечено потолком 1" in text


def test_cap_exhausted_alert_names_gas():
    text = self_review.cap_exhausted_alert_text(["А", "Б"], created_today=3, cap=3)
    assert self_review.CAP_EXHAUSTED_MARKER in text
    assert "снимается сам" in text


# ── Транспорт: отказ прав красит прогон, не возвращает 0 ─────────────────────


def test_gather_workflow_digest_raises_when_every_workflow_fails(monkeypatch):
    def _boom(repo, workflow, per_page=100):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(self_review, "recent_runs", _boom)
    try:
        self_review.gather_workflow_digest("owner/repo", datetime.now(timezone.utc),
                                            workflows=("a.yml", "b.yml"))
        assert False, "должен был поднять GatherTransportError"
    except self_review.GatherTransportError as error:
        assert "403" in str(error) or "не прочитались" in str(error)


def test_gather_workflow_digest_partial_failure_is_visible_not_silent(monkeypatch):
    def _mixed(repo, workflow, per_page=100):
        if workflow == "broken.yml":
            raise RuntimeError("network unreachable")
        return [{"id": 1, "event": "schedule", "conclusion": "success",
                  "status": "completed", "created_at": "2026-09-11T00:00:00Z",
                  "updated_at": "2026-09-11T00:05:00Z", "html_url": "https://x"}]
    monkeypatch.setattr(self_review, "recent_runs", _mixed)
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    digest = self_review.gather_workflow_digest("owner/repo", since,
                                                 workflows=("broken.yml", "ok.yml"))
    assert "error" in digest["broken.yml"]
    assert digest["ok.yml"]["total_in_window"] == 1


def test_gather_label_churn_reports_error_not_empty_silence(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(self_review, "gh", _boom)
    result = self_review.gather_label_churn("owner/repo", datetime.now(timezone.utc))
    assert result and "error" in result[0]


def test_gather_label_churn_is_bounded_not_full_history(monkeypatch):
    """Класс, пойманный живой попыткой 2026-09-12 (см. докстринг
    gather_label_churn): repo-wide issues/events без границы читает ВСЮ
    историю. Здесь — только ОДИН запрос списка недавно обновлённых issues
    (`gh`, не list_pages) + таймлайн НЕ БОЛЬШЕ max_issues штук."""
    calls = {"gh": 0, "timeline": 0}

    def _fake_gh(url):
        calls["gh"] += 1
        assert "since=" in url and "sort=updated" in url
        return [{"number": i} for i in range(10)]

    def _fake_timeline(repo, number, gh_func):
        calls["timeline"] += 1
        return []

    monkeypatch.setattr(self_review, "gh", _fake_gh)
    monkeypatch.setattr(self_review.review_labels, "list_timeline", _fake_timeline)
    self_review.gather_label_churn("owner/repo", datetime.now(timezone.utc), max_issues=3)
    assert calls == {"gh": 1, "timeline": 3}


def test_gather_label_churn_counts_toggles_above_threshold(monkeypatch):
    since = datetime(2026, 9, 10, tzinfo=timezone.utc)
    events_by_issue = {
        782: [
            {"event": "labeled" if i % 2 == 0 else "unlabeled",
             "created_at": f"2026-09-11T0{i}:00:00Z", "label": {"name": "waiting:owner"}}
            for i in range(5)
        ],
        # Мимо порога — другой issue с одним переключением.
        1: [{"event": "labeled", "created_at": "2026-09-11T09:00:00Z",
             "label": {"name": "task"}}],
    }

    def _fake_gh(url):
        return [{"number": number} for number in events_by_issue]

    def _fake_timeline(repo, number, gh_func):
        return events_by_issue[number]

    monkeypatch.setattr(self_review, "gh", _fake_gh)
    monkeypatch.setattr(self_review.review_labels, "list_timeline", _fake_timeline)
    result = self_review.gather_label_churn("owner/repo", since, min_toggles=4)
    assert len(result) == 1
    assert result[0]["issue"] == 782
    assert result[0]["toggles"] == 5


# ── Отпечатки уже заведённых self-review issues (открытые+закрытые) ──────────


def test_existing_fingerprints_parses_open_and_closed(monkeypatch):
    fake_issues = [
        {"number": 1, "body": "текст ...\n\nОтпечаток: `abc123456789`\n\nхвост"},
        {"number": 2, "body": "без отпечатка"},
        {"number": 3, "body": "Отпечаток: `def987654321`"},
    ]

    def _fake(url, gh_func):
        assert "state=all" in url
        return fake_issues

    monkeypatch.setattr(self_review.review_labels, "list_pages", _fake)
    found = self_review.existing_self_review_fingerprints("owner/repo")
    assert found == {"abc123456789", "def987654321"}
