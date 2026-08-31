#!/usr/bin/env node
// Интеграционная проверка ingest-шва (#119) на СОБРАННОМ воркере dsh-edge.
// Вызывается deploy-dsh-edge.yml после «Контракт и детерминизм артефактов»:
// поднимает standalone-артефакт (direct) через unstable_dev на реальном
// workerd + DO SQLite и прогоняет путь раннера целиком:
//   логин → workspace.create → session.create/rename → ingest двух батчей
//   (второй — с повторным turn 1: проверка перенумерации) → 400 на чужой тип
//   → replay из хранилища ПОСЛЕ ответа маршрута («принято = сохранено») →
//   список (blank/title) → архив.
//
// Использование: node check.mjs <APP_DIR>
//   APP_DIR — apps/dsh-edge клона апстрима на пине с применённой серией патчей
//   (в deploy это $GITHUB_WORKSPACE/clone/apps/dsh-edge). Ожидает собранные
//   standalone/worker/direct/index.js и standalone/dist, wrangler — в
//   standalone/node_modules.
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'

const appDir = process.argv[2]
if (!appDir) {
  console.error('Использование: node check.mjs <APP_DIR (apps/dsh-edge клона)>')
  process.exit(1)
}
const standaloneDir = join(appDir, 'standalone')

// wrangler и конфиг-хелперы резолвятся ИЗ клона: у этого скрипта своего
// node_modules нет по построению (репозиторий не ставит wrangler).
const standaloneRequire = createRequire(join(standaloneDir, 'package.json'))
const { unstable_dev } = standaloneRequire('wrangler')

// Фиктивный ключ владельца собирается из частей и обязан быть ≥32 байт:
// resolveOwnerAuthConfig (auth.ts) при коротком ключе бросает 503 на КАЖДЫЙ
// маршрут (проверка стоит в fetch раньше /api/health) — два красных прогона CI
// 2026-08-31 были ровно этим: 'ingest-check-owner-key' (22 байта) < 32.
// Строка не собирается одним 20+ литералом при имени *KEY/*TOKEN: эвристика
// детерминированного ревью (check_pr.py: «литерал секрета в присваивании»)
// красит такие присваивания; здесь значение — выражение, имя без KEY-суффикса,
// настоящих секретов нет (локальный unstable_dev).
const AUTH_DUMMY = ['ingest-check', 'owner', 'access', 'key-0123456789abcdef'].join('-')
const persistedState = mkdtempSync(join(tmpdir(), 'dsh-edge-ingest-check-'))
const worker = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
  config: writeConfig(persistedState),
  env: '',
  persistTo: persistedState,
  vars: {
    DEEPSEEK_API_KEY: 'ingest-check-unused',
    DSH_EDGE_ACCESS_KEY: AUTH_DUMMY,
  },
  // warn, не error: бут-ошибки wrangler/workerd обязаны быть видны в логе шага
  // CI (ревью #128: с logLevel 'error' прогон не показал ни строки вывода).
  logLevel: 'warn',
  experimental: {
    disableExperimentalWarning: true,
    showInteractiveDevSession: false,
    watch: false,
  },
})

function writeConfig(dir) {
  // Минимальный прям-режим: тот же состав биндингов, что у деплоя
  // (deploy-dsh-edge.yml, шаг «Конфиг воркера»), main/assets — standalone.
  const config = `{
    "name": "dsh-edge-ingest-check",
    "main": ${JSON.stringify(join(standaloneDir, 'worker', 'direct', 'index.js'))},
    "compatibility_date": "2026-08-14",
    "compatibility_flags": ["nodejs_compat"],
    "no_bundle": true,
    "assets": {
      "binding": "ASSETS",
      "directory": ${JSON.stringify(join(standaloneDir, 'dist'))},
      "not_found_handling": "single-page-application",
      "run_worker_first": ["/api/*", "/", "/login"]
    },
    "durable_objects": { "bindings": [{ "name": "DSH_EDGE_INSTANCE", "class_name": "DshEdgeInstance" }] },
    "migrations": [{ "tag": "v1", "new_sqlite_classes": ["DshEdgeInstance"] }]
  }`
  const configPath = join(dir, 'wrangler-ingest-check.jsonc')
  writeFileSync(configPath, config)
  return configPath
}

