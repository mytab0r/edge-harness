#!/usr/bin/env bash
# Гвардия класса «зависший вызов агента держит единственный слот воркера
# часами» (#1160/#1141, живые прогоны worker.yml 34757182001/34801868104,
# 2026-09-13/14 — оба съели ~5 часов каждый, отменены извне: рипером и
# 6-часовой стеной job'а). Проверяет DSH_CHAIN_TOTAL_BUDGET_SECS
# (dsh_run_with_provider_chain, scripts/lib/dsh-ci.sh) — суммарный wall-clock
# потолок на ВЕСЬ прогон цепочки, реализованный как min(DSH_TIMEOUT_SECS,
# остаток бюджета) на КАЖДУЮ попытку.
#
# Поведенческий, не структурный тест (AGENTS.md, класс #891/#93). Два слоя:
#
#   Сценарии 1-3 — НАСТОЯЩЕЕ время: настоящий coreutils `timeout`, настоящий
#   `sleep` внутри заглушки `dsh`, настоящее течение времени (`date +%s`).
#   Урезание доказано фактом убийства процесса, не значением переменной —
#   ценой нескольких секунд реального wall-clock.
#
#   Сценарии 4-8 — СИМУЛИРОВАННЫЕ часы (симуляция нужна, потому что
#   обратный прогон инцидентов — это тысячи симулированных секунд): `date`,
#   `sleep` и `dsh` — исполняемые заглушки над общим файлом часов. Часы —
#   единственная симуляция: код dsh-ci.sh (цикл цепочки, retry RATE_LIMIT,
#   срез таймаута, классификатор отказов) исполняется НАСТОЯЩИЙ, и
#   `timeout` — настоящий binary (не успевает сработать: заглушки выходят
#   мгновенно, длительность попытки задаётся сдвигом часов). Заглушка dsh
#   честно соблюдает тот же нож, что и прод: аппетит попытки обрезается до
#   min(аппетит, DSH_TIMEOUT_SECS) — ровно то, что реальному процессу даёт
#   `timeout`. Сценарии 6-7 — обратный прогон ОБОИХ инцидентов по
#   фактическим длительностям попыток из их логов (не по памяти: числа
#   сняты с «длилась Nс» прогонов 34757182001/34801868104), сценарий 8 —
#   мутационный критерий приёмки #1160 (все провайдеры дают ретраибельный
#   RATE_LIMIT — отказ обязан прийти от бюджета, не от конца списка).
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
  {"name":"TERTIARY","model_sha256":"$(hash_of tertiary-model)","confirmed_at":"2026-09-14","evidence":"smoke fixture (#1160)"},
  {"name":"GLM (симуляция инцидентов)","model_sha256":"$(hash_of glm-5.3-flash)","confirmed_at":"2026-09-14","evidence":"run 34757182001/34801868104 logs (#1160)"},
  {"name":"OpenRouter-2 (симуляция инцидентов)","model_sha256":"$(hash_of nemotron-3-super-120b)","confirmed_at":"2026-09-14","evidence":"run 34757182001/34801868104 logs (#1160)"},
  {"name":"Ollama-2 (симуляция инцидентов)","model_sha256":"$(hash_of nemotron-3-ultra)","confirmed_at":"2026-09-14","evidence":"run 34757182001/34801868104 logs (#1160)"},
  {"name":"Ollama-3 (симуляция инцидентов)","model_sha256":"$(hash_of nemotron-3-ultra-3)","confirmed_at":"2026-09-14","evidence":"run 34757182001/34801868104 logs (#1160); в проде id совпадал с Ollama-2, суффикс -3 здесь только для раздельных маркеров вызова"}
]
JSON
export DSH_CONFIRMED_MODELS_FILE="$CONFIRMED_MODELS_FIXTURE"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

export HOME="$(mktemp -d)"

# ── Заглушки сценариев 1-3: реальное время ──────────────────────────────────
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

# ── 1) Бюджет узкий (6с): PRIMARY реально спит 3с и падает (не наш таймаут) —
# тратит ~3с из 6с общего бюджета. SECONDARY должен получить УРЕЗАННЫЙ
# эффективный таймаут (~3с остатка, не номинальные DSH_TIMEOUT_SECS=100) — его
# заглушка пытается спать 15с (успела бы ответить успехом, если бы не обрезали)
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

