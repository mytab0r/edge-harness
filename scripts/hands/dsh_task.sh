#!/usr/bin/env bash
# Клиент рук слайса 1 (dsh-in-job): задача из морды → DSH headless → журнал.
# Контракт журнала — openspec/specs/journal-tasks-hands.md, дизайн —
# openspec/changes/dsh-in-job/design.md, критерии готовности — proposal/tasks там же.
# Правила: fail loud, без silent-wrong; heartbeat — доказательство живости процесса,
# а не прогресса DSH (durable-улика сессии — слайс 2).
set -euo pipefail

# Пины версий и целостности — единственное место правды по DSH в руках.
# integrity = dist.integrity из metadata реестра; сверяется с фактически
# скачанным tarball'ом — несовпадение это громкий отказ, а не warning.
DSH_VERSION="0.1.1-rc.2"
DSH_INTEGRITY="sha512-UP1UIh6q3Gme/yXRn/QL2P8IsVlv8Shpg22TRJIZPsCRWLm4CBiA1MUvXmJAfsOEETBMLAl+xWPtFw6ICsN3wg=="
# 0.0.1-rc.1 намеренно НЕ используется: тянет @deepseek-ai/dsh-code-runtime-worker,
# который в публичном npm отсутствует (tarball 404); с 0.0.1-rc.3 зависимость —
# dsh-code-runtime-worker-thread, она опубликована (проверено установкой, 475 пакетов).
DSH_HEADLESS_VERSION="0.1.1-rc.2"
DSH_HEADLESS_INTEGRITY="sha512-Pk50xwmUUehOxNe8DJ2/tThj7Aw1MmJQeUkfAQh9miF7Tm+WOOxiOOei/H4wjH9cf+FuqtbLDw6jrHmGotfhjw=="
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

# GH маскирует секреты только в своих логах; наш журнал — DO SQLite, туда уходит
# то, что мы постим. Затираем узнаваемые префиксы ключей до любой отправки.
redact() {
  sed -E -e 's/nvapi-[A-Za-z0-9_-]{4,}/nvapi-[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}/\1sk-[REDACTED]/g'
}

SEQ=0
add_event() { # kind json_data
  SEQ=$((SEQ + 1))
  printf '{"seq":%s,"kind":"%s","ts":%s,"data":%s}\n' \
    "$SEQ" "$1" "$(date -u +%s000)" "${2:-null}" >>"$EVENTS_FILE"
}

flush_events() {
  if [ ! -s "$EVENTS_FILE" ]; then return 0; fi
  local body attempt
  body=$(jq -s --arg t "$TASK_ID" '{task_id: $t, source: "job", events: .}' "$EVENTS_FILE")
  for attempt in 1 2 3 4 5; do
    if api_post /api/events "$body" >/dev/null; then
      : >"$EVENTS_FILE"
      return 0
    fi
    sleep $((attempt * 2))
  done
  echo "::error::Журнал не принял батч из 5 попыток — задача не может считаться завершённой" >&2
  return 1
}

