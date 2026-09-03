#!/usr/bin/env bash
# Клиент job'а автоматизации (#116): repository_dispatch harness-automation →
# работа по kind из конфига → результат в журнал. Контракт —
# openspec/changes/automations-hub/. Конфиг читается ИЗ ЖУРНАЛА по AUTOMATION_ID
# (одно место правды), а не из payload: payload несёт только адреса прогона.
#
# Правила: fail loud; жизненный цикл job (job_start/job_end) этот файл ведёт для
# digest/pool сам, для kind=hands — уступает scripts/hands/dsh_task.sh (тот сеет
# journal-seq и сам постит job_end — наш двойной цикл дал бы два job_start).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HEARTBEAT_SECS="${HEARTBEAT_SECS:-20}"
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIMEOUT=30

: "${HANDS_URL:?HANDS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"
: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или workflow input)}"
: "${AUTOMATION_ID:?AUTOMATION_ID не задан (repository_dispatch payload или workflow input)}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}"
CONFIG="${CONFIG:-}"
TRIGGER="${TRIGGER:-manual}"
PERIOD_SINCE_TS="${PERIOD_SINCE_TS:-}"
PERIOD_UNTIL_TS="${PERIOD_UNTIL_TS:-}"

JOB_ID="${JOB_ID:-automation-${GITHUB_RUN_ID:-local}-$$}"
WORK="${RUNNER_TEMP:-/tmp}/automation-run"
mkdir -p "$WORK"
CONFIG_FILE="$WORK/config.json"
RESULT_FILE="$WORK/result.json"
EVENTS_FILE="$WORK/events.jsonl"
: >"$EVENTS_FILE"

api_post() { # path body
  curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIMEOUT" \
    -X POST -H "Authorization: Bearer $HANDS_TOKEN" \
    -H "Content-Type: application/json" -d "$2" "$HANDS_URL$1"
}

# journal-seq: единственный писатель — этот файл (для kind=hands нумерацию
# продолжит scripts/hands/dsh_task.sh, посеяв максимум с сервера: уникальность
# (task_id, seq) соблюдается, события не теряются и не двоятся).
SEQ=0
add_event() { # kind json_data
  SEQ=$((SEQ + 1))
  printf '{"seq":%s,"kind":"%s","ts":%s,"data":%s}\n' \
    "$SEQ" "$1" "$(date -u +%s000)" "${2:-null}" >>"$EVENTS_FILE"
}
flush_events() {
  [ -s "$EVENTS_FILE" ] || return 0
  local body attempt
  body=$(jq -s --arg t "$TASK_ID" --arg s "automation" \
    '{task_id: $t, source: $s, events: .}' "$EVENTS_FILE")
  for attempt in 1 2 3 4 5; do
    if api_post /api/events "$body" >/dev/null; then : >"$EVENTS_FILE"; return 0; fi
    sleep $((attempt * 2))
  done
  echo "::error::Журнал не принял батч из 5 попыток — прогон не может считаться завершённым" >&2
  return 1
}
post_job_end() { # result
  add_event "job_end" "{\"result\":\"$1\"}"
  flush_events
  JOB_ENDED=1
}
HB_PID=""
start_heartbeat() {
  while :; do
    sleep "$HEARTBEAT_SECS"
    api_post /api/heartbeat "{\"job_id\":\"$JOB_ID\",\"task_id\":\"$TASK_ID\"}" >/dev/null || true
  done
}
# LIFECYCLE_OURS=0 — lifecycle ведёт dsh_task.sh (kind=hands): наш cleanup молчит.
LIFECYCLE_OURS=1
JOB_ENDED=0
cleanup() {
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
  if [ "$JOB_ENDED" -eq 0 ] && [ "$LIFECYCLE_OURS" -eq 1 ]; then
    add_event "job_end" '{"result":"fail","reason":"job завершён до финала (отмена/ошибка среды)"}' || true
    flush_events || true
  fi
}
trap cleanup EXIT

fail_loud() { # сообщение — конфигурационная поломка ДО работы: честный красный след
  add_event "job_start" "{\"job_id\":\"$JOB_ID\",\"automation\":\"$AUTOMATION_ID\",\"trigger\":\"$TRIGGER\"}"
  add_event "automation_result" "{\"ok\":false,\"error\":$(jq -Rn --arg e "$1" '$e')}"
  post_job_end "fail"
  echo "::error::$1" >&2
  exit 1
}

# ── 1. Конфиг — из журнала, не из payload и не из догадок ──────────────────────────
if [ -n "$CONFIG" ]; then
  printf '%s' "$CONFIG" >"$CONFIG_FILE"
else
  curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIMEOUT" \
    -H "Authorization: Bearer $HANDS_TOKEN" "$HANDS_URL/api/automations" \
    | jq -c --arg id "$AUTOMATION_ID" '.automations[] | select(.id == $id) | .config' >"$CONFIG_FILE" \
    || true
