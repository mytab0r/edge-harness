#!/usr/bin/env bash
# Носитель тела гвардии бита исполнения (#1069, ревью PR #1117, находка 2):
# файл в директории `test/` канарейка осиротевших тестов видит и краснеет при
# удалении обёртки scripts/ci/guards/exec-bit-guard.sh — прямой
# `python scripts/lib/exec_bit_guard.py` для её логики покрытия невидим
# (не pytest/node --test/bash-файл). Удалишь обёртку — канарейка красит.
set -euo pipefail
python scripts/lib/exec_bit_guard.py
