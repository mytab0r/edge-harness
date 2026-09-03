#!/usr/bin/env node
// Интеграционная проверка канала конвейера (#86) на СОБРАННОМ воркере dsh-edge.
// Вызывается deploy-dsh-edge.yml после интеграции ingest-шва (#119): поднимает
// standalone-артефакт (direct) через unstable_dev на реальном workerd + DO
// SQLite и прогоняет путь статуса конвейера целиком:
//   логин → workspace.create → session.create/rename (harness-pipeline) →
//   ingest трёх событий статуса + одно «чужое» user/message →
//   GET /api/harness/events: форма строки журнала, фильтр по task_id,
//   пагинация after/limit, has_more/next_after, пустая выборка, 400 без
//   task_id, 405 на POST.
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
const standaloneRequire = createRequire(join(standaloneDir, 'package.json'))
const { unstable_dev } = standaloneRequire('wrangler')

// Фиктивный ключ владельца — как в ingest-integration: ≥32 байта, не литерал
// секрета (локальный unstable_dev, настоящих секретов нет).
const AUTH_DUMMY = ['pipeline-check', 'owner', 'access', 'key-0123456789abcdef'].join('-')
const persistedState = mkdtempSync(join(tmpdir(), 'dsh-edge-pipeline-check-'))

const PIPELINE_SESSION = 'harness-pipeline'
const PIPELINE_SOURCE = 'harness-pipeline-status'
const PIPELINE_WORKSPACE = '/workspace/edge-harness'

/** Строка статуса — форма писателя scripts/lib/dsh-edge-pipeline.sh. */
function statusEvent(taskId, kind, data, ts) {
  return {
    type: 'user/message',
    data: {
      id: crypto.randomUUID(),
      role: 'user',
      content: [{ type: 'text', text: JSON.stringify({ task_id: taskId, kind, data, source: 'check', ts }) }],
      source: { kind: PIPELINE_SOURCE },
    },
  }
}

function writeConfig(dir) {
  const config = `{
    "name": "dsh-edge-pipeline-check",
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
  const configPath = join(dir, 'wrangler-pipeline-check.jsonc')
  writeFileSync(configPath, config)
  return configPath
}

const worker = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
  config: writeConfig(persistedState),
  env: '',
  persistTo: persistedState,
  vars: {
    DEEPSEEK_API_KEY: 'pipeline-check-unused',
    DSH_EDGE_ACCESS_KEY: AUTH_DUMMY,
  },
  logLevel: 'warn',
  experimental: {
    disableExperimentalWarning: true,
    showInteractiveDevSession: false,
    watch: false,
  },
})

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
async function ingest(sessionId, events) {
  const { response, body } = await request(`/api/sessions/${sessionId}/ingest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ events }),
  }).then(async (response) => ({ response, body: await response.json() }))
  return { response, body }
}

