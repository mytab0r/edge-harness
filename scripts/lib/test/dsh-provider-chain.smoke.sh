#!/usr/bin/env bash
# Гвардия класса «цепочка провайдеров переключается по классу отказа, а не
# по любой ошибке» (#727). Прогоняет dsh_run_with_provider_chain (lib/
# dsh-ci.sh) ЦЕЛИКОМ — не bash -n, настоящее исполнение с заглушкой `dsh`,
# роутящей ответ по модели (DEEPSEEK_MODEL, который chain выставляет на
# каждую попытку) — тот же приём, что dsh-clients.smoke.sh уже применяет для
# RATE_LIMIT (SMOKE_RATE_LIMIT_MODE), здесь на два провайдера сразу.
#
# Прод-форма отказов — дословно:
#   RATE_LIMIT: Weekly/Monthly Limit Exhausted... run 34176910458
#     (docs/runbooks/switch-llm-provider.md, ai-review PR #206 разбор)
#   HTTP_404: modelCode does not exist... прогон 33572445063 (PR #190,
#     scripts/lib/test/dsh-clients.smoke.sh:217, ai_review.py::test_ai_review)
#
# Запуск: bash scripts/lib/test/dsh-provider-chain.smoke.sh  (jq обязателен)
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::SMOKE(chain): $*" >&2; exit 1; }

# Реестр подтверждённых id (#737) — фикстура ДО source dsh-ci.sh, т.к.
# DSH_CONFIRMED_MODELS_FILE резолвится на дефолт при загрузке файла:
# реальный реестр (scripts/lib/confirmed-provider-models.json) не знает про
# фиктивные модели этого смоука по построению (см. allowlist ниже про
# фиктивные имена).
WORK="$(mktemp -d)"
hash_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
CONFIRMED_MODELS_FIXTURE="$WORK/confirmed-models.json"
cat >"$CONFIRMED_MODELS_FIXTURE" <<JSON
[
  {"name":"PRIMARY","model_sha256":"$(hash_of primary-model)","confirmed_at":"2026-09-08","evidence":"smoke fixture"},
  {"name":"SECONDARY","model_sha256":"$(hash_of secondary-model)","confirmed_at":"2026-09-08","evidence":"smoke fixture"}
]
JSON
export DSH_CONFIRMED_MODELS_FILE="$CONFIRMED_MODELS_FIXTURE"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

# ── Заглушки: dsh_install/dsh_patch_profile не нужны сети, но профиль пишется
# на диск (HOME) — используем временный HOME, чтобы не мусорить и не зависеть
# от состояния хоста. dsh() — единственная внешняя команда, которую видит
# dsh_run_with_retry; timeout() пропускает вызов насквозь без реального sleep.
export HOME="$(mktemp -d)"
timeout() { shift; "$@"; }
sleep() { :; }
export -f timeout sleep

# Заглушка dsh: маршрут по DEEPSEEK_MODEL (chain выставляет его на каждую
# попытку ПЕРЕД вызовом dsh_patch_profile/dsh_run_with_retry — см.
# dsh_run_with_provider_chain) — режим каждой модели читается из отдельной
# переменной окружения (SMOKE_MODE_<MODEL>), не общего счётчика попыток:
# сценарии этого файла не полагаются на порядок вызовов внутри ретрая
# dsh_run_with_retry (короткий RATE_LIMIT здесь не участвует — это уже
# доказано dsh-clients.smoke.sh, здесь предмет — переход МЕЖДУ провайдерами).
dsh() {
  case "${1:-}" in
    --profile)
      local mode_var="SMOKE_MODE_${DEEPSEEK_MODEL//-/_}"
      local mode="${!mode_var:-ok}"
      case "$mode" in
        ok)
          echo "smoke: ответ от $DEEPSEEK_MODEL"
          return 0 ;;
        quota)
          echo "dsh: RATE_LIMIT: Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-09-10 08:51:55" >&2
          return 1 ;;
        http404)
          # Прод-форма прогона 33572445063 (PR #190) — не пересказ.
          echo "dsh: HTTP_404: modelCode does not exist" >&2
          return 1 ;;
        real-error)
          echo "dsh: INVALID_API_KEY: unauthorized" >&2
          return 1 ;;
        leaky-error)
          # #743, живая утечка: начало ответа 401 у провайдера типично несёт
          # эхо заголовка Authorization — прод-форма, не выдумка (см.
          # docs/runbooks/switch-llm-provider.md). Маркер синтетический
          # (SMOKE-...), но лежит в позиции, где реальный производный
          # DEEPSEEK_API_KEY оказался бы у настоящего провайдера.
          echo "dsh: INVALID_API_KEY: unauthorized — received header Authorization: Bearer sk-SMOKE1EEDEDBEEFCAFEBABE1234567890abcdef" >&2
          return 1 ;;
        silent)
          # #737, живой случай — прогон 34188152283: rc=1, НИ ОДНОГО байта в
          # stderr (не пересказ — воспроизводит ровно тот факт).
          return 1 ;;
        *)
          echo "::error::SMOKE: неизвестный режим $mode для $mode_var" >&2
          return 99 ;;
      esac ;;
    *)
      echo "::error::SMOKE: dsh-заглушка не знает вызов: $*" >&2
      return 99 ;;
  esac
}
export -f dsh

