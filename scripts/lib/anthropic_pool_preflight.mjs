#!/usr/bin/env node
// Предполётная проверка аккаунтов пула Claude (#1311).
//
// Класс дефекта, который она закрывает: пул отвечает одним агрегатом
// `pool_unavailable` + `reason`, посчитанным через `some()` — и этот агрегат
// НЕ РАЗЛИЧАЕТ «оба аккаунта исчерпали квоту» от «один исчерпал, второй
// вообще не смог быть использован». Живой случай (прогоны worker.yml
// 34942030597 и 35010410097, 2026-09-15): `reason: rate_limited` печатался
// час подряд, владелец при этом РАБОТАЛ на втором аккаунте вручную — то есть
// вывод «квота у обоих, ждать сброса» был неверен, а данных отличить одно от
// другого в логе не было вовсе.
//
// Почему аккаунт может выбыть молча (lib/index.js плагина, ветка catch вокруг
// ensureFresh): отказ OAuth-рефреша даёт `lastError` + cooldown 15с и класс
// `network_error`, который в приоритете classifyPoolUnavailable НИЖЕ
// `rate_limited` соседа. Снаружи это неотличимо от квоты.
//
// Отдельная, названная вслух причина, по которой refreshToken в секрете
// протухает сам: Anthropic РОТИРУЕТ refresh-токен на каждом рефреше
// (`refreshToken: next.refresh_token || oauth.refreshToken`, lib/pool.js
// плагина). Живая сессия владельца на его машине рефрешится и уводит токен
// вперёд; в секрете ANTHROPIC_OAUTH_<slot> лежит СНИМОК, сделанный раньше, и
// он перестаёт приниматься. Аккаунт при этом совершенно жив у владельца —
// «не работает» только наш снимок. Лечение — рунбук
// docs/runbooks/refresh-anthropic-pool.md (свежий экспорт krouter).
//
// Что делает эта проверка:
//   1. Для КАЖДОГО аккаунта пула отдельно: рефреш (если нужен) + один дешёвый
//      GET /v1/models?limit=1 — и печатает ФАКТ по каждому: OK / RATE_LIMITED
//      (с датой сброса) / AUTH_REJECTED / REFRESH_FAILED / HTTP_<код>.
//   2. Сохраняет обновлённый accessToken в файл аккаунта — пул на своём первом
//      запросе рефреш уже не делает (и не спотыкается на нём в разгар работы).
//   3. Аккаунт, который заведомо непригоден ПРЯМО СЕЙЧАС (REFRESH_FAILED /
//      AUTH_REJECTED), помечается disabled в pool.json ЭТОГО job'а — пул не
//      тратит на него попытку. Газ: $HOME job'а эфемерен, состояние живёт
//      один прогон; следующий прогон проверяет заново, руками ничего
//      возвращать не нужно.
//
// Секреты: ни один токен (ни access, ни refresh, ни их производные) не
// печатается — только id аккаунта, класс, код ответа HTTP и дата сброса
// (AGENTS.md, «Секреты»: репозиторий публичный, GitHub маскирует только
// точное совпадение со значением секрета, не производное).
//
// Константы OAuth (эндпоинт, client_id, беты, база API) НЕ дублируются здесь:
// читаются из исходника самого плагина, уже распакованного и сверенного по
// sha256 (dsh_install_anthropic_pool). Форма не совпала — падаем громко, как
// scripts/lib/patch_anthropic_pool_plugin.py, а не подставляем своё значение.
//
// Использование: node anthropic_pool_preflight.mjs <распакованный package/>
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const pkgDir = process.argv[2]
if (!pkgDir) {
  console.error('usage: anthropic_pool_preflight.mjs <распакованный каталог package/>')
  process.exit(2)
}

const indexSrc = fs.readFileSync(path.join(pkgDir, 'lib', 'index.js'), 'utf8')
function constFromPlugin(name) {
  const match = indexSrc.match(new RegExp(`const ${name} = '([^']+)'`))
  if (!match) {
    throw new Error(`PLUGIN_SHAPE_CHANGED: константа ${name} не найдена в lib/index.js плагина — ` +
      'предполётная проверка не умеет угадывать её значение (#1311)')
  }
  return match[1]
}
const API_BASE = constFromPlugin('API_BASE')
const TOKEN_URL = constFromPlugin('TOKEN_URL')
const CLIENT_ID = constFromPlugin('CLIENT_ID')
const betasMatch = indexSrc.match(/const OAUTH_BETAS = \[([^\]]+)\]/)
if (!betasMatch) throw new Error('PLUGIN_SHAPE_CHANGED: OAUTH_BETAS не найдены в lib/index.js плагина (#1311)')
const OAUTH_BETAS = betasMatch[1].split(',').map((s) => s.trim().replace(/^'|'$/g, '')).filter(Boolean)

