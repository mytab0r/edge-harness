#!/usr/bin/env bash
# Шов «сессия раннера в морде dsh-edge» (#119): раннер гоняет своего DSH вне
# деплоя, а ход работы виден владельцу в НАТИВНОМ DSH UI. Механизм один: спул
# плагина dsh-hands-streamer (канонические события сессии) дренируется в
# сессию-просмотрщик морды через POST /api/sessions/:id/ingest (патч 0004,
# dsh-edge/PATCHES.md). Журнал edge-harness транскрипт больше не получает —
# только события жизненного цикла job (замещает стрим #112).
#
# Контракт морды (выверен живым продом 2026-08-31, docs/research/12-…md):
#   POST /api/auth/login  form accessKey=… → 303 + кука __Host-dsh_edge_owner
#   RPC  POST /api/<method>  {"type":"client-request","rpcId","method","payload"}
#        ответ {"type":"server-response","result":{"ok":true,value}|{ok:false,error}}
#   session.create {workspaceId|cwd, sessionId?} · session.rename {sessionId,title}
#   workspace.create {path} (идемпотентен) · workspace.archiveSession {sessionId}
#
# Зависимости: источник dsh-ci.sh ДО этого файла (нужен redact), jq, curl.
# Обязательное окружение: DSH_EDGE_URL, DSH_EDGE_ACCESS_KEY, WORK.
# Имена secret/var репозитория — те же: secrets.DSH_EDGE_ACCESS_KEY, vars.DSH_EDGE_URL.

DSH_EDGE_WORKSPACE_PATH="/workspace/edge-harness"
DSH_EDGE_CURL="curl -fsS --connect-timeout 5 --max-time 30"
# Потолки батча дрена: тело морды 1 MiB (MAX_HARNESS_INGEST_BODY_BYTES,
# патч 0004) с запасом на конверт; ≤256 событий за батч (лимит маршрута).
DSH_EDGE_BATCH=50
DSH_EDGE_BATCH_BYTES=700000

dsh_edge_init() { # пути состояния; WORK задаёт вызывающий раннер до любого вызова
  if [ -z "${WORK:-}" ]; then
    echo "::error::WORK не задан — lib dsh-edge-session не знает, где держать куку и курсор" >&2
    return 1
  fi
  DSH_EDGE_CJAR="$WORK/dsh-edge.cookies.txt"      # кука владельца морды
  DSH_EDGE_DRAIN_CURSOR="$WORK/dsh-edge.drained"  # строк спула ПРИНЯТО мордой
}

dsh_edge_enabled() { # конфигурация задана? (проверка без фатала)
  [ -n "${DSH_EDGE_URL:-}" ] && [ -n "${DSH_EDGE_ACCESS_KEY:-}" ]
}

dsh_edge_require_config() { # раннер без шва работать не может — отказ громкий
  dsh_edge_init || return 1
  if ! dsh_edge_enabled; then
    echo "::error::DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY не заданы — сессия не появится в морде (#119). Это настройка репозитория (vars.DSH_EDGE_URL / secrets.DSH_EDGE_ACCESS_KEY), а не сбой сети." >&2
    return 1
  fi
}

dsh_edge_login() { # обмен access-ключа на куку владельца
  dsh_edge_require_config || return 1
  local attempt code
  for attempt in 1 2 3 4; do
    # Сетевой сбой может склеить «код + 000» в одном значении — распознаём
    # вхождение 303, а не равенство: строгое равенство ловит только чистый прогон.
    code=$($DSH_EDGE_CURL -o /dev/null -w '%{http_code}' -c "$DSH_EDGE_CJAR" \
      -X POST "$DSH_EDGE_URL/api/auth/login" \
      --data-urlencode "accessKey=$DSH_EDGE_ACCESS_KEY" 2>/dev/null || echo 000)
    case "$code" in *303*) break ;; esac
    echo "::warning::логин в морду не удался (HTTP $code), попытка $attempt/4" >&2
    sleep $((attempt * 2))
  done
  case "$code" in *303*) : ;; *)
    echo "::error::Морда не выдала куку владельца (последний HTTP $code) — транскрипт не попадёт в морду" >&2
    return 1 ;;
  esac
  local attempt
  for attempt in 1 2 3; do
    if $DSH_EDGE_CURL -b "$DSH_EDGE_CJAR" "$DSH_EDGE_URL/api/auth/session" 2>/dev/null \
        | grep -q '"authenticated":true'; then
      return 0
    fi
    sleep $((attempt * 2))
  done
  echo "::error::Кука владельца не принята мордой (/api/auth/session)" >&2
  return 1
}

