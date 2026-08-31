#!/usr/bin/env python3
"""Метки-вердикты конвейера ревью — одно место правды для всех потребителей.

Два гейта, две независимые метки (задача #18):

  Гейт 1 — детерминированное ревью (scripts/review/check_pr.py, workflow
  pr-review): review:ok / review:changes-requested, размерный гейт
  review:large с осознанным обходом review:large-ok.

  Гейт 2 — AI-ревью диффа (scripts/review/ai_review.py, workflow ai-review):
  ai:ok / ai:changes-requested / ai:failed.

Слияние (scheduler.merge_queue) требует review:ok И ai:ok. Вердикт AI
привязан к head, который ревьюили: детерминированное ревью при каждом своём
запуске (новый пуш) снимает все ai:*-метки — протухший вердикт не может
открыть слияние (см. ai_verdicts_to_drop).

Импорт из соседних каталогов — importlib по файлу (паттерн claim_task):
скрипты запускаются как файлы, не как пакет.
"""

# ── Гейт 1: детерминированное ревью ──────────────────────────────────────────
REVIEW_OK = "review:ok"
REVIEW_CHANGES = "review:changes-requested"
REVIEW_LARGE = "review:large"
LARGE_OK = "review:large-ok"

# ── Гейт 2: AI-ревью ─────────────────────────────────────────────────────────
AI_OK = "ai:ok"
AI_CHANGES = "ai:changes-requested"
AI_FAILED = "ai:failed"
AI_VERDICTS = (AI_OK, AI_CHANGES, AI_FAILED)


def _names(labels) -> set[str]:
    """Имена меток из любой прод-формы: список dict'ов API или множество имён."""
    if isinstance(labels, str):
        return {labels}
    try:
        return {label["name"] for label in labels}
    except TypeError:
        return set(labels)


def merge_label_gate(labels) -> str | None:
    """Причина, по которой метки запрещают слияние; None — метки слияние открыли.

    Единственное место, где гейт слияния формулируется словами: scheduler
    печатает причину в отчёт, тесты доказывают обе ветки. Порог «оба гейта
    зелёные» — здесь, а не в вызывающем коде.
    """
    names = _names(labels)
    if REVIEW_OK not in names:
        return f"нет вердикта {REVIEW_OK} (ждёт детерминированное ревью или доработку)"
    if AI_OK not in names:
        return f"нет вердикта {AI_OK} (ждёт AI-ревью, доработку или повтор после сбоя)"
    return None


def ai_verdicts_to_drop(labels) -> list[str]:
    """ai:*-метки, которые детерминированное ревью снимает перед своим новым
    вердиктом. Вердикт AI действителен только для head, на котором сделан:
    новый пуш запускает новое AI-ревью, а старая метка не должна доживать
    до окна слияния (класс «протухшая метка открывает гейт»).
    """
    names = _names(labels)
    return [label for label in AI_VERDICTS if label in names]
