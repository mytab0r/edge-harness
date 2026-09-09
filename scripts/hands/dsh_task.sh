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
#
# Ретрай временного RATE_LIMIT провайдера (#422, механизм #419/#421 —
# dsh_run_with_retry в lib/dsh-ci.sh, остаётся заботой КАЖДОЙ попытки цепочки
# ниже). Бюджет ожидания HANDS_RATE_LIMIT_MAX_WAIT_SECS (по умолчанию
# 600с/10 мин) короче, чем у ai-review/воркера (30 мин) нарочно: канал рук —
# интерактивный (репозиторный dispatch из морды или ручной запуск),
# собственный job живёт всего 30 мин (timeout-minutes), и держать раннер
# занятым треть часа ради окна, которое обычно снимается секундами
# (docs/runbooks/switch-llm-provider.md), не оправдано — короче отказать и
# вернуть задачу в пул, чем занимать редкий Free-план слот. При исчерпании
# бюджета, недельной/месячной квоте ОДНОГО провайдера, или исчерпании ВСЕЙ
# цепочки (#727/#805) задача (issue-N, если была аренда) возвращается в пул
# СРАЗУ: lease_cli release-full, не 24-часовой TTL-сборщик — вина не в задаче.
#
# Цепочка провайдеров (#727, довод #805 — тот же механизм, что уже несёт
# ai-review.yml и worker.yml, #797): dsh_require_provider_env/
# dsh_run_with_retry заменены на dsh_require_provider_chain/
# dsh_run_with_provider_chain (lib/dsh-ci.sh). Профиль затравлен ПЕРВЫМ
# провайдером цепочки (chain[0]) ДО первого `dsh` этого прогона (dsh plugin
# add, шаг 3d) — тот же приём, что у worker.yml (initProfile не перезаписывает
# уже существующий cordis.patch.yml дефолтом); цепочка перепатчивает профиль
# заново на каждую попытку внутри шага 4 — тот же dsh_patch_profile, не
# второй механизм.
#
# Особый риск рук, которого нет у worker.yml (тот не пишет в журнал вовсе):
# событие `bootstrap` ниже (шаг 3d) уходит в журнал ДО первой попытки
# цепочки — на тот момент известен только `chain[0]`, не тот провайдер,
# который в итоге ответит. Решение (не молчаливый пропуск, #805): `bootstrap`
# называет `chain[0]` явно как ПЕРВОГО КАНДИДАТА (`provider_chain.head_model`/
# `provider_chain.candidates`), не как факт «эта модель ответила»; факт
# определяется ПОСЛЕ прогона (шаг 4/5) и уходит уже существующим,
# безусловным событием `agent_answer` новыми полями `provider`/`model`
# (значение `$DSH_MODEL`/`$DSH_CHAIN_PROVIDER` после
# `dsh_run_with_provider_chain` — эта функция перепатчивает профиль на
# КАЖДОЙ попытке, поэтому оба глобальных значения после её возврата
# отражают именно последнего опробованного/успешного провайдера, тот же,
# что и реально обслужил вызов при rc=0).
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
# Бюджет ретрая временного RATE_LIMIT (#422) — обоснование см. в шапке файла.
HANDS_RATE_LIMIT_MAX_WAIT_SECS="${HANDS_RATE_LIMIT_MAX_WAIT_SECS:-600}"
HANDS_RATE_LIMIT_INITIAL_DELAY_SECS="${HANDS_RATE_LIMIT_INITIAL_DELAY_SECS:-15}"
HANDS_RATE_LIMIT_MAX_DELAY_SECS="${HANDS_RATE_LIMIT_MAX_DELAY_SECS:-120}"
DRAIN_INTERVAL_SECS="${DRAIN_INTERVAL_SECS:-1}"
CURL_CONNECT_TIMEOUT=5
CURL_MAX_TIMEOUT=30       # зависший curl в api-подшелле вешал бы клиент до конца job

