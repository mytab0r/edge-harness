#!/usr/bin/env bash
# Гвардия scripts/orchestra/best_effort_outcome_guard.py (#887): свод РЕАЛЬНЫХ
# исходов continue-on-error шагов job `orchestra` — Jobs API после завершения
# прогона отдаёт `steps[].conclusion` уже ПОСЛЕ маскировки continue-on-error
# (всегда success), не `outcome` (реальный результат, виден только
# `toJSON(steps)` ВНУТРИ ещё идущего job). Без прогона тестов здесь скрипт
# лежал бы без единого шага CI (класс канарейки осиротевших тестов, #583).
# Регистрация сразу через каталог гвардий (#749) — не рукописный шаг
# repo-ci.yml.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_best_effort_outcome_guard.py -q
