#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия окна контекста env-provider (#789)» добавлен PR #792
# (задача #789, слит независимо) уже ПОСЛЕ того, как ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 был заморожен —
# гвардия каталога справедливо покраснела на «новый рукописный шаг», это
# перенос, а не поднятие потолка (тот же класс, что PR #734/
# provider-secrets-import-guard.sh, ratchet ALLOWLIST_RATCHET_MAX не
# двигается).
#
# Контекстное окно env-provider-маршрута combo-router обязано читаться из
# vars.DSH_EDGE_MODEL_CATALOG (реальное окно модели), не из DSH_MAX_TOKENS
# (потолок ДЛИНЫ ОТВЕТА) — смешение отбрасывало главного провайдера в разы
# раньше нужного.
set -euo pipefail
bash scripts/lib/test/dsh-context-window.guard.sh
