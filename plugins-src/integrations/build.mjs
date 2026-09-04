/**
 * Сборка релизного пакета integrations (issue #115).
 *
 * Продукт (оба файла генерируются, в git не хранятся — .gitignore):
 *   client/client.js   — финальный клиентский бандл в обёртке
 *                        window.__ModuleLoader__.load({id, factory}) (форма
 *                        продовых бандлов ростера, напр. dsh-edge-client-ui);
 *   integrations.json  — байт-в-байт копия dsh-edge/integrations.json: реестр
 *                        приезжает в пакет при сборке релиза, чтобы в бандле
 *                        и в tarball был один и тот же проверенный источник.
 *
 * Единственное место правды формы реестра — dsh-edge/integrations.mjs
 * (loadIntegrations): невалидный реестр = падение сборки без продукта.
 * Сквозная гвардия сборки: имена инструментов в реестре ↔ имена инструментов
 * в server/index.js обязаны называть друг друга в обе стороны — иначе
 * интеграция объявляет тул, которого нет (тихий минус возможностей), или тул
 * живёт в коде без строки в реестре (тихий минус описания в UI).
 *
 * Дальше по конвейеру #80 (см. README): npm pack → переименование asset'а →
 * gh release create → sha256 в dsh-edge/plugins.json (PR).
 *
 * Использование: node plugins-src/integrations/build.mjs
 */

import { spawnSync } from 'node:child_process'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseIntegrations } from '../../dsh-edge/integrations.mjs'

const pluginDir = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(pluginDir, '..', '..')

// ── 1. Декларация пакета ───────────────────────────────────────────────────────
// assemble-standalone-web.mjs кладёт в ростер только пакеты с веб-декларацией
// dsh.client и читает бандл из exports['./client']; smoke-edge-plugins.mjs и
// esbuild берут сервер через main. Проверяем обе стороны здесь, а не на чужой
// сборке (класс: пакет собрался, но молча не попал в морду).
const pkg = JSON.parse(await readFile(join(pluginDir, 'package.json'), 'utf8'))
failUnless(pkg.name === '@edge-harness/dsh-plugin-integrations',
  'package.json: name должен быть "@edge-harness/dsh-plugin-integrations" (id обёртки бандла обязан совпадать с именем пакета — по нему boot-граф ищет фабрику)')
failUnless(pkg.main === 'server/index.js', 'package.json: main обязан указывать на server/index.js (серверную половину импортирует кодогенерация)')
failUnless(pkg.dsh?.client?.platform === 'web',
  'package.json: нужна декларация dsh.client { platform: "web" } — без неё assemble исключит пакет из ростера')
failUnless(Array.isArray(pkg.dsh.client.inject) && pkg.dsh.client.inject.length > 0,
  'package.json: dsh.client.inject обязан перечислять сервисные пакеты (порядок загрузки ростера)')

// ── 2. Реестр интеграций (одно место правды формы — dsh-edge/integrations.mjs) ─
const registryPath = join(repoRoot, 'dsh-edge', 'integrations.json')
const registrySource = await readFile(registryPath, 'utf8')
// Пустой реестр parseIntegrations отвергает сама (форма), второй проверки не надо.
const registry = parseIntegrations(registrySource)

