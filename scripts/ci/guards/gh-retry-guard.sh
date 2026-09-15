#!/usr/bin/env bash
# Регистрация гвардии данными (#749/#1069, orphan-test-guard): без шага CI
# новый тест-файл не подключён ни одним workflow — orphan-test-guard красит
# job `test` громко, а не молча.
#
# issue #770: gh api, оборвавшийся посреди прогона, повторяется с выдержкой
# только для класса TRANSIENT — scripts/lib/gh_retry.py, одно место правды
# на весь репозиторий, вшито в ai_review.py::gh()/run_gh().
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_gh_retry.py -q
