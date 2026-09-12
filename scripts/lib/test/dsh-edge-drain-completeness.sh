#!/usr/bin/env bash
# Issue #1048 — доказательство требования «финальный слив не теряет хвост»:
# при БОЛЬШОМ DRAIN_INTERVAL_SECS периодический (soft) цикл дрена
# (dsh_edge_start_drain) физически не успевает сработать ни разу за время
# теста — эта проверка подтверждает, что ПОЛНОТА транскрипта не зависит от
# периодичности: единственный вызов dsh_edge_drain_spool hard в конце (тот
# же путь, что scripts/hands/dsh_task.sh и scripts/worker/task.sh зовут
# после завершения dsh) выносит спул ЦЕЛИКОМ, независимо от того, сколько
# (если вообще) успел сделать soft-цикл. Не полагается на гонку: перед
# финальным hard-вызовом читает журнал ingest-вызовов и требует, чтобы их
# БЫЛО РОВНО НОЛЬ (иначе тест не изолирует то, что проверяет, и сам обязан
# упасть громко, а не притвориться зелёным).
#
# Запуск: bash scripts/lib/test/dsh-edge-drain-completeness.sh (jq обязателен)
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$TEST_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
INGEST_LOG="$TMP/ingest-calls.log"
: >"$INGEST_LOG"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# redact() (dsh_edge_ingest пропускает батч через неё) — из dsh-ci.sh,
# ДО библиотеки сессии, тот же порядок, что предписывает шапка
# dsh-edge-session.sh.
source "$REPO/scripts/lib/dsh-ci.sh"
source "$REPO/scripts/lib/dsh-edge-session.sh"

# Заглушка curl: единственный маршрут, которым дрен реально пользуется —
# POST .../ingest (dsh_edge_ingest). Каждый вызов дописывает число событий
# батча в INGEST_LOG — так тест видит ФАКТ количества вызовов и их состав,
# не гадает по побочным эффектам.
curl() {
  local url="" data_file="" outfile="" arg prev=""
  for arg in "$@"; do
    case "$arg" in http*) url=$arg ;; esac
    case "$prev" in
      -o) outfile=$arg ;;
      --data-binary) data_file="${arg#@}" ;;
    esac
    prev=$arg
  done
  case "$url" in
    */ingest)
      # $data_file — уже собранный конверт {"events":[...]} (dsh_edge_ingest),
      # не сырые строки спула: считаем длину МАССИВА events внутри, не число
      # top-level значений (`jq -s length` здесь всегда дало бы 1 — конверт
      # это один JSON-объект, не несколько значений построчно).
      local count
      count=$(command jq '.events | length' <"$data_file" 2>/dev/null || echo 0)
      printf 'events=%s\n' "$count" >>"$INGEST_LOG"
      printf '{"appended":%s,"lastSeq":%s}' "$count" "$count" >"$outfile"
      printf '%s' 200 ;;
    *)
      echo "СТУБ curl (тест дрена): неожиданный URL «$url»" >&2
      return 1 ;;
  esac
}

WORK="$TMP/work"
mkdir -p "$WORK"
export WORK
export DSH_EDGE_URL="https://morde.test"
export DSH_EDGE_ACCESS_KEY="test-access-key-at-least-32-bytes-long!!"
export DSH_EDGE_SESSION_ID="harness-test"

SPOOL_FILE="$TMP/session-stream.ndjson"
export SPOOL_FILE
TOTAL_LINES=37
: >"$SPOOL_FILE"
for i in $(seq 1 "$TOTAL_LINES"); do
  printf '{"type":"note","data":{"i":%s}}\n' "$i" >>"$SPOOL_FILE"
done

# Интервал ЗАВЕДОМО больше времени работы этого теста: periodic soft-цикл
# физически не может протикать внутри него ни разу.
export DRAIN_INTERVAL_SECS=999999
dsh_edge_start_drain
sleep 1   # шанс планировщику ОС провернуть фоновый процесс; не тест на время
dsh_edge_stop_drain

pre_hard_calls=$(wc -l <"$INGEST_LOG" 2>/dev/null || echo 0)
if [ "$pre_hard_calls" -ne 0 ]; then
  echo "::error::periodic soft-дрен успел сработать $pre_hard_calls раз(а) до финального hard — тест не изолирует то, что проверяет (уменьши DRAIN_INTERVAL_SECS теста или профиль машины слишком медленный)" >&2
  exit 1
fi

# Финальный слив — тот же вызов, что клиенты делают ПОСЛЕ dsh (dsh_task.sh:
# dsh_edge_stop_drain; dsh_edge_drain_spool hard).
dsh_edge_drain_spool hard

drained=$(cat "$WORK/dsh-edge.drained" 2>/dev/null || echo 0)
if [ "$drained" -ne "$TOTAL_LINES" ]; then
  echo "::error::hard-дрен вынес $drained из $TOTAL_LINES строк спула — хвост потерян при большом DRAIN_INTERVAL_SECS (issue #1048)" >&2
  exit 1
fi

total_ingested=$(awk -F= '{s+=$2} END{print s+0}' "$INGEST_LOG")
if [ "$total_ingested" -ne "$TOTAL_LINES" ]; then
  echo "::error::морда приняла $total_ingested событий вместо $TOTAL_LINES суммарно по вызовам ingest — хвост потерян (issue #1048)" >&2
  exit 1
fi

echo "OK: hard-дрен вынес все $TOTAL_LINES строк спула независимо от DRAIN_INTERVAL_SECS=999999 (periodic ни разу не сработал до него)"
