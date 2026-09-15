#!/usr/bin/env bash
# Гвардия класса #838: быстрый провайдер Claude (anthropic-oauth-pool)
# монтируется НЕЗАВИСИМО от suite ротации учёток (vars.PLUGINS_SUITE_URL),
# гейтится наличием секретов аккаунтов, импортирует их прод-кодом плагина
# (не пересказом), и не ломает атрибуцию существующей цепочки провайдеров
# при отказе/отсутствии.
#
# Секции 6-8 — класс #859 (живой инцидент PR #858, 2026-09-10): BOM/битый
# JSON в ОДНОМ секрете пула не роняет весь шаг, аккаунт пропускается, пул
# продолжает с остальными аккаунтами/цепочкой.
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

# ── 6) BOM в начале ИНАЧЕ валидного секрета -> BOM снимается, аккаунт
#      импортируется как обычно (не пропускается) — прод-форма живого
#      инцидента (#859: ANTHROPIC_OAUTH_1 с ведущим EF BB BF). Второй секрет
#      в этом же прогоне — намеренно битый JSON -> пропущен с warning, но
#      функция НЕ падает и первый (валидный после BOM-strip) всё равно
#      импортирован. ─────────────────────────────────────────────────────
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$FIXTURE_PKG"
  export DSH_ANTHROPIC_POOL_DIR="$WORK/anthropic-accounts-bom"
  # BOM (EF BB BF) перед иначе валидным JSON — прод-форма инцидента PR #858.
  export ANTHROPIC_OAUTH_1=$'\xef\xbb\xbf{"claudeAiOauth":{"accessToken":"bom-access-token","refreshToken":"bom-refresh-token"}}'
  # Битый JSON — второй секрет в этом же прогоне, не должен утянуть за собой первый.
  export ANTHROPIC_OAUTH_2='{"claudeAiOauth": not valid json'
  LOG="$WORK/log6.txt"
  dsh_import_anthropic_accounts >"$LOG" 2>&1 || { echo "::error::6) dsh_import_anthropic_accounts НЕ ДОЛЖЕН падать на BOM/битом JSON: $(cat "$LOG")" >&2; exit 1; }
  [ "$DSH_ANTHROPIC_POOL_ACTIVE" = "1" ] || { echo "::error::6) есть один валидный аккаунт (после BOM-strip) — пул обязан остаться активным" >&2; exit 1; }
  ACCOUNT_FILE="$DSH_ANTHROPIC_POOL_DIR/anthropic-1.json"
  [ -f "$ACCOUNT_FILE" ] || { echo "::error::6) $ACCOUNT_FILE не создан — секрет с BOM обязан восстановиться и импортироваться: $(cat "$LOG")" >&2; exit 1; }
  got_access=$(node -e "console.log(JSON.parse(require('fs').readFileSync(process.argv[1],'utf8')).claudeAiOauth.accessToken)" "$ACCOUNT_FILE")
  [ "$got_access" = "bom-access-token" ] || { echo "::error::6) accessToken не перенесён верно после BOM-strip: '$got_access'" >&2; exit 1; }
  [ ! -f "$DSH_ANTHROPIC_POOL_DIR/anthropic-2.json" ] || { echo "::error::6) битый JSON (ANTHROPIC_OAUTH_2) не должен был импортироваться" >&2; exit 1; }
  grep -qi "невалидный JSON" "$LOG" || { echo "::error::6) лог обязан назвать причину пропуска аккаунта 2: $(cat "$LOG")" >&2; exit 1; }
  [ -z "${ANTHROPIC_OAUTH_1:-}" ] || { echo "::error::6) ANTHROPIC_OAUTH_1 обязан быть unset после обработки" >&2; exit 1; }
  [ -z "${ANTHROPIC_OAUTH_2:-}" ] || { echo "::error::6) ANTHROPIC_OAUTH_2 обязан быть unset даже при пропуске (секрет уже прочитан)" >&2; exit 1; }
) || fail "6) BOM/битый JSON рядом с валидным секретом сломаны"
echo "GUARD(anthropic-pool): 6) BOM восстановлен и импортирован, соседний битый JSON пропущен, шаг не падает — ок"

# ── 7) ОБА секрета битые (без единого валидного аккаунта) -> функция НЕ
#      падает, пул сам себя отключает (DSH_ANTHROPIC_POOL_ACTIVE=0) —
#      живая форма «плохой секрет пула не роняет гейт» (#859) в
#      максимальном виде: без единого рабочего аккаунта пул тихо выходит
#      из игры вместо ::error::+return 1 (старое поведение до #859). ───────
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$FIXTURE_PKG"
  export DSH_ANTHROPIC_POOL_DIR="$WORK/anthropic-accounts-allbad"
  export ANTHROPIC_OAUTH_1='{not valid json at all'
  export ANTHROPIC_OAUTH_2='{"claudeAiOauth":{"accessToken":"","refreshToken":""}}'
  LOG="$WORK/log7.txt"
  dsh_import_anthropic_accounts >"$LOG" 2>&1 || { echo "::error::7) dsh_import_anthropic_accounts НЕ ДОЛЖЕН падать, даже если ВСЕ секреты биты: $(cat "$LOG")" >&2; exit 1; }
  [ "$DSH_ANTHROPIC_POOL_ACTIVE" = "0" ] || { echo "::error::7) без единого валидного аккаунта пул обязан себя отключить (получено '$DSH_ANTHROPIC_POOL_ACTIVE'): $(cat "$LOG")" >&2; exit 1; }
  [ ! -d "$DSH_ANTHROPIC_POOL_DIR" ] || [ -z "$(ls -A "$DSH_ANTHROPIC_POOL_DIR" 2>/dev/null)" ] || { echo "::error::7) ни один файл аккаунта не должен был появиться" >&2; exit 1; }
) || fail "7) все секреты биты — пул обязан тихо отключиться, а не падать"
echo "GUARD(anthropic-pool): 7) все секреты пула биты -> пул сам отключается, шаг не падает — ок"

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
  POOL_LOG="$WORK/pool-warning.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$POOL_LOG" 2>&1
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::4) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $(cat "$POOL_LOG")" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::4) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY (цепочка)" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::4) цепочка обязана была запуститься после отказа пула" >&2; exit 1; }
  [[ "$DSH_CHAIN_TRIED" == "anthropic-oauth-pool, PRIMARY" ]] || { echo "::error::4) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — обязан называть и пул, и PRIMARY по порядку" >&2; exit 1; }
  # #1067 (живой инцидент — прогон worker.yml 34735752165): раньше
  # предупреждение об отказе пула называло только rc, сам stderr терялся
  # НАВСЕГДА (тот же $ERR перезаписывается первой попыткой цепочки строкой
  # выше) — причина отказа была невидима ни в одном логе прогона. Мок пула
  # (SMOKE_POOL_MODE=fail) пишет прод-форму реального отказа
  # (`dsh: HTTP_503: pool_unavailable — no Anthropic account is available`,
  # см. dsh() выше) — сообщение обязано процитировать её, не только код.
  grep -q "pool_unavailable" "$POOL_LOG" || { echo "::error::4) предупреждение об отказе пула не называет причину (stderr потерян) — регрессия #1067: $(cat "$POOL_LOG")" >&2; exit 1; }
) || fail "4) отказ пула не откатывается на цепочку честно"
echo "GUARD(anthropic-pool): 4) пул отказывает -> честный откат на цепочку, атрибуция называет обоих и НАЗЫВАЕТ ПРИЧИНУ — ок"

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

# ── 8) Сквозной путь: dsh_install_anthropic_pool видит секреты (активирует
#      пул) -> dsh_import_anthropic_accounts не находит ни одного валидного
#      аккаунта (оба секрета биты) -> пул сам себя отключает ->
#      dsh_run_with_pool_then_chain идёт СРАЗУ на цепочку, не пытаясь
#      смонтировать пустой пул (#859: живой инцидент — именно так должен
#      был вести себя PR #858 вместо падения ДО цепочки). ─────────────────
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$FIXTURE_PKG"
  export DSH_ANTHROPIC_POOL_DIR="$WORK/anthropic-accounts-e2e"
  export ANTHROPIC_OAUTH_1=$'\xef\xbb\xbf{invalid json with bom'
  export ANTHROPIC_OAUTH_2=''
  export SMOKE_MODE_primary_model=ok
  LOG="$WORK/log8.txt"
  dsh_import_anthropic_accounts >"$LOG" 2>&1 || { echo "::error::8) dsh_import_anthropic_accounts НЕ ДОЛЖЕН падать: $(cat "$LOG")" >&2; exit 1; }
  [ "$DSH_ANTHROPIC_POOL_ACTIVE" = "0" ] || { echo "::error::8) пул обязан себя отключить после нуля валидных аккаунтов" >&2; exit 1; }
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::8) ожидался успех цепочки, получено rc=$DSH_RUN_RC" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::8) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY" >&2; exit 1; }
  [ "$DSH_CHAIN_TRIED" = "PRIMARY" ] || { echo "::error::8) DSH_CHAIN_TRIED='$DSH_CHAIN_TRIED' — самоотключившийся пул не должен упоминаться" >&2; exit 1; }
) || fail "8) сквозной путь «все секреты пула биты -> сразу цепочка» сломан"
echo "GUARD(anthropic-pool): 8) все секреты пула биты сквозным путём -> шаг доходит до цепочки, не падает — ок (класс #859)"

# ── 9) Инвариант #860 «пул только в worker/hands»: канал ai-review не
#      потребляет пул ВООБЩЕ — вне комментариев ai_dsh.sh нет ни одного
#      вызова пула (dsh_install_anthropic_pool/dsh_import_anthropic_accounts/
#      dsh_mount_anthropic_pool/dsh_run_with_pool_then_chain), а в
#      ai-review.yml вне комментариев нет проводки ANTHROPIC_OAUTH*. Гейт
#      «секрета нет -> пул неактивен» (секции 1-8) такой регресс НЕ ловит:
#      вернуть env/вызовы можно, не покрасив ни одной из них, — и недельная
#      квота Claude снова жглась бы ревью (вред, который закрывает #860).
#      Комментарии вырезаются до матчинга (sed 's/#.*$//'): поясняющие
#      блоки ОБЯЗАНЫ называть отсутствующий механизм, код — нет; реальные
#      вызовы стоят на отдельных строках, эвристика им ничего не прячет.
# ─────────────────────────────────────────────────────────────────────
AI_DSH="$REPO/scripts/review/ai_dsh.sh"
AI_REVIEW_WF="$REPO/.github/workflows/ai-review.yml"
[ -f "$AI_DSH" ] || fail "9) не найден $AI_DSH"
[ -f "$AI_REVIEW_WF" ] || fail "9) не найден $AI_REVIEW_WF"
POOL_CALL_RE='dsh_install_anthropic_pool|dsh_import_anthropic_accounts|dsh_patch_anthropic_pool_plugin|dsh_mount_anthropic_pool|dsh_run_with_pool_then_chain'
if sed -e 's/#.*$//' "$AI_DSH" | grep -En "$POOL_CALL_RE"; then
  fail "9) scripts/review/ai_dsh.sh вне комментариев зовёт пул (#860: ревью идёт ТОЛЬКО цепочкой GLM/ZAI, напрямую dsh_run_with_provider_chain) — строки выше"