: "${HANDS_URL:?HANDS_URL не задан}"
: "${HANDS_TOKEN:?HANDS_TOKEN не задан}"
: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или manual-<run_id>)}"
# Цепочка приходит из манифеста использования (openspec/changes/
# llm-provider-usage-manifest, config/provider-usage.json, потребитель
# "hands") — dsh_require_provider_chain резолвит её по id ПЕРЕД обычной
# валидацией; манифеста нет вовсе — фоллбэк на vars.DSH_PROVIDER_CHAIN
# (#727/#805) как раньше. Проверяем в блоке обязательных переменных — ДО
# heartbeat, dsh_edge_login и создания сессии в морде: иначе конфиг-ошибка
# даёт пустую сессию в UI морды и задачу, помеченную провалом, вместо
# честного «не сконфигурировано».
dsh_require_provider_chain "hands" || exit 1
JOB_ID="${JOB_ID:-hands-${GITHUB_RUN_ID:-local}-$$}"
WORK="${RUNNER_TEMP:-/tmp}/dsh-hands"
mkdir -p "$WORK"
ANSWER_FILE="$WORK/answer.txt"
ERR_FILE="$WORK/stderr.txt"
EVENTS_FILE="$WORK/events.jsonl"
START_MARK="$WORK/.start-mark"
SPOOL_FILE="$WORK/session-stream.ndjson"      # NDJSON-спул плагина dsh-hands-streamer
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
ISSUE_NUMBER=""
if [[ "$TASK_ID" =~ ^issue-([0-9]+)$ ]]; then
  ISSUE_NUMBER="${BASH_REMATCH[1]}"
  # CLAIM_ACTOR обязан быть валидным логином (назначение идёт им): не
  # переопределяем — current_actor() возьмёт GITHUB_ACTOR (аккаунт,
  # инициировавший dispatch). Канал для следа в задаче — CLAIM_VIA.
  export CLAIM_VIA="hands $TASK_ID (run ${GITHUB_RUN_ID:-local})"
  claim_out="$(lease_cli claim "$ISSUE_NUMBER" 2>&1)" && claim_rc=0 || claim_rc=$?
  if [ "$claim_rc" -eq 1 ]; then
    echo "Задача #$ISSUE_NUMBER занята другим исполнителем — зелёный no-op: $claim_out"
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
  # task-branch (playbook, шаг 3) тоже умеет арендовать (#356, внешний путь) —
  # DSH-агент запускается ниже как дочерний процесс этого шелла и наследует
  # флаг. Флаг несёт НОМЕР арендованной задачи (не булев признак): task-branch
  # сверяет его с номером СВОЕЙ ветки и пропускает claim только при совпадении
  # — булев признак молча пропустил бы аренду ВТОРОЙ задачи, если за один
  # прогон создаётся вторая ветка на другой номер (playbook это допускает).
  export LEASE_ALREADY_CLAIMED="${BASH_REMATCH[1]}"
fi

# Токен аренды больше не нужен никому ниже, включая DSH: снимается сразу после
# блока аренды и до любого выхода из скрипта. Раньше блока снимать нельзя —
# claim в проде авторизуется именно GH_RUN_TOKEN. Копия в НЕэкспортируемую
# переменную (#422) переживает unset: если провайдер окажется в лимите,
# release-full в конце скрипта происходит уже ПОСЛЕ выхода DSH — не
# экспортируется и агенту не видна, тот же trust-zone, что и раньше.
LEASE_RELEASE_TOKEN="${GH_RUN_TOKEN:-}"
unset GH_RUN_TOKEN

add_event "job_start" "{\"job_id\":\"$JOB_ID\"}"
flush_events
start_heartbeat &
HB_PID=$!

# ── 1b. Сессия раннера в морде (#119): создать/переиспользовать и назвать ─────────
# Отказ громкий: без сессии ход работы владельцу не виден, job красный.
# «Не настроено» и «сломано» — разные сообщения (dsh_edge_require_config).
if [ -n "$ISSUE_NUMBER" ]; then
  HARNESS_SID="harness-${ISSUE_NUMBER}"
  HARNESS_TITLE="#${ISSUE_NUMBER}: $(head -n1 <<<"$TASK_TEXT" | cut -c1-160)"
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

# ── 2. Профиль headless — pnpm-workspace ──────────────────────────────────────────
# `pnpm add` внутри требует явного подтверждения root (иначе
# ERR_PNPM_ADDING_TO_ROOT, живой прогон 2026-08-30). Провайдер (DEEPSEEK_*)
# экспортируется ниже, на шаге 3b, из ПЕРВОГО элемента цепочки — не здесь:
# цепочка проверена выше (dsh_require_provider_chain), но конкретные
# значения известны только после разбора vars.DSH_PROVIDER_CHAIN.
export npm_config_ignore_workspace_root_check=true

# ── 3. Установка DSH: tarball + сверка целостности (supply-chain пин) ─────────────
PKGS="$WORK/pkgs"
dsh_install "$PKGS"
dsh --version || true

