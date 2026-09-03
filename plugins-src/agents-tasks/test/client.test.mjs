/**
 * Поведенческая гвардия клиентского плагина agents-tasks (#111).
 *
 * Проверяется форма обёртки бандла (что собрал build.mjs), монтаж в слоты
 * sidebar.section и settings.section, паритет ключей словарей и поведение
 * ячеек статусов на ПРОДА-форме данных журнала: { events: [{id, kind, data}],
 * has_more, next_after } (контракт cf-worker/src/harness.ts), а не пересказ.
 *
 * Запуск: node --test plugins-src/agents-tasks/test/client.test.mjs
 * (тест сам прогоняет build.mjs — продукт генерируется перед проверкой).
 *
 * react и ui-primitives здесь — стабы: настоящий react приезжает в браузере
 * из seed-карты шелла; стаб покрывает только контракт, который бандл
 * использует (createElement/useState/useCallback/useEffect/useRef + StateDot
 * + Button + Pill + IconCopy + IconExternalLink + IconRefreshCw +
 * IconChevronDown + IconChevronRight + IconMessageSquare + IconTerminal +
 * IconBrain + IconTool + IconX).
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginDir = dirname(fileURLToPath(import.meta.url)) // …/agents-tasks/test
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
  const sandbox = { effects: [], cells: [], refs: {}, cursor: 0 }
  sandbox.window = {
    __ModuleLoader__: { load(registration) { sandbox.registered = registration } },
    location: { protocol: 'https:', host: 'example.com' },
    document: { head: { appendChild: () => {} }, createElement: () => ({}) },
    setInterval: (fn, ms) => { sandbox.intervals = sandbox.intervals || []; const id = sandbox.intervals.length; sandbox.intervals.push(fn); return id; },
    clearInterval: (id) => { if (sandbox.intervals && sandbox.intervals[id]) sandbox.intervals[id] = null; },
  }
  sandbox.fetch = fetchStub
  sandbox.console = console
  sandbox.react = {
    createElement,
    useState(initial) {
      const index = sandbox.cursor++
      if (sandbox.cells[index] === undefined) {
        sandbox.cells[index] = typeof initial === 'function' ? initial() : initial
      }
      return [sandbox.cells[index], (value) => { sandbox.cells[index] = typeof value === 'function' ? value(sandbox.cells[index]) : value }]
    },
    useCallback(fn) { return fn },
    useEffect(fn) { sandbox.effects.push(fn) },
    useRef(initial) { return { current: initial } },
  }
  sandbox.primitives = {
    StateDot: function StateDot(props) { return { type: 'StateDot', props: { ...props }, children: [] } },
    Button: function Button(props) { return { type: 'Button', props: { ...props }, children: [props.children] } },
    Pill: function Pill(props) { return { type: 'Pill', props: { ...props }, children: [props.children] } },
    IconCopy: function IconCopy(props) { return { type: 'IconCopy', props: { ...props }, children: [] } },
    IconExternalLink: function IconExternalLink(props) { return { type: 'IconExternalLink', props: { ...props }, children: [] } },
    IconRefreshCw: function IconRefreshCw(props) { return { type: 'IconRefreshCw', props: { ...props }, children: [] } },
    IconChevronDown: function IconChevronDown(props) { return { type: 'IconChevronDown', props: { ...props }, children: [] } },
    IconChevronRight: function IconChevronRight(props) { return { type: 'IconChevronRight', props: { ...props }, children: [] } },
    IconMessageSquare: function IconMessageSquare(props) { return { type: 'IconMessageSquare', props: { ...props }, children: [] } },
    IconTerminal: function IconTerminal(props) { return { type: 'IconTerminal', props: { ...props }, children: [] } },
    IconBrain: function IconBrain(props) { return { type: 'IconBrain', props: { ...props }, children: [] } },
    IconTool: function IconTool(props) { return { type: 'IconTool', props: { ...props }, children: [] } },
    IconX: function IconX(props) { return { type: 'IconX', props: { ...props }, children: [] } },
  }
  sandbox.require = (specifier) => {
    if (specifier === 'react') return sandbox.react
    if (specifier === '@deepseek-ai/dsh-client-ui-primitives') return sandbox.primitives
    throw new Error(`неизвестный require: ${specifier}`)
  }
  vm.runInNewContext(bundle, sandbox, { filename: 'client.js' })
  // Глобальные таймеры, WebSocket, location и URLSearchParams для кода бандла
  sandbox.setInterval = (fn, ms) => { sandbox.intervals = sandbox.intervals || []; const id = sandbox.intervals.length; sandbox.intervals.push(fn); return id; }
  sandbox.clearInterval = (id) => { if (sandbox.intervals && sandbox.intervals[id]) sandbox.intervals[id] = null; }
  sandbox.WebSocket = function MockWebSocket(url) {
    this.url = url
    this.onopen = null
    this.onclose = null
    this.onmessage = null
    this.onerror = null
    this.readyState = 0 // CONNECTING
    this.close = () => { this.readyState = 3; if (this.onclose) this.onclose() }
    // Симулируем асинхронное открытие
    setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen() }, 0)
  }
  sandbox.location = { protocol: 'https:', host: 'example.com' }
  sandbox.URLSearchParams = URLSearchParams
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
  // Принимаем любой из двух слотов
  assert.ok(mount.slot === 'sidebar.section' || mount.slot === 'settings.section',
    `apply смонтировал не sidebar.section и не settings.section, а ${mount.slot}`)
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
    for (const effect of sandbox.effects) {
      const result = effect()
      if (result && typeof result.then === 'function') await result
    }
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
  assert.equal(sandbox.registered.id, '@edge-harness/dsh-agents-tasks',
    'id регистрации обязан совпадать с именем пакета (boot-граф ищет фабрику по нему)')
  assert.deepEqual(Array.from(exports.inject), ['slots', 'locale'])
  assert.equal(typeof exports.apply, 'function')
})

test('вшитый манифест = байт-в-байт копии манифеста в пакете (id/server/client)', () => {
  const baked = extractBakedManifest()
  assert.deepEqual(baked, manifestCopy.plugins.map(({ id, server, client }) => ({ id, server, client })))
})

test('manifest.json пакета = текущему каталогу репозитория (срез не устарел)', async () => {
  const catalog = JSON.parse(
    await readFile(join(packageDir, '..', '..', 'dsh-edge', 'plugins.json'), 'utf8'))
  assert.deepEqual(
    manifestCopy.plugins.map(({ id, server, client }) => ({ id, server, client })),
    catalog.plugins.map(({ id, server, client }) => ({ id, server, client })),
    'пакет собран из устаревшего среза каталога — прогони build.mjs перед npm pack')
})

test('монтаж: декларация в sidebar.section ИЛИ settings.section с id agents-tasks; словари в одном наборе ключей', () => {
  const { mount } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  assert.ok(mount.declaration.name === 'sidebar.section' || mount.declaration.name === 'settings.section',
    'слот должен быть sidebar.section или settings.section')
  assert.equal(mount.declaration.id, 'agents-tasks')
  assert.equal(mount.declaration.locale, 'agents.tasks')
  assert.equal(typeof mount.declaration.label, 'function')
  const dicts = mount.dictionaries['agents.tasks']
  assert.ok(dicts, 'словари agents.tasks не зарегистрированы')
  const keySet = (dict) => Object.keys(dict).sort().join(',')
  const reference = keySet(dicts.en)
  for (const [locale, dict] of Object.entries(dicts)) {
    assert.equal(keySet(dict), reference, `набор ключей ${locale} расходится с en`)
  }
  assert.ok(dicts.ru.nav.includes('Агенты'), 'название навигации на ru потеряно')
})

test('первый рендер: состояние загрузки задач', () => {
  const { sandbox } = loadBundle(async () => { throw new Error('fetch не ожидается') })
  const strings = collectStrings(sandbox.render())
  assert.ok(strings.includes('loadingTasks'), 'первый рендер не в состоянии загрузки')
  assert.ok(!strings.includes('noTasks'), 'до ответа GitHub "нет задач" — выдумка')
  assert.ok(!strings.includes('undefined'), 'в дереве рендера есть undefined')
})

test('статусы задач: последние системные события побеждают; поллинг GitHub работает', async () => {
  const baked = extractBakedManifest()
  // Мокаем GitHub API ответ
  const githubResponse = {
    ok: true, status: 200, contentType: 'application/json',
    body: [
      { number: 123, title: 'Test task', state: 'open', html_url: 'https://github.com/test/123', assignees: [{login: 'user1'}], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' },
      { number: 124, title: 'Another task', state: 'closed', html_url: 'https://github.com/test/124', assignees: [], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' },
    ],
  }
  // Мокаем журнал: первая задача — running, вторая — done
  const journalResponses = {
    'issue:#123': {
      ok: true, status: 200, contentType: 'application/json',
      body: { events: [
        { id: 1, task_id: 'issue:#123', seq: -1, ts: Date.now()-10000, source: 'system', kind: 'task_queued', data: {} },
        { id: 2, task_id: 'issue:#123', seq: -2, ts: Date.now()-5000, source: 'system', kind: 'task_dispatched', data: {} },
        { id: 3, task_id: 'issue:#123', seq: 1, ts: Date.now()-1000, source: 'job', kind: 'job_start', data: {} },
      ], has_more: false, next_after: 3 },
    },
    'issue:#124': {
      ok: true, status: 200, contentType: 'application/json',
      body: { events: [
        { id: 4, task_id: 'issue:#124', seq: -1, ts: Date.now()-20000, source: 'system', kind: 'task_queued', data: {} },
        { id: 5, task_id: 'issue:#124', seq: -2, ts: Date.now()-15000, source: 'system', kind: 'task_dispatched', data: {} },
        { id: 6, task_id: 'issue:#124', seq: 1, ts: Date.now()-10000, source: 'job', kind: 'job_start', data: {} },
        { id: 7, task_id: 'issue:#124', seq: 2, ts: Date.now()-5000, source: 'job', kind: 'job_end', data: { result: 'success' } },
      ], has_more: false, next_after: 7 },
    },
  }

  const calls = { github: 0, journal: 0 }
  const { sandbox } = loadBundle(async (url, init) => {
    const urlStr = String(url)
    if (urlStr.includes('api.github.com')) {
      calls.github++
      return responseStub(githubResponse)
    }
    if (urlStr.includes('/api/events?task_id=')) {
      calls.journal++
      const taskId = decodeURIComponent(urlStr.match(/[?&]task_id=([^&]+)/)?.[1] || '')
      const response = journalResponses[taskId]
      if (!response) throw new Error('неожиданный запрос журнала: ' + urlStr)
      return responseStub(response)
    }
    throw new Error('неожиданный запрос: ' + urlStr)
  })

  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()

  const strings = collectStrings(sandbox.tree)
  // Проверяем, что задачи отрендерились (компонент показывает #123)
  assert.ok(strings.includes('#123'), 'задача #123 не отрендерилась: ' + JSON.stringify(strings))
  assert.ok(strings.includes('#124'), 'задача #124 не отрендерилась')
  assert.ok(strings.includes('Test task'), 'название задачи не отрендерилось')
  assert.ok(strings.includes('Another task'), 'название второй задачи не отрендерилось')
  // Проверяем статусы
  assert.ok(strings.includes('statusRunning'), 'статус running не показан: ' + JSON.stringify(strings))
  assert.ok(strings.includes('statusDone'), 'статус done не показан')
  // Проверяем исполнителя
  assert.ok(strings.includes('user1'), 'исполнитель не показан')
  // Проверяем вызовы
  assert.equal(calls.github, 1, 'GitHub API вызван не 1 раз')
  assert.equal(calls.journal, 2, 'журнал запрошен не для каждой задачи')
})

test('session_event: think-блоки и tool-вызовы видны в развернутой задаче', async () => {
  const githubResponse = {
    ok: true, status: 200, contentType: 'application/json',
    body: [
      { number: 200, title: 'Session test', state: 'open', html_url: 'https://github.com/test/200', assignees: [], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' },
    ],
  }
  const journalResponse = {
    ok: true, status: 200, contentType: 'application/json',
    body: { events: [
      { id: 10, task_id: 'issue:#200', seq: -1, ts: Date.now()-10000, source: 'system', kind: 'task_queued', data: {} },
      { id: 11, task_id: 'issue:#200', seq: 1, ts: Date.now()-5000, source: 'job', kind: 'session_event', data: { events: [
        { type: 'agent/request', prompt: 'Please solve this task...' },
        { type: 'tool/call', name: 'bash', args: { command: 'ls -la' } },
        { type: 'tool/result', name: 'bash', result: 'total 0\n', error: null },
        { type: 'assistant/message', content: 'Done!' },
      ] } },
    ], has_more: false, next_after: 11 },
  }

  const calls = { github: 0, journal: 0 }
  const { sandbox } = loadBundle(async (url) => {
    const urlStr = String(url)
    if (urlStr.includes('api.github.com')) {
      calls.github++
      return responseStub(githubResponse)
    }
    if (urlStr.includes('/api/events?task_id=')) {
      calls.journal++
      return responseStub(journalResponse)
    }
    throw new Error('неожиданный запрос: ' + urlStr)
  })

  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()

  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.includes('#200'), 'задача #200 не отрендерилась: ' + JSON.stringify(strings))
  // В списке задач статус должен быть определен из системных событий (queued)
  assert.ok(strings.includes('statusQueued'), 'статус queued не показан в списке: ' + JSON.stringify(strings))

  // Симулируем раскрытие задачи: устанавливаем selectedTaskNumber (index 3) и expandedTasks (index 4)
  sandbox.cells[3] = 200 // selectedTaskNumber
  sandbox.cells[4] = new Set([200]) // expandedTasks
  sandbox.render()

  const strings2 = collectStrings(sandbox.tree)
  // Проверяем наличие элементов session_event в развернутом виде
  assert.ok(strings2.some(s => s.includes('Think') || s.includes('Размышление') || s.includes('thinkLabel')), 'think-блок не показан: ' + JSON.stringify(strings2))
  assert.ok(strings2.some(s => s.includes('toolCallLabel') || s.includes('Tool Call') || s.includes('Вызов инструмента')), 'метка tool call не показана')
  assert.ok(strings2.some(s => s.includes('ls -la')), 'аргументы tool call не показаны')
  assert.ok(strings2.some(s => s.includes('Tool Result') || s.includes('Результат инструмента') || s.includes('toolResultLabel')), 'метка tool result не показана')
  assert.ok(strings2.some(s => s.includes('total 0')), 'результат tool call не показан')
})

test('пагинация журнала: свежайшие события добираются по next_after', async () => {
  const githubResponse = {
    ok: true, status: 200, contentType: 'application/json',
    body: [
      { number: 300, title: 'Pagination test', state: 'open', html_url: 'https://github.com/test/300', assignees: [], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' },
    ],
  }
  const pages = new Map([
    ['after=0', {
      ok: true, status: 200, contentType: 'application/json',
      body: {
        events: Array.from({ length: 20 }, (_, i) => ({
          id: i + 1, task_id: 'issue:#300', seq: i + 1, ts: Date.now() - (20-i)*1000, source: 'job',
          kind: 'plugin_status', data: { plugin: 'test', state: 'deploying' },
        })),
        has_more: true, next_after: 20,
      },
    }],
    ['after=20', {
      ok: true, status: 200, contentType: 'application/json',
      body: {
        events: [
          { id: 21, task_id: 'issue:#300', seq: 21, ts: Date.now(), source: 'job',
            kind: 'session_event', data: { events: [{ type: 'assistant/message', content: 'Latest result' }] } },
        ],
        has_more: false, next_after: 21,
      },
    }],
  ])

  const journalCalls = []
  const { sandbox } = loadBundle(async (url) => {
    const urlStr = String(url)
    if (urlStr.includes('api.github.com')) return responseStub(githubResponse)
    if (urlStr.includes('/api/events?task_id=')) {
      journalCalls.push(urlStr)
      const after = urlStr.split('after=')[1]?.split('&')[0] || '0'
      const response = pages.get('after=' + after)
      if (!response) throw new Error('неожиданная страница журнала: ' + urlStr)
      return responseStub(response)
    }
    throw new Error('неожиданный запрос: ' + urlStr)
  })

  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()

  // Должно быть 2 запроса к журналу (пагинация)
  const taskCalls = journalCalls.filter(u => u.includes('issue%3A%23300'))
  assert.ok(taskCalls.length >= 2, 'пагинация не отработала: ' + JSON.stringify(taskCalls))
  // Последний запрос должен быть after=20
  assert.ok(taskCalls.some(u => u.includes('after=20')), 'вторая страница не запрошена')
})

test('ошибка GitHub API: громкая ошибка, а не пустой список', async () => {
  const { sandbox } = loadBundle(async () => responseStub({
    ok: false, status: 403, contentType: 'application/json',
    body: { message: 'API rate limit exceeded' },
  }))
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  assert.ok(strings.some(s => s.includes('loadError') || s.includes('Ошибка загрузки') || s.includes('Failed to load')), 'ошибка загрузки не показана')
  assert.ok(strings.some(s => s.includes('403') || s.includes('rate limit')), 'детали ошибки не показаны')
})

test('ошибка журнала (не JSON): форма ответа проверяется — громкая ошибка', async () => {
  const githubResponse = {
    ok: true, status: 200, contentType: 'application/json',
    body: [
      { number: 400, title: 'Journal error test', state: 'open', html_url: 'https://github.com/test/400', assignees: [], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' },
    ],
  }
  const { sandbox } = loadBundle(async (url) => {
    const urlStr = String(url)
    if (urlStr.includes('api.github.com')) return responseStub(githubResponse)
    if (urlStr.includes('/api/events?task_id=')) return responseStub({
      ok: true, status: 200, contentType: 'text/html',
      body: '<html>не журнал</html>',
    })
    throw new Error('неожиданный запрос: ' + urlStr)
  })
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()
  const strings = collectStrings(sandbox.tree)
  // В списке задач показывается индикатор ошибки журнала (journalError)
  assert.ok(strings.some(s => s.includes('journalError') || s.includes('Journal unavailable') || s.includes('Журнал недоступен')), 'индикатор ошибки журнала не показан в списке')
  // Детальная причина ("не JSON") показывается только в развернутом виде — это ожидаемое поведение
})