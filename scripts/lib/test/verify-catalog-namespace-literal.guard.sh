#!/usr/bin/env bash
# Носитель мутационного теста гвардии «каталог плагинов не отравляет литерал
# namespace» (#1069, ревью PR #1117, находка 2 — тот же механизм, что
# verify-provider-namespace.guard.sh): `.test.mjs` в `dsh-edge/` канарейке не
# виден, файл-носитель в `test/` — виден.
set -euo pipefail
node --test dsh-edge/verify-catalog-namespace-literal.test.mjs
