#!/usr/bin/env bash
# Гвардия класса #838: быстрый провайдер Claude (anthropic-oauth-pool)
# монтируется НЕЗАВИСИМО от suite ротации учёток (vars.PLUGINS_SUITE_URL),
# гейтится наличием секретов аккаунтов, импортирует их прод-кодом плагина
# (не пересказом), и не ломает атрибуцию существующей цепочки провайдеров
# при отказе/отсутствии.
#
# Запуск: bash scripts/lib/test/dsh-anthropic-pool.guard.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

fail() { echo "::error::GUARD(anthropic-pool): $*" >&2; exit 1; }

# shellcheck source=scripts/lib/dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

export HOME="$(mktemp -d)"
WORK="$(mktemp -d)"

# ── 1) Ни один секрет не задан → dsh_install_anthropic_pool не качает сеть,
#      не падает, DSH_ANTHROPIC_POOL_ACTIVE=0 (поведение как раньше). ────────
(
  unset ANTHROPIC_OAUTH_1 ANTHROPIC_OAUTH_2 2>/dev/null || true
  # curl обязан НЕ вызываться вовсе — заглушка падает громко, если это не так.
  curl() { echo "::error::curl вызван без единого заданного секрета — сеть не должна была тронуться" >&2; exit 99; }
  export -f curl
  LOG="$WORK/log1.txt"
  dsh_install_anthropic_pool "$WORK/pool1" >"$LOG" 2>&1 || { echo "::error::1) dsh_install_anthropic_pool не должен был падать без секретов: $(cat "$LOG")" >&2; exit 1; }
  [ "$DSH_ANTHROPIC_POOL_ACTIVE" = "0" ] || { echo "::error::1) DSH_ANTHROPIC_POOL_ACTIVE обязан остаться 0, получено '$DSH_ANTHROPIC_POOL_ACTIVE'" >&2; exit 1; }
  grep -qi "не подключён" "$LOG" || { echo "::error::1) сообщение обязано называть факт «не подключён»: $(cat "$LOG")" >&2; exit 1; }
) || fail "1) поведение без секретов сломано"
echo "GUARD(anthropic-pool): 1) нет секретов -> пул не подключён, сеть не тронута — ок"

# ── 2) Секрет задан → dsh_import_anthropic_accounts кладёт корректный
#      ~/.dsh/anthropic-accounts/<id>.json ПРОД-КОДОМ плагина (bin/
#      dsh-anthropic-pool.js + lib/accounts.js — точная копия, инспектирована
#      живьём в релизе dsh-plugins-suite-v1, не пересказ), и УДАЛЯЕТ секрет
#      из окружения после импорта (находка design.md — доверенная граница
#      ai-review, #18: ANTHROPIC_OAUTH_1/2 не подпадают под паттерн
#      *_KEY/*_TOKEN/*_SECRET, которым DSH сам вырезает секреты из
#      model-shell). ──────────────────────────────────────────────────────
FIXTURE_PKG="$WORK/pool-extracted/package"
mkdir -p "$FIXTURE_PKG/bin" "$FIXTURE_PKG/lib"
# lib/accounts.js — точная копия package/lib/accounts.js релиза
# dsh-anthropic-oauth-pool-0.1.0.tgz (dsh-plugins-suite-v1), не переписана.
cat >"$FIXTURE_PKG/lib/accounts.js" <<'ACCOUNTS_JS'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export const poolDir = () => process.env.DSH_ANTHROPIC_POOL_DIR || path.join(os.homedir(), '.dsh', 'anthropic-accounts')
export const configFile = () => path.join(poolDir(), 'pool.json')

export function safeId(id) {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/.test(id || '')) throw new Error('Account id must match [a-zA-Z0-9][a-zA-Z0-9._-]{0,63}')
  return id
}

export function accountFile(id) { return path.join(poolDir(), safeId(id) + '.json') }

export function readConfig() {
  try { return JSON.parse(fs.readFileSync(configFile(), 'utf8')) }
  catch (error) {
    if (error.code !== 'ENOENT') throw error
    return { strategy: 'least-used', accounts: [] }
  }
}

export function writeConfig(config) {
  fs.mkdirSync(poolDir(), { recursive: true, mode: 0o700 })
  fs.writeFileSync(configFile(), JSON.stringify(config, null, 2) + '\n', { mode: 0o600 })
  fs.chmodSync(configFile(), 0o600)
}

export function readAccount(id) {
  const value = JSON.parse(fs.readFileSync(accountFile(id), 'utf8'))
  const oauth = value.claudeAiOauth || value.oauth
  if (!oauth) throw new Error(`No claudeAiOauth in account ${id}`)
  return { id, oauth }
}

