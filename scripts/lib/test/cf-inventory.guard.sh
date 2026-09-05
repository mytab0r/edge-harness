#!/usr/bin/env bash
# Гвардия класса «инцидент 2026-09-05» (#322, ревью PR #328 находка 3):
# scripts/cf/lib.sh фильтрует account-wide листинги (чужие воркеры/DO
# namespace/bindings-значения не печатаются), но единственным гейтом на
# сам файл был `bash -n` — он не исполняет тело функций и не ловит, если
# кто-то заменит jq-фильтр на `.result` целиком (регресс молча вернул бы
# утечку чужих id/значений в публичный лог). Здесь `cf_get` заглушается
# консервным прод-JSON (9 воркеров в аккаунте, из них 2 наших; 3 DO
# namespace, из них 2 наших — те же числа, что в docs/agents/INFRA-CF.md),
# и проверяется и позитив (свои проходят), и негатив (чужие id/имена/
# значения bindings в stdout не попадают). Снять фикс из lib.sh — тест
# краснеет (проверено: возврат `jq '.'` вместо фильтра ломает assert 2/4/6).
set -euo pipefail

dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../cf" && pwd)"
# shellcheck source=scripts/cf/lib.sh
source "$dir/lib.sh"

export CLOUDFLARE_ACCOUNT_ID="test-acct"
export CLOUDFLARE_API_TOKEN="test-token"

FOREIGN_NAMES="foo-1 foo-2 foo-3 foo-4 foo-5 foo-6 foo-7"
SECRET_TEXT="do-not-print-me"

fail=0

# ── 0: cf_get (реальная функция lib.sh, до заглушки ниже) — HTTP 200 с
# success:false в конверте v4 — отказ, не молчаливая подмена ошибки нулём
# (находка ревью PR #328, находка 3). curl застаблен ЛОКАЛЬНО (function
# перекрывает бинарь только в этом шелле), реальная сеть не нужна.
curl() {
  local out_file="" prev=""
  for arg in "$@"; do
    if [ "$prev" = "-o" ]; then out_file="$arg"; fi
    prev="$arg"
  done
  printf '{"success": false, "errors": [{"message": "test failure"}]}' > "$out_file"
  printf '200'
}
set +e
out=$(cf_get "/user/tokens/verify" 2>/tmp/cf_guard_stderr_success)
rc=$?
set -e
if [ "$rc" = 0 ]; then
  echo "::error::cf_get вернул успех (rc=0) на HTTP 200 с success:false в конверте — должен отказать"
  fail=1
fi
if [ -n "$out" ]; then
  echo "::error::cf_get напечатал тело в stdout при success:false — должен молчать в stdout и писать причину в stderr (получено: $out)"
  fail=1
fi
if ! grep -q "success:false" /tmp/cf_guard_stderr_success; then
  echo "::error::cf_get не назвал причину «success:false» в stderr"
  fail=1
fi
rm -f /tmp/cf_guard_stderr_success
unset -f curl

# cf_get заглушен: возвращает консервный статический JSON по path, не делает
# сетевых запросов. Экспортируется через `export -f`, чтобы функции lib.sh
# (которые вызывают cf_get по имени, не по ссылке) увидели заглушку.
cf_get() {
  case "$1" in
    */workers/scripts)
      cat <<'JSON'
{"result": [
  {"id":"edge-harness"}, {"id":"dsh-edge"},
  {"id":"foo-1"}, {"id":"foo-2"}, {"id":"foo-3"}, {"id":"foo-4"},
  {"id":"foo-5"}, {"id":"foo-6"}, {"id":"foo-7"}
]}
JSON
      ;;
    */workers/durable_objects/namespaces)
      cat <<'JSON'
{"result": [
  {"id":"ns-edge-harness","script":"edge-harness"},
  {"id":"ns-dsh-edge","script":"dsh-edge"},
  {"id":"ns-foreign","script":"foo-1"}
]}
JSON
      ;;
    */workers/scripts/edge-harness/settings)
      printf '{"result": {"bindings": [{"name":"GH_REPO","type":"plain_text","text":"%s"},{"name":"GH_DISPATCH_TOKEN","type":"secret_text"}]}}\n' "$SECRET_TEXT"
      ;;
    */storage/kv/namespaces)
      echo '{"result": [{"id":"kv-1"},{"id":"kv-2"},{"id":"kv-3"}]}'
      ;;
    *)
      echo "cf-inventory.guard.sh: незаглушенный путь $1" >&2
      return 1
      ;;
  esac
}
export -f cf_get

# ── 1/2: cf_workers_own — чужие имена не в stdout, счётчик совпадает ──────
out=$(cf_workers_own 2>/tmp/cf_guard_stderr)
for name in $FOREIGN_NAMES; do
  if grep -q "$name" <<<"$out"; then
    echo "::error::cf_workers_own напечатал чужое имя '$name' в stdout — фильтр CF_OWN_WORKERS сломан"
    fail=1
  fi
