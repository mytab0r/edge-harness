import test from 'node:test'
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

test('debug fetch', async () => {
  const calls = []
  const { sandbox } = loadBundle(async (url) => {
    calls.push(String(url))
    if (String(url).includes('api.github.com')) return responseStub({ ok: true, status: 200, contentType: 'application/json', body: [{ number: 123, title: 'Test', state: 'open', html_url: 'https://github.com/test/123', assignees: [{login: 'u'}], labels: [{name: 'task'}], created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-02T00:00:00Z' }] })
    if (String(url).includes('/api/events')) return responseStub({ ok: true, status: 200, contentType: 'application/json', body: { events: [], has_more: false, next_after: 0 } })
    throw new Error('unexpected: ' + url)
  })
  sandbox.render()
  console.log('After first render, calls:', calls)
  await sandbox.runEffectsAndSettle()
  console.log('After effects, calls:', calls)
  sandbox.render()
  console.log('After second render, calls:', calls)
  const strings = []
  function collect(n) { if (n === null || n === undefined || typeof n === 'boolean') return; if (typeof n === 'string' || typeof n === 'number') { strings.push(String(n)); return } if (Array.isArray(n)) { for (const c of n) collect(c); return } if (typeof n === 'object') { if (n.props) collect(n.props.children); collect(n.children) } }
  collect(sandbox.tree)
  console.log('Render strings:', strings)
})