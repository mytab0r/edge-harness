#!/usr/bin/env bash
# Гвардия обязательного локального гейта гвардий перед push (issue #1280):
# доказывает поведение РЕАЛЬНЫХ .githooks/pre-push +
# scripts/lib/pre_push_guard_gate.py на живом git-репозитории (4 случая:
# зелёная/красная гвардия, аварийный выход, третье состояние) —
# scripts/git/test/pre-push-guard-gate.test.sh, не пересказ поведения.
set -euo pipefail
bash scripts/git/test/pre-push-guard-gate.test.sh
pip install --quiet pytest
python -m pytest scripts/lib/test_pre_push_guard_gate.py -q
