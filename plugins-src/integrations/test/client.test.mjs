/**
 * Поведенческая гвардия клиентского бандла integrations (#115).
 *
 * Проверяется форма обёртки бандла (что собрал build.mjs), монтаж в слот
 * settings.section, паритет ключей словарей, вшитый реестр (= dsh-edge/
 * integrations.json), и поведение ячеек статусов на ПРОДА-форме ответа
 * журнала {events:[{id, kind, data}], has_more, next_after} с проходом
 * страниц до конца выборки — по контракту cf-worker/src/harness.ts.
 *
 * Запуск: node --test plugins-src/integrations/test/client.test.mjs
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

const pluginDir = dirname(fileURLToPath(import.meta.url)) // …/integrations/test
const packageDir = dirname(pluginDir)
const repoRoot = join(packageDir, '..', '..')

// ── Сборка: тот же шаг, что и перед npm pack ──────────────────────────────────
// Синхронно ДО чтения продукта ниже: тесты проверяют ровно то, что собралось,
// а не бандл предыдущего прогона.
const buildResult = spawnSync(process.execPath, [join(packageDir, 'build.mjs')], { encoding: 'utf8' })

test('build.mjs собирает продукт без ошибок', async () => {
  assert.equal(buildResult.status, 0, `build.mjs упал:\n${buildResult.stdout}\n${buildResult.stderr}`)
  await assert.doesNotReject(() => readFile(join(packageDir, 'client', 'client.js'), 'utf8'))
  await assert.doesNotReject(() => readFile(join(packageDir, 'integrations.json'), 'utf8'))
})

const bundle = await readFile(join(packageDir, 'client', 'client.js'), 'utf8')
const registryCopy = JSON.parse(await readFile(join(packageDir, 'integrations.json'), 'utf8'))
const registryRepo = JSON.parse(await readFile(join(repoRoot, 'dsh-edge', 'integrations.json'), 'utf8'))

function extractBakedRegistry() {
  const match = bundle.match(/const INTEGRATIONS = (\[[\s\S]*?\]);\n/)
  assert.ok(match, 'в бандле нет константы INTEGRATIONS')
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
      return [sandbox.cells[index], (value) => {
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

function collectButtons(node, out = []) {
  if (node === null || node === undefined || typeof node !== 'object') return out
  if (Array.isArray(node)) { for (const child of node) collectButtons(child, out); return out }
  if (node.type === 'Button' && typeof node.props?.onClick === 'function') out.push(node.props)
  for (const child of node.children ?? []) collectButtons(child, out)
  if (node.props !== undefined && node.props !== null) collectButtons(node.props.children, out)
  return out
}

function findNode(tree, predicate) {
  const stack = [tree]
  while (stack.length > 0) {
    const node = stack.pop()
    if (node === null || node === undefined || typeof node !== 'object') continue
    if (Array.isArray(node)) { stack.push(...node); continue }
    if (predicate(node)) return node
    stack.push(node.children ?? [])
    if (node.props !== undefined && node.props !== null) stack.push(node.props.children ?? [])
  }
  return null
}

function responseStub({ ok, status, contentType, body }) {
  return {
    ok, status,
    headers: { get: (name) => (name.toLowerCase() === 'content-type' ? contentType : null) },
    json: async () => body,
  }
}

/**
 * Прод-форма журнала (контракт cf-worker/src/harness.ts): события по возрасту
 * id, страницы от старейших, has_more/next_after. statuses — карта id → массив
 * data событий integration_status, разложенная по страницам по 1 событию, чтобы
 * проход по next_after был настоящим, а не декорацией.
 */
function journalStub(statuses) {
  const calls = []
  return { calls, fetch: async (url) => {
    calls.push(String(url))
    const address = new URL('https://morde.test' + String(url))
    if (!address.pathname.startsWith('/api/harness/events')) {
      throw new Error('неожиданный запрос: ' + url)
    }
    const taskId = address.searchParams.get('task_id') ?? ''
    const id = taskId.startsWith('integration:') ? taskId.slice('integration:'.length) : null
    if (id === null || !Object.hasOwn(statuses, id)) {
      throw new Error('неожиданный task_id: ' + taskId)
    }
    const events = statuses[id].map((data, index) => ({ id: index + 1, seq: index + 1, source: 'deploy', kind: 'integration_status', data }))
    const after = Number(address.searchParams.get('after') ?? '0')
    const page = events.filter((event) => event.id > after).slice(0, 1)
    const last = page.length > 0 ? page[page.length - 1].id : after
    return responseStub({
      ok: true, status: 200, contentType: 'application/json',
      body: { events: page, has_more: last < events.length, next_after: last },
    })
  } }
}

