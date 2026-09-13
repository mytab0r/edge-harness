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
 * Тело массива edgePlugins — текст между строкой объявления
 * `export const edgePlugins… = [` и закрывающей `]` (обрезанный по краям).
 * codegen-edge-plugins.mjs::renderServerModule эмитит объявление и закрывающую
 * скобку БЕЗУСЛОВНО, даже при нуле серверных плагинов (обе строки —
 * безусловные `lines.push`), поэтому по телу различимо «плагинов нет»
 * (тело пусто) и «записи в тексте есть, но regex их не видит» (тело непусто)
 * — без тела совместный дрейф обоих regex'ов извлечения неотличим от
 * легитимного нуля. null — объявление или закрывающая скобка не найдены:
 * форма модуля уехала целиком, о «плагинах нет» говорить нельзя.
 */
export function extractRegistryBody(generated) {
  const lines = generated.split('\n')
  const open = lines.findIndex(l => /^export const edgePlugins\b.*= \[\s*$/.test(l))
  if (open === -1) return null
  const close = lines.findIndex((l, i) => i > open && l.trim() === ']')
  if (close === -1) return null
  return lines.slice(open + 1, close).join('\n').trim()
}

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
  return { imports, entries, registryBody: extractRegistryBody(generated) }
}

/**
 * Классифицирует результат parseGeneratedModule до любых дорогих шагов
 * (запись бутстрапа, spawn cordis). РАЗНЫЕ условия, не один OR (класс
 * «тихий ноль», живая находка): «плагинов нет» — это записей нет И тело
 * массива пусто И импортов нет. renderServerModule эмитит import-строку,
 * запись реестра, объявление массива и закрывающую скобку согласованным
 * способом (записи и импорты — из одного цикла одним стилем, объявление и
 * скобка — безусловно), поэтому любое отклонение от «тело пусто, импортов
 * нет» при нуле сматченных записей — дрейф формы, а не ноль плагинов:
 *   - тело непусто (записи в тексте ЕСТЬ, regex их не видит) — покрывает и
 *     односторонний дрейф формы записи, и СОВМЕСТНЫЙ дрейф обоих regex'ов
 *     (одно изменение стиля эмита, например кавычки, ломает оба разом —
 *     это наиболее вероятная форма дрейфа, а не экзотика);
 *   - объявление/скобка не найдены (registryBody === null) — форма модуля
 *     уехала целиком;
 *   - тело пусто, но импорты есть — в прод-форме невозможно.
 * Старый код сваливал все эти состояния в ветку «обе-нуля» → no-plugins →
 * exit 0 — реально сгенерированные плагины молча выпадали из дыма.
 * Импортов нет, но записи реестра ЕСТЬ — дрейф формы import-строки
 * (`import X from '...'`): старый общий OR читал этот случай как первый и
 * выходил ДО проверки unknown, которая именно этот случай и ловит.
 */
export function classifyParsedModule({ imports, entries, registryBody }) {
  if (entries.length === 0) {
    if (registryBody === '' && imports.size === 0) {
      return { kind: 'no-plugins' }
    }
    return {
      kind: 'registry-format-drift',
      importsCount: imports.size,
      registryBody: registryBody === null ? 'missing' : 'non-empty',
    }
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
    const bodyState = classified.registryBody === 'missing'
      ? 'объявление массива edgePlugins не найдено вовсе'
      : 'тело массива edgePlugins НЕПУСТО (записи в тексте есть, regex их не видит)'
    process.stderr.write(`smoke-edge-plugins: сгенерированный модуль дал 0 сматченных записей реестра при ${classified.importsCount} сматченном(ых) импорте(ах), ${bodyState} — форма записей и/или всего модуля разошлась с regex'ами извлечения, кодогенератор менял форму? Бросить громко, а не читать как «плагинов нет».\n`)
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
// шов ctx.settings.installSection, метод сервиса с dsh-settings 0.1.2-rc.1,
// см. issue #806/#507). In-memory SettingsProvider: persist — no-op (write()
// сам кладёт раздел в this.document), load() отдаёт текущий документ.
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
