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

section "Воркеры проекта (/workers/scripts, отфильтровано до \$CF_OWN_WORKERS — аккаунт общий с другими проектами владельца)"
run cf_workers_own

# Секции ниже — цикл по CF_OWN_WORKERS (lib.sh, единственное место правды),
# а не N хардкоженных секций: третий воркер в allowlist раньше не попадал бы
# в деплои/bindings молча, дрейф ловился только предупреждением lib.sh, а не
# самим составом инвентаря (находка ревью PR #328, п.2).
for worker in $CF_OWN_WORKERS; do
  section "Последний деплой $worker (/workers/scripts/$worker/deployments)"
  run cf_get "/accounts/${acc}/workers/scripts/$worker/deployments"
done

for worker in $CF_OWN_WORKERS; do
  section "Bindings $worker — только имена, не значения (/workers/scripts/$worker/settings)"
  run cf_bindings_names "/accounts/${acc}/workers/scripts/$worker/settings"
done

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
Живой расход DO (rows_read/rows_written) читается через GraphQL Analytics
тем же секретом CLOUDFLARE_API_TOKEN: python scripts/measure/do_rows_read.py
--days N (задача #320; право Account Analytics Read у токена есть — доказано
живыми прогонами 2026-09-05, раздел «Замер факта: rows_read в проде» там же).
Не подтверждено: датасеты requests/GB-s duration — инструмента под них ещё
нет (таблица «Что реально доступно» в docs/agents/INFRA-CF.md).
EOF

echo
echo "Инвентарь завершён: успешно $ok_count / нет доступа $fail_count. Секции с 'НЕТ ДОСТУПА' — недостающее право, см. docs/agents/INFRA-CF.md."
if [ "$ok_count" = 0 ]; then
  echo "ОШИБКА: ни одна секция не выполнилась — прогон не доказывает ничего, токен отозван/протух или сеть недоступна." >&2
  exit 1
fi