export function writeAccount(id, value) {
  fs.mkdirSync(poolDir(), { recursive: true, mode: 0o700 })
  fs.writeFileSync(accountFile(id), JSON.stringify({ claudeAiOauth: value.oauth }, null, 2) + '\n', { mode: 0o600 })
  fs.chmodSync(accountFile(id), 0o600)
}

export function importAccount(id, source) {
  const json = JSON.parse(fs.readFileSync(source, 'utf8'))
  const oauth = json.claudeAiOauth || json.oauth
  if (!oauth?.accessToken || !oauth?.refreshToken) throw new Error('Source has no usable claudeAiOauth credentials')
  writeAccount(id, { id, oauth })
  const config = readConfig()
  if (!config.accounts.some((a) => a.id === id)) config.accounts.push({ id, enabled: true, weight: 1 })
  writeConfig(config)
}
ACCOUNTS_JS
# bin/dsh-anthropic-pool.js — точная копия того же релиза.
cat >"$FIXTURE_PKG/bin/dsh-anthropic-pool.js" <<'BIN_JS'
#!/usr/bin/env node
import path from 'node:path'
import os from 'node:os'
import { importAccount, readConfig, writeConfig, poolDir } from '../lib/accounts.js'

const [, , command, ...args] = process.argv
function usage(code = 0) {
  console.log(`Usage:
  dsh-anthropic-pool add <id> [credentials-file]
  dsh-anthropic-pool list
  dsh-anthropic-pool strategy <least-used|round-robin>
  dsh-anthropic-pool enable <id>
  dsh-anthropic-pool disable <id>`)
  process.exit(code)
}
if (command === 'add') {
  const [id, source = path.join(os.homedir(), '.claude', '.credentials.json')] = args
  if (!id) usage(1)
  importAccount(id, source); console.log(`Imported ${id} into ${poolDir()}`)
} else if (command === 'list') {
  const config = readConfig(); console.log(`Strategy: ${config.strategy}`)
  for (const account of config.accounts) console.log(`${account.id}\t${account.enabled === false ? 'disabled' : 'enabled'}`)
} else if (command === 'strategy') {
  const [strategy] = args
  if (!['least-used', 'round-robin'].includes(strategy)) usage(1)
  const config = readConfig(); config.strategy = strategy; writeConfig(config); console.log(`Strategy set to ${strategy}`)
} else if (command === 'enable' || command === 'disable') {
  const [id] = args; const config = readConfig(); const account = config.accounts.find((a) => a.id === id)
  if (!account) throw new Error(`Unknown account: ${id}`)
  account.enabled = command === 'enable'; writeConfig(config); console.log(`${id}: ${command}d`)
} else usage(command ? 1 : 0)
BIN_JS

command -v node >/dev/null || fail "node не найден — гвардия требует Node.js (тот же рантайм, что и dsh)"

(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$FIXTURE_PKG"
  export DSH_ANTHROPIC_POOL_DIR="$WORK/anthropic-accounts-2"
  # Фейковый credentials JSON — НЕ боевой, синтетические значения токенов.
  export ANTHROPIC_OAUTH_1='{"claudeAiOauth":{"accessToken":"smoke-access-token","refreshToken":"smoke-refresh-token","expiresAt":9999999999999}}'
  unset ANTHROPIC_OAUTH_2 2>/dev/null || true
  dsh_import_anthropic_accounts || { echo "::error::2) dsh_import_anthropic_accounts отказал" >&2; exit 1; }
  ACCOUNT_FILE="$DSH_ANTHROPIC_POOL_DIR/anthropic-1.json"
  [ -f "$ACCOUNT_FILE" ] || { echo "::error::2) $ACCOUNT_FILE не создан" >&2; exit 1; }
  got_access=$(node -e "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).claudeAiOauth.accessToken)" "$ACCOUNT_FILE")
  [ "$got_access" = "smoke-access-token" ] || { echo "::error::2) accessToken не перенесён верно: '$got_access'" >&2; exit 1; }
  got_refresh=$(node -e "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).claudeAiOauth.refreshToken)" "$ACCOUNT_FILE")
  [ "$got_refresh" = "smoke-refresh-token" ] || { echo "::error::2) refreshToken не перенесён верно: '$got_refresh'" >&2; exit 1; }
  [ -z "${ANTHROPIC_OAUTH_1:-}" ] || { echo "::error::2) ANTHROPIC_OAUTH_1 обязан быть unset после импорта (доверенная граница ai-review, #18) — остался: непусто" >&2; exit 1; }
) || fail "2) импорт аккаунта прод-кодом плагина сломан"
echo "GUARD(anthropic-pool): 2) импорт секрета прод-кодом плагина -> файл создан верно, секрет вычищен из окружения — ок"

