#!/usr/bin/env node
// Интеграционная проверка Settings → Models на СОБРАННОМ воркере dsh-edge.
// Вызывается deploy-dsh-edge.yml после сборки (рядом с ingest-integration):
// поднимает standalone-артефакт (direct) через unstable_dev на реальном
// workerd + DO SQLite и прогоняет путь владельца штатного Settings → Models
// через те же RPC, что шлёт клиентский UI. С бампа пина до dsh-edge-v0.26.0
// (#1637/#1640) namespace llm-pi-ai монтирует сам апстрим
// (apps/dsh-edge/src/edge-llm-settings.ts::mountPiAiProvider, пакет
// @deepseek-ai/dsh-llm-pi-ai) — плагин provider-registry снят как затычка,
// чьё окно закрылось (исходный дефект #378 закрыт апстримом); directory
// готовых маршрутов больше НЕ публикуется (кнопка «Add» — пустой список,
// создание кастомного провайдера работает без него):
//
//   settings.describe → namespace llm-pi-ai смонтирован (кнопка «Add custom
//     provider» жива: схема содержит union-протокол из трёх значений
//     openai-completions / openai-responses / anthropic-messages);
//   llm.providers → штатный deepseek-official на месте и активен;
//   негатив: маршрут deepseek-official и кривой baseURL отказаны ПРИ ЗАПИСИ
//     (settings-rejected — ошибка видна в Settings, морда жива);
//   добавление провайдера (settings.mutate + credentials.set, как
//     CustomProviderCard) → маршрут активен, llm.models показывает его
//     каталог в пикере;
//   рестарт DO (второй unstable_dev над тем же persistTo) → настройка
//     пережила рестарт без передеплоя;
//   удаление (unset + credentials.unset, как removeProviderProfile) →
//     маршрут и ключ исчезли полностью.
//
// Использование: node check.mjs <APP_DIR>
//   APP_DIR — apps/dsh-edge клона апстрима на пине с применённой серией патчей
//   и выполненной кодогенерацией (в deploy это $GITHUB_WORKSPACE/clone/apps/dsh-edge).
//   Ожидает собранные standalone/worker/direct/index.js и standalone/dist,
//   wrangler — в standalone/node_modules.
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'

const appDir = process.argv[2]
if (!appDir) {
  console.error('Использование: node check.mjs <APP_DIR>')
  process.exit(1)
}
const standaloneDir = join(appDir, 'standalone')

// wrangler резолвится ИЗ клона: у этого скрипта своего node_modules нет.
const standaloneRequire = createRequire(join(standaloneDir, 'package.json'))
const { unstable_dev } = standaloneRequire('wrangler')

// Фиктивный ключ владельца ≥32 байт (см. комментарий в ingest-integration):
// короче — resolveOwnerAuthConfig отвечает 503 на каждый маршрут.
const AUTH_DUMMY = ['registry-check', 'owner', 'access', 'key-0123456789abcdef'].join('-')
const persistedState = mkdtempSync(join(tmpdir(), 'dsh-edge-registry-check-'))

/** Поднять воркер над общим persistedState (новый вызов = рестарт DO). */
async function boot() {
  const instance = await unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
    config: writeConfig(persistedState),
    env: '',
    persistTo: persistedState,
    vars: {
      DEEPSEEK_API_KEY: 'registry-unused',
      DSH_EDGE_ACCESS_KEY: AUTH_DUMMY,
    },
    logLevel: 'warn',
    experimental: {
      disableExperimentalWarning: true,
      showInteractiveDevSession: false,
      watch: false,
    },
  })
  return instance
}

function writeConfig(dir) {
  // Минимальный прям-режим: тот же состав биндингов, что у деплоя.
  const config = `{
    "name": "dsh-edge-registry-check",
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
  const configPath = join(dir, 'wrangler-registry-check.jsonc')
  writeFileSync(configPath, config)
  return configPath
}

let ownerCookie
function makeClient(worker) {
  const rpc = async (method, payload) => {
    const headers = new Headers({ 'content-type': 'application/json' })
    if (ownerCookie !== undefined) headers.set('cookie', ownerCookie)
    const response = await worker.fetch(`http://dsh-edge.test/api/${method}`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload }),
    })
    return { response, result: (await response.json()).result }
  }
  return { rpc }
}

