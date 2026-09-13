#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Тесты бенчмарка латентности и discovery model id провайдеров»
# добавлен независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — гвардия каталога
# справедливо покраснела на «новый рукописный шаг», это перенос, не
# поднятие потолка.
#
# Бенчмарк латентности провайдеров (#836) и discovery живых model id
# (#848) — раньше эти тесты гонял ТОЛЬКО ручной provider-latency-bench.yml
# (workflow_dispatch), ни один push/PR их не проверял: живая цена — #850
# изменил config/provider-usage.json и сломал
# test_real_repo_candidates_cover_full_owner_set_without_codex, но ни
# один обязательный гейт этого не заметил (найдено этим PR, #848).
# Добавлено сюда, чтобы дрейф между provider-usage.json/dsh-ci.sh и
# структурными тестами этих файлов был виден на каждом PR, не только
# при ручном запуске бенчмарка.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/measure/test_provider_latency.py scripts/measure/test_provider_model_discovery.py -q
