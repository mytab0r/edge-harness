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
POOL_CALL_RE='dsh_install_anthropic_pool|dsh_import_anthropic_accounts|dsh_mount_anthropic_pool|dsh_run_with_pool_then_chain'
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
#       структурно недоступна в headless (см. комментарий у функции).
#       baseURL патча обязан указывать на ТОТ ЖЕ порт, что экспортируется в
#       DSH_ANTHROPIC_POOL_PORT (сервер плагина слушает именно эту
#       переменную), apiKeyEnv обязан резолвиться в НЕПУСТОЕ значение той же
#       переменной окружения. Мутация (снять provider-блок из фикса) красит
#       эту секцию — доказательство приложено в PR текстом обоих прогонов. ──
(
  export HOME="$(mktemp -d)"
  _dsh_patch_profile_anthropic_pool headless
  PATCH_FILE="$HOME/.dsh/profiles/headless/cordis.patch.yml"
  [ -f "$PATCH_FILE" ] || { echo "::error::10) $PATCH_FILE не создан" >&2; exit 1; }
  grep -q '^- id: llm-pi-ai$' "$PATCH_FILE" || { echo "::error::10) патч не содержит секцию llm-pi-ai — провайдер anthropic-pool не зарегистрирован статически (регресс #1097, ctx.get('settings') недоступен в headless): $(cat "$PATCH_FILE")" >&2; exit 1; }
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
ANTHROPIC_POOL_INDEX_JS_FIXTURE_OK='const name = "dsh-anthropic-oauth-pool"

function apply(ctx) {
  let port = 47291
  let models = [{ id: "claude-sonnet-4-5", name: "Claude Sonnet 4.5", contextWindow: 200000, maxTokens: 64000 }]

  async function discoverModels() {}

  async function ensureProvider() {
    await discoverModels()
    try { await ctx.get('"'"'credentials'"'"').set(CREDS_REF, '"'"'managed-by-anthropic-pool'"'"') } catch {}
    const provider = { displayName: '"'"'Anthropic OAuth Pool'"'"', apiKeyEnv: CREDS_REF, api: '"'"'anthropic-messages'"'"', baseURL: `http://127.0.0.1:${port}`, models }
    const settings = ctx.get('"'"'settings'"'"')
    if (typeof settings.update === '"'"'function'"'"') await settings.update('"'"'llm-pi-ai'"'"', { providers: { [PROVIDER_KEY]: provider } })
    else if (typeof settings.mutate === '"'"'function'"'"') await settings.mutate('"'"'llm-pi-ai'"'"', [{ op: '"'"'add'"'"', path: ['"'"'providers'"'"', PROVIDER_KEY], value: provider }])
    else throw new Error('"'"'DSH settings service cannot install the Anthropic pool provider'"'"')
  }
}

export { name, apply }
'
(
  FIXTURE_DIR="$(mktemp -d)"
  mkdir -p "$FIXTURE_DIR/lib"
  printf '%s' "$ANTHROPIC_POOL_INDEX_JS_FIXTURE_OK" >"$FIXTURE_DIR/lib/index.js"
  # Не пересказ: строка ensureProvider ниже — ТОЧНАЯ копия
  # dsh-anthropic-oauth-pool-0.1.0.tgz (релиз dsh-plugins-suite-v1),
  # инспектирована живьём при разборе #1097/#1130. Сверяем байт-в-байт с
  # тем, что реально проверяет патч-скрипт (OLD-константа), не с нашим
  # пересказом её содержимого.
  grep -q "const settings = ctx.get('settings')" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) фикстура сама не содержит ожидаемую строку — тест сломан до патча" >&2; exit 1; }
  if ! python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR/lib/index.js" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::11) патч не применился к прод-форме фикстуры: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1
  fi
  # Ищем именно ЖИВОЙ вызов (`const settings = ctx.get(...)`), не подстроку
  # "ctx.get('settings')" целиком — наш же поясняющий комментарий в патче
  # ЗАКОННО упоминает эту фразу текстом (находка при первом прогоне этой
  # секции: голый grep по подстроке ловил СОБСТВЕННЫЙ комментарий патча как
  # ложное срабатывание).
  grep -q "const settings = ctx.get(" "$FIXTURE_DIR/lib/index.js" && { echo "::error::11) после патча живой вызов 'const settings = ctx.get(...)' всё ещё присутствует — self-регистрация НЕ нейтрализована" >&2; exit 1; }
  grep -q "settings.update(" "$FIXTURE_DIR/lib/index.js" && { echo "::error::11) после патча settings.update(...) всё ещё вызывается" >&2; exit 1; }
  grep -q "async function ensureProvider" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) патч удалил саму функцию ensureProvider вместо нейтрализации тела" >&2; exit 1; }
) || fail "11) патч плагина (happy path) не нейтрализует self-регистрацию в прод-форме"
echo "GUARD(anthropic-pool): 11a) патч нейтрализует ctx.get('settings') в прод-форме ensureProvider — ок (#1130)"

(
  FIXTURE_DIR="$(mktemp -d)"
  mkdir -p "$FIXTURE_DIR/lib"
  printf '%s' "$ANTHROPIC_POOL_INDEX_JS_FIXTURE_OK" >"$FIXTURE_DIR/lib/index.js"
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
  if python3 "$REPO/scripts/lib/patch_anthropic_pool_plugin.py" "$FIXTURE_DIR/lib/index.js" >"$FIXTURE_DIR/patch.log" 2>&1; then
    echo "::error::11) патч ОБЯЗАН был отказать на изменённой форме ensureProvider, но применился молча" >&2; exit 1
  fi
  grep -qi "PATCH_MARKER_NOT_FOUND" "$FIXTURE_DIR/patch.log" || { echo "::error::11) отказ патча не назвал причину PATCH_MARKER_NOT_FOUND: $(cat "$FIXTURE_DIR/patch.log")" >&2; exit 1; }
  grep -q "const settingsService = ctx.get('settings')" "$FIXTURE_DIR/lib/index.js" || { echo "::error::11) файл фикстуры не должен был измениться при отказе патча" >&2; exit 1; }
) || fail "11) патч не падает громко на изменённой форме ensureProvider (мутация #1130)"
echo "GUARD(anthropic-pool): 11b) мутация формы ensureProvider -> патч отказывает громко (PATCH_MARKER_NOT_FOUND), файл не тронут — ок (#1130)"

echo "GUARD(anthropic-pool): быстрый провайдер Claude (#838), инвариант #860 «пул только в worker/hands» — гвардия зелёная"
