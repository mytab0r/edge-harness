/**
 * Поведенческая гвардия клиентского плагина plugin-manager (#102, заказ — #113).
 *
 * Проверяется форма обёртки бандла (что собрал build.mjs), монтаж в слот
 * settings.section, паритет ключей словарей, поведение ячеек статусов на
 * ПРОДА-форме данных журнала: { events: [{id, kind, data}], has_more,
 * next_after } (контракт cf-worker/src/harness.ts), а также конвейер заказа
 * (#113) на ПРОДА-форме RPC морды (контракт @deepseek-ai/dsh-host-apiproxy
 * 0.1.1-rc.2, docs/research/12-dsh-edge-session-api.md):
 * {type:"server-response", result:{ok,value}|{ok:false,error:{code,message}}}.
 *
 * Запуск: node --test plugins-src/plugin-manager/test/client.test.mjs
 * (тест сам прогоняет build.mjs — продукт генерируется перед проверкой).
 *
 * react и ui-primitives здесь — стабы: настоящий react приезжает в браузере
 * из seed-карты шелла; стаб покрывает только контракт, который бандл
 * использует (createElement/useState/useCallback/useEffect + Button/StateDot).
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginDir = dirname(fileURLToPath(import.meta.url)) // …/plugin-manager/test
const packageDir = dirname(pluginDir)

// ── Сборка: тот же шаг, что и перед npm pack ──────────────────────────────────
// Синхронно ДО чтения продукта ниже: тесты проверяют ровно то, что собралось,
// а не бандл предыдущего прогона.
const buildResult = spawnSync(process.execPath, [join(packageDir, 'build.mjs')], { encoding: 'utf8' })

test('build.mjs собирает продукт без ошибок', async () => {
  assert.equal(buildResult.status, 0, `build.mjs упал:\n${buildResult.stdout}\n${buildResult.stderr}`)
  await assert.doesNotReject(() => readFile(join(packageDir, 'client', 'client.js'), 'utf8'))
  await assert.doesNotReject(() => readFile(join(packageDir, 'manifest.json'), 'utf8'))
})

const bundle = await readFile(join(packageDir, 'client', 'client.js'), 'utf8')
const manifestCopy = JSON.parse(await readFile(join(packageDir, 'manifest.json'), 'utf8'))

function extractBakedManifest() {
  const match = bundle.match(/const MANIFEST = (\[[\s\S]*?\]);\n/)
  assert.ok(match, 'в бандле нет константы MANIFEST')
  return JSON.parse(match[1])
}

function extractBakedCatalog() {
  const match = bundle.match(/const CATALOG = (\[[\s\S]*?\]);\n/)
  assert.ok(match, 'в бандле нет константы CATALOG')
  return JSON.parse(match[1])
}

/** Доступные к заказу — та же формула вычитания, что в бандле (#113). */
function computeAvailable() {
  const catalog = extractBakedCatalog()
  const manifest = extractBakedManifest()
  return catalog.filter((entry) => !manifest.some((plugin) => plugin.id === entry.id))
}

// ── Песочница: ModuleLoader + стабы react/primitives ──────────────────────────
function createElement(type, props, ...children) {
  const element = {
    type, props: props ?? {},
    children: children.flat(Infinity).filter(
      (child) => child !== null && child !== undefined && child !== false,
    ),
  }
  // Функциональные компоненты вызываются сразу — стаб не строит очередь
  // рендера; дереву теста нужен весь состав за один проход.
  if (typeof type === 'function') return type({ ...element.props, children: element.children })
  return element
}

async function flush() {
  // fetch-стабы резолвятся сразу; цепочки load()/loadOrders()/order()
  // (allSettled → setState) укладываются в несколько раундов макротасок.
  for (let round = 0; round < 5; round += 1) {
    await new Promise((resolve) => setImmediate(resolve))
  }
}

