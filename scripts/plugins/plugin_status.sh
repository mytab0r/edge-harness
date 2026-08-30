#!/usr/bin/env bash
# Постинг статуса плагина в журнал харнеса (kind: plugin_status, псевдо-задача
# plugin:<id>). Контракт канала — openspec/changes/dsh-edge-plugin-system/design.md
# («Канал обновления и статусы»), транспорт — тот же POST /api/events, что у рук
# (openspec/specs/journal-tasks-hands.md).
#
# Правило fail loud с градацией: ФИНАЛЬНЫЙ статус (ready/failed) обязан доехать
# до журнала — иначе скрипт выходит с ошибкой и красит job (деплой без статуса
# = silent-wrong). Промежуточные статусы (deploying) — best-effort: журнал может
# лежать в момент, когда морду как раз чинят, поэтому недоступность журнала
# не красит job и не блокирует деплой (warning в лог).
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

TASK_ID="plugin:${PLUGIN_ID}"
api() { curl -fsS -H "Authorization: Bearer $HANDS_TOKEN" "$@"; }
api_post() {
  curl -fsS -X POST -H "Authorization: Bearer $HANDS_TOKEN" \
    -H "Content-Type: application/json" -d "$2" "$1"
}

# seq сеется с сервера, не с потолка: берём максимум по задаче (spec п.2).
# Посев — под теми же ретраями, что и пост: лежащий журнал не должен убивать
# скрипт до постинга (под set -e первый упавший GET делал именно это).
after=0
max_seq=0
seeded=0
for attempt in 1 2 3 4 5; do
  if resp=$(api "$HARNESS_URL/api/events?task_id=$TASK_ID&after=$after&limit=256"); then
    ms=$(jq '[.events[].seq] | max // 0' <<<"$resp")
    if [ "$ms" -gt "$max_seq" ]; then max_seq=$ms; fi
    has_more=$(jq -r '.has_more' <<<"$resp")
    after=$(jq -r '.next_after' <<<"$resp")
    if [ "$has_more" != "true" ]; then seeded=1; break; fi
  else
    sleep $((attempt * 2))
  fi
done
if [ "$seeded" != "1" ]; then
  message="Журнал недоступен: не удалось засеять seq для $TASK_ID из 5 попыток"
  if [ "$FINAL" = "1" ]; then
    echo "::error::$message — финальный статус обязан доехать (fail loud)" >&2
    exit 1
  fi
  echo "::warning::$message (промежуточный статус пропущен)" >&2
  exit 0
fi
seq=$((max_seq + 1))

# DETAIL — свободный текст (не JSON): передаём строкой через --arg,
# пустая строка = поля detail в событии нет.
data=$(jq -n --arg p "$PLUGIN_ID" --arg s "$STATE" --arg d "${DETAIL:-}" \
  '{plugin: $p, state: $s} + (if $d == "" then {} else {detail: $d} end)')
body=$(jq -n --arg t "$TASK_ID" --arg src "$SOURCE" --argjson seq "$seq" --argjson data "$data" --argjson ts "$(date -u +%s000)" \
  '{task_id: $t, source: $src, events: [{seq: $seq, ts: $ts, kind: "plugin_status", data: $data}]}')

for attempt in 1 2 3 4 5; do
  if api_post "$HARNESS_URL/api/events" "$body" >/dev/null; then
    echo "plugin_status: $TASK_ID -> $STATE (seq $seq) принят журналом"
    exit 0
  fi
  sleep $((attempt * 2))
done

message="Журнал не принял plugin_status $STATE для $TASK_ID из 5 попыток"
if [ "$FINAL" = "1" ]; then
  echo "::error::$message — финальный статус обязан доехать (fail loud)" >&2
  exit 1
fi
echo "::warning::$message (промежуточный статус)" >&2
