#!/usr/bin/env bash
# Канал наблюдаемости уборки рабочих деревьев (#1250, п.4): гвардия
# scripts/lib/test_worktree_snapshot.py — поведение транспорта записи на
# data-ветку (bare origin + реальный push, антидубль по факту с сервера),
# чистой логики записи и замера устаревших копий гвардий. Зарегистрирована
# каталогом (#749), не рукописным шагом repo-ci.yml.
#
# Внимание: соседний scripts/ci/guards/worktree-cleanup-guard.sh гоняет
# ПОВЕДЕНЧЕСКУЮ гвардию самого уборщика (test_worktree_cleanup_guard.py),
# включая CLI-сценарий публикации end-to-end; этот файл — модульные тесты
# модуля канала, второго носителя того же поведения не заводим.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_worktree_snapshot.py -q
