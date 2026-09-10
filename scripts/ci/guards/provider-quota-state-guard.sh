#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Тесты персистентного состояния квоты провайдеров» добавлен
# независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Персистентное состояние квоты провайдеров (#857, openspec/changes/
# provider-quota-gating): чистые функции parse/merge/expire + IO на
# фейковом gh_func. Мутация — сломать любую из трёх функций красит
# соответствующий тест по отдельности.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/orchestra/test_provider_quota_state.py -q
