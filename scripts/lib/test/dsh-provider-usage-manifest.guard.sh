#!/usr/bin/env bash
# Гвардия манифеста использования (openspec/changes/llm-provider-usage-manifest):
# dsh_require_provider_chain "<consumer_id>" резолвит DSH_PROVIDER_CHAIN из
# config/provider-usage.json ПЕРЕД обычной валидацией — приоритетнее
# vars.DSH_PROVIDER_CHAIN, если манифест присутствует; отсутствие записи
# потребителя/несуществующая или пустая цепочка — fail loud (класс #727->#797:
# «у кого-то нет валидного назначения» обязано падать здесь, не только в
# репо-инварианте). Файла нет вовсе — тихий фоллбэк на прежний
# vars.DSH_PROVIDER_CHAIN (переходный период, design.md «Потребители»).
# Вызов БЕЗ consumer_id (старые смок-тесты) манифест не трогает вовсе —
# обратная совместимость доказывается тестом 6 ниже.
#
# Запуск: bash scripts/lib/test/dsh-provider-usage-manifest.guard.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::GUARD(provider-usage-manifest): $*" >&2; exit 1; }

export HOME="$(mktemp -d)"
WORK="$(mktemp -d)"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

export DEEPSEEK_API_KEY="test-key"
FALLBACK_CHAIN='[{"name":"FALLBACK","base_url":"https://fallback.test/v1","model":"fallback-model","secret_env":"DEEPSEEK_API_KEY","max_output_tokens":4096}]'

write_manifest() { # path json
  printf '%s' "$2" >"$1"
}

# ── 1) Файла нет вовсе — тихий фоллбэк на vars.DSH_PROVIDER_CHAIN. ──────────
(
  export DSH_PROVIDER_USAGE_MANIFEST="$WORK/no-such-file.json"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  dsh_require_provider_chain "worker" || { echo "::error::1) без файла манифеста ожидался тихий фоллбэк" >&2; exit 1; }
  [ "$DSH_PROVIDER_CHAIN" = "$FALLBACK_CHAIN" ] || { echo "::error::1) DSH_PROVIDER_CHAIN не должен был измениться: $DSH_PROVIDER_CHAIN" >&2; exit 1; }
) || fail "1) фоллбэк на отсутствие файла не сработал"
echo "GUARD(provider-usage-manifest): 1) манифеста нет -> тихий фоллбэк на vars — ок"

# ── 2) Манифест валиден — переопределяет DSH_PROVIDER_CHAIN, даже если vars
#      несла ДРУГОЕ значение (манифест приоритетнее). ───────────────────────
MANIFEST_CHAIN='[{"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"DEEPSEEK_API_KEY","max_output_tokens":8192},{"name":"SECONDARY","base_url":"https://secondary.test/v1","model":"secondary-model","secret_env":"DEEPSEEK_API_KEY","max_output_tokens":8192}]'
(
  MJSON="$WORK/valid.json"
  write_manifest "$MJSON" "{\"chains\":{\"tasks-combo\":$MANIFEST_CHAIN},\"usage\":{\"worker\":\"tasks-combo\"}}"
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log2.txt"
  dsh_require_provider_chain "worker" >"$LOG" 2>&1 || { echo "::error::2) валидный манифест обязан был пройти: $(cat "$LOG")" >&2; exit 1; }
  [ "$(jq -c . <<<"$DSH_PROVIDER_CHAIN")" = "$(jq -c . <<<"$MANIFEST_CHAIN")" ] || {
    echo "::error::2) DSH_PROVIDER_CHAIN обязан был стать цепочкой из манифеста, а не vars: $DSH_PROVIDER_CHAIN" >&2; exit 1; }
  grep -q "tasks-combo" "$LOG" || { echo "::error::2) сообщение обязано называть имя цепочки: $(cat "$LOG")" >&2; exit 1; }
) || fail "2) манифест не переопределил vars"
echo "GUARD(provider-usage-manifest): 2) валидный манифест -> приоритетнее vars — ок"

