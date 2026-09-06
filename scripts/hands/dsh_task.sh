#!/usr/bin/env bash
# Клиент рук (dsh-in-job слайс 1 + dsh-streaming слайс 2): задача из журнала →
# DSH headless → сессия в морде dsh-edge (#119). Контракт журнала —
# openspec/specs/journal-tasks-hands.md, дизайн стрима —
# openspec/changes/dsh-streaming/design.md, шов морды — openspec/changes/runner-sessions-in-dsh-morde/.
# Правила: fail loud, без silent-wrong; heartbeat — доказательство живости процесса,
# а не прогресса DSH; улика прогресса — транскрипт сессии в морде (плагин
# dsh-hands-streamer пишет NDJSON-спул, этот клиент дренирует его в DSH-сессию
# через scripts/lib/dsh-edge-session.sh). МЕХАНИЗМ ОДИН: журнал транскрипт
# больше не получает — только жизненный цикл job (замещает стрим #112).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины версий/целостности, установка и redact — единственное место правды:
# scripts/lib/dsh-ci.sh (общее с автономным воркером).
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"
# Шов сессии раннера в морду (#119): логин, begin, дрен спула в ingest, архив.
# shellcheck source=scripts/lib/dsh-edge-session.sh
source "$SCRIPT_DIR/../lib/dsh-edge-session.sh"
# Аренда задачи (#121): claim/release/locks — единственный вход в работу.
# shellcheck source=scripts/lib/lease.sh
source "$SCRIPT_DIR/../lib/lease.sh"

REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

HEARTBEAT_SECS="${HEARTBEAT_SECS:-20}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-1500}"
DRAIN_INTERVAL_SECS="${DRAIN_INTERVAL_SECS:-1}"
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIMEOUT=30       # зависший curl в api-подшелле вешал бы клиент до конца job

: "${HANDS_URL:?HANDS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"
: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или manual-<run_id>)}"
# Одно место правды — vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория (#153):
# зашитых фолбэков на конкретный эндпоинт/модель здесь больше нет. Проверяем
# в блоке обязательных переменных — ДО heartbeat, dsh_edge_login и создания
# сессии в морде: иначе конфиг-ошибка даёт пустую сессию в UI морды и задачу,
# помеченную провалом, вместо честного «не сконфигурировано».
dsh_require_provider_env || exit 1
JOB_ID="${JOB_ID:-hands-${GITHUB_RUN_ID:-local}-$$}"
WORK="${RUNNER_TEMP:-/tmp}/dsh-hands"
mkdir -p "$WORK"
ANSWER_FILE="$WORK/answer.txt"
ERR_FILE="$WORK/stderr.txt"
EVENTS_FILE="$WORK/events.jsonl"
START_MARK="$WORK/.start-mark"
AGENT_DIR="$WORK/agent"                       # каталог агента: спул пишет он сам (#140)
SPOOL_FILE="$AGENT_DIR/session-stream.ndjson" # NDJSON-спул плагина dsh-hands-streamer
# HANDS_SPOOL экспортируется ДО prepare (#140): prepare доказывает проводку
# каждой заданной env_keep-переменной агенту — включая спул.
export HANDS_SPOOL="$SPOOL_FILE"
SEQ_FILE="$WORK/.seq"                         # журнал-seq — единственный владелец: bash (этот клиент)
: >"$ANSWER_FILE"; : >"$ERR_FILE"; : >"$EVENTS_FILE"

# ── Изоляция адаптера модели (#140) — сразу после проверки провайдера и ДО
# аренды: отказ изоляции не должен оставлять живую аренду задачи. Руки — режим
# nogh: пуш/PR рукам запрещены по дизайну (GH_RUN_TOKEN снимается до старта
# DSH), gh-авторизации у агента нет и быть не должно; DSH_AGENT_PNPM=1 —
# плагин стрима ставится под агентом (dsh plugin add), gh-зеркало не нужно.
export DSH_AGENT_PNPM=1
dsh_agent_isolation_prepare nogh "${GITHUB_WORKSPACE:-$REPO_DIR}" "$AGENT_DIR" "$WORK/dsh-agent-launcher.sh"

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

