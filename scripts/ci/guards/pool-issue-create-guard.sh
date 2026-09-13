#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#1069,
# ревью PR #1117, находка 1: шаг «Заведение issue пула — только через
# pool_issue.create_pool_issue (класс #179/#526)» — гвардия-инвариант
# репозитория по критерию задачи, жившая только рукописным шагом job `test`).
# Тело — в носителе scripts/lib/test/pool-issue-create.guard.sh: файл в
# директории `test/` канарейка осиротевших тестов видит, удаление этой
# обёртки красит (класс «снять запись из каталога → должно покраснеть»,
# #749).
set -euo pipefail
bash scripts/lib/test/pool-issue-create.guard.sh
