#!/usr/bin/env bash
# Гвардия входа `slot` worker.yml (#827, находка ai-review PR #831): свободная
# `type: string` принимала недопустимые слоты (например 3) — концу
# concurrency-группа `worker-3` не учитывается WORKER_MAX_CONCURRENCY=2 в
# scheduler.py (диапазон слотов 1..2 в free_worker_slot). Гвардия читает
# исходник worker.yml (yaml.safe_load) и красит регресс на `type: string` —
# доказано мутацией (снятие `type: choice` краснит все три теста).
#
# Регистрируется каталогом (#749), не рукописным шагом repo-ci.yml —
# ci-guard-registration.sh замораживает список рукописных шагов, новый шаг
# здесь провалил бы её.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_worker_slot_input_guard.py -q
