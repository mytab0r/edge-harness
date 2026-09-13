#!/usr/bin/env bash
# Номер ADR/research-документа не назначается вручную без арбитра (#1078,
# см. scripts/lib/decision_numbering.py — докстринг несёт полный класс и
# живые случаи коллизии). Каталог гвардий (#749) — регистрация файлом, без
# правки repo-ci.yml.
#
# Два шага:
#   1. pytest — чистые функции (мутируемо, см. докстринг test_decision_
#      numbering.py) + поведенческий тест на настоящем временном
#      git-репозитории (build_origin_with_number_collision).
#   2. Живая проверка `decision_numbering.py check` на ТЕКУЩЕМ репозитории:
#      main ∪ все открытые PR прямо сейчас. Красит CI, если два источника
#      подставили под один номер разные файлы — до мержа, а не после.
#
# GH_TOKEN не объявляется здесь: приходит от шага-перебора «Каталог гвардий
# scripts/ci/guards — перебор (#749)» в repo-ci.yml (env: GH_TOKEN уровня
# ЭТОГО шага) — тот же приём, что worktree-cleanup-guard.sh/stale-blocked-
# guard.sh. `git fetch` внутри decision_numbering.py читает публичный
# репозиторий (origin уже настроен actions/checkout) — своего токена не
# требует, но GITHUB_REPOSITORY (для gh api .../pulls?state=open) нужен и
# уже экспортирован раннером Actions безусловно.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_decision_numbering.py -q
python scripts/lib/decision_numbering.py check
