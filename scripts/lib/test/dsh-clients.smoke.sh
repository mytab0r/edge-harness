#!/usr/bin/env bash
# Гвардия класса «удалил определение при живом вызове» (ревью #128, Б1):
# bash -n тело не исполняет и молчит, когда определение функции удалено, а вызовы
# остались (случай hands: SEQ/seq_persist/add_event/flush_events удалены, вызовы
# живы). Здесь каждый bash-клиент (hands dsh_task.sh, worker task.sh,
# ревьюер ai_dsh.sh) исполняется ЦЕЛИКОМ дочерним bash на заглушках внешнего мира:
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
JOURNAL_CAPT="$TMP/journal-events.ndjson"   # каптурка POST /api/events curl-заглушки
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
    *api.telegram.org*)
      # Telegram-отчёт воркера (#170): каптурируем ВЕСЬ вызов — по журналу
      # ассерты держат и parse_mode=HTML, и экранирование заголовка (tg_html).
      log_call "TG-SEND $*"
      body='{"ok":true,"result":{"message_id":1}}' ;;
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
  elif [[ "$sig" == *"issue list"* ]]; then
    # Пул свободных задач для auto-сценария воркера (free_task).
    payload="${GH_ISSUE_LIST_JSON:-[]}"
  elif [[ "$sig" == *"pr list"* && "$sig" == *"--json url"* ]]; then
    # Переопределяемо сценарием (#422): провайдер в лимите — PR не открыт,
    # worker/task.sh обязан различить это от «PR уже есть». Дефолт — ОТДЕЛЬНОЙ
    # переменной, не буквальными скобками внутри ${VAR:-...}: непарная '}' в
    # литерале JSON преждевременно закрывает подстановку (bash: первая
    # НЕэкранированная '}' завершает ${...}, даже если это середина JSON) —
    # живой прогон CI 34009616520, jq упал на «Unmatched ']'».
    _default_pr_list_url='[{"url":"https://github.test/mytab0r/edge-harness/pull/9"}]'
    payload="${GH_PR_LIST_URL_JSON:-$_default_pr_list_url}"
  elif [[ "$sig" == *"pr list"* ]]; then
    payload='[]'
  elif [[ "$sig" == *"run list"* ]]; then
    # Гвардия дублей воркера: в smoke нет живых прогонов.
    payload='[]'
  elif [[ "$sig" == *"api users"* ]]; then
    payload='{"id":7416604}'
  elif [[ "$sig" == *" comment "* ]]; then
    # Полные аргументы (не просто факт вызова) — тело комментария едет вторым
    # словом --body: сценарии #422 сверяют, что текст различает «провайдер в
    # лимите» от «воркер не справился» (правило AGENTS.md).
    log_call "GH-COMMENT $*"
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
      # Ретрай RATE_LIMIT в ai-review (#419): режим задаёт сценарий через
      # SMOKE_RATE_LIMIT_MODE, попытки считает переменная процесса — эта
      # заглушка живёт в одном bash-процессе ai_dsh.sh на весь ретрай-цикл
      # (несколько вызовов dsh — один процесс, файл состояния не нужен).
      if [ -n "${SMOKE_RATE_LIMIT_MODE:-}" ]; then
        _smoke_rl_attempt=$(( ${_smoke_rl_attempt:-0} + 1 ))
        case "$SMOKE_RATE_LIMIT_MODE" in
          transient-then-ok)
            if [ "$_smoke_rl_attempt" -le "${SMOKE_RATE_LIMIT_TRANSIENT_COUNT:-1}" ]; then
              echo "dsh: RATE_LIMIT: Rate limit reached for requests" >&2
              return 1
            fi
            # worker/hands (#422, в отличие от ai_dsh.sh) требуют спул стрима
            # на успехе — та же запись, что и обычный успешный путь ниже.
            if [ -n "${HANDS_SPOOL:-}" ]; then
              printf '%s\n' \
                '{"v":1,"session_id":"smoke","seq":0,"time":0,"type":"turn/start","data":{"turn":1}}' \
                '{"v":1,"session_id":"smoke","seq":1,"time":0,"type":"user/message","data":{"id":"m1","role":"user","content":[{"type":"text","text":"smoke"}],"source":{"kind":"user"}}}' \
                '{"v":1,"session_id":"smoke","seq":2,"time":0,"type":"turn/end","data":{"turn":1,"reason":{"kind":"completed"}}}' >>"$HANDS_SPOOL"
            fi
            echo "smoke: работа сделана после ретрая"
            return 0 ;;
          always-transient)
            echo "dsh: RATE_LIMIT: Rate limit reached for requests" >&2
            return 1 ;;
          quota-exhausted)
            echo "dsh: RATE_LIMIT: Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-09-10T00:00:00Z" >&2
            return 1 ;;
          real-error)
            echo "dsh: HTTP_404: modelCode does not exist" >&2
            return 1 ;;
          *)
            echo "::error::SMOKE: неизвестный SMOKE_RATE_LIMIT_MODE: $SMOKE_RATE_LIMIT_MODE" >&2
            return 99 ;;
        esac
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

