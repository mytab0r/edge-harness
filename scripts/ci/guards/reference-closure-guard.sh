#!/usr/bin/env bash
# Гвардия приёмки по ссылке (#1042): второй источник связи PR→задача
# (scripts/lib/task_ref.py::also_closes_targets) и потребитель, который
# закрывает задачу по этому источнику (scripts/orchestra/reference_closure.py).
# Зарегистрирована как данные каталога (#749), не рукописным шагом в
# repo-ci.yml — этот файл единственное, что нужно для регистрации.
#
# also_closes_targets покрыт также scripts/lib/test_task_ref.py, уже
# зарегистрированным рукописным шагом repo-ci.yml (класс не дублируется —
# та регистрация была раньше #749 и не переносится ради переноса).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/orchestra/test_reference_closure.py -q
