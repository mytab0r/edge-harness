#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#1069,
# ревью PR #1117, находка 1: шаг «Оркестрация без keyword-аргументов gh()» —
# гвардия-инвариант репозитория по критерию задачи, жившая только
# рукописным шагом job `test`). Тело — в носителе
# scripts/lib/test/gh-keyword-args.guard.sh — см.
# pool-issue-create-guard.sh.
set -euo pipefail
bash scripts/lib/test/gh-keyword-args.guard.sh