fi
if sed -e 's/#.*$//' "$AI_REVIEW_WF" | grep -En 'ANTHROPIC_OAUTH'; then
  fail "9) ai-review.yml вне комментариев содержит ANTHROPIC_OAUTH* (#860: секреты пула — только worker.yml/hands.yml) — строки выше"
fi
echo "GUARD(anthropic-pool): 9) ai-review не потребляет пул ни кодом, ни env-проводкой (#860) — ок"

# ── 10) #1097 (живой инцидент): _dsh_patch_profile_anthropic_pool ОБЯЗАНА
#       сама прописывать llm-pi-ai.providers.anthropic-pool статически, а не
#       полагаться на самопрописку плагина через ctx.get('settings') — та
#       падала TypeError (settings не был готов на момент вызова) и, даже
#       когда settings реально доступен (#1130: сервис ДЕЙСТВИТЕЛЬНО
#       смонтирован в headless через dsh-base — см. комментарий у функции),
#       гонится с нашей статической регистрацией и способна перезаписать
#       models живым каталогом discoverModels(). baseURL патча обязан
#       указывать на ТОТ ЖЕ порт, что экспортируется в
#       DSH_ANTHROPIC_POOL_PORT (сервер плагина слушает именно эту
#       переменную), apiKeyEnv обязан резолвиться в НЕПУСТОЕ значение той же
#       переменной окружения. Мутация (снять provider-блок из фикса) красит
#       эту секцию — доказательство приложено в PR текстом обоих прогонов. ──
(
  export HOME="$(mktemp -d)"
  _dsh_patch_profile_anthropic_pool headless
  PATCH_FILE="$HOME/.dsh/profiles/headless/cordis.patch.yml"
  [ -f "$PATCH_FILE" ] || { echo "::error::10) $PATCH_FILE не создан" >&2; exit 1; }
  grep -q '^- id: llm-pi-ai$' "$PATCH_FILE" || { echo "::error::10) патч не содержит секцию llm-pi-ai — провайдер anthropic-pool не зарегистрирован статически (регресс #1097/#1130, self-регистрация плагина ненадёжна/гонится с нашей): $(cat "$PATCH_FILE")" >&2; exit 1; }
  grep -q '^      anthropic-pool:$' "$PATCH_FILE" || { echo "::error::10) провайдер anthropic-pool не найден внутри llm-pi-ai.providers: $(cat "$PATCH_FILE")" >&2; exit 1; }
  grep -q "^        baseURL: http://127.0.0.1:${ANTHROPIC_OAUTH_POOL_PORT}\$" "$PATCH_FILE" || { echo "::error::10) baseURL патча не указывает на фиксированный порт \$ANTHROPIC_OAUTH_POOL_PORT=${ANTHROPIC_OAUTH_POOL_PORT}: $(cat "$PATCH_FILE")" >&2; exit 1; }
  [ "${DSH_ANTHROPIC_POOL_PORT:-}" = "$ANTHROPIC_OAUTH_POOL_PORT" ] || { echo "::error::10) DSH_ANTHROPIC_POOL_PORT не экспортирован (получено '${DSH_ANTHROPIC_POOL_PORT:-}', ожидался ${ANTHROPIC_OAUTH_POOL_PORT}) — плагин слушает именно эту переменную, без неё порт сервера не совпадёт с baseURL патча" >&2; exit 1; }
  APIKEY_LINE=$(grep '^        apiKeyEnv: ' "$PATCH_FILE" || true)
  APIKEY_VARNAME=${APIKEY_LINE#*apiKeyEnv: }
  [ -n "$APIKEY_VARNAME" ] || { echo "::error::10) apiKeyEnv отсутствует в патче: $(cat "$PATCH_FILE")" >&2; exit 1; }
  [ -n "${!APIKEY_VARNAME:-}" ] || { echo "::error::10) переменная apiKeyEnv '$APIKEY_VARNAME' пуста в окружении — llm-pi-ai откажется резолвить провайдер" >&2; exit 1; }
) || fail "10) статическая регистрация anthropic-pool в cordis.patch.yml сломана (регресс #1097)"
echo "GUARD(anthropic-pool): 10) llm-pi-ai.providers.anthropic-pool зарегистрирован статически, порт/apiKeyEnv согласованы — ок (#1097)"

# ── 11) #1097/#1130 (живой инцидент, второй заход): структурная секция 10
#      выше была ЗЕЛЁНОЙ и при NO_ADAPTER (до первого фикса), и при
#      UNKNOWN_MODEL (после первого фикса, гонка с self-регистрацией плагина
#      через ctx.get('settings')) — она проверяет ТОЛЬКО НАШ статический
#      патч, не то, что плагин продолжает писать в тот же settings-namespace
#      и способен перезаписать models живым дискавери-каталогом. Эта секция
#      доказывает, что `dsh_patch_anthropic_pool_plugin` РЕАЛЬНО вырезает
#      self-регистрацию из ПРОД-ФОРМЫ плагина (точная копия ensureProvider()
#      из released dsh-anthropic-oauth-pool-0.1.0.tgz, не пересказ) —
#      патченный код внутри репакованного tgz БОЛЬШЕ НЕ содержит
#      `ctx.get('settings')`. Мутация (искажение формы ensureProvider в
#      фикстуре) красит патч именно там, где он обязан упасть — на
#      несовпадении маркера, не молча пропуститьself-регистрацию. ──────────
# $1 — каталог, куда положить обе прод-формы (lib/index.js + lib/pool.js);
# патч-скрипт принимает КАТАЛОГ пакета (#1130 доработка — три патча в двух
# файлах), не путь к одному index.js. Heredoc с закавыченным делимитером
# (не '%s' + одинарные кавычки) — тот же приём, что уже применяют фикстуры
# accounts.js/bin.js выше в этом файле: реальный JS-текст без экранирования
# каждой одинарной кавычки.
write_fixture_package() {
  local dir=$1
  mkdir -p "$dir/lib"
  # index.js — ensureProvider() и окружающий forward()/401-403-ветка —
  # ТОЧНАЯ копия dsh-anthropic-oauth-pool-0.1.0.tgz (релиз
  # dsh-plugins-suite-v1), инспектирована живьём при разборе #1097/#1130.
  # Строка import — тоже точная копия верхушки реального lib/index.js
  # (#1192: PATCH_IMPORT_CLASSIFY трогает именно её).
  cat >"$dir/lib/index.js" <<'INDEX_JS'
import { createRefreshCoordinator, selectAccount, updateQuotaFromHeaders, available } from './pool.js'

const name = "dsh-anthropic-oauth-pool"

function apply(ctx) {
  let port = 47291
  let models = [{ id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", contextWindow: 200000, maxTokens: 64000 }]

  async function discoverModels() {}

  async function ensureProvider() {
    await discoverModels()
    try { await ctx.get('credentials').set(CREDS_REF, 'managed-by-anthropic-pool') } catch {}
    const provider = { displayName: 'Anthropic OAuth Pool', apiKeyEnv: CREDS_REF, api: 'anthropic-messages', baseURL: `http://127.0.0.1:${port}`, models }
    const settings = ctx.get('settings')
    if (typeof settings.update === 'function') await settings.update('llm-pi-ai', { providers: { [PROVIDER_KEY]: provider } })
    else if (typeof settings.mutate === 'function') await settings.mutate('llm-pi-ai', [{ op: 'add', path: ['providers', PROVIDER_KEY], value: provider }])
    else throw new Error('DSH settings service cannot install the Anthropic pool provider')
  }

  async function forward(req, res) {
    let body
    try { body = await readBody(req) } catch (error) { res.writeHead(413); res.end(error.message); return }
    const config = reload()
    const attempted = new Set()
    let lastResponse
    let lastResponseBody
    let lastError
    while (attempted.size < runtime.size) {
      const candidates = [...runtime.values()].filter((a) => !attempted.has(a.id))
      const account = selectAccount(candidates, config.strategy, cursor++)
      if (!account) break
      attempted.add(account.id)
      account.lastUsedAt = Date.now()
      account.requests = (account.requests || 0) + 1
      try {
        const stored = await ensureFresh(account.id)
        const response = await fetch(new URL(req.url || '/', API_BASE), {
          method: req.method, headers: oauthHeaders(stored.oauth.accessToken, req.headers),
          body: ['GET', 'HEAD'].includes(req.method) ? undefined : body,
          redirect: 'manual', signal: AbortSignal.timeout(10 * 60 * 1000),
        })
        updateQuotaFromHeaders(account, response.headers, response.status)
        account.lastStatus = response.status
        if ([401, 403].includes(response.status)) {
          account.cooldownUntil = Date.now() + 60_000; lastResponse = response; lastResponseBody = Buffer.from(await response.arrayBuffer()); continue
        }
        if (response.status === 429) { lastResponse = response; lastResponseBody = Buffer.from(await response.arrayBuffer()); continue }
        res.statusCode = response.status
        for (const [key, value] of response.headers) {
          if (!['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(key.toLowerCase())) res.setHeader(key, value)
        }
        res.setHeader('x-dsh-anthropic-account', account.id)
        if (response.body) Readable.fromWeb(response.body).pipe(res); else res.end()
        return
      } catch (error) {
        account.errors = (account.errors || 0) + 1
        account.lastError = String(error?.message || error).slice(0, 300)
        account.cooldownUntil = Date.now() + 15_000
        lastError = error
      }
    }
    if (lastResponse) {
      res.statusCode = lastResponse.status
      res.setHeader('content-type', lastResponse.headers.get('content-type') || 'application/json')
      res.end(lastResponseBody)
    } else {
      res.statusCode = 503; res.setHeader('content-type', 'application/json')
      const next = [...runtime.values()].filter((a) => a.cooldownUntil).sort((a, b) => a.cooldownUntil - b.cooldownUntil)[0]
      res.end(JSON.stringify({ type: 'error', error: { type: 'pool_unavailable', message: lastError?.message || 'No Anthropic account is available', retryAt: next?.cooldownUntil || null } }))
    }
  }
}

export { name, apply }
INDEX_JS
  # pool.js — createRefreshCoordinator — ТОЧНАЯ копия того же релиза.
  cat >"$dir/lib/pool.js" <<'POOL_JS'
const REFRESH_SKEW_MS = 5 * 60 * 1000

export function createRefreshCoordinator({ readAccount, writeAccount, refreshToken }) {
  const pending = new Map()
  return async function ensureFresh(id) {
    if (pending.has(id)) return pending.get(id)
    const work = (async () => {
      const account = await readAccount(id)
      const oauth = account.oauth
      if (!oauth?.accessToken || !oauth?.refreshToken) throw new Error(`Account ${id} has no Claude OAuth credentials`)
      if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account
      const next = await refreshToken(oauth.refreshToken)
      account.oauth = {
        ...oauth,
        accessToken: next.access_token,
        refreshToken: next.refresh_token || oauth.refreshToken,
        expiresAt: Date.now() + (Number(next.expires_in) || 3600) * 1000,
      }
      await writeAccount(id, account)
      return account
    })().finally(() => pending.delete(id))
    pending.set(id, work)
    return work
  }
}
POOL_JS
}

(
  FIXTURE_DIR="$(mktemp -d)"
  write_fixture_package "$FIXTURE_DIR"
  # Сверяем байт-в-байт с тем, что реально проверяет патч-скрипт
  # (OLD-константы), не с нашим пересказом их содержимого.
  grep -q "const settings = ctx.get('settings')" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) фикстура index.js сама не содержит ожидаемую строку ensureProvider — тест сломан до патча" >&2; exit 1; }
  grep -q "\[401, 403\].includes(response.status)" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) фикстура index.js сама не содержит ожидаемую 401/403-ветку — тест сломан до патча" >&2; exit 1; }
  grep -q "oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS" "$FIXTURE_DIR/lib/pool.js" || { echo "::error::11) фикстура pool.js сама не содержит ожидаемое условие пропуска рефреша — тест сломан до патча" >&2; exit 1; }
  if ! python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::11) патч не применился к прод-форме фикстуры: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1
  fi
  # Ищем именно ЖИВОЙ вызов (`const settings = ctx.get(...)`), не подстроку
  # "ctx.get('settings')" целиком — наш же поясняющий комментарий в патче
  # ЗАКОННО упоминает эту фразу текстом (находка при первом прогоне этой
  # секции: голый grep по подстроке ловил СОБСТВЕННЫЙ комментарий патча как
  # ложное срабатывание).
  # if/then, не `grep ... && { ...; exit 1; }` — живая находка при первой
  # версии этой секции: когда такая проверка оказывается ПОСЛЕДНЕЙ командой
  # подоболочки, POSIX-семантика AND-OR списка под `set -e` делает НЕсовпадение
  # (grep вернул 1, ожидаемый/верный исход) кодом возврата ВСЕЙ подоболочки —
  # `|| fail` снаружи ложно красит секцию (класс воспроизведён в секции 13
  # этого же файла, см. её комментарий).
  if grep -q "const settings = ctx.get(" "$FIXTURE_DIR/lib/index.js"; then
    echo "::error::11) после патча живой вызов 'const settings = ctx.get(...)' всё ещё присутствует — self-регистрация НЕ нейтрализована" >&2; exit 1
  fi
  if grep -q "settings.update(" "$FIXTURE_DIR/lib/index.js"; then
    echo "::error::11) после патча settings.update(...) всё ещё вызывается" >&2; exit 1
  fi
  grep -q "async function ensureProvider" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) патч удалил саму функцию ensureProvider вместо нейтрализации тела" >&2; exit 1; }
  # Патч 3 (реактивный рефреш на 401/403): старая ветка (cooldown+continue
  # без единой попытки рефреша) заменена на try-рефреш-и-повтор.
  grep -q "await ensureFresh(account.id)" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) 401/403-ветка не содержит реактивный вызов ensureFresh — патч 3 не применился" >&2; exit 1; }
  grep -q "expiresAt: Date.now() - 1" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) 401/403-ветка не помечает аккаунт как просроченный перед повторным ensureFresh — патч 3 не применился" >&2; exit 1; }
  # Патч 2 (pool.js): условие пропуска обязано пропускать И при отсутствии expiresAt.
  if grep -q "if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account" "$FIXTURE_DIR/lib/pool.js"; then
    echo "::error::11) pool.js всё ещё трактует отсутствующий expiresAt как «истёк» — патч 2 не применился" >&2; exit 1
  fi
  grep -q "if (!oauth.expiresAt || oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account" "$FIXTURE_DIR/lib/pool.js" || { echo "::error::11) pool.js не содержит новое условие пропуска рефреша — патч 2 не применился корректно" >&2; exit 1; }
) || fail "11) патч плагина (happy path) не нейтрализует self-регистрацию и не чинит рефреш долгоживущих токенов в прод-форме"
echo "GUARD(anthropic-pool): 11a) все три патча (ensureProvider, reactive-refresh, pool-skip-condition) применились к прод-форме — ок (#1097/#1130)"

