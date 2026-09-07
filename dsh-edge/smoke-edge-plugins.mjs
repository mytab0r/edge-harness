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
 * С #378 в бутстрапе смонтированы также LlmRuntime и in-memory
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
import { pathToFileURL } from 'node:url'

/**
 * Чистый разбор текста сгенерированного модуля (без IO) — вынесен отдельно,
 * чтобы юнит-тест (dsh-edge/test/smoke-edge-plugins.test.mjs) кормился
 * реальным форматом кодогенератора без необходимости поднимать cordis/clone.
 */
export function parseGeneratedModule(generated) {
  const imports = new Map()
  for (const m of generated.matchAll(/^import (\w+) from '([^']+)'$/gmu)) imports.set(m[1], m[2])
  const entries = [...generated.matchAll(/^\s*\{ id: '([^']+)', plugin: (\w+) \},?$/gmu)]
    .map(m => ({ id: m[1], specifier: imports.get(m[2]) }))
  return { imports, entries }
}

/**
 * Классифицирует результат parseGeneratedModule до любых дорогих шагов
 * (запись бутстрапа, spawn cordis). РАЗНЫЕ условия, не один OR (класс
 * «тихий ноль», живая находка): реестр пуст И импортов нет
 * (entries.length === 0 && imports.size === 0) — легитимное состояние
 * «плагинов нет», дымить действительно нечего.
 * Импортов нет (imports.size === 0), но записи РЕЕСТРА ЕСТЬ — это НЕ
 * «плагинов нет», а формат import-строки (`import X from '...'`) разошёлся
 * с regex'ом: старый общий OR читал этот случай как первый и выходил ДО
 * проверки unknown, которая именно этот случай и ловит — плагины молча
 * пропадали бы из дыма при смене формы import-объявления, exit 0.
 * Зеркальная сторона того же класса: записей реестра нет (entries.length
 * === 0), но импорты ЕСТЬ (imports.size > 0) — codegen-edge-plugins.mjs
 * эмитит import и запись `{ id: '...', plugin: X }` в одном цикле
 * (renderServerModule), поэтому такое расхождение в проде означает, что
 * regex записи реестра разошёлся с реальной формой (кавычки, лишнее поле,
 * перенос строки) — это НЕ «плагинов нет», а registry-format-drift. Старый
 * код читал этот случай как no-plugins (первая же проверка entries.length
 * === 0) и молча уходил в exit 0, теряя реально сгенерированные плагины из
 * дыма.
 */
export function classifyParsedModule({ imports, entries }) {
  if (entries.length === 0 && imports.size > 0) {
    return { kind: 'registry-format-drift', importsCount: imports.size }
  }
  if (entries.length === 0) {
    return { kind: 'no-plugins' }
  }
  if (imports.size === 0) {
    return { kind: 'import-format-drift', entriesCount: entries.length }
  }
  const unknown = entries.filter(e => e.specifier === undefined)
  if (unknown.length > 0) {
    return { kind: 'unknown-specifier', unknownIds: unknown.map(e => e.id) }
  }
  return { kind: 'ok', entries }
}

// CLI-режим: только при прямом вызове `node dsh-edge/smoke-edge-plugins.mjs`,
// не при импорте функций выше юнит-тестом (тот же приём, что integrations.mjs).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
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
  const parsed = parseGeneratedModule(generated)
  const classified = classifyParsedModule(parsed)

  if (classified.kind === 'no-plugins') {
    console.log('smoke-edge-plugins: серверных плагинов нет — деградация в апстримную сборку, дымить нечего')
    process.exit(0)
  }
  if (classified.kind === 'registry-format-drift') {
    process.stderr.write(`smoke-edge-plugins: сгенерированный модуль несёт ${classified.importsCount} import(ов), но НИ ОДНОЙ записи реестра — форма '{ id: \\'...\\', plugin: X }' разошлась с regex'ом извлечения записей, кодогенератор менял форму? Бросить громко.\n`)
    process.exit(2)
  }
  if (classified.kind === 'import-format-drift') {
    process.stderr.write(`smoke-edge-plugins: сгенерированный модуль несёт ${classified.entriesCount} запись(ей) реестра, но НИ ОДНОГО import — форма 'import X from \\'...\\'' разошлась с regex'ом извлечения импортов, кодогенератор менял форму? Бросить громко.\n`)
    process.exit(2)
  }
  if (classified.kind === 'unknown-specifier') {
    process.stderr.write(`smoke-edge-plugins: сгенерированный модуль вне ожидаемой формы (импорт не найден для: ${classified.unknownIds.join(', ')}) — кодогенератор менял форму? Бросить громко.\n`)
    process.exit(2)
  }

  const entries = classified.entries
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
// inject плагина реестра провайдеров #378) и провайдер настроек ('settings' —
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
}