# ── 3a. Suite ротации учёток (#215) — скачивание+проверка ДО патча профиля,
# монтаж (dsh plugin add) — ПОСЛЕ (см. dsh_install_plugins_suite в lib для
# обоснования порядка, тот же приём, что уже доказан ниже для hands-streamer).
dsh_install_plugins_suite "$WORK/plugins-suite" \
  || { echo "::error::suite ротации учёток не установился (см. ::error:: выше, #215)" >&2; exit 1; }
# Быстрый провайдер Claude (#838) — независимо от suite выше, гейт: секреты
# ANTHROPIC_OAUTH_1/2, не vars.PLUGINS_SUITE_URL.
dsh_install_anthropic_pool "$WORK/anthropic-pool" \
  || { echo "::error::быстрый провайдер Claude не установился (см. ::error:: выше, #838)" >&2; exit 1; }
dsh_import_anthropic_accounts \
  || { echo "::error::импорт аккаунтов Claude не удался (см. ::error:: выше, #838)" >&2; exit 1; }

# ── 3b. Модель и лимит ответа — settings-слой профиля, ДО монтажа плагина ─────────
# Порядок важен: --dump-config в 3d обязан доказывать монтаж плагина поверх
# ИТОГОВОГО патча профиля — ровно той конфигурации, с которой стартует dsh,
# а не промежуточной.
# Адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
#
# Затравка профиля ПЕРВЫМ провайдером цепочки (chain[0]) — тот же приём, что
# worker.yml/task.sh (#797): ОБЯЗАНА случиться ДО первого `dsh` этого прогона
# (dsh plugin add, шаг 3d) — «initProfile пишет package.json/cordis.patch.yml/
# pnpm-workspace.yaml только при отсутствии, ничего не перезаписывает»
# (research/10-dsh-architecture.md) — если файл патча ещё не существует к
# моменту plugin add, initProfile создаст его сам с содержимым, которое
# отсюда не контролируется. На шаге 4 dsh_run_with_provider_chain
# перепатчивает профиль ЗАНОВО на каждую попытку (тот же dsh_patch_profile,
# полная перезапись файла) — здесь важен только факт, что файл СУЩЕСТВУЕТ к
# моменту первого `dsh`, монтаж плагина (отдельный слой bundles) этим не
# затрагивается.
_chain_head=$(jq -c '.[0]' <<<"$DSH_PROVIDER_CHAIN")
_chain_head_secret=$(jq -r '.secret_env' <<<"$_chain_head")
DEEPSEEK_BASE_URL=$(jq -r '.base_url' <<<"$_chain_head")
DEEPSEEK_MODEL=$(jq -r '.model' <<<"$_chain_head")
DEEPSEEK_API_KEY="${!_chain_head_secret:-}"
export DEEPSEEK_BASE_URL DEEPSEEK_MODEL DEEPSEEK_API_KEY
DSH_MAX_TOKENS=$(jq -r '.max_output_tokens // 131072' <<<"$_chain_head") dsh_patch_profile headless

# ── 3c. Монтаж suite (после патча — тот же порядок, что доказан для
# hands-streamer в 3d ниже) ────────────────────────────────────────────────────
dsh_mount_plugins_suite headless \
  || { echo "::error::suite ротации учёток не смонтировался (см. ::error:: выше, #215)" >&2; exit 1; }
dsh_mount_anthropic_pool headless \
  || { echo "::error::быстрый провайдер Claude не смонтировался (см. ::error:: выше, #838)" >&2; exit 1; }

# ── 3d. Плагин стрима: bundle-механизм профиля, факт монтажа доказывается здесь ───
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
# bootstrap уходит ДО первой попытки цепочки (#727/#805) — на этот момент
# известен только `chain[0]` (уже засеянный в профиль на шаге 3b), а не тот
# провайдер, который в итоге ответит на вызов. Событие называет его ЯВНО как
# первого кандидата (provider_chain.head/candidates), не как факт «эта модель
# ответила» — иначе рассинхрон «bootstrap называет одну модель, ответила
# другая» был бы silent-wrong. Факт, какой провайдер реально обслужил вызов,
# появляется ПОСЛЕ прогона в agent_answer (шаг 5, поля provider/model).
add_event "bootstrap" "$(jq -n \
  --arg dsh "$DSH_VERSION" --arg hl "$DSH_HEADLESS_VERSION" \
  --arg node "$(node --version)" --arg head "$DSH_MODEL" \
  --argjson mt "$DSH_MAX_TOKENS" \
  --argjson candidates "$(jq -c '[.[].name]' <<<"$DSH_PROVIDER_CHAIN")" \
  '{dsh: $dsh, dsh_headless: $hl, node: $node, integrity: "verified",
    provider_chain: {head_model: $head, candidates: $candidates},
    max_tokens: $mt, stream_plugin: "hands-streamer"}')"
