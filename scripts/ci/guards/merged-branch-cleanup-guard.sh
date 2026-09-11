#!/usr/bin/env bash
# Уборка веток `agent/*` после слияния/закрытия PR (#940): чистая функция
# `branch_deletion_candidates` (retention, приоритет open > merged/closed,
# guard `agent/`-префикса) + поведенческий тест на настоящем bare-репозитории
# (`delete_branch` реально снимает ref через `git push --delete`), не текст
# исходника — переименование/вырезание условия красит тест, не просто
# отсутствие имени функции.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_merged_branch_cleanup_guard.py -q
