#!/usr/bin/env bash
# Тесты механического потолка закрытий pm-прогона (#869, drain-health-
# curator): pm_dispatch.py считает ФАКТ (закрытия за окно по issues/events,
# не декларацию модели), красит шаг при превышении PM_MAX_CLOSURES_PER_RUN
# или нарушении границ pm.md (исполнитель/waiting:owner), падает громко при
# усечённом окне чтения событий. Зарегистрирована файлом каталога (#749),
# не рукописным шагом repo-ci.yml — рукописный шаг, приехавший в ветку #869
# до появления каталога на main, краснит
# scripts/lib/test_ci_guard_registration_guard.py (ребейз #869, 2026-09-11).
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_pm_dispatch.py -q
