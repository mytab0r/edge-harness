#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# три шага «Тесты сборщика снимка здоровья конвейера»/«Тесты детектора
# регрессии здоровья конвейера»/«Тесты само-аудита здоровья конвейера»
# добавлены независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос
# (объединены в один файл каталога, потому что проверяют один связанный
# механизм и делят один комментарий в исходном workflow), не поднятие
# потолка.
#
# Само-аудит здоровья конвейера (openspec/changes/pipeline-health-
# self-audit): снимок 6-7 дешёвых метрик (сборщик — прод-форма
# PR/issues/runs, git-транспорт на живом bare-репозитории), детектор
# регрессии (baseline/streak/честное «недостаточно данных», мутация
# границ), само-аудит (дедуп по отпечатку, свой суточный потолок,
# различение регрессия/пожар) — проводка на моке gh.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/measure/test_pipeline_health.py -q
python -m pytest scripts/orchestra/test_health_regression.py -q
python -m pytest scripts/orchestra/test_health_audit.py -q
