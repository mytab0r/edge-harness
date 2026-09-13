#!/usr/bin/env bash
# Гвардия #945 (закрывает #661 вариант 1, #664): авто-PR форжа сливаем без
# человека — ветка по контракту PR↔задача, task_issue обязателен, push+
# schedule триггеры на месте, текст авто-PR не врёт про триггер деплоя.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_plugin_forge_auto_pr_guard.py -q
