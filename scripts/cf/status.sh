#!/usr/bin/env bash
# Инвентарь возможностей Cloudflare-аккаунта: что развёрнуто, какие права у
# токена. Каждая секция печатает JSON-ответ при успехе и явную причину при
# отказе ("НЕТ ДОСТУПА: ...") — секция не молчит и не роняет весь прогон.
#
# Запуск: только там, где есть секреты (workflow_dispatch cf-inventory.yml).
# Локально CLOUDFLARE_API_TOKEN/CLOUDFLARE_ACCOUNT_ID не заданы — cf_require_env
# остановит скрипт с понятной инструкцией.
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cf/lib.sh
source "$dir/lib.sh"
cf_require_env || exit 1
set +e  # каждая секция сама сообщает об отказе; одна секция не должна ронять весь инвентарь

acc="$CLOUDFLARE_ACCOUNT_ID"
section() { echo; echo "== $1 =="; }

section "Токен: что он может (/user/tokens/verify)"
cf_get "/user/tokens/verify"

section "Воркеры аккаунта (/accounts/{id}/workers/scripts)"
cf_get "/accounts/${acc}/workers/scripts"

section "Последний деплой edge-harness (/workers/scripts/edge-harness/deployments)"
cf_get "/accounts/${acc}/workers/scripts/edge-harness/deployments"

section "Bindings edge-harness — только имена, не значения (/workers/scripts/edge-harness/settings)"
cf_get "/accounts/${acc}/workers/scripts/edge-harness/settings"

section "Поддомен workers.dev (/workers/subdomain)"
cf_get "/accounts/${acc}/workers/subdomain"

section "Durable Object namespaces (/durable_objects/namespaces)"
cf_get "/accounts/${acc}/durable_objects/namespaces"

section "KV namespaces (/storage/kv/namespaces)"
cf_get "/accounts/${acc}/storage/kv/namespaces"

section "D1 databases (/d1/database)"
cf_get "/accounts/${acc}/d1/database"

section "R2 buckets (/r2/buckets)"
cf_get "/accounts/${acc}/r2/buckets"

section "Зоны/домены (/zones) — требует Zone:Read, у аккаунта может не быть своей зоны (issue #289)"
cf_get "/zones"

section "Расход по квотам"
cat <<'EOF'
Статические лимиты Free — docs/research/20-cloudflare-free.md.
Живой расход (requests/duration/rows_read и т.п.) простым GET не отдаётся —
только через GraphQL Analytics API (POST /client/v4/graphql), которому нужно
отдельное право Account Analytics; здесь намеренно не реализовано (см.
docs/agents/INFRA-CF.md, раздел "Расход по квотам"), чтобы не гадать со
схемой GraphQL вслепую. Фактический расход rows_read по DO собирает #320
(scripts/measure/) изнутри самого Durable Object — это дополняет, не дублирует.
EOF

echo
echo "Инвентарь завершён. Секции с 'НЕТ ДОСТУПА' — недостающее право, см. docs/agents/INFRA-CF.md."
