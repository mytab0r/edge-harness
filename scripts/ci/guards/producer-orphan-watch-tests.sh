#!/usr/bin/env bash
# Производитель задач пула без измеримого потребителя — инвариант 23
# (#1277): маркер producer в pool_issue.create_pool_issue, классификатор/
# пороги/вердикт в producer_orphan_watch.py.
#
# Зарегистрирована файлом каталога (#749), не рукописным шагом repo-ci.yml
# (рукописная версия краснит test_ci_guard_registration_guard::
# test_live_repo_ci_matches_frozen_allowlist: «перенеси в scripts/ci/
# guards/<имя>.sh, не дописывай шаг в общий файл» — ALLOWLIST заморожен
# рейчетом).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_producer_orphan_watch.py -q
