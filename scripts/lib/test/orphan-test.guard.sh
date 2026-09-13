#!/usr/bin/env bash
# Носитель тела гвардии осиротевших тестов (#1069, ревью PR #1117, находка 2 —
# тот же механизм, что exec-bit.guard.sh): без файла-носителя в `test/`
# удаление обёртки scripts/ci/guards/orphan-test-guard.sh не красил бы ничто.
set -euo pipefail
python scripts/lib/orphan_test_guard.py
