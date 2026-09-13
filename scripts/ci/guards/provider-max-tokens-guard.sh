#!/usr/bin/env bash
# Гвардия класса #1062 (живой инцидент — прогон worker.yml 34730173870):
# config/provider-usage.json не несёт max_output_tokens выше ПОДТВЕРЖДЁННОГО
# потолка ответа модели (scripts/lib/confirmed-provider-models.json::
# confirmed_max_output_tokens) — регрессионный свидетель на реальном
# манифесте плюс синтетическая мутация над потолком, см.
# scripts/lib/test_provider_max_tokens_check.py.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_provider_max_tokens_check.py -q
