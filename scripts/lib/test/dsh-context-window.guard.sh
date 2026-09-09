#!/usr/bin/env bash
# Гвардия #789: контекстное окно env-provider-маршрута combo-router обязано
# приходить из vars.DSH_EDGE_MODEL_CATALOG (реальный размер окна модели), а
# НЕ из DSH_MAX_TOKENS (потолок ДЛИНЫ ОТВЕТА, adapter-конфиг llm-deepseek
# maxTokens) — живой прогон на фактически сгенерированном конфиге показал:
# combo-router::compatible() (contextTokens > contextWindow*0.92) отбрасывал
# glm-5.3-flash (реальное окно 1000000) при contextTokens=157516, потому что
# contextWindow был подставлен равным DSH_MAX_TOKENS=131072.
#
# Проверка СЕМАНТИЧЕСКАЯ, не по конкретному числу 131072 (подстрочная
# проверка на это число гвардией не считается — тот приём уже ловили на
# PR #780): каталог и
# DSH_MAX_TOKENS здесь намеренно РАЗНЫЕ и ни одно не равно другому — если
# фикс снят и contextWindow снова читает DSH_MAX_TOKENS, тест 1) увидит
# число DSH_MAX_TOKENS там, где ждёт число каталога (проверка на равенство
# каталогу, не на неравенство старому числу).
#
# Запуск: bash scripts/lib/test/dsh-context-window.guard.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::GUARD(context-window): $*" >&2; exit 1; }

export HOME="$(mktemp -d)"

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

# ── 1) Модель ЕСТЬ в каталоге, значение каталога заведомо ОТЛИЧАЕТСЯ от
#      DSH_MAX_TOKENS — combo-router-маршрут обязан нести окно каталога. ────
(
  export DEEPSEEK_API_KEY="test-key"
  export DEEPSEEK_BASE_URL="https://provider.test/v1"
  export DEEPSEEK_MODEL="guard-model"
  export DSH_MAX_TOKENS="40000"
  export DSH_EDGE_MODEL_CATALOG='[{"id":"guard-model","name":"Guard Model","contextWindow":900000}]'
  export DSH_PLUGINS_SUITE_ACTIVE=1
  unset DSH_CHAIN_ACTIVE 2>/dev/null || true
  LOG="$(mktemp)"
  dsh_patch_profile headless >"$LOG" 2>&1
  patch="$HOME/.dsh/profiles/headless/cordis.patch.yml"
  [ -f "$patch" ] || { echo "::error::1) патч профиля не создан: $(cat "$LOG")" >&2; exit 1; }
  grep -q "provider: combo" "$patch" || { echo "::error::1) combo-router не активирован (suite активна): $(cat "$patch")" >&2; exit 1; }
  # Ровно ОДНО вхождение "contextWindow: 900000" (модели каталога) и НИ
  # ОДНОГО "contextWindow: 40000" (DSH_MAX_TOKENS) — семантика «окно
  # каталога», а не совпадение с любым конкретным числом само по себе.
  got_catalog=$(grep -c "contextWindow: 900000" "$patch" || true)
  got_max_tokens=$(grep -c "contextWindow: 40000" "$patch" || true)
  [ "$got_catalog" -eq 2 ] || { echo "::error::1) contextWindow каталога (900000) обязан стоять и в providers, и в routes (найдено $got_catalog раз): $(cat "$patch")" >&2; exit 1; }
  [ "$got_max_tokens" -eq 0 ] || { echo "::error::1) DSH_MAX_TOKENS (40000) не должен попадать в contextWindow env-provider — модель есть в каталоге: $(cat "$patch")" >&2; exit 1; }
) || fail "1) окно каталога не подставлено в env-provider (регресс #789 — подстановка потолка вывода в поле окна контекста)"
echo "GUARD(context-window): 1) модель в каталоге -> contextWindow = окно каталога, не DSH_MAX_TOKENS — ок"

# ── 2) Модели НЕТ в каталоге — консервативный фолбэк на DSH_MAX_TOKENS,
#      явно и видимо (::warning::), не тихая подмена. ───────────────────────
(
  export HOME="$(mktemp -d)"
  export DEEPSEEK_API_KEY="test-key"
  export DEEPSEEK_BASE_URL="https://provider.test/v1"
  export DEEPSEEK_MODEL="model-outside-catalog"
  export DSH_MAX_TOKENS="55555"
  export DSH_EDGE_MODEL_CATALOG='[{"id":"guard-model","name":"Guard Model","contextWindow":900000}]'
  export DSH_PLUGINS_SUITE_ACTIVE=1
  unset DSH_CHAIN_ACTIVE 2>/dev/null || true
  LOG="$(mktemp)"
  dsh_patch_profile headless >"$LOG" 2>&1
  patch="$HOME/.dsh/profiles/headless/cordis.patch.yml"
  grep -q "contextWindow: 55555" "$patch" || { echo "::error::2) модель вне каталога обязана падать на фолбэк DSH_MAX_TOKENS (55555): $(cat "$patch")" >&2; exit 1; }
  grep -qi "не найдено в vars.DSH_EDGE_MODEL_CATALOG" "$LOG" || { echo "::error::2) фолбэк обязан быть виден предупреждением, не тихим: $(cat "$LOG")" >&2; exit 1; }
) || fail "2) фолбэк на модель вне каталога сломан или стал тихим"
echo "GUARD(context-window): 2) модель вне каталога -> фолбэк DSH_MAX_TOKENS + видимое предупреждение — ок"

echo "GUARD(context-window): окно контекста env-provider (#789) читается из каталога, не из потолка вывода — гвардия зелёная"