function loadBundle(fetchStub) {
  const sandbox = { effects: [], cells: [], cursor: 0 }
  sandbox.window = { __ModuleLoader__: { load(registration) { sandbox.registered = registration } } }
  sandbox.fetch = fetchStub
  sandbox.console = console
  sandbox.react = {
    createElement,
    useState(initial) {
      const index = sandbox.cursor++
      if (sandbox.cells[index] === undefined) {
        sandbox.cells[index] = typeof initial === 'function' ? initial() : initial
      }
      return [sandbox.cells[index], (value) => {
        // Функциональный updater — как в react: следующее состояние из
        // предыдущего. Заказ (#113) обновляет кнопку именно так, чтобы
        // параллельные заказы не затирали друг друга протухшим замыканием.
        sandbox.cells[index] = typeof value === 'function' ? value(sandbox.cells[index]) : value
      }]
    },
    useCallback(fn) { return fn },
    useEffect(fn) { sandbox.effects.push(fn) },
  }
  sandbox.primitives = {
    StateDot: function StateDot(props) { return { type: 'StateDot', props: { ...props }, children: [] } },
    Button: function Button(props) { return { type: 'Button', props: { ...props }, children: [props.children] } },
  }
  sandbox.require = (specifier) => {
    if (specifier === 'react') return sandbox.react
    if (specifier === '@deepseek-ai/dsh-client-ui-primitives') return sandbox.primitives
    throw new Error(`неизвестный require: ${specifier}`)
  }
  vm.runInNewContext(bundle, sandbox, { filename: 'client.js' })
  assert.ok(sandbox.registered, 'бандл не зарегистрировал фабрику в __ModuleLoader__')
  const exports = sandbox.registered.factory(sandbox.require)

  const mount = { slot: null, declaration: null, component: null, dictionaries: {} }
  const ctx = {
    effect(fn) { fn() },
    locale: {
      register: (ns, dicts) => { mount.dictionaries[ns] = dicts },
      bind: (ns) => (key) => `${ns}:${key}`,
    },
    slots: {
      inject: (slot, callback) => { mount.slot = slot; mount.callback = callback },
      register: (declaration, component) => { mount.declaration = declaration; mount.component = component },
    },
  }
  exports.apply(ctx)
  assert.equal(mount.slot, 'settings.section', 'apply смонтировал не settings.section')
  mount.callback()
  assert.ok(mount.component, 'колбэк слота не зарегистрировал компонент')

  /** Ре-рендер: ячейки состояния живут в sandbox между рендерами. */
  sandbox.render = () => {
    sandbox.cursor = 0
    sandbox.effects = []
    sandbox.tree = mount.component({ t: (key) => key })
    return sandbox.tree
  }
  sandbox.runEffectsAndSettle = async () => {
    for (const effect of sandbox.effects) effect()
    await flush()
  }
  return { sandbox, exports, mount }
}