const accounts = await import(pathToFileURL(path.join(pkgDir, 'lib', 'accounts.js')).href)

function headersFor(token) {
  return {
    authorization: `Bearer ${token}`,
    'anthropic-version': '2023-06-01',
    'anthropic-beta': OAUTH_BETAS.join(','),
    'user-agent': 'claude-cli/2.1.74 (external, sdk-cli)',
    'x-app': 'cli',
    'x-anthropic-billing-header': 'cc_entrypoint=sdk-cli;cch=00000;',
  }
}

async function refresh(refreshToken) {
  const response = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'application/json' },
    body: JSON.stringify({ grant_type: 'refresh_token', client_id: CLIENT_ID, refresh_token: refreshToken }),
    signal: AbortSignal.timeout(20_000),
  })
  if (!response.ok) {
    // Тело ответа НЕ печатается: оно может содержать эхо отправленного токена.
    const error = new Error(`HTTP ${response.status}`)
    error.httpStatus = response.status
    throw error
  }
  return response.json()
}

function resetNote(response) {
  const reset = response.headers.get('anthropic-ratelimit-unified-5h-reset')
    || response.headers.get('anthropic-ratelimit-unified-7d-reset')
    || response.headers.get('retry-after')
  if (!reset) return ''
  const n = Number(reset)
  if (!Number.isFinite(n)) return ''
  const ms = n > 10_000_000_000 ? n : (n > 1_600_000_000 ? n * 1000 : Date.now() + n * 1000)
  return `, сброс ~${new Date(ms).toISOString()}`
}

const config = accounts.readConfig()
const rows = config.accounts || []
if (!rows.length) {
  console.log('предполётная проверка пула: в pool.json нет ни одного аккаунта — проверять нечего (#1311)')
  process.exit(0)
}

let usable = 0
const verdicts = []
for (const row of rows) {
  let account
  try {
    account = accounts.readAccount(row.id)
  } catch (error) {
    verdicts.push({ id: row.id, verdict: 'BROKEN_FILE', detail: String(error?.message || error).slice(0, 120), usable: false })
    continue
  }
  const oauth = account.oauth || {}
  // Рефреш — ТОЛЬКО когда сам токен объявил срок и срок близок/прошёл.
  // ОТСУТСТВИЕ expiresAt значит «срок неизвестен», а НЕ «срок истёк»: это
  // класс #1130 (живой прогон worker.yml 34753001158) — прежняя версия
  // плагина рефрешила превентивно на каждый запрос для любого аккаунта без
  // expiresAt, в том числе для ДОЛГОЖИВУЩЕГО токена владельца, которому
  // рефреш не нужен вовсе и у которого refreshToken может отсутствовать
  // физически. Условие здесь обязано совпадать с патченным
  // pool.js::createRefreshCoordinator (patch_anthropic_pool_plugin.py,
  // PATCH_POOL_SKIP_CONDITION) — иначе проверка «лечила» бы то, чего нет, и
  // гасила рабочий аккаунт.
  const needsRefresh = Boolean(oauth.expiresAt) && oauth.expiresAt - Date.now() <= 5 * 60 * 1000
  let token = oauth.accessToken
  if (!token) {
    verdicts.push({ id: row.id, verdict: 'NO_ACCESS_TOKEN', detail: 'в секрете нет accessToken', usable: false })
    continue
  }
  // Рефреш вообще возможен только когда ЕСТЬ чем: без refreshToken пробуем
  // тот токен, что лежит в секрете. Это не «поломка секрета», а штатный
  // случай долгоживущего токена (#1130); жив он или нет — решает проба ниже,
  // а не наше предположение.
  let refreshNote = 'токен из секрета, рефреш не требовался'
  if (needsRefresh && oauth.refreshToken) {
    try {
      const next = await refresh(oauth.refreshToken)
      token = next.access_token
      refreshNote = 'токен обновлён по refreshToken'
      account.oauth = {
        ...oauth,
        accessToken: next.access_token,
        refreshToken: next.refresh_token || oauth.refreshToken,
        expiresAt: Date.now() + (Number(next.expires_in) || 3600) * 1000,
      }
      accounts.writeAccount(row.id, account)
    } catch (error) {
      verdicts.push({
        id: row.id,
        verdict: 'REFRESH_FAILED',
        detail: `expiresAt истёк, и OAuth-рефреш отверг refreshToken (${error.httpStatus ? 'HTTP ' + error.httpStatus : String(error?.message || error).slice(0, 80)})`,
        usable: false,
      })
      continue
    }
  } else if (needsRefresh) {
    refreshNote = 'expiresAt истёк, refreshToken в секрете нет — проверяю тот токен, что есть'
  }
  try {
    const probe = await fetch(`${API_BASE}/v1/models?limit=1`, {
      headers: headersFor(token), signal: AbortSignal.timeout(20_000),
    })
    if (probe.status === 200) {
      // ЧЕСТНАЯ ГРАНИЦА: /v1/models — не инференс, и 200 здесь доказывает
      // ЖИВЫЕ КРЕДЫ, а не наличие квоты на запросы к модели. Аккаунт,
      // исчерпавший лимит /v1/messages, здесь вполне может ответить 200.
      // Не называть это «аккаунт работает» — ровно та подмена измеренного
      // предполагаемым, из-за которой агрегат reason и читался как «квота у
      // обоих» (#1311).
      verdicts.push({
        id: row.id,
        verdict: 'OK',
        detail: refreshNote
          + ', креды приняты Anthropic (квота на инференс этим запросом НЕ проверяется: /v1/models не инференс)',
        usable: true,
      })
      usable += 1
    } else if (probe.status === 429) {
      verdicts.push({ id: row.id, verdict: 'RATE_LIMITED', detail: `квота Anthropic исчерпана (HTTP 429${resetNote(probe)})`, usable: false })
    } else if (probe.status === 401 || probe.status === 403) {
      verdicts.push({ id: row.id, verdict: 'AUTH_REJECTED', detail: `Anthropic отверг доступ (HTTP ${probe.status})`, usable: false })
    } else {
      verdicts.push({ id: row.id, verdict: `HTTP_${probe.status}`, detail: 'не 200/401/403/429 — класс не распознан', usable: false })
    }
  } catch (error) {
    verdicts.push({ id: row.id, verdict: 'NETWORK_ERROR', detail: String(error?.message || error).slice(0, 80), usable: false })
  }
}

