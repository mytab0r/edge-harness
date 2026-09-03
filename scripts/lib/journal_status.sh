#!/usr/bin/env bash
# Единственное место правды механики статусов в журнале харнеса (POST /api/events
# в псевдо-задачу, openspec/specs/journal-tasks-hands.md): посев seq с сервера
# (повторный запуск job продолжает нумерацию, а не теряет события), ретраи на
# лежащем журнале, fail-loud градация финальных статусов. Обёртки — только
# сборка task_id/kind/data:
#   - scripts/plugins/plugin_status.sh     → plugin:<id>,      kind plugin_status
#   - scripts/plugins/integration_status.sh → integration:<id>, kind integration_status
#
# Правило fail loud с градацией: ФИНАЛЬНЫЙ статус (ready/failed и т.п.) обязан
# доехать до журнала — иначе exit 1 красит job (деплой без статуса = silent-wrong).
# Промежуточные статусы — best-effort: журнал может лежать в момент, когда морду
# как раз чинят, поэтому недоступность журнала не красит job (warning в лог).
#
# Контракт функции (переменные окружения вызывающего):
#   TASK_ID      — псевдо-задача журнала (например integration:jira)
#   KIND         — kind события (integration_status / plugin_status)
#   DATA_JSON    — data события, уже собранный JSON (jq -n у обёртки)
#   SOURCE       — кто постит (forge|deploy)
#   FINAL        — 1 для финальных статусов: не доехал — exit 1
#   HARNESS_URL  — база журнала (vars.HARNESS_URL)
#   HANDS_TOKEN  — токен журнала (secrets.HANDS_TOKEN)

# Гвардия однократного сёрса: библиотека содержит только определения.
journal_post_event() {
  : "${TASK_ID:?TASK_ID не задан}"
  : "${KIND:?KIND не задан}"
  : "${DATA_JSON:?DATA_JSON не задан}"
  local source="${SOURCE:-deploy}"
  local final="${FINAL:-0}"

  api() { curl -fsS -H "Authorization: Bearer $HANDS_TOKEN" "$@"; }
  api_post() {
    curl -fsS -X POST -H "Authorization: Bearer $HANDS_TOKEN" \
      -H "Content-Type: application/json" -d "$2" "$1"
  }

  # seq сеется с сервера, не с потолка: берём максимум по задаче (spec п.2).
  # Посев — под теми же ретраями, что и пост: лежащий журнал не должен убивать
  # вызов до постинга (под set -e первый упавший GET делал именно это).
  # Пауза ретрая — только при сбое запроса: успешные страницы с has_more=true
  # едут подряд без ожидания.
  local after=0 max_seq=0 seeded=0 resp ms has_more attempt
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
    local message="Журнал недоступен: не удалось засеять seq для $TASK_ID из 5 попыток"
    if [ "$final" = "1" ]; then
      echo "::error::$message — финальный статус обязан доехать (fail loud)" >&2
      return 1
    fi
    echo "::warning::$message (промежуточный статус пропущен)" >&2
    return 0
  fi
  local seq=$((max_seq + 1))

  local body
  body=$(jq -n --arg t "$TASK_ID" --arg src "$source" --arg kind "$KIND" --argjson seq "$seq" \
    --argjson data "$DATA_JSON" --argjson ts "$(date -u +%s000)" \
    '{task_id: $t, source: $src, events: [{seq: $seq, ts: $ts, kind: $kind, data: $data}]}')

  for attempt in 1 2 3 4 5; do
    if api_post "$HARNESS_URL/api/events" "$body" >/dev/null; then
      echo "$KIND: $TASK_ID -> $(jq -r '.state // "?"' <<<"$DATA_JSON") (seq $seq) принят журналом"
      return 0
    fi
    sleep $((attempt * 2))
  done

  local state_name failure
  state_name=$(jq -r '.state // "?"' <<<"$DATA_JSON")
  failure="Журнал не принял $KIND $state_name для $TASK_ID из 5 попыток"
  if [ "$final" = "1" ]; then
    echo "::error::$failure — финальный статус обязан доехать (fail loud)" >&2
    return 1
  fi
  echo "::warning::$failure (промежуточный статус)" >&2
  return 0
}
