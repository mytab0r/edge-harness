#!/usr/bin/env bash
# Гвардия носителя третьего состояния (issue #1096): наблюдатель, который не
# смог посмотреть, обязан сказать ❓ (check_result.unknown), а не тихо
# схлопнуться в 💚 (check_result.ok()) через `except RuntimeError: return []`.
# scan_silent_except — узкая AST-проверка (см. докстринг check_result.py),
# область — только уже мигрированные функции; не общий линт по всему
# repo_invariants.py (это работа шага 2, отдельная задача — иначе гвардия
# красила бы CI на КАЖДОМ ещё не мигрированном инварианте до его миграции).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_check_result.py -q
python -m pytest scripts/lib/test_check_result_migrations.py -q
