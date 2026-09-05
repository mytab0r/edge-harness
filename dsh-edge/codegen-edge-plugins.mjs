/**
 * Кодогенератор плагинной композиции dsh-edge: dsh-edge/plugins.json →
 *   1. <clone>/apps/dsh-edge/src/edge-plugins.generated.ts — серверный состав
 *      (литеральные импорты; именно их подхватывает регэксп алиасов,
 *      расширенный патчем 0001, поэтому плагин попадает в бандл воркера);
 *   2. <clone>/apps/dsh-edge/standalone/edge-plugins.json — клиентский ростер,
 *      который читают патчи 0003 (assemble-standalone-web.mjs +
 *      verify-standalone.mjs).
 *
 * Пустой манифест деградирует в апстримную сборку: оба артефакта пишутся
 * пустыми, патчи ведут себя как апстримный код. Использование:
 *
 *   node dsh-edge/codegen-edge-plugins.mjs <clone-root>
 *
 * clone-root — каталог клона pawaca/dsh-edge (пин из dsh-edge/upstream.json).
 */

import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { loadManifestWithForgeExtra, manifestDirectory } from './manifest.mjs'

const cloneRoot = process.argv[2]
if (!cloneRoot) {
  process.stderr.write('Usage: node dsh-edge/codegen-edge-plugins.mjs <clone-root>\n')
  process.exit(2)
}

const MODULE_HEADER = '// Generated from dsh-edge/plugins.json — do not edit; run dsh-edge/codegen-edge-plugins.mjs.\n'

const repoDir = manifestDirectory()
// FORGE_EXTRA_PLUGIN (env) — плагин форжа, которого ещё нет в dsh-edge/plugins.json
// на момент интеграционного дыма plugin-forge.yml; см. manifest.mjs.
const manifest = await loadManifestWithForgeExtra(repoDir)

const generatedModule = renderServerModule(manifest.plugins)
const generatedRoster = renderClientRoster(manifest.plugins)

const modulePath = join(cloneRoot, 'apps', 'dsh-edge', 'src', 'edge-plugins.generated.ts')
const rosterPath = join(cloneRoot, 'apps', 'dsh-edge', 'standalone', 'edge-plugins.json')
await writeFile(modulePath, generatedModule, 'utf8')
await writeFile(rosterPath, generatedRoster, 'utf8')

const serverIds = manifest.plugins.filter(p => p.server).map(p => p.id)
const clientIds = manifest.plugins.filter(p => p.client).map(p => p.id)
process.stdout.write(
  `edge-plugins: generated composition for ${manifest.plugins.length} plugin(s)`
  + ` (server: ${serverIds.join(', ') || 'none'}; client: ${clientIds.join(', ') || 'none'})\n`,
)

function renderServerModule(plugins) {
  const lines = [MODULE_HEADER]
  const entries = []
  for (const plugin of plugins.filter(p => p.server)) {
    // p_-префикс: id формально легален как зарезервированное слово JS
    // (class, default) — импорт с ним был бы SyntaxError на сборке.
    const identifier = `p_${plugin.id.replace(/-/g, '_')}`
    lines.push(`import ${identifier} from '${plugin.package}'`)
    entries.push(`  { id: '${plugin.id}', plugin: ${identifier} },`)
  }
  if (entries.length > 0) lines.push('')
  lines.push('export const edgePlugins: { id: string; plugin: unknown }[] = [')
  lines.push(...entries)
  lines.push(']')
  lines.push('')
  return lines.join('\n')
}

function renderClientRoster(plugins) {
  const entries = plugins.filter(p => p.client).map(p => ({ id: p.id, package: p.package }))
  return `${JSON.stringify(entries, null, 2)}\n`
}
