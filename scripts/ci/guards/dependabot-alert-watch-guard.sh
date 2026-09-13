#!/usr/bin/env bash
# Алерты Dependabot → задача пула (сирота B, аудит владельца 2026-09-11):
# `git grep "dependabot/alerts"` по scripts/ был пуст до этого модуля.
# Гвардия покрывает дедуп по номеру алерта, суточный потолок и газ
# (закрытие задачи по решённому алерту) на моке gh, кормится прод-формой
# живого алерта репозитория (сеть не нужна).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_dependabot_alert_watch.py -q
