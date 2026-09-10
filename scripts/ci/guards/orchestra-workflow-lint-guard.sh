#!/usr/bin/env bash
# Гвардия класса «continue-on-error шаг без id остаётся невидимым» (#887):
# новый continue-on-error шаг job `orchestra` без явного `id:` не попадёт в
# `toJSON(steps)`, который читает `best_effort_outcome_guard.py` — реальный
# провал шага снова станет невидимым тем же способом, каким уже был живой
# инцидент 2026-09-10 (прогон 34506949025, шаг «Гвардия протухшей метки
# blocked», Jobs API отдал success при `##[error]Process completed with
# exit code 1` в логе). Регистрация сразу через каталог гвардий (#749) —
# не рукописный шаг repo-ci.yml.
#
# Живой снимок ниже — required-гейт сразу (0 запросов к GitHub API, только
# локальный yaml.safe_load .github/workflows/orchestra.yml).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_orchestra_workflow_lint.py -q
python scripts/lib/orchestra_workflow_lint.py
