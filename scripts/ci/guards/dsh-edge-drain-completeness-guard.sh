#!/usr/bin/env bash
# Issue #1048: доказательство, что финальный (hard) слив спула в морду
# по-прежнему выносит транскрипт ЦЕЛИКОМ при увеличенном DRAIN_INTERVAL_SECS
# — главный риск повышения интервала. Подробности — шапка
# scripts/lib/test/dsh-edge-drain-completeness.sh.
set -euo pipefail
bash scripts/lib/test/dsh-edge-drain-completeness.sh
