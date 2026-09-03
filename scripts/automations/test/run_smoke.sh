#!/usr/bin/env bash
# Smoke клиента автоматизаций (scripts/automations/run.sh) на заглушках внешнего
# мира — гвардия класса Б1 (#119, ревью #128): bash -n тело не исполняет, «удалил
# определение при живом вызове» красится только полным прогоном.
#
# Сценарии:
#   A. kind=pool: конфиг из журнала → gh создаёт задачу пула → журнал получил
#      job_start / automation_result {ok:true, issue:999} / job_end ok, код 0.
#   B. kind=digest без каналов: digest.py сам отклоняет (второй рубеж формы) —
#      job_end fail, код не 0, сети не было.
#   C. конфига в журнале нет: честный красный след (job_start + job_end fail).
#   D. автоматизация выключена: работы нет, job_start с skipped, job_end ok.
#
# curl/gh/timeout — функции-заглушки (export -f), digest.py — настоящий
# (сценарий B до сети не доходит: каналы проверяются первыми). Тела POST
# /api/events складываются в events.ndjson — по ним ассерты журнала.
#
# Запуск: bash scripts/automations/test/run_smoke.sh  (jq обязателен)
set -uo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
# SMOKE_KEEP=1 — оставить временный каталог для разбора упавшего смоука.
[ "${SMOKE_KEEP:-0}" = "1" ] || trap 'rm -rf "$TMP"' EXIT

STATE="$TMP/state"
mkdir -p "$STATE"
EVENTS="$TMP/events.ndjson"
: >"$EVENTS"
STDOUT_LOG="$TMP/stdout.log"
FAILURES=0
export TMP

# ── Заглушки внешнего мира ──────────────────────────────────────────────────────────

curl() {
  local url="" method="GET" body="" prev="" arg
  for arg in "$@"; do
    case "$arg" in
      http*) url=$arg ;;
      POST) method="POST" ;;
    esac
    case "$prev" in
      -d) body=$arg ;;
    esac
    prev=$arg
  done
  printf 'method=%s url=%s body_len=%s\n' "$method" "$url" "${#body}" >>"$TMP/calls.log"
  if [ "$method" = "POST" ] && [[ "$url" == *"/api/events" ]] && [ -n "$body" ]; then
    printf '%s\n' "$body" >>"$EVENTS"
  fi
  case "$url" in
    */api/automations)
      [ -f "$STATE/no-config" ] && { printf '{"automations": []}'; return 0; }
      if [ -f "$STATE/disabled" ]; then
        printf '{"automations": [{"id": "smoke", "enabled": false, "config": {"enabled": false, "trigger": {"type": "webhook"}, "task": {"kind": "pool", "title": "t", "body": ""}, "report": {"channels": []}}}]}'
      else
        printf '{"automations": [{"id": "smoke", "enabled": true, "config": %s}]}' "$(cat "$STATE/config")"
      fi
      ;;
    *) printf '{}' ;;
  esac
}

gh() {
  local arg
  for arg in "$@"; do
    if [ "$arg" = "issue" ]; then
      printf 'https://github.com/mytab0r/edge-harness/issues/999'
      return 0
    fi
  done
  echo "gh: неожиданный вызов: $*" >&2
  return 1
}

timeout() { local secs=$1; shift; "$@"; }  # секунды не ждём: digest.py в смоуке отклоняется до сети

export -f curl gh timeout
export STATE EVENTS

run_automation() { # task_id → код job'а
  ( cd "$REPO" && TASK_ID="$1" AUTOMATION_ID="smoke" \
    HANDS_URL="https://hands.smoke" HANDS_TOKEN="t" \
    GITHUB_REPOSITORY="mytab0r/edge-harness" RUNNER_TEMP="$TMP" \
    bash scripts/automations/run.sh ) >"$STDOUT_LOG" 2>&1
}

check() { # имя код_или_строка
  local name=$1 result=$2
  if [ "$result" = "0" ]; then
    echo "  ok: $name"
  else
    echo "  FAIL: $name" >&2
    FAILURES=$((FAILURES + 1))
  fi
}

journal_has() { # jq-условие по всем событиям журнала; -s агрегирует батчи
  # (файл — поток батчей {task_id, events}; без -s jq -e смотрит только последний)
  jq -s -e "any(.[] | .events[]; $1)" "$EVENTS" >/dev/null 2>&1
}

# ── A: pool — счастливый путь ───────────────────────────────────────────────────────
echo "A: kind=pool"
printf '{"enabled": true, "trigger": {"type": "webhook"}, "task": {"kind": "pool", "title": "разобрать хвост", "body": "тело задачи"}, "report": {"channels": []}}' >"$STATE/config"
run_automation "automation:smoke:A"; check "код 0" "$?"
check "gh создал задачу пула" "$(grep -qc "issues/999" "$STDOUT_LOG" && echo 0 || echo 1)"
check "job_start в журнале" "$(journal_has '.kind == "job_start" and .data.automation == "smoke"' && echo 0 || echo 1)"
check "automation_result ok, issue 999" "$(journal_has '.kind == "automation_result" and .data.ok == true and .data.issue == 999' && echo 0 || echo 1)"
check "job_end ok" "$(journal_has '.kind == "job_end" and .data.result == "ok"' && echo 0 || echo 1)"

: >"$EVENTS"

# ── B: digest без каналов — второй рубеж формы, красный прогон без сети ─────────────
echo "B: kind=digest без каналов"
printf '{"enabled": true, "trigger": {"type": "webhook"}, "task": {"kind": "digest"}, "report": {"channels": []}}' >"$STATE/config"
run_automation "automation:smoke:B"; RC=$?
check "код не 0" "$([ "$RC" -ne 0 ] && echo 0 || echo 1)"
check "job_end fail в журнале" "$(journal_has '.kind == "job_end" and .data.result == "fail"' && echo 0 || echo 1)"

: >"$EVENTS"

# ── C: конфига нет — честный красный след ───────────────────────────────────────────
echo "C: конфиг отсутствует"
touch "$STATE/no-config"
run_automation "automation:smoke:C"; RC=$?
check "код не 0" "$([ "$RC" -ne 0 ] && echo 0 || echo 1)"
check "журнал знает про поломку" "$(journal_has '.kind == "automation_result" and .data.ok == false and (.data.error | contains("не найден"))' && journal_has '.kind == "job_end" and .data.result == "fail"' && echo 0 || echo 1)"
rm -f "$STATE/no-config"

: >"$EVENTS"

# ── D: автоматизация выключена — работы нет, прогон честно ok ───────────────────────
echo "D: выключена"
touch "$STATE/disabled"
run_automation "automation:smoke:D"; check "код 0" "$?"
check "job_start с skipped=disabled" "$(journal_has '.kind == "job_start" and .data.skipped == "disabled"' && echo 0 || echo 1)"
check "job_end ok" "$(journal_has '.kind == "job_end" and .data.result == "ok"' && echo 0 || echo 1)"

echo
if [ "$FAILURES" -ne 0 ]; then
  echo "run_smoke: $FAILURES проверок не сошлось (хвост stdout клиента):" >&2
  tail -5 "$STDOUT_LOG" >&2
  exit 1
fi
echo "run_smoke: все проверки сошлись"