for (const v of verdicts) console.log(`аккаунт ${v.id}: ${v.verdict} — ${v.detail}`)

// Заведомо непригодные ПРЯМО СЕЙЧАС — выключить на этот прогон, чтобы пул не
// тратил на них попытку. RATE_LIMITED не выключается: его cooldown пул
// посчитает сам из заголовков, и квота может отпустить в середине прогона.
const disable = new Set(verdicts.filter((v) => ['REFRESH_FAILED', 'AUTH_REJECTED', 'NO_ACCESS_TOKEN', 'BROKEN_FILE'].includes(v.verdict)).map((v) => v.id))
if (disable.size) {
  const fresh = accounts.readConfig()
  for (const row of fresh.accounts || []) if (disable.has(row.id)) row.enabled = false
  accounts.writeConfig(fresh)
  console.log(`выключены на этот прогон (эфемерный $HOME, следующий прогон проверит заново): ${[...disable].join(', ')}`)
}

const owner = verdicts.filter((v) => ['REFRESH_FAILED', 'AUTH_REJECTED', 'NO_ACCESS_TOKEN'].includes(v.verdict))
if (usable === 0) {
  console.log('::warning::предполётная проверка пула: ни один аккаунт Claude не пригоден в этом прогоне — '
    + verdicts.map((v) => `${v.id}=${v.verdict}`).join(', ')
    + (owner.length ? ` — нужен владелец: перевыпустить ANTHROPIC_OAUTH для ${owner.map((v) => v.id).join(', ')} (docs/runbooks/refresh-anthropic-pool.md)` : '')
    + ' (#1311)')
} else {
  console.log(`предполётная проверка пула: пригодных аккаунтов ${usable} из ${rows.length} (#1311)`)
  if (owner.length) {
    console.log('::warning::предполётная проверка пула: ' + owner.map((v) => `${v.id} — ${v.detail}`).join('; ')
      + ' — пул продолжит на остальных, но этот аккаунт из ротации выпал и сам не вернётся: '
      + 'нужен свежий экспорт (docs/runbooks/refresh-anthropic-pool.md, #1311). '
      + 'Частая причина: Anthropic ротирует refreshToken на каждом рефреше, и снимок в секрете отстал от живой сессии владельца.')
  }
}
process.exit(0)
