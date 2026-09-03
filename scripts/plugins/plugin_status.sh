#!/usr/bin/env bash
# Постинг статуса плагина в канал конвейера (kind: plugin_status, псевдо-задача
# plugin:<id>). Контракт события — прежний журнальный; транспорт с #86 —
# сессия-конвейер морды dsh-edge (ingest, чтение через /api/harness/events,
# патчи 0004/0005; см. openspec/changes/edge-harness-decommission/). Механика
# (ретраи, fail-loud градация финальных статусов) живёт в общей библиотеке
# scripts/lib/dsh-edge-pipeline.sh — эта обёртка только собирает task_id/kind/data.
#
# Переменные окружения:
#   PLUGIN_ID    — id плагина из манифеста (например hello)
#   STATE        — building|built|deploying|ready|failed
#   SOURCE       — forge|deploy (кто постит; по умолчанию deploy)
#   DETAIL       — необязательный текст (ошибка/версия), уходит в data.detail
#   DSH_EDGE_URL — база морды (vars.DSH_EDGE_URL)
#   DSH_EDGE_ACCESS_KEY — ключ владельца морды (secrets.DSH_EDGE_ACCESS_KEY)
#   FINAL        — 1 для финальных статусов (ready/failed): не доехал — exit 1
set -euo pipefail

: "${PLUGIN_ID:?PLUGIN_ID не задан}"
: "${STATE:?STATE не задан}"
SOURCE="${SOURCE:-deploy}"
FINAL="${FINAL:-0}"
: "${DSH_EDGE_URL:?DSH_EDGE_URL не задан}"
: "${DSH_EDGE_ACCESS_KEY:?DSH_EDGE_ACCESS_KEY не задан}"

# shellcheck source=../lib/dsh-edge-pipeline.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/dsh-edge-pipeline.sh"

TASK_ID="plugin:${PLUGIN_ID}"
KIND="plugin_status"
# DETAIL — свободный текст (не JSON): передаём строкой через --arg,
# пустая строка = поля detail в событии нет.
DATA_JSON=$(jq -n --arg p "$PLUGIN_ID" --arg s "$STATE" --arg d "${DETAIL:-}" \
  '{plugin: $p, state: $s} + (if $d == "" then {} else {detail: $d} end)')

pipeline_post_event
