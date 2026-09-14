#!/usr/bin/env bash
# Гвардия класса «зависший вызов агента держит единственный слот воркера
# часами» (#1160/#1141, живые прогоны worker.yml 34757182001/34801868104,
# 2026-09-13/14 — оба съели ~5 часов каждый, отменены внешним рипером
# WORKER_STALL_MINUTES=295). Проверяет DSH_CHAIN_TOTAL_BUDGET_SECS
# (dsh_run_with_provider_chain, scripts/lib/dsh-ci.sh) — суммарный wall-clock
# потолок на ВЕСЬ прогон цепочки, реализованный как min(DSH_TIMEOUT_SECS,
# остаток бюджета) на КАЖДУЮ попытку.
#
# Поведенческий, не структурный тест (AGENTS.md, класс #891/#893): в отличие
# от соседнего dsh-provider-chain.smoke.sh, здесь `timeout` и `sleep` НЕ
# заглушены — используется настоящий coreutils `timeout`, настоящий `sleep`
# внутри заглушки `dsh`, и настоящее течение времени между итерациями цикла
# (`date +%s`). Тест доказывает, что бюджет реально ОБРЕЗАЕТ процесс
# (не просто меняет число в переменной), ценой нескольких секунд реального
# wall-clock времени — приемлемо для этого одного файла.
#
# Запуск: bash scripts/lib/test/dsh-chain-budget.smoke.sh  (jq обязателен)
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::SMOKE(chain-budget): $*" >&2; exit 1; }

WORK="$(mktemp -d)"
hash_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
CONFIRMED_MODELS_FIXTURE="$WORK/confirmed-models.json"
cat >"$CONFIRMED_MODELS_FIXTURE" <<JSON
[
  {"name":"PRIMARY","model_sha256":"$(hash_of primary-model)","confirmed_at":"2026-09-14","evidence":"smoke fixture (#1160)"},
  {"name":"SECONDARY","model_sha256":"$(hash_of secondary-model)","confirmed_at":"2026-09-14","evidence":"smoke fixture (#1160)"},
  {"name":"TERTIARY","model_sha256":"$(hash_of tertiary-model)","confirmed_at":"2026-09-14","evidence":"smoke fixture (#1160)"}
]
JSON
export DSH_CONFIRMED_MODELS_FILE="$CONFIRMED_MODELS_FIXTURE"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

export HOME="$(mktemp -d)"

# dsh — единственная внешняя команда, которую видит dsh_run_with_retry.
# ВАЖНО: здесь `timeout` НЕ заглушен (в отличие от соседнего
# dsh-provider-chain.smoke.sh) — используется настоящий coreutils `timeout`,
# который вызывает цель через execvp(), а НЕ через bash — экспортированная
# bash-функция `dsh` (`export -f`) в этом случае невидима реальному
# `timeout`, он находит и запускает НАСТОЯЩИЙ dsh с PATH. Поэтому заглушка —
# РЕАЛЬНЫЙ исполняемый скрипт в отдельном каталоге PATH, найденный ПЕРЕД
# настоящим dsh. Режим каждой модели читается из SMOKE_SLEEP_<MODEL> (сколько
# РЕАЛЬНО спать, секунд) + SMOKE_RC_<MODEL> (что вернуть, если успевает
# доспать) + SMOKE_CALLED_<MODEL> — маркер-файл, доказывающий факт вызова
# (или его отсутствие — для TERTIARY, которого бюджет обязан не дать вызвать).
BIN_DIR="$WORK/bin"
mkdir -p "$BIN_DIR"
cat >"$BIN_DIR/dsh" <<'DSHSTUB'
#!/usr/bin/env bash
set -u
if [ "${1:-}" != "--profile" ]; then
  echo "::error::SMOKE: dsh-заглушка не знает вызов: $*" >&2
  exit 99
fi
model_key="${DEEPSEEK_MODEL//-/_}"
marker_var="SMOKE_CALLED_${model_key}"
: >"${!marker_var}"
sleep_var="SMOKE_SLEEP_${model_key}"
rc_var="SMOKE_RC_${model_key}"
sleep "${!sleep_var:-0}"
rc="${!rc_var:-1}"
if [ "$rc" = "0" ]; then
  echo "smoke: ответ от $DEEPSEEK_MODEL"
  exit 0
fi
echo "dsh: SMOKE_ERROR: провайдер $DEEPSEEK_MODEL ответил ошибкой (не RATE_LIMIT — автопереход по #1084)" >&2
exit 1
DSHSTUB
chmod +x "$BIN_DIR/dsh"
export PATH="$BIN_DIR:$PATH"

export PRIMARY_KEY="primary-test-key"
export SECONDARY_KEY="secondary-test-key"
export TERTIARY_KEY="tertiary-test-key"
CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096},
  {"name":"TERTIARY","base_url":"https://tertiary.test/v1","model":"tertiary-model","secret_env":"TERTIARY_KEY","max_output_tokens":4096}
]'
export DSH_PROVIDER_CHAIN="$CHAIN"

ANSWER="$WORK/answer.txt"
ERR="$WORK/err.txt"

reset_scenario() {
  unset SMOKE_SLEEP_primary_model SMOKE_SLEEP_secondary_model SMOKE_SLEEP_tertiary_model 2>/dev/null || true
  unset SMOKE_RC_primary_model SMOKE_RC_secondary_model SMOKE_RC_tertiary_model 2>/dev/null || true
  export SMOKE_CALLED_primary_model="$WORK/called-primary"
  export SMOKE_CALLED_secondary_model="$WORK/called-secondary"
  export SMOKE_CALLED_tertiary_model="$WORK/called-tertiary"
  rm -f "$SMOKE_CALLED_primary_model" "$SMOKE_CALLED_secondary_model" "$SMOKE_CALLED_tertiary_model"
  : >"$ANSWER"; : >"$ERR"
}

