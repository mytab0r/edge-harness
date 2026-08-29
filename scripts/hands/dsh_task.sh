#!/usr/bin/env bash
# Клиент рук слайса 1 (dsh-in-job): задача из морды → DSH headless → журнал.
# Контракт журнала — openspec/specs/journal-tasks-hands.md, дизайн —
# openspec/changes/dsh-in-job/design.md. Правила: fail loud, без silent-wrong.
set -euo pipefail

# Пины версий — единственное место правды по версиям DSH в руках.
DSH_VERSION="${DSH_VERSION:-0.1.1-rc.2}"
DSH_HEADLESS_VERSION="${DSH_HEADLESS_VERSION:-0.0.1-rc.1}"
HEARTBEAT_SECS="${HEARTBEAT_SECS:-20}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-1500}"

: "${HANDS_URL:?HANDS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"
: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или manual-<run_id>)}"
JOB_ID="${JOB_ID:-hands-${GITHUB_RUN_ID:-local}-$$}"
WORK="${RUNNER_TEMP:-/tmp}/dsh-hands"
mkdir -p "$WORK"
ANSWER_FILE="$WORK/answer.txt"
ERR_FILE="$WORK/stderr.txt"
EVENTS_FILE="$WORK/events.jsonl"
START_MARK="$WORK/.start-mark"
: >"$ANSWER_FILE"; : >"$ERR_FILE"; : >"$EVENTS_FILE"

api() { curl -fsS -H "Authorization: Bearer $HANDS_TOKEN" "$@"; }
api_post() {
  local path=$1 body=$2
  curl -fsS -X POST -H "Authorization: Bearer $HANDS_TOKEN" \
    -H "Content-Type: application/json" -d "$body" "$HANDS_URL$path"
}

SEQ=0
add_event() { # kind json_data
  SEQ=$((SEQ + 1))
  printf '{"seq":%s,"kind":"%s","ts":%s,"data":%s}\n' \
    "$SEQ" "$1" "$(date -u +%s000)" "${2:-null}" >>"$EVENTS_FILE"
}

flush_events() {
  if [ ! -s "$EVENTS_FILE" ]; then return 0; fi
  local body
  body=$(jq -s --arg t "$TASK_ID" '{task_id: $t, source: "job", events: .}' "$EVENTS_FILE")
  api_post /api/events "$body" >/dev/null
  : >"$EVENTS_FILE"
}

JOB_ENDED=0
# Единственная точка, где задача получает финальный статус; trap доводит дело до
# конца при любом выходе, повторная доставка не двоит журнал (UNIQUE task_id+seq).
post_job_end() { # result
  add_event "job_end" "{\"result\":\"$1\"}"
  flush_events || true
  JOB_ENDED=1
}

HB_PID=""
start_heartbeat() {
  while :; do
    sleep "$HEARTBEAT_SECS"
    api_post /api/heartbeat "{\"job_id\":\"$JOB_ID\",\"task_id\":\"$TASK_ID\"}" >/dev/null || true
  done
}
cleanup() {
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
  if [ "$JOB_ENDED" -eq 0 ]; then post_job_end "fail"; fi
}
trap cleanup EXIT

# ── 1. Текст задачи и посев seq — из журнала, не из догадок (спека п.2) ───────────
TASK_TEXT="${TASK_TEXT:-}"
after=0
while :; do
  resp=$(api "$HANDS_URL/api/events?task_id=$TASK_ID&after=$after&limit=256")
  n=$(jq '.events | length' <<<"$resp")
  if [ "$n" -eq 0 ]; then break; fi
  ms=$(jq '[.events[] | select(.source == "job") | .seq] | max // 0' <<<"$resp")
  if [ "$ms" -gt "$SEQ" ]; then SEQ=$ms; fi
  if [ -z "$TASK_TEXT" ]; then
    extracted=$(jq -r '
      ([.events[] | select(.kind == "task_queued")][0].data.payload // empty)
      | if type == "object" and has("task") and (.task | type == "string") then .task
        elif type == "string" then .
        else tostring end' <<<"$resp" 2>/dev/null || true)
    if [ -n "$extracted" ]; then TASK_TEXT=$extracted; fi
  fi
  has_more=$(jq -r '.has_more' <<<"$resp")
  after=$(jq -r '.next_after' <<<"$resp")
  if [ "$has_more" != "true" ]; then break; fi
done
if [ -z "$TASK_TEXT" ]; then
  echo "::error::Текст задачи не найден: в журнале $TASK_ID нет task_queued с payload.task" >&2
  exit 1
fi
echo "Задача $TASK_ID, seq посеян с $((SEQ + 1))"

add_event "job_start" "{\"job_id\":\"$JOB_ID\"}"
flush_events
start_heartbeat &
HB_PID=$!

# ── 2. Провайдер: без ключа работа невозможна — падаем сразу, не через 10 минут ───
if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
  echo "::error::DEEPSEEK_API_KEY не задан — DSH не сможет вызвать модель" >&2
  exit 1
fi
export DEEPSEEK_API_KEY
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://integrate.api.nvidia.com/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-nvidia/nemotron-3-super-120b-a12b}"

# ── 3. Установка DSH tarball'ами (npm install этих пакетов даёт 404) ──────────────
PKGS="$WORK/pkgs"
mkdir -p "$PKGS"
cd "$PKGS"
npm pack "@deepseek-ai/dsh@$DSH_VERSION" "@deepseek-ai/dsh-headless@$DSH_HEADLESS_VERSION"
npm install -g ./*.tgz
command -v dsh >/dev/null
dsh --version || true
add_event "note" "{\"text\":\"DSH $DSH_VERSION установлен\"}"
flush_events

# ── 4. Прогон: one-shot dsh-headless над этим репозиторием ────────────────────────
cd "${GITHUB_WORKSPACE:-$WORK}"
touch "$START_MARK"
DSH_START_TS=$(date -u +%s)
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$TASK_TEXT" >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))

# ── 5. Журнал: ответ и ход сессии ──────────────────────────────────────────────────
ANSWER=$(tail -c 60000 "$ANSWER_FILE")
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" '{text: $t, elapsed_s: $secs}')"

# Ход сессии: dsh-headless персистит и флашит сессию на диск; раскладка профиля
# до первого прогона не подтверждена — берём самый свежий файл, изменившийся
# во время прогона, и честно пишем, если не нашли.
SESSION_FILE=$(find "$HOME/.dsh" "$HOME/.config/dsh" -type f -newer "$START_MARK" 2>/dev/null | sort | tail -1 || true)
if [ -n "$SESSION_FILE" ]; then
  EXCERPT=$(tail -c 16000 "$SESSION_FILE")
  add_event "session_note" "$(jq -n --arg f "$SESSION_FILE" --arg x "$EXCERPT" '{file: $f, tail: $x}')"
else
  add_event "session_note" '{"text":"session-файл не найден — раскладку профиля уточнить по логам job"}'
fi

if [ "$rc" -eq 0 ]; then
  post_job_end "ok"
else
  ERRTEXT=$(tail -c 8000 "$ERR_FILE")
  add_event "agent_error" \
    "$(jq -n --arg t "$ERRTEXT" --argjson code "$rc" '{stderr: $t, exit_code: $code}')"
  flush_events
  post_job_end "fail"
  echo "::error::dsh завершился с кодом $rc" >&2
  exit 1
fi
