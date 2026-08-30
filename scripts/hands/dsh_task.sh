#!/usr/bin/env bash
# Клиент рук (dsh-in-job слайс 1 + dsh-streaming слайс 2): задача из морды →
# DSH headless → журнал. Контракт журнала — openspec/specs/journal-tasks-hands.md,
# дизайн живого стрима — openspec/changes/dsh-streaming/design.md.
# Правила: fail loud, без silent-wrong; heartbeat — доказательство живости процесса,
# а не прогресса DSH; улика прогресса — события session_event (плагин
# dsh-hands-streamer пишет NDJSON-спул, этот клиент дренирует его в журнал).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины версий/целостности, установка и redact — единственное место правды:
# scripts/lib/dsh-ci.sh (общее с автономным воркером).
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"

REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

HEARTBEAT_SECS="${HEARTBEAT_SECS:-20}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-1500}"
DRAIN_INTERVAL_SECS="${DRAIN_INTERVAL_SECS:-1}"
DRAIN_BATCH=50            # потолок батча сервера по СТРОКАМ (LIMITS.batchMax, cf-worker/src/config.ts)
DRAIN_BATCH_BYTES=700000  # потолок батча по БАЙТАМ: тело сервера 1 MiB (LIMITS.bodyMaxBytes)
                          # с запасом ~300 KB на конверт события журнала (seq/kind/ts/обёртка)
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIMEOUT=30       # зависший curl в drain-подшеллe вешал бы стрим до конца job

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
SPOOL_FILE="$WORK/session-stream.ndjson"      # NDJSON-спул плагина dsh-hands-streamer
DRAIN_CURSOR="$WORK/.drain-cursor"            # сколько полных строк спула принято журналом
SEQ_FILE="$WORK/.seq"                         # журнал-seq — единственный владелец: bash (этот клиент)
: >"$ANSWER_FILE"; : >"$ERR_FILE"; : >"$EVENTS_FILE"

api() {
  curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIMEOUT" \
    -H "Authorization: Bearer $HANDS_TOKEN" "$@"
}
api_post() {
  local path=$1 body=$2
  curl -fsS --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIMEOUT" \
    -X POST -H "Authorization: Bearer $HANDS_TOKEN" \
    -H "Content-Type: application/json" -d "$body" "$HANDS_URL$path"
}

# ── Журнал-seq: один писатель — bash. ─────────────────────────────────────────────
# Два независимых счётчика (основной поток + drain-цикл) гонялись бы за одной
# парой UNIQUE(task_id, seq). Писатели здесь не гоняются: в каждый момент
# времени события добавляет ровно один из них (drain-цикл жив только между
# start_drain и stop_drain, основной поток не добавляет события в этом окне),
# а $SEQ_FILE — сквозное место правды счётчика для передачи нумерации.
# seq батча стрима привязан к ПОЗИЦИИ строки в спуле (DRAIN_BASE + индекс):
# повторная доставка тех же строк несёт ТЕ ЖЕ seq — сервер гасит дубль
# UNIQUE(task_id, seq), если батч был принят, а ответ потерялся. Верхняя
# граница аллокации пишется в файл до поста: сбой батча оставляет дырку
# (легальна), но не возвращает seq в оборот — чужое событие не встанет на
# чужой номер и не будет молча съедено серверным INSERT OR IGNORE.
SEQ=0
seq_persist() { printf '%s\n' "$SEQ" >"$SEQ_FILE"; }
seq_load() { SEQ=$(cat "$SEQ_FILE" 2>/dev/null || echo "$SEQ"); }

