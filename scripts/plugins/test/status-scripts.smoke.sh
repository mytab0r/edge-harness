#!/usr/bin/env bash
# Гвардия класса «удалил определение при живом вызове» (ревью #128, Б1) для
# обёрток статусов канала конвейера (#86, бывший журнал): bash -n тело не
# исполняет и молчит, когда функция исчезла из общей библиотеки, а вызов остался.
# Здесь каждая обёртка (plugin_status.sh, integration_status.sh) исполняется
# ЦЕЛИКОМ дочерним bash на заглушке curl (export -f затеняет бинарник): морда
# отвечает логином (303), /api/auth/session, RPC session.create/rename
# ({result:{ok:true}}) и ingest'ом ({appended:1}).
#
# Ассерты: код 0, ingest-тело по контракту канала (user/message с
# source.kind=harness-pipeline-status, внутри текста — строгий рекорд статуса
# с task_id/kind/state/detail), финальный статус при лежащей морде красит вызов
# (exit 1), промежуточный — нет (exit 0). Сломанная проводка библиотеки = красный
# smoke, а не тихий пропуск статуса на живом деплое.
#
# Запуск: bash scripts/plugins/test/status-scripts.smoke.sh  (jq обязателен)
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
CALLLOG="$TMP/calls.log"
export CALLLOG
: >"$CALLLOG"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# Многоразовая заглушка морды: логин → кука → RPC ок → ingest принял 1.
# -o ФАЙЛ получает тело ответа (как у настоящего curl), -w печатает код.
curl() {
  local url="" method="GET" data="" datafile="" out="" wfmt="" i prev=""
  local body=""
  for i in "$@"; do
    case "$i" in
      http*) url=$i ;;
      -X) method="PENDING" ;;
      -o|-w|--data-binary|--data|-d) : ;;
      --data-urlencode) data="__form" ;;
    esac
    case "$prev" in
      -X) method=$i ;;
      -o) out=$i ;;
      -w) wfmt=$i ;;
      --data-binary|--data|-d)
        case "$i" in
          @*) datafile=${i#@} ;;
          *) data=$i ;;
        esac ;;
    esac
    prev=$i
  done
  case "$url" in
    */api/auth/login) body=""; printf '303'; return 0 ;;
    */api/auth/session) body='{"authenticated":true}' ;;
    */ingest) body='{"appended":1,"lastSeq":42}' ;;
    */api/*) body='{"type":"server-response","rpcId":"smoke","result":{"ok":true,"value":{"workspace":{"workspaceId":"ws-smoke"}}}}' ;;
    *) body='{}' ;;
  esac
  if [ -n "$datafile" ]; then
    # Тела POST-ов пишутся в отдельный слот: ingest — последний из них,
    # его и читают ассерты (тело многострочно, в лог-строку не положить).
    cat "$datafile" >"$CALLLOG.body" 2>/dev/null || : >"$CALLLOG.body"
    printf '%s\n' "$method $url <body>" >>"$CALLLOG"
  else
    printf '%s\n' "$method $url $data" >>"$CALLLOG"
  fi
  if [ -n "$out" ]; then printf '%s\n' "$body" >"$out"; fi
  [ -n "$wfmt" ] && printf '200'
  [ -z "$out" ] && [ -z "$wfmt" ] && printf '%s\n' "$body"
  return 0
}
export -f curl
sleep() { :; }   # ретраи не тянут прогон: механика ретраев проверена кейсом «морда лежит» ниже
export -f sleep

run_wrapper() { # $1 — скрипт-обёртка, остальные — env-присваивания
  local script="$1"
  shift
  env "$@" DSH_EDGE_URL="https://morde.test" DSH_EDGE_ACCESS_KEY="morde-key-smoke" \
    WORK="$TMP/work" bash "$REPO/scripts/plugins/$script"
}

ingest_body() { # тело последнего POST-запроса (ingest), из слота тел
  cat "$CALLLOG.body"
}

# ── plugin_status.sh: контракт события канала ─────────────────────────────────
run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=ready FINAL=1 DETAIL="0.1.1" >/dev/null
body=$(ingest_body)
jq -e '.events | length == 1' >/dev/null 2>&1 <<<"$body" \
  || { echo "::error::plugin_status: ingest-тело без массива events: $body" >&2; exit 1; }
jq -e '.events[0].type == "user/message"' >/dev/null <<<"$body" \
  || { echo "::error::plugin_status: событие не user/message: $body" >&2; exit 1; }
jq -e '.events[0].data.source.kind == "harness-pipeline-status"' >/dev/null <<<"$body" \
  || { echo "::error::plugin_status: нет штампа источника канала: $body" >&2; exit 1; }
record=$(jq -c '.events[0].data.content[0].text | fromjson' <<<"$body")
[ "$(jq -r '.task_id' <<<"$record")" = "plugin:hello" ] \
  || { echo "::error::plugin_status: task_id потерян: $record" >&2; exit 1; }
[ "$(jq -r '.kind' <<<"$record")" = "plugin_status" ] \
  || { echo "::error::plugin_status: kind потерян: $record" >&2; exit 1; }
[ "$(jq -r '.data.state' <<<"$record")" = "ready" ] \
  || { echo "::error::plugin_status: state потерян: $record" >&2; exit 1; }
[ "$(jq -r '.data.detail' <<<"$record")" = "0.1.1" ] \
  || { echo "::error::plugin_status: detail потерян: $record" >&2; exit 1; }
[ -n "$(jq -r '.emitted' <<<"$record")" ] \
  || { echo "::error::plugin_status: повторную доставку не отличить — нет emitted: $record" >&2; exit 1; }

# ── integration_status.sh: контракт события канала ────────────────────────────
: >"$CALLLOG"
run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=not_configured FINAL=1 DETAIL="нет секретов: JIRA_API_TOKEN" >/dev/null
body=$(ingest_body)
record=$(jq -c '.events[0].data.content[0].text | fromjson' <<<"$body")
[ "$(jq -r '.task_id' <<<"$record")" = "integration:jira" ] \
  || { echo "::error::integration_status: task_id потерян: $record" >&2; exit 1; }
[ "$(jq -r '.kind' <<<"$record")" = "integration_status" ] \
  || { echo "::error::integration_status: kind потерян: $record" >&2; exit 1; }
[ "$(jq -r '.data.state' <<<"$record")" = "not_configured" ] \
  || { echo "::error::integration_status: state потерян: $record" >&2; exit 1; }
grep -q "нет секретов: JIRA_API_TOKEN" <<<"$body" \
  || { echo "::error::integration_status: detail потерян: $body" >&2; exit 1; }

# ── Промежуточный статус проходит тем же каналом (FINAL=0 по умолчанию) ───────
: >"$CALLLOG"
if ! run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=deploying >/dev/null; then
  echo "::error::промежуточный статус при живой морде не должен красить вызов" >&2
  exit 1
fi
grep -q 'ingest' "$CALLLOG" || { echo "::error::промежуточный статус не дошёл до ingest" >&2; exit 1; }
# state живёт внутри JSON-экранированного текста рекорда — grep -F по сырой форме
grep -qF '\"state\":\"deploying\"' "$CALLLOG.body" \
  || { echo "::error::промежуточный статус дошёл до ingest с другим телом" >&2; exit 1; }

# ── Финальный статус при лежащей морде красит, промежуточный — нет ────────────
curl() { return 7; } # морда лежит (сетевой отказ)
export -f curl
if run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=ready FINAL=1 >/dev/null 2>&1; then
  echo "::error::final: лежащая морда обязана красить вызов (fail loud), а не возвращать 0" >&2
  exit 1
fi
if ! run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=deploying FINAL=0 >/dev/null 2>&1; then
  echo "::error::промежуточный статус при лежащей морде не должен красить вызов" >&2
  exit 1
fi

# ── Не настроено ≠ сломано: пустая конфигурация — громкий отказ без сети ──────
if env -u DSH_EDGE_URL -u DSH_EDGE_ACCESS_KEY WORK="$TMP/work" \
    PLUGIN_ID=hello STATE=ready bash "$REPO/scripts/plugins/plugin_status.sh" >/dev/null 2>/tmp/pipeline-cfg-msg.txt; then
  echo "::error::без конфигурации морды вызов обязан падать громко, а не молча пропускать статус" >&2
  exit 1
fi
grep -q "не настроен\|не задан" /tmp/pipeline-cfg-msg.txt \
  || { echo "::error::отказ конфигурации должен называться «не настроен/не заданы», а не сетевым сбоем" >&2; exit 1; }
rm -f /tmp/pipeline-cfg-msg.txt

echo "status-scripts smoke: ок — обе обёртки держат контракт канала конвейера и градацию fail loud"
