#!/usr/bin/env bash
# Гвардия класса #786/#840: gh 2.85 не знает --body-file у gh secret set /
# gh variable set (unknown flag) — значение только через stdin. Гвардия по
# исходнику: ни один вызов этих подкоманд в scripts/ не несёт --body-file.
# Обёртки gh issue create/gh pr create не задеты — та же опция у ДРУГИХ
# подкоманд gh, где она рабочая.
#
# Регистрируется сразу в каталоге гвардий (#749), не рукописным шагом
# repo-ci.yml — ALLOWLIST scripts/lib/ci_guard_registration_guard.py
# заморожен для PR #771.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_gh_body_file_guard.py -q
