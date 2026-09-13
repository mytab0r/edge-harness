#!/usr/bin/env bash
# Реестр «мерж → реагирующий workflow» + единая функция диспатча с дедупом
# по head_sha (issue #955, следствие #929): scripts/lib/merge_reactions.py.
# Регистрация в каталоге гвардий #749 сразу (не рукописный шаг repo-ci.yml).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_merge_reactions.py -q
