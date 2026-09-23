#!/usr/bin/env bash
# Гвардия класса «цепочка провайдеров переключается по классу отказа, а не
# по факту отказа» (#727). Прогоняет dsh_run_with_provider_chain (lib/
# dsh-ci.sh) ЦЕЛИКОМ — не bash -n, настоящее исполнение с заглушкой `dsh`,
# роутящей ответ по модели (DEEPSEEK_MODEL, который chain выставляет на
# каждую попытку) — тот же приём, что dsh-clients.smoke.sh уже применяет для
# RATE_LIMIT (SMOKE_RATE_LIMIT_MODE), здесь на два провайдера сразу.
#
# #1084: с этого PR умолчание перевёрнуто — раньше НЕ распознанный явно
# текст стопорил цепочку целиком (заголовок этого файла так и назывался:
# «а не по любой ошибке»), теперь такой текст тоже переключает, и в функции
# `dsh_chain_should_advance` сейчас НЕТ ни одной именованной ветки со
# `stop`/`return 1` вовсе — единственный прежний кандидат (`INVALID_API_KEY`)
# снят как неподтверждённый прод-формой (см. комментарий над функцией в
# dsh-ci.sh, «Не подтверждено»). Сценарии 3/8/18 ниже это доказывают.
#
# Прод-форма отказов — дословно:
#   RATE_LIMIT: Weekly/Monthly Limit Exhausted... run 34176910458
#     (docs/runbooks/switch-llm-provider.md, ai-review PR #206 разбор)
#   HTTP_404: modelCode does not exist... прогон 33572445063 (PR #190,
#     scripts/lib/test/dsh-clients.smoke.sh:217, ai_review.py::test_ai_review)
#   rc=124 (наш DSH_TIMEOUT_SECS истёк, `timeout` убил процесс SIGTERM без
#     единой строки в stderr) — прогон worker.yml 34498185823 (задача #140,
#     #877): GLM отработал ровно 9000с и был убит НАШИМ ножом, не отказом
#     провайдера — сценарий 12 ниже доказывает, что это отличается от
#     "silent" (#737, rc=1, тоже пустой stderr, но другая причина) текстом
#     сообщения, не только решением «переключаемся».
#   STREAM_CLOSED: SSE stream ended without [DONE]... прогон worker.yml
#     2026-09-13T09:39Z (задача #1055/#1087, #1084) — GLM оборвал SSE-поток;
#     тот же текст видели у OpenRouter-2 той же ночью (00:07Z) — не привязан
#     к одному провайдеру, сценарий 17 ниже доказывает автопереход (#1084).
#     Сценарий 18 доказывает перевёрнутое умолчание: НЕИЗВЕСТНЫЙ класс отказа
#     (никакой явный признак не совпал) отныне тоже переключает, не стопорит
#     цепочку (#1084) — в функции сейчас нет ни одной ветки stop вовсе
#     (сценарии 3/8 — та же мутация на других литералах stderr).
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
  {"name":"SECONDARY","model_sha256":"$(hash_of secondary-model)","confirmed_at":"2026-09-08","evidence":"smoke fixture"},
  {"name":"TERTIARY","model_sha256":"$(hash_of tertiary-model)","confirmed_at":"2026-09-13","evidence":"smoke fixture (#1121/#1124)"},
  {"name":"MULTI-DEAD","model_sha256":"$(hash_of dead-model)","confirmed_at":"2026-09-15","evidence":"smoke fixture (#1309): id, который провайдер снял (410) — подтверждён реестром, но мёртв в каталоге"},
  {"name":"MULTI-ALSO-DEAD","model_sha256":"$(hash_of also-dead-model)","confirmed_at":"2026-09-15","evidence":"smoke fixture (#1309)"},
  {"name":"MULTI-LIVE","model_sha256":"$(hash_of live-model)","confirmed_at":"2026-09-15","evidence":"smoke fixture (#1309): второй кандидат того же аккаунта, живой"}
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
        always-rate-limit)
          # #877: короткое окно RATE_LIMIT, которое НИКОГДА не снимается —
          # dsh_run_with_retry ретраит, пока не кончится бюджет ожидания
          # (DSH_RATE_LIMIT_MAX_WAIT_SECS), а не таймаут/квота. Прод-форма
          # строки — та же, что уже используют dsh-clients.smoke.sh и
          # dsh_run_with_retry сам ищет («RATE_LIMIT:» без «Weekly/Monthly»).
          echo "dsh: RATE_LIMIT: Rate limit reached for requests" >&2
          return 1 ;;
        timeoutkill)
          # #877, живой случай — прогон worker.yml 34498185823: `timeout`
          # убивает процесс SIGTERM без единой строки в stderr — та же
          # пустота stderr, что и "silent" выше, но ДРУГАЯ причина (rc=124,
          # не молчание провайдера). Смоук держит `timeout()`
          # переопределённой в проходной no-op (см. заголовок файла) — здесь
          # эмулируется РЕЗУЛЬТАТ настоящего таймаута напрямую кодом возврата.
          return 124 ;;
        invalid-request-maxtokens)
          # #1062, живой случай — прогон worker.yml 34730173870: наш
          # config/provider-usage.json нёс неверный max_output_tokens для
          # ЭТОЙ записи — дословная прод-форма Ollama Cloud.
          echo "dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output tokens (65536) for model nemotron-3-ultra" >&2
          return 1 ;;
        rate-limit-then-410)
          # #1309: ЕДИНСТВЕННАЯ достижимая последовательность, в которой один
          # аккаунт тратит ожидание И переходит на следующую свою модель:
          # провайдер под нагрузкой отвечает 429 несколько раз, а потом
          # оказывается, что сам id снят из каталога. Обе строки — дословные
          # прод-формы (см. заголовок файла и режимы always-rate-limit/http410
          # выше); синтетическая здесь только их ПОСЛЕДОВАТЕЛЬНОСТЬ, которая и
          # есть предмет фикстуры. Счётчик — файл, а не переменная: заглушка
          # вызывается в подоболочках.
          local counter="$WORK/calls-${DEEPSEEK_MODEL}"
          local calls=$(( $(cat "$counter" 2>/dev/null || echo 0) + 1 ))
          echo "$calls" >"$counter"
          if [ "$calls" -le 2 ]; then
            echo "dsh: RATE_LIMIT: Rate limit reached for requests" >&2
          else
            echo "dsh: HTTP_410: DeepSeek API error (HTTP 410)" >&2
          fi
          return 1 ;;
        http410)
          # #1309, живой прогон worker.yml 35010410097 (2026-09-15) — дословно:
          # id модели снят провайдером, аккаунт при этом жив.
          echo "dsh: HTTP_410: DeepSeek API error (HTTP 410)" >&2
          return 1 ;;
        stream-closed)
          # #1084, живой случай — прогон worker.yml 2026-09-13T09:39Z: GLM
          # оборвал SSE-поток без терминального [DONE] — дословная прод-форма.
          echo "dsh: STREAM_CLOSED: SSE stream ended without [DONE]" >&2
          return 1 ;;
        unknown-error)
          # #1084: НИКАКОЙ явный признак не совпадает — представитель класса
          # «новый провайдер, свой текст ошибки, которого ещё не видели».
          # Раньше это стопорило цепочку целиком (общий стоп-класс), теперь —
          # автопереход (перевёрнутое умолчание, см. dsh-ci.sh).
          echo "dsh: TRANSPORT_HICCUP: upstream reset the connection mid-response" >&2
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
  rm -f "$WORK"/calls-* 2>/dev/null || true
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

