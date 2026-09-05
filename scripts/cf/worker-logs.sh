#!/usr/bin/env bash
# Проверка: доступны ли логи воркера за прошедшее окно через REST-API (не
# live-tail). Cloudflare отдаёт исторические логи только через Logpush-джобы
# (данные летят во внешнее хранилище, настраиваются заранее) — простого
# "GET логи за последний час" в API нет. Этот скрипт проверяет, настроен ли
# хоть один Logpush-джоб, и честно фиксирует факт, а не гадает.
#
# Живые логи в моменте — только `wrangler tail` (WebSocket, интерактивно,
# не подходит для CI-прогона и не читает прошлое).
set -euo pipefail
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cf/lib.sh
source "$dir/lib.sh"
cf_require_env || exit 1

echo "== Logpush jobs (/accounts/{id}/logpush/jobs) =="
if cf_get "/accounts/${CLOUDFLARE_ACCOUNT_ID}/logpush/jobs"; then
  echo
  echo "Пусто/список выше = логи воркера НЕ архивируются нигде: окно для чтения нет."
  echo "Живые логи в моменте: 'npx wrangler tail edge-harness' из cf-worker/ (нужен CLOUDFLARE_API_TOKEN, интерактивно, не для CI)."
else
  echo
  echo "НЕТ ДОСТУПА к списку Logpush-джобов — токену не хватает права Logs Read, или Logpush недоступен на текущем плане."
fi
