#!/usr/bin/env bash
# Носитель тела гвардии «заведение issue пула только через pool_issue»
# (#1069, ревью PR #1117, находка 1 — шаг мигрирован из repo-ci.yml в каталог
# этим PR; тело run: перенесено дословно). Файл в директории `test/` канарейка
# осиротевших тестов видит: удаление обёртки
# scripts/ci/guards/pool-issue-create-guard.sh красит, а не молчит.
#
# Класс #179/#526: сырой POST repos/{repo}/issues вне pool_issue.py —
# рецидив, красит CI.
set -euo pipefail
if grep -rn 'repos/{repo}/issues"' scripts/orchestra/ scripts/lib/ scripts/review/ --include=*.py | grep -v 'scripts/lib/pool_issue.py'; then
  echo "::error::сырой POST repos/{repo}/issues вне pool_issue.py (класс #179/#526) — используй pool_issue.create_pool_issue, иначе task можно забыть"
  exit 1
fi
echo "orchestra+lib+review: заведение issue пула только через pool_issue.create_pool_issue"