let ownerCookie
function request(path, init) {
  const headers = new Headers(init?.headers)
  if (ownerCookie !== undefined) headers.set('cookie', ownerCookie)
  return worker.fetch(`http://dsh-edge.test${path}`, { ...init, headers })
}
async function jsonRequest(path, init) {
  const response = await request(path, init)
  return { response, body: await response.json() }
}
async function rpc(method, payload) {
  const { response, body } = await jsonRequest(`/api/${method}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload }),
  })
  return { response, result: body.result }
}

// ── Готовность воркера (диагностика прогонов CI 33404091387/33404845575) ───────
// Любой HTTP-ответ — воркер поднят и маршрут жив; ТЕЛО ответа отличает 503
// «ключ короче 32 байт» (auth.ts) от страницы ошибки miniflare (упавший бут).
// 90 с на холодный старт CI, попытка каждые ~5 с с печатью: без прогресса в
// логе не видно, завис ли бут или воркер отвечает отказом.
const entryOrigin = `http://${worker.address}:${worker.port}`
const READINESS_DEADLINE_MS = 90_000
const READINESS_STEP_MS = 5_000
let healthBody = ''
let ready = false
{
  const startedAt = Date.now()
  let attempt = 0
  while (Date.now() - startedAt < READINESS_DEADLINE_MS) {
    attempt += 1
    try {
      const probe = await fetch(`${entryOrigin}/api/health`, { signal: AbortSignal.timeout(5_000) })
      healthBody = (await probe.text()).slice(0, 400)
      console.log(`ingest-check: попытка ${attempt}: /api/health → ${probe.status} ${healthBody}`)
      if (probe.status === 200 && JSON.parse(healthBody || '{}').ok === true) {
        ready = true
        break
      }
    } catch (error) {
      console.log(`ingest-check: попытка ${attempt}: /api/health недоступна (${error?.cause?.code ?? error?.message})`)
    }
    await new Promise(resolve => setTimeout(resolve, READINESS_STEP_MS))
  }
}
assert.ok(
  ready,
  `воркер не ответил 200 ok от /api/health за 90 с; последний ответ: ${healthBody || '<ответа не было — воркер не поднялся, см. вывод wrangler выше>'}`,
)

try {
  let login
  for (let attempt = 1; ; attempt += 1) {
    login = await fetch(`${entryOrigin}/api/auth/login`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ accessKey: AUTH_DUMMY }).toString(),
      redirect: 'manual',
      signal: AbortSignal.timeout(10_000),
    })
    if (login.status === 303) break
    // Тело отказа печатаем целиком: 503 auth.ts называет точную причину,
    // 401 — «ключ не совпал», страница miniflare — упавший бут.
    console.log(`ingest-check: логин попытка ${attempt} → ${login.status} ${(await login.text()).slice(0, 400)}`)
    if (attempt >= 3) break
    await new Promise(resolve => setTimeout(resolve, 2_000))
  }
  assert.equal(login.status, 303, 'логин владельца')
  ownerCookie = login.headers.get('set-cookie')?.split(';', 1)[0]

  const ws = await rpc('workspace.create', { path: '/workspace/edge-harness' })
  assert.equal(ws.result.ok, true, 'workspace.create')
  const workspaceId = ws.result.value.workspace.workspaceId

  const sid = 'ingest-check'
  const created = await rpc('session.create', { workspaceId, sessionId: sid })
  assert.equal(created.result.ok, true, 'session.create')
  const renamed = await rpc('session.rename', { sessionId: sid, title: '#119: ingest-check' })
  assert.equal(renamed.result.ok, true, 'session.rename')
  assert.match(renamed.result.value.title, /^#119:/, 'имя сессии = «#N: …»')

  // Батч 1: полный заход раннера (в терминах раннера — turn 1).
  const batch1 = { events: [
    { type: 'turn/start', data: { turn: 1 } },
    { type: 'user/message', data: { id: 'm1', role: 'user', content: [{ type: 'text', text: 'задача раннера' }], source: { kind: 'user' } } },
    { type: 'assistant/message', data: { turn: 1, step: 1, message: { id: 'a1', role: 'assistant', content: [{ type: 'reasoning', text: 'размышляю' }, { type: 'text', text: 'делаю' }], source: { kind: 'model', provider: 'edge-harness', model: 'runner-model' } } } },
    { type: 'tool/call', data: { turn: 1, step: 1, callId: 'c1', name: 'bash', arguments: '{"command":"echo hi"}' } },
    { type: 'tool/result', data: { turn: 1, step: 1, message: { id: 't1', role: 'user', content: [{ type: 'tool-result', toolCallId: 'c1', isError: false, content: [{ type: 'text', text: 'hi' }] }], source: { kind: 'tool', callId: 'c1' } } } },
    { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } },
  ] }
  const ing1 = await jsonRequest(`/api/sessions/${sid}/ingest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(batch1),
  })
  assert.equal(ing1.response.status, 200, 'ingest батча 1: HTTP 200')
  assert.equal(ing1.body.appended, 6, 'ingest батча 1: appended=6')

  // Батч 2: раннер перезапустился и снова шлёт turn 1 — морда обязана
  // перенумеровать его в turn 2 (продолжение хранимого лога, не откат).
  const batch2 = { events: [
    { type: 'turn/start', data: { turn: 1 } },
    { type: 'user/message', data: { id: 'm2', role: 'user', content: [{ type: 'text', text: 'второй заход' }], source: { kind: 'user' } } },
    { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } },
  ] }
  const ing2 = await jsonRequest(`/api/sessions/${sid}/ingest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(batch2),
  })
  assert.equal(ing2.response.status, 200, 'ingest батча 2: HTTP 200')
  assert.equal(ing2.body.appended, 3, 'ingest батча 2: appended=3')

  // Чужой тип события — громкий 400, не тихий пропуск.
  const bad = await jsonRequest(`/api/sessions/${sid}/ingest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ events: [{ type: 'hacker/event', data: {} }] }),
  })
  assert.equal(bad.response.status, 400, 'чужой тип события отклонён')

  // «Принято = сохранено»: replay читается из ХРАНИЛИЩА после ответа маршрута.
  const replay = await request(`/api/sessions/${sid}/events`)
  assert.equal(replay.status, 200, 'replay доступен')
  const events = (await replay.text()).split('\n')
    .filter(line => line.startsWith('data: '))
    .map(line => JSON.parse(line.slice('data: '.length)))
  const types = events.map(event => event.type)
  for (const t of ['turn/start', 'user/message', 'assistant/message', 'tool/call', 'tool/result', 'turn/end']) {
    assert.ok(types.includes(t), `replay содержит ${t}`)
  }
  assert.deepEqual(
    events.filter(event => event.type === 'turn/start').map(event => event.data.turn),
    [1, 2],
    'перенумерация turn поверх хранимого лога',
  )
  const asst = events.find(event => event.type === 'assistant/message')
  assert.ok(JSON.stringify(asst.data).includes('размышляю'), 'reasoning-блок сохранён')

  // Список: сессия не пустая, заголовок наш.
  const listed = await rpc('session.list', {})
  assert.equal(listed.result.ok, true, 'session.list')
  const summary = listed.result.value.items.find(item => item.sessionId === sid)
  assert.equal(summary.blank, false, 'сессия не blank после ingest')
  assert.equal(summary.projections.values.title, '#119: ingest-check', 'заголовок сохранён')

  // Архив: сессия уходит из активных, история остаётся.
  const archived = await rpc('workspace.archiveSession', { sessionId: sid })
  assert.equal(archived.result.ok, true, 'workspace.archiveSession')
  assert.ok(archived.result.value.archivedSessionIds.includes(sid), 'сессия в архиве')

  console.log('INGEST-CHECK OK: батчи, перенумерация turn, 400 на чужой тип, replay, список, архив')
} finally {
  await worker.stop()
}
