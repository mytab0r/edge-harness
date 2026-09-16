#!/usr/bin/env bash
# Классификация класса дефекта в источнике (#1237): контракт КЛАСС в
# ai_prompt.md, словарь известных/кандидатов defect_classes.py, третье
# состояние (не назван/кандидат/известен — тот же принцип, что
# scripts/lib/check_result.py, #1096). Чистая логика + синтетический
# gh_func, без сети — та же дисциплина, что у test_ai_review.py.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/review/test_defect_classes.py -q