# ── Журнал-seq: один писатель — bash (клиент рук). ────────────────────────────────
# Жизненный цикл job (job_start/bootstrap/agent_answer/stream_note/agent_error/
# job_end) — зона ЭТОГО файла: события с уникальным journal-seq уходят в журнал
# edge-harness. Транскрипт сессии (drain спула) сюда не ходит — он в морду через
# scripts/lib/dsh-edge-session.sh и на SEQ не влияет. seq_load — передача
# нумерации, если какой-то цикл временно заберёт писательство (точка возврата).
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

# ── Транскрипт сессии: дрен спула в морду (#119) ──────────────────────────────────
# Спул плагина — единственный источник; дрен живёт в scripts/lib/dsh-edge-session.sh
# и постит батчи строк спула в DSH-сессию (POST /api/sessions/:id/ingest).
# Журнал-seq остаётся за жизненным циклом job; дрен морды на SEQ не влияет.

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
  dsh_edge_stop_drain
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
  if [ "$JOB_ENDED" -eq 0 ]; then
    dsh_edge_drain_spool hard || true   # улики — до job_end; упавший дрен не отменяет финальный статус
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

# ── 1a. Аренда задачи (#121): заказ «поработай над issue-N» берётся только ────────
# через атомарный claim. Без замка морда-агент мог заказать работу по задаче,
# которую уже делает воркер, — два исполнителя на одной работе. Отказ —
# зелёный no-op job'а (контракт #121), но журнал получает честный финал
# task_busy + job_end: задача не должна висеть «dispatched» вечно, а «done»
# означал бы ложь «работа сделана». Задачи без issue (manual-*) — вне пула,
# аренды не имеют. Поломка утилиты — громкий красный job: «инструмент сломан»
# и «задача занята» — разные состояния.
if [[ "$TASK_ID" =~ ^issue-([0-9]+)$ ]]; then
  # CLAIM_ACTOR обязан быть валидным логином (назначение идёт им): не
  # переопределяем — current_actor() возьмёт GITHUB_ACTOR (аккаунт,
  # инициировавший dispatch). Канал для следа в задаче — CLAIM_VIA.
  export CLAIM_VIA="hands $TASK_ID (run ${GITHUB_RUN_ID:-local})"
  claim_out="$(lease_cli claim "${BASH_REMATCH[1]}" 2>&1)" && claim_rc=0 || claim_rc=$?
  if [ "$claim_rc" -eq 1 ]; then
    echo "Задача #${BASH_REMATCH[1]} занята другим исполнителем — зелёный no-op: $claim_out"
    add_event "agent_error" \
      "$(jq -n --arg t "$claim_out" '{error: "task_busy", detail: $t}')"
    post_job_end "fail"
    exit 0
  fi
  if [ "$claim_rc" -ne 0 ]; then
    echo "::error::claim_task сломался (rc=$claim_rc): $claim_out" >&2
    exit 1
  fi
  echo "Аренда взята: $claim_out"
fi

# Токен аренды больше не нужен никому ниже, включая DSH: снимается сразу после
# блока аренды и до любого выхода из скрипта. Раньше блока снимать нельзя —
# claim в проде авторизуется именно GH_RUN_TOKEN.
unset GH_RUN_TOKEN

add_event "job_start" "{\"job_id\":\"$JOB_ID\"}"
flush_events
start_heartbeat &
HB_PID=$!

# ── 1b. Сессия раннера в морде (#119): создать/переиспользовать и назвать ─────────
# Отказ громкий: без сессии ход работы владельцу не виден, job красный.
# «Не настроено» и «сломано» — разные сообщения (dsh_edge_require_config).
if [[ "$TASK_ID" =~ ^issue-([0-9]+)$ ]]; then
  HARNESS_SID="harness-${BASH_REMATCH[1]}"
  HARNESS_TITLE="#${BASH_REMATCH[1]}: $(head -n1 <<<"$TASK_TEXT" | cut -c1-160)"
