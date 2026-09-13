#!/usr/bin/env bash
# Постинг статуса интеграции в журнал харнеса (#115): kind integration_status,
# псевдо-задача integration:<id>. Реестр интеграций — dsh-edge/integrations.json;
# читатель статусов — раздел «Интеграции» морды (клиентский плагин integrations).
# Механика (посев seq, ретраи, fail-loud градация) — общая:
# scripts/lib/journal_status.sh.
#
# Переменные окружения:
#   INTEGRATION_ID — id интеграции из реестра (например jira)
#   STATE          — ready|not_configured|failed|building|built|deploying
#   SOURCE         — forge|deploy (кто постит; по умолчанию deploy)
#   DETAIL         — необязательный текст (каких секретов нет / ошибка / версия)
#   HARNESS_URL    — база журнала (vars.HARNESS_URL)
#   HANDS_TOKEN    — токен журнала (secrets.HANDS_TOKEN)
#   FINAL          — 1 для финальных статусов: не доехал — exit 1
set -euo pipefail

: "${INTEGRATION_ID:?INTEGRATION_ID не задан}"
: "${STATE:?STATE не задан}"
SOURCE="${SOURCE:-deploy}"
FINAL="${FINAL:-0}"
: "${HARNESS_URL:?HARNESS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"

# shellcheck source=../lib/journal_status.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/journal_status.sh"

TASK_ID="integration:${INTEGRATION_ID}"
KIND="integration_status"
# DETAIL — свободный текст (не JSON): передаём строкой через --arg,
# пустая строка = поля detail в событии нет.
DATA_JSON=$(jq -n --arg i "$INTEGRATION_ID" --arg s "$STATE" --arg d "${DETAIL:-}" \
  '{integration: $i, state: $s} + (if $d == "" then {} else {detail: $d} end)')

journal_post_event
