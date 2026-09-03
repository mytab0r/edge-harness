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
  sandbox.react = { createElement, useState(initial) { const i = sandbox.cursor++; if (sandbox.cells[i] === undefined) sandbox.cells[i] = typeof initial === 'function' ? initial() : initial; return [sandbox.cells[i], v => { sandbox.cells[i] = typeof v === 'function' ? v(sandbox.cells[i]) : v }] }, useCallback(fn) { return fn }, useEffect(fn) { sandbox.effects.push(fn) }, useRef(init) { return { current: init } } }
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

test('debug session_event expanded', async () => {
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

  const { sandbox } = loadBundle(async (url) => {
    const urlStr = String(url)
    if (urlStr.includes('api.github.com')) return responseStub(githubResponse)
    if (urlStr.includes('/api/events?task_id=')) return responseStub(journalResponse)
    throw new Error('unexpected: ' + urlStr)
  })

  sandbox.render()
  await sandbox.runEffectsAndSettle()
  sandbox.render()

  const strings1 = collectStrings(sandbox.tree)
  console.log('Collapsed view:', strings1)

  // Expand task
  sandbox.cells[3] = 200 // selectedTaskNumber
  sandbox.cells[4] = new Set([200]) // expandedTasks
  sandbox.render()

  const strings2 = collectStrings(sandbox.tree)
  console.log('Expanded view:', strings2)
})