#!/usr/bin/env bash
# Гвардия класса #1286 (живой отказ — прогон worker.yml 34893177035, задача
# #1286): цепочка провайдеров исчерпана целиком, задача корректно возвращена
# в пул (release-full), сигнал ушёл в задачу и Telegram — а job всё равно
# красный: `die` в конце блока провайдерного отказа в scripts/worker/task.sh.
# Красный прогон воркера по чужой (провайдерной) вине кормит предохранитель
# диспатча (#226) и задерживает восстановление после сброса квоты (снимает
# его только зелёная проба, #205). Контракт после #1286: код возврата job'а
# разделяет классы так же, как шапка комментария (#1322) —
#   quota_exhausted / rate_limit_retry_budget_exceeded / all_providers_exhausted
#     -> exit 0 (задача возвращена в пул, сигнал живёт в комментарии/Telegram);
#   prompt_too_long -> die (НАШ детерминированный отказ, #1315: серию его
#     повторов останавливает предохранитель — красные прогоны worker.yml его
#     кормят; зелёный код возврата сделал бы этот названный тормоз слепым).
#
# Извлекаем РЕАЛЬНЫЙ блок из scripts/worker/task.sh (между якорь-комментарием
# «Цепочка провайдеров отказала — квота» и первым закрывающим `fi` на нулевом
# отступе) и исполняем его как есть — не переписываем логику здесь заново,
# иначе тест держит копию, а не проверяет код (тот же приём, что
# continue-pr-worktree.smoke.sh, класс #476).
#
# Мутации, которыми доказана проверка (исполнены в PR #1313, дословный вывод
# в теле PR):
#   1) верни блоку безусловный `die` вместо `case … *) exit 0` (старое
#      поведение до #1286) — сценарий 1 обязан покраснеть;
#   2) сделай prompt_too_long тоже зелёным (`prompt_too_long) exit 0`) —
#      сценарий 4 обязан покраснеть: снятие тормоза #1315 не проходит молча.
#
# Запуск: bash scripts/worker/test/provider-exhaustion-exit-code.smoke.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TASK_SH="$REPO_ROOT/scripts/worker/task.sh"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail() { echo "::error::SMOKE(worker-exhaustion-exit-code): $*" >&2; exit 1; }
note() { echo "SMOKE(worker-exhaustion-exit-code): $*"; }

# ── извлечение блока провайдерного отказа из настоящего task.sh ─────────────
SNIPPET="$WORK/exhaustion-block.sh"
awk '
  /^# Цепочка провайдеров отказала — квота/ { grab = 1 }
  grab { print }
  grab && /^fi$/ { exit }
' "$TASK_SH" >"$SNIPPET"
[ -s "$SNIPPET" ] || fail "блок провайдерного отказа не найден в $TASK_SH — якорь-комментарий переименован?"
# Гвардия дрейфа якоря: тест обязан исполнять блок с контрактом кода возврата,
# а не случайный соседний текст (fail loud, не silent-wrong).
grep -q 'case "$WORKER_TASK_FAILURE_REASON" in' "$SNIPPET" \
  || fail "в извлечённом блоке нет case по WORKER_TASK_FAILURE_REASON — контракт кода возврата переехал или якорь снял чужой блок?"
grep -q 'prompt_too_long) die ' "$SNIPPET" \
  || fail "в извлечённом блоке нет громкой ветки prompt_too_long) die — тормоз #1315 снят?"

# ── заглушки каналов блока (сеть не нужна): каждая пишет свой след ───────────
CALLS="$WORK/calls.log"
: >"$CALLS"
export CALLS
export number=1286
export rc=1
export ERR_TAIL="хвост stderr DSH (заглушка)"
export ANSWER_TAIL="хвост ответа DSH (заглушка)"
export WORKER_RATE_LIMIT_MAX_WAIT_SECS=1800
export WORKER_CHAIN_TRIED="anthropic-oauth-pool, GLM"
export WORKER_CHAIN_RESET_HINT="GLM: 2026-09-17 08:51:55"
export WORKER_CHAIN_OUTCOME_SUMMARY="6 из 8 без настоящей попытки"
export WORKER_CHAIN_RETRY_USEFUL="0"