(
  FIXTURE_DIR="$(mktemp -d)"
  write_fixture_package "$FIXTURE_DIR"
  # Мутация: форма ensureProvider изменилась (как если бы апстрим переписал
  # плагин) — точное совпадение обязано провалиться, а не тихо пропустить.
  # python3 (не bash `${var/pattern/repl}` — та ломается на кавычках внутри
  # паттерна, живая находка при первом прогоне этой секции: подстановка
  # молча не срабатывала, MUTATED оставался равен оригиналу).
  python3 -c "
import sys
path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    content = f.read()
marker = \"const settings = ctx.get('settings')\"
assert marker in content, 'fixture setup broken'
content = content.replace(marker, \"const settingsService = ctx.get('settings')\")
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
" "$FIXTURE_DIR/lib/index.js"
  grep -q "const settingsService = ctx.get('settings')" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) мутация фикстуры не применилась — тест сломан до патча" >&2; exit 1; }
  if python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::11) патч ОБЯЗАН был отказать на изменённой форме ensureProvider, но применился молча" >&2; exit 1
  fi
  grep -qi "PATCH_MARKER_NOT_FOUND" "$FIXTURE_DIR/patch.log" || { echo "::error::11) отказ патча не назвал причину PATCH_MARKER_NOT_FOUND: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1; }
  grep -q "const settingsService = ctx.get('settings')" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) файл фикстуры не должен был измениться при отказе патча" >&2; exit 1; }
  # Атомарность: ensure_provider не совпал первым — pool.js (третий по счёту
  # патч) обязан остаться НЕТРОНУТЫМ, а не частично пропатченным.
  grep -q "if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account" "$FIXTURE_DIR/lib/pool.js" || { echo "::error::11) pool.js изменился, хотя патч должен был отказать ДО записи любого файла (нарушена атомарность)" >&2; exit 1; }
) || fail "11) патч не падает громко на изменённой форме ensureProvider (мутация #1130)"
echo "GUARD(anthropic-pool): 11b) мутация формы ensureProvider -> патч отказывает громко (PATCH_MARKER_NOT_FOUND), ОБА файла не тронуты (атомарность) — ок (#1130)"

# ── 12) #1130 (находка ai-review PR #1132): suite-путь (dsh_mount_plugins_suite)
#       НЕ монтирует свою (непатченную) копию dsh-anthropic-oauth-pool, когда
#       standalone-путь уже активен — иначе смонтировались бы ДВЕ копии
#       плагина, одна из них без патча #1130, и self-регистрация снова
#       гонилась бы со статической регистрацией. Мокаем `dsh`, чтобы
#       доказать: аргумент `$DSH_PLUGINS_SUITE_OAUTH_PKG` НИКОГДА не доходит
#       до `dsh plugin add`, когда DSH_ANTHROPIC_POOL_ACTIVE=1. ─────────────
(
  export HOME="$(mktemp -d)"
  export DSH_PLUGINS_SUITE_ACTIVE=1
  export DSH_PLUGINS_SUITE_COMBO_PKG="$WORK/combo.tgz"
  export DSH_PLUGINS_SUITE_OAUTH_PKG="$WORK/suite-oauth-unpatched.tgz"
  : >"$DSH_PLUGINS_SUITE_COMBO_PKG"; : >"$DSH_PLUGINS_SUITE_OAUTH_PKG"
  SUITE_OAUTH_MOUNTED_MARK="$WORK/suite-oauth-mounted.mark"
  rm -f "$SUITE_OAUTH_MOUNTED_MARK"
  dsh() {
    case "${1:-}" in
      plugin)
        # dsh plugin --profile <profile> add <pkg>: $1=plugin $2=--profile
        # $3=<profile> $4=add $5=<pkg>.
        case "${5:-}" in
          "$DSH_PLUGINS_SUITE_OAUTH_PKG") touch "$SUITE_OAUTH_MOUNTED_MARK" ;;
        esac
        return 0 ;;
      --profile)
        # --dump-config: combo-router + provider:combo/model:auto (активация
        # подтверждена, чтобы не задеть отдельную ветку отката
        # _dsh_patch_profile_plain — та не тема этой секции и требует
        # DSH_MODEL/DSH_MAX_TOKENS, которых эта минимальная фикстура не
        # ставит). anthropic-oauth-pool сюда НЕ входит — наш путь сам
        # подтверждает его следующим шагом, не эта функция.
        printf '%s\n' "- id: combo-router" "  config:" "    provider: combo" "    model: auto"
        return 0 ;;
      *) echo "::error::SMOKE(12): dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
    esac
  }
  export -f dsh
  (
    export DSH_ANTHROPIC_POOL_ACTIVE=1
    dsh_mount_plugins_suite headless || { echo "::error::12) dsh_mount_plugins_suite отказал при активном standalone-пуле" >&2; exit 1; }
  )
  [ ! -f "$SUITE_OAUTH_MOUNTED_MARK" ] || { echo "::error::12) suite смонтировал СВОЮ (непатченную) копию dsh-anthropic-oauth-pool, хотя standalone-путь уже активен — двойной монтаж, self-регистрация снова гонится со статической (регресс #1130)" >&2; exit 1; }
) || fail "12) suite-путь монтирует непатченную копию плагина при активном standalone-пуле"
echo "GUARD(anthropic-pool): 12) suite пропускает свой oauth-add при активном standalone-пуле — двойной монтаж исключён (#1130)"