# ── 3) #1084 (пересмотр #727, п.4): «настоящая ошибка» (нераспознанный,
# непустой stderr) теперь ТОЖЕ переключает — до этого PR здесь проверялся
# ОБРАТНЫЙ факт («не любой отказ переключает», единственным примером был
# синтетический INVALID_API_KEY). Находка ai-review при доводке: тот литерал
# ни разу не встречался ни в одном живом прогоне — держать единственный
# стоп-класс на непроверенной строке значило бы одновременно (а) не ловить
# реальный отказ credentials с ДРУГИМ текстом и (б) кормить гвардию
# пересказом, а не прод-формой (AGENTS.md, «Тест кормит прод-форму, а не
# пересказ»). Решение #1084 — снять стоп-класс целиком, пока не найдётся
# живая цитата. `real-error` остаётся представителем «непустой, но
# нераспознанный текст» — используется теперь как ЕЩЁ один пример общего
# перевёрнутого умолчания (см. также сценарий 18).
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
[ "$DSH_RUN_RC" = "0" ] || fail "3) ожидался успех после нераспознанной ошибки у первого (перевёрнутое умолчание #1084), получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "3) ожидался переход на SECONDARY, получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "3) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — оба провайдера обязаны быть опробованы (нет стоп-класса, #1084)"
[[ "$OUT" == *"класс не распознан"* ]] || fail "3) сообщение обязано честно назвать «класс не распознан»: $OUT"
echo "SMOKE(chain): 3) нераспознанная ошибка -> автопереход, стоп-класс снят как неподтверждённый (#1084) — ок"

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

# ── 8) Класс #743: сырой stderr нераспознанного класса кладётся в
# DSH_CHAIN_CLASS_NOTE — маркер, похожий на эхо заголовка Authorization,
# ОБЯЗАН уйти замаскированным и в переменную, и в лог (::warning:: печатает
# её же). До #1084 этот путь был стоп-классом (цепочка не шла дальше) —
# теперь та же самая непустая нераспознанная строка переключает
# (DSH_CHAIN_PROVIDER=SECONDARY), redact-дисциплина #743 при этом не
# зависит от решения переключаемости и обязана держаться так же.
# GITHUB_STEP_SUMMARY этот путь не трогает вовсе (dsh-ci.sh нигде его не
# пишет — проверено grep'ом по scripts/lib/dsh-ci.sh), поэтому здесь не
# проверяется отдельно.
reset_scenario
MARKER="sk-SMOKE1EEDEDBEEFCAFEBABE1234567890abcdef"
SMOKE_MODE_primary_model="leaky-error"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log8.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "8) ожидался успех после leaky-error у первого (перевёрнутое умолчание #1084), получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "8) ожидался переход на SECONDARY, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" != *"$MARKER"* ]] || fail "8) сырой маркер секрета уехал в лог (stdout/stderr шага): $OUT"
[[ "$DSH_CHAIN_CLASS_NOTE" != *"$MARKER"* ]] || fail "8) сырой маркер секрета остался в DSH_CHAIN_CLASS_NOTE после формирования: $DSH_CHAIN_CLASS_NOTE"
[[ "$OUT" == *"sk-[REDACTED]"* ]] || fail "8) redact() не отработал — в логе нет ожидаемой замены sk-[REDACTED]: $OUT"
echo "SMOKE(chain): 8) сырой stderr нераспознанного класса маскируется redact() до печати и до записи в переменную (класс переключаем, #1084) — ок"

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

# ── 12) #877: rc=124 (наш таймаут) ОТЛИЧАЕТСЯ от "silent" (#737, rc=1, тоже
# пустой stderr) — оба переключают на следующего провайдера (решение то же),
# но сообщение обязано честно называть НАШ таймаут, не «класс не установить».
# #880 (некритичная находка ai-review): «наш нож» утверждается только когда
# попытка реально длилась не меньше своего таймаута — `timeout()` в этом
# смоуке пропускает вызов насквозь без сна (заголовок файла), реального
# зависания не воспроизвести, поэтому DSH_TIMEOUT_SECS=0 делает elapsed(0
# или доли секунды)>=timeout(0) тривиально истинным — граничный, но честный
# случай той же проверки dsh_chain_should_advance.
reset_scenario
SMOKE_MODE_primary_model="timeoutkill"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log12.txt"
DSH_TIMEOUT_SECS=0 dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "12) ожидался успех после таймаута у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "12) ожидался переход на SECONDARY при таймауте PRIMARY, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" == *"rc=124"* ]] || fail "12) сообщение обязано называть код возврата 124: $OUT"
[[ "$OUT" == *"наш таймаут"* ]] || fail "12) сообщение обязано честно называть НАШ таймаут, не «класс не установить»: $OUT"
[[ "$OUT" != *"стдерр пуст"* && "$OUT" != *"stderr пуст"* ]] || fail "12) rc=124 не должен маскироваться под «стдерр пуст» (#737) — это другая причина: $OUT"
echo "SMOKE(chain): 12) rc=124 (наш таймаут) -> автопереход с честным сообщением, не спутан с молчанием провайдера (#737) — ок"

# ── 13) #877 (находка ai-review PR #880): бюджет ожидания RATE_LIMIT ОБЩИЙ
# на весь прогон цепочки, не полный заново на каждого провайдера. PRIMARY
# никогда не снимает короткий RATE_LIMIT — выжигает ВЕСЬ общий бюджет (60с)
# сам; SECONDARY получает то, что осталось (0с) и обязан сдаться на ПЕРВОЙ
# же попытке, не получив собственных полных 60с заново.
reset_scenario
SMOKE_MODE_primary_model="always-rate-limit"
SMOKE_MODE_secondary_model="always-rate-limit"
LOG="$WORK/log13.txt"
DSH_RATE_LIMIT_MAX_WAIT_SECS=60 \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS=30 \
DSH_RATE_LIMIT_MAX_DELAY_SECS=30 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" != "0" ] || fail "13) оба провайдера в вечном RATE_LIMIT — успеха быть не должно"
[ "$DSH_RUN_FAILURE_REASON" = "all_providers_exhausted" ] || fail "13) ожидался all_providers_exhausted, получено '$DSH_RUN_FAILURE_REASON'"
[[ "$OUT" == *"остаток общего бюджета RATE_LIMIT: 60с из 60с"* ]] \
  || fail "13) PRIMARY обязан стартовать с полного общего бюджета (60с): $OUT"
[[ "$OUT" == *"остаток общего бюджета RATE_LIMIT: 0с из 60с"* ]] \
  || fail "13) SECONDARY обязан получить то, что осталось от бюджета PRIMARY (0с), а не полные 60с заново: $OUT"
secondary_attempts=$(grep -c 'dsh: попытка' <(sed -n '/пробую SECONDARY/,$p' "$LOG")) || true
[ "$secondary_attempts" -le 1 ] \
  || fail "13) SECONDARY сделал $secondary_attempts попыток — с нулевым остатком бюджета обязана быть ровно одна (бюджет исчерпан ДО первого ретрая): $OUT"