function collectStrings(node, out = []) {
  if (node === null || node === undefined || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { for (const child of node) collectStrings(child, out); return out }
  if (typeof node === 'object') {
    if (node.props !== undefined && node.props !== null) collectStrings(node.props.children, out)
    collectStrings(node.children, out)
  }
  return out
}

/** Кнопки дерева: стаб Button — узел с type 'Button' и props.onClick. */
function collectButtons(node, out = []) {
  if (node === null || node === undefined || typeof node !== 'object') return out
  if (Array.isArray(node)) { for (const child of node) collectButtons(child, out); return out }
  if (node.type === 'Button' && typeof node.props?.onClick === 'function') out.push(node.props)
  for (const child of node.children ?? []) collectButtons(child, out)
  if (node.props !== undefined && node.props !== null) collectButtons(node.props.children, out)
  return out
}

/**
 * Список каталога секции: ul с aria-label "catalogTitle" (отличает его от
 * списка установленных с aria-label "title").
 */
function catalogListNode(tree) {
  const stack = [tree]
  while (stack.length > 0) {
    const node = stack.pop()
    if (node === null || node === undefined || typeof node !== 'object') continue
    if (Array.isArray(node)) { stack.push(...node); continue }
    if (node.type === 'ul' && node.props?.['aria-label'] === 'catalogTitle') return node
    stack.push(node.children ?? [])
    if (node.props !== undefined && node.props !== null) stack.push(node.props.children ?? [])
  }
  return null
}

/** id строк каталога: где-то в строке есть code с id записи (вложен в блок описания). */
function catalogRowIds(tree) {
  const list = catalogListNode(tree)
  if (list === null) return null
  const findCodeId = (node) => {
    const stack = [node]
    while (stack.length > 0) {
      const current = stack.pop()
      if (current === null || current === undefined || typeof current !== 'object') continue
      if (Array.isArray(current)) { stack.push(...current); continue }
      if (current.type === 'code') return current.children[0]
      stack.push(current.children ?? [])
      if (current.props !== undefined && current.props !== null) stack.push(current.props.children ?? [])
    }
    return null
  }
  return list.children.map(findCodeId)
}

function responseStub({ ok, status, contentType, body }) {
  return {
    ok, status,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => body,
  }
}

/** Ответ RPC морды по контракту конверта (форма ответа — responseStub). */
function rpcStub({ ok = true, value = null, code = null, message = null } = {}) {
  return responseStub({
    ok: true, status: 200, contentType: 'application/json',
    body: ok
      ? { type: 'server-response', rpcId: 'stub', result: { ok: true, value } }
      : { type: 'server-response', rpcId: 'stub', result: { ok: false, error: { code, message: message ?? code } } },
  })
}

/**
 * Роутер fetch: журнал (/api/harness/events?task_id=…) и RPC (/api/<method>).
 * journal(url, init) — только запросы журнала; rpc({method, payload, init}) —
 * только POST-конверты. Неожиданный запрос — throw: тест не должен молча
 * отвечать на то, чего не ожидал.
 */
function routeFetch({ journal, rpc }) {
  return async (url, init) => {
    const address = String(url)
    if (address.startsWith('/api/harness/events')) return journal(url, init)
    if (address.startsWith('/api/') && init?.method === 'POST') {
      const body = JSON.parse(init.body)
      return rpc({ method: body.method, payload: body.payload, envelope: body, init })
    }
    throw new Error('неожиданный запрос: ' + address)
  }
}

const EMPTY_JOURNAL = {
  ok: true, status: 200, contentType: 'application/json',
  body: { events: [], has_more: false, next_after: 0 },
}

const NO_SESSION_HISTORY = rpcStub({ ok: false, code: 'session-not-found' })

// ── Проверки ──────────────────────────────────────────────────────────────────

test('обёртка бандла: id = имя пакета, на экспорте inject/apply', () => {
  const { sandbox, exports } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  assert.equal(sandbox.registered.id, '@edge-harness/dsh-plugin-manager',
    'id регистрации обязан совпадать с именем пакета (boot-граф ищет фабрику по нему)')
  assert.deepEqual(Array.from(exports.inject), ['slots', 'locale'])
  assert.equal(typeof exports.apply, 'function')
})

test('вшитый манифест = байт-в-байт копии манифеста в пакете (id/server/client)', () => {
  const baked = extractBakedManifest()
  assert.deepEqual(baked, manifestCopy.plugins.map(({ id, server, client }) => ({ id, server, client })))
})

test('вшитый каталог = dsh-edge/plugins-catalog.json (срез не устарел)', async () => {
  const catalogFile = JSON.parse(
    await readFile(join(packageDir, '..', '..', 'dsh-edge', 'plugins-catalog.json'), 'utf8'))
  assert.deepEqual(extractBakedCatalog(), catalogFile.plugins)
})

test('маркер заказа выведен из шаблона id и матчится каждым id каталога и манифеста', () => {
  const match = bundle.match(/const ORDER_MARKER_SOURCE = ("(?:[^"\\]|\\.)*");/)
  assert.ok(match, 'в бандле нет константы ORDER_MARKER_SOURCE (маркер обязан выводиться сборкой из шаблона id)')
  const marker = new RegExp(JSON.parse(match[1]))
  for (const id of [...extractBakedManifest(), ...extractBakedCatalog()].map((x) => x.id)) {
    const captured = marker.exec('[plugin-order:' + id + ']')
    assert.ok(captured && captured[1] === id,
      `маркер заказа не узнаёт id "${id}" целиком — дедупликация молча пропустит дубликат`)
  }
})

test('manifest.json пакета = текущему каталогу репозитория (срез не устарел)', async () => {
  // sha256 в каталоге доказывает целостность артефакта, но не свежесть:
  // пакет, собранный из старого среза, закрепил бы в релизе чужой состав.
  const catalog = JSON.parse(
    await readFile(join(packageDir, '..', '..', 'dsh-edge', 'plugins.json'), 'utf8'))
  assert.deepEqual(
    manifestCopy.plugins.map(({ id, server, client }) => ({ id, server, client })),
    catalog.plugins.map(({ id, server, client }) => ({ id, server, client })),
    'пакет собран из устаревшего среза каталога — прогони build.mjs перед npm pack')
})

test('монтаж: декларация settings.section с id plugin-manager; словари в одном наборе ключей', () => {
  const { mount } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  assert.equal(mount.declaration.name, 'settings.section')
  assert.equal(mount.declaration.id, 'plugin-manager')
  assert.equal(mount.declaration.locale, 'settings.plugins')
  assert.equal(mount.declaration.label(), 'settings.plugins:nav')
  const dicts = mount.dictionaries['settings.plugins']
  assert.ok(dicts, 'словари settings.plugins не зарегистрированы')
  const keySet = (dict) => Object.keys(dict).sort().join(',')
  const reference = keySet(dicts.en)
  for (const [locale, dict] of Object.entries(dicts)) {
    assert.equal(keySet(dict), reference, `набор ключей ${locale} расходится с en`)
  }
  assert.ok(dicts.ru.hintText.includes('умный принцип'), 'подсказка #102 потеряла формулировку')
})

test('первый рендер: все строки манифеста и каталога в состоянии загрузки', () => {
  const { sandbox } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  const strings = collectStrings(sandbox.render())
  for (const plugin of extractBakedManifest()) {
    assert.ok(strings.includes(plugin.id), `строки ${plugin.id} нет при первом рендере`)
  }
  for (const entry of computeAvailable()) {
    assert.ok(strings.includes(entry.id), `строки каталога ${entry.id} нет при первом рендере`)
  }
  assert.ok(strings.includes('statusLoading'), 'первый рендер не в состоянии загрузки')
  assert.ok(strings.includes('orderButton'), 'кнопки заказа нет при первом рендере')
  assert.ok(!strings.includes('statusInstalled'), 'до ответа журнала «установлен» — выдумка')
  assert.ok(!strings.includes('undefined'), 'в дереве рендера есть undefined')
})

test('доступные для заказа = каталог минус установленные (#113)', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async () => NO_SESSION_HISTORY,
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const ids = catalogRowIds(sandbox.tree)
  assert.ok(ids, 'списка каталога нет в дереве')
  assert.deepEqual(ids, computeAvailable().map((entry) => entry.id),
    'список каталога не совпал с «каталог минус манифест»')
  for (const plugin of extractBakedManifest()) {
    assert.ok(!ids.includes(plugin.id), `установленный ${plugin.id} попал в каталог заказа`)
  }
})

