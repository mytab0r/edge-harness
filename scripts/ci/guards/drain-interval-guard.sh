#!/usr/bin/env bash
# Issue #1048: интервал фонового дрена морды (DRAIN_INTERVAL_SECS) не должен
# молча вернуться к 1с — старому значению, при котором почти каждое событие
# транскрипта оплачивало полный ре-скан истории сессии в dsh-edge (замер
# #678, docs/research/20-cloudflare-free.md). Подробности класса и мутации —
# докстринг scripts/lib/test_drain_interval_guard.py.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_drain_interval_guard.py -q
