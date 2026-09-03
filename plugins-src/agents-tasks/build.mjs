/**
 * Сборка релизного пакета agents-tasks (issue #111).
 *
 * Продукт (оба файла генерируются, в git не хранятся — .gitignore):
 *   client/client.js — финальный клиентский бандл в обёртке
 *                      window.__ModuleLoader__.load({id, factory}) (форма
 *                      продовых бандлов ростера, напр. dsh-edge-client-ui);
 *   manifest.json    — байт-в-байт копия dsh-edge/plugins.json: манифест
 *                      приезжает в пакет при сборке релиза, чтобы в бандле
 *                      и в tarball был один и тот же проверенный источник.
 *
 * Единственное место правды формы манифеста — dsh-edge/manifest.mjs
 * (parseManifest): невалидный манифест = ненулевой exit без записи продукта.
 *
 * Дальше по конвейеру #80 (см. README): npm pack → переименование asset'а →
 * gh release create → sha256 в dsh-edge/plugins.json (PR).
 *
 * Использование: node plugins-src/agents-tasks/build.mjs
 */

import { spawnSync } from 'node:child_process'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parseManifest } from '../../dsh-edge/manifest.mjs'

const pluginDir = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(pluginDir, '..', '..')

// ── 1. Декларация пакета ───────────────────────────────────────────────────────
// assemble-standalone-web.mjs кладёт в ростер только пакеты с веб-декларацией
// dsh.client и читает бандл из exports['./client']; без этого пакет соберётся,
// но молча не попадёт в морду — проверяем здесь, а не на чужой сборке.
const pkg = JSON.parse(await readFile(join(pluginDir, 'package.json'), 'utf8'))
failUnless(pkg.name === '@edge-harness/dsh-agents-tasks',
  'package.json: name должен быть "@edge-harness/dsh-agents-tasks" (id обёртки бандла обязан совпадать с именем пакета — по нему boot-граф ищет фабрику)')
failUnless(pkg.dsh?.client?.platform === 'web',
  'package.json: нужна декларация dsh.client { platform: "web" } — без неё assemble исключит пакет из ростера')
failUnless(Array.isArray(pkg.dsh.client.inject) && pkg.dsh.client.inject.length > 0,
  'package.json: dsh.client.inject обязан перечислять сервисные пакеты (порядок загрузки ростера)')

// ── 2. Манифест (одно место правды формы — dsh-edge/manifest.mjs) ─────────────
const manifestPath = join(repoRoot, 'dsh-edge', 'plugins.json')
const manifestSource = await readFile(manifestPath, 'utf8')
const manifest = parseManifest(manifestSource)
// Пакет обязан быть объявлен в том каталоге, из которого собирается: sha256
// в каталоге доказывает целостность артефакта, но не его свежесть — сборка
// из устаревшего среза каталога молча уехала бы в релиз с чужим составом.
failUnless(manifest.plugins.some((plugin) => plugin.package === pkg.name),
  `dsh-edge/plugins.json: пакет ${pkg.name} не объявлен в каталоге — релиз нельзя собирать из несвежего среза`)
// Клиенту нужен только состав: id и где он живёт. release/sha256 — канал
// поставки, воркеру и журналу они в браузере ничего не говорят.
const roster = manifest.plugins.map(({ id, server, client }) => ({ id, server, client }))

// ── 3. Тело бандла и его гвардии ──────────────────────────────────────────────
const body = await readFile(join(pluginDir, 'src', 'body.js'), 'utf8')

// Seed-карта шелла (staticModules при boot) — снята с продового бандла
// dsh-edge 0.7.1 (assets/index-*.js). require чего-то ещё без декларации в
// dsh.client.inject = throw при материализации в браузере: ловим на сборке,
// а не в консоли владельца. Апстрим обновил карту — сборка здесь покраснеет,
// список сверяется заново (fail loud, не тихий уход в рантайм).
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
// присваивания (`require = …` проскочит) — регэкс-гвард по определению
// неполон; достаточно, потому что присваивание свободной переменной обёртки
// затирало бы её только внутри фабрики и ловится синтаксической проверкой
// бандла при использовании.
for (const reserved of ['module', 'exports', 'require', 'MANIFEST']) {
  failUnless(!new RegExp(`(?:var|let|const|function|class)\\s+${reserved}\\b`).test(body),
    `src/body.js: тело объявляет "${reserved}" — это свободная переменная обёртки, коллизия`)
}

// ── 4. Бандл ──────────────────────────────────────────────────────────────────
const bundle = [
  `window.__ModuleLoader__.load({`,
  `\tid: ${JSON.stringify(pkg.name)},`,
  `\tfactory: (require) => {`,
  `\tvar module = { exports: {} }; var exports = module.exports;`,
  ``,
  `const MANIFEST = ${JSON.stringify(roster, null, 2)};`,
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
// Копия — байт-в-байт: пакет показывает тот же проверенный манифест, из
// которого сшит бандл, а не пересказ.
await writeFile(join(pluginDir, 'manifest.json'), manifestSource, 'utf8')

checkSyntax(join(pluginDir, 'src', 'body.js'))
checkSyntax(bundlePath)

const clientIds = manifest.plugins.filter((plugin) => plugin.client).map((plugin) => plugin.id)
process.stdout.write(
  `agents-tasks: бандл собран (${required.length} seed-require), манифест ${manifest.plugins.length} плагин(ов) ` +
  `[${manifest.plugins.map((plugin) => plugin.id).join(', ')}], клиентских в ростере: [${clientIds.join(', ') || 'нет'}]\n` +
  `  ${relative(repoRoot, bundlePath)}\n` +
  `  ${relative(repoRoot, join(pluginDir, 'manifest.json'))}\n` +
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
  if (!condition) throw new Error(`agents-tasks build: ${message}`)
}