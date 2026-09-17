#!/usr/bin/env bash
# Миграция задач-хвостов «Хвост чеклиста ревью PR #N» в реестр находок
# (#1262, этап 2 — openspec/changes/review-findings-registry/tasks.md).
# Гвардия покрывает чистую логику (extract_findings/guess_exact_file/
# plan_migration) на реальных телах хвостов (fixtures_tail_*.json, сняты
# gh api repos/mytab0r/edge-harness/issues/<N> — прод-форма, не пересказ) —
# сеть не нужна.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_migrate_review_findings.py -q