# Два фиктивных провайдера — имена/URL НЕ совпадают с реальными (api.z.ai,
# integrate.api.nvidia.com, glm-*, nemotron*): гвардия класса #153
# (provider-default.guard.sh) сканирует scripts/** на литералы прежних
# дефолтов, реальные значения живут в vars.DSH_PROVIDER_CHAIN репозитория,
# не здесь.
export PRIMARY_KEY="primary-test-key"
export SECONDARY_KEY="secondary-test-key"
CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096}
]'
export DSH_PROVIDER_CHAIN="$CHAIN"

ANSWER="$WORK/answer.txt"
ERR="$WORK/err.txt"

reset_scenario() {
  unset SMOKE_MODE_primary_model SMOKE_MODE_secondary_model 2>/dev/null || true
  : >"$ANSWER"; : >"$ERR"
}

dsh_require_provider_chain || fail "dsh_require_provider_chain отказал на валидной цепочке"

# ── 1) quota_exhausted у первого — автопереход на второго, который отвечает ──
reset_scenario
SMOKE_MODE_primary_model="quota"
SMOKE_MODE_secondary_model="ok"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke"
[ "$DSH_RUN_RC" = "0" ] || fail "1) ожидался успех (rc=0), получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "1) ожидался переход на SECONDARY, получено '$DSH_CHAIN_PROVIDER'"
grep -q "ответ от secondary-model" "$ANSWER" || fail "1) answer.txt не от второго провайдера"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "1) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — оба провайдера обязаны быть опробованы по порядку"
[[ "$DSH_CHAIN_RESET_HINT" == *"PRIMARY: 2026-09-10 08:51:55"* ]] || fail "1) дата сброса PRIMARY потеряна: '$DSH_CHAIN_RESET_HINT'"
echo "SMOKE(chain): 1) quota_exhausted -> автопереход — ок"

# ── 2) HTTP_404 (транспорт, без единой строки RATE_LIMIT) — тоже автопереход ──
reset_scenario
SMOKE_MODE_primary_model="http404"
SMOKE_MODE_secondary_model="ok"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke"
[ "$DSH_RUN_RC" = "0" ] || fail "2) ожидался успех после HTTP_404 у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "2) ожидался переход на SECONDARY при HTTP_404, получено '$DSH_CHAIN_PROVIDER'"
echo "SMOKE(chain): 2) HTTP_404 -> автопереход — ок"

# ── 3) Настоящая ошибка (ключ битый и т.п.) — цепочка НЕ идёт дальше ──────────
# Мутация ключевого требования (#727, п.4): не любой отказ переключает.
# #737: сообщение обязано называть код возврата даже на остановке (Дефект 1).
# ВАЖНО: вывод перехватывается редиректом в файл, НЕ через $(...) вокруг
# самого вызова — command substitution породила бы subshell, и
# DSH_RUN_RC/DSH_CHAIN_* из dsh_run_with_provider_chain потерялись бы
# (живая находка мутационной проверки этого же PR).
reset_scenario
SMOKE_MODE_primary_model="real-error"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" != "0" ] || fail "3) настоящая ошибка не должна была дать успех"
[ "$DSH_CHAIN_PROVIDER" = "" ] || fail "3) успешного провайдера быть не должно, получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || fail "3) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — SECONDARY не должен был тронуться (не переключаемый класс)"
[ "$DSH_RUN_FAILURE_REASON" != "all_providers_exhausted" ] || fail "3) причина не обязана звучать как 'все исчерпаны' — это НЕ переключаемый класс"
[[ "$OUT" == *"rc="* ]] || fail "3) сообщение об остановке обязано называть код возврата: $OUT"
echo "SMOKE(chain): 3) настоящая ошибка -> цепочка остановлена, второй провайдер не тронут, сообщение честное — ок"