flush_events

# ── 4. Прогон: one-shot dsh-headless над этим репозиторием ────────────────────────
# cwd ДО старта становится корнем воркспейса и после не меняется (контракт dsh).
cd "${GITHUB_WORKSPACE:-$WORK}"
touch "$START_MARK"

# Спул стрима: путь задаётся плагину через env до старта dsh; чистый прогон не
# должен дочитывать старьё от предыдущей попытки. Курсор дрена — единственный
# владелец границы «принято мордой» (ретрай батча идёт от позиции, не от содержимого).
rm -f "$SPOOL_FILE" "$SPOOL_FILE.stats.json"
export HANDS_SPOOL="$SPOOL_FILE"
dsh_edge_start_drain

DSH_START_TS=$(date -u +%s)
HANDS_TASK_FAILURE_REASON=""
DSH_RATE_LIMIT_MAX_WAIT_SECS="$HANDS_RATE_LIMIT_MAX_WAIT_SECS" \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS="$HANDS_RATE_LIMIT_INITIAL_DELAY_SECS" \
DSH_RATE_LIMIT_MAX_DELAY_SECS="$HANDS_RATE_LIMIT_MAX_DELAY_SECS" \
  dsh_run_with_pool_then_chain "$ANSWER_FILE" "$ERR_FILE" "$TASK_TEXT"
rc=$DSH_RUN_RC
HANDS_TASK_FAILURE_REASON="$DSH_RUN_FAILURE_REASON"
HANDS_CHAIN_PROVIDER="$DSH_CHAIN_PROVIDER"
HANDS_CHAIN_TRIED="$DSH_CHAIN_TRIED"
HANDS_CHAIN_RESET_HINT="$DSH_CHAIN_RESET_HINT"
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))
echo "dsh завершился с кодом $rc (провайдер: ${HANDS_CHAIN_PROVIDER:-нет успеха}, опробованы: ${HANDS_CHAIN_TRIED:-?})"

# Финальный drain — жёсткий и ДО ответа: транскрипт сессии в морде обязан
# обгонять финальный статус job в журнале.
dsh_edge_stop_drain
dsh_edge_drain_spool hard || { echo "::error::Хвост транскрипта не принят мордой" >&2; exit 1; }
drained_lines=$(cat "$DSH_EDGE_DRAIN_CURSOR" 2>/dev/null || echo 0)

# ── 5. Журнал: ответ и улики ───────────────────────────────────────────────────────
# provider/model — ФАКТ по итогу цепочки (#727/#805), не затравка bootstrap:
# dsh_run_with_provider_chain перепатчивает профиль (dsh_patch_profile) на
# КАЖДОЙ попытке, поэтому $DSH_MODEL после её возврата — модель ПОСЛЕДНЕГО
# опробованного провайдера (успешного при rc=0, последнего при отказе/
# исчерпании цепочки целиком) — тот же провайдер, что реально принял вызов.
# Это безусловное событие (уходит и на успехе, и на провале) — единственное
# место, где рассинхрон bootstrap/факт закрывается: bootstrap выше называет
# только первого кандидата (provider_chain.head_model), это событие — то,
# что случилось на самом деле.
ANSWER=$(tail -c 60000 "$ANSWER_FILE" | redact)
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" \
      --arg provider "${HANDS_CHAIN_PROVIDER:-}" --arg model "${DSH_MODEL:-}" \
      --arg tried "${HANDS_CHAIN_TRIED:-}" \
      '{text: $t, elapsed_s: $secs,
        provider: (if $provider == "" then null else $provider end),
        model: (if $model == "" then null else $model end),
        provider_chain_tried: (if $tried == "" then null else $tried end)}')"

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

# Рендер транскрипта (#131): доставка в морду не значит, что владелец увидит
# актуальные provider/model и раскрытые детали тула — структурный инвариант
# формы батча (research/12), best-effort (не роняет успешный прогон).
transcript_issues="[]"
if [ "${drained_lines:-0}" -gt 0 ]; then
  verify_out=$(dsh_edge_verify_transcript "$DSH_EDGE_SESSION_ID" 2>&1 >/dev/null) && verify_rc=0 || verify_rc=$?
  if [ "$verify_rc" != 0 ]; then
    transcript_issues=$(printf '%s\n' "$verify_out" | jq -R . | jq -s .)
  fi
fi

