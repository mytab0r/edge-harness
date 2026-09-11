#!/usr/bin/env bash
# Гвардия класса «сетевой снимок (`git ls-remote`, `gh api`) под
# `set -euo pipefail` без обработки отказа» (находка ai-review PR #937,
# задача #935, маркер расширен вторым проходом ревью того же PR, скан
# расширен на экстеншн-less production-скрипты третьим проходом): отказ
# сети/API роняет job голой bash-ошибкой, без ::error::, без отчёта о
# прогоне. Доказано мутацией — см. заголовок файла теста.
set -euo pipefail
python -m pytest scripts/lib/test_worker_network_snapshot_guard.py -q