echo "SMOKE(chain): 13) общий бюджет RATE_LIMIT на весь прогон цепочки, не на каждого провайдера заново — ок"

# ── 14) #877/#880 (находка ai-review PR #880 на первой версии фикса стыка):
# бюджет RATE_LIMIT, потраченный ПУЛОМ (dsh_run_with_pool_then_chain пробует
# anthropic-oauth-pool ДО цепочки), обязан вычитаться из общего бюджета
# цепочки — пул это 1-я из 10 попыток прогона, не отдельная ось. Без
# передачи DSH_RUN_WAITED_SECS пула цепочка получала бы полный бюджет
# заново (ровно тот баг, который эта проверка ловит мутацией).
reset_scenario
export DSH_ANTHROPIC_POOL_ACTIVE=1
DEEPSEEK_MODEL="pool-model"
SMOKE_MODE_pool_model="always-rate-limit"
SMOKE_MODE_primary_model="ok"
LOG="$WORK/log14.txt"
DSH_RATE_LIMIT_MAX_WAIT_SECS=60 \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS=30 \
DSH_RATE_LIMIT_MAX_DELAY_SECS=30 \
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
unset DSH_ANTHROPIC_POOL_ACTIVE
[ "$DSH_RUN_RC" = "0" ] || fail "14) PRIMARY отвечает успехом после отказа пула, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "14) ожидался переход на PRIMARY после отказа пула, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" == *"остаток общего бюджета RATE_LIMIT: 0с из 60с"* ]] \
  || fail "14) PRIMARY обязан получить остаток ПОСЛЕ пула (0с из 60с), не полный бюджет заново: $OUT"
echo "SMOKE(chain): 14) бюджет RATE_LIMIT пула вычитается из общего бюджета цепочки — ок"

# ── 15) #880 (некритичная находка ai-review): rc=124, но попытка длилась
# МЕНЬШЕ своего таймаута — ребёнок сам вышел с 124 по собственной причине,
# `timeout` его не убивал. «Наш нож» здесь утверждать НЕЛЬЗЯ (мы не можем
# это доказать) — DSH_TIMEOUT_SECS заведомо больше реальной (нулевой)
# длительности фиктивного вызова, elapsed(0) < timeout(3600) — сообщение
# обязано остаться в общей недиагностируемой ветке («стдерр пуст»), не
# заявлять факт, который не проверялся.
reset_scenario
SMOKE_MODE_primary_model="timeoutkill"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log15.txt"
DSH_TIMEOUT_SECS=3600 dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "15) ожидался успех после rc=124 у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "15) ожидался переход на SECONDARY, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" != *"наш таймаут"* ]] \
  || fail "15) elapsed < timeout — «наш нож» не доказан замером, но сообщение его утверждает: $OUT"
[[ "$OUT" == *"stderr пуст"* ]] \
  || fail "15) недоказанный rc=124 обязан остаться в общей недиагностируемой ветке: $OUT"
echo "SMOKE(chain): 15) rc=124 без подтверждения elapsed>=timeout -> не приписывается «нашему ножу» без доказательства (#880) — ок"

# ── 16) #1062, живой случай — прогон worker.yml 34730173870: наш собственный
# конфиг (max_output_tokens в config/provider-usage.json) неверен для ОДНОЙ
# записи — провайдер отвечает INVALID_REQUEST про max_tokens/лимит модели.
# Это ошибка ПАРАМЕТРОВ ЗАПРОСА этой конкретной записи, не признак «дальше
# пробовать бессмысленно» — цепочка обязана переключиться на следующего
# (у него свой max_output_tokens), а сообщение обязано прямо назвать
# «конфиг этого провайдера неверен», не раствориться в стоп-классе.
reset_scenario
SMOKE_MODE_primary_model="invalid-request-maxtokens"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log16.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "16) ожидался успех после INVALID_REQUEST/max_tokens у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "16) ожидался переход на SECONDARY при ошибке конфига PRIMARY, получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "16) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — оба провайдера обязаны быть опробованы"
[[ "$OUT" == *"конфиг ЭТОГО провайдера неверен"* ]] \
  || fail "16) сообщение обязано прямо назвать «конфиг этого провайдера неверен» (AGENTS.md, «возможность есть, но сломана»): $OUT"
[[ "$OUT" != *"класс НЕ переключаемый"* ]] \
  || fail "16) ошибка параметров запроса ОДНОГО провайдера не обязана останавливать цепочку: $OUT"
echo "SMOKE(chain): 16) INVALID_REQUEST/max_tokens (наш конфиг неверен для этой записи) -> автопереход, не стоп-класс (#1062) — ок"

# ── 17) #1084, живой инцидент — прогон worker.yml 2026-09-13T09:39Z (задача
# #1055/#1087): GLM (единственный реально отвечавший провайдер) оборвал
# SSE-поток без терминального [DONE]. Раньше это НЕ совпадало ни с одним
# явным признаком и падало в стоп-класс — цепочка останавливалась целиком.
# STREAM_CLOSED теперь явный переключаемый класс, симметричный HTTP_404/
# EMPTY_RESPONSE (сценарий 2 выше).
reset_scenario
SMOKE_MODE_primary_model="stream-closed"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log17.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "17) ожидался успех после STREAM_CLOSED у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "17) ожидался переход на SECONDARY при STREAM_CLOSED PRIMARY, получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "17) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — оба провайдера обязаны быть опробованы"
[[ "$OUT" != *"класс НЕ переключаемый"* ]] \
  || fail "17) STREAM_CLOSED — transient-обрыв SSE, не обязан останавливать цепочку: $OUT"
# ai-review (доводка #1084): без явной ветки `grep -qE 'STREAM_CLOSED:'` в
# dsh-ci.sh этот сценарий всё равно зеленел бы — перевёрнутое умолчание само
# переключает ЛЮБОЙ нераспознанный текст, включая STREAM_CLOSED, и печатало
# бы «класс не распознан (…)». Явную ветку доказывает ИМЕННО текст причины:
# только она пишет «класс отказа: STREAM_CLOSED (…)» (::warning:: печатает
# «класс отказа: $DSH_CHAIN_CLASS_NOTE»); «класс не распознан» — другой текст.
[[ "$OUT" == *"класс отказа: STREAM_CLOSED"* ]] \
  || fail "17) явная ветка STREAM_CLOSED обязана дать СВОЙ текст причины, не «класс не распознан»: $OUT"
echo "SMOKE(chain): 17) STREAM_CLOSED (обрыв SSE без [DONE]) -> автопереход по ЯВНОЙ ветке, не по умолчанию — ок"