# ── 13) #1130 (некритичное замечание ai-review PR #1132): сама обёртка
#      dsh_patch_anthropic_pool_plugin (репак + перепривязка PKG) — секции
#      11a/11b проверяют только вызываемый ею python-скрипт напрямую.
#      Здесь — сквозной вызов ЧЕРЕЗ функцию dsh-ci.sh: DSH_ANTHROPIC_POOL_PKG
#      обязан после вызова указывать на НОВЫЙ существующий tgz (репак), а
#      распакованный из НЕГО lib/index.js обязан НЕ содержать живой вызов
#      settings (тот же признак, что 11a, но через полный путь функции). ──
(
  FIXTURE_ROOT="$(mktemp -d)"
  EXTRACT_DIR="$FIXTURE_ROOT/anthropic-oauth-pool-extracted/package"
  write_fixture_package "$EXTRACT_DIR"
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$EXTRACT_DIR"
  ORIGINAL_PKG="$FIXTURE_ROOT/original.tgz"
  : >"$ORIGINAL_PKG"
  export DSH_ANTHROPIC_POOL_PKG="$ORIGINAL_PKG"
  dsh_patch_anthropic_pool_plugin || { echo "::error::13) dsh_patch_anthropic_pool_plugin отказала на валидной фикстуре" >&2; exit 1; }
  [ "$DSH_ANTHROPIC_POOL_PKG" != "$ORIGINAL_PKG" ] || { echo "::error::13) DSH_ANTHROPIC_POOL_PKG не перепривязан на патченный tgz — dsh_mount_anthropic_pool смонтирует оригинал без патча" >&2; exit 1; }
  [ -f "$DSH_ANTHROPIC_POOL_PKG" ] || { echo "::error::13) $DSH_ANTHROPIC_POOL_PKG (репак) не создан" >&2; exit 1; }
  REPACK_CHECK_DIR="$FIXTURE_ROOT/repack-check"
  mkdir -p "$REPACK_CHECK_DIR"
  tar -xzf "$DSH_ANTHROPIC_POOL_PKG" -C "$REPACK_CHECK_DIR" || { echo "::error::13) репак нечитаем tar'ом" >&2; exit 1; }
  [ -f "$REPACK_CHECK_DIR/package/lib/index.js" ] || { echo "::error::13) репак не сохранил структуру package/lib/index.js" >&2; exit 1; }
  [ -f "$REPACK_CHECK_DIR/package/lib/pool.js" ] || { echo "::error::13) репак не сохранил package/lib/pool.js" >&2; exit 1; }
  if grep -q "const settings = ctx.get(" "$REPACK_CHECK_DIR/package/lib/index.js"; then
    echo "::error::13) внутри репака живой вызов ctx.get('settings') остался — обёртка не патчит реальный монтируемый архив" >&2; exit 1
  fi
  if grep -q "if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS) return account" "$REPACK_CHECK_DIR/package/lib/pool.js"; then
    echo "::error::13) внутри репака pool.js всё ещё трактует отсутствующий expiresAt как «истёк»" >&2; exit 1
  fi
) || fail "13) обёртка dsh_patch_anthropic_pool_plugin (репак + перепривязка PKG) сломана"
echo "GUARD(anthropic-pool): 13) dsh_patch_anthropic_pool_plugin сквозным вызовом: PKG перепривязан, репак читаем, settings-вызов вырезан — ок (#1130)"

# ── 14) #1130 (доработка, решение владельца): ПОВЕДЕНЧЕСКОЕ доказательство
#      фикса превентивного рефреша долгоживущих токенов — не текстовый grep
#      по условию (уже сделан в 11a/13), а РЕАЛЬНЫЙ вызов патченного pool.js
#      (`createRefreshCoordinator`) в node с синтетическими
#      readAccount/writeAccount/refreshToken. Доказывает ОБА направления:
#      без expiresAt рефреша НЕТ (главная цель фикса — долгоживущий токен
#      без явного срока не считается протухшим), с expiresAt в прошлом
#      рефреш ЕСТЬ (короткоживущие токены продолжают рефрешиться как
#      раньше — фикс не сломал этот путь, требование владельца п.3). ──────
(
  FIXTURE_ROOT="$(mktemp -d)"
  EXTRACT_DIR="$FIXTURE_ROOT/anthropic-oauth-pool-extracted/package"
  write_fixture_package "$EXTRACT_DIR"
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$EXTRACT_DIR"
  export DSH_ANTHROPIC_POOL_PKG="$FIXTURE_ROOT/original.tgz"
  : >"$DSH_ANTHROPIC_POOL_PKG"
  dsh_patch_anthropic_pool_plugin || { echo "::error::14) dsh_patch_anthropic_pool_plugin отказала на валидной фикстуре" >&2; exit 1; }

  cat >"$EXTRACT_DIR/lib/behavior-check.mjs" <<'BEHAVIOR_MJS'
import { createRefreshCoordinator } from './pool.js'

async function refreshCalledFor(expiresAtOverride) {
  let refreshCalled = false
  const oauth = { accessToken: 'atok', refreshToken: 'rtok', ...(expiresAtOverride === undefined ? {} : { expiresAt: expiresAtOverride }) }
  const coordinator = createRefreshCoordinator({
    readAccount: async (id) => ({ id, oauth }),
    writeAccount: async () => {},
    refreshToken: async () => { refreshCalled = true; return { access_token: 'NEW', refresh_token: 'NEW_R', expires_in: 3600 } },
  })
  await coordinator('acc')
  return refreshCalled
}

const noExpiresAt = await refreshCalledFor(undefined)
const expiresInPast = await refreshCalledFor(Date.now() - 1000)
const expiresFarFuture = await refreshCalledFor(Date.now() + 999999999)
console.log(`no_expires_at=${noExpiresAt} expires_in_past=${expiresInPast} expires_far_future=${expiresFarFuture}`)
if (noExpiresAt) { console.error('FAIL: без expiresAt рефреш всё равно случился — долгоживущий токен считается протухшим'); process.exit(1) }
if (!expiresInPast) { console.error('FAIL: с expiresAt в прошлом рефреш НЕ произошёл — реально истёкший токен не восстановится'); process.exit(1) }
if (expiresFarFuture) { console.error('FAIL: с expiresAt далеко в будущем рефреш произошёл — короткоживущие токены сломаны'); process.exit(1) }
console.log('OK: без expiresAt рефреша нет, с expiresAt в прошлом рефреш есть, с expiresAt в будущем рефреша нет')
BEHAVIOR_MJS
  BEHAVIOR_LOG="$FIXTURE_ROOT/behavior.log"
  if ! node "$EXTRACT_DIR/lib/behavior-check.mjs" >"$BEHAVIOR_LOG" 2>&1; then
    { echo "::error::14) поведенческая проверка патченного pool.js провалилась:"; cat "$BEHAVIOR_LOG"; } >&2
    exit 1
  fi
  cat "$BEHAVIOR_LOG"
) || fail "14) патч превентивного рефреша долгоживущих токенов не подтверждён поведенчески"
echo "GUARD(anthropic-pool): 14) патченный pool.js — без expiresAt рефреша нет, с expiresAt в прошлом рефреш есть, короткоживущие токены не задеты — ок (#1130)"

# ── 15) #1192: 4-й патч (classifyPoolUnavailable/reason/accounts) применился
#       к прод-форме фикстуры — расширение секции 11a теми же приёмами:
#       живой вызов classifyPoolUnavailable в else-ветке, обновлённый import
#       из pool.js, экспортированная функция в pool.js. ────────────────────
(
  FIXTURE_DIR="$(mktemp -d)"
  write_fixture_package "$FIXTURE_DIR"
  grep -q "import { createRefreshCoordinator, selectAccount, updateQuotaFromHeaders, available } from './pool.js'" "$FIXTURE_DIR/lib/index.js" || { echo "::error::15) фикстура index.js сама не содержит ожидаемую строку import — тест сломан до патча" >&2; exit 1; }
  if ! python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::15) патч не применился к прод-форме фикстуры: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1
  fi
  grep -q "available, classifyPoolUnavailable } from './pool.js'" "$FIXTURE_DIR/lib/index.js" || { echo "::error::15) import classifyPoolUnavailable не добавлен в index.js" >&2; exit 1; }
  grep -q "const { reason, accounts } = classifyPoolUnavailable(\[...runtime.values()\])" "$FIXTURE_DIR/lib/index.js" || { echo "::error::15) else-ветка pool_unavailable не зовёт classifyPoolUnavailable" >&2; exit 1; }
  grep -q "retryAt: next?.cooldownUntil || null, reason, accounts } }))" "$FIXTURE_DIR/lib/index.js" || { echo "::error::15) reason/accounts не попали в JSON-тело pool_unavailable" >&2; exit 1; }
  grep -q "^export function classifyPoolUnavailable(accounts) {" "$FIXTURE_DIR/lib/pool.js" || { echo "::error::15) pool.js не содержит экспортированную classifyPoolUnavailable" >&2; exit 1; }
) || fail "15) патч 4 (reason/accounts у pool_unavailable) не применился к прод-форме"
echo "GUARD(anthropic-pool): 15) патч 4 (classifyPoolUnavailable/reason/accounts) применился к прод-форме — ок (#1192)"