# ── Заглушки сценариев 4-8: симулированные часы ─────────────────────────────
# Один общий файл часов $SIM_CLOCK: `date +%s` его читает, `sleep N` и
# заглушка `dsh` его сдвигают. SIM_DATE_TICK=1 (сценарий 5) заставляет часы
# тикать на 1с при КАЖДОМ вызове date — так воспроизводится граница бюджета
# ВНУТРИ одной итерации цикла (между верхней проверкой и точкой попытки время
# реально проходит: jq, гейты, патч профиля). Заглушка dsh исполняет Маркер
# вызова SIM_CALLED_<модель>, сдвиг часов на «аппетит» попытки (SIM_ADVANCE_
# <модель>, либо расписание SIM_SCHED_<модель> — файл с длительностью каждой
# попытки по строкам, для OpenRouter-2 с её разными по замеру попытками) и
# режим ответа SIM_MODE_<модель>: rate_limit (ретраибельный) или http_404
# (транспортный, автопереход). Аппетит обрезается разрешённым попытке
# таймаутом — ровно то, что реальному процессу делает `timeout`.
SIM_DIR="$WORK/simbin"
mkdir -p "$SIM_DIR"
cat >"$SIM_DIR/date" <<'DATESTUB'
#!/usr/bin/env bash
set -u
t=$(( "$(cat "$SIM_CLOCK")" + ${SIM_DATE_TICK:-0} ))
printf '%s\n' "$t" > "$SIM_CLOCK"
printf '%s\n' "$t"
DATESTUB
cat >"$SIM_DIR/sleep" <<'SLEEPSTUB'
#!/usr/bin/env bash
# «Сон» в симуляции — только сдвиг часов, реальной паузы нет.
set -u
t=$(( "$(cat "$SIM_CLOCK")" + $1 ))
printf '%s\n' "$t" > "$SIM_CLOCK"
SLEEPSTUB
cat >"$SIM_DIR/dsh" <<'SIMDSHSTUB'
#!/usr/bin/env bash
set -u
if [ "${1:-}" != "--profile" ]; then
  echo "::error::SMOKE(sim): dsh-заглушка не знает вызов: $*" >&2
  exit 99
fi
model_key="${DEEPSEEK_MODEL//[.-]/_}"
marker_var="SIM_CALLED_${model_key}"
: >"${!marker_var}"
# Аппетит попытки: расписание по строкам (разные попытки одного провайдера)
# либо одно число на все вызовы; обрезается разрешённым попытке таймаутом —
# так же, как реальный процесс обрезается настоящим `timeout`.
sched_var="SIM_SCHED_${model_key}"; sched_file="${!sched_var:-}"
adv_var="SIM_ADVANCE_${model_key}"
appetite="${!adv_var:-0}"
if [ -n "$sched_file" ] && [ -s "$sched_file" ]; then
  appetite="$(head -n1 "$sched_file")"
  tail -n +2 "$sched_file" > "$sched_file.tmp" && mv "$sched_file.tmp" "$sched_file"
fi
allowed="${DSH_TIMEOUT_SECS:-999999999}"
[ "$appetite" -gt "$allowed" ] && appetite="$allowed"
t=$(( "$(cat "$SIM_CLOCK")" + appetite ))
printf '%s\n' "$t" > "$SIM_CLOCK"
mode_var="SIM_MODE_${model_key}"
case "${!mode_var:-http_404}" in
  rate_limit)
    echo "dsh: RATE_LIMIT: 429 Too Many Requests (sim)" >&2 ;;
  *)
    echo "dsh: HTTP_404: sim transport failure" >&2 ;;
esac
exit 1
SIMDSHSTUB
chmod +x "$SIM_DIR/date" "$SIM_DIR/sleep" "$SIM_DIR/dsh"

