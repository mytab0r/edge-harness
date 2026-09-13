#!/usr/bin/env bash
# Гвардия #1123 (находка 1 из #1121): автооткат прода обязан целиться в
# последнюю ИЗВЕСТНО-ХОРОШУЮ версию (снятую ДО деплоя этого прогона), а не в
# «предыдущую по created_on» — эта эвристика апстрима вслепую откатывала прод
# на версию, созданную секундами раньше ЭТИМ ЖЕ прогоном (wrangler secret put
# создаёт новую 100%-версию на каждый секрет). Живая улика: run 34750000094 —
# откат уехал на версию секрета GH_RUNNER_TOKEN, а не на прошлый релиз.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_deploy_prod_rollback_target_guard.py -q