# ── 16) #1192 (мутация): форма else-ветки pool_unavailable изменилась (как
#       если бы апстрим переписал плагин) — точное совпадение обязано
#       провалиться громко, а НЕ применить патч частично. Проверяем
#       атомарность: ни index.js, ни pool.js не изменились (все 6
#       _replace_required идут на content-буферах ДО первой записи файла). ──
(
  FIXTURE_DIR="$(mktemp -d)"
  write_fixture_package "$FIXTURE_DIR"
  python3 -c "
import sys
path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    content = f.read()
marker = \"type: 'pool_unavailable', message: lastError?.message || 'No Anthropic account is available', retryAt: next?.cooldownUntil || null\"
assert marker in content, 'fixture setup broken'
content = content.replace(marker, \"type: 'pool_unavailable', reason_message: lastError?.message || 'No Anthropic account is available', retryAt: next?.cooldownUntil || null\")
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
" "$FIXTURE_DIR/lib/index.js"
  ORIGINAL_INDEX_JS="$(cat "$FIXTURE_DIR/lib/index.js")"
  ORIGINAL_POOL_JS="$(cat "$FIXTURE_DIR/lib/pool.js")"
  if python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::16) патч ОБЯЗАН был отказать на изменённой форме else-ветки pool_unavailable, но применился молча" >&2; exit 1
  fi
  grep -qi "PATCH_MARKER_NOT_FOUND\[pool_unavailable_reason\]" "$FIXTURE_DIR/patch.log" || { echo "::error::16) отказ патча не назвал причину PATCH_MARKER_NOT_FOUND[pool_unavailable_reason]: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1; }
  [ "$(cat "$FIXTURE_DIR/lib/index.js")" = "$ORIGINAL_INDEX_JS" ] || { echo "::error::16) index.js изменился, хотя патч должен был отказать ДО записи любого файла" >&2; exit 1; }
  [ "$(cat "$FIXTURE_DIR/lib/pool.js")" = "$ORIGINAL_POOL_JS" ] || { echo "::error::16) pool.js изменился, хотя патч 4-й (pool_unavailable_reason) не совпал первым в index.js — атомарность нарушена (pool_classify_export не должен был примениться)" >&2; exit 1; }
) || fail "16) патч не падает громко на изменённой форме else-ветки pool_unavailable (мутация #1192)"
echo "GUARD(anthropic-pool): 16) мутация формы pool_unavailable -> патч отказывает громко (PATCH_MARKER_NOT_FOUND), ОБА файла не тронуты (атомарность) — ок (#1192)"

# ── 17) #1192: ПОВЕДЕНЧЕСКОЕ доказательство classifyPoolUnavailable — не
#       текстовый grep (уже сделан в 15), а РЕАЛЬНЫЙ вызов патченной функции
#       в node с синтетическими аккаунтами. Доказывает: 401/403 ->
#       auth_rejected; 429 (без auth_rejected рядом) -> rate_limited;
#       исключение без HTTP-ответа -> network_error; ничего не пробовалось
#       ЛИБО последний записанный ответ вне {401,403,429} (например 200) ->
#       unknown; auth_rejected ИМЕЕТ ПРИОРИТЕТ над rate_limited, когда оба
#       класса присутствуют одновременно (владелец не должен быть пропущен
#       только потому, что ДРУГОЙ аккаунт всего лишь упёрся в лимит). ───────
(
  FIXTURE_ROOT="$(mktemp -d)"
  EXTRACT_DIR="$FIXTURE_ROOT/anthropic-oauth-pool-extracted/package"
  write_fixture_package "$EXTRACT_DIR"
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_ANTHROPIC_POOL_EXTRACTED="$EXTRACT_DIR"
  export DSH_ANTHROPIC_POOL_PKG="$FIXTURE_ROOT/original.tgz"
  : >"$DSH_ANTHROPIC_POOL_PKG"
  dsh_patch_anthropic_pool_plugin || { echo "::error::17) dsh_patch_anthropic_pool_plugin отказала на валидной фикстуре" >&2; exit 1; }

  cat >"$EXTRACT_DIR/lib/classify-check.mjs" <<'CLASSIFY_MJS'
import { classifyPoolUnavailable } from './pool.js'

const authOnly = classifyPoolUnavailable([{ id: 'a1', lastStatus: 401 }, { id: 'a2', lastStatus: 403 }])
const rateOnly = classifyPoolUnavailable([{ id: 'a1', lastStatus: 429 }])
const networkOnly = classifyPoolUnavailable([{ id: 'a1', lastError: 'fetch failed: ECONNRESET' }])
const nothing = classifyPoolUnavailable([{ id: 'a1' }])
const mixedAuthWins = classifyPoolUnavailable([{ id: 'a1', lastStatus: 429 }, { id: 'a2', lastStatus: 401 }])
// Находка ai-review PR #1193, раунд 4: класс unknown ШИРЕ, чем «ни разу не
// пробовался» — записанный lastStatus вне {401,403,429} (например 200 из
// прошлого вызова, живущий в runtime-состоянии так же, как 401) тоже даёт
// unknown. Поведение фиксируется кейсом, чтобы докстринг и код не разошлись.
const stale200 = classifyPoolUnavailable([{ id: 'a1', lastStatus: 200 }])

const checks = [
  ['authOnly.reason', authOnly.reason, 'auth_rejected'],
  ['authOnly.accounts[0].class', authOnly.accounts[0].class, 'auth_rejected'],
  ['authOnly.accounts[1].class', authOnly.accounts[1].class, 'auth_rejected'],
  ['rateOnly.reason', rateOnly.reason, 'rate_limited'],
  ['networkOnly.reason', networkOnly.reason, 'network_error'],
  ['nothing.reason', nothing.reason, 'unknown'],
  ['mixedAuthWins.reason', mixedAuthWins.reason, 'auth_rejected'],
  ['stale200.reason', stale200.reason, 'unknown'],
]
let failed = false
for (const [label, got, want] of checks) {
  const ok = got === want
  console.log(`${ok ? 'OK' : 'FAIL'}: ${label}=${got} (want ${want})`)
  if (!ok) failed = true
}
if (failed) process.exit(1)
console.log(`OK: classifyPoolUnavailable — все ${checks.length} проверок прошли, 4 класса (auth_rejected/rate_limited/network_error/unknown) различены верно, auth_rejected приоритетнее rate_limited`)
CLASSIFY_MJS
  CLASSIFY_LOG="$FIXTURE_ROOT/classify.log"
  if ! node "$EXTRACT_DIR/lib/classify-check.mjs" >"$CLASSIFY_LOG" 2>&1; then
    { echo "::error::17) поведенческая проверка classifyPoolUnavailable провалилась:"; cat "$CLASSIFY_LOG"; } >&2
    exit 1
  fi
  cat "$CLASSIFY_LOG"
) || fail "17) classifyPoolUnavailable не подтверждена поведенчески"
echo "GUARD(anthropic-pool): 17) classifyPoolUnavailable — auth_rejected/rate_limited/network_error/unknown различены, auth_rejected приоритетнее — ок (#1192)"

# ── 18) #1192: dsh_pool_unavailable_owner_note — прод-форма stderr (реальный
#       вид «dsh: SERVER: 503 {...}» из живого прогона worker.yml 34792555573,
#       дополненный полем reason нашим же патчем). ВОСЕМЬ stderr-форм, каждая —
#       РАЗНЫЙ факт, не гадание: auth_rejected называет "владелец нужен",
#       rate_limited — "владелец НЕ нужен" (два сообщения обязаны различаться
#       буквально, не одним и тем же текстом с другой меткой); поле reason
#       отсутствует (форма до патча/апстрим сменился) -> честный пробел
#       «не классифицирована», а не подстановка одной из гипотез; чужой
#       reason ДО и ПОСЛЕ тела (находка ai-review PR #1193, раунд 4: вырезка
#       тела обязана держаться в пределах строки тела — причина пула не может
#       прийти из чужой строки stderr). ─────────────────────────────────────
(
  WORK18="$(mktemp -d)"
  cat >"$WORK18/err_auth.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948,"reason":"auth_rejected","accounts":[{"id":"anthropic-1","class":"auth_rejected","lastStatus":401,"cooldownUntil":1789345764948},{"id":"anthropic-2","class":"auth_rejected","lastStatus":403,"cooldownUntil":1789345764948}]}}
EOF
  cat >"$WORK18/err_rate.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948,"reason":"rate_limited","accounts":[{"id":"anthropic-1","class":"rate_limited","lastStatus":429,"cooldownUntil":1789345764948}]}}
EOF
  cat >"$WORK18/err_network.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"fetch failed: ECONNRESET","retryAt":null,"reason":"network_error","accounts":[{"id":"anthropic-1","class":"network_error","lastStatus":null,"cooldownUntil":1789345764948}]}}
EOF
  cat >"$WORK18/err_unknown.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":null,"reason":"unknown","accounts":[]}}
EOF
  # Прод-форма ДО патча #1192 (живой прогон 34792555573 — ровно этот текст) —
  # тело pool_unavailable ЕСТЬ, поле reason отсутствует физически.
  cat >"$WORK18/err_no_reason.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948}}
EOF
  # Отказ пула СОВСЕМ ДРУГОЙ природы (таймаут до локального прокси) — тела
  # pool_unavailable в stderr нет вовсе. Находка ai-review PR #1193: сообщение
  # не имеет права утверждать «поле reason отсутствует В ОТВЕТЕ ПУЛА», если
  # само тело pool_unavailable не было замечено вовсе — это разные факты.
  cat >"$WORK18/err_unrelated.txt" <<'EOF'
dsh: connect ECONNREFUSED 127.0.0.1:47291
EOF
  # Чужой reason ПОСЛЕ тела (находка ai-review PR #1193, раунд 4): тело
  # pool_unavailable БЕЗ reason, НИЖЕ — несвязанная строка чужого компонента
  # со СВОИМ "reason":"auth_rejected". Причина обязана остаться честным
  # пробелом «тело есть, без поля reason», а не стать «владелец нужен» —
  # иначе скоуп вырезки шире строки тела и чужой факт приписан пулу.
  cat >"$WORK18/err_foreign_after.txt" <<'EOF'
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948}}
2026-09-14T00:24:00Z cache-backend {"level":"error","reason":"auth_rejected","msg":"backend rejected credentials"}
EOF
  # Чужой reason ДО тела — тот же класс с другой стороны (чеклист ai-review
  # PR #1193, раунд 3): тело само несёт auth_rejected, чужая строка ВЫШЕ со
  # своим rate_limited не должна перебить факт тела.
  cat >"$WORK18/err_foreign_before.txt" <<'EOF'