# ── 3) Манифест есть, но нет записи потребителя — fail loud, не фоллбэк. ────
(
  MJSON="$WORK/no-consumer.json"
  write_manifest "$MJSON" "{\"chains\":{\"tasks-combo\":$MANIFEST_CHAIN},\"usage\":{\"worker\":\"tasks-combo\"}}"
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log3.txt"
  if dsh_require_provider_chain "hands" >"$LOG" 2>&1; then
    echo "::error::3) отсутствие записи потребителя обязано падать громко" >&2; exit 1
  fi
  grep -qi "hands" "$LOG" || { echo "::error::3) сообщение обязано называть потребителя: $(cat "$LOG")" >&2; exit 1; }
) || fail "3) fail loud на отсутствии записи потребителя не сработал"
echo "GUARD(provider-usage-manifest): 3) манифест без записи потребителя -> fail loud — ок"

# ── 4) Манифест ссылается на несуществующую цепочку — fail loud. ───────────
(
  MJSON="$WORK/dangling.json"
  write_manifest "$MJSON" '{"chains":{"tasks-combo":[]},"usage":{"worker":"ghost-chain"}}'
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log4.txt"
  if dsh_require_provider_chain "worker" >"$LOG" 2>&1; then
    echo "::error::4) ссылка на несуществующую цепочку обязана падать громко" >&2; exit 1
  fi
  grep -qi "ghost-chain" "$LOG" || { echo "::error::4) сообщение обязано называть имя цепочки-призрака: $(cat "$LOG")" >&2; exit 1; }
) || fail "4) fail loud на несуществующей цепочке не сработал"
echo "GUARD(provider-usage-manifest): 4) манифест с несуществующей цепочкой -> fail loud — ок"

# ── 5) Цепочка существует, но пуста — fail loud. ────────────────────────────
(
  MJSON="$WORK/empty-chain.json"
  write_manifest "$MJSON" '{"chains":{"tasks-combo":[]},"usage":{"worker":"tasks-combo"}}'
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log5.txt"
  if dsh_require_provider_chain "worker" >"$LOG" 2>&1; then
    echo "::error::5) пустая цепочка обязана падать громко" >&2; exit 1
  fi
  grep -qi "пуста" "$LOG" || { echo "::error::5) сообщение обязано называть факт пустоты: $(cat "$LOG")" >&2; exit 1; }
) || fail "5) fail loud на пустой цепочке не сработал"
echo "GUARD(provider-usage-manifest): 5) манифест с пустой цепочкой -> fail loud — ок"

# ── 6) Обратная совместимость: вызов БЕЗ consumer_id манифест не трогает,
#      даже если валидный файл присутствует и вызывающий не передал бы своего
#      id (старые смок-тесты dsh-provider-chain.smoke.sh не обязаны знать про
#      манифест). ───────────────────────────────────────────────────────────
(
  MJSON="$WORK/present-but-unused.json"
  write_manifest "$MJSON" "{\"chains\":{\"tasks-combo\":$MANIFEST_CHAIN},\"usage\":{\"worker\":\"tasks-combo\"}}"
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  dsh_require_provider_chain || { echo "::error::6) вызов без consumer_id обязан пройти на vars как раньше" >&2; exit 1; }
  [ "$DSH_PROVIDER_CHAIN" = "$FALLBACK_CHAIN" ] || { echo "::error::6) вызов без consumer_id не должен трогать DSH_PROVIDER_CHAIN: $DSH_PROVIDER_CHAIN" >&2; exit 1; }
) || fail "6) обратная совместимость (вызов без id) сломана"
echo "GUARD(provider-usage-manifest): 6) вызов без consumer_id -> манифест не тронут (обратная совместимость) — ок"

# ── 7) Манифест — невалидный JSON — fail loud, не молчаливый парс-крах. ────
(
  MJSON="$WORK/broken.json"
  printf '{not json' >"$MJSON"
  export DSH_PROVIDER_USAGE_MANIFEST="$MJSON"
  export DSH_PROVIDER_CHAIN="$FALLBACK_CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  LOG="$WORK/log7.txt"
  if dsh_require_provider_chain "worker" >"$LOG" 2>&1; then
    echo "::error::7) невалидный JSON манифеста обязан падать громко" >&2; exit 1
  fi
  grep -qi "JSON" "$LOG" || { echo "::error::7) сообщение обязано называть проблему JSON: $(cat "$LOG")" >&2; exit 1; }
) || fail "7) fail loud на битом JSON не сработал"
echo "GUARD(provider-usage-manifest): 7) битый JSON манифеста -> fail loud — ок"

echo "GUARD(provider-usage-manifest): манифест использования LLM-провайдеров — гвардия зелёная"