try {
  // ── Готовность воркера ────────────────────────────────────────────────────────
  const entryOrigin = `http://${worker.address}:${worker.port}`
  const READINESS_DEADLINE_MS = 90_000
  let ready = false
  {
    const startedAt = Date.now()
    let attempt = 0
    while (Date.now() - startedAt < READINESS_DEADLINE_MS) {
      attempt += 1
      try {
        const probe = await fetch(`${entryOrigin}/api/health`, { signal: AbortSignal.timeout(5_000) })
        if (probe.status === 200 && JSON.parse((await probe.text()) || '{}').ok === true) {
          ready = true
          break
        }
      } catch {
        // воркер ещё бутится — повторяем до дедлайна
      }
      await new Promise(resolve => setTimeout(resolve, 5_000))
    }
  }
  assert.ok(ready, 'воркер не поднялся за 90 с (/api/health)')

  // ── Логин владельца ───────────────────────────────────────────────────────────
  const login = await request('/api/auth/login', {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ accessKey: AUTH_DUMMY }).toString(),
    redirect: 'manual',
  })
  assert.equal(login.status, 303, `логин владельца: HTTP ${login.status}`)
  ownerCookie = login.headers.get('set-cookie')?.split(';')[0]
  assert.ok(ownerCookie, 'кука владельца не выдана')

  // ── Воркспейс и сессия конвейера (как у писателя) ─────────────────────────────
  const ws = await rpc('workspace.create', { path: PIPELINE_WORKSPACE })
  assert.equal(ws.result?.ok, true, `workspace.create: ${JSON.stringify(ws.result)}`)
  const workspaceId = ws.result.value.workspace.workspaceId
  const created = await rpc('session.create', { workspaceId, sessionId: PIPELINE_SESSION })
  assert.equal(created.result?.ok, true, `session.create: ${JSON.stringify(created.result)}`)
  const renamed = await rpc('session.rename', { sessionId: PIPELINE_SESSION, title: 'Конвейер edge-harness' })
  assert.equal(renamed.result?.ok, true, 'session.rename отклонён')

  // ── Ingest статусов: два по plugin:hello, один по integration:jira, ───────────
  // одно «чужое» user/message (не конвейерное) и одно с текстом-не-JSON.
  const batch = [
    statusEvent('plugin:hello', 'plugin_status', { plugin: 'hello', state: 'deploying' }, 1_000),
    statusEvent('plugin:hello', 'plugin_status', { plugin: 'hello', state: 'ready', detail: '0.8.0' }, 2_000),
    statusEvent('integration:jira', 'integration_status', { integration: 'jira', state: 'not_configured' }, 3_000),
    { type: 'user/message', data: { id: crypto.randomUUID(), role: 'user', content: [{ type: 'text', text: 'человек печатал в сессию конвейера' }], source: { kind: 'human' } } },
    statusEvent('plugin:hello', 'plugin_status', 'не-JSON-текст', 4_000),
  ]
  const put = await ingest(PIPELINE_SESSION, batch)
  assert.equal(put.response.status, 200, `ingest: HTTP ${put.response.status}`)
  assert.equal(put.body.appended, batch.length, `ingest принял ${put.body.appended} из ${batch.length}`)

  // ── Форма ответа: журнал-контракт, фильтр по task_id, чужое скрыто ────────────
  const page1 = await jsonRequest('/api/harness/events?task_id=' + encodeURIComponent('plugin:hello') + '&limit=10')
  assert.equal(page1.response.status, 200)
  assert.equal(page1.response.headers.get('content-type')?.includes('json'), true, 'ответ не JSON')
  assert.ok(Array.isArray(page1.body.events), 'нет массива events')
  assert.equal(page1.body.has_more, false, 'has_more должен быть false для 2 событий при limit 10')
  assert.equal(page1.body.events.length, 2, `ожидается 2 события plugin:hello, пришло ${page1.body.events.length}`)
  const last = page1.body.events[page1.body.events.length - 1]
  for (const field of ['id', 'task_id', 'seq', 'ts', 'source', 'kind', 'data']) {
    assert.ok(field in last, `в строке журнала нет поля ${field}`)
  }
  assert.equal(last.task_id, 'plugin:hello')
  assert.equal(last.kind, 'plugin_status')
  assert.equal(last.data.state, 'ready', 'не последнее событие: читатель берёт хвост, порядок нарушен')
  assert.equal(last.data.detail, '0.8.0')
  assert.equal(last.source, 'check')
  assert.equal(last.ts, 2_000)
  assert.ok(Number.isInteger(last.id) && Number.isInteger(last.seq), 'id/seq обязаны быть числами')

  // ── Пагинация: limit=1 → первая (старейшая) строка + has_more/next_after ──────
  const p1 = await jsonRequest('/api/harness/events?task_id=' + encodeURIComponent('plugin:hello') + '&limit=1&after=0')
  assert.equal(p1.body.events.length, 1)
  assert.equal(p1.body.has_more, true, 'has_more потерян на границе страницы')
  const after = p1.body.next_after
  const p2 = await jsonRequest(`/api/harness/events?task_id=${encodeURIComponent('plugin:hello')}&limit=1&after=${after}`)
  assert.equal(p2.body.events.length, 1, 'вторая страница пуста — курсор не совпал')
  assert.equal(p2.body.has_more, false)
  assert.notEqual(p2.body.events[0].id, p1.body.events[0].id, 'страницы отдали одно и то же событие')
  assert.equal(p2.body.events[0].data.state, 'ready')

  // ── Пустая выборка и отказы по контракту ──────────────────────────────────────
  const none = await jsonRequest('/api/harness/events?task_id=' + encodeURIComponent('plugin:nope'))
  assert.deepEqual(none.body.events, [], 'несуществующий task_id обязан дать пустой список')
  const noTask = await jsonRequest('/api/harness/events')
  assert.equal(noTask.response.status, 400, 'без task_id ожидается 400 (журнальный контракт)')
  const forbidden = await request('/api/harness/events?task_id=x', { method: 'POST' })
  // Маршрут read-only: сам обработчик отвечает 405 на чужой метод; POST дополнительно
  // может упереться в разбор тела на входе в DO (400) — важно, что это НЕ 200.
  assert.equal(forbidden.status === 405 || forbidden.status === 400, true,
    `чужой метод обязан отказать (405/400), пришло ${forbidden.status}`)
  // Неавторизованный запрос — auth морды стоит до маршрута.
  const anon = await fetch(`${entryOrigin}/api/harness/events?task_id=x`)
  assert.notEqual(anon.status, 200, 'без куки владельца маршрут не отвечает 200')

  console.log('PIPELINE-CHECK OK: канал конвейера держит журнальный контракт (#86)')
} finally {
  await worker.stop()
}
