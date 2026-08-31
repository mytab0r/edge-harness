#!/usr/bin/env bash
# Гвардия класса «удалил определение при живом вызове» (ревью #128, Б1):
# bash -n тело не исполняет и молчит, когда определение функции удалено, а вызовы
# остались (случай hands: SEQ/seq_persist/add_event/flush_events удалены, вызовы
# живы). Здесь каждый bash-клиент (hands dsh_task.sh, worker task.sh) исполняется
# ЦЕЛИКОМ дочерним bash на заглушках внешнего мира:
#   - функции-заглушки curl/gh/dsh/pnpm/timeout экспортируются (export -f) и
#     затеняют бинарники из PATH; dsh-ci.sh, который клиент пересорсирует и
#     который перезатирает dsh_install/dsh_patch_profile, при этом не обманешь —
#     его сетевые зависимости (npm pack из реестра, openssl-сверка целостности)
#     застаблены ПУТЁМ: npm, openssl, git — исполняемые заглушки в PATH;
#   - dsh_install честно отрабатывает на заглушках (пустые tgz + константы
#     integrity из dsh-ci.sh), сетевых вызовов нет.
# Заглушки пишут журнал вызовов; после прогона — ассерты: код 0, журнал получил
# job_start/job_end ok, морда — session.create/rename и ingest, воркер доложил
# в задачу. Сломанное определение = падение клиента или пустой журнал = красный
# smoke.
#
# Запуск: bash scripts/lib/test/dsh-clients.smoke.sh  (jq обязателен)
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
CALLLOG="$TMP/calls.log"
: >"$CALLLOG"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# Константы dsh-ci.sh нужны openssl-заглушке как эталон сверок целостности.
source "$REPO/scripts/lib/dsh-ci.sh"
export DSH_INTEGRITY DSH_HEADLESS_INTEGRITY
SMOKE_STATE="$TMP/state"
mkdir -p "$SMOKE_STATE"
export SMOKE_STATE

log_call() { printf '%s\n' "$*" >>"$CALLLOG"; }

# ── Функции-заглушки (export -f: видны дочернему bash клиента) ───────────────────