2026-09-14T00:23:00Z cache-backend {"level":"warn","reason":"rate_limited","msg":"retry budget drained"}
dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948,"reason":"auth_rejected","accounts":[{"id":"anthropic-1","class":"auth_rejected","lastStatus":401,"cooldownUntil":1789345764948}]}}
EOF

  note_auth=$(dsh_pool_unavailable_owner_note "$WORK18/err_auth.txt")
  note_rate=$(dsh_pool_unavailable_owner_note "$WORK18/err_rate.txt")
  note_network=$(dsh_pool_unavailable_owner_note "$WORK18/err_network.txt")
  note_unknown=$(dsh_pool_unavailable_owner_note "$WORK18/err_unknown.txt")
  note_no_reason=$(dsh_pool_unavailable_owner_note "$WORK18/err_no_reason.txt")
  note_unrelated=$(dsh_pool_unavailable_owner_note "$WORK18/err_unrelated.txt")
  note_foreign_after=$(dsh_pool_unavailable_owner_note "$WORK18/err_foreign_after.txt")
  note_foreign_before=$(dsh_pool_unavailable_owner_note "$WORK18/err_foreign_before.txt")

  echo "18) auth_rejected:   $note_auth"
  echo "18) rate_limited:    $note_rate"
  echo "18) network_error:   $note_network"
  echo "18) unknown:         $note_unknown"
  echo "18) без reason:      $note_no_reason"
  echo "18) не пул вовсе:    $note_unrelated"
  echo "18) чужой reason после тела: $note_foreign_after"
  echo "18) чужой reason до тела:    $note_foreign_before"

  [[ "$note_auth" == *"владелец нужен"* ]] || { echo "::error::18) auth_rejected обязан назвать «владелец нужен»: $note_auth" >&2; exit 1; }
  [[ "$note_auth" == *"ANTHROPIC_OAUTH_1"* && "$note_auth" == *"ANTHROPIC_OAUTH_2"* ]] || { echo "::error::18) auth_rejected обязан назвать имена секретов на перевыпуск: $note_auth" >&2; exit 1; }
  [[ "$note_rate" == *"владелец НЕ нужен"* ]] || { echo "::error::18) rate_limited обязан назвать «владелец НЕ нужен»: $note_rate" >&2; exit 1; }
  [[ "$note_rate" == *"2026-09-14"* ]] || { echo "::error::18) rate_limited обязан назвать ретрай, вычисленный из retryAt (эпоха 1789345764948мс): $note_rate" >&2; exit 1; }
  [ "$note_auth" != "$note_rate" ] || { echo "::error::18) auth_rejected и rate_limited дали ОДИНАКОВЫЙ текст — ровно та проблема, ради которой заведена задача #1192" >&2; exit 1; }
  [[ "$note_network" == *"не подтверждено"* ]] || { echo "::error::18) network_error обязан честно назвать «не подтверждено»: $note_network" >&2; exit 1; }
  [[ "$note_unknown" == *"не подтверждено"* ]] || { echo "::error::18) unknown (плагин сам не смог классифицировать) обязан назвать «не подтверждено»: $note_unknown" >&2; exit 1; }
  # Находка ai-review PR #1193, раунд 4: класс unknown шире, чем «ни разу не
  # пробовался», — формулировка не имеет права утверждать факт «ответа не
  # было», который по записанному состоянию не проверялся.
  [[ "$note_unknown" != *"не получил ответ"* ]] || { echo "::error::18) unknown НЕ должен утверждать «ответа не было» — в recorded-состоянии мог быть ответ вне 401/403/429: $note_unknown" >&2; exit 1; }
  # Мутация класса «алерт не гадает»: без поля reason сообщение обязано
  # признать пробел, а НЕ выбрать одну из гипотез (auth_rejected/rate_limited)
  # наугад.
  [[ "$note_no_reason" == *"не классифицирована"* ]] || { echo "::error::18) без поля reason (прод-форма ДО патча) сообщение обязано признать пробел, не угадывать: $note_no_reason" >&2; exit 1; }
  [[ "$note_no_reason" != *"владелец нужен"* && "$note_no_reason" != *"владелец НЕ нужен"* ]] || { echo "::error::18) без поля reason сообщение НЕ должно утверждать о владельце ни в одну сторону (это и есть гадание) — $note_no_reason" >&2; exit 1; }
  [[ "$note_no_reason" == *"тело pool_unavailable есть"* ]] || { echo "::error::18) без поля reason (тело pool_unavailable ЕСТЬ) сообщение обязано это отличать от «тела вовсе не было»: $note_no_reason" >&2; exit 1; }
  # Отказ пула другой природы — тела pool_unavailable в stderr нет вовсе;
  # сообщение НЕ должно утверждать «поле reason отсутствует В ОТВЕТЕ ПУЛА»
  # (это факт, который в этом случае не проверялся — ai-review PR #1193).
  [[ "$note_unrelated" == *"не классифицирована"* ]] || { echo "::error::18) без тела pool_unavailable сообщение обязано признать пробел, не угадывать: $note_unrelated" >&2; exit 1; }
  [[ "$note_unrelated" != *"владелец нужен"* && "$note_unrelated" != *"владелец НЕ нужен"* ]] || { echo "::error::18) без тела pool_unavailable сообщение НЕ должно утверждать о владельце ни в одну сторону — $note_unrelated" >&2; exit 1; }
  [[ "$note_unrelated" == *"pool_unavailable в stderr не найдено"* ]] || { echo "::error::18) без тела pool_unavailable сообщение обязано назвать именно ЭТОТ факт (не «поле reason отсутствует В ответе», которого не было): $note_unrelated" >&2; exit 1; }
  [ "$note_unrelated" != "$note_no_reason" ] || { echo "::error::18) «тела нет вовсе» и «тело есть, поля reason нет» дали ОДИНАКОВЫЙ текст — разные факты, разные сообщения" >&2; exit 1; }
  # Чужой reason ПОСЛЕ тела (находка ai-review PR #1193, раунд 4): причина
  # читается ТОЛЬКО из строки тела — чужая строка ниже не приписывает пулу
  # «владелец нужен», класс остаётся честным пробелом.
  [[ "$note_foreign_after" == *"не классифицирована"* ]] || { echo "::error::18) чужой reason ПОСЛЕ тела: сообщение обязано держать честный пробел, не принимать чужой reason: $note_foreign_after" >&2; exit 1; }
  [[ "$note_foreign_after" == *"тело pool_unavailable есть"* ]] || { echo "::error::18) чужой reason ПОСЛЕ тела: тело-то БЫЛО, факт «тело есть» обязан сохраниться: $note_foreign_after" >&2; exit 1; }
  [[ "$note_foreign_after" != *"владелец нужен"* && "$note_foreign_after" != *"владелец НЕ нужен"* ]] || { echo "::error::18) чужой reason ПОСЛЕ тела принят за причину пула — скоуп вырезки шире строки тела (находка ai-review PR #1193, раунд 4): $note_foreign_after" >&2; exit 1; }
  [ "$note_foreign_after" != "$note_auth" ] || { echo "::error::18) «чужой reason после тела» дал тот же текст, что реальный auth_rejected — чужой факт неотличим от факта тела" >&2; exit 1; }
  # Чужой reason ДО тела: факт тела (auth_rejected) обязан победить, чужой
  # rate_limited строкой выше не перебивает его.
  [[ "$note_foreign_before" == *"владелец нужен"* ]] || { echo "::error::18) чужой reason ДО тела: факт ТЕЛА (auth_rejected) обязан остаться причиной: $note_foreign_before" >&2; exit 1; }
  [[ "$note_foreign_before" != *"rate_limited"* ]] || { echo "::error::18) чужой reason ДО тела (rate_limited) перебил факт тела (auth_rejected) — причина читается не из тела: $note_foreign_before" >&2; exit 1; }
) || fail "18) dsh_pool_unavailable_owner_note не различает исходы, гадает при отсутствии reason либо берёт причину не из строки тела"
echo "GUARD(anthropic-pool): 18) dsh_pool_unavailable_owner_note — восемь stderr-форм, каждый факт свой; чужой reason до/после тела не приписывается пулу, без reason — честный пробел — ок (#1192, раунды 3-4)"

