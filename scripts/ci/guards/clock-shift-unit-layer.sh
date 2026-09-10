#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Тесты носителя «настенные часы» — без сдвига (#649)» добавлен
# независимо слитым PR #667 ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py — перенос, не поднятие потолка.
#
# Юнит-слой носителя «тест зависит от настенных часов» (#649) — БЕЗ сдвига
# (CLOCK_SHIFT_DAYS не задан, scripts/conftest.py — no-op). Полный прогон
# scripts/ на горизонтах живёт только в clock-shift-tests.yml раз в сутки
# (находка второго гейта ревью PR #667): правка conftest.py/
# clock_shift_suite.py без этого слоя проходила бы обязательный `test` без
# единой их проверки — поломку поймал бы только завтрашний красный прогон,
# уже после мержа. Слой копеечный (доли секунды) — страховка на каждый PR;
# полный прогон на горизонтах остаётся отдельным периодическим workflow'ем.
set -euo pipefail
pip install --quiet pytest pyyaml freezegun
python -m pytest scripts/measure/test_clock_shift_suite.py scripts/test_conftest_clock_shift.py -q
