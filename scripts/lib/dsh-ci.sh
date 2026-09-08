#!/usr/bin/env bash
# Общая CI-механика DSH: установка tarball'ами с пином целостности, GLM-патч
# профиля, маскирование секретов. Единственное место правды — используется и
# руками (scripts/hands/dsh_task.sh), и автономным воркером (scripts/worker/task.sh).
# Обоснование механики и «почему так» — docs/research/10-dsh-architecture.md,
# слайс 1 (#48), живые прогоны 2026-08-30.
#
# Подключение: source "$(dirname "${BASH_SOURCE[0]}")/../lib/dsh-ci.sh"
# Рассчитано на bash с set -euo pipefail (источник задаёт).

# Пины версий и целостности (integrity = dist.integrity из metadata реестра),
# сверяются с фактически скачанным tarball'ом — несовпадение это громкий отказ,
# а не warning. Пакеты ставятся ТОЛЬКО tarball'ами: `npm install <pkg>` даёт 404.
# 0.0.1-rc.1 намеренно НЕ используется: тянет @deepseek-ai/dsh-code-runtime-worker,
# который в публичном npm отсутствует (tarball 404); с 0.0.1-rc.3 зависимость —
# dsh-code-runtime-worker-thread, она опубликована (проверено установкой, 475 пакетов).
DSH_VERSION="0.1.1-rc.2"
DSH_INTEGRITY="sha512-UP1UIh6q3Gme/yXRn/QL2P8IsVlv8Shpg22TRJIZPsCRWLm4CBiA1MUvXmJAfsOEETBMLAl+xWPtFw6ICsN3wg=="
DSH_HEADLESS_VERSION="0.1.1-rc.2"
DSH_HEADLESS_INTEGRITY="sha512-Pk50xwmUUehOxNe8DJ2/tThj7Aw1MmJQeUkfAQh9miF7Tm+WOOxiOOei/H4wjH9cf+FuqtbLDw6jrHmGotfhjw=="

# Провайдер и модель — ровно одно место правды: vars.DEEPSEEK_BASE_URL /
# vars.DEEPSEEK_MODEL репозитория (#153). Зашитых фолбэков на конкретный
# эндпоинт/модель в коде больше нет нигде — их отсутствие обязано падать
# громко здесь, до любой дорогой работы (установка DSH, клоны, сессия морды),
# а не молча подставлять чужого провайдера. Разные причины — разные сообщения.
dsh_require_provider_env() {
  local missing=0
  if [ -z "${DEEPSEEK_BASE_URL:-}" ]; then
    echo "::error::не задан vars.DEEPSEEK_BASE_URL — эндпоинт провайдера объявляется только в vars репозитория, зашитых дефолтов больше нет" >&2
    missing=1
  fi
  if [ -z "${DEEPSEEK_MODEL:-}" ]; then
    echo "::error::не задан vars.DEEPSEEK_MODEL — модель объявляется только в vars репозитория, зашитых дефолтов больше нет" >&2
    missing=1
  fi
  if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    # Значение сюда попадает из secrets[vars.DSH_PROVIDER_KEY_SECRET] на
    # уровне workflow (#716, docs/runbooks/switch-llm-provider.md) — сама
    # переменная окружения здесь не переименована, только источник значения
    # у вызывающего workflow-файла.
    echo "::error::DEEPSEEK_API_KEY не задан — DSH не сможет вызвать модель" >&2
    missing=1
  fi
  [ "$missing" = 0 ]
}

# GH маскирует секреты только в своих логах; всё, что уходит наружу (журнал DO,
# комментарии в задачах, Telegram), надо затирать до отправки. GitHub PAT
# (#95: токен теперь живёт и в морде — GH_RUNNER_TOKEN) маскируется
# производным паттерном формы токена: точное совпадение секрета GH покрывает
# только внутри своих логов.
redact() {
  sed -E -e 's/nvapi-[A-Za-z0-9_-]{4,}/nvapi-[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}/\1sk-[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_])ghp_[A-Za-z0-9]{20,}/\1ghp_[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}/\1github_pat_[REDACTED]/g'
}

dsh_verify_integrity() { # file expected-integrity
  local actual
  actual="sha512-$(openssl dgst -sha512 -binary "$1" | openssl base64 -A)"
  if [ "$actual" != "$2" ]; then
    echo "::error::Integrity mismatch: $1 (ожидался $2, получен $actual)" >&2
    return 1
  fi
}