# gh уровня ПРОЦЕССА: claim_task.py (#121) зовёт бинарник `gh` через
# subprocess.run — export -f на дочерний процесс python не действует. Заглушка
# — мини-сервер аренды на файле состояния: POST существующего ref → 422
# (серверная атомарность GitHub), DELETE идемпотентен (404 на отсутствии),
# matching-refs отдаёт живые замки. Мутирующие вызовы пишутся в CALLLOG —
# ассерты сценариев доказывают «замок взят»/«замка не было» по журналу, а не
# по коду возврата.
cat >"$TMP/bin/gh" <<'GHSTUB'
#!/usr/bin/env bash
# Прод-форма gh (#121-ревью): без токена реальный gh неавторизован — заглушка
# обязана падать так же (rc 4 + ::error::), иначе безтокенная ветка канала
# (например «unset GH_RUN_TOKEN до вызова аренды») зелёная в тесте и красная
# в проде.
[ -n "${GH_TOKEN:-}" ] || { echo "gh: SMOKE: нет GH_TOKEN — реальный gh был бы неавторизован" >&2; exit 4; }
sig=" $* "
state="${SMOKE_STATE:?SMOKE_STATE не задан}/locks"
touch "$state"
log() { printf '%s\n' "$*" >>"${CALLLOG:?CALLLOG не задан}"; }
resp() { printf '%s\n' "$1"; exit 0; }
case "$sig" in
  *"matching-refs/locks/"*)
    out="[]"
    if [ -s "$state" ]; then
      items=""
      while IFS= read -r ref; do
        [ -n "$ref" ] || continue
        items="${items:+$items,}{\"ref\":\"$ref\",\"object\":{\"sha\":\"sha-$ref\"}}"
      done <"$state"
      out="[$items]"
    fi
    resp "$out" ;;
  *"commits/main "*|*"commits/"*)
    resp '{"sha":"basesha","commit":{"tree":{"sha":"treesha"}}}' ;;
  *"git/commits "*)
    resp '{"sha":"locksha"}' ;;
  *"git/refs"*)
    ref=""
    for a in "$@"; do case "$a" in ref=*) ref="${a#ref=}" ;; esac; done
    if [[ "$sig" == *"-X POST"* ]]; then
      if grep -qxF -- "$ref" "$state"; then
        echo "gh: HTTP 422: Reference already exists [$ref]" >&2
        exit 1
      fi
      printf '%s\n' "$ref" >>"$state"
      log "GH-API-LOCK-CREATE $ref"
      exit 0
    fi
    if [[ "$sig" == *"-X DELETE"* ]]; then
      # путь repos/o/r/git/refs/locks/task-N → ref-имя refs/locks/task-N
      pathref="refs/${sig##*git/refs/}"
      pathref="${pathref%% }"
      if grep -qxF -- "$pathref" "$state"; then
        printf '%s\n' "$(grep -vxF -- "$pathref" "$state")" >"$state"
        log "GH-API-LOCK-DELETE $pathref"
        exit 0
      fi
      echo "gh: HTTP 404: Not Found [$pathref]" >&2
      exit 1
    fi
    echo "gh: SMOKE: неизвестный метод для git/refs: $sig" >&2
    exit 99 ;;
  *"issues/"*"assignees "*)
    issue="${sig##*issues/}"; issue="${issue%%/*}"
    # release_full (#422) снимает назначение через DELETE — отличаем от
    # claim's POST: одно и то же слово "assignees" покрывает оба глагола.
    if [[ "$sig" == *"-X DELETE"* ]]; then
      log "GH-API-UNASSIGN issue-$issue"
    else
      log "GH-API-ASSIGN issue-$issue"
    fi
    resp '{}' ;;
  *"issues/"*"comments "*)
    issue="${sig##*issues/}"; issue="${issue%%/*}"
    log "GH-API-COMMENT issue-$issue"
    resp '{}' ;;
  *"issues/"*)
    # Проверка на входе claim_task.py::claim (#357): GET состояния задачи
    # перед созданием замка — открыта, метка task, не blocked. Все смоук-
    # сценарии этого файла заводят задачи, которые ОБЯЗАНЫ пройти эту
    # проверку (сама задача занятости решается ниже, замком/веткой), поэтому
    # заглушка отвечает одинаково открытой/размеченной задачей для любого номера.
    # assignees непусты (#422): к моменту, когда release_full() читает этот
    # GET, claim уже назначил исполнителя — иначе снимать было бы нечего.
    resp '{"state":"open","labels":[{"name":"task"}],"assignees":[{"login":"'"${WORKER_LOGIN:-mytab0r}"'"}]}' ;;
  *)
    echo "gh: SMOKE: заглушка не знает вызов: $sig" >&2
    exit 99 ;;