dsh_edge_rpc() { # METHOD PAYLOAD_JSON → stdout: .result.value; fail loud по result.ok
  local method=$1 payload=$2
  local attempt body response
  body=$(jq -n --arg method "$method" --arg rpcId "harness-$$-$RANDOM" \
    --argjson payload "$payload" \
    '{type:"client-request", rpcId:$rpcId, method:$method, payload:$payload}') \
    || { echo "::error::Не собрали RPC-конверт для $method (payload не JSON?)" >&2; return 1; }
  for attempt in 1 2 3; do
    if response=$($DSH_EDGE_CURL -b "$DSH_EDGE_CJAR" -H 'content-type: application/json' \
        -X POST "$DSH_EDGE_URL/api/$method" --data-binary "$body" 2>/dev/null); then
      if jq -e '.result.ok == true' <<<"$response" >/dev/null; then
        jq '.result.value' <<<"$response"
        return 0
      fi
      # Ошибка контракта (не сеть): ретрай бессмыслен — падаем сразу с причиной.
      jq -r '"::error::Морда отклонила " + .result.error.code + ": " + .result.error.message' <<<"$response" >&2
      return 1
    fi
    sleep $((attempt * 2))
  done
  echo "::error::Морда недоступна для $method из 3 попыток (сеть/деплой)" >&2
  return 1
}

dsh_edge_harness_workspace() { # stdout: workspaceId воркспейса edge-harness (идемпотентно)
  local payload
  payload=$(jq -n --arg path "$DSH_EDGE_WORKSPACE_PATH" '{path:$path}') \
    || { echo "::error::Не собрали payload workspace.create (jq)" >&2; return 1; }
  dsh_edge_rpc workspace.create "$payload" | jq -r '.workspace.workspaceId' >"$WORK/dsh-edge.workspaceId" || return 1
  local ws
  ws=$(cat "$WORK/dsh-edge.workspaceId")
  [ -n "$ws" ] && [ "$ws" != "null" ] || { echo "::error::Пустой workspaceId от workspace.create" >&2; return 1; }
  printf '%s' "$ws"
}

dsh_edge_session_begin() { # SESSION_ID TITLE — создать/переиспользовать и назвать; идемпотентно
  dsh_edge_require_config || return 1
  local session_id=$1 title=$2 payload ws
  ws=$(dsh_edge_harness_workspace) || return 1
  payload=$(jq -n --arg wid "$ws" --arg sid "$session_id" '{workspaceId:$wid, sessionId:$sid}')
  dsh_edge_rpc session.create "$payload" >/dev/null || return 1
  payload=$(jq -n --arg sid "$session_id" --arg title "$title" '{sessionId:$sid, title:$title}')
  dsh_edge_rpc session.rename "$payload" >/dev/null || return 1
  printf '%s\n' "$session_id"
}

dsh_edge_ingest() { # SESSION_ID SPOOL_LINES_FILE — батч строк спула → события морды
  local session_id=$1 lines_file=$2 batch_file resp_file appended
  dsh_edge_init || return 1
  [ -s "$lines_file" ] || return 0   # пустой батч — не ошибка
  batch_file="$WORK/dsh-edge.ingest.json"
  # redact — единственное место обезвреживания секретов на пути спула в морду.
  redact <"$lines_file" | jq -s '{events: [.[] | {type: .type, data: .data}]}' >"$batch_file" \
    || { echo "::error::Не собрали ingest-батч (jq по строкам спула)" >&2; return 1; }
  # БЕЗ curl -f: отказ морды (404/409/413/500) должен доехать до лога job телом,
  # а не «curl: (22) The requested URL returned error» без причины.
  local http_code out appended resp_file="$WORK/dsh-edge.ingest.resp"
  http_code=$(curl -sS --connect-timeout 5 --max-time 30 -o "$resp_file" -w '%{http_code}' \
    -b "$DSH_EDGE_CJAR" -H 'content-type: application/json' \
    -X POST "$DSH_EDGE_URL/api/sessions/$session_id/ingest" \
    --data-binary "@$batch_file" 2>"$WORK/dsh-edge.ingest.err") \
    || { echo "::error::Ingest не дошёл до морды (curl): $(cat "$WORK/dsh-edge.ingest.err")" >&2; return 1; }
  case "$http_code" in
    2*) : ;;
    *)
      echo "::error::Ingest отклонён мордой (HTTP $http_code): $(cat "$resp_file" 2>/dev/null || echo "<нет тела>")" >&2
      return 1 ;;
  esac
  appended=$(jq -r '.appended // empty' "$resp_file" 2>/dev/null) \
    || appended=""
  if [ -z "$appended" ]; then
    echo "::error::Ingest: морда ответила 2xx без appended: $(cat "$resp_file" 2>/dev/null || echo "<нет тела>")" >&2
    return 1
  fi
  [ "$appended" -ge 1 ] \
    || { echo "::error::Ingest: морда не приняла ни одного события (appended=$appended)" >&2; return 1; }
}