# ── 18) #1084: перевёрнутое умолчание — НЕИЗВЕСТНЫЙ класс отказа (никакой
# явный признак не совпал) отныне ТОЖЕ переключает — в этой функции сейчас
# НЕТ ни одной ветки, возвращающей стоп (см. «Не подтверждено» в dsh-ci.sh:
# бывший именованный стоп-класс INVALID_API_KEY снят как неподтверждённый
# прод-формой, #1084). До этой правки этот же сценарий стопорил бы цепочку
# (общий стоп-класс по умолчанию) — именно эта мутация и доказывает
# переворот: с прежним кодом (return 1 в общем catch-all) DSH_CHAIN_PROVIDER
# остался бы пустым, а DSH_CHAIN_TRIED — только "PRIMARY".
reset_scenario
SMOKE_MODE_primary_model="unknown-error"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log18.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "18) ожидался успех после нераспознанного класса у первого, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || fail "18) ожидался переход на SECONDARY при нераспознанном классе PRIMARY (перевёрнутое умолчание #1084), получено '$DSH_CHAIN_PROVIDER'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY" ] || fail "18) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — оба провайдера обязаны быть опробованы"
[[ "$OUT" == *"класс не распознан"* ]] || fail "18) сообщение обязано честно назвать «класс не распознан»: $OUT"
[[ "$OUT" != *"класс НЕ переключаемый"* ]] \
  || fail "18) нераспознанный класс не обязан останавливать цепочку (перевёрнутое умолчание #1084): $OUT"
echo "SMOKE(chain): 18) нераспознанный класс отказа -> автопереход по умолчанию, не стоп (перевёрнутое умолчание #1084) — ок"

# ── 19) #1121/#1124, живой инцидент — прогоны worker.yml 34735752165/
# 34739313568: ОДИН провайдер (там — OpenRouter-2) тратит ретраем ВЕСЬ
# общий бюджет RATE_LIMIT сам, следующему достаётся 0с (см. сценарий 13 —
# это УЖЕ доказанное, ожидаемое поведение БЕЗ потолка). Здесь — потолок на
# долю ОДНОГО провайдера (DSH_RATE_LIMIT_PROVIDER_CAP_SECS): три провайдера,
# первые два в вечном RATE_LIMIT, третий отвечает успехом. Бюджет 60с,
# потолок 20с, задержка бэкоффа стартует с 30с (больше потолка — потолок
# обязан урезать её ДО потолка на первом же шаге, не только считать сумму).
# Без потолка PRIMARY выжег бы все 60с сам (та же арифметика, что сценарий
# 13), и SECONDARY получил бы 0с — печатался бы «остаток общего бюджета
# RATE_LIMIT: 0с из 60с» без какой-либо пометки потолка. С потолком PRIMARY
# обязан остановиться ровно на 20с (потолок), у SECONDARY реально остаётся
# 40с (60-20), из которых ему тоже выделяется не больше потолка (20с) — то
# есть SECONDARY обязан получить РЕАЛЬНЫЙ многошаговый шанс, а не 0.
reset_scenario
THREE_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096},
  {"name":"TERTIARY","base_url":"https://tertiary.test/v1","model":"tertiary-model","secret_env":"TERTIARY_KEY","max_output_tokens":4096}
]'
export TERTIARY_KEY="tertiary-test-key"
SMOKE_MODE_primary_model="always-rate-limit"
SMOKE_MODE_secondary_model="always-rate-limit"
SMOKE_MODE_tertiary_model="ok"
LOG="$WORK/log19.txt"
( export DSH_PROVIDER_CHAIN="$THREE_CHAIN"
  DSH_RATE_LIMIT_MAX_WAIT_SECS=60 \
  DSH_RATE_LIMIT_INITIAL_DELAY_SECS=30 \
  DSH_RATE_LIMIT_MAX_DELAY_SECS=30 \
  DSH_RATE_LIMIT_PROVIDER_CAP_SECS=20 \
    dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::19) TERTIARY отвечает успехом, ожидался rc=0, получено $DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "TERTIARY" ] || { echo "::error::19) ожидался переход на TERTIARY, получено '$DSH_CHAIN_PROVIDER'" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "PRIMARY, SECONDARY, TERTIARY" ] || { echo "::error::19) все три провайдера обязаны быть опробованы: '$DSH_CHAIN_TRIED'" >&2; exit 1; }
  [[ "$OUT" == *"пробую PRIMARY"*"остаток общего бюджета RATE_LIMIT: 60с из 60с"*", провайдеру выделено не больше 20с (потолок 20с на провайдера, #1121)"* ]] \
    || { echo "::error::19) PRIMARY обязан стартовать с полного общего бюджета (60с), урезанного потолком до 20с: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"пробую SECONDARY"*"остаток общего бюджета RATE_LIMIT: 40с из 60с"*", провайдеру выделено не больше 20с (потолок 20с на провайдера, #1121)"* ]] \
    || { echo "::error::19) SECONDARY обязан получить РЕАЛЬНЫЙ остаток (40с из 60с), урезанный потолком до 20с — НЕ 0с, как было бы без потолка (мутация: сними cap-логику, эта строка покраснеет): $OUT" >&2; exit 1; }
  [[ "$OUT" == *"пробую TERTIARY"*"остаток общего бюджета RATE_LIMIT: 20с из 60с"* ]] \
    || { echo "::error::19) TERTIARY обязан увидеть остаток 20с из 60с (PRIMARY+SECONDARY суммарно потратили ровно 40с, не 60): $OUT" >&2; exit 1; }
) || fail "19) сценарий с потолком на провайдера провалился"
echo "SMOKE(chain): 19) потолок доли ОДНОГО провайдера из общего бюджета RATE_LIMIT — второй провайдер получает реальный шанс, не 0с (#1121/#1124) — ок"

# Номер 20 намеренно пропущен: он занят открытым PR #1306 (HTTP_410 как
# именованный терминальный класс в dsh_chain_should_advance). Сценарии ниже
# трогают ДРУГИЕ функции (модельный цикл, сводка исхода, доля пула) и с ним
# не конфликтуют по смыслу — только по номеру, если занять тот же.

# ── 21) #1309: мёртвый id модели НЕ сжигает живой аккаунт. У PRIMARY два
# кандидата: первый отвечает HTTP 410 Gone (дословная прод-форма прогона
# worker.yml 35010410097), второй — успехом. Цепочка обязана остаться на ТОМ
# ЖЕ аккаунте: SECONDARY не тронут вовсе. Мутация: убери модельную ветку в
# dsh_run_with_provider_chain — ответит SECONDARY, и обе проверки ниже
# (DSH_CHAIN_PROVIDER и DSH_CHAIN_TRIED) покраснеют.
reset_scenario
MULTI_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","models":["dead-model","live-model"],"secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096}
]'
SMOKE_MODE_dead_model="http410"
SMOKE_MODE_live_model="ok"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log21.txt"
( export DSH_PROVIDER_CHAIN="$MULTI_CHAIN"
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::21) вторая модель того же аккаунта отвечает успехом, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::21) аккаунт меняться не должен — мёртв был ID МОДЕЛИ, получено '$DSH_CHAIN_PROVIDER'" >&2; exit 1; }
  [ "$DSH_CHAIN_MODEL" = "live-model" ] || { echo "::error::21) ответить обязана вторая модель того же аккаунта: '$DSH_CHAIN_MODEL'" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || { echo "::error::21) SECONDARY не должен быть тронут вовсе (аккаунт PRIMARY жив): '$DSH_CHAIN_TRIED'" >&2; exit 1; }
  [ "$DSH_CHAIN_MODELS_TRIED" = "PRIMARY/dead-model, PRIMARY/live-model" ] || { echo "::error::21) обе модели обязаны быть учтены по порядку: '$DSH_CHAIN_MODELS_TRIED'" >&2; exit 1; }
  [[ "$OUT" == *"пробую следующую модель ЭТОГО же провайдера"* ]] || { echo "::error::21) сообщение обязано назвать факт смены МОДЕЛИ, а не аккаунта: $OUT" >&2; exit 1; }
  grep -q "ответ от live-model" "$ANSWER" || { echo "::error::21) answer.txt не от второй модели" >&2; exit 1; }
) || fail "21) модельный фолбэк внутри аккаунта провалился"
echo "SMOKE(chain): 21) мёртвый id модели -> следующая модель ТОГО ЖЕ аккаунта, аккаунт не расходуется (#1309) — ок"