# Статистика плагина (счётчики отброшенного, capped) — одна строка stream_note
# на прогон вместо строк на каждое событие. capped/транскрипт-находки — warn.
if [ -f "$SPOOL_FILE.stats.json" ]; then
  jq -e . "$SPOOL_FILE.stats.json" >/dev/null 2>&1 || { echo '{"accepted":null,"capped":false,"note":"stats повреждён"}' >"$SPOOL_FILE.stats.json"; }
  add_event "stream_note" "$(jq -n \
    --slurpfile s "$SPOOL_FILE.stats.json" \
    --argjson drained "${drained_lines:-0}" \
    --argjson transcript_issues "$transcript_issues" \
    '{level: (if (($s[0].capped // false) or ($transcript_issues | length) > 0) then "warn" else "debug" end),
      note: "статистика стрима сессии",
      spool: {accepted: $s[0].accepted, drained: $drained, dropped: ($s[0].dropped // {}), capped: ($s[0].capped // false)},
      transcript_render: {issues: $transcript_issues}}')"
fi

if [ "$rc" -eq 0 ]; then
  # Ответ обязан дойти до журнала ДО ok: флаш под set -e — падение красит job.
  flush_events
  post_job_end "ok"
else
  ERRTEXT=$(tail -c 8000 "$ERR_FILE" | redact)
  # Провайдер в лимите (#422) — не сбой агента: событие журнала различает это
  # явным полем failure_reason, а не общим stderr (правило AGENTS.md —
  # «возможности нет» и «возможность есть, но сломана» лечатся по-разному).
  add_event "agent_error" \
    "$(jq -n --arg t "$ERRTEXT" --argjson code "$rc" --arg reason "$HANDS_TASK_FAILURE_REASON" \
        --arg tried "${HANDS_CHAIN_TRIED:-}" --arg reset "${HANDS_CHAIN_RESET_HINT:-}" \
        '{stderr: $t, exit_code: $code, failure_reason: (if $reason == "" then null else $reason end),
          provider_chain_tried: (if $tried == "" then null else $tried end),
          provider_chain_reset_hint: (if $reset == "" then null else $reset end)}')"
  flush_events
  # Провайдер в лимите/квоте надолго ИЛИ цепочка исчерпана целиком (#727/#805)
  # — вина не в задаче: возвращаем её в пул СРАЗУ (замок + назначение), не
  # дожидаясь 24-часового TTL-сборщика. Только для задач issue-N: manual-*
  # аренды не имеют, снимать нечего.
  if [ -n "$ISSUE_NUMBER" ] && { [ "$HANDS_TASK_FAILURE_REASON" = "quota_exhausted" ] || \
      [ "$HANDS_TASK_FAILURE_REASON" = "rate_limit_retry_budget_exceeded" ] || \
      [ "$HANDS_TASK_FAILURE_REASON" = "all_providers_exhausted" ]; }; then
    release_out="$(GH_RUN_TOKEN="$LEASE_RELEASE_TOKEN" lease_cli release-full "$ISSUE_NUMBER" 2>&1)" \
      && release_rc=0 || release_rc=$?
    if [ "$release_rc" -eq 0 ]; then
      echo "Провайдер в лимите — задача #$ISSUE_NUMBER возвращена в пул немедленно: $release_out"
    else
      echo "::warning::задача #$ISSUE_NUMBER не возвращена в пул (rc=$release_rc): $release_out — снимет TTL-сборщик через 24 ч"
    fi
  fi
  post_job_end "fail"
  case "$HANDS_TASK_FAILURE_REASON" in
    quota_exhausted)
      echo "::error::провайдер: квота исчерпана надолго (RATE_LIMIT: Weekly/Monthly Limit Exhausted, код возврата $rc) — не сбой агента, см. docs/runbooks/switch-llm-provider.md" >&2
      ;;
    rate_limit_retry_budget_exceeded)
      echo "::error::провайдер: временный RATE_LIMIT не снялся за бюджет ожидания ${HANDS_RATE_LIMIT_MAX_WAIT_SECS}с (код возврата $rc) — не сбой агента" >&2
      ;;
    all_providers_exhausted)
      echo "::error::цепочка провайдеров исчерпана целиком (опробованы: ${HANDS_CHAIN_TRIED:-?})${HANDS_CHAIN_RESET_HINT:+, ближайший названный сброс: $HANDS_CHAIN_RESET_HINT} — повтор внутри этого прогона не поможет (docs/runbooks/switch-llm-provider.md, #727)" >&2
      ;;
    *)
      echo "::error::dsh завершился с кодом $rc" >&2
      ;;
  esac
  exit 1
fi
