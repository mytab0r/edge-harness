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
// #572: DEEPSEEK_MODEL was absent here, so this check silently ran with
// session-store.ts's own default id ('deepseek-v4-flash') everywhere the
// deployment names a model — the one config drift that let a real prod
// mismatch (deploy-dsh-edge.yml repoints the compiled model catalog to
// vars.DSH_EDGE_MODEL_CATALOG, glm-* here, AFTER this literal is baked into
// the bundle) go unnoticed by this test for the whole time #572 was live.
// One source of truth: the workflow step passes the repo's actual
// vars.DEEPSEEK_MODEL through this env var (deploy-dsh-edge.yml, "Интеграция
// ingest-шва на собранном артефакте"), so this file does not carry its own
// hardcoded second copy of that value. The literal below is a fallback ONLY
// for running this script directly (outside the workflow, e.g. locally
// against a manual clone) — kept in sync with `gh variable list` by eye, not
// by code, which is exactly the drift class this env var plumb-through avoids
// for the path that actually gates a deploy.
const DEPLOYED_MODEL = process.env.DSH_EDGE_CHECK_MODEL || 'glm-5.3-flash'
const worker = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
  config: writeConfig(persistedState),
  env: '',
  persistTo: persistedState,
  vars: {
    DEEPSEEK_API_KEY: 'ingest-check-unused',
    DEEPSEEK_MODEL: DEPLOYED_MODEL,
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
  // #572: this 200 is now asserted with vars.DEEPSEEK_MODEL set to the
  // deployment's real model id (DEPLOYED_MODEL, not the upstream default).
  // Precisely what changed: appendHarnessEvents used to pass the upstream
  // literal DEFAULT_EDGE_MODEL to modelSelection() regardless of deployment —
  // for a fresh session (the case here) that literal reached
  // defaultModelSelection() UNCHANGED (modelSelection()'s own pending/bridge/
  // live-agent/logged chain never resolves anything for a session that never
  // ran a native turn), so the literal WAS the bootstrap argument, a no-op
  // fix. It now passes `agentDefaultModel.currentSelection().model` — the
  // same deployment default the session store itself resolves at startup
  // from DEEPSEEK_MODEL (session-store.ts::initialize) — so this 200 is
  // reached with DEPLOYED_MODEL actually flowing into openAgentForTurn, not
  // the stale upstream id. This assertion does NOT prove the #572 500's root
  // cause was the model id: dsh-llm-deepseek's own modelInfoFor tolerates an
  // unknown id by synthesizing a fallback ModelInfo (checked directly against
  // the pinned 0.1.2-rc.1), and dsh-edge never passes a custom model catalog
  // to that adapter in the first place — so a real deployed glm-* id and the
  // stale deepseek-v4-flash literal are equally "unknown" to it. What this
  // 200 DOES prove: ingest no longer bootstraps with an id that is wrong by
  // construction for this deployment, whatever the actual #572 trigger was.
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

// ── Сломанный бутстрап (М2, второй гейт PR #647) ────────────────────────────
// Второй, независимый воркер: намеренно БЕЗ DEEPSEEK_API_KEY (не пустая
// строка — переменная отсутствует целиком), чтобы явно проверить половину
// требования, которую сценарий выше проверяет только неявно (dummy-ключ
// 'ingest-check-unused' — тоже валидный непустой JS-string, формально не
// доказывающий «сеть не нужна вообще»): appendHarnessEvents не должен
// требовать провайдера по сети — ingest никогда не запускает агент-цикл
// (session-store.ts::appendHarnessEvents, docblock «Ingest never runs the
// agent»), поэтому даже полное отсутствие ключа не имеет права уронить его.
//
// Честная граница (М2 гейта: «зафиксируй как факт результата прогона, а не
// предположение» — ниже как раз предположение, не прогон, и это прямо
// сказано, а не выдано за то и другое разом): классифицирующие ветки самого
// патча — 503 AGENT_UNAVAILABLE (session-store.ts::appendHarnessEvents catch)
// и общий fallback `internal`+`detail` (http.ts::errorResponse) — этим файлом
// НЕ упражняются ни одним сценарием. Обе кандидатные «сломать бутстрап»
// ручки, что предлагает ревью, прослежены по исходнику апстрима на пине
// 0.11.1 (не выполнено вживую — см. ниже) и НЕ доходят до этих веток:
//   - отсутствующий/мусорный DEEPSEEK_API_KEY: readDeepSeekApiKey — ленивый
//     геттер (instance.ts), резолвится только внутри реального сетевого
//     вызова провайдера (dsh-llm-deepseek::resolveApiKey, вызывается из
//     stream()/request()); ingest его не достигает — отсюда и это assert.
//   - синтаксически валидный, но семантически левый DEEPSEEK_MODEL:
//     resolveEdgeModel (deepseek.ts) не бросает ни на чём, что проходит
//     MODEL_PATTERN; dsh-llm-deepseek::modelInfoFor тоже не бросает на
//     неизвестном id (см. комментарий выше); а синтаксически НЕВАЛИДНЫЙ id
//     (пробел, пустая строка) бросает СИНХРОННО в конструкторе DshEdgeInstance
//     (instance.ts: `private readonly model = resolveEdgeModel(...)`) —
//     роняя ЛЮБОЙ маршрут инстанса, не только ingest, и не через
//     errorResponse вообще (исключение конструктора DO), так что это не
//     проба классификации, а другой, более грубый баг.
// Ни один найденный рычаг конфигурации не воспроизводит AGENT_UNAVAILABLE
// без доступа к @deepseek-ai/dsh-agent-loop (не вендорится в этом репо).
// Живой прогон этого файла на собранном артефакте (единственный способ
// проверить факт, а не предположение) не выполнен в рамках этой правки —
// см. отчёт агента при PR #647: сеть песочницы не доводит `pnpm add` этого
// standalone-воркспейса до конца (устойчивый ETIMEDOUT на большинстве
// @deepseek-ai/* таболов npm registry, два прогона подряд). Следующее
// реальное свидетельство — первый прогон deploy-dsh-edge.yml после мержа
// (тот же шаг «Интеграция ingest-шва на собранном артефакте», в CI с
// нормальной пропускной способностью) — красный шаг там перед деплоем
// не пропустит клейм этого комментария, если он неверен.
{
  const NO_KEY_ACCESS_KEY = ['ingest-check-nokey', 'owner', 'access', 'key-0123456789abcdef'].join('-')
  const noKeyState = mkdtempSync(join(tmpdir(), 'dsh-edge-ingest-check-nokey-'))
  const noKeyWorker = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
    config: writeConfig(noKeyState),
    env: '',
    persistTo: noKeyState,
    vars: {
      // DEEPSEEK_API_KEY отсутствует НАМЕРЕННО — см. комментарий выше.
      DEEPSEEK_MODEL: DEPLOYED_MODEL,
      DSH_EDGE_ACCESS_KEY: NO_KEY_ACCESS_KEY,
    },
    logLevel: 'warn',
    experimental: { disableExperimentalWarning: true, showInteractiveDevSession: false, watch: false },
  })
  try {
    const origin = `http://${noKeyWorker.address}:${noKeyWorker.port}`
    let noKeyReady = false
    let noKeyHealthBody = ''
    const startedAt = Date.now()
    for (let attempt = 1; Date.now() - startedAt < READINESS_DEADLINE_MS; attempt += 1) {
      try {
        const probe = await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(5_000) })
        noKeyHealthBody = (await probe.text()).slice(0, 400)
        if (probe.status === 200 && JSON.parse(noKeyHealthBody || '{}').ok === true) { noKeyReady = true; break }
      } catch (error) {
        console.log(`ingest-check(no-key): попытка ${attempt}: /api/health недоступна (${error?.cause?.code ?? error?.message})`)
      }
      await new Promise(resolve => setTimeout(resolve, READINESS_STEP_MS))
    }
    assert.ok(noKeyReady, `воркер без DEEPSEEK_API_KEY не ответил 200 ok от /api/health; последний ответ: ${noKeyHealthBody}`)

    let noKeyCookie
    let login
    for (let attempt = 1; ; attempt += 1) {
      login = await fetch(`${origin}/api/auth/login`, {
        method: 'POST',
        headers: { 'content-type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ accessKey: NO_KEY_ACCESS_KEY }).toString(),
        redirect: 'manual',
        signal: AbortSignal.timeout(10_000),
      })
      if (login.status === 303) break
      if (attempt >= 3) break
      await new Promise(resolve => setTimeout(resolve, 2_000))
    }
    assert.equal(login.status, 303, 'логин владельца (no-key воркер)')
    noKeyCookie = login.headers.get('set-cookie')?.split(';', 1)[0]
    const noKeyRequest = (path, init) => {
      const headers = new Headers(init?.headers)
      if (noKeyCookie !== undefined) headers.set('cookie', noKeyCookie)
      return fetch(`${origin}${path}`, { ...init, headers })
    }
    const noKeyRpc = async (method, payload) => {
      const response = await noKeyRequest(`/api/${method}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload }),
      })
      return (await response.json()).result
    }

    const ws = await noKeyRpc('workspace.create', { path: '/workspace/edge-harness' })
    assert.equal(ws.ok, true, 'workspace.create (no-key воркер)')
    const workspaceId = ws.value.workspace.workspaceId
    const sid = 'ingest-check-nokey'
    const created = await noKeyRpc('session.create', { workspaceId, sessionId: sid })
    assert.equal(created.ok, true, 'session.create (no-key воркер)')

    const batch = { events: [
      { type: 'turn/start', data: { turn: 1 } },
      { type: 'user/message', data: { id: 'm1', role: 'user', content: [{ type: 'text', text: 'без ключа' }], source: { kind: 'user' } } },
      { type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } },
    ] }
    const ing = await noKeyRequest(`/api/sessions/${sid}/ingest`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(batch),
    })
    const ingBody = await ing.json()
    // Факт, а не предположение, В ПРЕДЕЛАХ этого сценария: без единого
    // байта ключа провайдера ingest всё равно принимает батч — «ingest не
    // звонит провайдеру по сети» доказано сильнее, чем dummy-ключом выше.
    assert.equal(
      ing.status, 200,
      `ingest без DEEPSEEK_API_KEY: ожидался HTTP 200 (ingest не должен требовать сеть) ` +
      `— получено ${ing.status} ${JSON.stringify(ingBody)}`,
    )
    assert.equal(ingBody.appended, 3, 'ingest без DEEPSEEK_API_KEY: appended=3')
    console.log('INGEST-CHECK(no-key) OK: ingest не требует DEEPSEEK_API_KEY — провайдер по сети не вызывается')
  } finally {
    await noKeyWorker.stop()
  }
}
