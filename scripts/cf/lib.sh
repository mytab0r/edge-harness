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
# продолжать инвентарь дальше.
cf_get() {
  local path="$1"
  _cf_request GET "$path" ""
}

# cf_post <path> <json-body> — POST с тем же контрактом, что и cf_get.
cf_post() {
  local path="$1" body="$2"
  _cf_request POST "$path" "$body"
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
  local allow_json
  allow_json=$(printf '%s\n' $CF_OWN_WORKERS | jq -R . | jq -s .)
  echo "$resp" | jq --argjson allow "$allow_json" '
    (.result // []) as $all
    | ($all | map(select(.id as $id | $allow | index($id))
        | {id, modified_on, last_deployed_from, compatibility_date, usage_model})) as $own
    | {свои: $own, всего_в_аккаунте: ($all|length), чужих_не_показано: (($all|length)-($own|length))}'
}

# cf_do_namespaces_own — /durable_objects/namespaces отфильтрован по script
# из CF_OWN_WORKERS тем же способом.
cf_do_namespaces_own() {
  local resp
  resp=$(cf_get "/accounts/${CLOUDFLARE_ACCOUNT_ID}/durable_objects/namespaces") || return 1
  if ! command -v jq >/dev/null 2>&1; then
    echo "ОШИБКА: jq недоступен — не печатаю account-wide список без фильтра (чужие проекты в том же аккаунте)." >&2
    return 1
  fi
  local allow_json
  allow_json=$(printf '%s\n' $CF_OWN_WORKERS | jq -R . | jq -s .)
  echo "$resp" | jq --argjson allow "$allow_json" '
    (.result // []) as $all
    | ($all | map(select(.script as $s | $allow | index($s)))) as $own
    | {свои: $own, всего_в_аккаунте: ($all|length), чужих_не_показано: (($all|length)-($own|length))}'
}

_cf_request() {
  local method="$1" path="$2" body="$3"
  local url="https://api.cloudflare.com/client/v4${path}"
  local tmp code
  tmp=$(mktemp)
  if [ -n "$body" ]; then
    code=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      -H "Content-Type: application/json" \
      --data "$body" "$url")
  else
    code=$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" \
      -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
      "$url")
  fi
  case "$code" in
    200)
      cat "$tmp"; rm -f "$tmp"
      ;;
    403)
      echo "НЕТ ДОСТУПА: 403 на $method $path — токену не хватает права на эту область." >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
    404)
      echo "НЕ НАЙДЕНО: 404 на $method $path — ресурс отсутствует или путь неверен." >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
    *)
      echo "ОШИБКА: $method $path вернул HTTP $code" >&2
      cat "$tmp" >&2; rm -f "$tmp"
      return 1
      ;;
  esac
}