# ── 22) #1309: кандидаты КОНЧИЛИСЬ — решение о переходе к следующему аккаунту
# принимает та же dsh_chain_should_advance, что и до #1309 (модельный цикл её
# не подменяет). Оба кандидата PRIMARY мертвы -> отвечает SECONDARY.
reset_scenario
DEAD_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","models":["dead-model","also-dead-model"],"secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096}
]'
SMOKE_MODE_dead_model="http410"
SMOKE_MODE_also_dead_model="http410"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log22.txt"
( export DSH_PROVIDER_CHAIN="$DEAD_CHAIN"
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::22) SECONDARY отвечает успехом, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || { echo "::error::22) кандидаты PRIMARY кончились — обязан быть переход на SECONDARY: '$DSH_CHAIN_PROVIDER'" >&2; exit 1; }
  [ "$DSH_CHAIN_MODELS_TRIED" = "PRIMARY/dead-model, PRIMARY/also-dead-model, SECONDARY/secondary-model" ] \
    || { echo "::error::22) обязаны быть опробованы обе модели PRIMARY и затем SECONDARY: '$DSH_CHAIN_MODELS_TRIED'" >&2; exit 1; }
) || fail "22) исчерпание кандидатов внутри аккаунта провалилось"
echo "SMOKE(chain): 22) все модели аккаунта мертвы -> переход на следующий аккаунт как раньше (#1309) — ок"

# ── 34) #1494: 404 — НЕ доказательство мёртвого id. Живой случай (замер
# 2026-09-23, docs/research/28-provider-chain-truth-table.md): GLM отдал
# HTTP_404 через цепочку (03:51 UTC) и HTTP 200 прямым вызовом того же base_url
# тем же секретом (13:03 UTC, другой раннер) — вердикт «dead_model» отправлял
# чинить исправное.
# Поведение (переход к следующей модели того же аккаунта) сохранено; проверяется
# ЗАЯВЛЕНИЕ: класс исхода model_unclear, а не dead_model, и действие называет
# дешёвую проверку. Мутация: верни 'HTTP_404:' в _dsh_failure_is_model_scoped —
# исход снова станет «мёртвая конфигурация», и обе проверки ниже покраснеют.
reset_scenario
UNCLEAR_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","models":["dead-model","also-dead-model"],"secret_env":"PRIMARY_KEY","max_output_tokens":4096}
]'
SMOKE_MODE_dead_model="http404"
SMOKE_MODE_also_dead_model="http404"
LOG="$WORK/log34.txt"
( export DSH_PROVIDER_CHAIN="$UNCLEAR_CHAIN"
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1 || true
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" != "0" ] || { echo "::error::34) обе модели отдают 404 — успеха быть не может. ЛОГ: $OUT" >&2; exit 1; }
  # Со ЗНАЧЕНИЕМ, а не по подстроке: название класса стоит в шаблоне сводки
  # всегда, хоть при счётчике 0, и проверка по подстроке прошла бы на сломанном
  # коде молча (поймано ревью этого же PR — под мутацией она не покраснела,
  # покраснела только следующая). AGENTS.md: гвардия, которая не может
  # покраснеть, читается как доказательство и им не является.
  [[ "$DSH_CHAIN_OUTCOME_SUMMARY" == *"причина не установлена (404 — модель или маршрут, #1494): 1"* ]] \
    || { echo "::error::34) класс «причина не установлена» обязан быть посчитан (ожидалась 1): '$DSH_CHAIN_OUTCOME_SUMMARY'" >&2; exit 1; }
  [[ "$DSH_CHAIN_OUTCOME_SUMMARY" == *"мёртвая конфигурация (нет секрета/неподтверждённый id/снятая моделью): 0"* ]] \
    || { echo "::error::34) 404 не имеет права засчитываться мёртвой конфигурацией: '$DSH_CHAIN_OUTCOME_SUMMARY'" >&2; exit 1; }
  [[ "$OUT" == *"не различает"* ]] \
    || { echo "::error::34) сообщение обязано сказать, что 404 не различает модель и маршрут: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"provider_latency.py"* ]] \
    || { echo "::error::34) действие обязано назвать дешёвую проверку, а не отправлять «узнать точный id»: $OUT" >&2; exit 1; }
  [[ "$OUT" != *"узнать точный id"* && "$OUT" != *"Узнать точный id"* ]] \
    || { echo "::error::34) при одном лишь 404 действие «узнать точный id модели» утверждает недоказанное: $OUT" >&2; exit 1; }
) || fail "34) класс «причина не установлена» для 404 провалился"
echo "SMOKE(chain): 34) 404 -> причина НЕ установлена, а не «мёртвый id» (#1494) — ок"

# ── 23) #1309: обе формы элемента читаются ОДНИМ местом правды. Прежняя форма
# ({model, max_output_tokens}) обязана давать ровно один кандидат — иначе вся
# цепочка до #1309 поменяла бы поведение молча.
one=$(dsh_entry_model_candidates '{"name":"X","model":"solo","max_output_tokens":777}')
[ "$(jq -c . <<<"$one")" = '[{"model":"solo","max_output_tokens":777}]' ] \
  || fail "23) форма {model} обязана давать ровно один кандидат: $one"
many=$(dsh_entry_model_candidates '{"name":"X","models":["a","b"],"max_output_tokens":777}')
[ "$(jq -c . <<<"$many")" = '[{"model":"a","max_output_tokens":777},{"model":"b","max_output_tokens":777}]' ] \
  || fail "23) форма {models:[строки]} обязана наследовать потолок элемента: $many"
mixed=$(dsh_entry_model_candidates '{"name":"X","models":[{"id":"a","max_output_tokens":10},"b"],"max_output_tokens":777}')
[ "$(jq -c . <<<"$mixed")" = '[{"model":"a","max_output_tokens":10},{"model":"b","max_output_tokens":777}]' ] \
  || fail "23) свой потолок кандидата обязан побеждать потолок элемента: $mixed"
[ "$(dsh_chain_head_model "$MULTI_CHAIN")" = "dead-model" ] || fail "23) затравка профиля обязана брать ПЕРВОГО кандидата chain[0]"
[ "$(dsh_chain_head_max_tokens "$MULTI_CHAIN")" = "4096" ] || fail "23) затравка профиля обязана брать потолок chain[0]"
echo "SMOKE(chain): 23) {model} и {models:[…]} — одно место правды, обратная совместимость побайтная (#1309) — ок"

