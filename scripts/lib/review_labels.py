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

# ── Конфликт (mark_conflicts, scheduler.py) ──────────────────────────────────
# Единственное определение (было задублировано локальной константой в
# scheduler.py) — should_update_branch ниже читает её же.
CONFLICT_LABEL = "conflict"


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


def should_update_branch(labels) -> bool:
    """Газ выборочного подтягивания веток (#252): true — обновлять ветку из
    main стоит, false — нет.

    Раньше scheduler.update_remaining_pulls дёргал gh pr update-branch для
    ВСЕХ открытых недрафт PR после каждого слияния (до 96 запусков оркестратора
    в сутки, cron */15) — тот же вызов и для PR, отставшего в merge_queue.
    Каждый update-branch — это push в чужую ветку → GitHub шлёт
    pull_request:synchronize → pr-review.yml перезапускается → снимает все
    ai:*-метки (ai_verdicts_to_drop выше) → при review:ok стартует дорогое
    ai-review.yml. PR, которому рано сливаться (нет вердиктов, в доработке,
    ai:changes-requested), от этого не выигрывает ничего — только теряет
    валидный вердикт и жжёт AI-квоту вхолостую (замер #252: 142 прогона
    ai-review.yml за 14.5 ч, из них подавляющее большинство — merge-коммиты
    от update-branch, а не новый пуш автора).

    Обновлять стоит только два случая:
      1. оба вердикта уже зелёные (merge_label_gate(labels) is None) —
         PR реально близок к слиянию, следующий обход merge_queue его сольёт,
         и свежий head ему нужен;
      2. PR уже помечен CONFLICT_LABEL — подтягивание из main может расшить
         конфликт (только оно и способно).
    Во всех остальных случаях (нет вердиктов, review:changes-requested,
    ai:changes-requested, ai:failed без обоих ok) — подтягивание пропускается.

    ГАЗ (обязателен, автоматический, см. AGENTS.md «тормоз без газа не
    принимается»): предикат не хранит собственного состояния — он на лету
    читает текущие labels PR. Как только детерминированное и AI-ревью
    проставят оба вердикта (или mark_conflicts повесит CONFLICT_LABEL),
    САМЫЙ СЛЕДУЮЩИЙ прогон оркестратора (update_remaining_pulls после
    следующего слияния или behind-ветка merge_queue) увидит новые labels и
    снова начнёт подтягивать этот PR — без ручного вмешательства. Тормоз и
    газ — одно и то же чтение labels, разнесённое по времени.
    """
    names = _names(labels)
    if CONFLICT_LABEL in names:
        return True
    return merge_label_gate(names) is None


def ai_verdicts_to_drop(labels) -> list[str]:
    """ai:*-метки, которые детерминированное ревью снимает перед своим новым
    вердиктом. Вердикт AI действителен только для head, на котором сделан:
    новый пуш запускает новое AI-ревью, а старая метка не должна доживать
    до окна слияния (класс «протухшая метка открывает гейт»).
    """
    names = _names(labels)
    return [label for label in AI_VERDICTS if label in names]
