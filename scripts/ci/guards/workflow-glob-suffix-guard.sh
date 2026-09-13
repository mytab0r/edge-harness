#!/usr/bin/env bash
# Гвардия класса «скан каталога .github/workflows по одному суффиксу» (#635):
# GitHub Actions грузит workflows из .yml И .yaml — скан одним суффиксом
# пропускает *.yaml мимо (урок жил лишь комментарием в
# scripts/lib/test_dispatch_token_usage.py:35-36 и оставался живым в трёх
# местах). scripts/lib/workflow_glob_suffix_guard.py — семантический (ast)
# разбор, не grep по подстроке. Зарегистрирована файлом каталога (#749),
# не рукописным шагом repo-ci.yml.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_workflow_glob_suffix_guard.py -q
python scripts/lib/workflow_glob_suffix_guard.py
