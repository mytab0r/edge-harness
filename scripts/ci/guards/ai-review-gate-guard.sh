#!/usr/bin/env bash
# Перенесено из .github/workflows/repo-ci.yml транслятором рукописных
# шагов гвардий (issue #897, продолжение #749/#771/#762/#764): исходный
# шаг «Гвардия гейта первого ревью ai-review.yml (#204)» перенесён сюда автоматически, при механическом
# ребейзе, без содержательной правки run: — исходный комментарий шага
# (если был) приведён ниже дословно.
#
# Гейт первого ревью в ai-review.yml (#204, второй заход): шаг `facts`
# решает `go=true/false` bash-логикой ВНУТРИ самого workflow — тест
# исполняет РЕАЛЬНЫЙ `run:`-скрипт этого шага, не пересказ на python.
# Лежал без единого шага CI (находка канарейки осиротевших тестов,
# #583) — тот же класс, что закрывали test_waiting_owner_guard.py и
# test_apply_owner_decision.py выше по этому же файлу.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/review/test_ai_review_gate.py -q