/** Дождаться /api/health 200 ok (диагностика: печатает каждую попытку). */
async function waitReady(worker, label) {
  const origin = `http://${worker.address}:${worker.port}`
  const startedAt = Date.now()
  let attempt = 0
  while (Date.now() - startedAt < 90_000) {
    attempt += 1
    try {
      const probe = await fetch(`${origin}/api/health`, { signal: AbortSignal.timeout(5_000) })
      const text = await probe.text()
      console.log(`registry-check[${label}]: попытка ${attempt}: /api/health → ${probe.status} ${text.slice(0, 300)}`)
      // Парсим ПОЛНЫЙ текст: укороченный для лога ломает JSON.parse.
      if (probe.status === 200 && JSON.parse(text || '{}').ok === true) return origin
    } catch (error) {
      console.log(`registry-check[${label}]: попытка ${attempt}: недоступно (${error?.cause?.code ?? error?.message})`)
    }
    await new Promise(resolve => setTimeout(resolve, 5_000))
  }
  assert.fail(`[${label}] воркер не ответил 200 ok за 90 с`)
}

/** Логин владельца, возвращает куки. */
async function login(origin) {
  const response = await fetch(`${origin}/api/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ accessKey: AUTH_DUMMY }).toString(),
    redirect: 'manual',
    signal: AbortSignal.timeout(10_000),
  })
  assert.equal(response.status, 303, 'логин владельца')
  return response.headers.get('set-cookie')?.split(';', 1)[0]
}

/** Выбор протоколов ТОЧНО по тому пути, что клиент и registry.test.mjs
 *  (providers → dict-запись → object → api), а не первый попавшийся union
 *  где угодно в схеме (находка AI-ревью PR #237: union, случайно уехавший
 *  с этого пути, давал бы ложный зелёный на мёртвой кнопке создания).
 *  schemaJson — сырой {uid, refs} от Schema.prototype.toJSON (см.
 *  @deepseek-ai/schemastery): refs[uid].dict/.inner/.list хранят UID'ы
 *  дочерних узлов, не сами узлы — разрешаем на каждом шаге. */
function protocolChoices(schemaJson) {
  const refs = schemaJson?.refs ?? {}
  const ref = (uid) => (uid === undefined || uid === null ? undefined : refs[String(uid)])
  let node = ref(schemaJson?.uid)
  for (const key of ['providers', '\0probe', 'api']) {
    if (!node) return []
    if (node.type === 'object') node = ref(node.dict?.[key])
    else if (node.type === 'dict' || node.type === 'array') node = ref(node.inner)
    else return []
  }
  if (node?.type !== 'union' || !Array.isArray(node.list)) return []
  return node.list
    .map((uid) => ref(uid))
    .filter((member) => member?.type === 'const')
    .map((member) => member.value)
    .filter((value) => typeof value === 'string')
}

const ZHIPU_PROFILE = {
  displayName: 'Z.ai (GLM)',
  api: 'openai-completions',
  baseURL: 'https://open.bigmodel.cn/api/paas/v4',
  models: [{ id: 'glm-4.6', name: 'GLM-4.6', contextWindow: 204800, maxTokens: 131072 }],
}

// ── Фаза 1: монтирование, негатив при записи, добавление ─────────────────────
{
  const worker = await boot()
  try {
    await waitReady(worker, 'boot1')
    ownerCookie = await login(`http://${worker.address}:${worker.port}`)
    const { rpc } = makeClient(worker)

    const described = await rpc('settings.describe', {})
    assert.equal(described.result.ok, true, 'settings.describe')
    const view = described.result.value.namespaces.find((d) => d.ns === 'llm-pi-ai')
    assert.ok(view !== undefined, 'namespace llm-pi-ai смонтирован реестром — иначе кнопка создания мертва')
    assert.equal(described.result.value.writable, true, 'настройки морды writable')
    // Апстрим dsh-edge-v0.26.0 расширил union-протокол клиента (schema.namespace):
    // к openai-completions добавились openai-responses и anthropic-messages —
    // фиксируем новую прод-форму целиком (сорт, чтобы порядок union не ломал чек);
    // следующий бамп пина с изменением набора снова уронит чек громко.
    assert.deepEqual([...protocolChoices(view.schema)].sort(),
      ['anthropic-messages', 'openai-completions', 'openai-responses'],
      'schema.namespace должен нести union-протокол (protocolChoices клиента)')

    const providers = await rpc('llm.providers', {})
    assert.equal(providers.result.ok, true, 'llm.providers')
    const rows = providers.result.value.providers
    const official = rows.find((row) => row.provider === 'deepseek-official')
    assert.ok(official !== undefined, 'штатный провайдер морды на месте')
    assert.equal(official.active, true, 'штатный провайдер активен — селектор моделей жив')
    // Негатив при записи: занятие маршрута штатного провайдера — отказ
    // (валидация апстримного mountPiAiProvider: маршрут, который уже обслуживает
    // другой адаптер, отказан целиком). Текст отказа апстримный — подстроку не
    // фиксируем, фиксируем сам факт отказа записи.
    const reserved = await rpc('settings.mutate', {
      ns: 'llm-pi-ai',
      ops: [{ op: 'set', path: ['providers', 'deepseek-official'], value: { baseURL: 'https://x.example/v1', models: [{ id: 'm' }] } }],
    })
    assert.equal(reserved.result.ok, false, 'маршрут deepseek-official занят — запись отказана')

    // Негатив при записи: кривой baseURL отказан, морда жива.
    const bad = await rpc('settings.mutate', {
      ns: 'llm-pi-ai',
      ops: [{ op: 'set', path: ['providers', 'bad-route'], value: { baseURL: 'not-a-url', models: [{ id: 'm' }] } }],
    })
    assert.equal(bad.result.ok, false, 'кривой baseURL отказан при записи')
    const stillThere = await rpc('settings.describe', {})
    assert.equal(stillThere.result.ok, true, 'морда жива после отказанных записей')

    // Добавление провайдера ровно как CustomProviderCard: один settings.mutate
    // на профиль + credentials.set под производной ссылкой.
    const added = await rpc('settings.mutate', {
      ns: 'llm-pi-ai',
      ops: [{ op: 'set', path: ['providers', 'zhipu'], value: { ...ZHIPU_PROFILE, apiKeyEnv: 'ZHIPU_API_KEY' } }],
    })
    assert.equal(added.result.ok, true, `профиль zhipu записан: ${JSON.stringify(added.result.error ?? {})}`)
    const key = await rpc('credentials.set', { ref: 'ZHIPU_API_KEY', value: 'registry-check-dummy-key' })
    assert.equal(key.result.ok, true, 'ключ маршрута хранится в credential-хранилище морды')

    const afterAdd = await rpc('llm.providers', {})
    const zhipu = afterAdd.result.value.providers.find((row) => row.provider === 'zhipu')
    assert.ok(zhipu !== undefined, 'zhipu виден в llm.providers после добавления')
    assert.equal(zhipu.active, true, 'zhipu стал активным маршрутом')

    // Пикер моделей: группа zhipu с её каталогом (без сети — каталог локальный).
    const catalog = await rpc('llm.models', {})
    assert.equal(catalog.result.ok, true, 'llm.models')
    const group = catalog.result.value.groups.find((entry) => entry.id === 'zhipu')
    assert.ok(group !== undefined, `в пикере есть группа zhipu (groups: ${JSON.stringify(catalog.result.value.groups.map((g) => g.id))}, failures: ${JSON.stringify(catalog.result.value.failures)})`)
    assert.deepEqual(group.models.map((model) => model.id), ['glm-4.6'])
    const officialGroup = catalog.result.value.groups.find((entry) => entry.id === 'deepseek-official')
    assert.ok(officialGroup !== undefined, 'группа штатного провайдера в пикере не пострадала')

    console.log('registry-check: фаза 1 пройдена (namespace, негатив, добавление, пикер)')
  } finally {
    await worker.stop()
  }
}

