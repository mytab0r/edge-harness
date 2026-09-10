#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия границы доверия — персистентная квота провайдеров» добавлен
# независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Граница доверия персистентной квоты провайдеров (#857, openspec/
# changes/provider-quota-gating): чейн-раннер (в т.ч. недоверенный
# ai-review.yml, #18) только читает vars.DSH_PROVIDER_QUOTA_UNTIL,
# пишет исключительно код пульса (scripts/orchestra/**). Мутация —
# добавить `gh variable set DSH_PROVIDER_QUOTA_UNTIL` в любой workflow
# или вызвать save_quota_state вне scripts/orchestra/ — краснеет.
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_provider_quota_state_guard.py -q
