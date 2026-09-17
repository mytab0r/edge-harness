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
#     -> exit 0 (задача возвращена в пул, сигнал живёт в комментарии/Telegram,
#        комментарий несёт машиночитаемый маркер WORKER_PROVIDER_REFUSAL_MARKER
#        для оркестратора — класс «второй-потребитель-забыт», ai-review #1313);
#   prompt_too_long -> die (НАШ детерминированный отказ, #1315: серию его
#     повторов останавливает предохранитель — красные прогоны worker.yml его
#     кормят; зелёный код возврата сделал бы этот названный тормоз слепым);
#   неизвестный класс -> громкий die: молчаливый зелёный дефолт спрятал бы
#     завтрашний пятый класс (находка ai-review PR #1313, ЗАМЕЧАНИЕ).
#
# Извлекаем РЕАЛЬНЫЙ блок из scripts/worker/task.sh (между якорь-комментарием
# «Цепочка провайдеров отказала — квота» и первым закрывающим `fi` на нулевом
# отступе) и исполняем его как есть — не переписываем логику здесь заново,
# иначе тест держит копию, а не проверяет код (тот же приём, что
# continue-pr-worktree.smoke.sh, класс #476).
#
# Мутации (исполнены, дословный вывод в теле PR #1313). Точка покраснения
# названа честно, по слоям:
#   1) «зелёная ветка -> die» / «prompt_too_long -> зелёная» — тест краснеет
#      на ГВАРДИИ ДРЕЙФА ЯКОРЯ ниже (структурная проверка контракта в
#      извлечённом блоке): защитный слой срабатывает раньше сценариев;
#   2) поведенческий слой доказан ОТДЕЛЬНО (та же мутация при СНЯТЫХ
#      структурных проверках теста), дословно:
#      1-я мутация -> `::error::SMOKE(worker-exhaustion-exit-code): 1)
#      all_providers_exhausted обязан завершать job кодом 0 (задача возвращена
#      в пул, не сбой воркера), получено rc=7`;
#      2-я мутация -> `::error::SMOKE(worker-exhaustion-exit-code): 4)
#      prompt_too_long обязан умирать громко (die, rc=7): серию его повторов
#      останавливает предохранитель диспатча (#1315), получено rc=0`.
#
# Запуск: bash scripts/worker/test/provider-exhaustion-exit-code.smoke.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TASK_SH="$REPO_ROOT/scripts/worker/task.sh"
SCHEDULER="$REPO_ROOT/scripts/orchestra/scheduler.py"

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
grep -q 'refusal_marker=' "$SNIPPET" \
  || fail "в извлечённом блоке нет refusal_marker — машиночитаемый след провайдерного отказа переехал или якорь снял чужой блок?"
grep -q 'job_exit="green"' "$SNIPPET" \
  || fail "в извлечённом блоке нет зелёной ветки job_exit — фикс #1286 снят?"
grep -q 'prompt_too_long) ;;' "$SNIPPET" \
  || fail "в извлечённом блоке нет громкой ветки prompt_too_long (die) — тормоз #1315 снят?"
grep -q 'неизвестный класс отказа' "$SNIPPET" \
  || fail "в извлечённом блоке нет громкой ветки неизвестного класса — дефолт снова молча-зелёный (находка ai-review PR #1313)?"

# ── одно место правды на маркер: bash-писатель (task.sh) и питон-читатель
# (scheduler.py) обязаны нести ОДИН и тот же литерал ──────────────────────────
MARKER="[воркер: провайдерный отказ (#1286)]"
grep -qF "$MARKER" "$TASK_SH" || fail "маркер '$MARKER' не найден в $TASK_SH — писатель маркера потерян"
grep -qF "$MARKER" "$SCHEDULER" || fail "маркер '$MARKER' не найден в $SCHEDULER — читатель (worker_run_was_provider_refusal) потерял литерал писателя"

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
grep -qF "$MARKER" "$CALLS" \
  || fail "1) в комментарии нет машиночитаемого маркера провайдерного отказа — оркестратор не различит зелёный отказ доводки (ai-review #1313): $(head -8 "$CALLS")"
grep -q "telegram_report" "$CALLS" \
  || fail "1) Telegram-сигнал не отправлен — канал владельца обязан остаться, следы: $(head -5 "$CALLS")"
note "1) all_providers_exhausted -> rc=0, release-full + комментарий с маркером + Telegram — ок"

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
if grep -qF "$MARKER" "$CALLS"; then
  fail "4) маркер провайдерного отказа попал в комментарий НАШЕГО отказа — оркестратор принял бы его за провайдерный"
fi
note "4) prompt_too_long -> die (rc=7), без маркера, задача всё равно возвращена в пул — ок"

# ── 5) #1307: честная формулировка, когда повтор ИМЕЕТ смысл ─────────────────
: >"$CALLS"
WORKER_CHAIN_RETRY_USEFUL="1" run_block all_providers_exhausted || true
grep -qF "Повтор ИМЕЕТ смысл" "$CALLS" \
  || fail "5) при WORKER_CHAIN_RETRY_USEFUL=1 комментарий обязан называть, что повтор имеет смысл (#1307): $(head -8 "$CALLS")"
grep -q "lease_cli release-full 1286" "$CALLS" || fail "5) release-full не вызван"
note "5) #1307-формулировка доходит до комментария — ок"

# ── 6) неизвестный класс — громкий die, а не молчаливо зелёный дефолт ────────
# ЧЕСТНАЯ ГРАНИЦА исполняемости: сегодня ветка `*)` недостижима ИЗВНЕ блока —
# вход в блок охраняет внешний `if` по четырём именованным классам, и
# исполняемый сценарий с пятым классом просто не зашёл бы в блок (rc=0 мимо).
# Ветка нужна НА ЗАВТРА: пятый класс, добавленный во внешний `if` и забытый
# здесь, обязан умереть громко, а не пройти зелёным (находка ai-review PR
# #1313). Поэтому проверяется структурно — гвардией дрейфа якоря выше
# (`grep -q 'неизвестный класс отказа'`).
note "6) неизвестный класс — ветка *) громкая, проверена структурно (сегодня недостижима извне блока)"

echo "SMOKE(worker-exhaustion-exit-code): контракт кода возврата блока провайдерного отказа выполнен"