esac
GHSTUB
chmod +x "$TMP/bin/gh"

# ── Окружение клиентов ────────────────────────────────────────────────────────────
export DSH_EDGE_URL="https://morde.test"
export DSH_EDGE_ACCESS_KEY="smoke-access-key-at-least-32-bytes-long!!"
export HANDS_URL="https://journal.test"
export HARNESS_URL="https://journal.test"
export HANDS_TOKEN="smoke-hands-token"
export DEEPSEEK_API_KEY="smoke-deepseek-key"
# ФИКСТУРА теста, не дефолт прода (#153): dsh_require_provider_env требует
# непустых значений, значение здесь произвольно и не читается как источник
# правды — им остаются только vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория.
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

assert_not_log() { # SUBSTR MESSAGE — отрицательный ассерт честен на чистом журнале
  if grep -qF -- "$1" "$CALLLOG"; then
    echo "::error::SMOKE: нежданный вызов «$1» — $2" >&2
    echo "--- журнал вызовов ---" >&2
    cat "$CALLLOG" >&2
    exit 1
  fi
}

# Начало сценария: чистые журналы и состояние замков. Ассерты «не было
# вызова» и «замок ещё не взят» честны только на пустом состоянии.
scenario_start() { # [SEED_REF...] — замки, живые ДО запуска клиента
  : >"$CALLLOG"
  rm -f "$JOURNAL_CAPT"
  : >"$SMOKE_STATE/locks"
  for ref in "$@"; do printf '%s\n' "$ref" >>"$SMOKE_STATE/locks"; done
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

# Симметрично run_client, но для сценариев, где красный job — ОЖИДАЕМЫЙ
# исход (#422: провайдер в лимите/квоте — die/exit 1 по контракту, это не
# поломка клиента, а честный красный прогон): неожиданный rc=0 здесь и есть
# провал smoke, не наоборот.
run_client_expect_fail() { # LABEL SCRIPT
  local label=$1 script=$2 rc=0
  rm -f "$SMOKE_STATE/openssl-n"
  echo "SMOKE: прогон $label (ожидаем красный job)"
  if ( bash "$script" </dev/null ); then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    echo "::error::SMOKE: $label завершился ЗЕЛЁНЫМ (0), а обязан был провалиться (провайдер в лимите — #422)" >&2
    echo "--- журнал вызовов ---" >&2
    cat "$CALLLOG" >&2
    exit 1
  fi
}

# ── Клиент рук ────────────────────────────────────────────────────────────────────
RUNNER_TEMP="$TMP/rt" \
TASK_ID="issue-123" \
TASK_TEXT="Smoke задача: проверить гвардию класса" \
GH_RUN_TOKEN="smoke-run-token" \
  run_client "hands" "$REPO/scripts/hands/dsh_task.sh"

assert_log "MORDE-RPC session.create" "hands: сессия морды не создана"
assert_log "MORDE-RPC session.rename" "hands: сессия морды не названа"
assert_log "MORDE-INGEST" "hands: транскрипт не уехал в морду"
assert_log "JOURNAL-POST /api/events" "hands: журнал не получил жизненный цикл job"
# Аренда взята до работы (#121): замок создан, назначение и след — после него.
assert_log "GH-API-LOCK-CREATE refs/locks/task-123" "hands: аренда issue-123 не взята"
assert_log "GH-API-ASSIGN issue-123" "hands: задача не назначена при claim"
# Состав батчей — по каптурке curl-заглушки: events.jsonl клиент очищает после
# каждого принятого флаша (flush_events), к моменту ассертов он пуст.
grep -qE '"kind": *"job_start"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: в журнале нет job_start" >&2
       echo "--- каптурка журнала ---" >&2; cat "$JOURNAL_CAPT" 2>&1 >&2; exit 1; }
