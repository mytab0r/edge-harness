#!/usr/bin/env bash
# Юнит-слой носителя «тест зависит от настенных часов» (#649) в обязательном
# repo-ci: находка второго гейта ревью PR #667 — без него правка
# conftest.py/clock_shift_suite.py проходила бы `test` без единой их
# проверки, поломку ловил бы только завтрашний красный прогон, уже после
# мержа. Ветка PR #667 добавила слой рукописным шагом repo-ci.yml раньше,
# чем на main доехал механизм каталога гвардий (#749): при ребейзе шаг
# перенесён сюда — перенос, НЕ поднятие потолка ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py (77 осталось 77).
# clock-shift-tests.yml вызывает этот же файл — второго места правды
# списка тестов слоя нет. Слой копеечный (доли секунды); полный прогон
# на горизонтах остаётся отдельным периодическим workflow'ем.
set -euo pipefail
pip install --quiet pytest pyyaml freezegun
python -m pytest scripts/measure/test_clock_shift_suite.py scripts/test_conftest_clock_shift.py -q