# Цепочка с именами и порядком инцидентов 09-13/14 (логи прогонов
# 34757182001/34801868104): GLM → OpenRouter-2 → Ollama-2 → Ollama-3.
# id моделей сокращены до санитарных для имён env-переменных заглушки
# (прод-строки вида "nvidia/nemotron-3-super-120b-a12b:free" содержат "/" и
# ":"; на арифметику бюджета идёт НАЗВАНИЕ провайдера и ДЛИТЕЛЬНОСТЬ попытки,
# не строка id) — длительности попыток везде взяты из логов инцидентов.
SIM_KEY_GLM="glm-test-key"; SIM_KEY_OR2="or2-test-key"; SIM_KEY_OLL2="oll2-test-key"; SIM_KEY_OLL3="oll3-test-key"
export SIM_KEY_GLM SIM_KEY_OR2 SIM_KEY_OLL2 SIM_KEY_OLL3
SIM_CHAIN='[
  {"name":"GLM","base_url":"https://api.z.ai/api/coding/paas/v4","model":"glm-5.3-flash","secret_env":"SIM_KEY_GLM","max_output_tokens":4096},
  {"name":"OpenRouter-2","base_url":"https://openrouter.ai/api/v1","model":"nemotron-3-super-120b","secret_env":"SIM_KEY_OR2","max_output_tokens":4096},
  {"name":"Ollama-2","base_url":"https://ollama.com/v1","model":"nemotron-3-ultra","secret_env":"SIM_KEY_OLL2","max_output_tokens":4096},
  {"name":"Ollama-3","base_url":"https://ollama.com/v1","model":"nemotron-3-ultra-3","secret_env":"SIM_KEY_OLL3","max_output_tokens":4096}
]'

SIM_CLOCK="$WORK/sim-clock"
SIM_CALLED_GLM="$WORK/sim-called-glm"
SIM_CALLED_OR2="$WORK/sim-called-or2"
SIM_CALLED_OLL2="$WORK/sim-called-oll2"
SIM_CALLED_OLL3="$WORK/sim-called-oll3"
SIM_SCHED_OR2="$WORK/sim-sched-or2"
export SIM_CLOCK

reset_sim_scenario() {
  # Заглушка адресует переменные ИМЕНЕМ МОДЕЛИ (SIM_CALLED_<model_key>),
  # поэтому файлы-маркеры пробрасываются ей под этими именами.
  export SIM_CALLED_glm_5_3_flash="$SIM_CALLED_GLM"
  export SIM_CALLED_nemotron_3_super_120b="$SIM_CALLED_OR2"
  export SIM_CALLED_nemotron_3_ultra="$SIM_CALLED_OLL2"
  export SIM_CALLED_nemotron_3_ultra_3="$SIM_CALLED_OLL3"
  export SIM_SCHED_nemotron_3_super_120b="$SIM_SCHED_OR2"
  unset SIM_DATE_TICK SIM_ADVANCE_glm_5_3_flash SIM_ADVANCE_nemotron_3_super_120b \
        SIM_ADVANCE_nemotron_3_ultra SIM_ADVANCE_nemotron_3_ultra_3 \
        SIM_MODE_glm_5_3_flash SIM_MODE_nemotron_3_super_120b \
        SIM_MODE_nemotron_3_ultra SIM_MODE_nemotron_3_ultra_3 \
        DSH_CHAIN_TOTAL_BUDGET_SECS DSH_TIMEOUT_SECS \
        DSH_RATE_LIMIT_PROVIDER_CAP_SECS DSH_RATE_LIMIT_MAX_WAIT_SECS \
        DSH_RATE_LIMIT_INITIAL_DELAY_SECS DSH_RATE_LIMIT_MAX_DELAY_SECS 2>/dev/null || true
  printf '0\n' > "$SIM_CLOCK"
  rm -f "$SIM_CALLED_GLM" "$SIM_CALLED_OR2" "$SIM_CALLED_OLL2" "$SIM_CALLED_OLL3" "$SIM_SCHED_OR2" "$SIM_SCHED_OR2.tmp"
  : >"$ANSWER"; : >"$ERR"
}

run_sim() { # <файл лога> — прогон цепочки инцидентов с симулированными часами
  LOG="$1"
  local saved_path="$PATH"
  export PATH="$SIM_DIR:$PATH"
  DSH_PROVIDER_CHAIN="$SIM_CHAIN" dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  export PATH="$saved_path"
}