// ── Проверки ──────────────────────────────────────────────────────────────────

test('обёртка бандла: id = имя пакета, на экспорте inject/apply', () => {
  const { sandbox, exports } = loadBundle(journalStub({}).fetch)
  assert.equal(sandbox.registered.id, '@edge-harness/dsh-plugin-integrations',
    'id регистрации обязан совпадать с именем пакета (boot-граф ищет фабрику по нему)')
  assert.deepEqual(Array.from(exports.inject), ['slots', 'locale'])
  assert.equal(typeof exports.apply, 'function')
})

test('монтаж: слот settings.section, id integrations, namespace словарей settings.integrations', () => {
  const { mount } = loadBundle(journalStub({}).fetch)
  assert.deepEqual(
    { id: mount.declaration.id, order: mount.declaration.order, locale: mount.declaration.locale, name: mount.declaration.name },
    { id: 'integrations', order: 94, locale: 'settings.integrations', name: 'settings.section' },
  )
  assert.ok(typeof mount.declaration.label === 'function')
  assert.ok(mount.dictionaries['settings.integrations'], 'словари не зарегистрированы')
})

test('словари: набор ключей одинаков в en/zh/ru', () => {
  const { mount } = loadBundle(journalStub({}).fetch)
  const dicts = mount.dictionaries['settings.integrations']
  const locales = Object.keys(dicts)
  assert.ok(locales.includes('en') && locales.includes('zh') && locales.includes('ru'), `локали: ${locales.join(', ')}`)
  const reference = Object.keys(dicts.en).sort()
  for (const locale of locales) {
    assert.deepEqual(Object.keys(dicts[locale]).sort(), reference, `набор ключей ${locale} разошёлся с en`)
  }
})

test('вшитый реестр = dsh-edge/integrations.json репозитория (id, тулы, имена секретов)', () => {
  const baked = extractBakedRegistry()
  assert.deepEqual(baked, registryCopy.integrations, 'вшитый срез разошёлся с копией реестра в пакете')
  assert.deepEqual(
    registryCopy.integrations.map((entry) => entry.id),
    registryRepo.integrations.map((entry) => entry.id),
    'копия реестра в пакете разошлась с репозиторием — пересобери перед npm pack',
  )
})

test('секция: строка на каждую интеграцию — имя, тулы, имена ключей, значения секретов не появляются', async () => {
  const statuses = Object.fromEntries(registryCopy.integrations.map((entry) => [entry.id, [{ integration: entry.id, state: 'ready' }]]))
  const { sandbox } = loadBundle(journalStub(statuses).fetch)
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  const tree = sandbox.render()

  const strings = collectStrings(tree)
  for (const entry of registryCopy.integrations) {
    assert.ok(strings.includes(entry.id), `нет строки ${entry.id}`)
    assert.ok(strings.includes(entry.title), `нет названия ${entry.title}`)
  }
  assert.ok(strings.includes('jira_issue') && strings.includes('slack_post'), 'инструменты не показаны')
  assert.ok(strings.includes('JIRA_API_TOKEN') && strings.includes('SLACK_BOT_TOKEN'), 'имена ключей не показаны')
  // Структурная гвардия «значений секретов не существует»: у вшитой записи нет
  // полей вне формы реестра, а все значения описаний — из того же файла репо.
  const baked = extractBakedRegistry()
  const ALLOWED_KEYS = ['id', 'title', 'summary', 'tools', 'credentials', 'docs', 'wired']
  for (const entry of baked) {
    for (const key of Object.keys(entry)) {
      assert.ok(ALLOWED_KEYS.includes(key), `в вшитой записи ${entry.id} лишнее поле ${key} — форме реестра там не место`)
    }
    for (const [name, description] of Object.entries(entry.credentials?.secrets ?? {})) {
      assert.equal(typeof description, 'string', `описание секрета ${name} обязано быть текстом описания, не значением`)
    }
  }
})