# ── 24) #1307: итог цепочки называет ЧИСЛА по классам, а не одну фразу
# «исчерпана целиком». Три провайдера, три РАЗНЫХ класса: PRIMARY реально без
# квоты, SECONDARY выжигает остаток бюджета (наш бюджет, не квота), TERTIARY
# несёт мёртвый id модели. Живой прототип — прогон worker.yml 35010410097:
# 1 реально без квоты из 8, 5 по нашему бюджету, 2 мёртвых id.
reset_scenario
MIXED_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096},
  {"name":"TERTIARY","base_url":"https://tertiary.test/v1","model":"tertiary-model","secret_env":"TERTIARY_KEY","max_output_tokens":4096}
]'
export TERTIARY_KEY="tertiary-test-key"
SMOKE_MODE_primary_model="quota"
SMOKE_MODE_secondary_model="always-rate-limit"
SMOKE_MODE_tertiary_model="http410"
LOG="$WORK/log24.txt"
( export DSH_PROVIDER_CHAIN="$MIXED_CHAIN"
  DSH_RATE_LIMIT_MAX_WAIT_SECS=30 \
  DSH_RATE_LIMIT_INITIAL_DELAY_SECS=30 \
  DSH_RATE_LIMIT_MAX_DELAY_SECS=30 \
    dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_FAILURE_REASON" = "all_providers_exhausted" ] || { echo "::error::24) контракт причины не меняется: '$DSH_RUN_FAILURE_REASON'" >&2; exit 1; }
  [[ "$OUT" != *"провайдеров исчерпана целиком"* ]] || { echo "::error::24) УТВЕРЖДЕНИЕ «цепочка провайдеров исчерпана целиком» при одном реально исчерпанном из трёх — это и есть дефект #1307: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"НЕ «исчерпана целиком»"* ]] || { echo "::error::24) сообщение обязано прямо опровергнуть прежнюю формулировку, а не просто её не печатать: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"реально без квоты: 1 из 3"* ]] || { echo "::error::24) сводка обязана назвать число реально исчерпанных: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"не пробованы по-настоящему (наш бюджет ожидания исчерпан): 1"* ]] || { echo "::error::24) сводка обязана назвать число непробованных по нашему бюджету: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"мёртвая конфигурация"*": 1"* ]] || { echo "::error::24) сводка обязана назвать число мёртвых конфигураций: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"Действие:"* ]] || { echo "::error::24) сообщение обязано назвать действие, следующее из разбора: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_RETRY_USEFUL" = "1" ] || { echo "::error::24) повтор имеет смысл (не все без квоты) — DSH_CHAIN_RETRY_USEFUL='$DSH_CHAIN_RETRY_USEFUL'" >&2; exit 1; }
) || fail "24) честный разбор исхода цепочки провалился"
echo "SMOKE(chain): 24) итог цепочки — числа по классам и действие, не «исчерпана целиком» (#1307) — ок"

# ── 25) #1307, обратный случай: ВСЕ реально без квоты — прежняя формулировка
# верна буквально и обязана остаться, а повтор обязан быть назван
# бессмысленным. Без этой пары сценарий 24 можно было бы «починить»
# вычёркиванием фразы отовсюду.
reset_scenario
SMOKE_MODE_primary_model="quota"
SMOKE_MODE_secondary_model="quota"
LOG="$WORK/log25.txt"
dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[[ "$OUT" == *"провайдеров исчерпана целиком"* ]] || fail "25) все реально без квоты — прежняя формулировка обязана остаться: $OUT"
[[ "$OUT" == *"все 2 реально без квоты"* ]] || fail "25) сообщение обязано назвать число: $OUT"
[ "$DSH_CHAIN_RETRY_USEFUL" = "0" ] || fail "25) все без квоты — повтор бессмысленен, DSH_CHAIN_RETRY_USEFUL='$DSH_CHAIN_RETRY_USEFUL'"
echo "SMOKE(chain): 25) все провайдеры реально без квоты -> «исчерпана целиком» остаётся верной, повтор назван бессмысленным (#1307) — ок"

# ── 26) #1307, живой инцидент (прогоны worker.yml 34942030597 и 35010410097,
# 2026-09-15): пул шёл БЕЗ потолка доли провайдера и выжигал весь общий
# бюджет ожидания — цепочка получала 0с и сдавалась на первом же ответе
# каждого провайдера. Здесь: общий бюджет 60с, потолок 20с; пул в вечном
# RATE_LIMIT. PRIMARY обязан увидеть РЕАЛЬНЫЙ остаток (40с из 60с), а не 0с.
# Мутация: сними DSH_RATE_LIMIT_MAX_WAIT_SECS="$pool_share" у вызова пула —
# строка «остаток общего бюджета RATE_LIMIT: 0с из 60с» вернётся, проверка
# покраснеет (ровно то, что показывал живой лог).
reset_scenario
export DSH_ANTHROPIC_POOL_ACTIVE=1
DEEPSEEK_MODEL="pool-model"
SMOKE_MODE_pool_model="always-rate-limit"
SMOKE_MODE_primary_model="ok"
LOG="$WORK/log26.txt"
DSH_RATE_LIMIT_MAX_WAIT_SECS=60 \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS=10 \
DSH_RATE_LIMIT_MAX_DELAY_SECS=10 \
DSH_RATE_LIMIT_PROVIDER_CAP_SECS=20 \
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
unset DSH_ANTHROPIC_POOL_ACTIVE
[ "$DSH_RUN_RC" = "0" ] || fail "26) PRIMARY отвечает успехом после отказа пула, получено $DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "26) ожидался PRIMARY после отказа пула, получено '$DSH_CHAIN_PROVIDER'"
[[ "$OUT" == *"доля общего бюджета RATE_LIMIT: 20с (потолок 20с на провайдера из 60с"* ]] \
  || fail "26) пул обязан получить ДОЛЮ (20с), а не весь бюджет: $OUT"
[[ "$OUT" == *"остаток общего бюджета RATE_LIMIT: 40с из 60с"* ]] \
  || fail "26) цепочке обязан остаться реальный остаток (40с из 60с), а не 0с — это и есть дефект #1307: $OUT"
echo "SMOKE(chain): 26) пул делит потолок доли провайдера наравне с цепочкой, не монополизирует бюджет (#1307) — ок"

# ── 27) #1309: элемент без единого id модели — fail loud ДО любого вызова,
# а не «id не подтверждён» на модели "null" (это формально верно, но уводит
# от настоящей причины — опечатки в имени поля).
BROKEN_CHAIN='[{"name":"NO-MODEL","base_url":"https://x.test/v1","secret_env":"PRIMARY_KEY","max_output_tokens":4096}]'
( export DSH_PROVIDER_CHAIN="$BROKEN_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log27.txt"
  if dsh_require_provider_chain >"$LOG" 2>&1; then
    echo "::error::27) элемент без model/models обязан падать громко" >&2; exit 1
  fi
  grep -q "нет ни одного id модели" "$LOG" || { echo "::error::27) сообщение обязано называть причину: $(cat "$LOG")" >&2; exit 1; }
  grep -q "NO-MODEL" "$LOG" || { echo "::error::27) сообщение обязано называть ИМЯ элемента: $(cat "$LOG")" >&2; exit 1; }
) || fail "27) fail loud на элементе без id модели не сработал"
# Обратная сторона: обе валидные формы проходят (иначе «починить» проверку
# можно было бы, запретив список целиком).
( export DSH_PROVIDER_CHAIN="$MULTI_CHAIN"; unset PLUGINS_SUITE_URL 2>/dev/null || true
  dsh_require_provider_chain >/dev/null 2>&1 ) || fail "27) форма {models:[…]} обязана проходить валидацию"
