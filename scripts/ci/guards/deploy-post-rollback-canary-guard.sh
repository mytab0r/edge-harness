#!/usr/bin/env bash
# Гвардия #1170: шаг «Канарейка после автооткота» обязан падать (exit != 0),
# когда прод после автоотката НЕ отвечает 200 — иначе провал восстановления
# виден только аннотацией `::error::` в логе, а сам шаг/job остаётся success
# («Тормоз без газа», AGENTS.md).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_deploy_post_rollback_canary_guard.py -q