test('статусы: прод-форма журнала, свежайшее событие побеждает (проход страниц по next_after)', async () => {
  const statuses = {
    jira: [
      { integration: 'jira', state: 'not_configured', detail: 'нет секретов: JIRA_API_TOKEN' },
      { integration: 'jira', state: 'ready' },
    ],
    confluence: [{ integration: 'confluence', state: 'not_configured', detail: 'нет секретов: CONFLUENCE_BASE_URL' }],
    bitbucket: [{ integration: 'bitbucket', state: 'failed', detail: 'деплой упал' }],
    slack: [{ integration: 'slack', state: 'deploying' }],
    telegram: [],
  }
  const stub = journalStub(statuses)
  const { sandbox } = loadBundle(stub.fetch)
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  const tree = sandbox.render()
  const strings = collectStrings(tree)

  assert.ok(strings.includes('statusReady'), 'jira должна показать ready (последнее событие)')
  assert.ok(strings.includes('statusNotConfigured'), 'confluence осталась not_configured')
  assert.ok(strings.includes('statusFailed'), 'bitbucket — failed')
  assert.ok(strings.includes('statusOngoing'), 'deploying — промежуточное, «в процессе»')
  assert.ok(strings.includes('statusNoEvents'), 'telegram без событий — честное «деплой ещё не отчитывался»')
  assert.ok(stub.calls.some((url) => url.includes('after=')), 'проход по страницам был не единственным запросом')
})

test('not_configured: detail с именами недостающих секретов виден рядом со статусом', async () => {
  const statuses = {
    jira: [{ integration: 'jira', state: 'not_configured', detail: 'нет секретов: JIRA_API_TOKEN' }],
    confluence: [], bitbucket: [], slack: [], telegram: [],
  }
  const { sandbox } = loadBundle(journalStub(statuses).fetch)
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  const tree = sandbox.render()
  const jiraRow = findNode(tree, (node) => node.type === 'li' && collectStrings(node).includes('jira'))
  const strings = collectStrings(jiraRow)
  assert.ok(strings.includes('statusNotConfigured'))
  assert.ok(strings.includes('нет секретов: JIRA_API_TOKEN'), 'деталь статуса не дошла до строки')
})

test('событие без state — warning с сырыми данными, неизвестный state показывается как есть', async () => {
  const statuses = {
    jira: [{ integration: 'jira', source: 'deploy' }],
    confluence: [{ integration: 'confluence', state: 'somewhere-else' }],
    bitbucket: [], slack: [], telegram: [],
  }
  const { sandbox } = loadBundle(journalStub(statuses).fetch)
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  const strings = collectStrings(sandbox.render())
  assert.ok(strings.includes('statusUnknown'), 'событие без state — не «подключено»')
  assert.ok(collectStrings(sandbox.render()).some((s) => s.includes('"source":"deploy"')), 'сырые данные события видны')
  assert.ok(strings.includes('somewhere-else'), 'неизвестный state не выдумывается')
})

test('отказ журнала — громкая ошибка секции с «Повторить», а не тихие «не настроено»', async () => {
  const failing = async () => responseStub({ ok: false, status: 503, contentType: 'application/json', body: { error: { code: 'unavailable', message: 'x' } } })
  const { sandbox } = loadBundle(failing)
  sandbox.render()
  await sandbox.runEffectsAndSettle()
  let strings = collectStrings(sandbox.render())
  assert.ok(strings.includes('journalError'), 'ошибка журнала не показана')
  assert.ok(collectButtons(sandbox.render()).length > 0, 'нет кнопки Повторить')

  const notJson = async () => responseStub({ ok: true, status: 200, contentType: 'text/html', body: '<html></html>' })
  const second = loadBundle(notJson)
  second.sandbox.render()
  await second.sandbox.runEffectsAndSettle()
  strings = collectStrings(second.sandbox.render())
  assert.ok(strings.includes('journalError'), 'чужой origin с HTML не должен притворяться журналом')

  const wrongShape = async () => responseStub({ ok: true, status: 200, contentType: 'application/json', body: { unexpected: true } })
  const third = loadBundle(wrongShape)
  third.sandbox.render()
  await third.sandbox.runEffectsAndSettle()
  assert.ok(collectStrings(third.sandbox.render()).includes('journalError'), 'ответ без массива events — ошибка, не «статусов нет»')
})
