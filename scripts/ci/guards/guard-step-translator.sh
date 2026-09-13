#!/usr/bin/env bash
# Транслятор рукописного шага гвардии в каталог scripts/ci/guards (issue
# #897, продолжение #749/#771/#762/#764): scripts/lib/guard_step_translator.py.
# Зарегистрирован СРАЗУ в каталоге, не рукописным шагом repo-ci.yml —
# ci_guard_registration_guard.py и есть модуль, который эту дисциплину
# проверяет, заводить его собственную гвардию в обход было бы прямым
# нарушением класса, который она закрывает.
#
# Прод-форма фикстур — вербатим куски реальных диффов измеренных PR
# (#870/#841/#241 на дату issue #897), см. докстринг test_guard_step_translator.py.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_guard_step_translator.py -q
