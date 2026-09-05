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

section "Воркеры проекта (/workers/scripts, отфильтровано до edge-harness/dsh-edge — аккаунт общий с другими проектами владельца)"
cf_workers_own

section "Последний деплой edge-harness (/workers/scripts/edge-harness/deployments)"
cf_get "/accounts/${acc}/workers/scripts/edge-harness/deployments"

section "Bindings edge-harness — только имена, не значения (/workers/scripts/edge-harness/settings)"
cf_get "/accounts/${acc}/workers/scripts/edge-harness/settings"

section "Bindings dsh-edge — только имена, не значения (/workers/scripts/dsh-edge/settings)"
cf_get "/accounts/${acc}/workers/scripts/dsh-edge/settings"

section "Поддомен workers.dev (/workers/subdomain)"
cf_get "/accounts/${acc}/workers/subdomain"

section "Durable Object namespaces проекта (/durable_objects/namespaces, отфильтровано так же)"
cf_do_namespaces_own

section "KV namespaces — этот проект их не использует (wrangler.jsonc без kv_namespaces), только счётчик аккаунта"
cf_count_only "/accounts/${acc}/storage/kv/namespaces"

section "D1 databases — этот проект их не использует, только счётчик аккаунта"
cf_count_only "/accounts/${acc}/d1/database"

section "R2 buckets — этот проект их не использует, только счётчик аккаунта"
cf_count_only "/accounts/${acc}/r2/buckets"

section "Зоны/домены — только счётчик (домены других проектов не публикуем); issue #289 остаётся открытым вопросом"
cf_count_only "/zones"

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
