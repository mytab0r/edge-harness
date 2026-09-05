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
ok_count=0
fail_count=0
section() { echo; echo "== $1 =="; }
# Агрегат вместо голого exit 0 в конце (находка ревью PR #328): при отозванном
# токене каждая секция сама честно пишет «НЕТ ДОСТУПА», но без счётчика
# скрипт всё равно печатает «Инвентарь завершён» и выходит 0 — зелёный
# «прогон-доказательство» без единого факта. run() запускает секцию и считает
# исход; при нуле успешных из всех секций — жёсткий exit 1 в конце (одиночный
# отказ при живом остальном инвентаре остаётся некритичным).
run() { if "$@"; then ok_count=$((ok_count + 1)); else fail_count=$((fail_count + 1)); fi; }

section "Токен: что он может (/user/tokens/verify)"
run cf_get "/user/tokens/verify"

section "Воркеры проекта (/workers/scripts, отфильтровано до edge-harness/dsh-edge — аккаунт общий с другими проектами владельца)"
run cf_workers_own

section "Последний деплой edge-harness (/workers/scripts/edge-harness/deployments)"
run cf_get "/accounts/${acc}/workers/scripts/edge-harness/deployments"

section "Последний деплой dsh-edge (/workers/scripts/dsh-edge/deployments)"
run cf_get "/accounts/${acc}/workers/scripts/dsh-edge/deployments"

section "Bindings edge-harness — только имена, не значения (/workers/scripts/edge-harness/settings)"
run cf_bindings_names "/accounts/${acc}/workers/scripts/edge-harness/settings"

section "Bindings dsh-edge — только имена, не значения (/workers/scripts/dsh-edge/settings)"
run cf_bindings_names "/accounts/${acc}/workers/scripts/dsh-edge/settings"

section "Поддомен workers.dev (/workers/subdomain)"
run cf_get "/accounts/${acc}/workers/subdomain"

section "Durable Object namespaces проекта (/durable_objects/namespaces, отфильтровано так же)"
run cf_do_namespaces_own

section "KV namespaces — этот проект их не использует (wrangler.jsonc без kv_namespaces), только счётчик аккаунта"
run cf_count_only "/accounts/${acc}/storage/kv/namespaces"

section "D1 databases — этот проект их не использует, только счётчик аккаунта"
run cf_count_only "/accounts/${acc}/d1/database"

section "R2 buckets — этот проект их не использует, только счётчик аккаунта"
run cf_count_only "/accounts/${acc}/r2/buckets"

section "Зоны/домены — только счётчик (домены других проектов не публикуем); 0 зон закрывает issue #289, см. docs/research/20-cloudflare-free.md"
run cf_count_only "/zones"

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
echo "Инвентарь завершён: успешно $ok_count / нет доступа $fail_count. Секции с 'НЕТ ДОСТУПА' — недостающее право, см. docs/agents/INFRA-CF.md."
if [ "$ok_count" = 0 ]; then
  echo "ОШИБКА: ни одна секция не выполнилась — прогон не доказывает ничего, токен отозван/протух или сеть недоступна." >&2
  exit 1
fi