curl() { # заглушка-диспетчер по URL; поддерживает -o FILE и -w %{http_code}
  local url="" data_file="" data_str="" outfile="" want_code=0 prev="" arg have_post=0
  for arg in "$@"; do
    case "$arg" in
      http*) url=$arg ;;
      -X) have_post=1 ;;
      @*) [ -n "$data_file" ] || data_file="${arg#@}" ;;
      -o) outfile="PENDING" ;;
      -w) want_code=1 ;;
    esac
    case "$prev" in
      -o) outfile=$arg ;;
      -d|--data|--data-binary)
        case "$arg" in
          @*) data_file="${arg#@}" ;;
          *) data_str=$arg ;;
        esac ;;
    esac
    prev=$arg
  done
  local body="" code=200
  case "$url" in
    */api/auth/login)
      code=303
      body="" ;;
    */api/auth/session)
      body='{"authenticated":true}' ;;
    */ingest)
      local count
      count=$(command jq -s 'length' <"$data_file" 2>/dev/null || echo 0)
      log_call "MORDE-INGEST $url events=$count"
      body="{\"appended\":$count,\"lastSeq\":$count}" ;;
    *journal.test/api/events*)
      if [ "$have_post" -eq 1 ]; then
        log_call "JOURNAL-POST /api/events"
        # Тело батча каптурируется: events.jsonl клиент очищает после флаша,
        # состав job_start/job_end проверяется по каптурке, не по файлу клиента.
        printf '%s\n' "${data_str:-}" >>"${CALLLOG%/*}/journal-events.ndjson"
        body='{"ok":true}'
      else
        body='{"events":[],"has_more":false,"next_after":0}'
      fi ;;
    */api/heartbeat*)
      body='{"ok":true}' ;;
    *morde.test/api/*)
      local m="${url##*morde.test/api/}"
      log_call "MORDE-RPC $m"
      case "$m" in
        workspace.create)
          body='{"type":"server-response","rpcId":"s","result":{"ok":true,"value":{"workspace":{"workspaceId":"ws-smoke"},"created":true}}}' ;;
        session.create)
          body='{"type":"server-response","rpcId":"s","result":{"ok":true,"value":{"sessionId":"smoke","agentPreset":"dsh-edge"}}}' ;;
        session.rename)
          body='{"type":"server-response","rpcId":"s","result":{"ok":true,"value":{"title":"smoke","seq":1}}}' ;;
        workspace.archiveSession)
          body='{"type":"server-response","rpcId":"s","result":{"ok":true,"value":{"archivedSessionIds":[]}}}' ;;
        *)
          body="{\"type\":\"server-response\",\"rpcId\":\"s\",\"result\":{\"ok\":false,\"error\":{\"code\":\"smoke-no-stub\",\"message\":\"нет заглушки для $m\"}}}"
          code=400 ;;
      esac ;;
    *)
      echo "::error::SMOKE: curl-заглушка не знает URL: ${url:-<пусто>}" >&2
      return 99 ;;
  esac
  if [ -n "$outfile" ] && [ "$outfile" != "/dev/null" ]; then printf '%s' "$body" >"$outfile"; fi
  if [ "$want_code" -eq 1 ]; then printf '%s' "$code"; else printf '%s\n' "$body"; fi
  [ "$code" -lt 400 ] && return 0
  return 0   # заглушка всегда «доставляет» ответ: код разбирает вызывающий
}

gh() { # canned-ответ на сигнатуру вызова; --jq применяется настоящим jq
  # Сигнатура — ОДНОЙ строкой: $* склеивает аргументы пробелами, многострочный
  # printf здесь ломал диспетчер (подстрока «issue view» не видна через \n).
  local sig=" $* "
  local payload=""
  if [[ "$sig" == *"--json assignees"* ]]; then
    payload="{\"assignees\":[{\"login\":\"${WORKER_LOGIN:-mytab0r}\"}]}"
  elif [[ "$sig" == *"issue view"* ]]; then
    payload="$GH_ISSUE_JSON"
    [ -n "$payload" ] || payload='{"number":0}'
  elif [[ "$sig" == *"pr list"* && "$sig" == *"--json url"* ]]; then
    payload='[{"url":"https://github.test/mytab0r/edge-harness/pull/9"}]'
  elif [[ "$sig" == *"pr list"* ]]; then
    payload='[]'
  elif [[ "$sig" == *"run list"* ]]; then
    # Гвардия дублей воркера: в smoke нет живых прогонов.
    payload='[]'
  elif [[ "$sig" == *"api users"* ]]; then
    payload='{"id":7416604}'
  elif [[ "$sig" == *" comment "* ]]; then
    log_call "GH-COMMENT"
    return 0
  elif [[ "$sig" == *"issues/"* ]]; then
    log_call "GH-ISSUE-WRITE"
    return 0
  else
    payload='{}'
  fi
  local prev="" filter="" a
  for a in "$@"; do
    { [ "$prev" = "--jq" ] || [ "$prev" = "-q" ]; } && filter=$a
    prev=$a
  done
  if [ -n "$filter" ]; then
    command jq -r "$filter" <<<"$payload"
  else
    printf '%s\n' "$payload"
  fi
}

dsh() { # прогон пишет спул+ответ; dump-config доказывает монтаж плагина
  case "${1:-}" in
    --version)
      echo "dsh 0.0.0-smoke"
      return 0 ;;
    plugin)
      return 0 ;;
    --profile)
      if [ "${3:-}" = "--dump-config" ]; then
        printf -- '- id: hands-streamer\n'
        return 0
      fi
      [ -n "${HANDS_SPOOL:-}" ] || { echo "SMOKE: HANDS_SPOOL не задан" >&2; return 1; }
      printf '%s\n' \
        '{"v":1,"session_id":"smoke","seq":0,"time":0,"type":"turn/start","data":{"turn":1}}' \
        '{"v":1,"session_id":"smoke","seq":1,"time":0,"type":"user/message","data":{"id":"m1","role":"user","content":[{"type":"text","text":"smoke"}],"source":{"kind":"user"}}}' \
        '{"v":1,"session_id":"smoke","seq":2,"time":0,"type":"turn/end","data":{"turn":1,"reason":{"kind":"completed"}}}' >>"$HANDS_SPOOL"
      echo "smoke: работа сделана"
      return 0 ;;
    *)
      echo "::error::SMOKE: dsh-заглушка не знает вызов: $*" >&2
      return 99 ;;
  esac
}

pnpm() { return 0; }
timeout() { local secs=$1; shift; "$@"; }

# ── Заглушки-исполняемые файлы (PATH): их не перезатирает source dsh-ci.sh ───────

mkdir -p "$TMP/bin"

cat >"$TMP/bin/git" <<'GITSTUB'
#!/usr/bin/env bash
case "${1:-}" in
  ls-remote) printf '0000000000000000000000000000000000000000\trefs/heads/main\n' ;;
  rev-parse) printf '0000000000000000000000000000000000000000\n' ;;
  *) exit 0 ;;
esac
GITSTUB

# npm pack: tarball'ы из реестра — пустые файлы нужных имён (целостность сверит
# openssl-заглушка), локальный каталог — пустой tgz под glob клиента. Опции
# (-g и пр.) глотаются: install -g у dsh_install не должен падать на basename.
cat >"$TMP/bin/npm" <<'NPMSTUB'
#!/usr/bin/env bash
[ "${1:-}" = "pack" ] && shift
dest="."
specs=()
prev=""
for a in "$@"; do
  if [ "$prev" = "--pack-destination" ]; then dest=$a
  elif [ "$a" != "--pack-destination" ]; then specs+=("$a")
  fi
  prev=$a
done
for s in "${specs[@]}"; do
  case "$s" in
    -*) : ;;
    @deepseek-ai/dsh@*) : >"deepseek-ai-dsh-${s#@deepseek-ai/dsh@}.tgz" ;;
    @deepseek-ai/dsh-headless@*) : >"deepseek-ai-dsh-headless-${s#@deepseek-ai/dsh-headless@}.tgz" ;;
    *) : >"$dest/$(basename -- "$s")-0.0.0-smoke.tgz" ;;
  esac
done
exit 0
NPMSTUB

# openssl: dgst молчит (вход — пустой tgz), base64 по счётчику вызовов печатает
# ожидаемую константу dsh-ci.sh: первая сверка — dsh, вторая — dsh-headless.
cat >"$TMP/bin/openssl" <<'OPENSSLSTUB'
#!/usr/bin/env bash
if [ "${1:-}" = "dgst" ]; then exit 0; fi
n=$(cat "$SMOKE_STATE/openssl-n" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s\n' "$n" >"$SMOKE_STATE/openssl-n"
if [ "$n" = "1" ]; then printf '%s' "${DSH_INTEGRITY#sha512-}"
elif [ "$n" = "2" ]; then printf '%s' "${DSH_HEADLESS_INTEGRITY#sha512-}"
fi
exit 0
OPENSSLSTUB

chmod +x "$TMP/bin/git" "$TMP/bin/npm" "$TMP/bin/openssl"

# ── Окружение клиентов ────────────────────────────────────────────────────────────
export DSH_EDGE_URL="https://morde.test"
export DSH_EDGE_ACCESS_KEY="smoke-access-key-at-least-32-bytes-long!!"
export HANDS_URL="https://journal.test"
export HARNESS_URL="https://journal.test"
export HANDS_TOKEN="smoke-hands-token"
export DEEPSEEK_API_KEY="smoke-deepseek-key"
export DEEPSEEK_BASE_URL="https://llm.test"
export DEEPSEEK_MODEL="glm-5"
export DRAIN_INTERVAL_SECS="1"
export HEARTBEAT_SECS="3600"
export GITHUB_REPOSITORY="mytab0r/edge-harness"
export PATH="$TMP/bin:$PATH"
export -f curl gh dsh pnpm timeout log_call
export CALLLOG

assert_log() { # SUBSTR MESSAGE
  if ! grep -qF -- "$1" "$CALLLOG"; then
    echo "::error::SMOKE: не дождались «$1» в журнале вызовов — $2" >&2
    echo "--- журнал вызовов ---" >&2
    cat "$CALLLOG" >&2
    exit 1
  fi
}

run_client() { # LABEL SCRIPT — прогон в дочернем bash; exit клиента не убивает smoke
  local label=$1 script=$2 rc=0
  # Счётчик openssl-заглушки — на клиента: у каждого своя пара сверок целостности
  # (первая base64-подмена — dsh, вторая — dsh-headless).
  rm -f "$SMOKE_STATE/openssl-n"
  echo "SMOKE: прогон $label"
  if ( bash "$script" </dev/null ); then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    echo "::error::SMOKE: $label завершился с кодом $rc" >&2
    echo "--- журнал вызовов ---" >&2
    cat "$CALLLOG" >&2
    exit 1
  fi
}

# ── Клиент рук ────────────────────────────────────────────────────────────────────
RUNNER_TEMP="$TMP/rt" \
TASK_ID="issue-123" \
TASK_TEXT="Smoke задача: проверить гвардию класса" \
  run_client "hands" "$REPO/scripts/hands/dsh_task.sh"

assert_log "MORDE-RPC session.create" "hands: сессия морды не создана"
assert_log "MORDE-RPC session.rename" "hands: сессия морды не названа"
assert_log "MORDE-INGEST" "hands: транскрипт не уехал в морду"
assert_log "JOURNAL-POST /api/events" "hands: журнал не получил жизненный цикл job"
# Состав батчей — по каптурке curl-заглушки: events.jsonl клиент очищает после
# каждого принятого флаша (flush_events), к моменту ассертов он пуст.
JOURNAL_CAPT="$TMP/journal-events.ndjson"
grep -qE '"kind": *"job_start"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: в журнале нет job_start" >&2
       echo "--- каптурка журнала ---" >&2; cat "$JOURNAL_CAPT" 2>&1 >&2; exit 1; }
grep -qE '"kind": *"job_end"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: в журнале нет job_end" >&2; exit 1; }
grep -qE '"result": *"ok"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: job_end не ok" >&2; exit 1; }
echo "SMOKE: hands — ок"

# ── Клиент автономного воркера ────────────────────────────────────────────────────
WORKER_LOGIN="mytab0r" \
WORKER_TASK="123" \
RUNNER_TEMP="$TMP/rtw" \
GH_ISSUE_JSON='{"number":123,"title":"Smoke задача для гвардии класса","body":"## Цель\nпрогон\n\n## Критерий готовности\nсессия в морде","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
  run_client "worker" "$REPO/scripts/worker/task.sh"

assert_log "MORDE-RPC session.create" "worker: сессия морды не создана"
assert_log "MORDE-RPC session.rename" "worker: сессия морды не названа"
assert_log "MORDE-INGEST" "worker: транскрипт не уехал в морду"
assert_log "GH-COMMENT" "worker: нет отчёта в задачу"
echo "SMOKE: worker — ок"

echo "SMOKE: оба клиента целы — гвардия класса зелёная"
