#!/usr/bin/env bash
# Носитель мутационного теста гвардии литерала namespace провайдера (#1069,
# ревью PR #1117, находка 2): сам `.test.mjs` лежит в `dsh-edge/` — вне
# директории `test/` канарейка осиротевших тестов его не видит; файл-носитель
# в `test/` делает удаление обёртки
# scripts/ci/guards/verify-provider-namespace-guard.sh красным.
set -euo pipefail
node --test dsh-edge/verify-provider-namespace.test.mjs
