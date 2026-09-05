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
