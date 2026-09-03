#!/usr/bin/env bash
# Канал статусов конвейера (#86): статус уезжает в сессию-конвейер морды
# dsh-edge (harness-pipeline) через ingest-шов (патч 0004) и читается назад
# маршрутом /api/harness/events (патч 0005) в форме прежнего журнала. Механика
# (ретраи, fail-loud градация финальных статусов) — наследник
# scripts/lib/journal_status.sh, транспорт — морда вместо журнала edge-harness.
# Обёртки — только сборка task_id/kind/data:
#   - scripts/plugins/plugin_status.sh      → plugin:<id>,      kind plugin_status
#   - scripts/plugins/integration_status.sh → integration:<id>, kind integration_status
#   - scripts/hands/dsh_task.sh             → жизненный цикл job (job_*, bootstrap…)
#
# Правило fail loud с градацией: ФИНАЛЬНЫЙ статус (ready/failed и т.п.) обязан
# доехать до морды — иначе exit 1 красит job (деплой без статуса = silent-wrong).
# Промежуточные статусы — best-effort: морда может быть недоступна в момент,
# когда её как раз чинят, поэтому недоступность не красит job (warning в лог).
#
# Форма события (проверяется патчем 0004, читается патчем 0005):
#   {"type":"user/message","data":{"id":"<uuid>","role":"user",
#     "content":[{"type":"text","text":"<однострочный JSON-рекорд>"}],
#     "source":{"kind":"harness-pipeline-status"}}}
# Рекорд: {task_id, kind, data, source, ts, emitted} — task_id/kind строковые,
# data — объект|null; повторная доставка отличима по emitted (uuid), дубли
# статуса безвредны: читатель берёт последний.
#
# Контракт функции (переменные окружения вызывающего):
#   TASK_ID      — псевдо-задача канала (например integration:jira)
#   KIND         — kind события (integration_status / plugin_status / job_…)
#   DATA_JSON    — data события, уже собранный JSON (jq -n у обёртки)
#   SOURCE       — кто постит (forge|deploy|job)
#   FINAL        — 1 для финальных статусов: не доехал — exit 1
#   PIPELINE_HARD — 1 для событий жизненного цикла job (улики прогона):
#                   любое событие обязано доехать, не только финальное
#   DSH_EDGE_URL — база морды (vars.DSH_EDGE_URL)
#   DSH_EDGE_ACCESS_KEY — ключ владельца морды (secrets.DSH_EDGE_ACCESS_KEY)
#   WORK         — рабочий каталог (не задан → общий каталог канала)

# shellcheck shell=bash

PIPELINE_SESSION_ID="${PIPELINE_SESSION_ID:-harness-pipeline}"
PIPELINE_SOURCE_KIND="harness-pipeline-status"

pipeline_init() {
  if [ -z "${DSH_EDGE_URL:-}" ] || [ -z "${DSH_EDGE_ACCESS_KEY:-}" ]; then
    echo "::error::DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY не заданы — канал конвейера (#86) не настроен. Это vars/secrets репозитория, а не сбой сети." >&2
    return 1
  fi
  if [ -z "${WORK:-}" ]; then
    WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/harness-pipeline"
    export WORK
  fi
  mkdir -p "$WORK"
}

