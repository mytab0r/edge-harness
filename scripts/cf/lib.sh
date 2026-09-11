#!/usr/bin/env bash
# Общая обёртка над Cloudflare API v4 для скриптов scripts/cf/.
#
# Почему отдельная обёртка, а не голый curl в каждом скрипте: токен нигде не
# печатается (ни в теле, ни в заголовках ошибки), а при 403/404 скрипт должен
# сказать, какого именно права не хватает — не просто "ошибка".
#
# Секреты только из окружения: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
# Локально их нет — прогон только через workflow_dispatch cf-inventory.yml
# (там секреты репозитория), см. docs/agents/INFRA-CF.md.
set -euo pipefail

cf_require_env() {
  local missing=0
  for name in CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID; do
    if [ -z "${!name:-}" ]; then
      echo "ОШИБКА: переменная $name не задана. Секретов локально нет — прогоняй через .github/workflows/cf-inventory.yml (workflow_dispatch)." >&2
      missing=1
    fi
  done
  [ "$missing" = 0 ]
}

# cf_get <path> — GET https://api.cloudflare.com/client/v4<path>.
# Печатает тело ответа (JSON) в stdout при успехе. При отказе пишет причину
# в stderr и возвращает ненулевой код — вызывающий решает, останавливаться или
# продолжать инвентарь дальше. Только GET: это инвентарь, не панель управления —
# мутирующих запросов (POST/PUT/DELETE) в scripts/cf/ намеренно нет, см.
# docs/agents/INFRA-CF.md, раздел «Границы».
cf_get() {
  local path="$1"
  local url="https://api.cloudflare.com/client/v4${path}"
  local tmp code
  tmp=$(mktemp)
  code=$(curl -sS -o "$tmp" -w '%{http_code}' \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    "$url")
  case "$code" in
    200)
      # HTTP 200 не значит успех: v4-конверт может нести success:false с
      # errors (находка ревью PR #328) — cf_count_only на таком ответе тихо
      # печатал бы "count: 0" через (.result // []), подменяя ошибку честным
      # на вид нулём. jq недоступен здесь же (до сюда ещё никто не проверял) —
      # без него полагаемся на код 200 как раньше, честнее не разгадать нечем.
      if command -v jq >/dev/null 2>&1 && ! jq -e '.success == true' >/dev/null 2>&1 < "$tmp"; then
        echo "ОШИБКА: GET $path вернул HTTP 200, но success:false в конверте." >&2
        cat "$tmp" >&2; rm -f "$tmp"
        return 1
      fi
      cat "$tmp"; rm -f "$tmp"
      ;;
    403)
      echo "НЕТ ДОСТУПА: 403 на GET $path — токену не хватает права на эту область." >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
    404)
      echo "НЕ НАЙДЕНО: 404 на GET $path — ресурс отсутствует или путь неверен." >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
    *)
      echo "ОШИБКА: GET $path вернул HTTP $code" >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
  esac
}

# Проект, за который отвечает этот репозиторий, — ровно два воркера
# (cf-worker/wrangler.jsonc: "edge-harness"; deploy-dsh-edge.yml: "dsh-edge").
# CLOUDFLARE_API_TOKEN/ACCOUNT_ID общие на весь аккаунт владельца, где есть
# И ДРУГИЕ, не относящиеся к этому репозиторию проекты. Их имена — не наше
# дело и не для публичного лога/доки: инцидент 2026-09-05, первый прогон
# cf-inventory напечатал чужие имена воркеров в публичный Actions-лог этого
# репозитория (лог удалён, класс закрыт здесь — фильтром на API, а не один раз).
CF_OWN_WORKERS="edge-harness dsh-edge"

# cf_count_only <path> — для account-wide списков (KV/D1/R2/зоны и т.п.):
# печатает ТОЛЬКО количество через jq, никогда не печатает сырые имена/id —
# они могут принадлежать чужим проектам в том же аккаунте.
cf_count_only() {
  local path="$1" resp
  resp=$(cf_get "$path") || return 1
  if ! command -v jq >/dev/null 2>&1; then
    echo "ОШИБКА: jq недоступен — не печатаю account-wide список без фильтра (чужие проекты в том же аккаунте)." >&2
    return 1
  fi
  echo "$resp" | jq '{count: (.result | length)}'
}

