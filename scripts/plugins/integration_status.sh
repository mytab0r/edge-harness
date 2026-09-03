#!/usr/bin/env bash
# Постинг статуса интеграции в канал конвейера (#115): kind integration_status,
# псевдо-задача integration:<id>. Реестр интеграций — dsh-edge/integrations.json;
# читатель статусов — раздел «Интеграции» морды (клиентский плагин integrations).
# Транспорт с #86 — сессия-конвейер морды dsh-edge вместо журнала edge-harness.
# Механика (ретраи, fail-loud градация) — общая:
# scripts/lib/dsh-edge-pipeline.sh.
#
# Переменные окружения:
#   INTEGRATION_ID — id интеграции из реестра (например jira)
#   STATE          — ready|not_configured|failed|building|built|deploying
#   SOURCE         — forge|deploy (кто постит; по умолчанию deploy)
#   DETAIL         — необязательный текст (каких секретов нет / ошибка / версия)
#   DSH_EDGE_URL   — база морды (vars.DSH_EDGE_URL)
#   DSH_EDGE_ACCESS_KEY — ключ владельца морды (secrets.DSH_EDGE_ACCESS_KEY)
#   FINAL          — 1 для финальных статусов: не доехал — exit 1
set -euo pipefail

: "${INTEGRATION_ID:?INTEGRATION_ID не задан}"
: "${STATE:?STATE не задан}"
SOURCE="${SOURCE:-deploy}"
FINAL="${FINAL:-0}"
: "${DSH_EDGE_URL:?DSH_EDGE_URL не задан}"
: "${DSH_EDGE_ACCESS_KEY:?DSH_EDGE_ACCESS_KEY не задан}"

# shellcheck source=../lib/dsh-edge-pipeline.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/dsh-edge-pipeline.sh"

TASK_ID="integration:${INTEGRATION_ID}"
KIND="integration_status"
# DETAIL — свободный текст (не JSON): передаём строкой через --arg,
# пустая строка = поля detail в событии нет.
DATA_JSON=$(jq -n --arg i "$INTEGRATION_ID" --arg s "$STATE" --arg d "${DETAIL:-}" \
  '{integration: $i, state: $s} + (if $d == "" then {} else {detail: $d} end)')

pipeline_post_event