dsh_edge_drain_batch() { # MODE LINES_FILE — один батч в морду; soft: одна попытка
  local mode=$1 lines=$2
  if dsh_edge_ingest "${DSH_EDGE_SESSION_ID:?DSH_EDGE_SESSION_ID не задан}" "$lines"; then
    return 0
  fi
  [ "$mode" = "hard" ] || return 1
  local attempt
  for attempt in 2 3 4 5; do
    sleep $((attempt * 2))
    dsh_edge_ingest "$DSH_EDGE_SESSION_ID" "$lines" && return 0
  done
  echo "::error::Морда не приняла батч транскрипта из 5 попыток — часть хода работы не доехала до морды" >&2
  return 1
}

dsh_edge_drain_spool() { # MODE — дочитывает СПОЛ (SPOOL_FILE) до курсора; 0: чисто, 1: сбой поста
  # cleanup зовёт drain и при смерти клиента ДО start_drain: без инициализации
  # (unset-курсор под set -u) дрен обязан тихо пропустить, а не уронить уборку.
  dsh_edge_init || return 0
  local mode=$1 drained lo end bl batch_lines
  local spool="${SPOOL_FILE:?SPOOL_FILE не задан}"
  drained=$(cat "$DSH_EDGE_DRAIN_CURSOR" 2>/dev/null)
  drained=${drained:-0}
  [ -f "$spool" ] || return 0
  local avail
  avail=$(wc -l <"$spool")
  [ "$avail" -gt "$drained" ] || return 0
  # Незавершённый хвост (без \n) не считается: wc — число полных строк.
  tail -n +"$((drained + 1))" "$spool" >"$WORK/dsh-edge.chunk.ndjson"
  local chunk_lines
  chunk_lines=$(wc -l <"$WORK/dsh-edge.chunk.ndjson")
  [ "$chunk_lines" -gt 0 ] || return 0
  # Нарезка ≤DSH_EDGE_BATCH строк И ≤DSH_EDGE_BATCH_BYTES байт (LC_ALL=C — байты).
  head -n "$chunk_lines" "$WORK/dsh-edge.chunk.ndjson" | LC_ALL=C \
    awk -v ml="$DSH_EDGE_BATCH" -v mb="$DSH_EDGE_BATCH_BYTES" -v out="$WORK/dsh-edge.batch" '
      function flushbatch() { printf "%s", buf > (out "." sprintf("%03d", ++k) ".lines"); buf = ""; n = 0; b = 0 }
      { len = length($0) + 1
        if (n >= ml || (n > 0 && b + len > mb)) flushbatch()
        buf = buf $0 "\n"; n++; b += len }
      END { if (n > 0) flushbatch() }'
  lo=$drained
  for batch_lines in "$WORK"/dsh-edge.batch.*.lines; do
    [ -e "$batch_lines" ] || continue
    bl=$(wc -l <"$batch_lines")
    end=$((lo + bl))
    dsh_edge_drain_batch "$mode" "$batch_lines" || return 1
    lo=$end
    echo "$lo" >"$DSH_EDGE_DRAIN_CURSOR"   # курсор растёт с каждым ПРИНЯТЫМ батчем
    rm -f "$batch_lines"
  done
  rm -f "$WORK/dsh-edge.chunk.ndjson"
}

DSH_EDGE_DRAIN_PID=""
dsh_edge_start_drain() { # фоновый мягкий дрен; сбой тика ретраится следующим
  dsh_edge_init || return 1
  printf '0\n' >"$DSH_EDGE_DRAIN_CURSOR"
  (
    while :; do
      sleep "${DRAIN_INTERVAL_SECS:-1}"
      dsh_edge_drain_spool soft || true
    done
  ) &
  DSH_EDGE_DRAIN_PID=$!
}

dsh_edge_stop_drain() {
  if [ -n "$DSH_EDGE_DRAIN_PID" ]; then
    kill "$DSH_EDGE_DRAIN_PID" 2>/dev/null || true
    wait "$DSH_EDGE_DRAIN_PID" 2>/dev/null || true
    DSH_EDGE_DRAIN_PID=""
  fi
}

dsh_edge_session_archive() { # SESSION_ID — архив; неизвестная сессия = норма (PR без раннера)
  dsh_edge_require_config || return 1
  dsh_edge_login || return 1
  local payload
  payload=$(jq -n --arg sid "$1" '{sessionId:$sid}')
  if dsh_edge_rpc workspace.archiveSession "$payload" >/dev/null; then
    echo "Сессия $1 заархивирована"
    return 0
  fi
  echo "::warning::Архив сессии $1 не удался (см. ошибку выше) — сессия останется в списке активных" >&2
  return 1
}
