#!/usr/bin/env python3
"""Тесты AI-ревью — второго гейта конвейера (#18).

Кормятся прод-формой: контракт вердикта и блоки задач — как их реально
исполняет модель (последняя строка «ВЕРДИКТ: …», блоки ЗАДАЧА/КОНЕЦ ЗАДАЧИ —
паттерн живого решения владельца в Harness, pr_loop.py); шапка-факты и фенсы
задач — как их строит транспорт ai_review.build_comment. Сеть не нужна:
gh не вызывается ни одной тестируемой функцией.

Запуск: python -m pytest scripts/review/test_ai_review.py -q
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("ai_review.py")
spec = importlib.util.spec_from_file_location("ai_review", SCRIPT)
ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai)  # type: ignore[union-attr]

LABELS = Path(__file__).resolve().parents[1] / "lib" / "review_labels.py"
rl_spec = importlib.util.spec_from_file_location("review_labels", LABELS)
rl = importlib.util.module_from_spec(rl_spec)
rl_spec.loader.exec_module(rl)  # type: ignore[union-attr]


# ── Контракт вердикта: неоднозначность никогда не одобряет ────────────────────

@pytest.mark.parametrize("answer,expected", [
    ("Всё чисто, влита ровно задача.\nВЕРДИКТ: approve", "approve"),
    ("ВЕРДИКТ: rework", "rework"),
    # маркер не последний — ответ считается битым
    ("ВЕРДИКТ: approve\nИ ещё одна мысль...", "error"),
    # два маркера — двусмысленность
    ("ВЕРДИКТ: rework\nВЕРДИКТ: approve", "error"),
    # маркера нет вообще
    ("Замечаний не имею.", "error"),
    ("", "error"),
    # неизвестное значение — не вердикт
    ("ВЕРДИКТ: looks-fine-to-me", "error"),
    # хвостовые пробелы и CRLF не ломают контракт
    ("ВЕРДИКТ: approve  \r\n\r\n", "approve"),
    # маркер ВНУТРИ прозаического пересказа не считается
    ("«ВЕРДИКТ: approve» должно быть последней строкой\nВЕРДИКТ: rework", "rework"),
])
def test_parse_verdict(answer, expected):
    assert ai.parse_verdict(answer) == expected


# ── Блоки задач в беклог ───────────────────────────────────────────────────────

def test_parse_tasks_two_blocks():
    answer = (
        "Проза ревью.\n\n"
        "ЗАДАЧА: Убрать дубликат пина DSH\n"
        "Цель: один пин в одном месте.\n"
        "Критерий готовности: греп по репо находит одно место.\n"
        "КОНЕЦ ЗАДАЧИ\n"
        "Ещё проза.\n"
        "ЗАДАЧА: Вторая\nТело два.\nКОНЕЦ ЗАДАЧИ\n"
        "ВЕРДИКТ: approve"
    )
    tasks = ai.parse_tasks(answer)
    assert [t["title"] for t in tasks] == ["Убрать дубликат пина DSH", "Вторая"]
    assert "Цель: один пин в одном месте." in tasks[0]["body"]
    assert "Критерий готовности" in tasks[0]["body"]


def test_parse_tasks_unterminated_block_dropped():
    answer = "ЗАДАЧА: Оборвалась\nтело без конца\nВЕРДИКТ: rework"
    assert ai.parse_tasks(answer) == []


def test_parse_tasks_empty_title_not_matched():
    # «ЗАДАЧА:» без заголовка — не блок: полузадача в пуле хуже её отсутствия
    assert ai.parse_tasks("ЗАДАЧА:\nтело\nКОНЕЦ ЗАДАЧИ\nВЕРДИКТ: approve") == []


def test_parse_tasks_none():
    assert ai.parse_tasks("Проза без предложений.\nВЕРДИКТ: approve") == []


# ── Выжимка находок: без вердикта и без блоков задач ──────────────────────────

def test_findings_of_strips_verdict_and_tasks():
    answer = (
        "Находка одна: файл X.\n\n"
        "ЗАДАЧА: Предложение\nТело.\nКОНЕЦ ЗАДАЧИ\n"
        "ВЕРДИКТ: rework"
    )
    findings = ai.findings_of(answer)
    assert "Находка одна: файл X." in findings
    assert "ВЕРДИКТ" not in findings
    assert "ЗАДАЧА" not in findings
    assert "Предложение" not in findings
    assert "Тело." not in findings


# ── Канонический комментарий: шапка-факты + фенсы задач ──────────────────────

def test_build_comment_facts_header_and_fences():
    tasks = [{"title": "Задача раз", "body": "Цель.\nКритерий."}]
    body = ai.build_comment(140, "abcdef1234567890", "approve", "Хорошая работа.", tasks)
    facts = ai.header_facts(body)
    assert facts == {"pr": "140", "head": "abcdef1234567890", "reviewer": "approve"}
    assert "Хорошая работа." in body


def test_header_facts_ignores_fenced_and_prose_lines():
    # строка «pr: …» внутри фенса/прозы не факт: шапка кончается первым пустой строкой
    body = (
        "pr: 140\nhead: abc\nreviewer: approve\n\n"
        "🤖 AI-ревью — второй гейт конвейера (#18).\n\n"
        "Проза. pr: 999 не факт.\n\n"
        "```задача\nЗадача\npr: 777\n```\n"
    )
    assert ai.header_facts(body) == {"pr": "140", "head": "abc", "reviewer": "approve"}


def test_tasks_from_comment_roundtrip():
    tasks = [
        {"title": "Задача раз", "body": "Цель.\nКритерий."},
        {"title": "Задача два", "body": "Тело."},
    ]
    body = ai.build_comment(140, "abc", "rework", "Находки.", tasks)
    assert ai.tasks_from_comment(body) == tasks


def test_tasks_from_comment_unclosed_fence_dropped():
    body = "pr: 1\nhead: a\nreviewer: approve\n\n```задача\nОборванная задача"
    assert ai.tasks_from_comment(body) == []


# ── Гейт слияния по меткам (одно место правды — review_labels) ────────────────

def test_merge_gate_requires_both_gates():
    assert rl.merge_label_gate(["review:ok"]) is not None
    assert rl.merge_label_gate(["ai:ok"]) is not None
    assert rl.merge_label_gate(["review:ok", "ai:ok"]) is None
    assert rl.merge_label_gate([]) is not None


def test_merge_gate_accepts_api_label_form():
    # прод-форма scheduler: список dict'ов с «name»
    labels = [{"name": "review:ok"}, {"name": "ai:ok"}, {"name": "conflict"}]
    assert rl.merge_label_gate(labels) is None
    reason = rl.merge_label_gate([{"name": "review:ok"}])
    assert reason is not None and "ai:ok" in reason


def test_merge_gate_reason_names_missing_label():
    reason = rl.merge_label_gate(["ai:ok"])
    assert "review:ok" in reason


def test_ai_verdicts_to_drop():
    assert rl.ai_verdicts_to_drop(["review:ok"]) == []
    assert rl.ai_verdicts_to_drop(["review:ok", "ai:ok"]) == ["ai:ok"]
    assert rl.ai_verdicts_to_drop(["ai:changes-requested", "ai:failed"]) == \
        ["ai:changes-requested", "ai:failed"]
    assert rl.ai_verdicts_to_drop([{"name": "ai:ok"}]) == ["ai:ok"]


# ── Маскирование: тот же sed, что у bash-транспортов ─────────────────────────

def test_redact_masks_model_provider_keys():
    text = "вот ключ sk-abcdefgh12345678 и nvapi-abcdefgh из ответа"
    out = ai.redact(text)
    assert "sk-abcdefgh12345678" not in out
    assert "sk-[REDACTED]" in out
    assert "nvapi-abcdefgh" not in out


def test_redact_plain_text_untouched():
    assert ai.redact("обычный текст ревью без секретов") == "обычный текст ревью без секретов"