grep -qE '"kind": *"job_end"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: в журнале нет job_end" >&2; exit 1; }
grep -qE '"result": *"ok"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands: job_end не ok" >&2; exit 1; }
echo "SMOKE: hands — ок"

# ── Клиент автономного воркера ────────────────────────────────────────────────────
scenario_start   # чистое состояние аренды: замок из hands-сценария не должен мешать
WORKER_LOGIN="mytab0r" \
WORKER_TASK="123" \
RUNNER_TEMP="$TMP/rtw" \
GH_TOKEN="smoke-pat-token" \
TELEGRAM_BOT_TOKEN="smoke-tg-token" \
TELEGRAM_CHAT_ID="42" \
GH_ISSUE_JSON='{"number":123,"title":"Smoke задача & для гвардии класса","body":"## Цель\nпрогон\n\n## Критерий готовности\nсессия в морде","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
  run_client "worker" "$REPO/scripts/worker/task.sh"

assert_log "MORDE-RPC session.create" "worker: сессия морды не создана"
assert_log "MORDE-RPC session.rename" "worker: сессия морды не названа"
assert_log "MORDE-INGEST" "worker: транскрипт не уехал в морду"
assert_log "GH-COMMENT" "worker: нет отчёта в задачу"
# Захват через аренду (#121): замок создан ДО сессии и работы.
assert_log "GH-API-LOCK-CREATE refs/locks/task-123" "worker: аренда задачи 123 не взята"
# Telegram-отчёт (#170): доставлен С parse_mode (иначе кликабельных ссылок
# не бывает — plain text), и заголовок с «&» ушёл экранированным (иначе
# Telegram отклонил бы всё сообщение целиком). Слово «выполнена» в отчёте
# воркера запрещено: открытый PR ≠ сделанная задача, это слово теперь значит
# только «слито в main» (scheduler.after_merge).
tg_line=$(grep -F "TG-SEND" "$CALLLOG" | head -1)
[ -n "$tg_line" ] || { echo "::error::SMOKE: worker: Telegram-отчёт не отправлен" >&2; exit 1; }
grep -qF -- "parse_mode=HTML" <<<"$tg_line" \
  || { echo "::error::SMOKE: worker: Telegram-отчёт без parse_mode=HTML: $tg_line" >&2; exit 1; }
grep -qF -- "Smoke задача &amp; для гвардии класса" <<<"$tg_line" \
  || { echo "::error::SMOKE: worker: заголовок ушёл в Telegram неэкранированным: $tg_line" >&2; exit 1; }
grep -qF -- "выполнена" <<<"$tg_line" \
  && { echo "::error::SMOKE: worker: «выполнена» в отчёте об открытом PR — класс #170 вернулся: $tg_line" >&2; exit 1; }
echo "SMOKE: worker — ок"

# ── Сценарии аренды (#121): занято/свободно на мини-сервере замков ────────────────
# Отказ claim = зелёный no-op: job завершается 0, работы НЕТ (нет сессии в
# морде, нет отчёта в задачу), журнал получает честный финал.

# hands при живом замке: отказ, зелёный no-op, task_busy в журнале, без морды.
scenario_start "refs/locks/task-123"
TASK_ID="issue-123" \
TASK_TEXT="Smoke задача: отказ аренды" \
RUNNER_TEMP="$TMP/rt-hands-busy" \
GH_RUN_TOKEN="smoke-run-token" \
  run_client "hands-busy" "$REPO/scripts/hands/dsh_task.sh"
assert_not_log "GH-API-LOCK-CREATE" "hands-busy: замок создан поверх чужого — атомарности нет"
assert_not_log "GH-API-ASSIGN" "hands-busy: назначение при отказе аренды"
assert_not_log "MORDE-RPC" "hands-busy: сессия в морде создана при отказе аренды"
grep -q 'task_busy' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands-busy: в журнале нет task_busy — отказ не виден" >&2
       cat "$JOURNAL_CAPT" >&2; exit 1; }