die() { echo "die: $*" >>"$CALLS"; exit 7; }
lease_cli() { echo "lease_cli $*" >>"$CALLS"; echo "замок и назначение сняты"; }
gh() { echo "gh $*" >>"$CALLS"; return 0; }
telegram_report() { echo "telegram_report" >>"$CALLS"; return 0; }
export -f die lease_cli gh telegram_report

run_block() { # $1 = WORKER_TASK_FAILURE_REASON; вызывать как `run_block X || rc=$?`
  WORKER_TASK_FAILURE_REASON="$1" bash "$SNIPPET" >>"$CALLS" 2>&1
}

# ── 1) живой класс отказа задачи #1286: all_providers_exhausted → job зелёный,
# задача возвращена в пул, сигнал ушёл в комментарий и Telegram ───────────────
rc_run=0
run_block all_providers_exhausted || rc_run=$?
[ "$rc_run" = "0" ] || fail "1) all_providers_exhausted обязан завершать job кодом 0 (задача возвращена в пул, не сбой воркера), получено rc=$rc_run"
grep -q "lease_cli release-full 1286" "$CALLS" \
  || fail "1) задача не возвращена в пул: release-full не вызван (следы: $(head -5 "$CALLS"))"
grep -q "gh issue comment 1286" "$CALLS" \
  || fail "1) комментарий в задачу не отправлен — сигнал канала обязан остаться (fail loud), следы: $(head -5 "$CALLS")"
grep -qF "цепочка провайдеров исчерпана целиком" "$CALLS" \
  || fail "1) в комментарии не прод-формулировка исчерпания цепочки (#1307): $(head -8 "$CALLS")"
grep -q "telegram_report" "$CALLS" \
  || fail "1) Telegram-сигнал не отправлен — канал владельца обязан остаться, следы: $(head -5 "$CALLS")"
note "1) all_providers_exhausted -> rc=0, release-full + комментарий + Telegram — ок"

# ── 2) quota_exhausted — тот же провайдерный класс → зелёный ────────────────
: >"$CALLS"
rc_run=0
run_block quota_exhausted || rc_run=$?
[ "$rc_run" = "0" ] || fail "2) quota_exhausted обязан завершать job кодом 0, получено rc=$rc_run"
grep -q "lease_cli release-full 1286" "$CALLS" || fail "2) release-full не вызван"
note "2) quota_exhausted -> rc=0 — ок"

# ── 3) rate_limit_retry_budget_exceeded — тот же провайдерный класс → зелёный ─
: >"$CALLS"
rc_run=0
run_block rate_limit_retry_budget_exceeded || rc_run=$?
[ "$rc_run" = "0" ] || fail "3) rate_limit_retry_budget_exceeded обязан завершать job кодом 0, получено rc=$rc_run"
grep -q "lease_cli release-full 1286" "$CALLS" || fail "3) release-full не вызван"
note "3) rate_limit_retry_budget_exceeded -> rc=0 — ок"

# ── 4) prompt_too_long — НАШ отказ (#1315) → громкий die, job красный ────────
: >"$CALLS"
rc_run=0
run_block prompt_too_long || rc_run=$?
[ "$rc_run" = "7" ] || fail "4) prompt_too_long обязан умирать громко (die, rc=7): серию его повторов останавливает предохранитель диспатча (#1315), получено rc=$rc_run"
grep -q "^die: " "$CALLS" || fail "4) die не вызван для prompt_too_long, следы: $(head -5 "$CALLS")"
grep -q "lease_cli release-full 1286" "$CALLS" || fail "4) release-full не вызван и для НАШЕГО отказа — задача обязана вернуться в пул (#1315)"
note "4) prompt_too_long -> die (rc=7), задача всё равно возвращена в пул — ок"

# ── 5) #1307: честная формулировка, когда повтор ИМЕЕТ смысл ─────────────────
: >"$CALLS"
WORKER_CHAIN_RETRY_USEFUL="1" run_block all_providers_exhausted || true
grep -qF "Повтор ИМЕЕТ смысл" "$CALLS" \
  || fail "5) при WORKER_CHAIN_RETRY_USEFUL=1 комментарий обязан называть, что повтор имеет смысл (#1307): $(head -8 "$CALLS")"
grep -q "lease_cli release-full 1286" "$CALLS" || fail "5) release-full не вызван"
note "5) #1307-формулировка доходит до комментария — ок"

echo "SMOKE(worker-exhaustion-exit-code): контракт кода возврата блока провайдерного отказа выполнен"