# Гвардия однократного сёрса: библиотека содержит только определения.
#
# Градация отказа (fail loud с газом):
#   FINAL=1 (ready/failed/job_end)  — не доехал → exit 1 (деплой/job без финала
#                                     = silent-wrong);
#   PIPELINE_HARD=1 (раннеры)       — любое событие жизненного цикла обязано
#                                     доехать: это и есть улики прогона;
#   иначе (промежуточные статусы деплоя) — недоступность морды не красит job
#                                     (морду как раз чинят): warning и return 0.
# Отказ КОНФИГУРАЦИИ («не настроено») громкий всегда — это не сеть.
pipeline_post_event() {
  : "${TASK_ID:?TASK_ID не задан}"
  : "${KIND:?KIND не задан}"
  : "${DATA_JSON:?DATA_JSON не задан}"
  local source="${SOURCE:-deploy}"
  local final="${FINAL:-0}"
  local hard="${PIPELINE_HARD:-0}"

  pipeline_init || return 1
  # shellcheck source=../lib/dsh-ci.sh
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dsh-ci.sh"
  # shellcheck source=../lib/dsh-edge-session.sh
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dsh-edge-session.sh"
  dsh_edge_init || return 1
  if [ "${PIPELINE_LOGGED_IN:-0}" != "1" ]; then
    if ! dsh_edge_login; then
      pipeline_channel_down "$final" "$hard" "Морда недоступна: логин владельца не удался, $KIND для $TASK_ID не отправлен"
      return $?
    fi
    PIPELINE_LOGGED_IN=1
  fi
  # Сессия конвейера: create-or-reuse + заголовок (идемпотентно, как у раннеров).
  # В одном процессе достаточно один раз — каждый вызов post-а не пересоздаёт.
  if [ "${PIPELINE_SESSION_READY:-0}" != "1" ]; then
    if ! dsh_edge_session_begin "$PIPELINE_SESSION_ID" "Конвейер edge-harness" >/dev/null; then
      pipeline_channel_down "$final" "$hard" "Морда недоступна: сессия конвейера не создана, $KIND для $TASK_ID не отправлен"
      return $?
    fi
    PIPELINE_SESSION_READY=1
  fi

  local record event_file
  record=$(jq -nc \
    --arg task_id "$TASK_ID" --arg kind "$KIND" --arg source "$source" \
    --argjson ts "$(date -u +%s000)" --arg emitted "$(uuidgen 2>/dev/null || printf '%s-%s' "$$" "$RANDOM")" \
    --argjson data "$DATA_JSON" \
    '{task_id: $task_id, kind: $kind, data: $data, source: $source, ts: $ts, emitted: $emitted}') \
    || { echo "::error::Рекорд статуса не собрался (DATA_JSON не JSON?)" >&2; return 1; }
  event_file="$WORK/pipeline-event.ndjson"
  # Идентификатор события — сессия + emitted: повторная доставка отличима,
  # коллизии нет. redact применяет сам dsh_edge_ingest на пути в морду.
  jq -nc --argjson record "$record" --arg sid "$PIPELINE_SESSION_ID" --arg kind "$PIPELINE_SOURCE_KIND" \
    '{type: "user/message",
      data: {id: ($sid + "-" + ($record.emitted|tostring)), role: "user",
             content: [{type: "text", text: ($record|tojson)}],
             source: {kind: $kind}}}' >"$event_file" \
    || { echo "::error::Событие статуса не собралось (jq)" >&2; return 1; }

  local attempt ok=1
  for attempt in 1 2 3 4 5; do
    if dsh_edge_ingest "$PIPELINE_SESSION_ID" "$event_file"; then
      ok=0
      break
    fi
    # Возможная причина отказа — протухшая сессия владельца: перелогин перед повтором.
    dsh_edge_login || true
    sleep $((attempt * 2))
  done
  if [ "$ok" -eq 0 ]; then
    echo "$KIND: $TASK_ID -> $(jq -r '.state // "?"' <<<"$DATA_JSON") принят каналом конвейера"
    return 0
  fi
  pipeline_channel_down "$final" "$hard" "Морда не приняла $KIND $(jq -r '.state // "?"' <<<"$DATA_JSON") для $TASK_ID из 5 попыток"
  return $?
}

# Общая хвостовая градация: final или hard → громко и 1, иначе warning и 0.
pipeline_channel_down() { # FINAL HARD СООБЩЕНИЕ
  if [ "$1" = "1" ] || [ "$2" = "1" ]; then
    echo "::error::$3 — статус обязан доехать (fail loud)" >&2
    return 1
  fi
  echo "::warning::$3 (промежуточный статус пропущен)" >&2
  return 0
}