grep -qE '"result": *"fail"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands-busy: job_end не fail — задача повиснет dispatched" >&2
       cat "$JOURNAL_CAPT" >&2; exit 1; }
if grep -qE '"kind": *"job_start"' "$JOURNAL_CAPT"; then
  echo "::error::SMOKE: hands-busy: job_start при отказе аренды — работа начата" >&2
  cat "$JOURNAL_CAPT" >&2
  exit 1
fi
echo "SMOKE: hands-busy — ок"

# worker (manual) при живом замке: отказ, зелёный no-op, без сессии и отчёта.
scenario_start "refs/locks/task-123"
WORKER_LOGIN="mytab0r" \
WORKER_TASK="123" \
RUNNER_TEMP="$TMP/rt-w-busy" \
GH_TOKEN="smoke-pat-token" \
GH_ISSUE_JSON='{"number":123,"title":"Smoke задача занята","body":"## Цель\nгонка","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
  run_client "worker-busy" "$REPO/scripts/worker/task.sh"
assert_not_log "GH-API-LOCK-CREATE" "worker-busy: замок создан поверх чужого — атомарности нет"
assert_not_log "GH-API-ASSIGN" "worker-busy: назначение при отказе аренды"
assert_not_log "MORDE-RPC" "worker-busy: сессия в морде создана при отказе аренды"
assert_not_log "GH-COMMENT" "worker-busy: отчёт «не справился» при штатном отказе аренды"
echo "SMOKE: worker-busy — ок"

# worker (auto) при частично занятом пуле: замок 200 пропускается, берётся 201.
scenario_start "refs/locks/task-200"
WORKER_LOGIN="mytab0r" \
WORKER_TASK="" \
RUNNER_TEMP="$TMP/rt-w-auto" \
GH_TOKEN="smoke-pat-token" \
GH_ISSUE_LIST_JSON='[{"number":200,"assignees":[],"title":"Занята арендой"},{"number":201,"assignees":[],"title":"Свободна для воркера"}]' \
GH_ISSUE_JSON='{"number":201,"title":"Свободна для воркера","body":"## Цель\nauto\n\n## Критерий готовности\nпул","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
  run_client "worker-auto" "$REPO/scripts/worker/task.sh"
assert_log "GH-API-LOCK-CREATE refs/locks/task-201" "worker-auto: свободная 201 не взята в аренду"
assert_not_log "refs/locks/task-200" "worker-auto: задача под живым замком попала в работу"
assert_log "MORDE-RPC session.create" "worker-auto: сессия морды не создана"
assert_log "GH-COMMENT" "worker-auto: нет отчёта в задачу"
echo "SMOKE: worker-auto — ок"

# ── Клиент AI-ревью (второй гейт #18) ────────────────────────────────────────
# Транспорт ревьюера: без GitHub-токена по построению, поэтому smoke проверяет
# только проводку dsh_install → dsh_patch_profile → dsh: ответ заглушки обязан
# оказаться в answer.txt — это единственный выход недоверенного шага.
AI_SMOKE="$TMP/ai"
mkdir -p "$AI_SMOKE"
printf 'Промпт ревью (smoke): проверь проводку транспорта\n' >"$AI_SMOKE/prompt.md"
# Значение DEEPSEEK_API_KEY-заглушки короче 20 символов и не похоже на литерал
# секрета: детерминированный гейт (check_pr) сканирует добавленные строки
# паттерном KEY="<20+ символов>" — тестовые данные не должны в него попадать.
# Комментарий — ДО env-префикса: строка с «\» в продолжении делает следующий
# физический строк частью команды, и коммент посреди продолжения молча
# отрезает переменные от вызова (поймано красным CI на e31c7d0).
AI_WORK="$AI_SMOKE" \
DEEPSEEK_API_KEY="smoke-key" \
HANDS_SPOOL="$AI_SMOKE/spool.ndjson" \
  run_client "ai-review" "$REPO/scripts/review/ai_dsh.sh"
grep -q "smoke: работа сделана" "$AI_SMOKE/answer.txt" \
  || { echo "::error::SMOKE: ai-review: ответ DSH не записан в answer.txt" >&2; exit 1; }
