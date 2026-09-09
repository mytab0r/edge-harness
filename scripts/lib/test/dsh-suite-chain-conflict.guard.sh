#!/usr/bin/env bash
# Гвардия стыка suite ротации учёток (#215) и цепочки провайдеров (#727/#737),
# design.md dsh-in-job «Стык suite и цепочки провайдеров»: обе фичи решают
# «какого провайдера пробовать дальше» на разных уровнях (цепочка — между
# ПРОВАЙДЕРАМИ по классу отказа, suite — между УЧЁТКАМИ внутри combo-router),
# и слепое объединение тихо ломает тормоза цепочки (атрибуция
# DSH_CHAIN_PROVIDER, реестр подтверждённых моделей #737 — маршруты suite его
# не проходят). Правило: vars.PLUGINS_SUITE_URL и vars.DSH_PROVIDER_CHAIN
# заданные ОДНОВРЕМЕННО — fail loud (dsh_require_provider_chain), не
# молчаливый приоритет одной над другой; а ВНУТРИ цикла цепочки
# dsh_patch_profile всегда пишет плоский (не combo/auto) патч, даже если
# suite активна defensively (DSH_CHAIN_ACTIVE=1).
#
# Запуск: bash scripts/lib/test/dsh-suite-chain-conflict.guard.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::GUARD(suite-chain): $*" >&2; exit 1; }

export HOME="$(mktemp -d)"
WORK="$(mktemp -d)"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

export DEEPSEEK_API_KEY="test-key"

# ── 1) Обе переменные заданы разом — dsh_require_provider_chain отказывает
#      громко, ДО какой-либо дорогой работы (не молчаливый приоритет). ───────
CHAIN='[{"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"DEEPSEEK_API_KEY","max_output_tokens":4096}]'
(
  export DSH_PROVIDER_CHAIN="$CHAIN"
  export PLUGINS_SUITE_URL="dsh-plugins-suite-v1"
  LOG="$WORK/log1.txt"
  if dsh_require_provider_chain >"$LOG" 2>&1; then
    echo "::error::1) dsh_require_provider_chain обязан был отказать при заданных обеих переменных" >&2
    exit 1
  fi
  grep -qi "PLUGINS_SUITE_URL" "$LOG" || { echo "::error::1) сообщение не называет vars.PLUGINS_SUITE_URL: $(cat "$LOG")" >&2; exit 1; }
  grep -qi "DSH_PROVIDER_CHAIN" "$LOG" || { echo "::error::1) сообщение не называет vars.DSH_PROVIDER_CHAIN: $(cat "$LOG")" >&2; exit 1; }
) || fail "1) fail-loud на обеих переменных не сработал"
echo "GUARD(suite-chain): 1) обе переменные разом -> fail loud до дорогой работы — ок"

# ── 2) Только цепочка (suite не задана) — гейт не мешает штатной работе. ────
(
  export DSH_PROVIDER_CHAIN="$CHAIN"
  unset PLUGINS_SUITE_URL 2>/dev/null || true
  dsh_require_provider_chain || { echo "::error::2) валидная цепочка без suite не должна отказывать" >&2; exit 1; }
) || fail "2) штатная цепочка без suite сломана"
echo "GUARD(suite-chain): 2) только цепочка -> проходит — ок"

# ── 3) Защитный путь: если бы suite и цепочка ОДНОВРЕМЕННО дошли до
#      dsh_patch_profile (гипотетически, минуя гейт 1) — патч ОБЯЗАН остаться
#      плоским (deepseek-official), не combo/auto, и об этом обязано быть
#      предупреждение, а не тишина. ──────────────────────────────────────────
(
  export DEEPSEEK_MODEL="primary-model"
  export DSH_CHAIN_ACTIVE=1
  export DSH_PLUGINS_SUITE_ACTIVE=1
  LOG="$WORK/log3.txt"
  dsh_patch_profile headless >"$LOG" 2>&1
  patch="$HOME/.dsh/profiles/headless/cordis.patch.yml"
  [ -f "$patch" ] || { echo "::error::3) патч профиля не создан" >&2; exit 1; }
  grep -q "provider: deepseek-official" "$patch" || { echo "::error::3) внутри цепочки патч обязан остаться плоским (deepseek-official): $(cat "$patch")" >&2; exit 1; }
  grep -q "provider: combo" "$patch" && { echo "::error::3) suite не должна активироваться внутри цикла цепочки: $(cat "$patch")" >&2; exit 1; }
  grep -qi "игнорирует suite" "$LOG" || { echo "::error::3) обход suite обязан быть виден предупреждением, не тихим: $(cat "$LOG")" >&2; exit 1; }
) || fail "3) plain-патч внутри цепочки не подтверждён"
echo "GUARD(suite-chain): 3) suite внутри цикла цепочки -> плоский патч + видимое предупреждение — ок"

echo "GUARD(suite-chain): стык suite (#215) и цепочки провайдеров (#727/#737) — гвардия зелёная"
