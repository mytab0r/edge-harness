#!/usr/bin/env bash
# Клиент job'а автоматизации (#116): repository_dispatch harness-automation →
# работа по kind из конфига → результат в журнал. Контракт — задача #116
# (дельта-спека приходит change-каталогом во второй части эпика).
# Конфиг читается ИЗ ЖУРНАЛА по AUTOMATION_ID
# (одно место правды), а не из payload: payload несёт только адреса прогона.
#
# Правила: fail loud; жизненный цикл job (job_start/job_end) этот файл ведёт для
# digest/pool сам, для kind=hands — уступает scripts/hands/dsh_task.sh (тот сеет
# journal-seq и сам постит job_end — наш двойной цикл дал бы два job_start).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# journal-seq: посев максимумом с сервера (находка ревью PR #241, п.1) — один
# хелпер, тем же приёмом, что scripts/hands/dsh_task.sh.
# shellcheck source=scripts/lib/journal_seq_seed.sh
source "$SCRIPT_DIR/../lib/journal_seq_seed.sh"
HEARTBEAT_SECS="${HEARTBEAT_SECS:-20}"
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIMEOUT=30

: "${HANDS_URL:?HANDS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"
: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или workflow input)}"
: "${AUTOMATION_ID:?AUTOMATION_ID не задан (repository_dispatch payload или workflow input)}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}"
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
# (task_id, seq) соблюдается, события не теряются и не двоятся). Посев здесь
# максимумом с сервера ПО ВСЕМ страницам под TASK_ID (находка ревью PR #241,
# п.1) — повторный прогон job'а под тем же TASK_ID (re-run failed jobs) без
# посева начинал бы с SEQ=0, сервер молча отбрасывал бы дубликаты по
# UNIQUE(task_id, seq), и успешный повтор остался бы невидим в журнале.
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

# Посев SEQ — до первого add_event (fail_loud тоже пишет события): сеть/gh
# ещё не тронуты ниже, посев не может провалиться позже них.
SEQ=$(journal_seq_seed) || fail_loud "не смог посеять journal-seq с сервера для $TASK_ID — прогон без этого рискует затоптать чужие seq"

# ── 1. Конфиг — из журнала, не из payload и не из догадок ──────────────────────────
curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIMEOUT" \
  -H "Authorization: Bearer $HANDS_TOKEN" "$HANDS_URL/api/automations" \
  | jq -c --arg id "$AUTOMATION_ID" '.automations[] | select(.id == $id) | .config' >"$CONFIG_FILE" \
  || true
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

# kind=hands: lifecycle (job_start/job_end/heartbeat) ведёт dsh_task.sh — наш
# job_start дал бы второй job_start и коллизию journal-seq (ревью #116, major 2).
if [ "$KIND" = "hands" ]; then LIFECYCLE_OURS=0; fi
if [ "$LIFECYCLE_OURS" -eq 1 ]; then
  add_event "job_start" "{\"job_id\":\"$JOB_ID\",\"automation\":\"$AUTOMATION_ID\",\"trigger\":\"$TRIGGER\",\"kind\":\"$KIND\"}"
  flush_events
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
    # GH_TOKEN/GITHUB_TOKEN живут в env этого job'а ради kind=pool (gh issue
    # create выше) — DSH headless их видеть не должен: у агента нет прав на
    # пуш (тот же класс, что hands.yml закрывает persist-credentials:false +
    # unset GH_RUN_TOKEN в dsh_task.sh до старта DSH; здесь другое имя
    # переменной — GH_TOKEN, тот же принцип, находка AI-ревью PR #241).
    TASK_TEXT="$TEXT" env -u GH_TOKEN -u GITHUB_TOKEN bash "$SCRIPT_DIR/../hands/dsh_task.sh"
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
