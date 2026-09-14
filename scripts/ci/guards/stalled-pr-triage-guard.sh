#!/usr/bin/env bash
# Триаж застрявшего PR (ADR 0021/0024, #1218): величины 1,2,3,7 считаются
# git'ом/AST, не руками — на настоящем временном git-репозитории для
# measure_*/ensure_unshallow, чистые функции для decide(). Новый шаг CI
# заводится сразу в каталоге гвардий (#749), не рукописной строкой в
# repo-ci.yml.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_stalled_pr_triage.py -q