test('статусы: последнее plugin_status побеждает; без событий — «установлен»; fetch по контракту', async () => {
  const baked = extractBakedManifest()
  const responses = new Map()
  // Первый плагин: два события — берётся ПОСЛЕДНЕЕ (ready с detail), не первое.
  responses.set(baked[0].id, {
    ok: true, status: 200, contentType: 'application/json',
    body: { events: [
      { id: 11, task_id: 'plugin:' + baked[0].id, seq: 1, ts: 1, source: 'deploy', kind: 'plugin_status', data: { plugin: baked[0].id, state: 'deploying' } },
      { id: 12, task_id: 'plugin:' + baked[0].id, seq: 2, ts: 2, source: 'deploy', kind: 'plugin_status', data: { plugin: baked[0].id, state: 'ready', detail: '0.7.1' } },
    ], has_more: false, next_after: 12 },
  })
  if (baked[1]) {
    // Второй: журнал жив, событий ещё нет → честное «установлен».
    responses.set(baked[1].id, {
      ok: true, status: 200, contentType: 'application/json',
      body: { events: [], has_more: false, next_after: 0 },
    })
  }
  if (baked[2]) {
    // Третий: финальный failed с detail (форма DETAIL из deploy-джобы).
    responses.set(baked[2].id, {
      ok: true, status: 200, contentType: 'application/json',
      body: { events: [
        { id: 21, task_id: 'plugin:' + baked[2].id, seq: 1, ts: 3, source: 'deploy', kind: 'plugin_status', data: { plugin: baked[2].id, state: 'failed', detail: 'деплой упал, см. лог job' } },
      ], has_more: false, next_after: 21 },
    })
  }
  const journalCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async (url, init) => {
      journalCalls.push({ url: String(url), init })
      const taskId = String(url).match(/[?&]task_id=([^&]+)/)
      const id = decodeURIComponent(taskId ? taskId[1] : '').replace(/^plugin:/, '')
      // Записи каталога в этом тесте — пустой журнал (их статусы проверяются ниже).
      const response = responses.get(id) ?? EMPTY_JOURNAL
      return responseStub(response)
    },
    rpc: async () => NO_SESSION_HISTORY,
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('statusReady'), 'последний статус (ready) не показан')
  assert.ok(strings.includes('0.7.1'), 'detail события не показан')
  assert.ok(!strings.includes('statusDeploying'), 'взято не последнее событие')
  if (baked[1]) {
    assert.ok(strings.includes('statusInstalled'), 'пустой журнал должен давать «установлен»')
  }
  if (baked[2]) {
    assert.ok(strings.includes('statusFailed'), 'failed-статус не показан')
    assert.ok(strings.includes('деплой упал, см. лог job'), 'detail failed-статуса не показан')
  }
  assert.ok(!strings.includes('journalError'), 'живой журнал показан как ошибка')
  const available = computeAvailable()
  assert.equal(journalCalls.length, baked.length + available.length,
    'запрос статуса не на каждый плагин (манифест + каталог)')
  for (const call of journalCalls) {
    assert.ok(call.url.startsWith('/api/harness/events?task_id=plugin%3A'),
      'URL журнала не по контракту: ' + call.url)
    assert.ok(call.url.includes('&limit=10&after=0'),
      'первая страница журнала обязана быть after=0: ' + call.url)
    assert.equal(call.init.credentials, 'include', 'браузер обязан идти сессионной кукой')
  }
})