# ── 4) Оба провайдера исчерпаны — честное 'все исчерпаны' + обе даты сброса ──
reset_scenario
SMOKE_MODE_primary_model="quota"
SMOKE_MODE_secondary_model="quota"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke"
[ "$DSH_RUN_RC" != "0" ] || fail "4) оба исчерпаны — успеха быть не должно"
[ "$DSH_RUN_FAILURE_REASON" = "all_providers_exhausted" ] || fail "4) ожидался all_providers_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "4) оба провайдера обязаны быть опробованы"
[[ "$DSH_CHAIN_RESET_HINT" == *"PRIMARY: 2026-09-10 08:51:55"* && "$DSH_CHAIN_RESET_HINT" == *"SECONDARY: 2026-09-10 08:51:55"* ]] \
  || fail "4) обе даты сброса обязаны попасть в подсказку: '$DSH_CHAIN_RESET_HINT'"
echo "SMOKE(chain): 4) все провайдеры исчерпаны — честное сообщение с датами — ок"

# ── 5) Секрет второго провайдера не передан этим workflow — пропуск, не провал ─
reset_scenario
SMOKE_MODE_primary_model="quota"
SMOKE_MODE_secondary_model="ok"
( unset SECONDARY_KEY
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke"
  [ "$DSH_RUN_RC" != "0" ] || { echo "::error::5) без секрета SECONDARY успеха быть не должно" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || { echo "::error::5) SECONDARY обязан быть учтён как опробованный (пропущен, не потерян)" >&2; exit 1; }
) || fail "5) сценарий с отсутствующим секретом провалился"
echo "SMOKE(chain): 5) пропуск провайдера без секрета — ок"

# ── 6) rc=1, ПУСТОЙ stderr (#737, живой случай — прогон 34188152283: NVIDIA
# rc=1, ни одного байта в stderr) — недиагностируемый транспортный сбой по
# умолчанию ПЕРЕКЛЮЧАЕТ (Дефект 2), сообщение обязано называть rc и факт
# «stderr пуст» (Дефект 1), не утверждать распознанный класс, которого нет.
reset_scenario
SMOKE_MODE_primary_model="silent"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "6) ожидался успех после пустого stderr у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "6) ожидался переход на SECONDARY при пустом stderr, получено '$DSH_CHAIN_PROVIDER'"
[ ! -s "$ERR" ] || fail "6) err.txt последней (успешной) попытки не должен быть пуст — фикстура сломана"
[[ "$OUT" == *"rc="* ]] || fail "6) сообщение обязано называть код возврата: $OUT"
[[ "$OUT" == *"stderr пуст"* ]] || fail "6) сообщение обязано называть факт «stderr пуст»: $OUT"
echo "SMOKE(chain): 6) rc=1 без диагностики -> автопереход с честным сообщением — ок"

# ── 7) Неподтверждённый id модели (#737, руnbook «Узнать точный id модели») —
# провайдер пропущен ДО вызова dsh (не тратит попытку/время), следующий
# пробуется как обычно, сообщение называет факт «id не подтверждён».
reset_scenario
UNCONFIRMED_CHAIN='[
  {"name":"UNCONFIRMED","base_url":"https://unconfirmed.test/v1","model":"unconfirmed-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096}
]'
( export DSH_PROVIDER_CHAIN="$UNCONFIRMED_CHAIN"
  LOG="$WORK/log7.txt"
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::7) ожидался успех после пропуска неподтверждённого id, получено $DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || { echo "::error::7) ожидался переход на SECONDARY, получено '$DSH_CHAIN_PROVIDER'" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "UNCONFIRMED, SECONDARY" ] || { echo "::error::7) UNCONFIRMED обязан быть учтён как опробованный (пропущен, не потерян): '$DSH_CHAIN_TRIED'" >&2; exit 1; }
  [[ "$OUT" == *"НЕ подтверждён"* ]] || { echo "::error::7) сообщение обязано называть факт «id не подтверждён»: $OUT" >&2; exit 1; }
) || fail "7) сценарий с неподтверждённым id модели провалился"
echo "SMOKE(chain): 7) неподтверждённый id модели -> пропуск с честным сообщением, время/квота не потрачены — ок"

