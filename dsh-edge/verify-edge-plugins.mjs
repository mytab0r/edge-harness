/**
 * Гвардия состава: манифест ↔ собранный артефакт. Закрывает класс ошибки
 * «пайплайн зелёный — а плагина в деплое нет» (белое пятно #43).
 *
 * Для каждого enabled-плагина манифеста:
 *   - server: true → литерал состава `id:"<id>"` обязан быть в собранных
 *     воркер-бандлах обоих режимов (worker/direct/index.js,
 *     worker/isolated/index.js). Литерал приносит сгенерированный
 *     edge-plugins.generated.ts (патч 0002); сам импорт плагина резолвится
 *     алиасом патча 0001 и при неразрешении валит сборку, так что литерал в
 *     бандле доказывает: композиция собрана и код плагина внутри.
 *     Замечание против дизайна: bundle-meta.json, названный в design.md,
 *     эфемерен — bundle-standalone.mjs удаляет временную папку с метаданными;
 *     гвардия проверяет durability-артефакт того же доказательства.
 *   - client: true → собранный клиентский бандл обязан лежать в
 *     dist/plugins/<package>/client.js, а boot-граф в dist/index.html обязан
 *     содержать запись с id = имя пакета (то есть плагин попал в ростер
 *     морды, а не только в dist).
 *
 * Использование: node dsh-edge/verify-edge-plugins.mjs <clone-root>
 */

import { access, readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { loadManifest, manifestDirectory } from './manifest.mjs'

const cloneRoot = process.argv[2]
if (!cloneRoot) {
  process.stderr.write('Usage: node dsh-edge/verify-edge-plugins.mjs <clone-root>\n')
  process.exit(2)
}

const manifest = await loadManifest(manifestDirectory())
const standaloneRoot = join(cloneRoot, 'apps', 'dsh-edge', 'standalone')

for (const plugin of manifest.plugins) {
  if (plugin.server) await verifyServerComposition(plugin)
  if (plugin.client) await verifyClientComposition(plugin)
}

process.stdout.write(
  `edge-plugins: composition verified — ${manifest.plugins.length} plugin(s) from the manifest are present in the built artifacts.\n`,
)

async function verifyServerComposition(plugin) {
  for (const mode of ['direct', 'isolated']) {
    const worker = await readFile(join(standaloneRoot, 'worker', mode, 'index.js'), 'utf8')
    // Форма литерала состава: минифицированный esbuild-вывод двойными
    // кавычками (подтверждено живым бандлом апстрима: id:"deepseek-v4-flash").
    const marker = `id:"${plugin.id}"`
    if (!worker.includes(marker)) {
      throw new Error(
        `Composition guard: server plugin "${plugin.id}" (${plugin.package}) is missing from the ${mode} Worker bundle `
        + `(no "${marker}" composition literal). The deploy would ship without it — failing loudly instead.`,
      )
    }
  }
}

async function verifyClientComposition(plugin) {
  const bundle = join(standaloneRoot, 'dist', 'plugins', plugin.package, 'client.js')
  try {
    await access(bundle)
  } catch {
    throw new Error(
      `Composition guard: client plugin "${plugin.id}" (${plugin.package}) has no assembled web bundle at `
      + `dist/plugins/${plugin.package}/client.js. The web roster would boot without it — failing loudly instead.`,
    )
  }
  const index = await readFile(join(standaloneRoot, 'dist', 'index.html'), 'utf8')
  if (!index.includes(`"${plugin.package}"`)) {
    throw new Error(
      `Composition guard: client plugin "${plugin.id}" (${plugin.package}) is not in the boot graph of dist/index.html. `
      + 'The bundle would never be loaded by the web shell — failing loudly instead.',
    )
  }
}
