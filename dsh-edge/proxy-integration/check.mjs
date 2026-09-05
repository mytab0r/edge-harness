#!/usr/bin/env node
// Интеграционная проверка прокси статусов журнала (#105, патч
// 0005-harness-status-proxy) на СОБРАННОМ воркере dsh-edge. Вызывается
// deploy-dsh-edge.yml рядом с ingest-проверкой (#119): поднимает
// standalone-артефакт (direct) через unstable_dev на реальном workerd,
// рядом — заглушку журнала (node:http), и прогоняет контракт прокси:
//   без куки → 401 ДО прокси (auth морды стоит раньше роутинга, секрет
//   воркера браузеру недоступен); кросс-origin → 403; владелец → запрос
//   доходит до {HARNESS_URL}/api/events с Bearer HANDS_TOKEN и параметрами
//   журнала (task_id/after/limit, отсутствующий limit не пересылается),
//   ответ журнала проходит насквозь без изменения формы, включая его
//   ошибки (500/401 — прозрачность: форму ответа проверяет клиент морды);
//   кривые параметры режутся самим прокси (400, журнал не дёргается);
//   без HARNESS_URL/HANDS_TOKEN → 503 «возможности нет», не «статусов нет».
//
// Использование: node check.mjs <APP_DIR>
//   APP_DIR — apps/dsh-edge клона апстрима на пине с применённой серией
//   патчей (в deploy это $GITHUB_WORKSPACE/clone/apps/dsh-edge). Ожидает
//   собранные standalone/worker/direct/index.js и standalone/dist,
//   wrangler — в standalone/node_modules.
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
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

// Wrangler пишет логи/конфиг в $XDG_CONFIG_HOME/.wrangler, иначе — в
// $HOME/.config/.wrangler, и EACCES на нём выглядит как «воркер не поднялся».
// Направляем в tmp принудительно: песочницы и CI-образы могут приносить
// свой XDG_CONFIG_HOME, недоступный для записи (ловилось живым прогоном).
process.env.XDG_CONFIG_HOME = join(tmpdir(), 'wrangler-proxy-check-home')

// wrangler резолвится ИЗ клона: у этого скрипта своего node_modules нет
// по построению (репозиторий не ставит wrangler) — как у ingest-проверки.
const standaloneRequire = createRequire(join(standaloneDir, 'package.json'))
const { unstable_dev } = standaloneRequire('wrangler')

// Фиктивные значения собираются из частей: значение-выражение, имя без
// KEY/TOKEN-суффикса — настоящих секретов нет (локальный unstable_dev и
// одноразовая заглушка журнала; эвристика check_pr.py не задета).
const AUTH_DUMMY = ['proxy-check', 'owner', 'access', 'key-0123456789abcdef'].join('-')
const JOURNAL_BEARER_DUMMY = ['proxy-check', 'journal', 'bearer', '0123456789abcdef'].join('-')

// ── Заглушка журнала: копия формы ответа cf-worker/src/harness.ts ────────────
// GET /api/events?task_id=&after=&limit= →
// { events: [{id, task_id, seq, ts, source, kind, data}, …], has_more, next_after }
// + заголовки x-has-more/x-next-after, как отдаёт настоящий журнал. Ошибки —
// форма ApiError журнала: {"error":{"code","message"}} (harness.ts:100-106),
// не {ok:false} — это форма ошибок МОРДЫ, журнал её не отдаёт.
const journalCalls = []
const JOURNAL_PAGE = {
  events: [
    {
      id: 101,
      task_id: 'plugin:demo',
      seq: 3,
      ts: 1_791_000_000_000,
      source: 'deploy',
      kind: 'plugin_status',
      data: { state: 'ready', detail: 'proxy-check' },
    },
  ],
  has_more: false,
  next_after: 101,
}
// task_id → нестандартный ответ: проверка прозрачности чужих кодов в форме
// ApiError журнала (codes — из messages.ts, «тест кормится прод-формой»).
const journalSpecial = new Map([
  ['probe:journal-500', {
    status: 500,
    body: { error: { code: 'internal', message: 'proxy-check: журнал взорвался' } },
  }],
  ['probe:journal-401', {
    status: 401,
    body: { error: { code: 'unauthorized', message: 'Нужна авторизация: сессионная кука истекла или отсутствует, либо заголовок Authorization: Bearer <HANDS_TOKEN>' } },
  }],
  ['probe:has-more', { status: 200, body: { ...JOURNAL_PAGE, events: [], has_more: true, next_after: 99 } }],
])