# ── 4) Дефолтное значение бюджета = 9000с — прод-значение обязано быть
# видно в поведении, а не только в тексте комментария (находка ai-review
# PR #1247: все явные сценарии передавали бюджет переменной, и мутация
# «дефолт 9000 → 999999» оставалась зелёной). Ни DSH_CHAIN_TOTAL_BUDGET_SECS,
# ни DSH_TIMEOUT_SECS здесь НЕ заданы — как в проде worker.yml.
# GLM съедает полный дефолтный таймаут попытки (3600с), Ollama-2 — урезанный
# остаток (1800с, сообщение «урезан до 1800с»), после 9000с симулированного
# времени Ollama-3 обязан быть НЕ ТРОНУТ с причиной chain_budget_exhausted.
reset_sim_scenario
export SIM_ADVANCE_glm_5_3_flash=3600
export SIM_ADVANCE_nemotron_3_super_120b=999999
export SIM_ADVANCE_nemotron_3_ultra=999999
LOG="$WORK/log4.txt"
run_sim "$LOG"
OUT="$(cat "$LOG")"
[ -f "$SIM_CALLED_GLM" ] || fail "4) GLM обязан быть вызван: $OUT"
[ -f "$SIM_CALLED_OLL2" ] || fail "4) Ollama-2 обязан быть вызван (остаток бюджета ещё был): $OUT"
[ ! -f "$SIM_CALLED_OLL3" ] || fail "4) Ollama-3 НЕ обязан быть вызван — дефолтный бюджет (9000с) исчерпан ДО него"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "4) ожидался chain_budget_exhausted при дефолтном бюджете, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"суммарный бюджет (9000с/#1160) исчерпан после 9000с и 3 из 4 провайдеров"* ]] \
  || fail "4) сообщение обязано назвать ДЕФОЛТНЫЕ 9000с (прод-значение) и 3 из 4 опробованных: $OUT"
[[ "$OUT" == *"таймаут попытки урезан до 1800с остатком общего бюджета цепочки (#1160)"* ]] \
  || fail "4) Ollama-2 обязан получить таймаут, урезанный до остатка 1800с: $OUT"
echo "SMOKE(chain-budget): 4) дефолтный бюджет 9000с виден в поведении без переменных окружения — ок"

# ── 5) Граница бюджета ВНУТРИ итерации: между верхней проверкой цикла и
# точкой старта попытки остаток уходит в 0 (находка ai-review PR #1247 —
# прежний код клампил остаток в 0 и стартовал попытку с `timeout 0`, что у
# coreutils означает «без ножа вовсе»). Симуляция: часы тикают на 1с при
# каждом вызове date, бюджет 2с → верхняя проверка видит остаток 1с,
# точка попытки — 0с. Попытка ОБЯЗАНА не стартовать (fail loud), маркер
# вызова — отсутствовать.
reset_sim_scenario
export SIM_DATE_TICK=1
export DSH_CHAIN_TOTAL_BUDGET_SECS=2
export DSH_TIMEOUT_SECS=100
LOG="$WORK/log5.txt"
run_sim "$LOG"
OUT="$(cat "$LOG")"
[ ! -f "$SIM_CALLED_GLM" ] || fail "5) GLM НЕ обязан быть вызван — остаток бюджета ушёл в 0 на границе попытки, timeout 0 означал бы запуск без ножа: $OUT"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "5) ожидался chain_budget_exhausted на границе попытки, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"исчерпан на границе попытки GLM — прошло 2с, опробовано 0 из 4"* ]] \
  || fail "5) сообщение обязано честно назвать границу попытки, 2с и 0 из 4: $OUT"
[[ "$OUT" == *"timeout 0 означал бы запуск без ножа"* ]] \
  || fail "5) сообщение обязано объяснить, почему попытка не стартует (timeout 0 = без ножа): $OUT"
echo "SMOKE(chain-budget): 5) нулевой остаток на границе попытки — fail loud без запуска без ножа — ок"