# ── 19) #1192 (сквозной): dsh_run_with_pool_then_chain печатает ОБА факта
#       (текст отказа + причину «владелец нужен/не нужен») И честно
#       откатывается на цепочку — рабочая деградация (класс #1067/#838) не
#       сломана добавлением reason. Мок пула отвечает прод-формой ответа
#       ПОСЛЕ патча #1192 (с полем reason), мок цепочки отвечает успехом. ───
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh() {
    case "${1:-}" in
      --profile)
        if grep -q 'provider: anthropic-pool' "$HOME/.dsh/profiles/headless/cordis.patch.yml" 2>/dev/null; then
          echo 'dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789345764948,"reason":"auth_rejected","accounts":[{"id":"anthropic-1","class":"auth_rejected","lastStatus":401,"cooldownUntil":1789345764948}]}}' >&2
          return 1
        else
          touch "$CHAIN_CALLED_MARK"; echo "smoke: ответ от $DEEPSEEK_MODEL"; return 0
        fi ;;
      *) echo "::error::SMOKE(19): dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
    esac
  }
  export -f dsh
  POOL_LOG19="$WORK/pool-warning-19.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$POOL_LOG19" 2>&1
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::19) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $(cat "$POOL_LOG19")" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::19) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY (деградация на цепочку сломана)" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::19) цепочка обязана была запуститься после отказа пула" >&2; exit 1; }
  grep -q "pool_unavailable" "$POOL_LOG19" || { echo "::error::19) предупреждение потеряло исходный текст отказа (регресс #1067): $(cat "$POOL_LOG19")" >&2; exit 1; }
  grep -q "владелец нужен" "$POOL_LOG19" || { echo "::error::19) предупреждение не назвало «владелец нужен» для auth_rejected: $(cat "$POOL_LOG19")" >&2; exit 1; }
  grep -q "перевыпуск секретов ANTHROPIC_OAUTH_1 ANTHROPIC_OAUTH_2" "$POOL_LOG19" || { echo "::error::19) предупреждение не назвало конкретные секреты на перевыпуск: $(cat "$POOL_LOG19")" >&2; exit 1; }
) || fail "19) сквозной путь: reason виден в предупреждении, откат на цепочку не сломан"
echo "GUARD(anthropic-pool): 19) dsh_run_with_pool_then_chain — reason виден владельцу, откат на цепочку честный (класс #1067/#838 не сломан) — ок (#1192)"
# ── 20) #1288 (живой инцидент, прогон worker.yml 34893177035): пул отдаёт
#      прод-форму отказа ДОСЛОВНО из этого инцидента — `retryAt` уже в
#      прошлом к моменту прогона гвардии (фиксированная дата инцидента,
#      2026-09-14T20:35:14Z) — эффективный остаток отрицателен, что попадает
#      в ветку «близко» (клампится к 0, ретраим немедленно, не «далеко»).
#      Второй вызов пула отвечает успехом -> цепочка НЕ вызывается вовсе,
#      несмотря на то что первая попытка пула провалилась.
#
# MUTATION-PROOF
# ref: c1df957
# paths: scripts/lib/dsh-ci.sh
# run: bash scripts/lib/test/dsh-anthropic-pool.guard.sh
# expect: 20) прод-форма ответа пула из инцидента #1288 (34893177035) не приводит к повтору того же провайдера
#
# (ref — голова main ДО этого фикса, c1df957 (#1192/#1193): там
# dsh_run_with_pool_then_chain не читает retryAt вовсе и откатывается на
# цепочку одной попыткой — секция 20 красная с DSH_CHAIN_PROVIDER='PRIMARY'
# вместо 'anthropic-oauth-pool'. Прежний ref 8cd752e6 после ребейза не
# годится: он старше #1192, откат к нему вырезал бы и
# dsh_pool_unavailable_owner_note, гвардия умирала бы на секциях 15-19
# main, не доходя до секции 20 — живая находка обязательной проверки test
# на ребейзнутом хеде.) ────
POOL_RETRY_CALL_LOG="$WORK/pool-retry-calls.log"
# Дословное тело ответа пула из инцидента #1288 (прогон 34893177035,
# 20:30:30): retryAt=1789418114634мс = 2026-09-14T20:35:14Z.
POOL_BODY_INCIDENT_1288='dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":1789418114634}}'
dsh_pool_retry_stub() {
  case "${1:-}" in
    --profile)
      if grep -q 'provider: anthropic-pool' "$HOME/.dsh/profiles/headless/cordis.patch.yml" 2>/dev/null; then
        echo call >>"$POOL_RETRY_CALL_LOG"
        local n; n=$(wc -l <"$POOL_RETRY_CALL_LOG")
        local body_var="SMOKE_POOL_BODY_$n"
        local body="${!body_var:-}"
        if [ -z "$body" ]; then
          echo "smoke: ответ от anthropic-oauth-pool (попытка $n)"
          return 0
        fi
        echo "$body" >&2
        return 1
      else
        touch "$CHAIN_CALLED_MARK"
        echo "smoke: ответ от $DEEPSEEK_MODEL"
        return 0
      fi ;;
    *) echo "::error::SMOKE(retryAt): dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
  esac
}
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_POOL_BODY_1="$POOL_BODY_INCIDENT_1288"
  unset SMOKE_POOL_BODY_2 2>/dev/null || true
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log20.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::20) ожидался успех пула на повторе, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "anthropic-oauth-pool" ] || { echo "::error::20) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался anthropic-oauth-pool" >&2; exit 1; }
  [ ! -f "$CHAIN_CALLED_MARK" ] || { echo "::error::20) цепочка не должна была вызываться — retryAt близко, пул обязан был ответить на повторе" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "2" ] || { echo "::error::20) пул обязан быть вызван РОВНО дважды (первая попытка + повтор после retryAt): $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
  [[ "$OUT" == *"пул сам назвал момент возврата через"* ]] || { echo "::error::20) сообщение обязано честно назвать факт retryAt: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"жду и повторяю тем же провайдером (#1288)"* ]] || { echo "::error::20) сообщение обязано назвать намерение повторить тот же провайдер: $OUT" >&2; exit 1; }
) || fail "20) прод-форма ответа пула из инцидента #1288 (34893177035) не приводит к повтору того же провайдера"
echo "GUARD(anthropic-pool): 20) retryAt прод-формы инцидента #1288 близко -> пул повторён, цепочка не тронута — ок"

# ── 21) Бюджет ожидания retryAt — СУММАРНЫЙ, не per-попытка: первый
#      close-retryAt (20с) при бюджете 30с укладывается (30>20), второй
#      (50с) — уже НЕ укладывается в остаток (30-20=10 < 50) -> третьей
#      попытки пула нет, честный откат на цепочку с остатком бюджета в
#      сообщении. ─────────────────────────────────────────────────────────
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_POOL_RETRY_AT_MAX_WAIT_SECS=30
  now_ms=$(( $(date +%s) * 1000 ))
  # Первый retryAt (20с) укладывается в полный бюджет (30с); второй (50с) —
  # НЕ укладывается в остаток (30-~20=~10 < 50), даже с учётом того, что
  # `sleep` в этой гвардии — заглушка-no-op (реальное время между попытками
  # не проходит, оба retryAt считаются от одной и той же точки «сейчас») и
  # с учётом накладных расходов на процессы между вызовом `date` здесь и
  # внутри `_dsh_pool_retry_at_wait_secs` (секунды на Windows Git Bash из-за
  # спавна процессов patch_profile/jq/grep) — разрыв 20с/50с выбран заведомо
  # больше любого реалистичного дребезга.
  export SMOKE_POOL_BODY_1="dsh: SERVER: 503 {\"type\":\"error\",\"error\":{\"type\":\"pool_unavailable\",\"message\":\"No Anthropic account is available\",\"retryAt\":$((now_ms + 20000))}}"
  export SMOKE_POOL_BODY_2="dsh: SERVER: 503 {\"type\":\"error\",\"error\":{\"type\":\"pool_unavailable\",\"message\":\"No Anthropic account is available\",\"retryAt\":$((now_ms + 50000))}}"
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log21.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::21) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::21) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY (откат на цепочку после исчерпания бюджета)" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::21) цепочка обязана была запуститься — суммарный бюджет исчерпан" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "2" ] || { echo "::error::21) пул обязан быть вызван РОВНО дважды (первый retryAt уложился, второй — уже нет): $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
  [[ "$OUT" == *"жду и повторяю тем же провайдером (#1288)"* ]] || { echo "::error::21) первый retryAt (20с) обязан был уложиться в бюджет и вызвать повтор: $OUT" >&2; exit 1; }
  # Находка ai-review PR #1292 (некритичное): текст обязан называть ОСТАТОК
  # бюджета, а не полный бюджет — «дольше бюджета ожидания 30с» при
  # retryAt=50с и остатке 10с противоречило бы собственной скобке (тот же
  # класс «Алерт не гадает», AGENTS.md).
  [[ "$OUT" == *"это дольше остатка бюджета ожидания ("*"с из 30с, уже ждал "*"с)"* ]] || { echo "::error::21) второй retryAt (50с) обязан честно назвать превышение ОСТАТКА суммарного бюджета (Xс из 30с), не полного бюджета заново: $OUT" >&2; exit 1; }
) || fail "21) суммарный бюджет ожидания retryAt не учитывает уже потраченное на предыдущих повторах"
echo "GUARD(anthropic-pool): 21) бюджет ожидания retryAt суммируется по повторам, не выдаётся заново — ок (#1288)"

# ── 22) retryAt ДАЛЕКО за пределами бюджета уже на ПЕРВОЙ попытке -> пул
#      вызывается ровно один раз (без ожидания), честный откат на цепочку
#      называет и факт retryAt, и то, что порог превышен. ─────────────────
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_POOL_RETRY_AT_MAX_WAIT_SECS=300
  now_ms=$(( $(date +%s) * 1000 ))
  # #1288: далёкое будущее (часы вперёд) — потолок ожидания обязателен, его
  # превышение (в т.ч. когда retryAt может оказаться враньём/далёким
  # будущим) — законный повод идти дальше, а не ждать это время целиком.
  export SMOKE_POOL_BODY_1="dsh: SERVER: 503 {\"type\":\"error\",\"error\":{\"type\":\"pool_unavailable\",\"message\":\"No Anthropic account is available\",\"retryAt\":$((now_ms + 14400000))}}"
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log22.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::22) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::22) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::22) цепочка обязана была запуститься" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "1" ] || { echo "::error::22) пул НЕ обязан быть вызван повторно — retryAt (4 часа) дальше бюджета (300с) уже на первой попытке: $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
  [[ "$OUT" == *"пул назвал момент возврата через"* ]] || { echo "::error::22) сообщение обязано назвать факт retryAt: $OUT" >&2; exit 1; }
  # Находка ai-review PR #1292 (некритичное): здесь ожиданий ещё не было
  # (остаток = полному бюджету, ждал 0с) — текст обязан называть остаток.
  [[ "$OUT" == *"это дольше остатка бюджета ожидания (300с из 300с, уже ждал 0с)"* ]] || { echo "::error::22) сообщение обязано честно назвать превышение ОСТАТКА бюджета (300с из 300с): $OUT" >&2; exit 1; }
) || fail "22) далёкий retryAt не откатывается на цепочку честно"
echo "GUARD(anthropic-pool): 22) retryAt дальше бюджета ожидания -> без ожидания, честный откат на цепочку — ок (#1288)"

# ── 23) pool_unavailable БЕЗ поля retryAt (null — прод-форма плагина, когда
#      ни у одного аккаунта нет известного cooldownUntil, lib/index.js:
#      `next?.cooldownUntil || null`) -> ответ не разобран, откат на
#      цепочку СТАРЫМ путём (не пытаемся угадывать время ожидания). ───────
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_POOL_BODY_1='dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":null}}'
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log23.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::23) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::23) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "1" ] || { echo "::error::23) пул НЕ обязан быть вызван повторно — retryAt отсутствует (null), ждать нечего: $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
  [[ "$OUT" == *"отказал (rc=1), причина:"*"пробую цепочку"* ]] || { echo "::error::23) без разобранного retryAt сообщение обязано остаться старым (регрессия #838): $OUT" >&2; exit 1; }
  [[ "$OUT" != *"пул сам назвал момент возврата"* ]] || { echo "::error::23) retryAt=null не должен трактоваться как разобранный: $OUT" >&2; exit 1; }
) || fail "23) pool_unavailable без retryAt должен откатываться старым путём, не гадая время ожидания"
echo "GUARD(anthropic-pool): 23) pool_unavailable без retryAt -> ответ не разобран, откат старым путём — ок (#1288)"

