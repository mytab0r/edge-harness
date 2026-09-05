/**
 * Юнит-гвардия чекера совместимости плагинов (plugin-forge, #77).
 *
 * Три контракта:
 *  1. sumBundleContribution — прод-форма esbuild-метафайла (то, что пишет
 *     wrangler --metafile в пробной сборке, форма как у апстримного
 *     reportLargestInputs): вклад пакета = сумма bytesInOutput его входов по
 *     всем выводам, ничего лишнего.
 *  2. detectServerClient — конвенция «server/client» сверяется с манифестом
 *     ВСЕХ реальных плагинов репо (plugins-src ↔ dsh-edge/plugins.json):
 *     расхождение детекта и установленной правды = красный тест, а не тихий
 *     клиент-only плагин, собранный с серверным составом (и наоборот).
 *  3. resolveClientEntry — точка входа клиентского бандла из
 *     exports['./client']: строка проходит насквозь, условные экспорты
 *     разрешаются в порядке апстримного exportPath (worker → browser →
 *     import → default), чужой порядок или не-.js — ловится здесь.
 *
 * Запуск: node --test dsh-edge/test/check-plugin-compat.test.mjs
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

import { detectServerClient, resolveClientEntry, sumBundleContribution } from '../check-plugin-compat.mjs'

const dshEdgeDir = join(fileURLToPath(import.meta.url), '..', '..') // dsh-edge/
const repoRoot = join(dshEdgeDir, '..')
const checker = join(dshEdgeDir, 'check-plugin-compat.mjs')

// ── 1. sumBundleContribution: прод-форма esbuild-метафайла ────────────────────

const HELLO = '@edge-harness/dsh-plugin-hello'

// Форма вывода `wrangler deploy --dry-run --metafile` (esbuild metafile):
// outputs → { вход: { bytesInOutput } }; входы плагина в pnpm-клоне лежат
// под …/node_modules/<package>/…, включая сегмент .pnpm с «+» вместо «/».
const REALISTIC_METAFILE = {
  inputs: {
    'src/index.ts': { bytes: 3210 },
    'node_modules/.pnpm/cordis@0.0.71/node_modules/cordis/lib/index.js': { bytes: 12000 },
  },
  outputs: {
    'worker/direct/index.js': {
      imports: [],
      inputs: {
        'src/session-store.ts': { bytesInOutput: 11000 },
        'node_modules/.pnpm/cordis@0.0.71/node_modules/cordis/lib/index.js': { bytesInOutput: 9000 },
        'node_modules/.pnpm/@edge-harness+dsh-plugin-hello@0.1.1/node_modules/@edge-harness/dsh-plugin-hello/src/index.ts': { bytesInOutput: 456 },
        'node_modules/.pnpm/@edge-harness+dsh-plugin-hello@0.1.1/node_modules/@edge-harness/dsh-plugin-hello/client/client.js': { bytesInOutput: 120 },
      },
      bytes: 22000,
    },
    'worker/direct/index.js.map': {
      imports: [],
      inputs: {},
      bytes: 100,
    },
  },
}

test('вклад пакета — сумма bytesInOutput его входов по всем выводам', () => {
  assert.equal(sumBundleContribution(REALISTIC_METAFILE, HELLO), 456 + 120)
})

test('чужие входы и отсутствующий пакет не дают ложного вклада', () => {
  assert.equal(sumBundleContribution(REALISTIC_METAFILE, '@edge-harness/dsh-plugin-runner-bridge'), 0)
  assert.equal(sumBundleContribution({ outputs: {} }, HELLO), 0)
  // Пакет-префикс другого плагина не матчится частичным именем.
  assert.equal(
    sumBundleContribution({
      outputs: {
        'index.js': {
          inputs: {
            [`node_modules/${HELLO}-extra/src/index.js`]: { bytesInOutput: 999 },
            [`node_modules/${HELLO}/src/index.js`]: { bytesInOutput: 7 },
          },
        },
      },
    }, HELLO),
    7,
  )
})

// ── 2. detectServerClient ↔ манифест реальных плагинов ────────────────────────

test('детект server/client совпадает с манифестом для каждого реального плагина', async () => {
  const manifest = JSON.parse(await readFile(join(dshEdgeDir, 'plugins.json'), 'utf8'))
  const byPackage = new Map()
  for (const dir of await readdir(join(repoRoot, 'plugins-src'), { withFileTypes: true })) {
    if (!dir.isDirectory()) continue
    const pkg = JSON.parse(await readFile(join(repoRoot, 'plugins-src', dir.name, 'package.json'), 'utf8'))
    byPackage.set(pkg.name, { dir: dir.name, pkg })
  }

  // Каждый манифестный плагин обязан иметь исходники (иначе сверять нечего —
  // а исходники без манифеста допустимы: плагин в разработке).
  const missing = manifest.plugins.filter(p => !byPackage.has(p.package))
  assert.deepEqual(missing, [], 'манифестные плагины без исходников в plugins-src — сверка детекта неполна')

  for (const plugin of manifest.plugins) {
    const { dir, pkg } = byPackage.get(plugin.package)
    assert.deepEqual(
      detectServerClient(pkg),
      { server: plugin.server, client: plugin.client },
      `детект ${dir} расходится с манифестной записью ${plugin.id}`,
    )
  }
})

// ── 3. resolveClientEntry: exports['./client'] → путь бандла ──────────────────

test('строковый экспорт проходит насквозь, отсутствующий даёт undefined', () => {
  assert.equal(resolveClientEntry('client/client.js'), 'client/client.js')
  assert.equal(resolveClientEntry(undefined), undefined)
  assert.equal(resolveClientEntry(null), undefined)
})

test('условные экспорты разрешаются в порядке worker → browser → import → default', () => {
  // Апстримный exportPath: первый определённый ключ выигрывает, порядок
  // ключей в самом объекте не важен — важен порядок ПОИСКА.
  assert.equal(resolveClientEntry({ import: 'a/import.js', default: 'a/default.js' }), 'a/import.js')
  assert.equal(resolveClientEntry({ default: 'a/default.js' }), 'a/default.js')
  assert.equal(resolveClientEntry({ worker: 'a/worker.js', browser: 'a/browser.js', default: 'a/default.js' }), 'a/worker.js')
  // Пустой объект условий — не строка: undefined (ошибку формы даёт чекер).
  assert.equal(resolveClientEntry({}), undefined)
})

// ── 4. CLI: флаг без пары — громкий отказ, не тихий пропуск замера ────────────

test('--bundle-meta без --package не проходит молча (exit 2)', () => {
  // Мутационный свидетель: снять гвардию — чекер уходит в main(), шаг 8
  // молча не выполняется (bundleMetaPath undefined) и прогон «ЗЕЛЁНЫЙ»
  // без проверки размера вклада.
  for (const argv of [[checker, 'plugins-src/hello-world', '--bundle-meta']]) {
    const r = spawnSync(process.execPath, argv, { encoding: 'utf8', cwd: repoRoot })
    assert.equal(r.status, 2, `ожидался exit 2 для ${argv.slice(1).join(' ')}, получено ${r.status}`)
    assert.match(r.stderr, /--bundle-meta требует пару/)
  }
})