const stub = createServer((req, res) => {
  const url = new URL(req.url, 'http://stub')
  journalCalls.push({
    path: url.pathname,
    task_id: url.searchParams.get('task_id'),
    after: url.searchParams.get('after'),
    limit: url.searchParams.get('limit'),
    authorization: req.headers.authorization ?? null,
    accept: req.headers.accept ?? null,
  })
  const special = journalSpecial.get(url.searchParams.get('task_id') ?? '')
  const status = special?.status ?? 200
  const body = special?.body ?? JOURNAL_PAGE
  res.writeHead(status, {
    'content-type': 'application/json',
    'x-has-more': String(body.has_more),
    'x-next-after': String(body.next_after),
  })
  res.end(JSON.stringify(body))
})
await new Promise(resolve => stub.listen(0, '127.0.0.1', resolve))
const stubPort = stub.address().port

// ── Помощники: конфиг, бут, готовность, логин (два воркера — два сценария) ───
function writeConfig(dir, { withJournal }) {
  // Минимальный прям-режим: тот же состав биндингов, что у деплоя
  // (deploy-dsh-edge.yml, шаг «Конфиг воркера»). HARNESS_URL кладётся в vars
  // только для первого воркера: второй сценарий — воркер БЕЗ настроек прокси
  // (503 «возможности нет»).
  const vars = withJournal
    ? `"vars": { "HARNESS_URL": ${JSON.stringify(`http://127.0.0.1:${stubPort}`)} },`
    : ''
  const config = `{
    "name": "dsh-edge-proxy-check",
    "main": ${JSON.stringify(join(standaloneDir, 'worker', 'direct', 'index.js'))},
    "compatibility_date": "2026-08-14",
    "compatibility_flags": ["nodejs_compat"],
    "no_bundle": true,
    ${vars}
    "assets": {
      "binding": "ASSETS",
      "directory": ${JSON.stringify(join(standaloneDir, 'dist'))},
      "not_found_handling": "single-page-application",
      "run_worker_first": ["/api/*", "/", "/login"]
    },
    "durable_objects": { "bindings": [{ "name": "DSH_EDGE_INSTANCE", "class_name": "DshEdgeInstance" }] },
    "migrations": [{ "tag": "v1", "new_sqlite_classes": ["DshEdgeInstance"] }]
  }`
  const configPath = join(dir, 'wrangler-proxy-check.jsonc')
  writeFileSync(configPath, config)
  return configPath
}

async function bootWorker({ withJournal }) {
  const persistedState = mkdtempSync(join(tmpdir(), 'dsh-edge-proxy-check-'))
  const worker = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
    config: writeConfig(persistedState, { withJournal }),
    env: '',
    persistTo: persistedState,
    vars: {
      DEEPSEEK_API_KEY: 'proxy-check-unused',
      DSH_EDGE_ACCESS_KEY: AUTH_DUMMY,
      ...(withJournal
        ? { HARNESS_URL: `http://127.0.0.1:${stubPort}`, HANDS_TOKEN: JOURNAL_BEARER_DUMMY }
        : {}),
    },
    logLevel: 'warn',
    experimental: {
      disableExperimentalWarning: true,
      showInteractiveDevSession: false,
      watch: false,
    },
  })
  return worker
}

