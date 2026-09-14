#!/usr/bin/env bash
# Гвардия задачи issue #925: «прогон workflow относится к main, а не к
# зелёной ветке-кандидату» (scripts/lib/ci_run_on_main.py) + инвариант 19
# (scripts/orchestra/repo_invariants.py::check_ci_failure_closed_but_main_red).
#
# Живой случай: три подряд «зелёных» прогона deploy-worker.yml
# (2026-09-12T00:06–00:17Z) были workflow_dispatch на ветке
# agent/678-rows-written-namespace (GitHub Compare API main...<sha> →
# diverged, не предок main), а последний прогон push'а в main
# (2026-09-11T14:24Z, слияние PR #943) — красный. Критерий «следующий прогон
# зелёный» эти два случая не различал.
#
# Зарегистрирована файлом каталога (#749), не рукописным шагом repo-ci.yml —
# .github/workflows/* не тронуты ни строкой (заняты параллельной работой,
# PR #1033/#1030/#831 и др.).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_ci_run_on_main.py -q
