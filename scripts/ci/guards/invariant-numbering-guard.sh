#!/usr/bin/env bash
# Номер инварианта repo_invariants.py не назначается вручную без арбитра
# (#904, см. scripts/lib/invariant_numbering.py — докстринг несёт полный
# класс и живой случай коллизии на номере 19 между PR #1061/#1136,
# найденный этим же PR). Каталог гвардий (#749) — регистрация файлом, без
# правки repo-ci.yml.
#
# Два шага:
#   1. pytest — чистые функции (мутируемо, см. докстринг test_invariant_
#      numbering.py) + поведенческий тест на настоящем временном
#      git-репозитории с ДОСЛОВНОЙ фикстурой живой коллизии #904.
#   2. Живая проверка `invariant_numbering.py check` на ТЕКУЩЕМ репозитории:
#      main ∪ все открытые PR прямо сейчас, сужено к текущей ветке (тот же
#      газ, что у decision-doc-numbering-guard.sh — посторонний PR не
#      обязан чинить чужую коллизию).
#
# GH_TOKEN не объявляется здесь: приходит от шага-перебора «Каталог гвардий
# scripts/ci/guards — перебор (#749)» в repo-ci.yml (тот же приём, что
# decision-doc-numbering-guard.sh). `git fetch` внутри invariant_numbering.py
# читает публичный репозиторий (origin уже настроен actions/checkout) — своего
# токена не требует, но GITHUB_REPOSITORY (для gh api .../pulls?state=open)
# нужен и уже экспортирован раннером Actions безусловно.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_invariant_numbering.py -q
python scripts/lib/invariant_numbering.py check
