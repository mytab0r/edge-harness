#!/usr/bin/env bash
# Снимок наблюдаемого состояния задача/PR (#1287, orchestrator-core-v2,
# tasks.md Этап 0, ходячий скелет): build_observed_pr/build_observed_task
# на реальных фикстурах (gh api pulls/1290, issues/1287), edge-triggered
# запись гейтится тем же _guard_raw_subprocess_write, что update_branch.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_snapshot_walking_skeleton.py -q
