#!/usr/bin/env bash
# Постинг статуса плагина в журнал харнеса (kind: plugin_status, псевдо-задача
# plugin:<id>). Контракт канала — openspec/changes/dsh-edge-plugin-system/design.md
# («Канал обновления и статусы»), транспорт — тот же POST /api/events, что у рук
# (openspec/specs/journal-tasks-hands.md). Механика (посев seq с сервера, ретраи,
# fail-loud градация финальных статусов) живёт в общей библиотеке
# scripts/lib/journal_status.sh — эта обёртка только собирает task_id/kind/data.
#
# Переменные окружения:
#   PLUGIN_ID    — id плагина из манифеста (например hello)
#   STATE        — building|built|deploying|ready|failed
#   SOURCE       — forge|deploy (кто постит; по умолчанию deploy)
#   DETAIL       — необязательный текст (ошибка/версия), уходит в data.detail
#   HARNESS_URL  — база журнала (vars.HARNESS_URL)
#   HANDS_TOKEN  — токен журнала (secrets.HANDS_TOKEN)
#   FINAL        — 1 для финальных статусов (ready/failed): не доехал — exit 1
set -euo pipefail

: "${PLUGIN_ID:?PLUGIN_ID не задан}"
: "${STATE:?STATE не задан}"
SOURCE="${SOURCE:-deploy}"
FINAL="${FINAL:-0}"
: "${HARNESS_URL:?HARNESS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"

# shellcheck source=../lib/journal_status.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib/journal_status.sh"

TASK_ID="plugin:${PLUGIN_ID}"
KIND="plugin_status"
# DETAIL — свободный текст (не JSON): передаём строкой через --arg,
# пустая строка = поля detail в событии нет.
DATA_JSON=$(jq -n --arg p "$PLUGIN_ID" --arg s "$STATE" --arg d "${DETAIL:-}" \
  '{plugin: $p, state: $s} + (if $d == "" then {} else {detail: $d} end)')

journal_post_event