# Гвардия класса «ошибка провайдера читается как нарушение контракта моделью»
# (прогон 33572445063, PR #190): dsh_rc.txt обязан появиться на успешном пути
# с кодом 0 — verdict (ai_review.py) отличает его от rc≠0 транспортной ошибки.
[ -f "$AI_SMOKE/dsh_rc.txt" ] \
  || { echo "::error::SMOKE: ai-review: dsh_rc.txt не записан — verdict не сможет отличить ошибку провайдера от плохого формата ответа" >&2; exit 1; }
grep -qx "0" "$AI_SMOKE/dsh_rc.txt" \
  || { echo "::error::SMOKE: ai-review: dsh_rc.txt ожидал '0' на успешном прогоне, получено: $(cat "$AI_SMOKE/dsh_rc.txt")" >&2; exit 1; }
echo "SMOKE: ai-review — ок"

# ── Ретрай RATE_LIMIT в ai-review (#419) ──────────────────────────────────────
# Живой факт: worker.yml 34007508064 упал с «dsh: RATE_LIMIT: Rate limit
# reached for requests» — квоту съело параллельное ai-review. Здесь —
# полный прогон ai_dsh.sh (не bash -n) на четырёх сценариях: временный
# лимит снимается ретраем, недельный/месячный лимит и настоящая ошибка не
# ждут вовсе, бюджет ожидания короткого лимита конечен. sleep стаблен
# ТОЛЬКО здесь (после уже отработавших worker/hands выше) — эти клиенты
# реального сна не ждут, а фоновый heartbeat-луп воркера/рук их не
# использует постфактум.
sleep() { :; }
export -f sleep

# 1) Временный RATE_LIMIT дважды, затем успех — ретрай обязан выжить.
AI_RL1="$TMP/ai-rl-transient"
mkdir -p "$AI_RL1"
printf 'Промпт ревью (smoke): временный RATE_LIMIT\n' >"$AI_RL1/prompt.md"
AI_WORK="$AI_RL1" \
DEEPSEEK_API_KEY="smoke-key" \
SMOKE_RATE_LIMIT_MODE="transient-then-ok" \
SMOKE_RATE_LIMIT_TRANSIENT_COUNT="2" \
AI_REVIEW_RATE_LIMIT_INITIAL_DELAY_SECS="1" \
AI_REVIEW_RATE_LIMIT_MAX_DELAY_SECS="1" \
  run_client "ai-review-rate-limit-transient" "$REPO/scripts/review/ai_dsh.sh"
grep -q "работа сделана после ретрая" "$AI_RL1/answer.txt" \
  || { echo "::error::SMOKE: ai-review-transient: ретрай не довёл до успешного ответа" >&2; cat "$AI_RL1/answer.txt" >&2; exit 1; }
grep -qx "0" "$AI_RL1/dsh_rc.txt" \
  || { echo "::error::SMOKE: ai-review-transient: dsh_rc.txt ожидал '0' после успешного ретрая, получено: $(cat "$AI_RL1/dsh_rc.txt" 2>/dev/null)" >&2; exit 1; }
[ -s "$AI_RL1/failure_reason.txt" ] \
  && { echo "::error::SMOKE: ai-review-transient: failure_reason.txt обязан быть пуст после успеха, получено: $(cat "$AI_RL1/failure_reason.txt")" >&2; exit 1; }
echo "SMOKE: ai-review-rate-limit-transient — ок"

# 2) RATE_LIMIT: Weekly/Monthly Limit Exhausted — падаем СРАЗУ, без ретрая
# (сброс через дни — ждать внутри прогона бессмысленно).
AI_RL2="$TMP/ai-rl-quota"
mkdir -p "$AI_RL2"
printf 'Промпт ревью (smoke): квота исчерпана надолго\n' >"$AI_RL2/prompt.md"
AI_WORK="$AI_RL2" \
DEEPSEEK_API_KEY="smoke-key" \
SMOKE_RATE_LIMIT_MODE="quota-exhausted" \
  run_client "ai-review-rate-limit-quota" "$REPO/scripts/review/ai_dsh.sh"
grep -qx "1" "$AI_RL2/dsh_rc.txt" \
  || { echo "::error::SMOKE: ai-review-quota: dsh_rc.txt ожидал '1', получено: $(cat "$AI_RL2/dsh_rc.txt" 2>/dev/null)" >&2; exit 1; }
