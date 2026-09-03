import test from 'node:test'
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginDir = dirname(fileURLToPath(import.meta.url))
const packageDir = dirname(pluginDir)

const buildResult = spawnSync(process.execPath, [join(packageDir, 'build.mjs')], { encoding: 'utf8' })
const bundle = await readFile(join(packageDir, 'client', 'client.js'), 'utf8')

function createElement(type, props, ...children) {
  const element = { type, props: props ?? {}, children: children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false) }
  if (typeof type === 'function') return type({ ...element.props, children: element.children })
  return element
}

async function flush() {
  for (let round = 0; round < 5; round += 1) await new Promise(r => setImmediate(r))
}

function loadBundle(fetchStub) {
  const sandbox = { effects: [], cells: [], refs: {}, cursor: 0 }
  sandbox.window = { __ModuleLoader__: { load(r) { sandbox.registered = r } }, location: { protocol: 'https:', host: 'example.com' }, document: { head: { appendChild: () => {} }, createElement: () => ({}) }, setInterval: (fn, ms) => { sandbox.intervals = sandbox.intervals || []; const id = sandbox.intervals.length; sandbox.intervals.push(fn); return id; }, clearInterval: (id) => { if (sandbox.intervals && sandbox.intervals[id]) sandbox.intervals[id] = null; } }
  sandbox.fetch = fetchStub
  sandbox.console = console
  sandbox.react = { createElement, useState(initial) { const i = sandbox.cursor++; if (sandbox.cells[i] === undefined) sandbox.cells[i] = typeof initial === 'function' ? initial() : initial; return [sandbox.cells[i], v => { sandbox.cells[i] = v }] }, useCallback(fn) { return fn }, useEffect(fn) { sandbox.effects.push(fn) }, useRef(init) { return { current: init } } }
  sandbox.primitives = { StateDot: (p) => ({ type: 'StateDot', props: p }), Button: (p) => ({ type: 'Button', props: p, children: [p.children] }), Pill: (p) => ({ type: 'Pill', props: p, children: [p.children] }), IconCopy: () => ({}), IconExternalLink: () => ({}), IconRefreshCw: () => ({}), IconChevronDown: () => ({}), IconChevronRight: () => ({}), IconMessageSquare: () => ({}), IconTerminal: () => ({}), IconBrain: () => ({}), IconTool: () => ({}), IconX: () => ({}) }
  sandbox.require = (s) => s === 'react' ? sandbox.react : s === '@deepseek-ai/dsh-client-ui-primitives' ? sandbox.primitives : (() => { throw new Error('unknown require: ' + s) })()
  vm.runInNewContext(bundle, sandbox, { filename: 'client.js' })
  sandbox.setInterval = (fn, ms) => { sandbox.intervals = sandbox.intervals || []; const id = sandbox.intervals.length; sandbox.intervals.push(fn); return id; }
  sandbox.clearInterval = (id) => { if (sandbox.intervals && sandbox.intervals[id]) sandbox.intervals[id] = null; }
  sandbox.WebSocket = function(u) { this.url = u; this.onopen = this.onclose = this.onmessage = this.onerror = null; this.readyState = 0; this.close = () => { this.readyState = 3; if (this.onclose) this.onclose() }; setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen() }, 0) }
  sandbox.location = { protocol: 'https:', host: 'example.com' }
  sandbox.URLSearchParams = URLSearchParams
  const exports = sandbox.registered.factory(sandbox.require)
  const mount = { slot: null, declaration: null, component: null, dictionaries: {} }
  const ctx = { effect(fn) { fn() }, locale: { register: (ns, d) => { mount.dictionaries[ns] = d }, bind: (ns) => (k) => ns + ':' + k }, slots: { inject: (s, cb) => { mount.slot = s; mount.callback = cb }, register: (d, c) => { mount.declaration = d; mount.component = c } } }
  exports.apply(ctx)
  mount.callback()
  sandbox.render = () => { sandbox.cursor = 0; sandbox.effects = []; sandbox.tree = mount.component({ t: k => k }); return sandbox.tree }
  sandbox.runEffectsAndSettle = async () => { for (const e of sandbox.effects) { const r = e(); if (r && r.then) await r } await flush() }
  return { sandbox, exports, mount }
}

function responseStub({ok, status, contentType, body}) { return { ok, status, headers: { get: n => n.toLowerCase() === 'content-type' ? contentType : null }, json: async () => body } }

function collectStrings(node, out = []) {
  if (node === null || node === undefined || typeof node === 'boolean') return out
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (Array.isArray(node)) { for (const child of node) collectStrings(child, out); return out }
  if (typeof node === 'object') { if (node.props !== undefined && node.props !== null) collectStrings(node.props.children, out); collectStrings(node.children, out) }
  return out
}

test('debug main test flow', async () => {
  const calls = []
  const { sandbox } = loadBundle(async (url, init) => {
    const urlStr = String(url)
    calls.push({ url: urlStr, init })
    if (urlStr.includes('api.github.com')) {
      return responseStub({ ok: true, status: 200, contentType: 'application/json', body: [{ number: 123, title: 'Test task', state: 'open', html_url: 'https://github.com/test/123', assignees: [{login: 'user1'}], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' }] })
    }
    if (urlStr.includes('/api/events?task_id=')) {
      const taskId = decodeURIComponent(urlStr.match(/[?&]task_id=([^&]+)/)?.[1] || '')
      return responseStub({ ok: true, status: 200, contentType: 'application/json', body: { events: [ { id: 1, task_id: taskId, seq: -1, ts: Date.now()-10000, source: 'system', kind: 'task_queued', data: {} }, { id: 2, task_id: taskId, seq: -2, ts: Date.now()-5000, source: 'system', kind: 'task_dispatched', data: {} }, { id: 3, task_id: taskId, seq: 1, ts: Date.now()-1000, source: 'job', kind: 'job_start', data: {} } ], has_more: false, next_after: 3 } })
    }
    throw new Error('unexpected: ' + urlStr)
  })
  const tree1 = sandbox.render()
  const strings1 = collectStrings(tree1)
  console.log('After first render:')
  console.log('  calls:', calls.map(c => c.url))
  console.log('  strings:', strings1)
  await sandbox.runEffectsAndSettle()
  console.log('After effects:')
  console.log('  calls:', calls.map(c => c.url))
  const tree2 = sandbox.render()
  const strings2 = collectStrings(tree2)
  console.log('After second render:')
  console.log('  strings:', strings2)
  assert.ok(strings2.includes('123'), 'задача #123 не отрендерилась: ' + JSON.stringify(strings2))
  assert.ok(strings2.includes('Test task'), 'название задачи не отрендерилось')
  assert.ok(strings2.includes('statusRunning'), 'статус running не показан: ' + JSON.stringify(strings2))
})