test('пагинация: свежайший статус за пределами первой страницы добирается по next_after', async () => {
  const baked = extractBakedManifest()
  const target = baked[0].id
  const taskParam = encodeURIComponent('plugin:' + target)
  const pages = new Map([
    ['after=0', {
      ok: true, status: 200, contentType: 'application/json',
      // Полная первая страница протухших событий — как после нескольких деплоев:
      // свежайшее состояние в неё уже не помещается.
      body: {
        events: Array.from({ length: 10 }, (_, i) => ({
          id: i + 1, task_id: 'plugin:' + target, seq: i + 1, ts: i + 1, source: 'deploy',
          kind: 'plugin_status', data: { plugin: target, state: 'deploying' },
        })),
        has_more: true, next_after: 10,
      },
    }],
    ['after=10', {
      ok: true, status: 200, contentType: 'application/json',
      body: {
        events: [
          { id: 11, task_id: 'plugin:' + target, seq: 11, ts: 11, source: 'deploy',
            kind: 'plugin_status', data: { plugin: target, state: 'ready', detail: 'свежий' } },
        ],
        has_more: false, next_after: 11,
      },
    }],
  ])
  const targetCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async (url) => {
      const address = String(url)
      if (!address.includes('task_id=' + taskParam)) {
        // остальные плагины: пустой журнал, одна страница
        return responseStub(EMPTY_JOURNAL)
      }
      targetCalls.push({ url: address })
      const after = address.split('after=')[1] ?? '0'
      const response = pages.get('after=' + after)
      if (!response) throw new Error('неожиданная страница журнала: ' + address)
      return responseStub(response)
    },
    rpc: async () => NO_SESSION_HISTORY,
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.equal(targetCalls.length, 2, 'свежайшая страница не добрана: ' + JSON.stringify(targetCalls.map((call) => call.url)))
  assert.ok(strings.includes('statusReady'), 'статус за пределами первой страницы не показан')
  assert.ok(strings.includes('свежий'), 'detail события со второй страницы не показан')
  assert.ok(!strings.includes('statusDeploying'), 'показан протухший статус с первой страницы')
  assert.ok(!strings.includes('journalError'), 'живой журнал показан как ошибка')
})

test('событие без state: warning с сырыми данными, а не притворный «установлен»', async () => {
  const baked = extractBakedManifest()
  const target = baked[0].id
  const taskParam = encodeURIComponent('plugin:' + target)
  const { sandbox } = loadBundle(routeFetch({
    journal: async (url) => {
      if (!String(url).includes('task_id=' + taskParam)) {
        return responseStub(EMPTY_JOURNAL)
      }
      return responseStub({
        ok: true, status: 200, contentType: 'application/json',
        body: {
          events: [
            { id: 5, task_id: 'plugin:' + target, seq: 1, ts: 1, source: 'deploy',
              kind: 'plugin_status', data: { plugin: target, detail: 'кривое событие' } },
          ],
          has_more: false, next_after: 5,
        },
      })
    },
    rpc: async () => NO_SESSION_HISTORY,
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('statusUnknown'), 'кривое событие не показано как неизвестное состояние')
  assert.ok(strings.some((s) => s.includes('кривое событие')),
    'сырые данные кривого события потеряны (показываются как JSON в note)')
  // statusInstalled при этом вправе показать другой плагин с пустым журналом,
  // поэтому целевой признак — именно связка statusUnknown + сырые данные выше.
})

test('отказ журнала (401): громкая ошибка, а не притворный «установлен»', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub({
      ok: false, status: 401, contentType: 'application/json',
      body: { error: { code: 'unauthorized', message: 'x' } },
    }),
    rpc: async () => rpcStub({ ok: false, code: 'unauthorized' }),
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('journalError'), 'ошибка журнала не показана')
  assert.ok(strings.includes('HTTP 401'), 'код ответа не виден владельцу')
  assert.ok(strings.includes('retry'), 'нет кнопки повторить')
  assert.ok(!strings.includes('statusInstalled'), 'отказ журнала притворился «установлен» — silent-wrong')
})

