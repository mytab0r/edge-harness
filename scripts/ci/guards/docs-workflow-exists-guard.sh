#!/usr/bin/env bash
# Обёртка гвардии «workflow из docs существует» из каталога (#749, #1069;
# исходный шаг нёс инлайн heredoc-python без отдельного файла — та же причина
# ручного переноса, что у workflow-concurrency-guard.sh). Тело и его
# докстринг — в носителе scripts/lib/test/docs-workflow-exists.guard.sh.
set -euo pipefail
# Тело (инлайн heredoc-python) вынесено в носитель
# scripts/lib/test/docs-workflow-exists.guard.sh (#1069, ревью PR #1117,
# находка 2) — см. workflow-concurrency-guard.sh.
bash scripts/lib/test/docs-workflow-exists.guard.sh