dsh_require_provider_chain || fail "dsh_require_provider_chain отказал на валидной цепочке"

# ── 1) Бюджет узкий (3с): PRIMARY спит 2с и падает (не наш таймаут) —
# тратит ~2с из 3с общего бюджета. SECONDARY должен получить УРЕЗАННЫЙ
# эффективный таймаут (~1с, не номинальные DSH_TIMEOUT_SECS=100) — его
# заглушка пытается спать 5с (успела бы ответить успехом, если бы не обрезали)
# — настоящий `timeout` обязан убить её РАНЬШЕ, чем она доспит, доказывая,
# что урезание — не бухгалтерская переменная, а реальный процесс-киллер.
# TERTIARY НЕ должен быть вызван вовсе — бюджет к этому моменту исчерпан.
reset_scenario
export SMOKE_SLEEP_primary_model=3
export SMOKE_RC_primary_model=1
export SMOKE_SLEEP_secondary_model=15
export SMOKE_RC_secondary_model=0
LOG="$WORK/log1.txt"
t0=$(date +%s)
DSH_TIMEOUT_SECS=100 DSH_CHAIN_TOTAL_BUDGET_SECS=6 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
t1=$(date +%s)
OUT="$(cat "$LOG")"
elapsed=$((t1 - t0))
[ -f "$SMOKE_CALLED_primary_model" ] || fail "1) PRIMARY обязан быть вызван"
[ -f "$SMOKE_CALLED_secondary_model" ] || fail "1) SECONDARY обязан быть вызван (бюджет ещё не исчерпан на старте)"
[ ! -f "$SMOKE_CALLED_tertiary_model" ] || fail "1) TERTIARY НЕ обязан быть вызван — общий бюджет цепочки (6с) исчерпан ДО него"
[ "$DSH_RUN_RC" != "0" ] || fail "1) SECONDARY должен быть убит урезанным таймаутом, не ответить успехом (rc=0 говорит, что урезание не сработало и \`timeout\` не убил процесс раньше 15с сна)"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "1) ожидался chain_budget_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"таймаут попытки урезан"* ]] \
  || fail "1) сообщение обязано честно назвать факт урезания таймаута попытки (#1160): $OUT"
[[ "$OUT" == *"суммарный бюджет (6с/#1160) исчерпан"* ]] \
  || fail "1) сообщение обязано назвать исчерпание общего бюджета цепочки: $OUT"
[[ "$OUT" == *"PRIMARY, SECONDARY"* ]] || fail "1) DSH_CHAIN_TRIED обязан назвать PRIMARY и SECONDARY (не TERTIARY): $OUT"
# Реальное wall-clock время прогона обязано остаться в разумных пределах —
# если бы урезание не сработало и SECONDARY доспал все 15с, elapsed был бы
# ≥18с; если бы `timeout` не убивал процесс вовсе, elapsed был бы ≥18с тоже.
# Потолок 12с даёт большой запас на оверхед bash/jq/date, но однозначно
# отличает «SECONDARY убит рано» от «SECONDARY доспал».
[ "$elapsed" -lt 12 ] \
  || fail "1) реальное время прогона ${elapsed}с — SECONDARY не был убит вовремя (ожидалось <12с, урезание не сработало на деле, не только в переменной)"
echo "SMOKE(chain-budget): 1) узкий общий бюджет реально обрезает попытку резервного провайдера настоящим timeout'ом и останавливает цепочку ДО следующего — ок (elapsed=${elapsed}с)"

# ── 2) Просторный бюджет (обычный прогон, ничего не меняется): PRIMARY
# отвечает успехом быстро — сообщение НЕ содержит «урезан», регрессия на
# здоровый путь исключена.
reset_scenario
export SMOKE_SLEEP_primary_model=0
export SMOKE_RC_primary_model=0
LOG="$WORK/log2.txt"
DSH_TIMEOUT_SECS=100 DSH_CHAIN_TOTAL_BUDGET_SECS=9000 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "2) ожидался успех PRIMARY при просторном бюджете, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "2) ожидался PRIMARY, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" != *"урезан"* ]] || fail "2) просторный бюджет НЕ должен урезать таймаут первой попытки: $OUT"
echo "SMOKE(chain-budget): 2) просторный бюджет не меняет поведение здорового прогона — ок"

# ── 3) Бюджет исчерпан ЕЩЁ ДО первой попытки (0с) — цепочка обязана
# завершиться chain_budget_exhausted, не вызвав вообще ни одного провайдера.
reset_scenario
LOG="$WORK/log3.txt"
DSH_TIMEOUT_SECS=100 DSH_CHAIN_TOTAL_BUDGET_SECS=0 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ ! -f "$SMOKE_CALLED_primary_model" ] || fail "3) PRIMARY НЕ обязан быть вызван — бюджет 0с исчерпан на старте"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "3) ожидался chain_budget_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[ -z "$DSH_CHAIN_TRIED" ] || fail "3) DSH_CHAIN_TRIED обязан остаться пустым — ни один провайдер не пробован: '$DSH_CHAIN_TRIED'"
[[ "$OUT" == *"суммарный бюджет (0с/#1160) исчерпан после 0с и 0 из 3 провайдеров"* ]] \
  || fail "3) сообщение обязано честно назвать 0 из 3 опробованных провайдеров: $OUT"
echo "SMOKE(chain-budget): 3) бюджет, исчерпанный до первой попытки, останавливает цепочку немедленно, честно называя 0 опробованных — ок"

echo "SMOKE(chain-budget): все сценарии суммарного бюджета цепочки целы — гвардия класса #1160/#1141 зелёная"
