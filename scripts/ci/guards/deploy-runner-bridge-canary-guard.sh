#!/usr/bin/env bash
# Гвардия #945: деплой обязан нести живую канарейку runner-bridge (реальный
# вызов runner_status через session.prompt против GitHub REST, не только
# «морда жива») — и НЕ дёргать runner_task автоматически на каждом деплое.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_deploy_runner_bridge_canary_guard.py -q