# ── 24) Блокер ai-review PR #1292: пул УПОРНО отвечает retryAt В ПРОШЛОМ
#      (прод-форма плагина: cooldown +60с/+15с ставится в момент отказа
#      аккаунта ВНУТРИ прохода forward — когда проход дольше самого
#      короткого кулдауна, retryAt оказывается в прошлом, секция 11
#      фикстуры выше). Прошлое/нулевое retryAt клампится к нулю, ожидание
#      НЕ тратит бюджет — без отдельного потолка повторов цикл «занят →
#      повтор немедленно» не кончается никогда (живой замер ревьюера:
#      582 вызова пула за 15с, rc=124). Здесь пять тел с прошлым retryAt
#      подряд: потолок DSH_POOL_RETRY_AT_MAX_RETRIES=2 обязан остановить
#      цикл на РОВНО трёх вызовах пула (исходная + два повтора) и честно
#      уйти на цепочку — не на шестом вызове (когда кончились бы тела).
#      Мутация (снять счётчик повторов: `pool_retry_at_retries + 1` → `+ 0`),
#      исполненная на ребейзнутом хеде, даёт ДВА наблюдаемых исхода (находка
#      ai-review PR #1292, второй раунд; оба прогонены, не по памяти):
#      полный прогон гвардии ВИСНЕТ на секции 19 #1192 — её стаб вечно
#      отвечает телом с УЖЕ ПРОШЕДШИМ retryAt, цикл без счётчика не кончается,
#      процесс убит по таймауту (rc=124, вывод обрывается после секции 18);
#      в изоляции от того ствига (прогон без секции 19) секция 24 краснеет
#      числом вызовов — 6 вместо 3. Дословные выводы обоих прогонов — в
#      PR #1292. ────────
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export DSH_POOL_RETRY_AT_MAX_RETRIES=2
  now_ms=$(( $(date +%s) * 1000 ))
  # Пять тел подряд с retryAt на 5с в прошлом — тел ХВАТИЛО БЫ на шесть
  # вызовов пула без потолка (исходная + пять повторов), поэтому точное
  # «ровно 3» отличает работающий потолок от исчерпания тел заглушки.
  for k in 1 2 3 4 5; do
    export SMOKE_POOL_BODY_$k="dsh: SERVER: 503 {\"type\":\"error\",\"error\":{\"type\":\"pool_unavailable\",\"message\":\"No Anthropic account is available\",\"retryAt\":$((now_ms - 5000))}}"
  done
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log24.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::24) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "PRIMARY" ] || { echo "::error::24) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался PRIMARY — цикл повторов пула обязан кончиться потолком, не успехом пула" >&2; exit 1; }
  [ -f "$CHAIN_CALLED_MARK" ] || { echo "::error::24) цепочка обязана была запуститься — потолок повторов исчерпан" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "3" ] || { echo "::error::24) пул обязан быть вызван РОВНО трижды (исходная + DSH_POOL_RETRY_AT_MAX_RETRIES=2 повтора), получено $(wc -l <"$POOL_RETRY_CALL_LOG") — прошлое retryAt не тратит бюджет, без потолка цикл бесконечен: $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
  [[ "$OUT" == *"исчерпан потолок повторов пула (2 из 2"* ]] || { echo "::error::24) сообщение обязано честно назвать факт исчерпания потолка повторов с числами: $OUT" >&2; exit 1; }
  [[ "$OUT" == *"пробую цепочку"* ]] || { echo "::error::24) сообщение обязано назвать намерение уйти на цепочку: $OUT" >&2; exit 1; }
) || fail "24) пул, упорно отвечающий retryAt в прошлом, обязан упереться в потолок повторов и уйти на цепочку"
echo "GUARD(anthropic-pool): 24) прошлое retryAt пять подряд -> потолок повторов (3 вызова), честный откат на цепочку — ок (блокер ai-review PR #1292)"

# ── 25) Блокер ai-review PR #1292 (второй раунд): многострочный stderr —
#      ЧУЖИЕ JSON-строки ДО и ПОСЛЕ тела pool_unavailable (реальная форма:
#      клиент пишет лог-строки вокруг ответа, #1193 раунд 4). Жадная вырезка
#      «tr '\n' ' ' | grep -oE '\{.*\}'» брала от первой { до последней } и
#      ломалась на таком шуме (rc=1, «не разобран») — фикс #1288 молча
#      выключался ровно на многострочном stderr, прогон неотличим от
#      дофиксного. Вырезка теперь ПОСТРОЧНАЯ с якорем, одно место правды с
#      dsh_pool_unavailable_owner_note (_dsh_pool_unavailable_body): шум до
#      и после не мешает, retryAt разбирается, пул повторён. Мутация (вернуть
#      tr-жадную вырезку в _dsh_pool_retry_at_wait_secs) красит эту секцию —
#      «цепочка не должна была вызываться». ─────────────────────────────────
(
  dsh() { dsh_pool_retry_stub "$@"; }
  export -f dsh
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  now_ms=$(( $(date +%s) * 1000 ))
  export SMOKE_POOL_BODY_1="$(printf '%s\n%s\n%s' \
    "{\"level\":\"info\",\"msg\":\"request started\",\"retryAt\":$((now_ms + 14400000)),\"extra\":{\"a\":1}}" \
    "dsh: SERVER: 503 {\"type\":\"error\",\"error\":{\"type\":\"pool_unavailable\",\"message\":\"No Anthropic account is available\",\"retryAt\":$((now_ms + 10000))}}" \
    '{"level":"error","msg":"upstream unavailable","retryAt":"NOT_A_NUMBER"}')"
  unset SMOKE_POOL_BODY_2 2>/dev/null || true
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK" "$POOL_RETRY_CALL_LOG"; : >"$ANSWER"; : >"$ERR"
  LOG="$WORK/log25.txt"
  dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$LOG" 2>&1
  OUT="$(cat "$LOG")"
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::25) ожидался успех пула на повторе, получено rc=$DSH_RUN_RC: $OUT" >&2; exit 1; }
  [ "$DSH_CHAIN_PROVIDER" = "anthropic-oauth-pool" ] || { echo "::error::25) DSH_CHAIN_PROVIDER='$DSH_CHAIN_PROVIDER', ожидался anthropic-oauth-pool — шум до/после тела не должен ломать разбор" >&2; exit 1; }
  [ ! -f "$CHAIN_CALLED_MARK" ] || { echo "::error::25) цепочка не должна была вызываться — тело с retryAt среди шума обязано разобраться: $OUT" >&2; exit 1; }
  [ "$(wc -l <"$POOL_RETRY_CALL_LOG")" = "2" ] || { echo "::error::25) пул обязан быть вызван РОВНО дважды (разбор + повтор): $(cat "$POOL_RETRY_CALL_LOG")" >&2; exit 1; }
) || fail "25) тело pool_unavailable среди чужих JSON-строк до/после не разбирается"
echo "GUARD(anthropic-pool): 25) многострочный stderr (шум до/после тела) -> retryAt разобран, пул повторён — ок (блокер ai-review PR #1292, раунд 2)"

# ── 26) #1310: поаккаунтная разбивка (`accounts`, добавлена #1192 ради
#      различения «какой ключ живой») ДОХОДИТ ДО ЛОГА. Живой дефект: текстовый
#      хвост режется до 200 символов (#1067) и обрывается ровно на
#      `"accounts":[{"id":"anthropic-1","c` — прогоны worker.yml 34942030597
#      (2026-09-15T08:04:16Z) и 35010410097 (19:28:38Z). Тело здесь —
#      ДОСЛОВНАЯ прод-форма второго из них, дополненная полем accounts в том
#      виде, в каком его пишет патченный плагин (сценарий 17 выше доказывает
#      этот вид отдельно). Мутация: убери pool_accounts_note из сообщения —
#      предупреждение снова расскажет «rate_limited», не сказав, что именно
#      anthropic-2 в этом процессе не пробовался ни разу.
(
  export DSH_ANTHROPIC_POOL_ACTIVE=1
  export SMOKE_MODE_primary_model=ok
  rm -f "$CHAIN_CALLED_MARK"; : >"$ANSWER"; : >"$ERR"
  dsh() {
    case "${1:-}" in
      --profile)
        if grep -q 'provider: anthropic-pool' "$HOME/.dsh/profiles/headless/cordis.patch.yml" 2>/dev/null; then
          echo 'dsh: RATE_LIMIT: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":0,"reason":"rate_limited","accounts":[{"id":"anthropic-1","class":"rate_limited","lastStatus":429,"cooldownUntil":1789500803738},{"id":"anthropic-2","class":"unknown","lastStatus":null,"cooldownUntil":null}]}}' >&2
          return 1
        else
          touch "$CHAIN_CALLED_MARK"; echo "smoke: ответ от $DEEPSEEK_MODEL"; return 0
        fi ;;
      *) echo "::error::SMOKE(26): dsh-заглушка не знает вызов: $*" >&2; return 99 ;;
    esac
  }
  export -f dsh
  POOL_LOG26="$WORK/pool-warning-26.txt"
  DSH_RATE_LIMIT_MAX_WAIT_SECS=0 dsh_run_with_pool_then_chain "$ANSWER" "$ERR" "промпт smoke" >"$POOL_LOG26" 2>&1
  [ "$DSH_RUN_RC" = "0" ] || { echo "::error::26) ожидался успех после отката на цепочку, получено rc=$DSH_RUN_RC: $(cat "$POOL_LOG26")" >&2; exit 1; }
  grep -q "аккаунты: anthropic-1: rate_limited (HTTP 429)" "$POOL_LOG26" \
    || { echo "::error::26) разбивка по аккаунтам не доехала до лога — ровно тот дефект, ради которого #1192 клал accounts в тело: $(cat "$POOL_LOG26")" >&2; exit 1; }
  grep -q "anthropic-2: unknown" "$POOL_LOG26" \
    || { echo "::error::26) ВТОРОЙ аккаунт обязан быть назван: агрегат reason=rate_limited верен и когда второй ключ не пробовался вовсе: $(cat "$POOL_LOG26")" >&2; exit 1; }
) || fail "26) поаккаунтная разбивка пула не доехала до лога"
echo "GUARD(anthropic-pool): 26) accounts[] из тела pool_unavailable доходит до лога целиком — видно, какой из двух ключей живой (#1310) — ок"

# ── 27) #1310: разбивки нет в теле (плагин без патча #1192 / старая форма) —
#      честный пробел, не выдуманный факт и не падение.
NO_ACC_ERR="$WORK/err-27.txt"
printf '%s\n' 'dsh: SERVER: 503 {"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic account is available","retryAt":null}}' >"$NO_ACC_ERR"
[ -z "$(dsh_pool_accounts_note "$NO_ACC_ERR")" ] \
  || fail "27) тела без accounts обязано давать ПУСТО, а не выдуманную разбивку: $(dsh_pool_accounts_note "$NO_ACC_ERR")"
printf '%s\n' 'dsh: TRANSPORT: connection refused' >"$NO_ACC_ERR"
[ -z "$(dsh_pool_accounts_note "$NO_ACC_ERR")" ] \
  || fail "27) отказ вообще без тела pool_unavailable обязан давать ПУСТО"
echo "GUARD(anthropic-pool): 27) нет accounts в теле -> честный пробел, не выдуманный факт (#1310) — ок"

echo "GUARD(anthropic-pool): быстрый провайдер Claude (#838), инвариант #860 «пул только в worker/hands» — гвардия зелёная"
