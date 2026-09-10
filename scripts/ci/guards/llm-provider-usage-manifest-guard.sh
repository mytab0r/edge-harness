#!/usr/bin/env bash
# Мигрировано из .github/workflows/repo-ci.yml в каталог гвардий (#749):
# шаг «Гвардия манифеста использования LLM-провайдеров (#823)» добавлен
# независимо слитым PR ПОСЛЕ заморозки ALLOWLIST
# scripts/lib/ci_guard_registration_guard.py для PR #771 — перенос, не
# поднятие потолка.
#
# Манифест использования LLM-провайдеров (#823, openspec/changes/
# llm-provider-usage-manifest): dsh_require_provider_chain "<id>"
# резолвит цепочку из config/provider-usage.json по id потребителя
# приоритетнее vars.DSH_PROVIDER_CHAIN — файла нет вовсе, есть, но нет
# записи потребителя, ссылка на несуществующую/пустую цепочку — три
# разных сценария, каждый доказан отдельной мутацией.
set -euo pipefail
bash scripts/lib/test/dsh-provider-usage-manifest.guard.sh