dsh_install() { # $1 — рабочий каталог для tarball'ов (создаётся)
  local pkgs=$1
  mkdir -p "$pkgs"
  (
    cd "$pkgs" || exit 1
    npm pack "@deepseek-ai/dsh@$DSH_VERSION" "@deepseek-ai/dsh-headless@$DSH_HEADLESS_VERSION"
    local dsh_tgz="deepseek-ai-dsh-$DSH_VERSION.tgz"
    local hl_tgz="deepseek-ai-dsh-headless-$DSH_HEADLESS_VERSION.tgz"
    [ -f "$dsh_tgz" ] || dsh_tgz=$(find . -maxdepth 1 -name "*dsh-$DSH_VERSION.tgz" | head -1)
    [ -f "$hl_tgz" ] || hl_tgz=$(find . -maxdepth 1 -name "*dsh-headless-$DSH_HEADLESS_VERSION.tgz" | head -1)
    dsh_verify_integrity "$dsh_tgz" "$DSH_INTEGRITY"
    dsh_verify_integrity "$hl_tgz" "$DSH_HEADLESS_INTEGRITY"
    npm install -g ./*.tgz
  )
  command -v dsh >/dev/null
}

# Выбор модели и лимит ответа — через родной settings-слой профиля, НЕ env:
# адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
dsh_patch_profile() { # $1 — имя профиля (обычно headless); выставляет DSH_MODEL/DSH_MAX_TOKENS
  local profile=$1
  # Модель обязана прийти из окружения (vars.DEEPSEEK_MODEL, #153) — здесь
  # больше нет зашитого дефолта. Вызывающий обязан вызвать
  # dsh_require_provider_env раньше и упасть громко, если модель не задана.
  : "${DEEPSEEK_MODEL:?DEEPSEEK_MODEL не задан — dsh_require_provider_env должен был отказать раньше}"
  DSH_MODEL="$DEEPSEEK_MODEL"
  DSH_MAX_TOKENS="${DSH_MAX_TOKENS:-131072}"
  local patch="$HOME/.dsh/profiles/$profile/cordis.patch.yml"
  mkdir -p "$(dirname "$patch")"
  cat >"$patch" <<PATCH
- id: agent-default-model
  config:
    provider: deepseek-official
    model: $DSH_MODEL
- id: llm-deepseek
  config:
    maxTokens: $DSH_MAX_TOKENS
PATCH
}

# Ретрай временного RATE_LIMIT провайдера вокруг ОДНОГО прогона dsh (#422,
# наследует механизм #419/#421 — там он появился только у ai-review, хотя
# первым от лимита падает автономный воркер: живой факт — прогоны worker.yml
# 34007508064/34006554580 упали с «dsh: RATE_LIMIT: Rate limit reached for
# requests», ai-review в тот момент ретраить ещё не умел вовсе). Единственное
# место правды: ai_dsh.sh (ревью), worker/task.sh, hands/dsh_task.sh зовут эту
# функцию вместо копии цикла — три расходящиеся копии дороже одной.
#
# Классификация по тексту stderr (docs/runbooks/switch-llm-provider.md):
#   «RATE_LIMIT: Weekly/Monthly Limit Exhausted…» — квота на дни, ретраить
#     внутри прогона бессмысленно — падаем сразу (failure_reason=quota_exhausted).
#   «RATE_LIMIT: …» без этой формы — короткое окно, снимается ожиданием:
#     экспоненциальная пауза с потолком и общим бюджетом ожидания.
#   Нет строки RATE_LIMIT вовсе — настоящая ошибка (ключ/модель/битый
#     запрос/сеть) — падаем сразу, как и раньше.
#
# Использование:
#   dsh_run_with_retry <answer_file> <err_file> <prompt_text>
# Настройки — через env (у каждого вызывающего свой бюджет и обоснование,
# см. worker.yml/hands.yml/ai-review.yml):
#   DSH_TIMEOUT_SECS               — таймаут КАЖДОЙ попытки (вызывающий уже задаёт)
#   DSH_RATE_LIMIT_MAX_WAIT_SECS   — суммарный бюджет ожидания (по умолчанию 1800 — 30 мин)
#   DSH_RATE_LIMIT_INITIAL_DELAY_SECS / DSH_RATE_LIMIT_MAX_DELAY_SECS — старт/потолок паузы
# Результат (переменные, не stdout — вызывающий печатает свой отчёт):
#   DSH_RUN_RC              — код возврата ПОСЛЕДНЕЙ попытки dsh
#   DSH_RUN_FAILURE_REASON  — "" (успех/обычный транспортный отказ) |
#                             quota_exhausted | rate_limit_retry_budget_exceeded
dsh_run_with_retry() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  local max_wait="${DSH_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
  local delay="${DSH_RATE_LIMIT_INITIAL_DELAY_SECS:-30}"
  local max_delay="${DSH_RATE_LIMIT_MAX_DELAY_SECS:-300}"
  local timeout_secs="${DSH_TIMEOUT_SECS:-3600}"
  local waited=0 attempt=1 wait_left rc
  DSH_RUN_FAILURE_REASON=""
  while :; do
    echo "dsh: попытка $attempt (суммарно уже ждал ${waited}с из бюджета ${max_wait}с)"
    set +e
    timeout "$timeout_secs" dsh --profile headless "$prompt_text" \
      >"$answer_file" 2>"$err_file"
    rc=$?
    set -e
    echo "dsh завершился с кодом $rc (попытка $attempt)"

    [ "$rc" -eq 0 ] && break

    if grep -q 'RATE_LIMIT: Weekly/Monthly Limit Exhausted' "$err_file"; then
      DSH_RUN_FAILURE_REASON="quota_exhausted"
      echo "::warning::квота провайдера исчерпана надолго (Weekly/Monthly) — повтор внутри прогона бессмысленен, падаю сразу (docs/runbooks/switch-llm-provider.md)"
      break
    fi

    if ! grep -q 'RATE_LIMIT:' "$err_file"; then
      # Настоящая ошибка провайдера/транспорта (ключ, модель, битый запрос,
      # сеть) — ждать её повтором нет смысла, падаем сразу, как и раньше.
      break
    fi

    wait_left=$((max_wait - waited))
    if [ "$wait_left" -le 0 ]; then
      DSH_RUN_FAILURE_REASON="rate_limit_retry_budget_exceeded"
      echo "::warning::бюджет ожидания временного RATE_LIMIT (${max_wait}с) исчерпан — сдаюсь"
      break
    fi
    [ "$delay" -gt "$wait_left" ] && delay=$wait_left
    echo "::warning::временный RATE_LIMIT провайдера — жду ${delay}с и повторяю (в сумме ждал ${waited}с)"
    sleep "$delay"
    waited=$((waited + delay))
    delay=$((delay * 2))
    [ "$delay" -gt "$max_delay" ] && delay=$max_delay
    attempt=$((attempt + 1))
  done
  DSH_RUN_RC=$rc
}
