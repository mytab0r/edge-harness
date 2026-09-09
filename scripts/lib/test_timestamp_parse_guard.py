#!/usr/bin/env python3
"""Гвардия класса «TypeError на вычитании aware-naive datetime, пойманный
только ValueError» (#780, доводка второго гейта #779).

Класс: `datetime.fromisoformat(raw.replace("Z", "+00:00"))` ПАРСИТ метку
времени БЕЗ offset ("Z"/"+HH:MM") как наивный datetime, не бросая ValueError
— падение приходит НИЖЕ по коду, на `aware - naive` вычитании/сравнении.
Первое закрытие класса (`review_labels.other_active_ai_review_runs`, #779)
ловило только `except (ValueError, TypeError)` СВОЕЙ копией try/except;
второе место, введённое ЭТИМ ЖЕ PR (#779) — `ai_review._verdict_label_and_age`
— сперва ловило только ValueError и падало на живом прогоне приёмки
(доводка #780). Копия try/except на каждое новое место — отложенный
рецидив (AGENTS.md «одно место правды»): `review_labels.parse_github_timestamp`
теперь единственное место разбора такой метки, и оба вызывающих зовут его,
а не открывают свою копию.

Эта гвардия проверяет, что новое сырое разбирание метки времени в этих ДВУХ
файлах (тех же, что закрыли класс) не появится незамеченным: число прямых
вызовов `datetime.fromisoformat(` в каждом файле зафиксировано, и рост числа
красит тест — значит, кто-то открыл новую копию вместо вызова хелпера.

Не проверяет остальной репозиторий (claim_task.py, pulse_guard.py,
dispatch_tail.py, label_churn_203.py, test_upstream_drift.py и другие несут
тот же паттерн вне review_labels.py/ai_review.py, но не тронуты #779/#780 —
отдельный, более широкий прочёс, не предмет этой правки).

Запуск: python -m pytest scripts/lib/test_timestamp_parse_guard.py -q
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FROMISO_RE = re.compile(r"datetime\.fromisoformat\(")


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def test_review_labels_raw_fromisoformat_count_is_pinned():
    # Три разрешённых вхождения, поимённо:
    #   1) докстринг parse_github_timestamp — упоминание паттерна текстом,
    #      не вызов (объясняет класс, который функция закрывает).
    #   2) сам вызов внутри тела parse_github_timestamp — единственное
    #      МЕСТО РАЗБОРА, вызывающие обязаны звать функцию, а не дублировать.
    #   3) latest_trusted_comment (`created_at <= since`) — ИЗВЕСТНЫЙ,
    #      поименованный пробел: тот же класс, но существовал ДО #779,
    #      не введён этой правкой, закрытие вынесено за пределы доводки
    #      #780 осознанно (см. находку 1 доводки #780). Не «забыт» — назван.
    text = _read("scripts/lib/review_labels.py")
    assert 'datetime.fromisoformat(created_at.replace("Z", "+00:00")) <= since' in text, (
        "latest_trusted_comment больше не содержит ожидаемый сырой вызов — "
        "если его закрыли через parse_github_timestamp, снизь ожидаемое число "
        "ниже с 3 до 2 и удали этот assert.")
    actual = len(FROMISO_RE.findall(text))
    assert actual == 3, (
        f"scripts/lib/review_labels.py: найдено {actual} сырых "
        "datetime.fromisoformat(, ожидалось 3 (докстринг + тело "
        "parse_github_timestamp + известный пробел latest_trusted_comment "
        "#576) — новое место обязано звать review_labels.parse_github_timestamp, "
        "не открывать свою копию try/except.")


def test_ai_review_has_no_raw_fromisoformat_call():
    # ai_review.py вообще не должен разбирать метку времени сам — единственный
    # разбор идёт через review_labels.parse_github_timestamp (#780).
    text = _read("scripts/review/ai_review.py")
    assert FROMISO_RE.search(text) is None, (
        "scripts/review/ai_review.py: найден сырой datetime.fromisoformat( — "
        "новое место обязано звать review_labels.parse_github_timestamp, а не "
        "открывать свою копию try/except (класс #780/#779).")