test('чужой API на пути журнала (не JSON): форма ответа проверяется — громкая ошибка', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub({
      ok: true, status: 200, contentType: 'text/html',
      body: '<html>не журнал</html>',
    }),
    rpc: async () => responseStub({
      ok: true, status: 200, contentType: 'text/html',
      body: '<html>не RPC</html>',
    }),
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('journalError'))
  assert.ok(!strings.includes('statusInstalled'), 'чужой ответ притворился «установлен» — silent-wrong')
  assert.ok(strings.some((text) => text.includes('JSON')), 'причина (не JSON) не видна владельцу')
})

// ── Заказ установки (#113) ────────────────────────────────────────────────────

test('статус каталога: пустой журнал — «конвейер не запускался», не «установлен»', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async () => NO_SESSION_HISTORY,
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  // Адресно по поддереву каталога: установленные строки вправе показывать
  // «установлен», строка каталога с пустым журналом — нет.
  const catalogStrings = collectStrings(catalogListNode(sandbox.tree))
  assert.ok(catalogStrings.includes('catalogIdle'),
    'строка каталога с пустым журналом должна показывать «конвейер не запускался»')
  assert.ok(!catalogStrings.includes('statusInstalled'),
    'строка каталога притворилась «установлен» — silent-wrong: плагина ещё нет')
})

test('заказ: клик готовой формулировкой через штатный RPC (create-or-reuse + prompt с маркером)', async () => {
  const available = computeAvailable()
  assert.ok(available.length > 0, 'в каталоге нет доступных записей — тест не на чем провести')
  const target = available[0]
  const rpcCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method, payload, envelope, init }) => {
      rpcCalls.push({ method, payload, envelope, init })
      if (method === 'session.history') return NO_SESSION_HISTORY
      if (['workspace.create', 'session.create', 'session.rename', 'session.prompt'].includes(method)) {
        return rpcStub({ value: method === 'workspace.create' ? { workspace: { workspaceId: 'ws-1' } } : { accepted: true } })
      }
      throw new Error('неожиданный RPC: ' + method)
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  // До заказа: сессии нет (session-not-found) → заказов не было → кнопка активна.
  assert.ok(!collectStrings(sandbox.tree).includes('dedupError'),
    'session-not-found показан как ошибка — это штатное «заказов ещё нет»')
  const buttonBefore = collectButtons(catalogListNode(sandbox.tree))[0]
  assert.equal(buttonBefore.disabled, false, 'кнопка заказа не активна без заказов')

  collectButtons(catalogListNode(sandbox.tree))[0].onClick()
  await flush()
  sandbox.render()

  const methods = rpcCalls.filter((call) => call.method !== 'session.history').map((call) => call.method)
  assert.deepEqual(methods, ['workspace.create', 'session.create', 'session.rename', 'session.prompt'],
    'последовательность RPC заказа не по контракту: ' + JSON.stringify(methods))
  for (const call of rpcCalls) {
    assert.equal(call.envelope.type, 'client-request', 'конверт RPC не client-request')
    assert.ok(call.envelope.rpcId, 'у конверта нет rpcId')
    assert.equal(call.envelope.method, call.method, 'method конверта не совпал с путём')
    assert.equal(call.init.credentials, 'include', 'RPC обязан идти кукой владельца')
    assert.equal(call.init.headers['content-type'], 'application/json', 'content-type не JSON')
  }
  const byMethod = Object.fromEntries(rpcCalls.map((call) => [call.method, call]))
  assert.deepEqual(byMethod['workspace.create'].payload, { path: '/workspace/edge-harness' },
    'воркспейс заказа не edge-harness')
  assert.equal(byMethod['session.create'].payload.sessionId, 'plugin-orders',
    'сессия заказов не plugin-orders (идемпотентный create-or-reuse сломан)')
  assert.equal(byMethod['session.rename'].payload.title, 'Заказ плагинов')
  const prompt = byMethod['session.prompt'].payload
  assert.equal(prompt.sessionId, 'plugin-orders')
  assert.equal(prompt.mode, 'queue', 'режим prompt не queue (steer вмешался бы в чужой ход)')
  assert.equal(prompt.content.length, 1)
  assert.equal(prompt.content[0].type, 'text')
  const text = prompt.content[0].text
  assert.ok(text.includes('[plugin-order:' + target.id + ']'), 'маркера заказа нет в тексте')
  assert.ok(text.includes(target.brief), 'готовой формулировки из каталога нет в заказе')
  assert.ok(text.includes('dsh-edge/plugins.json'), 'в заказе нет плана конвейера (PR в каталог)')

  const buttonAfter = collectButtons(catalogListNode(sandbox.tree))[0]
  assert.equal(buttonAfter.disabled, true, 'кнопка не заперта после заказа — дубликат возможен')
  assert.ok(collectStrings(catalogListNode(sandbox.tree)).includes('orderOrdered'),
    'после заказа кнопка не показывает «заказано»')
})

