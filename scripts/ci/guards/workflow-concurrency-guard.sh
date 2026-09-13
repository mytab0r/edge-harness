#!/usr/bin/env bash
# Обёртка гвардии concurrency из каталога (#749, #1069; исходный шаг
# «Гвардия concurrency — не сериализовать разные джобы одной статической
# группой» нёс инлайн heredoc-python без отдельного файла). Тело и его
# докстринг — в носителе scripts/lib/test/workflow-concurrency.guard.sh.
set -euo pipefail
# Тело (инлайн heredoc-python) вынесено в носитель
# scripts/lib/test/workflow-concurrency.guard.sh (#1069, ревью PR #1117,
# находка 2): без файла-носителя удаление обёртки было бы молчаливым.
bash scripts/lib/test/workflow-concurrency.guard.sh