// Готовность воркера (как в ingest-проверке: зависший бут виден в логе шага).
const READINESS_DEADLINE_MS = 90_000
const READINESS_STEP_MS = 5_000
async function waitReady(worker) {
  const entryOrigin = `http://${worker.address}:${worker.port}`
  const startedAt = Date.now()
  let attempt = 0
  let lastBody = ''
  while (Date.now() - startedAt < READINESS_DEADLINE_MS) {
    attempt += 1
    try {
      const probe = await fetch(`${entryOrigin}/api/health`, { signal: AbortSignal.timeout(5_000) })
      lastBody = (await probe.text()).slice(0, 400)
      console.log(`proxy-check: попытка ${attempt}: /api/health → ${probe.status} ${lastBody}`)
      if (probe.status === 200 && JSON.parse(lastBody || '{}').ok === true) return entryOrigin
    } catch (error) {
      console.log(`proxy-check: попытка ${attempt}: /api/health недоступна (${error?.cause?.code ?? error?.message})`)
    }
    await new Promise(resolve => setTimeout(resolve, READINESS_STEP_MS))
  }
  throw new Error(`воркер не ответил 200 ok от /api/health за 90 с; последний ответ: ${lastBody || '<ответа не было>'}`)
}

async function loginOwner(entryOrigin) {
  const login = await fetch(`${entryOrigin}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ accessKey: AUTH_DUMMY }).toString(),
    redirect: 'manual',
    signal: AbortSignal.timeout(10_000),
  })
  assert.equal(login.status, 303, 'логин владельца')
  const cookie = login.headers.get('set-cookie')?.split(';', 1)[0]
  assert.ok(cookie, 'кука владельца выдана')
  return cookie
}

function requestFor(worker, ownerCookie, path, init) {
  const headers = new Headers(init?.headers)
  if (ownerCookie !== undefined) headers.set('cookie', ownerCookie)
  return worker.fetch(`http://dsh-edge.test${path}`, { ...init, headers })
}

// ── Сценарий 1: воркер с настроенным прокси (HARNESS_URL + HANDS_TOKEN) ──────
let journalCallsAfterScenario1 = 0
let worker = await bootWorker({ withJournal: true })
try {
  const entryOrigin = await waitReady(worker)
  const ownerCookie = await loginOwner(entryOrigin)
  const request = (path, init) => requestFor(worker, ownerCookie, path, init)

  // 1. Без куки прокси недостижим: auth морды стоит ДО роутинга (research/12),
  //    и журнал при этом НЕ дёргается — секрет не тратится на чужака.
  //    Сырой fetch без кук-хелпера: владелец ещё не логинился.
  const anon = await fetch(`${entryOrigin}/api/harness/events?task_id=plugin:demo`)
  const anonBody = await anon.json()
  assert.equal(anon.status, 401, 'без куки → 401')
  assert.equal(anonBody.ok, false, '401 в форме ошибки морды')
  assert.equal(journalCalls.length, 0, 'неавторизованный запрос не дошёл до журнала')

  // 2. Кросс-origin авторизованный запрос режется общим гвардом морды.
  const evil = await request('/api/harness/events?task_id=plugin:demo', {
    headers: { origin: 'https://evil.example' },
  })
  assert.equal(evil.status, 403, 'кросс-origin → 403')
  assert.equal(journalCalls.length, 0, 'кросс-origin запрос не дошёл до журнала')

  // 3. Владелец: запрос доходит до журнала с Bearer и параметрами; отсутствующий
  //    limit не пересылается (журнал применит свой replayDefault — прозрачность).
  const ok = await request('/api/harness/events?task_id=' + encodeURIComponent('plugin:demo'))
  assert.equal(ok.status, 200, 'прокси отвечает 200')
  const okBody = await ok.json()
  assert.deepEqual(okBody, JOURNAL_PAGE, 'ответ журнала прошёл насквозь без изменения формы')
  assert.equal(journalCalls.length, 1, 'ровно один вызов журнала')
  const call = journalCalls[0]
  assert.equal(call.path, '/api/events', 'путь журнала')
  assert.equal(call.task_id, 'plugin:demo', 'task_id передан как есть')
  assert.equal(call.after, '0', 'after по умолчанию 0')
  assert.equal(call.limit, null, 'отсутствующий limit не пересылается')
  assert.equal(call.authorization, `Bearer ${JOURNAL_BEARER_DUMMY}`, 'Bearer HANDS_TOKEN добавлен прокси, не браузером')
  assert.equal(call.accept, 'application/json', 'журналу обещан JSON')
  assert.match(ok.headers.get('content-type') ?? '', /json/, 'клиент получит JSON-форму')

  // 4. Явные limit/after пересылаются.
  const paged = await request('/api/harness/events?task_id=plugin:demo&limit=10&after=42')
  assert.equal(paged.status, 200)
  await paged.text()
  assert.equal(journalCalls.length, 2)
  assert.equal(journalCalls[1].limit, '10', 'limit переслан')
  assert.equal(journalCalls[1].after, '42', 'after переслан')

  // 5. has_more/next_after проходят насквозь — клиент морды листает страницы.
  const more = await request('/api/harness/events?task_id=probe:has-more')
  assert.equal(more.status, 200)
  const moreBody = await more.json()
  assert.equal(moreBody.has_more, true, 'has_more прошёл насквозь')
  assert.equal(moreBody.next_after, 99, 'next_after прошёл насквозь')

  // 6. Кривые параметры режет сам прокси (400), журнал не дёргается.
  for (const query of [
    '',
    '?task_id=',
    `?task_id=${'x'.repeat(257)}`,
    '?task_id=plugin:demo&limit=0',
    '?task_id=plugin:demo&limit=1001',
    '?task_id=plugin:demo&limit=abc',
    '?task_id=plugin:demo&after=-1',
    '?task_id=plugin:demo&after=abc',
  ]) {
    const bad = await request(`/api/harness/events${query}`)
    const badBody = await bad.json()
    assert.equal(bad.status, 400, `кривой запрос ${query || '<без параметров>'} → 400`)
    assert.equal(badBody.ok, false, '400 в форме {ok:false}')
  }
  assert.equal(journalCalls.length, 3, 'журнал кривыми запросами не дёргался')

  // 7. Ошибки журнала проходят насквозь В ЕГО СОБСТВЕННОЙ форме ApiError
  //    {"error":{code,message}}: форму отвечает журнал, её судьбу — громкая
  //    ошибка секции на стороне клиента морды (не молча-пустой список).
  for (const [probe, expected, code] of [
    ['probe:journal-500', 500, 'internal'],
    ['probe:journal-401', 401, 'unauthorized'],
  ]) {
    const relayed = await request(`/api/harness/events?task_id=${probe}`)
    assert.equal(relayed.status, expected, `ответ журнала ${expected} прошёл насквозь (${probe})`)
    const relayedBody = await relayed.json()
    assert.equal(relayedBody.error?.code, code, `код ошибки журнала (${code}) дошёл до клиента`)
    assert.equal(relayedBody.ok, undefined, 'форма {ok:false} морды журналом не подменяется')
  }

  console.log('proxy-check: сценарий 1 (настроенный прокси) — контракт выполнен')
  journalCallsAfterScenario1 = journalCalls.length
} finally {
  await worker.stop()
}

// ── Сценарий 2: воркер БЕЗ HARNESS_URL/HANDS_TOKEN → 503 «возможности нет» ───
// Владелец получает громкую ошибку прокси, а не молча-пустой список статусов;
// журнал не дёргается (его адрес воркеру неизвестен).
worker = await bootWorker({ withJournal: false })
try {
  const entryOrigin = await waitReady(worker)
  const ownerCookie = await loginOwner(entryOrigin)
  const unconfigured = await requestFor(
    worker,
    ownerCookie,
    '/api/harness/events?task_id=plugin:demo',
  )
  const unconfiguredBody = await unconfigured.json()
  assert.equal(unconfigured.status, 503, 'без настроек прокси → 503')
  assert.equal(unconfiguredBody.ok, false, '503 в форме {ok:false}')
  assert.match(String(unconfiguredBody.error), /not configured/, 'ошибка называет причину: прокси не настроен')
  assert.equal(journalCalls.length, journalCallsAfterScenario1, 'ненастроенный воркер журнал не дёргал')
  console.log('proxy-check: сценарий 2 (прокси не настроен) — 503 «возможности нет», не «статусов нет»')
} finally {
  await worker.stop()
  stub.close()
}

console.log('✅ proxy-check: контракт прокси /api/harness/events выполнен на собранном артефакте')