test('дедупликация: маркер в истории сессии заказов — кнопка «заказано», клик отсечён', async () => {
  const available = computeAvailable()
  const target = available[0]
  const rpcCalls = []
  const history = rpcStub({
    value: {
      events: [
        { event: { type: 'turn/start', seq: 1, time: 1, data: { turn: 1 } } },
        { event: { type: 'user/message', seq: 2, time: 2, data: { id: 'm1', role: 'user',
          content: [{ type: 'text', text: '[plugin-order:' + target.id + '] Закажи установку плагина.' }] } } },
        { event: { type: 'assistant/message', seq: 3, time: 3, data: { turn: 1, step: 1,
          message: { id: 'm2', role: 'assistant', content: [{ type: 'text', text: 'Принято.' }], source: { kind: 'model' } } } } },
      ],
      hasMore: true,
    },
  })
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      rpcCalls.push(method)
      return method === 'session.history' ? history : rpcStub()
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const button = collectButtons(catalogListNode(sandbox.tree))[0]
  assert.equal(button.disabled, true, 'заказ из окна истории не отсёк повторный заказ')
  assert.ok(collectStrings(catalogListNode(sandbox.tree)).includes('orderOrdered'),
    'заказанное состояние не показано')
  const historyCalls = rpcCalls.filter((method) => method === 'session.history').length
  assert.equal(historyCalls, 1, 'история читается один раз за загрузку (окно, не весь лог)')
  button.onClick()
  await flush()
  assert.equal(rpcCalls.filter((method) => method !== 'session.history').length, 0,
    'клик по «заказано» пробил гвард и отправил дубликат заказа')
})

test('отказ проверки дедупликации: кнопки заперты + громкая ошибка, не тихое разрешение', async () => {
  const rpcCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      rpcCalls.push(method)
      return method === 'session.history'
        ? rpcStub({ ok: false, code: 'forbidden' })
        : rpcStub()
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('dedupError:'), 'отказ проверки заказов не показан')
  assert.ok(strings.some((text) => text.includes('forbidden')), 'код отказа не виден владельцу')
  assert.ok(strings.includes('retry'), 'нет кнопки повтора проверки')
  const button = collectButtons(catalogListNode(sandbox.tree))[0]
  assert.equal(button.disabled, true,
    'при неизвестном состоянии дедупликации кнопка молча разрешает заказ — возможен дубликат')
  button.onClick()
  await flush()
  assert.equal(rpcCalls.filter((method) => method !== 'session.history').length, 0,
    'запертая кнопка отправила заказ')
})

test('history не по контракту (без массива events): громкая ошибка, не «заказов нет»', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => (method === 'session.history'
      ? rpcStub({ value: { hasMore: false } })
      : rpcStub()),
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('dedupError:'), 'битый ответ history не показан как ошибка')
  assert.ok(strings.some((text) => text.includes('не по контракту морды')),
    'причина (не по контракту) не видна владельцу')
  assert.equal(collectButtons(catalogListNode(sandbox.tree))[0].disabled, true,
    'битый history открыл кнопки — дедупликация не доказана')
})

test('заказ не отправлен (prompt ok:false): громкая ошибка с кодом, кнопка возвращается', async () => {
  const available = computeAvailable()
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      if (method === 'session.history') return NO_SESSION_HISTORY
      if (method === 'workspace.create') return rpcStub({ value: { workspace: { workspaceId: 'ws-1' } } })
      if (method === 'session.prompt') return rpcStub({ ok: false, code: 'agent-busy', message: 'ход выполняется' })
      return rpcStub()
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  collectButtons(catalogListNode(sandbox.tree))[0].onClick()
  await flush()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('orderError:'), 'отказ заказа не показан')
  assert.ok(strings.some((text) => text.includes('agent-busy')), 'код отказа заказа не виден владельцу')
  const button = collectButtons(catalogListNode(sandbox.tree))[0]
  assert.equal(button.disabled, false, 'после отказа кнопка не вернулась в «можно заказывать»')
  assert.ok(!strings.includes('orderOrdered'), 'несостоявшийся заказ показан как «заказано»')
})

