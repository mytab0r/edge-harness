#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия pnpm для dsh plugin add (класс #83/#842)» добавлен
# независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Класс #83/#842: `dsh plugin add` без pnpm на PATH падает
# («pnpm not found on PATH»). Закрыт один раз для hands.yml (#83, PR
# #93/#94), не распространён на ai-review.yml, когда #838 добавил тот
# же вызов туда — ai-review.yml падал на КАЖДОМ прогоне (issue #842).
set -euo pipefail
pip install --quiet pytest pyyaml
python -m pytest scripts/lib/test_dsh_plugin_pnpm_guard.py -q
