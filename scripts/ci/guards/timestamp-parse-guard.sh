#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия разбора метки времени — новое место мимо общего хелпера»
# добавлен независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Класс «TypeError на вычитании aware-naive datetime, пойманный только
# ValueError» (#780, доводка #779): второе место повторило узкую версию
# первого. review_labels.parse_github_timestamp — единственное место
# разбора такой метки; гвардия ловит новое сырое datetime.fromisoformat(
# в review_labels.py/ai_review.py, открытое в обход хелпера.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_timestamp_parse_guard.py -q