add_event() { # kind json_data
  SEQ=$((SEQ + 1))
  seq_persist
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

# ── Drain спула стрима (слайс 2) ──────────────────────────────────────────────────
# Новые полные строки спула → redact → journal-seq → батчи в POST /api/events.
# Батч режется сразу по обоим потолкам сервера: ≤DRAIN_BATCH строк И
# ≤DRAIN_BATCH_BYTES байт (тело 1 MiB). Курсор — число строк, ПРИНЯТЫХ
# журналом, обновляется после КАЖДОГО принятого батча: строки, дописанные
# плагином во время чтения, остаются на следующий тик, принятые не
# перечитываются. Незавершённая последняя строка (torn tail, обрыв записи без
# \n) не считается и не читается — доставляется только committed prefix.
# mode=soft: одна попытка на батч, сбой мягкий — ретрай следующим тиком
# (с теми же seq — привязка «строка ↔ seq» детерминирована).
# mode=hard: до 5 ретраев с бэкоффом, как у flush_events; несовместимый сбой —
# красный job.

post_session_batch() { # mode lines_file lo hi — seq строк: DRAIN_BASE+lo .. DRAIN_BASE+hi
  local mode=$1 lines=$2 lo=$3 hi=$4 total n=0 seq sline
  local batch_events="$WORK/drain-batch.events"
  : >"$batch_events"
  total=$(wc -l <"$lines")
  while [ "$n" -lt "$total" ]; do
    n=$((n + 1))
    seq=$((DRAIN_BASE + lo - 1 + n))
    sline=$(sed -n "${n}p" "$lines")
    printf '%s\n' "$sline" | jq -c --argjson s "$seq" '{
      seq: $s,
      kind: "session_event",
      ts: (if (.time | type) == "number" then .time else null end),
      data: ({session_id: .session_id, session_seq: .seq, type: .type}
             + (if (.data.turn // null) != null then {turn: .data.turn} else {} end)
             + (if (.data.step // null) != null then {step: .data.step} else {} end)
             + {payload: .data})
    }' >>"$batch_events"
  done
  # Верхняя граница аллокации — до поста: краш не возвращает seq в оборот.
  # Пост идёт с seq, детерминированными позициями строк, поэтому ретрай этого
  # же батча (тиком, хард-циклом или cleanup'ом) несёт ТЕ ЖЕ seq.
  SEQ=$((DRAIN_BASE + hi))
  seq_persist
  local body attempt
  body=$(jq -s --arg t "$TASK_ID" '{task_id: $t, source: "job", events: .}' "$batch_events")
  for attempt in 1 2 3 4 5; do
    if api_post /api/events "$body" >/dev/null; then return 0; fi
    [ "$mode" = "hard" ] || return 1
    sleep $((attempt * 2))
  done
  echo "::error::Журнал не принял батч session_event (строки спула $lo–$hi) из 5 попыток — улики прогресса потеряны не могут" >&2
  return 1
}

drain_spool() { # mode — 0: спул дочитан до конца; 1: сбой поста, ретрай позже
  local mode=$1 drained lo end bl batch_lines
  drained=$(cat "$DRAIN_CURSOR" 2>/dev/null || echo 0)
  [ -f "$SPOOL_FILE" ] || return 0
  # Сравнение с курсором по wc спула — только быстрый выход на пустом тике:
  # tail читает ДО EOF, и строки, дописанные после wc, попадают в чанк
  # и учитываются курсором по факту приёма (курсор считает принятое, а не
  # устаревший wc). Незавершённый хвост (без \n) не считается: wc выдаёт
  # число полных строк — committed prefix.
  local avail chunk_lines
  avail=$(wc -l <"$SPOOL_FILE")
  [ "$avail" -gt "$drained" ] || return 0
  tail -n +"$((drained + 1))" "$SPOOL_FILE" | redact >"$WORK/drain-chunk.jsonl"
  chunk_lines=$(wc -l <"$WORK/drain-chunk.jsonl")
  [ "$chunk_lines" -gt 0 ] || return 0
  # Нарезка по обоим потолкам сразу: ≤50 строк и ≤бюджет байт на батч
  # (строка, не влезающая в бюджет, начинает СЛЕДУЮЩИЙ батч — инвариант
  # «батч ≤ бюджета» строгий). head отсекает возможный незавершённый хвост,
  # который awk иначе дочитал бы как полную запись. LC_ALL=C — length() в
  # байтах, не в символах локали; имена батчей с нулём-набивкой: glob обязан
  # давать числовой порядок.
  head -n "$chunk_lines" "$WORK/drain-chunk.jsonl" | LC_ALL=C \
    awk -v ml="$DRAIN_BATCH" -v mb="$DRAIN_BATCH_BYTES" -v out="$WORK/drain-batch" '
      function flushbatch() { printf "%s", buf > (out "." sprintf("%03d", ++k) ".lines"); buf = ""; n = 0; b = 0 }
      { len = length($0) + 1
        if (n >= ml || (n > 0 && b + len > mb)) flushbatch()
        buf = buf $0 "\n"; n++; b += len }
      END { if (n > 0) flushbatch() }'
  lo=$drained
  for batch_lines in "$WORK"/drain-batch.*.lines; do
    [ -e "$batch_lines" ] || continue   # шаблон без совпадений (nullglob выключен)
    bl=$(wc -l <"$batch_lines")
    end=$((lo + bl))
    post_session_batch "$mode" "$batch_lines" "$((lo + 1))" "$end" || return 1
    lo=$end
    echo "$lo" >"$DRAIN_CURSOR"   # курсор растёт с каждым ПРИНЯТЫМ батчем
  done
}

DRAIN_PID=""
DRAIN_BASE=0
start_drain() {
  # База дрена: seq строки спула = DRAIN_BASE + позиция строки. Фиксируется
  # один раз на прогон — детерминированная привязка «строка ↔ seq» на все
  # ретраи (софт-тики, хард-цикл, cleanup). Основной поток в окне дрена seq
  # не двигает (один писатель).
  DRAIN_BASE=$SEQ
  (
    while :; do
      sleep "$DRAIN_INTERVAL_SECS"
      drain_spool soft || true   # мягкий сбой: батч остаётся в спуле до следующего тика
    done
  ) &
  DRAIN_PID=$!
}

stop_drain() {
  if [ -n "$DRAIN_PID" ]; then
    kill "$DRAIN_PID" 2>/dev/null || true
    wait "$DRAIN_PID" 2>/dev/null || true
    DRAIN_PID=""
  fi
  seq_load   # нумерация возвращается основному потоку без коллизий
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
  stop_drain   # после него add_event снова принадлежит основному потоку
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
  if [ "$JOB_ENDED" -eq 0 ]; then
    drain_spool hard || true   # улики — до job_end; упавший дрен не отменяет финальный статус
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
seq_persist
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
# Профиль headless — pnpm-workspace: `pnpm add` внутри требует явного
# подтверждения root (иначе ERR_PNPM_ADDING_TO_ROOT, живой прогон 2026-08-30).
export npm_config_ignore_workspace_root_check=true
export DEEPSEEK_API_KEY
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://integrate.api.nvidia.com/v1}"
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-nvidia/nemotron-3-super-120b-a12b}"

# ── 3. Установка DSH: tarball + сверка целостности (supply-chain пин) ─────────────
PKGS="$WORK/pkgs"
dsh_install "$PKGS"
dsh --version || true

# ── 3a. Модель и лимит ответа — settings-слой профиля, ДО монтажа плагина ─────────
# Порядок важен: --dump-config в 3b обязан доказывать монтаж плагина поверх
# ИТОГОВОГО патча профиля — ровно той конфигурации, с которой стартует dsh,
# а не промежуточной.
# Адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
dsh_patch_profile headless

# ── 3b. Плагин стрима: bundle-механизм профиля, факт монтажа доказывается здесь ───
# (dsh-streaming, проверка допущений 0: `dsh plugin add` + `--dump-config`
# подтверждены живьём). tarball собирается из этого же чекаута — отдельный пин
# не нужен, версия приезжает вместе с клиентом. Плагин БЕЗ сети: транспорт и
# ретраи — только здесь; pnpm-форвардер — штатная механика `dsh plugin`.
# Проверка идёт ПОСЛЕ записи модельного патча (3a): dump-config доказывает
# совместный слой «модель + плагин». initProfile пишет файлы профиля только
# при отсутствии — наш патч при `dsh plugin add` не перезаписывается
# (docs/research/10-dsh-architecture.md, замер 2026-08-30).
command -v pnpm >/dev/null || { echo "::error::pnpm не найден — dsh plugin add без него не работает" >&2; exit 1; }
PLUGIN_TGZ="$WORK/dsh-hands-streamer.tgz"
npm pack "$REPO_DIR/scripts/dsh-hands-streamer" --pack-destination "$WORK" >/dev/null
mv "$WORK"/dsh-hands-streamer-*.tgz "$PLUGIN_TGZ"
dsh plugin --profile headless add "$PLUGIN_TGZ"
dsh --profile headless --dump-config >"$WORK/dump-config.txt" 2>&1 \
  || { echo "::error::dsh --dump-config упал — профиль headless не собирается" >&2; exit 1; }
grep -q '^- id: hands-streamer$' "$WORK/dump-config.txt" \
  || { echo "::error::плагин hands-streamer не смонтировался: --dump-config без его строки; стрим событий невозможен" >&2; exit 1; }
echo "Плагин hands-streamer смонтирован в профиль headless (вместе с модельным патчем)"
add_event "bootstrap" "$(jq -n \
  --arg dsh "$DSH_VERSION" --arg hl "$DSH_HEADLESS_VERSION" \
  --arg node "$(node --version)" --arg model "$DSH_MODEL" \
  --argjson mt "$DSH_MAX_TOKENS" \
  '{dsh: $dsh, dsh_headless: $hl, node: $node, integrity: "verified", model: $model, max_tokens: $mt, stream_plugin: "hands-streamer"}')"
flush_events

# ── 4. Прогон: one-shot dsh-headless над этим репозиторием ────────────────────────
# cwd ДО старта становится корнем воркспейса и после не меняется (контракт dsh).
cd "${GITHUB_WORKSPACE:-$WORK}"
touch "$START_MARK"

# Спул стрима: путь задаётся плагину через env до старта dsh; чистый прогон не
# должен дочитывать старьё от предыдущей попытки. Пока жив drain-цикл, события
# в журнал добавляет ТОЛЬКО он (один писатель journal-seq).
rm -f "$SPOOL_FILE" "$SPOOL_FILE.stats.json" "$DRAIN_CURSOR"
export HANDS_SPOOL="$SPOOL_FILE"
start_drain

DSH_START_TS=$(date -u +%s)
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$TASK_TEXT" >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))

# Финальный drain — жёсткий и ДО ответа: порядок журнала «события сессии →
# финальный ответ → job_end» держится journal-seq, а не временем прихода.
stop_drain
drain_spool hard
drained_lines=$(cat "$DRAIN_CURSOR" 2>/dev/null || echo 0)

# ── 5. Журнал: ответ и улики ───────────────────────────────────────────────────────
ANSWER=$(tail -c 60000 "$ANSWER_FILE" | redact)
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" '{text: $t, elapsed_s: $secs}')"

# Громкий отказ «стрим не доставил»: успешный прогон с нулём доставленных
# session_event — молчаливая деградация слоя доказательств, job обязан краснеть.
# «Спул не создан вовсе» — другой отказ: плагин не смонтировался ≠ событий не было.
if [ "$rc" -eq 0 ]; then
  if [ ! -f "$SPOOL_FILE" ]; then
    add_event "agent_error" '{"error":"stream_plugin_not_mounted","stderr":"прогон успешен, а спул стрима не создан — плагин hands-streamer не писал, хотя dump-config его смонтировал"}'
    flush_events
    post_job_end "fail"
    echo "::error::Спул стрима не создан при успешном прогоне — плагин не работал" >&2
    exit 1
  fi
  if [ "$drained_lines" -eq 0 ]; then
    add_event "agent_error" '{"error":"stream_no_events","stderr":"прогон успешен, а из спула не доставлен ни один session_event — стрим не доставил событий"}'
    flush_events
    post_job_end "fail"
    echo "::error::Ноль session_event при успешном прогоне — стрим не доставил событий" >&2
    exit 1
  fi
fi

# Статистика плагина (счётчики отброшенного, capped) — одна строка stream_note
# на прогон вместо строк на каждое событие. capped — warn: деградация объявлена.
if [ -f "$SPOOL_FILE.stats.json" ]; then
  jq -e . "$SPOOL_FILE.stats.json" >/dev/null 2>&1 || { echo '{"accepted":null,"capped":false,"note":"stats повреждён"}' >"$SPOOL_FILE.stats.json"; }
  add_event "stream_note" "$(jq -n \
    --slurpfile s "$SPOOL_FILE.stats.json" \
    --argjson drained "${drained_lines:-0}" \
    '{level: (if ($s[0].capped // false) then "warn" else "debug" end),
      note: "статистика стрима сессии",
      spool: {accepted: $s[0].accepted, drained: $drained, dropped: ($s[0].dropped // {}), capped: ($s[0].capped // false)}}')"
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