test('перепроверка дедупликации перед отправкой: заказ из второй вкладки отсечён', async () => {
  const available = computeAvailable()
  const target = available[0]
  const rpcCalls = []
  // При загрузке — заказов нет (кнопка активна); к моменту клика заказ уже
  // в истории (его сделал другая вкладка/устройство).
  let historyResponse = NO_SESSION_HISTORY
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      rpcCalls.push(method)
      if (method === 'session.history') return historyResponse
      if (method === 'workspace.create') return rpcStub({ value: { workspace: { workspaceId: 'ws-1' } } })
      return rpcStub({ value: { accepted: true } })
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  // Заказ «успел» появиться в истории, пока секция была открыта.
  historyResponse = rpcStub({
    value: {
      events: [
        { event: { type: 'user/message', seq: 2, time: 2, data: { id: 'm1', role: 'user',
          content: [{ type: 'text', text: '[plugin-order:' + target.id + '] Заказ.' }] } } },
      ],
      hasMore: false,
    },
  })
  collectButtons(catalogListNode(sandbox.tree))[0].onClick()
  await flush()
  sandbox.render()
  const methods = rpcCalls.filter((m) => m !== 'session.history')
  assert.deepEqual(methods, [],
    'перепроверка пропустила заказ, уже существующий в истории — дубликат')
  const strings = collectStrings(catalogListNode(sandbox.tree))
  assert.ok(strings.includes('orderOrdered'), 'отсечённый заказ не показан как «заказано»')
  assert.ok(!collectStrings(sandbox.tree).includes('orderError:'), 'отсечением назвали ошибкой')
})

test('перепроверка не удалась: заказ вслепую не отправляется (fail-closed), отказ громкий', async () => {
  const rpcCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      rpcCalls.push(method)
      if (method === 'session.history') {
        // Первый вызов (при монтировании) успешен и пуст, второй (перепроверка) — отказ.
        return rpcCalls.filter((m) => m === 'session.history').length === 1
          ? NO_SESSION_HISTORY
          : rpcStub({ ok: false, code: 'forbidden' })
      }
      return rpcStub({ value: { accepted: true } })
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  collectButtons(catalogListNode(sandbox.tree))[0].onClick()
  await flush()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(!rpcCalls.includes('workspace.create'),
    'при упавшей перепроверке заказ ушёл вслепую — silent-wrong')
  assert.ok(strings.includes('orderError:'), 'отказ перепроверки не показан')
  assert.ok(strings.some((text) => text.includes('forbidden')), 'код отказа не виден владельцу')
})

test('RPC не-2xx (HTTP-слой): громкая ошибка с кодом, а не тихая попытка продолжить', async () => {
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => (method === 'session.history'
      ? responseStub({ ok: false, status: 503, contentType: 'application/json', body: {} })
      : rpcStub()),
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('dedupError:'), 'не-2xx RPC не показан как ошибка дедупликации')
  assert.ok(strings.some((text) => text.includes('HTTP 503')), 'код HTTP не виден владельцу')
  assert.equal(collectButtons(catalogListNode(sandbox.tree))[0].disabled, true,
    'не-2xx RPC открыл кнопки — дедупликация не доказана')
})

test('workspace.create без workspaceId: заказ падает громко, prompt не вызывается', async () => {
  const rpcCalls = []
  const { sandbox } = loadBundle(routeFetch({
    journal: async () => responseStub(EMPTY_JOURNAL),
    rpc: async ({ method }) => {
      rpcCalls.push(method)
      if (method === 'session.history') return NO_SESSION_HISTORY
      return rpcStub({ value: {} })
    },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  collectButtons(catalogListNode(sandbox.tree))[0].onClick()
  await flush()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(!rpcCalls.includes('session.prompt'),
    'prompt вызван без сессии — заказ ушёл в неизвестность')
  assert.ok(strings.includes('orderError:'), 'кривой ответ workspace.create не показан')
  assert.ok(strings.some((text) => text.includes('без workspaceId')),
    'причина (без workspaceId) не видна владельцу')
})