( export DSH_PROVIDER_CHAIN="$CHAIN"; unset PLUGINS_SUITE_URL 2>/dev/null || true
  dsh_require_provider_chain >/dev/null 2>&1 ) || fail "27) прежняя форма {model} обязана проходить валидацию"
echo "SMOKE(chain): 27) элемент без id модели -> fail loud с именем элемента, обе валидные формы проходят (#1309) — ок"

# ── 28) #1309 + #1121: потолок доли бюджета — на ПРОВАЙДЕРА, не на каждую его
# модель. Достижимая последовательность: PRIMARY/dead-model дважды отвечает
# 429 (тратит 20с = весь потолок аккаунта), на третий вызов отдаёт HTTP 410 —
# id снят, и цепочка переходит на ВТОРУЮ модель ТОГО ЖЕ аккаунта. Вторая
# модель в вечном 429: с потолком НА ПРОВАЙДЕРА ей достаётся 0с (аккаунт свою
# долю уже израсходовал), и SECONDARY видит реальные 40с из 60с. Без общего
# счётчика (потолок на каждую модель) вторая модель получила бы ещё 20с, и
# SECONDARY увидел бы 20с — ровно та монополия бюджета, которую закрывал
# #1121, только через новую ось. Мутация исполнена: provider_cap_left →
# provider_wait_cap даёт «20с из 60с», проверка краснеет.
reset_scenario
MULTI_RL_CHAIN='[
  {"name":"PRIMARY","base_url":"https://primary.test/v1","models":["dead-model","also-dead-model"],"secret_env":"PRIMARY_KEY","max_output_tokens":4096},
  {"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"SECONDARY_KEY","max_output_tokens":4096}
]'
SMOKE_MODE_dead_model="rate-limit-then-410"
SMOKE_MODE_also_dead_model="always-rate-limit"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log28.txt"
( export DSH_PROVIDER_CHAIN="$MULTI_RL_CHAIN"
  DSH_RATE_LIMIT_MAX_WAIT_SECS=60 \
  DSH_RATE_LIMIT_INITIAL_DELAY_SECS=10 \
  DSH_RATE_LIMIT_MAX_DELAY_SECS=10 \
  DSH_RATE_LIMIT_PROVIDER_CAP_SECS=20 \
    dsh_run_with_provider_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_CHAIN_PROVIDER" = "SECONDARY" ] || { echo "::error::28) ожидался успех SECONDARY, получено '$DSH_CHAIN_PROVIDER'" >&2; exit 1; }
  [[ "$DSH_CHAIN_MODELS_TRIED" == "PRIMARY/dead-model, PRIMARY/also-dead-model, SECONDARY/secondary-model" ]] \
    || { echo "::error::28) фикстура сломана — обе модели PRIMARY обязаны быть опробованы: '$DSH_CHAIN_MODELS_TRIED'" >&2; exit 1; }
  [[ "$OUT" == *"пробую SECONDARY"*"остаток общего бюджета RATE_LIMIT: 40с из 60с"* ]] \
    || { echo "::error::28) аккаунт PRIMARY обязан потратить не больше ОДНОГО потолка (20с) на ВСЕХ своих кандидатов — SECONDARY ожидал 40с из 60с: $OUT" >&2; exit 1; }
) || fail "28) потолок доли провайдера с несколькими моделями провалился"
echo "SMOKE(chain): 28) потолок доли — на ПРОВАЙДЕРА, а не на каждую его модель (#1309 + #1121) — ок"

# ── 29) #1315, живой инцидент (прогон worker.yml 35046585539, задача #770):
# промпт длиннее предела ядра на ОДИН аргумент execve. dsh принимает задачу
# только позиционным аргументом, поэтому вызов не состоится ни у одного
# провайдера: в живом логе это было восемь «транзиентных отказов» за 78 секунд
# и совет «повтор ИМЕЕТ смысл», который был неправдой. Проверяется: ни одна
# попытка не делается (dsh() не вызван), цепочка НЕ идёт по провайдерам,
# сообщение называет наш отказ и предел.
reset_scenario
SMOKE_MODE_primary_model="ok"
SMOKE_MODE_secondary_model="ok"
LOG="$WORK/log29.txt"
# Промпт БЕЗ ЕДИНОГО ПРОБЕЛА — нарезка (#1318) его не спасает: резать не по
# чему, а по переводу строки нельзя (join вернул бы пробел и изменил текст).
LONG_PROMPT="$(head -c 200000 /dev/zero | tr '\0' 'x')"
DSH_PROMPT_MAX_BYTES=1000 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "$LONG_PROMPT" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" != "0" ] || fail "29) слишком длинный промпт обязан быть отказом, получено rc=$DSH_RUN_RC"
[ "$DSH_RUN_FAILURE_REASON" = "prompt_too_long" ] || fail "29) причина обязана быть prompt_too_long: '$DSH_RUN_FAILURE_REASON'"
[ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || fail "29) цепочка обязана остановиться на ПЕРВОМ провайдере (отказ наш, одинаковый у всех): '$DSH_CHAIN_TRIED'"
[[ "$OUT" != *"попытка 1"* ]] || fail "29) ни одной попытки dsh быть не должно — вызов заведомо не состоится: $OUT"
[[ "$OUT" == *"не режется на аргументы"* ]] || fail "29) сообщение обязано назвать, ПОЧЕМУ нарезка не помогла: $OUT"
[[ "$OUT" == *"join на стороне dsh поставил бы туда пробел"* ]] || fail "29) сообщение обязано назвать, почему нельзя резать по переводу строки (silent-wrong): $OUT"
[[ "$OUT" == *"класс НЕ переключаемый"* ]] || fail "29) отказ обязан быть НЕ переключаемым: следующий провайдер получит тот же промпт: $OUT"
[[ "$OUT" != *"повторить прогон"* ]] || fail "29) совет «повторить» при непоместившемся промпте — ровно та ложь, ради которой сценарий (#1315): $OUT"
echo "SMOKE(chain): 29) промпт длиннее предела execve -> ни одной попытки, наш отказ назван, цепочка не сожжена (#1315) — ок"

# ── 30) #1315, обратная сторона: промпт В ПРЕДЕЛАХ лимита идёт как обычно —
# иначе «починить» сценарий 29 можно было бы, запретив вызовы вовсе.
reset_scenario
SMOKE_MODE_primary_model="ok"
DSH_PROMPT_MAX_BYTES=1000 dsh_run_with_provider_chain "$ANSWER" "$ERR" "короткий промпт"
[ "$DSH_RUN_RC" = "0" ] || fail "30) промпт в пределах лимита обязан пройти как обычно, получено rc=$DSH_RUN_RC"
[ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || fail "30) ожидался обычный успех PRIMARY: '$DSH_CHAIN_PROVIDER'"
echo "SMOKE(chain): 30) промпт в пределах лимита -> обычный путь не задет (#1315) — ок"

# ── 31) #1318, ГЛАВНОЕ свойство нарезки: склейка на стороне dsh обязана вернуть
# исходный промпт БАЙТ-В-БАЙТ. Кормится прод-форма — реальные документы этого
# репозитория, которые воркер и впечатывает в промпт (кириллица по два байта
# на символ, markdown-списки с ведущими дефисами, длинные строки).
REAL_PROMPT="$(cat "$REPO/AGENTS.md" "$REPO/docs/agents/PROTOCOL.md" "$REPO/docs/agents/WORKER-PLAYBOOK.md")"
DSH_PROMPT_CHUNK_BYTES=30000 dsh_prompt_argv_chunks "$REAL_PROMPT"
CHUNKS=${#DSH_PROMPT_ARGV[@]}
[ "$CHUNKS" -gt 1 ] || fail "31) фикстура сломана: реальный промпт обязан резаться больше чем на один кусок (получено $CHUNKS)"
JOINED=$(printf '%s' "${DSH_PROMPT_ARGV[0]}"; for ((ci=1; ci<CHUNKS; ci++)); do printf ' %s' "${DSH_PROMPT_ARGV[ci]}"; done)
[ "$JOINED" = "$REAL_PROMPT" ] \
  || fail "31) join(\" \") НЕ восстановил исходный промпт — нарезка теряет или меняет байты (silent-wrong, #1318)"
for ((ci=0; ci<CHUNKS; ci++)); do
  clen=$(printf '%s' "${DSH_PROMPT_ARGV[ci]}" | LC_ALL=C wc -c)
  [ "$clen" -le 30000 ] || fail "31) кусок $ci длиной $clen превышает предел на один аргумент"
  case "${DSH_PROMPT_ARGV[ci]}" in
    -*) fail "31) кусок $ci начинается с '-' — commander на стороне dsh примет его за неизвестную опцию" ;;
  esac
done
echo "SMOKE(chain): 31) нарезка промпта — склейка join(\" \") байт-в-байт на прод-форме, ни один кусок не длиннее предела и не начинается с '-' (#1318) — ок"

# ── 32) #1318, сквозной путь: длинный промпт С ПРОБЕЛАМИ теперь ПРОХОДИТ.
# До #1318 ровно этот случай ложил конвейер (живые прогоны 35072416907/
# 35074809844/35079314952: «промпт 133199 байт при пределе 126976»). Заглушка
# склеивает полученные аргументы тем же join(" "), что и dsh-headless, и
# сверяет с исходником — то есть проверяется не «не упало», а доехавший текст.
reset_scenario
SEEN_PROMPT="$WORK/seen-prompt.txt"
dsh() {
  case "${1:-}" in
    --profile)
      shift 2
      printf '%s' "$*" >"$SEEN_PROMPT"
      echo "smoke: ответ от $DEEPSEEK_MODEL"
      return 0 ;;
    *) echo "::error::SMOKE(32): dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
  esac
}
export -f dsh
BIG_PROMPT="$REAL_PROMPT"
LOG="$WORK/log32.txt"
DSH_PROMPT_CHUNK_BYTES=30000 \
  dsh_run_with_provider_chain "$ANSWER" "$ERR" "$BIG_PROMPT" >"$LOG" 2>&1