// Сквозная гвардия «реестр ↔ серверный код». Форма объявления тула в
// server/index.js — литерал name: '<tool>' внутри defineTool; поиск по тексту
// достаточен, потому что проверяем НАЛИЧИЕ имени, а не структуру.
const serverSource = await readFile(join(pluginDir, 'server', 'index.js'), 'utf8')
for (const entry of registry.integrations) {
  for (const tool of entry.tools) {
    failUnless(serverSource.includes(`name: '${tool}'`),
      `реестр объявляет инструмент ${tool} (интеграция ${entry.id}), а в server/index.js его нет — тихий минус возможностей агента`)
  }
}
for (const match of serverSource.matchAll(/defineTool\(\{\s*\n\s*name: '([a-z0-9_]+)'/g)) {
  const tool = match[1]
  failUnless(registry.integrations.some((entry) => entry.tools.includes(tool)),
    `server/index.js регистрирует ${tool}, а в dsh-edge/integrations.json его нет — тул без описания в разделе «Интеграции»`)
}

// ── 3. Тело бандла и его гвардии ──────────────────────────────────────────────
const body = await readFile(join(pluginDir, 'src', 'body.js'), 'utf8')

// Seed-карта шелла (staticModules при boot) — та же, что у plugin-manager:
// require чего-то ещё без декларации в dsh.client.inject = throw при
// материализации в браузере; ловим на сборке, а не в консоли владельца.
const SEED_REQUIRE_ALLOWLIST = new Set([
  'react',
  'react/jsx-runtime',
  'react-dom',
  'react-dom/client',
  '@deepseek-ai/cordis',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-ui-primitives',
])
const required = [...body.matchAll(/require\((["'])([^"')]+)\1\)/g)].map((match) => match[2])
const unseeded = required.filter((specifier) => !SEED_REQUIRE_ALLOWLIST.has(specifier))
failUnless(unseeded.length === 0,
  `src/body.js: require(${unseeded.map((s) => JSON.stringify(s)).join(', ')}) не из seed-карты шелла — ` +
  'добавь пакет в dsh.client.inject package.json и сверь, что он в ростере апстрима')

// Имена, которые тело не имеет права объявлять: их приносит обёртка.
// Регэкс ловит только ОБЪЯВЛЕНИЯ (var/let/const/function/class), не
// присваивания — регэкс-гвард по определению неполон; достаточно, потому что
// присваивание свободной переменной обёртки затирало бы её только внутри
// фабрики и ловится синтаксической проверкой бандла при использовании.
for (const reserved of ['module', 'exports', 'require', 'INTEGRATIONS']) {
  failUnless(!new RegExp(`(?:var|let|const|function|class)\\s+${reserved}\\b`).test(body),
    `src/body.js: тело объявляет "${reserved}" — это свободная переменная обёртки, коллизия`)
}

// ── 4. Бандл ──────────────────────────────────────────────────────────────────
// Вшивается реестр ЦЕЛИКОМ (как CATALOG у plugin-manager): клиенту нужны
// имена секретов с описаниями («чей ключ»), инструменты и сводки — значения
// секретов в реестре не хранятся в принципе.
const bundle = [
  `window.__ModuleLoader__.load({`,
  `\tid: ${JSON.stringify(pkg.name)},`,
  `\tfactory: (require) => {`,
  `\tvar module = { exports: {} }; var exports = module.exports;`,
  ``,
  `const INTEGRATIONS = ${JSON.stringify(registry.integrations, null, 2)};`,
  ``,
  body,
  ``,
  `\texports.inject = inject;`,
  `\texports.apply = apply;`,
  `\treturn module.exports;`,
  `\t}`,
  `});`,
  ``,
].join('\n')

// ── 5. Продукт: пишем только после того, как всё сошлось ──────────────────────
await mkdir(join(pluginDir, 'client'), { recursive: true })
const bundlePath = join(pluginDir, 'client', 'client.js')
await writeFile(bundlePath, bundle, 'utf8')
// Копия — байт-в-байт: пакет показывает тот же проверенный реестр, из которого
// сшит бандл, а не пересказ.
await writeFile(join(pluginDir, 'integrations.json'), registrySource, 'utf8')

checkSyntax(join(pluginDir, 'src', 'body.js'))
checkSyntax(join(pluginDir, 'server', 'core.js'))
checkSyntax(join(pluginDir, 'server', 'index.js'))
checkSyntax(bundlePath)

const wired = registry.integrations.map((entry) =>
  `${entry.id}(${entry.tools.length > 0 ? entry.tools.join(', ') : 'инструментов нет, ' + (entry.wired ?? 'morde')})`)
process.stdout.write(
  `integrations: бандл собран (${required.length} seed-require), реестр ${registry.integrations.length} интеграций: ${wired.join(', ')}\n` +
  `  ${relative(repoRoot, bundlePath)}\n` +
  `  ${relative(repoRoot, join(pluginDir, 'integrations.json'))}\n` +
  'Дальше (конвейер #80): npm pack → asset в релиз → sha256 в dsh-edge/plugins.json (PR)\n',
)

function checkSyntax(file) {
  const result = spawnSync(process.execPath, ['--check', file], { stdio: 'pipe' })
  if (result.status !== 0) {
    process.stderr.write(result.stderr)
    throw new Error(`node --check не прошёл: ${file}`)
  }
}

function failUnless(condition, message) {
  if (!condition) throw new Error(`integrations build: ${message}`)
}
