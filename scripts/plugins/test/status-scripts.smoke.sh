#!/usr/bin/env bash
# Гвардия класса «удалил определение при живом вызове» (ревью #128, Б1) для
# обёрток статусов журнала: bash -n тело не исполняет и молчит, когда функция
# исчезла из общей библиотеки, а вызов остался. Здесь каждая обёртка
# (plugin_status.sh, integration_status.sh) исполняется ЦЕЛИКОМ дочерним bash
# на заглушке curl (export -f затеняет бинарник): журнал отдаёт прод-форму
# {events:[{seq}], has_more, next_after} и принимает POST /api/events.
#
# Ассерты: код 0, POST-тело по контракту журнала (task_id, kind, data, seq =
# max+1 — посев с сервера, не с потолка), финальный статус при лежащем журнале
# красит вызов (exit 1), промежуточный — нет (exit 0). Сломанная проводка
# библиотеки = красный smoke, а не тихий пропуск статуса на живом деплое.
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

curl() { # заглушка: GET (посев seq) и POST (приём события) журнала
  local url="" method="GET" data="" i prev=""
  for i in "$@"; do
    case "$i" in
      http*) url=$i ;;
      -X) method="PENDING" ;;
      -d|--data|--data-binary) : ;;
    esac
    case "$prev" in
      -X) method=$i ;;
      -d|--data|--data-binary) data=$i ;;
    esac
    prev=$i
  done
  printf '%s\n' "$method $url $data" >>"$CALLLOG"
  if [ "$method" = "POST" ]; then
    echo '{}'
  else
    echo '{"events":[{"id":1,"seq":3,"kind":"plugin_status","data":{}}],"has_more":false,"next_after":1}'
  fi
}
export -f curl

run_wrapper() { # $1 — скрипт-обёртка, остальные — env-присваивания
  local script="$1"
  shift
  env "$@" HARNESS_URL="https://journal.test" HANDS_TOKEN="hands-token-smoke" \
    bash "$REPO/scripts/plugins/$script"
}

post_bodies() { # POST-записи из журнала вызовов (тело jq печатает многострочно)
  sed -n '/^POST/,$p' "$CALLLOG" || true
}

# ── plugin_status.sh: контракт события ────────────────────────────────────────
run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=ready FINAL=1 DETAIL="0.1.1" >/dev/null
body=$(post_bodies)
for marker in '"task_id": "plugin:hello"' '"kind": "plugin_status"' '"plugin": "hello"' '"state": "ready"' '"detail": "0.1.1"' '"seq": 4'; do
  if ! grep -q -- "$marker" <<<"$body"; then
    echo "::error::plugin_status: в POST-теле нет $marker — контракт журнала нарушен" >&2
    exit 1
  fi
done

# ── integration_status.sh: контракт события ───────────────────────────────────
: >"$CALLLOG"
run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=not_configured FINAL=1 DETAIL="нет секретов: JIRA_API_TOKEN" >/dev/null
body=$(post_bodies)
for marker in '"task_id": "integration:jira"' '"kind": "integration_status"' '"integration": "jira"' '"state": "not_configured"' 'нет секретов: JIRA_API_TOKEN' '"seq": 4'; do
  if ! grep -q -- "$marker" <<<"$body"; then
    echo "::error::integration_status: в POST-теле нет $marker — контракт журнала нарушен" >&2
    exit 1
  fi
done

# ── Многостраничный посев seq: первая страница отдаёт has_more=true, ──────────
# вторая (после after) — хвост. seq события берётся как max по ОБЕИМ страницам.
: >"$CALLLOG"
curl() {
  local url="" method="GET" data="" i prev=""
  for i in "$@"; do
    case "$i" in
      http*) url=$i ;;
      -X) method="PENDING" ;;
    esac
    case "$prev" in
      -X) method=$i ;;
      -d|--data|--data-binary) data=$i ;;
    esac
    prev=$i
  done
  printf '%s\n' "$method $url $data" >>"$CALLLOG"
  if [ "$method" = "POST" ]; then
    echo '{}'
  elif [[ "$url" == *"after=0"* ]]; then
    echo '{"events":[{"id":1,"seq":5,"kind":"plugin_status","data":{}}],"has_more":true,"next_after":1}'
  else
    echo '{"events":[{"id":2,"seq":9,"kind":"plugin_status","data":{}}],"has_more":false,"next_after":2}'
  fi
}
export -f curl
run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=deploying FINAL=0 >/dev/null
if ! grep -q -- '"seq": 10' "$CALLLOG"; then
  echo "::error::многостраничный посев: seq не взял максимум с обеих страниц (ожидается 10 = max(5,9)+1)" >&2
  exit 1
fi

# ── Финальный статус при лежащем журнале красит, промежуточный — нет ──────────
curl() { return 7; } # журнал лежит
export -f curl
sleep() { :; }       # ретраи не тянут прогон: механика ретраев проверена выше
export -f sleep
if run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=ready FINAL=1 >/dev/null 2>&1; then
  echo "::error::final: лежащий журнал обязан красить вызов (fail loud), а не возвращать 0" >&2
  exit 1
fi
if ! run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=deploying FINAL=0 >/dev/null 2>&1; then
  echo "::error::промежуточный статус при лежащем журнале не должен красить вызов" >&2
  exit 1
fi

echo "status-scripts smoke: ок — обе обёртки держат контракт журнала и градацию fail loud"