fi
[ -s "$CONFIG_FILE" ] || fail_loud "конфиг автоматизации $AUTOMATION_ID не найден в журнале — прогон без конфига невозможен"
jq -e . "$CONFIG_FILE" >/dev/null || fail_loud "конфиг автоматизации $AUTOMATION_ID — не JSON"
KIND=$(jq -r '.task.kind // "unknown"' "$CONFIG_FILE")
ENABLED=$(jq -r '.enabled // false' "$CONFIG_FILE")
case "$KIND" in
  digest|pool|hands) ;;
  *) fail_loud "неизвестный task.kind: $KIND (ожидались digest|hands|pool)" ;;
esac
if [ "$ENABLED" != "true" ]; then
  echo "::notice::автоматизация $AUTOMATION_ID выключена — прогон завершается без работы (триггер обогнал выключение)"
  add_event "job_start" "{\"job_id\":\"$JOB_ID\",\"automation\":\"$AUTOMATION_ID\",\"trigger\":\"$TRIGGER\",\"skipped\":\"disabled\"}"
  post_job_end "ok"
  exit 0
fi

if [ "$KIND" = "hands" ]; then LIFECYCLE_OURS=0; fi
add_event "job_start" "{\"job_id\":\"$JOB_ID\",\"automation\":\"$AUTOMATION_ID\",\"trigger\":\"$TRIGGER\",\"kind\":\"$KIND\"}"
flush_events
if [ "$LIFECYCLE_OURS" -eq 1 ]; then
  start_heartbeat &
  HB_PID=$!
fi

RESULT_RC=1
case "$KIND" in
  digest)
    set +e
    timeout 900 python3 "$SCRIPT_DIR/digest.py" \
      --config-file "$CONFIG_FILE" \
      --since-ts "${PERIOD_SINCE_TS:-0}" --until-ts "${PERIOD_UNTIL_TS:-0}" \
      --result-file "$RESULT_FILE"
    RESULT_RC=$?
    set -e
    ;;
  pool)
    TITLE=$(jq -r '.task.title' "$CONFIG_FILE")
    BODY=$(jq -r '.task.body // ""' "$CONFIG_FILE")
    NOW_MS="$(date -u +%s%3N)"
    UNTIL_MS="${PERIOD_UNTIL_TS:-$NOW_MS}"
    if [ -n "$PERIOD_SINCE_TS" ]; then
      BODY="$BODY

---
Автоматизация \`$AUTOMATION_ID\` ($TRIGGER), период: $(date -u -d "@$((PERIOD_SINCE_TS / 1000))" '+%Y-%m-%d %H:%M') — $(date -u -d "@$((UNTIL_MS / 1000))" '+%Y-%m-%d %H:%M') UTC."
    fi
    set +e
    ISSUE_URL=$(gh issue create --label task --title "$TITLE" --body "$BODY" 2>"$WORK/pool.err")
    RESULT_RC=$?
    set -e
    if [ "$RESULT_RC" -eq 0 ]; then
      ISSUE_NUMBER="${ISSUE_URL##*/}"
      jq -n --arg url "$ISSUE_URL" --argjson n "$ISSUE_NUMBER" '{ok: true, issue: $n, issue_url: $url}' >"$RESULT_FILE"
      echo "Задача пула создана: $ISSUE_URL"
    else
      jq -n --arg e "$(tail -c 2000 "$WORK/pool.err")" '{ok: false, error: $e}' >"$RESULT_FILE"
    fi
    ;;
  hands)
    # Прямая работа раннера по шаблону текста: dsh_task.sh сам ведёт lifecycle
    # (job_start/job_end/heartbeat), сеет journal-seq и красит job при отказе.
    TEXT=$(jq -r '.task.text' "$CONFIG_FILE")
    NOW_MS="$(date -u +%s%3N)"
    SINCE_MS="${PERIOD_SINCE_TS:-$((NOW_MS - 7 * 24 * 3600 * 1000))}"
    UNTIL_MS="${PERIOD_UNTIL_TS:-$NOW_MS}"
    TEXT="${TEXT//\{period_since\}/$(date -u -d "@$((SINCE_MS / 1000))" '+%Y-%m-%d')}"
    TEXT="${TEXT//\{period_until\}/$(date -u -d "@$((UNTIL_MS / 1000))" '+%Y-%m-%d')}"
    echo "Передача в DSH headless (scripts/hands/dsh_task.sh), шаблон подставлен"
    set +e
    TASK_TEXT="$TEXT" bash "$SCRIPT_DIR/../hands/dsh_task.sh"
    RESULT_RC=$?
    set -e
    ;;
esac

if [ "$LIFECYCLE_OURS" -eq 0 ]; then
  exit "$RESULT_RC"   # hands: lifecycle уже закрыт dsh_task.sh
fi

if [ -s "$RESULT_FILE" ]; then
  jq -e . "$RESULT_FILE" >/dev/null 2>&1 || echo '{"ok":false,"error":"result-файл не JSON"}' >"$RESULT_FILE"
  add_event "automation_result" "$(jq -c . "$RESULT_FILE")"
else
  add_event "automation_result" "{\"ok\":false,\"error\":\"работа не оставила result (rc=$RESULT_RC)\"}"
fi
flush_events
if [ "$RESULT_RC" -eq 0 ]; then
  post_job_end "ok"
else
  post_job_end "fail"
  echo "::error::автоматизация $AUTOMATION_ID завершилась с кодом $RESULT_RC" >&2
  exit "$RESULT_RC"
fi
