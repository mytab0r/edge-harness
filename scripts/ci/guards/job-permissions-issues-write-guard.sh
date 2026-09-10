#!/usr/bin/env bash
# Класс «job-level permissions заменяет workflow-level целиком, issues:
# write теряется молча» (#884, живой прогон 34520146758, PR #870, job
# `verdict` — комментарий в WATCHDOG_ISSUE #120 упал 403 при зелёном
# job'е). Мутация: убрать issues: write из permissions job'а `verdict`
# в ai-review.yml — тест краснеет с именем job'а.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_job_permissions_issues_write_guard.py -q