JOB_ENDED=0
# Единственная точка финального статуса. job_end уходит ТОЛЬКО после того, как
# батч принят журналом: зелёный job с непринятым job_end — тот же silent-wrong.
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
cleanup() {
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
  if [ "$JOB_ENDED" -eq 0 ]; then
    add_event "agent_error" '{"stderr":"job завершён до финала (отмена/ошибка среды)"}'
    post_job_end "fail" || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT   # graceful-семантика dsh: SIGINT → 130
trap 'exit 143' TERM

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

# ── 3. Установка DSH: tarball + сверка целостности (supply-chain пин) ─────────────
PKGS="$WORK/pkgs"
mkdir -p "$PKGS"
cd "$PKGS"
npm pack "@deepseek-ai/dsh@$DSH_VERSION" "@deepseek-ai/dsh-headless@$DSH_HEADLESS_VERSION"
verify_integrity() { # file expected-integrity
  local actual
  actual="sha512-$(openssl dgst -sha512 -binary "$1" | openssl base64 -A)"
  if [ "$actual" != "$2" ]; then
    echo "::error::Integrity mismatch: $1 (ожидался $2, получен $actual)" >&2
    return 1
  fi
}
DSH_TGZ="deepseek-ai-dsh-$DSH_VERSION.tgz"
HL_TGZ="deepseek-ai-dsh-headless-$DSH_HEADLESS_VERSION.tgz"
[ -f "$DSH_TGZ" ] || DSH_TGZ=$(ls *dsh-$DSH_VERSION.tgz | head -1)
[ -f "$HL_TGZ" ] || HL_TGZ=$(ls *dsh-headless-$DSH_HEADLESS_VERSION.tgz | head -1)
verify_integrity "$DSH_TGZ" "$DSH_INTEGRITY"
verify_integrity "$HL_TGZ" "$DSH_HEADLESS_INTEGRITY"
npm install -g ./*.tgz
command -v dsh >/dev/null
dsh --version || true

# Выбор модели и лимит ответа — через родной settings-слой профиля:
# адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
DSH_MODEL="${DEEPSEEK_MODEL:-glm-5}"
DSH_MAX_TOKENS="${DSH_MAX_TOKENS:-131072}"
DSH_PATCH="$HOME/.dsh/profiles/headless/cordis.patch.yml"
mkdir -p "$(dirname "$DSH_PATCH")"
cat >"$DSH_PATCH" <<PATCH
- id: agent-default-model
  config:
    provider: deepseek-official
    model: $DSH_MODEL
- id: llm-deepseek
  config:
    maxTokens: $DSH_MAX_TOKENS
PATCH
add_event "bootstrap" "$(jq -n \
  --arg dsh "$DSH_VERSION" --arg hl "$DSH_HEADLESS_VERSION" \
  --arg node "$(node --version)" --arg model "$DSH_MODEL" \
  --argjson mt "$DSH_MAX_TOKENS" \
  '{dsh: $dsh, dsh_headless: $hl, node: $node, integrity: "verified", model: $model, max_tokens: $mt}')"
flush_events

# ── 4. Прогон: one-shot dsh-headless над этим репозиторием ────────────────────────
# cwd ДО старта становится корнем воркспейса и после не меняется (контракт dsh).
cd "${GITHUB_WORKSPACE:-$WORK}"
touch "$START_MARK"
DSH_START_TS=$(date -u +%s)
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$TASK_TEXT" >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))

# ── 5. Журнал: ответ и улики ───────────────────────────────────────────────────────
ANSWER=$(tail -c 60000 "$ANSWER_FILE" | redact)
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" '{text: $t, elapsed_s: $secs}')"

# Отладочная хвостовая улика профиля: НЕ доказательство сессии (durable-улики —
# слайс 2, родным плагином DSH). Читается только после смерти писателя; бинарные
# кодировки (.zstd) в журнал не тащим.
SESSION_FILE=$(find "$HOME/.dsh" "$HOME/.config/dsh" -type f -newer "$START_MARK" ! -name '*.zstd' 2>/dev/null | sort | tail -1 || true)
if [ -n "$SESSION_FILE" ]; then
  EXCERPT=$(tail -c 16000 "$SESSION_FILE" | redact)
  add_event "session_note" "$(jq -n --arg f "$SESSION_FILE" --arg x "$EXCERPT" \
    '{level: "debug", note: "хвост файла — не durable-улика, слайс 2 заменит", file: $f, tail: $x}')"
else
  add_event "session_note" '{level: "debug", note: "session-файл не найден — раскладку профиля уточнить по логам job"}'
fi

if [ "$rc" -eq 0 ]; then
  # Ответ обязан дойти до журнала ДО ok: флаш под set -e — падение красит job.
  flush_events
  post_job_end "ok"
else
  ERRTEXT=$(tail -c 8000 "$ERR_FILE" | redact)
  add_event "agent_error" \
    "$(jq -n --arg t "$ERRTEXT" --argjson code "$rc" '{stderr: $t, exit_code: $code}')"
  flush_events
  post_job_end "fail"
  echo "::error::dsh завершился с кодом $rc" >&2
  exit 1
fi