else
  slug=$(printf '%s' "$TASK_ID" | tr '[:upper:]' '[:lower:]' | tr -cs 'A-Za-z0-9' '-' \
  | sed -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' | cut -c1-48)
  HARNESS_SID="harness-${slug:-manual}"
  HARNESS_TITLE="$(head -n1 <<<"$TASK_TEXT" | cut -c1-160)"
fi
dsh_edge_login || { echo "::error::Нет доступа к морде dsh-edge — job красный (#119)" >&2; exit 1; }
dsh_edge_session_begin "$HARNESS_SID" "$HARNESS_TITLE" >/dev/null \
  || { echo "::error::Сессия $HARNESS_SID не создана в морде — ход работы останется невидимым (#119)" >&2; exit 1; }
export DSH_EDGE_SESSION_ID="$HARNESS_SID"
echo "Сессия морды: $HARNESS_SID — «$HARNESS_TITLE»"

# ── 2. Провайдер: проверка выше (блок обязательных переменных) — здесь только export ──
# Профиль headless — pnpm-workspace: `pnpm add` внутри требует явного
# подтверждения root (иначе ERR_PNPM_ADDING_TO_ROOT, живой прогон 2026-08-30).
export npm_config_ignore_workspace_root_check=true
export DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL

# ── 3. Установка DSH: tarball + сверка целостности (supply-chain пин) ─────────────
PKGS="$WORK/pkgs"
dsh_install "$PKGS"
# --version — от транспорта: бинарник только читается, секретов в нём нет (#140).
dsh --version || true

# ── 3a. Модель и лимит ответа — settings-слой профиля, ДО монтажа плагина ─────────
# Порядок важен: --dump-config в 3b обязан доказывать монтаж плагина поверх
# ИТОГОВОГО патча профиля — ровно той конфигурации, с которой стартует dsh,
# а не промежуточной.
# Адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
# Патч пишет транспорт в свой каталог, в дом агента ставит агент (#140).
dsh_patch_profile headless "$WORK/agent-headless.cordis.patch.yml"
dsh_agent_run install -D -m 644 "$WORK/agent-headless.cordis.patch.yml" \
  "$DSH_AGENT_HOME/.dsh/profiles/headless/cordis.patch.yml"

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
# Монтаж и доказательство конфига — под агент-юзером: профиль живёт в его доме,
# доказывать надо конфиг ровно того пользователя, с которым стартует dsh (#140).
dsh_agent_run dsh plugin --profile headless add "$PLUGIN_TGZ"
dsh_agent_run dsh --profile headless --dump-config >"$WORK/dump-config.txt" 2>&1 \
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
# должен дочитывать старьё от предыдущей попытки. Курсор дрена — единственный
# владелец границы «принято мордой» (ретрай батча идёт от позиции, не от
# содержимого). Файлы принадлежат агент-юзеру (#140) — снос тоже под агентом:
# у транспорта нет права записи в каталог агента.
dsh_agent_run rm -f "$SPOOL_FILE" "$SPOOL_FILE.stats.json"
dsh_edge_start_drain
# Передача воркспейса агенту — последний транспортный шаг перед прогоном (#140).
dsh_agent_handover

DSH_START_TS=$(date -u +%s)
set +e
timeout "$DSH_TIMEOUT_SECS" dsh_agent_run dsh --profile headless "$TASK_TEXT" >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))

# Финальный drain — жёсткий и ДО ответа: транскрипт сессии в морде обязан
# обгонять финальный статус job в журнале.
dsh_edge_stop_drain
dsh_edge_drain_spool hard || { echo "::error::Хвост транскрипта не принят мордой" >&2; exit 1; }
drained_lines=$(cat "$DSH_EDGE_DRAIN_CURSOR" 2>/dev/null || echo 0)

# ── 5. Журнал: ответ и улики ───────────────────────────────────────────────────────
ANSWER=$(tail -c 60000 "$ANSWER_FILE" | redact)
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" '{text: $t, elapsed_s: $secs}')"

# Громкий отказ «стрим не доставил»: успешный прогон с пустым транскриптом
# морды — молчаливая деградация слоя доказательств, job обязан краснеть.
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
    add_event "agent_error" '{"error":"stream_no_events","stderr":"прогон успешен, а в сессию морды не доставлено ни одного события — транскрипт пуст (#119)"}'
    flush_events
    post_job_end "fail"
    echo "::error::Ноль событий в сессии морды при успешном прогоне — стрим не доставил событий" >&2
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
