#!/usr/bin/env bash
# Дайджест мягких отказов (#1121): деградация, оформленная как штатная
# работа (::warning:: в зелёном прогоне, редкий условный шаг), не видна
# ничем месяцами. Гвардия покрывает оба канала (аннотации Checks API +
# условные шаги по `if:` исходника workflow), дедуп по открытым issues
# `soft-failure` и мутационное доказательство фильтра check_suite_id — на
# моке gh, кормится прод-формой живых аннотаций репозитория (сеть не нужна).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_soft_failure_digest.py -q
