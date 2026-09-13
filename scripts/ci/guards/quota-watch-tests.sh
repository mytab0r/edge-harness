#!/usr/bin/env bash
# Непрерывный сторож квот харнеса (#605): дедуп-эскалация + автозадача на
# переходе через порог (quota_alert.py) и проводка дешёвой/полной проверки
# с троттлингом (quota_watch.py) — см. .github/workflows/quota-watch.yml и
# докстринг quota_watch.py. Тот же прогон несёт и сам quota-watch.yml
# (шаг «Тесты сторожа квот») — здесь он нужен, чтобы красные тесты сторожа
# были видны и на каждый push в main, не только на событиях quota-watch.
#
# Зарегистрирована файлом каталога (#749), не рукописным шагом repo-ci.yml
# (рукописная версия краснила test_ci_guard_registration_guard::
# test_live_repo_ci_matches_frozen_allowlist: «перенеси в scripts/ci/
# guards/<имя>.sh, не дописывай шаг в общий файл»).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/measure/test_quota_alert.py scripts/measure/test_quota_watch.py -q