# ── 6) Обратный прогон инцидента 1: run 34757182001 (2026-09-13,
# 12:28:27Z → 17:31:08Z, окно 18161с ≈ 303 мин, отменён внешне). Фактические
# длительности попыток из его лога («длилась Nс»): пул 3с (до цепочки), GLM
# 7200с (наш таймаут), OpenRouter-2 1972с (10 попыток по ~17с + паузы 30..300,
# rate_limit_retry_budget_exceeded), Ollama-2 7200с (наш таймаут), Ollama-3 —
# начался и был отменён внешне, длительности не имеет. Часы симулированы,
# весь остальной код (цикл, retry RATE_LIMIT, срез таймаута, классификатор)
# — настоящий. Дефолты сегодня: бюджет 9000с, потолок ожидания на провайдера
# 300с (#1121 — ПОЗЖЕ инцидента, у него OpenRouter-2 выжег все 1800с; поэтому
# симулированный OpenRouter-2 сгорает за ~385с, а не 1972с — честная
# оговорка, на исход не влияет: остаток бюджета съедает Ollama-2).
reset_sim_scenario
export SIM_ADVANCE_glm_5_3_flash=7200
export SIM_MODE_nemotron_3_super_120b=rate_limit
export SIM_ADVANCE_nemotron_3_super_120b=17
export SIM_ADVANCE_nemotron_3_ultra=7200
export DSH_TIMEOUT_SECS=7200
LOG="$WORK/log6.txt"
run_sim "$LOG"
OUT="$(cat "$LOG")"
[ -f "$SIM_CALLED_GLM" ] || fail "6) GLM обязан быть вызван (инцидент: 7200с): $OUT"
[ -f "$SIM_CALLED_OR2" ] || fail "6) OpenRouter-2 обязан быть вызван (инцидент: 1972с): $OUT"
[ -f "$SIM_CALLED_OLL2" ] || fail "6) Ollama-2 обязан быть вызван (инцидент: 7200с): $OUT"
[ ! -f "$SIM_CALLED_OLL3" ] || fail "6) Ollama-3 НЕ обязан быть вызван — бюджет обязан остановить цепочку ДО него: $OUT"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "6) ожидался chain_budget_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"суммарный бюджет (9000с/#1160) исчерпан после 9000с и 3 из 4 провайдеров (опробованы: GLM, OpenRouter-2, Ollama-2)"* ]] \
  || fail "6) обратный прогон инцидента 1 обязан остановиться ровно на 9000с с тремя опробованными: $OUT"
[[ "$OUT" == *"таймаут попытки урезан до 1800с остатком общего бюджета цепочки (#1160)"* ]] \
  || fail "6) OpenRouter-2 обязан получить урезанный до остатка (1800с) таймаут: $OUT"
[[ "$OUT" == *"таймаут попытки урезан до 1415с остатком общего бюджета цепочки (#1160)"* ]] \
  || fail "6) Ollama-2 обязан получить урезанный до остатка (1415с) таймаут — инцидент дал бы ему полные 7200с: $OUT"
echo "SMOKE(chain-budget): 6) обратный прогон инцидента 34757182001: цепочка остановлена бюджетом на 9000с (150 мин) вместо наблюдаемых 18161с (~303 мин), Ollama-3 не тронут — ок"

# ── 7) Обратный прогон инцидента 2: run 34801868104 (2026-09-14,
# 03:13:39Z → 08:11:23Z, окно 17864с ≈ 298 мин, отменён внешне по 6-часовой
# стене job'а). Фактические длительности попыток из его лога: GLM 7200с (наш
# таймаут), OpenRouter-2 899с (5 попыток: 532/16/17/17/17с + паузы 30/60/120/90,
# потолок 300с уже действовал), Ollama-2 6047с (собственный rc=1, «stderr
# пуст»), Ollama-3 — начался и был отменён внешне. Расписание попыток
# OpenRouter-2 — построчно, из лога.
reset_sim_scenario
export SIM_ADVANCE_glm_5_3_flash=7200
export SIM_MODE_nemotron_3_super_120b=rate_limit
printf '532\n16\n17\n17\n17\n' > "$SIM_SCHED_OR2"
export SIM_ADVANCE_nemotron_3_ultra=6047
export DSH_TIMEOUT_SECS=7200
LOG="$WORK/log7.txt"
run_sim "$LOG"
OUT="$(cat "$LOG")"
[ -f "$SIM_CALLED_GLM" ] || fail "7) GLM обязан быть вызван (инцидент: 7200с): $OUT"
[ -f "$SIM_CALLED_OR2" ] || fail "7) OpenRouter-2 обязан быть вызван (инцидент: 899с, 5 попыток): $OUT"
[ -f "$SIM_CALLED_OLL2" ] || fail "7) Ollama-2 обязан быть вызван (инцидент: 6047с до собственного rc=1): $OUT"
[ ! -f "$SIM_CALLED_OLL3" ] || fail "7) Ollama-3 НЕ обязан быть вызван — бюджет обязан остановить цепочку ДО него: $OUT"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "7) ожидался chain_budget_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"суммарный бюджет (9000с/#1160) исчерпан после 9000с и 3 из 4 провайдеров (опробованы: GLM, OpenRouter-2, Ollama-2)"* ]] \
  || fail "7) обратный прогон инцидента 2 обязан остановиться ровно на 9000с с тремя опробованными: $OUT"
[[ "$OUT" == *"таймаут попытки урезан до 901с остатком общего бюджета цепочки (#1160)"* ]] \
  || fail "7) Ollama-2 обязан получить урезанный до остатка (901с) таймаут — в инциденте он сам шёл 6047с: $OUT"
echo "SMOKE(chain-budget): 7) обратный прогон инцидента 34801868104: цепочка остановлена бюджетом на 9000с (150 мин) вместо наблюдаемых 17864с (~298 мин), Ollama-3 не тронут — ок"

# ── 8) Критерий приёмки #1160 (мутационный): ВСЕ провайдеры дают
# ретраибельный RATE_LIMIT — прогон обязан завершиться отказом НА БЮДЖЕТЕ
# (chain_budget_exhausted), а не пройти все записи молча до конца списка
# (all_providers_exhausted). Бюджет 4с, потолок ожидания на провайдера 2с:
# GLM и OpenRouter-2 выжигают свою долю ретраями RATE_LIMIT, Ollama-2 обязан
# НЕ быть вызван.
reset_sim_scenario
export SIM_MODE_glm_5_3_flash=rate_limit
export SIM_MODE_nemotron_3_super_120b=rate_limit
export SIM_MODE_nemotron_3_ultra=rate_limit
export DSH_CHAIN_TOTAL_BUDGET_SECS=4
export DSH_TIMEOUT_SECS=100
export DSH_RATE_LIMIT_PROVIDER_CAP_SECS=2
export DSH_RATE_LIMIT_INITIAL_DELAY_SECS=1
export DSH_RATE_LIMIT_MAX_DELAY_SECS=1
export DSH_RATE_LIMIT_MAX_WAIT_SECS=1800
LOG="$WORK/log8.txt"
run_sim "$LOG"
OUT="$(cat "$LOG")"
[ -f "$SIM_CALLED_GLM" ] || fail "8) GLM обязан быть вызван: $OUT"
[ -f "$SIM_CALLED_OR2" ] || fail "8) OpenRouter-2 обязан быть вызван (его доля бюджета ещё не сгорела): $OUT"
[ ! -f "$SIM_CALLED_OLL2" ] || fail "8) Ollama-2 НЕ обязан быть вызван — бюджет (4с) исчерпан ДО него: $OUT"
[ "$DSH_RUN_FAILURE_REASON" = "chain_budget_exhausted" ] \
  || fail "8) отказ обязан прийти от БЮДЖЕТА (chain_budget_exhausted), не от конца списка (all_providers_exhausted): получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"суммарный бюджет (4с/#1160) исчерпан после 4с и 2 из 4 провайдеров (опробованы: GLM, OpenRouter-2)"* ]] \
  || fail "8) сообщение обязано назвать бюджет 4с и 2 из 4 опробованных: $OUT"
echo "SMOKE(chain-budget): 8) сплошной ретраибельный RATE_LIMIT: отказ приходит от бюджета, а не молчаливым проходом всего списка — ок"

echo "SMOKE(chain-budget): все сценарии суммарного бюджета цепочки целы — гвардия класса #1160/#1141 зелёная"
