/**
 * Поведенческая гвардия клиентского плагина plugin-manager (#102).
 *
 * Проверяется форма обёртки бандла (что собрал build.mjs), монтаж в слот
 * settings.section, паритет ключей словарей и поведение ячеек статусов на
 * ПРОДА-форме данных журнала: { events: [{id, kind, data}], has_more,
 * next_after } (контракт cf-worker/src/harness.ts), а не пересказ.
 *
 * Запуск: node --test plugins-src/plugin-manager/test/client.test.mjs
 * (тест сам прогоняет build.mjs — продукт генерируется перед проверкой).
 *
 * react и ui-primitives здесь — стабы: настоящий react приезжает в браузере
 * из seed-карты шелла; стаб покрывает только контракт, который бандл
 * использует (createElement/useState/useCallback/useEffect + StateDot).
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
  // fetch-стабы резолвятся сразу; цепочка load() (allSettled → setState)
  // укладывается в несколько раундов макротасок.
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
      return [sandbox.cells[index], (value) => { sandbox.cells[index] = value }]
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

function responseStub({ ok, status, contentType, body }) {
  return {
    ok, status,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => body,
  }
}

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
  assert.ok(baked.length >= 2, 'манифест должен нести hello и runner-bridge (критерий #102)')
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

test('первый рендер: все строки манифеста в состоянии загрузки', () => {
  const { sandbox } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  const strings = collectStrings(sandbox.render())
  for (const plugin of extractBakedManifest()) {
    assert.ok(strings.includes(plugin.id), `строки ${plugin.id} нет при первом рендере`)
  }
  assert.ok(strings.includes('statusLoading'), 'первый рендер не в состоянии загрузки')
  assert.ok(!strings.includes('statusInstalled'), 'до ответа журнала «установлен» — выдумка')
  assert.ok(!strings.includes('undefined'), 'в дереве рендера есть undefined')
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
  const calls = []
  const { sandbox } = loadBundle(async (url, init) => {
    calls.push({ url, init })
    const id = decodeURIComponent(String(url).split('task_id=')[1]).replace(/^plugin:/, '')
    const response = responses.get(id)
    if (!response) throw new Error('неожиданный запрос журнала: ' + url)
    return responseStub(response)
  })
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
  assert.equal(calls.length, baked.length, 'запрос статуса не на каждый плагин')
  for (const call of calls) {
    assert.ok(call.url.startsWith('/api/events?limit=10&task_id=plugin%3A'),
      'URL журнала не по контракту: ' + call.url)
    assert.equal(call.init.credentials, 'include', 'браузер обязан идти сессионной кукой')
  }
})

test('отказ журнала (401): громкая ошибка, а не притворный «установлен»', async () => {
  const { sandbox } = loadBundle(async () => responseStub({
    ok: false, status: 401, contentType: 'application/json',
    body: { error: { code: 'unauthorized', message: 'x' } },
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
  const { sandbox } = loadBundle(async () => responseStub({
    ok: true, status: 200, contentType: 'text/html',
    body: '<html>не журнал</html>',
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('journalError'))
  assert.ok(!strings.includes('statusInstalled'), 'чужой ответ притворился «установлен» — silent-wrong')
  assert.ok(strings.some((text) => text.includes('JSON')), 'причина (не JSON) не видна владельцу')
})