done
if ! grep -q '"всего_в_аккаунте": 9' <<<"$out"; then
  echo "::error::cf_workers_own: ожидался счётчик 'всего_в_аккаунте': 9, получено: $out"
  fail=1
fi
if [ -s /tmp/cf_guard_stderr ] && grep -q "ПРЕДУПРЕЖДЕНИЕ" /tmp/cf_guard_stderr; then
  echo "::error::cf_workers_own: allowlist совпадает с фикстурой (2 своих), предупреждения о дрейфе быть не должно"
  fail=1
fi

# ── 3/4: cf_do_namespaces_own — чужой id/script не в stdout ───────────────
out=$(cf_do_namespaces_own 2>/dev/null)
if grep -q "ns-foreign\|foo-1" <<<"$out"; then
  echo "::error::cf_do_namespaces_own напечатал чужой DO namespace в stdout"
  fail=1
fi
if ! grep -q '"всего_в_аккаунте": 3' <<<"$out"; then
  echo "::error::cf_do_namespaces_own: ожидался счётчик 'всего_в_аккаунте': 3, получено: $out"
  fail=1
fi

# ── 5: cf_bindings_names — значение plain_text не в stdout, только имя/тип ─
out=$(cf_bindings_names "/accounts/test-acct/workers/scripts/edge-harness/settings" 2>/dev/null)
if grep -q "$SECRET_TEXT" <<<"$out"; then
  echo "::error::cf_bindings_names напечатал значение plain_text binding в stdout — класс инцидента 2026-09-05 регрессировал"
  fail=1
fi
if ! grep -q '"name": *"GH_REPO"' <<<"$out"; then
  echo "::error::cf_bindings_names: имя biding'а GH_REPO пропало из вывода"
  fail=1
fi

# ── 6: cf_count_only — сырые id account-wide списка не в stdout ──────────
out=$(cf_count_only "/accounts/test-acct/storage/kv/namespaces" 2>/dev/null)
if grep -q "kv-1\|kv-2\|kv-3" <<<"$out"; then
  echo "::error::cf_count_only напечатал сырые id — должен печатать только count"
  fail=1
fi
if ! grep -q '"count": *3' <<<"$out"; then
  echo "::error::cf_count_only: ожидался count 3, получено: $out"
  fail=1
fi

rm -f /tmp/cf_guard_stderr

# ── 7/8: api.sh — отказ по умолчанию вне allowlist, без сети (находка ревью
# PR #328, находка 1). Подставной curl в PATH (не function — api.sh запускается
# отдельным процессом) на случай, если путь всё же дойдёт до cf_get: если
# гвардия регрессирует в блок-лист/пропускает лишнее, тест должен упасть на
# конкретном отказавшем пути, а не тихо съесть реальный сетевой вызов.
api_sh="$dir/api.sh"
curl_stub_dir=$(mktemp -d)
cat >"$curl_stub_dir/curl" <<'CURL_STUB'
#!/usr/bin/env bash
out_file="" prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out_file="$arg"; fi
  prev="$arg"
done
printf '{"success": true, "result": {}}' > "$out_file"
printf '200'
CURL_STUB
chmod +x "$curl_stub_dir/curl"

# 7: путь вне allowlist (account-wide, чужой класс инцидента 2026-09-05) —
# отказ ДО сети, без CF_API_SH_ALLOW_RAW.
if out=$(PATH="$curl_stub_dir:$PATH" bash "$api_sh" "/accounts/test-acct/workers/scripts" 2>&1); then
  echo "::error::api.sh пропустил account-wide путь вне allowlist (должен отказать по умолчанию): $out"
  fail=1
elif ! grep -q "ОТКАЗ" <<<"$out"; then
  echo "::error::api.sh отказал без внятной причины «ОТКАЗ»: $out"
  fail=1
fi

# 8: путь по нашему воркеру (allowlist) — не блокируется гейтом (сеть
# застаблена, дальше curl_stub отвечает success:true пустым result).
if out=$(PATH="$curl_stub_dir:$PATH" bash "$api_sh" "/accounts/test-acct/workers/scripts/edge-harness/deployments" 2>&1); then
  :
else
  if grep -q "ОТКАЗ" <<<"$out"; then
    echo "::error::api.sh отказал на пути по нашему воркеру (должен быть в allowlist): $out"
    fail=1
  fi
fi

rm -rf "$curl_stub_dir"

if [ "$fail" = 0 ]; then
  echo "cf-inventory: фильтры account-wide листингов/bindings, success:false-конверт и allowlist api.sh — чужие id/значения в stdout не попадают, счётчики верны, отказ по умолчанию держится"
fi
exit "$fail"