grep -qx "quota_exhausted" "$AI_RL2/failure_reason.txt" \
  || { echo "::error::SMOKE: ai-review-quota: failure_reason.txt ожидал 'quota_exhausted', получено: $(cat "$AI_RL2/failure_reason.txt" 2>/dev/null)" >&2; exit 1; }
echo "SMOKE: ai-review-rate-limit-quota — ок"

# 3) Настоящая ошибка провайдера (нет строки RATE_LIMIT вовсе) — падаем
# сразу, как и до #419: ретрай не должен трогать этот класс.
AI_RL3="$TMP/ai-rl-real-error"
mkdir -p "$AI_RL3"
printf 'Промпт ревью (smoke): настоящая ошибка\n' >"$AI_RL3/prompt.md"
AI_WORK="$AI_RL3" \
DEEPSEEK_API_KEY="smoke-key" \
SMOKE_RATE_LIMIT_MODE="real-error" \
  run_client "ai-review-rate-limit-real-error" "$REPO/scripts/review/ai_dsh.sh"
grep -qx "1" "$AI_RL3/dsh_rc.txt" \
  || { echo "::error::SMOKE: ai-review-real-error: dsh_rc.txt ожидал '1', получено: $(cat "$AI_RL3/dsh_rc.txt" 2>/dev/null)" >&2; exit 1; }
[ -s "$AI_RL3/failure_reason.txt" ] \
  && { echo "::error::SMOKE: ai-review-real-error: failure_reason.txt обязан быть пуст на настоящей ошибке (не лимит), получено: $(cat "$AI_RL3/failure_reason.txt")" >&2; exit 1; }
echo "SMOKE: ai-review-rate-limit-real-error — ок"

# 4) Временный RATE_LIMIT, который не снимается, — бюджет ожидания обязан
# кончиться (не бесконечный ретрай, не занятый навечно job-слот).
AI_RL4="$TMP/ai-rl-budget"
mkdir -p "$AI_RL4"
printf 'Промпт ревью (smoke): бюджет ретрая исчерпан\n' >"$AI_RL4/prompt.md"
AI_WORK="$AI_RL4" \
DEEPSEEK_API_KEY="smoke-key" \
SMOKE_RATE_LIMIT_MODE="always-transient" \
AI_REVIEW_RATE_LIMIT_MAX_WAIT_SECS="0" \
  run_client "ai-review-rate-limit-budget" "$REPO/scripts/review/ai_dsh.sh"
grep -qx "1" "$AI_RL4/dsh_rc.txt" \
  || { echo "::error::SMOKE: ai-review-budget: dsh_rc.txt ожидал '1', получено: $(cat "$AI_RL4/dsh_rc.txt" 2>/dev/null)" >&2; exit 1; }
grep -qx "rate_limit_retry_budget_exceeded" "$AI_RL4/failure_reason.txt" \
  || { echo "::error::SMOKE: ai-review-budget: failure_reason.txt ожидал 'rate_limit_retry_budget_exceeded', получено: $(cat "$AI_RL4/failure_reason.txt" 2>/dev/null)" >&2; exit 1; }
echo "SMOKE: ai-review-rate-limit-budget — ок"

# ── Тот же ретрай у worker/hands (#422 — раньше был только у ai-review) ──────────
# Общий механизм (dsh_run_with_retry, lib/dsh-ci.sh) теперь общий для трёх
# каналов; здесь — доказательство, что worker/hands реально его вызывают (а
# не третья копия цикла) и что провайдер в лимите/квоте возвращает задачу в
# пул (release-full: и замок, и назначение), а не оставляет её висеть.

# 1) worker: первая попытка RATE_LIMIT, вторая успешна — мутация класса:
# без ретрая (старое поведение) этот сценарий падал бы «без открытого PR».
scenario_start
WORKER_LOGIN="mytab0r" \
WORKER_TASK="123" \
RUNNER_TEMP="$TMP/rt-w-rl1" \
GH_TOKEN="smoke-pat-token" \
TELEGRAM_BOT_TOKEN="smoke-tg-token" \
TELEGRAM_CHAT_ID="42" \
GH_ISSUE_JSON='{"number":123,"title":"Smoke RATE_LIMIT транзит","body":"## Цель\nпрогон\n\n## Критерий готовности\nсессия","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
SMOKE_RATE_LIMIT_MODE="transient-then-ok" \
SMOKE_RATE_LIMIT_TRANSIENT_COUNT="1" \
WORKER_RATE_LIMIT_INITIAL_DELAY_SECS="1" \
WORKER_RATE_LIMIT_MAX_DELAY_SECS="1" \
  run_client "worker-rate-limit-transient" "$REPO/scripts/worker/task.sh"