# cf_workers_own — /workers/scripts отфильтрован до CF_OWN_WORKERS: печатает
# детали только по нашим двум воркерам, остальные — числом, без имён.
cf_workers_own() {
  local resp
  resp=$(cf_get "/accounts/${CLOUDFLARE_ACCOUNT_ID}/workers/scripts") || return 1
  if ! command -v jq >/dev/null 2>&1; then
    echo "ОШИБКА: jq недоступен — не печатаю account-wide список без фильтра (чужие проекты в том же аккаунте)." >&2
    return 1
  fi
  local allow_json expect own_count
  allow_json=$(printf '%s\n' $CF_OWN_WORKERS | jq -R . | jq -s .)
  expect=$(printf '%s\n' $CF_OWN_WORKERS | wc -w)
  # jq не принимает не-ASCII ключи как bare-идентификаторы в {key: ...} —
  # только строковые литералы в кавычках.
  echo "$resp" | jq --argjson allow "$allow_json" '
    (.result // []) as $all
    | ($all | map(select(.id as $id | $allow | index($id))
        | {id, modified_on, last_deployed_from, compatibility_date, usage_model})) as $own
    | {"свои": $own, "всего_в_аккаунте": ($all|length), "чужих_не_показано": (($all|length)-($own|length))}'
  own_count=$(echo "$resp" | jq --argjson allow "$allow_json" \
    '[(.result // [])[] | select(.id as $id | $allow | index($id))] | length')
  if [ "$own_count" != "$expect" ]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: CF_OWN_WORKERS перечисляет $expect воркеров, а найдено $own_count — allowlist разошёлся со списком в аккаунте, инвентарь может лгать (см. CF_OWN_WORKERS в lib.sh)." >&2
  fi
}

# cf_bindings_names <path> — для /workers/scripts/<name>/settings: печатает
# ТОЛЬКО имя и тип каждого binding, никогда не печатает поле "text" — на
# /settings оно приходит для plain_text bindings и это уже значение, не имя.
# "Только имена" в заголовках status.sh и в docs/agents/INFRA-CF.md было
# правдой для листингов (cf_workers_own и т.п.), но не для этого эндпоинта —
# без фильтра сырой JSON с значениями plain_text утёк бы в публичный лог.
cf_bindings_names() {
  local path="$1" resp
  resp=$(cf_get "$path") || return 1
  if ! command -v jq >/dev/null 2>&1; then
    echo "ОШИБКА: jq недоступен — не печатаю settings без фильтра (могут быть значения bindings)." >&2
    return 1
  fi
  echo "$resp" | jq '{bindings: [.result.bindings[]? | {name, type}]}'
}

# cf_do_namespaces_own — список DO namespaces отфильтрован по script из
# CF_OWN_WORKERS тем же способом. Путь под /workers/, не под корнем аккаунта
# (реальный путь проверен прогоном — плоский /durable_objects/namespaces
# отдаёт 7003 "No route for that URI").
cf_do_namespaces_own() {
  local resp
  resp=$(cf_get "/accounts/${CLOUDFLARE_ACCOUNT_ID}/workers/durable_objects/namespaces") || return 1
  if ! command -v jq >/dev/null 2>&1; then
    echo "ОШИБКА: jq недоступен — не печатаю account-wide список без фильтра (чужие проекты в том же аккаунте)." >&2
    return 1
  fi
  local allow_json expect own_count
  allow_json=$(printf '%s\n' $CF_OWN_WORKERS | jq -R . | jq -s .)
  expect=$(printf '%s\n' $CF_OWN_WORKERS | wc -w)
  echo "$resp" | jq --argjson allow "$allow_json" '
    (.result // []) as $all
    | ($all | map(select(.script as $s | $allow | index($s)))) as $own
    | {"свои": $own, "всего_в_аккаунте": ($all|length), "чужих_не_показано": (($all|length)-($own|length))}'
  own_count=$(echo "$resp" | jq --argjson allow "$allow_json" \
    '[(.result // [])[] | select(.script as $s | $allow | index($s))] | length')
  if [ "$own_count" != "$expect" ]; then
    echo "ПРЕДУПРЕЖДЕНИЕ: CF_OWN_WORKERS перечисляет $expect воркеров, а найдено $own_count DO-namespace'ов с нашим script — на момент написания это 1:1 (по одному классу на воркер); расхождение может быть и дрейфом allowlist, и новым вторым классом — проверь CF_OWN_WORKERS в lib.sh." >&2
  fi
}
