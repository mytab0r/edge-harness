#!/usr/bin/env python3
"""Тесты review_labels.py — единственного места правды для гейта слияния и
для выборочного подтягивания веток (#252).

fixtures_open_pulls_252.json — прод-форма, не пересказ: реальный ответ
`gh api "repos/mytab0r/edge-harness/pulls?state=open&per_page=100"` этого же
репозитория, снятый 2026-09-03 (после удаления собственного PR #252, чтобы
не засорять снимок). Поля сужены до number/draft/labels через `--jq`
(значения — те же самые, что вернул API, без пересказа) — полные объекты
несут в title/body случайные упоминания моделей из соседних задач, которые
ложно бьют guard'а #153 (стейл-провайдер/модель), а тесту здесь нужны только
labels. Тест разбирает то, что система реально отдаёт, а не наше
представление о формате.

Запуск: python -m pytest scripts/lib/test_review_labels.py -q
"""

import importlib.util
import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "review_labels.py"
spec = importlib.util.spec_from_file_location("review_labels", SCRIPT)
review_labels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_labels)  # type: ignore[union-attr]

FIXTURE = _DIR / "fixtures_open_pulls_252.json"


def _load_pulls() -> list[dict]:
    with open(FIXTURE, encoding="utf-8") as file:
        return json.load(file)


# ── Гвардия #252 на прод-форме: только близкие к слиянию или в конфликте ─────────
# Снимок содержал (2026-09-03): #162/#230/#231/#237 — метка conflict
# (подтягивать, может расшить); ни у одного открытого PR на момент снимка не
# было ОБОИХ вердиктов review:ok+ai:ok разом (см. отдельный синтетический
# тест ниже для этой ветки предиката) — #247/#248/#249/#253/#241/#181/#173 и
# другие стоят с review:large/ai:failed/ai:changes-requested/contract:failed
# без обоих зелёных (не подтягивать — дорогое AI-ревью и сброс вердикта без
# пользы для PR, которому рано сливаться).
EXPECTED_SHOULD_UPDATE = {162, 230, 231, 237}


def test_should_update_branch_matches_expected_on_real_open_pulls():
    pulls = _load_pulls()
    numbers = {pull["number"] for pull in pulls}
    # Гвардия свежести самого теста: если состав открытых PR в фикстуре
    # изменится (новый снимок), список ожиданий не должен молча протухнуть.
    assert numbers == {
        263, 262, 261, 260, 253, 249, 248, 247, 241, 237, 231, 230, 181, 173, 167, 162, 108,
    }

    should_update = {
        pull["number"] for pull in pulls
        if review_labels.should_update_branch(pull["labels"])
    }
    assert should_update == EXPECTED_SHOULD_UPDATE


def test_should_update_branch_true_only_for_conflict_or_both_verdicts():
    pulls = _load_pulls()
    for pull in pulls:
        names = {label["name"] for label in pull["labels"]}
        expected = ("conflict" in names) or (
            review_labels.REVIEW_OK in names and review_labels.AI_OK in names
        )
        actual = review_labels.should_update_branch(pull["labels"])
        assert actual == expected, f"#{pull['number']}: labels={sorted(names)}"


def test_should_update_branch_true_when_both_verdicts_green_without_conflict():
    # На снимке фикстуры такого PR нет (см. комментарий у EXPECTED_SHOULD_UPDATE) —
    # ветка предиката «близок к слиянию» проверяется синтетически, не прод-формой.
    labels = [{"name": "review:ok"}, {"name": "ai:ok"}]
    assert review_labels.should_update_branch(labels) is True


def test_should_update_branch_accepts_label_name_set_and_dict_list():
    # _names() поддерживает обе прод-формы: список dict'ов API и множество имён
    # (см. review_labels._names) — предикат обязан работать с обеими.
    dict_form = [{"name": "conflict"}]
    set_form = {"conflict"}
    assert review_labels.should_update_branch(dict_form) is True
    assert review_labels.should_update_branch(set_form) is True
    assert review_labels.should_update_branch([{"name": "review:ok"}]) is False
    assert review_labels.should_update_branch({"review:ok"}) is False


def test_should_update_branch_false_on_empty_labels():
    assert review_labels.should_update_branch([]) is False
    assert review_labels.should_update_branch(set()) is False


# ── Мутация гвардии: снять фильтр — тест обязан покраснеть ───────────────────────
# Доказательство держится руками в отчёте задачи #252 (правка should_update_branch
# на `return True` роняет test_should_update_branch_matches_expected_on_real_open_pulls
# и test_should_update_branch_true_only_for_conflict_or_both_verdicts), не тут:
# постоянная мутация в файле теста была бы сама по себе живым багом.