assert_log "GH-COMMENT" "worker-rate-limit-transient: нет отчёта в задачу после успешного ретрая"
assert_not_log "провайдер в лимите" "worker-rate-limit-transient: успешный ретрай не должен звучать как провал провайдера"
echo "SMOKE: worker-rate-limit-transient — ок"

# 2) worker: RATE_LIMIT Weekly/Monthly (квота надолго) — падает СРАЗУ, задача
# возвращается в пул (снят и замок, и назначение), сообщение различает
# «провайдер в лимите» от «воркер не справился» (правило AGENTS.md).
scenario_start
WORKER_LOGIN="mytab0r" \
WORKER_TASK="123" \
RUNNER_TEMP="$TMP/rt-w-rl2" \
GH_TOKEN="smoke-pat-token" \
TELEGRAM_BOT_TOKEN="smoke-tg-token" \
TELEGRAM_CHAT_ID="42" \
GH_ISSUE_JSON='{"number":123,"title":"Smoke квота исчерпана","body":"## Цель\nпрогон\n\n## Критерий готовности\nсессия","state":"OPEN","assignees":[],"labels":[{"name":"task"}]}' \
GH_PR_LIST_URL_JSON='[]' \
SMOKE_RATE_LIMIT_MODE="quota-exhausted" \
  run_client_expect_fail "worker-rate-limit-quota" "$REPO/scripts/worker/task.sh"
assert_log "GH-API-LOCK-DELETE refs/locks/task-123" "worker-rate-limit-quota: замок не снят при квоте — задача осталась занятой"
assert_log "GH-API-UNASSIGN issue-123" "worker-rate-limit-quota: назначение не снято при квоте — задача осталась занятой"
assert_log "провайдер в лимите" "worker-rate-limit-quota: сообщение не различает провайдера от собственной ошибки"
assert_not_log "Автономный воркер не справился" "worker-rate-limit-quota: сообщение спутало лимит провайдера с ошибкой агента"
tg_line=$(grep -F "TG-SEND" "$CALLLOG" | tail -1)
grep -qF -- "провайдер в лимите" <<<"$tg_line" \
  || { echo "::error::SMOKE: worker-rate-limit-quota: Telegram не различает провайдера от ошибки агента: $tg_line" >&2; exit 1; }
echo "SMOKE: worker-rate-limit-quota — ок"

# 3) hands: бюджет короткого RATE_LIMIT кончился раньше успеха — задача
# (issue-N) возвращается в пул ПОСЛЕ того, как GH_RUN_TOKEN уже снят из
# экспорта (trust-zone, #121): release-full обязан пройти на СОХРАНЁННОЙ
# копии токена (LEASE_RELEASE_TOKEN), не на переменной окружения.
scenario_start
TASK_ID="issue-123" \
TASK_TEXT="Smoke: бюджет ретрая рук исчерпан" \
RUNNER_TEMP="$TMP/rt-h-rl" \
GH_RUN_TOKEN="smoke-run-token" \
SMOKE_RATE_LIMIT_MODE="always-transient" \
HANDS_RATE_LIMIT_MAX_WAIT_SECS="0" \
  run_client_expect_fail "hands-rate-limit-budget" "$REPO/scripts/hands/dsh_task.sh"
assert_log "GH-API-LOCK-DELETE refs/locks/task-123" "hands-rate-limit-budget: замок не снят — задача осталась занятой"
assert_log "GH-API-UNASSIGN issue-123" "hands-rate-limit-budget: назначение не снято — задача осталась занятой"
grep -qF "rate_limit_retry_budget_exceeded" "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands-rate-limit-budget: журнал не получил failure_reason" >&2; cat "$JOURNAL_CAPT" >&2; exit 1; }
grep -qE '"result": *"fail"' "$JOURNAL_CAPT" \
  || { echo "::error::SMOKE: hands-rate-limit-budget: job_end не fail" >&2; exit 1; }
echo "SMOKE: hands-rate-limit-budget — ок"

echo "SMOKE: все клиенты целы — гвардия класса зелёная"
