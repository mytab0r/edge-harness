#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия реестра использования LLM-провайдеров — таблица видимости»
# добавлен независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Реестр использования LLM-провайдеров (#823, openspec/changes/
# llm-provider-usage-manifest): docs/agents/LLM-PROVIDER-USAGE.md не
# хардкодит назначения — scripts/lib/collect_provider_usage.py
# собирает их из config/provider-usage.json. Три мутации — пропавшая
# строка, лишняя строка, расхождение содержимого с генератором.
set -euo pipefail
pip install --quiet pytest
python -m pytest scripts/lib/test_provider_usage_registry.py -q