// ── Фаза 2: рестарт DO — настройка переживает без передеплоя ─────────────────
{
  const worker = await boot()
  try {
    await waitReady(worker, 'boot2')
    ownerCookie = await login(`http://${worker.address}:${worker.port}`)
    const { rpc } = makeClient(worker)

    const described = await rpc('settings.describe', {})
    const view = described.result.value.namespaces.find((d) => d.ns === 'llm-pi-ai')
    assert.ok(view?.value?.providers?.zhipu !== undefined, 'профиль zhipu пережил рестарт DO')

    const after = await rpc('llm.providers', {})
    const zhipu = after.result.value.providers.find((row) => row.provider === 'zhipu')
    assert.ok(zhipu !== undefined && zhipu.active === true, 'после рестарта zhipu снова активен (плагин поднял его из DO storage)')

    // Удаление ровно как removeProviderProfile: unset ключа + unset профиля.
    const keyGone = await rpc('credentials.unset', { ref: 'ZHIPU_API_KEY' })
    assert.equal(keyGone.result.ok, true, 'ключ удалён из credential-хранилища')
    const removed = await rpc('settings.mutate', {
      ns: 'llm-pi-ai',
      ops: [{ op: 'unset', path: ['providers', 'zhipu'] }],
    })
    assert.equal(removed.result.ok, true, 'профиль удалён')

    const afterRemove = await rpc('llm.providers', {})
    const dormant = afterRemove.result.value.providers.find((row) => row.provider === 'zhipu')
    assert.equal(dormant, undefined, 'zhipu исчез из llm.providers после удаления (directory у апстрима нет)')
    const gone = await rpc('settings.describe', {})
    assert.equal(gone.result.value.namespaces.find((d) => d.ns === 'llm-pi-ai')?.value?.providers?.zhipu, undefined,
      'профиль zhipu в namespace больше нет')

    console.log('registry-check: фаза 2 пройдена (переживание рестарта, удаление)')
  } finally {
    await worker.stop()
  }
}

console.log('REGISTRY-CHECK OK: namespace, directory, негатив при записи, добавление, пикер, рестарт DO, удаление')