# ── 8) Класс #743: стоп-класс кладёт сырой stderr в DSH_CHAIN_CLASS_NOTE —
# маркер, похожий на эхо заголовка Authorization, ОБЯЗАН уйти замаскированным
# и в переменную, и в лог (::error:: печатает её же). GITHUB_STEP_SUMMARY
# этот путь не трогает вовсе (dsh-ci.sh нигде его не пишет — проверено
# grep'ом по scripts/lib/dsh-ci.sh), поэтому здесь не проверяется отдельно.
reset_scenario
MARKER="sk-SMOKE1EEDEDBEEFCAFEBABE1234567890abcdef"
SMOKE_MODE_primary_model="leaky-error"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log8.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" != "0" ] || fail "8) leaky-error не должен был дать успех"
[[ "$OUT" != *"$MARKER"* ]] || fail "8) сырой маркер секрета уехал в лог (stdout/stderr шага): $OUT"
[[ "$DSH_CHAIN_CLASS_NOTE" != *"$MARKER"* ]] || fail "8) сырой маркер секрета остался в DSH_CHAIN_CLASS_NOTE после формирования: $DSH_CHAIN_CLASS_NOTE"
[[ "$OUT" == *"sk-[REDACTED]"* ]] || fail "8) redact() не отработал — в логе нет ожидаемой замены sk-[REDACTED]: $OUT"
echo "SMOKE(chain): 8) сырой stderr стоп-класса маскируется redact() до печати и до записи в переменную — ок"

# ── 9) #857: персистентная квота — провайдер с БУДУЩИМ reset_iso пропущен
# ДО попытки, реальный dsh() для него не вызывается. Доказательство: режим
# primary_model НЕ выставлен (дефолт "ok" — если бы гейт не сработал,
# PRIMARY ответил бы успехом сам) — единственная причина, по которой
# DSH_CHAIN_PROVIDER окажется SECONDARY, это сам гейт.
reset_scenario
SMOKE_MODE_secondary_model="ok"
export DSH_PROVIDER_QUOTA_UNTIL='{"PRIMARY":"2099-01-01T00:00:00Z"}'
LOG="$WORK/log9.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
unset DSH_PROVIDER_QUOTA_UNTIL
[ "$DSH_RUN_RC" = "0" ] || fail "9) ожидался успех после пропуска квотированного PRIMARY, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "9) ожидался переход на SECONDARY (PRIMARY квотирован), получено '$DSH_CHAIN_PROVIDER' — если PRIMARY, гейт не сработал"
[[ "$DSH_CHAIN_TRIED" == "PRIMARY (пропущен: квота до 2099-01-01T00:00:00Z), SECONDARY" ]] \
  || fail "9) DSH_CHAIN_TRIED обязан назвать пропуск и дату: '$DSH_CHAIN_TRIED'"
[[ "$DSH_CHAIN_RESET_HINT" == *"PRIMARY: 2099-01-01T00:00:00Z"* ]] || fail "9) дата возврата PRIMARY обязана попасть в подсказку: '$DSH_CHAIN_RESET_HINT'"
[[ "$OUT" == *"пропущен — квота до 2099-01-01T00:00:00Z"* ]] || fail "9) сообщение обязано называть факт пропуска и дату: $OUT"
echo "SMOKE(chain): 9) персистентная квота с будущим reset -> пропуск ДО попытки, dsh() не вызван — ок"

# ── 10) Мутация обратного случая: reset_iso УЖЕ в прошлом — провайдер
# пробуется как обычно (гейт не держит вечно, только до даты).
reset_scenario
SMOKE_MODE_primary_model="ok"
export DSH_PROVIDER_QUOTA_UNTIL='{"PRIMARY":"2020-01-01T00:00:00Z"}'
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke"
unset DSH_PROVIDER_QUOTA_UNTIL
[ "$DSH_RUN_RC" = "0" ] || fail "10) ожидался успех — PRIMARY, дата сброса в прошлом"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "10) ожидался PRIMARY (квота уже прошла), получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || fail "10) PRIMARY обязан быть опробован без пометки пропуска: '$DSH_CHAIN_TRIED'"
echo "SMOKE(chain): 10) персистентная квота с прошедшим reset -> пробуется как обычно — ок"

# ── 11) Повреждённое состояние (не JSON-объект) — фейл-открыто: гейт
# пропущен целиком, цепочка не падает, PRIMARY пробуется как обычно.
reset_scenario
SMOKE_MODE_primary_model="ok"
export DSH_PROVIDER_QUOTA_UNTIL='не json вовсе'
LOG="$WORK/log11.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
unset DSH_PROVIDER_QUOTA_UNTIL
[ "$DSH_RUN_RC" = "0" ] || fail "11) повреждённое состояние не должно ронять цепочку, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "11) ожидался PRIMARY (гейт фейл-открыт), получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" == *"не JSON-объект"* ]] || fail "11) сообщение обязано называть факт повреждённого состояния: $OUT"
echo "SMOKE(chain): 11) повреждённое состояние квоты -> фейл-открыто, цепочка не падает — ок"

echo "SMOKE(chain): все сценарии цепочки провайдеров целы — гвардия класса #727/#737/#743/#857 зелёная"
