/**
 * Рантайм-дым плагинной композиции dsh-edge: инсталлирует каждый серверный
 * плагин из сгенерированного модуля (edge-plugins.generated.ts) в настоящий
 * cordis Context с настоящими SystemPrompt + ToolRuntime из node_modules
 * клона и требует, чтобы apply каждого прошёл. Гвардия состава
 * (verify-edge-plugins.mjs) доказывает «плагин в бандле», этот дым —
 * «плагин инсталлируется»: класс ошибки #100 (плагин объявляет inject: [],
 * читает ctx.tools, cordis бросает «cannot get property … without inject»,
 * инсталл-цикл помечает failed уже в рантайме — а сборка зелёная) ловится
 * здесь, до деплоя. Красный дым = красный деплой, не тихая неполная морда.
 *
 * Ограничение честно названо: дым идёт в Node, не в workerd — путь execute()
 * инструментов (fetch, env воркера) им не покрыт; покрыт путь монтирования
 * (apply + effect + регистрация в реестре тулов), на котором случился #100.
 * С #114 в бутстрапе смонтированы также LlmRuntime и in-memory
 * SettingsProvider: серверный плагин реестра провайдеров объявляет inject
 * ['llm'] и монтирует settings-namespace — без этих сервисов его apply
 * не дошёл бы до конца, и дым не поймал бы класс «плагин не монтируется
 * без сервисов» до деплоя.
 *
 * Использование:
 *
 *   node dsh-edge/smoke-edge-plugins.mjs <clone-root>
 *
 * clone-root — каталог клона pawaca/dsh-edge с применёнными патчами,
 * установленными зависимостями standalone (pnpm install + pnpm add tgz) и
 * выполненной кодогенерацией (dsh-edge/codegen-edge-plugins.mjs). Порядок
 * шагов — как в deploy-dsh-edge.yml: дым запускается после кодогенерации.
 */

import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

const cloneRoot = process.argv[2]
if (!cloneRoot) {
  process.stderr.write('Usage: node dsh-edge/smoke-edge-plugins.mjs <clone-root>\n')
  process.exit(2)
}

const standaloneDir = join(cloneRoot, 'apps', 'dsh-edge', 'standalone')
const generatedPath = join(cloneRoot, 'apps', 'dsh-edge', 'src', 'edge-plugins.generated.ts')
// Бутстрап живёт внутри standalone, чтобы голые спецификаторы
// (@deepseek-ai/*, @edge-harness/*) резолвились из её node_modules. Сам
// сгенерированный модуль бутстрап не импортирует: его импорты резолвятся
// относительно src/ (в бандле их строит алиас патча 0001, голый Node их не
// возьмёт). Состав читается из его текста — формат фиксирован кодогенератором
// этого же репо (одно место правды о форме), пакеты импортирует бутстрап.
const generated = readFileSync(generatedPath, 'utf8')
const imports = new Map()
for (const m of generated.matchAll(/^import (\w+) from '([^']+)'$/gmu)) imports.set(m[1], m[2])
const entries = [...generated.matchAll(/^\s*\{ id: '([^']+)', plugin: (\w+) \},?$/gmu)]
  .map(m => ({ id: m[1], specifier: imports.get(m[2]) }))
if (imports.size === 0 || entries.length === 0) {
  console.log('smoke-edge-plugins: серверных плагинов нет — деградация в апстримную сборку, дымить нечего')
  process.exit(0)
}
const unknown = entries.filter(e => e.specifier === undefined)
if (unknown.length > 0) {
  process.stderr.write(`smoke-edge-plugins: сгенерированный модуль вне ожидаемой формы (импорт не найден для: ${unknown.map(e => e.id).join(', ')}) — кодогенератор менял форму? Бросить громко.\n`)
  process.exit(2)
}

const bootstrapPath = join(standaloneDir, 'smoke-edge-plugins.bootstrap.mjs')
const registry = JSON.stringify(entries)
// Стирание типов (import .ts) включено в Node по умолчанию с 23.6; CI и
// локально — Node 24.
const bootstrap = `
import { Context } from '@deepseek-ai/cordis'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { LlmRuntime } from '@deepseek-ai/dsh-llm'
import SettingsProvider from '@deepseek-ai/dsh-settings'

// Сервисы, без которых плагины морды не монтируются: LlmRuntime ('llm' —
// inject плагина реестра провайдеров #114) и провайдер настроек ('settings' —
// шов installSettingsSection). In-memory SettingsProvider: persist — no-op
// (write() сам кладёт раздел в this.document), load() отдаёт текущий документ.
class MemorySettingsProvider extends SettingsProvider {
  writable = true
  async load() { return this.document }
  async persist() {}
}

const entries = ${registry}
const ctx = new Context()
await ctx.plugin(SystemPrompt)
await ctx.plugin(ToolRuntime)
await ctx.plugin(LlmRuntime)
await ctx.plugin(MemorySettingsProvider)

const failed = []
for (const entry of entries) {
  const before = ctx.tools.schemas().length
  try {
    const plugin = (await import(entry.specifier)).default
    await ctx.plugin(plugin)
  } catch (error) {
    failed.push(entry.id)
    console.error('smoke-edge-plugins: плагин "' + entry.id + '" не инсталлировался:', error)
    continue
  }
  const delta = ctx.tools.schemas().length - before
  console.log('smoke-edge-plugins: "' + entry.id + '" инсталлировался (тулов добавлено: ' + delta + ')')
}

if (failed.length > 0) {
  console.error('smoke-edge-plugins: КРАСНЫЙ — не инсталлировались: ' + failed.join(', ')
    + '. Сборка с неинсталлируемым плагином запрещена: в проде это тихий минус тулов (класс #100).')
  process.exit(1)
}

const names = ctx.tools.schemas().map((s) => s.name).sort()
console.log('smoke-edge-plugins: тулсет после инсталла: ' + names.join(', '))
console.log('smoke-edge-plugins: ЗЕЛЁНЫЙ — все ' + entries.length + ' плагин(ов) инсталлируются в cordis с ToolRuntime')
process.exit(0)
`

await writeFile(bootstrapPath, bootstrap, 'utf8')
try {
  const run = spawnSync(process.execPath, [bootstrapPath], { cwd: standaloneDir, encoding: 'utf8' })
  process.stdout.write(run.stdout ?? '')
  process.stderr.write(run.stderr ?? '')
  process.exit(run.status ?? 1)
} finally {
  await rm(bootstrapPath, { force: true })
}