# ── 3) dsh_run_with_pool_then_chain: пул отвечает успехом -> цепочка НЕ
#      запускается вовсе, атрибуция называет пул. ───────────────────────────
export DSH_CONFIRMED_MODELS_FILE="$WORK/confirmed-models.json"
hash_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
cat >"$DSH_CONFIRMED_MODELS_FILE" <<JSON
[{"name":"PRIMARY","model_sha256":"$(hash_of primary-model)","confirmed_at":"2026-09-09","evidence":"smoke fixture"}]
JSON

timeout() { shift; "$@"; }
sleep() { :; }
export -f timeout sleep

CHAIN_CALLED_MARK="$WORK/chain-called.mark"
dsh() {
  case "${1:-}" in
    --profile)
      rm -f "$CHAIN_CALLED_MARK.checking"
      if grep -q 'provider: anthropic-pool' "$HOME/.dsh/profiles/headless/cordis.patch.yml" 2>/dev/null; then
        case "${SMOKE_POOL_MODE:-ok}" in
          ok) echo "smoke: ответ от anthropic-oauth-pool"; return 0 ;;
          fail) echo "dsh: HTTP_503: pool_unavailable — no Anthropic account is available" >&2; return 1 ;;
        esac
      else
        touch "$CHAIN_CALLED_MARK"
        local mode_var="SMOKE_MODE_${DEEPSEEK_MODEL//-/_}"
        local mode="${!mode_var:-ok}"
        case "$mode" in
          ok) echo "smoke: ответ от $DEEPSEEK_MODEL"; return 0 ;;
          *) echo "dsh: RATE_LIMIT: Weekly/Monthly Limit Exhausted. Your limit will reset at 2026-09-10 08:51:55" >&2; return 1 ;;
        esac
      fi ;;
    *) echo "::error::SMOKE: dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
  esac
}
export -f dsh

export PRIMARY_KEY="primary-test-key"
export DSH_PROVIDER_CHAIN='[{"name":"PRIMARY","base_url":"https://primary.test/v1","model":"primary-model","secret_env":"PRIMARY_KEY","max_output_tokens":4096}]'

ANSWER="$WORK/answer.txt"; ERR="$WORK/err.txt"

(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_POOL_MODE=ok
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::3) ожидался успех пула, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "anthropic-oauth-pool" ] || { echo "::error::3) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался anthropic-oauth-pool" >&2; exit 1; }
  [ ! -f "$CHAIN_CALLED_MARK" ] || { echo "::error::3) цепочка не должна была вызываться — пул уже ответил успехом" >&2; exit 1; }
  grep -q "ответ от anthropic-oauth-pool" "$ANSWER" || { echo "::error::3) answer.txt не от пула" >&2; exit 1; }
) || fail "3) пул успешен -> цепочка не должна была запускаться"
echo "GUARD(anthropic-pool): 3) пул отвечает первым успехом -> цепочка не трогается, атрибуция честная — ок"

# ── 4) Пул отказывает -> откат на цепочку, атрибуция называет ОБОИХ. ────────
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_POOL_MODE=fail
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::4) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::4) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY (цепочка)" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::4) цепочка обязана была запуститься после отказа пула" >&2; exit 1; }
  [[ "$DSH_CHAIN_TRIED" == "anthropic-oauth-pool, PRIMARY" ]] || { echo "::error::4) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — обязан называть и пул, и PRIMARY по порядку" >&2; exit 1; }
) || fail "4) отказ пула не откатывается на цепочку честно"
echo "GUARD(anthropic-pool): 4) пул отказывает -> честный откат на цепочку, атрибуция называет обоих — ок"

# ── 5) Пул неактивен (нет секретов) -> поведение идентично состоянию ДО
#      этого change: сразу цепочка, DSH_CHAIN_TRIED без упоминания пула. ────
(
  export DSH_ANTHROPIC_POOL_ACTIVE=0
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::5) ожидался успех, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::5) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || { echo "::error::5) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — пул неактивен, не должен упоминаться" >&2; exit 1; }
) || fail "5) поведение без пула изменилось относительно состояния ДО этого change"
echo "GUARD(anthropic-pool): 5) пул неактивен -> нулевое изменение поведения цепочки — ок"

echo "GUARD(anthropic-pool): быстрый провайдер Claude (#838) — гвардия зелёная"