OUT="$(cat "$LOG")"
[ "$DSH_RUN_RC" = "0" ] || fail "32) длинный промпт с пробелами обязан ПРОЙТИ после нарезки, получено rc=$DSH_RUN_RC: $OUT"
[ "$(cat "$SEEN_PROMPT")" = "$BIG_PROMPT" ] \
  || fail "32) до dsh доехал НЕ тот текст: склейка аргументов разошлась с исходным промптом (#1318)"
[[ "$OUT" == *"передан"*"аргументами"* ]] || fail "32) факт нарезки обязан быть виден в логе: $OUT"
echo "SMOKE(chain): 32) длинный промпт проходит цепочку нарезанным, до dsh доезжает исходный текст (#1318) — ок"

# ── 33) #1318: два свойства точки реза сторожатся ПРИЦЕЛЬНО. Сценарии 31/32
# выше их не ловят — на реальном тексте граница куска просто не попадает в
# нужное место, и мутация проходит зелёной (проверено исполнением обеих
# мутаций до написания этого сценария). Поэтому здесь фикстуры построены так,
# чтобы граница попадала ровно на спорный символ.
#
# (а) За пробелом идёт дефис. Резать там нельзя: следующий кусок начнётся с
#     '-', и commander на стороне dsh примет его за неизвестную опцию.
#     Фикстура: 60 байт, единственный «поздний» пробел — перед '-'.
DASH_FIXTURE="$(printf 'aaaaaaaaaa bbbbbbbbbb cccccccccc dddd -список продолжается')"
DSH_PROMPT_CHUNK_BYTES=38 dsh_prompt_argv_chunks "$DASH_FIXTURE"
DN=${#DSH_PROMPT_ARGV[@]}
[ "$DN" -gt 1 ] || fail "33а) фикстура сломана: ожидалась нарезка, получен $DN кусок"
for ((ci=0; ci<DN; ci++)); do
  case "${DSH_PROMPT_ARGV[ci]}" in
    -*) fail "33а) кусок $ci начинается с '-' — рез перед дефисом запрещён (#1318): [${DSH_PROMPT_ARGV[ci]}]" ;;
  esac
done
DJOINED=$(printf '%s' "${DSH_PROMPT_ARGV[0]}"; for ((ci=1; ci<DN; ci++)); do printf ' %s' "${DSH_PROMPT_ARGV[ci]}"; done)
[ "$DJOINED" = "$DASH_FIXTURE" ] || fail "33а) склейка разошлась с исходником"

# (б) Ближе к границе, чем любой пробел, стоит ПЕРЕВОД СТРОКИ. Резать по нему
#     нельзя: join(" ") на стороне dsh поставил бы на его место пробел и тихо
#     изменил текст — ровно silent-wrong. Фикстура: длинное слово, затем \n,
#     затем ещё текст; единственный пробел — заметно раньше перевода строки.
NL_FIXTURE="$(printf 'aa bb\ncccccccccccccccc dd')"
DSH_PROMPT_CHUNK_BYTES=20 dsh_prompt_argv_chunks "$NL_FIXTURE"
NN=${#DSH_PROMPT_ARGV[@]}
[ "$NN" -gt 1 ] || fail "33б) фикстура сломана: ожидалась нарезка, получен $NN кусок"
NJOINED=$(printf '%s' "${DSH_PROMPT_ARGV[0]}"; for ((ci=1; ci<NN; ci++)); do printf ' %s' "${DSH_PROMPT_ARGV[ci]}"; done)
[ "$NJOINED" = "$NL_FIXTURE" ] \
  || fail "33б) склейка НЕ вернула исходник — рез прошёл по переводу строки, join подменил его пробелом (silent-wrong, #1318)"
echo "SMOKE(chain): 33) точка реза: не перед дефисом и не по переводу строки — обе мутации красят именно этот сценарий (#1318) — ок"


echo "SMOKE(chain): все сценарии цепочки провайдеров целы — гвардия класса #727/#737/#743/#857/#877/#880/#1062/#1084/#1121/#1307/#1309 зелёная"
