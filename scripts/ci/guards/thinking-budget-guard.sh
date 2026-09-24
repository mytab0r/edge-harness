#!/usr/bin/env bash
# Гвардия #1533: конвейер не шлёт `thinking: enabled` без `budget_tokens`.
# Проверяется СОБРАННЫЙ профиль (и живой исходящий запрос там, где есть dsh),
# а не текст dsh-ci.sh: структурная проверка покрасилась бы зелёным и на
# закомментированной строке, и на голом `off`, который YAML читает булевым.
set -euo pipefail
python -m pytest scripts/lib/test_thinking_budget_guard.py -q
