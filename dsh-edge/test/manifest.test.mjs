/**
 * Юнит-гвардия подстановки форжа в манифест (FORGE_EXTRA_PLUGIN, #77).
 *
 * Класс «повторный форж существующего плагина» (ревью PR #162): подстановка
 * обязана быть апсертом — новый id добавляется, существующий ЗАМЕНЯЕТСЯ на
 * месте (бамп версии — главный повторяющийся сценарий форжа, а дым всегда
 * подставляет FORGE_EXTRA_PLUGIN; жёсткий запрет на существующий id красил бы
 * любой повторный прогон). Снятие любой проверки здесь красит
 * `node --test dsh-edge/test/manifest.test.mjs` в repo-ci.
 *
 * Тест кормится прод-формой: читается НАСТОЯЩИЙ dsh-edge/plugins.json —
 * подстановка живёт поверх него в памяти, файл не трогается (проверяется).
 *
 * Запуск: node --test dsh-edge/test/manifest.test.mjs
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { loadManifestWithForgeExtra, parseManifest } from '../manifest.mjs'

const dshEdgeDir = join(fileURLToPath(import.meta.url), '..', '..') // dsh-edge/

/** Прогон с подстановкой форжа и гарантированным восстановлением env. */
async function withForgeExtra(raw, fn) {
  const previous = process.env.FORGE_EXTRA_PLUGIN
  process.env.FORGE_EXTRA_PLUGIN = raw
  try {
    return await fn()
  } finally {
    if (previous === undefined) delete process.env.FORGE_EXTRA_PLUGIN
    else process.env.FORGE_EXTRA_PLUGIN = previous
  }
}

const NEW_PLUGIN = JSON.stringify({
  id: 'forge-fixture',
  package: '@edge-harness/dsh-plugin-forge-fixture',
  server: true,
  client: false,
})

test('без FORGE_EXTRA_PLUGIN манифест возвращается как прочитан', async () => {
  delete process.env.FORGE_EXTRA_PLUGIN
  const manifest = await loadManifestWithForgeExtra(dshEdgeDir)
  const raw = JSON.parse(await readFile(join(dshEdgeDir, 'plugins.json'), 'utf8'))
  assert.deepEqual(manifest.plugins.map((p) => p.id), raw.plugins.map((p) => p.id))
})

test('новый id добавляется в конец, файл на диске не меняется', async () => {
  const before = await readFile(join(dshEdgeDir, 'plugins.json'), 'utf8')
  const manifest = await withForgeExtra(NEW_PLUGIN, () => loadManifestWithForgeExtra(dshEdgeDir))
  const raw = JSON.parse(before)
  assert.equal(manifest.plugins.length, raw.plugins.length + 1)
  assert.equal(manifest.plugins.at(-1).id, 'forge-fixture')
  // остальной состав не тронут
  assert.deepEqual(manifest.plugins.slice(0, -1).map((p) => p.id), raw.plugins.map((p) => p.id))
  assert.equal(await readFile(join(dshEdgeDir, 'plugins.json'), 'utf8'), before)
})

test('существующий id ЗАМЕНЯЕТСЯ на месте — повторный форж (бамп версии) работает', async () => {
  // Прод-форма повторного форжа: id и пакет из манифеста, флаги из --detect.
  const manifest = await withForgeExtra(
    JSON.stringify({ id: 'hello', package: '@edge-harness/dsh-plugin-hello', server: true, client: true }),
    () => loadManifestWithForgeExtra(dshEdgeDir),
  )
  const ids = manifest.plugins.map((p) => p.id)
  assert.equal(ids.filter((id) => id === 'hello').length, 1, 'дубликат id в подставленном манифесте')
  // позиция сохранена: порядок манифеста = порядок инсталла
  assert.equal(ids[0], 'hello')
  // запись — заглушка форжа (source подменён, всё прочее — форма манифеста)
  assert.equal(manifest.plugins[0].source.release, 'forge-smoke-placeholder')
  assert.equal(manifest.plugins[0].package, '@edge-harness/dsh-plugin-hello')
  // остальной состав не тронут
  assert.ok(manifest.plugins.some((p) => p.id === 'runner-bridge'))
  assert.ok(manifest.plugins.some((p) => p.id === 'plugin-manager'))
})

test('смена пакета под существующим id — громкая ошибка, не тихая подмена', async () => {
  await assert.rejects(
    withForgeExtra(
      JSON.stringify({ id: 'hello', package: '@edge-harness/dsh-plugin-someone-else', server: true, client: true }),
      () => loadManifestWithForgeExtra(dshEdgeDir),
    ),
    /already declared with package @edge-harness\/dsh-plugin-hello/,
  )
})

test('битый JSON, кривой id, не-boolean и пустые флаги — громкие ошибки формы', async () => {
  await assert.rejects(
    withForgeExtra('{not json', () => loadManifestWithForgeExtra(dshEdgeDir)),
    /FORGE_EXTRA_PLUGIN is not valid JSON/,
  )
  await assert.rejects(
    withForgeExtra(
      JSON.stringify({ id: 'Bad_Id', package: '@edge-harness/dsh-plugin-x', server: true, client: false }),
      () => loadManifestWithForgeExtra(dshEdgeDir),
    ),
    /id must match/,
  )
  await assert.rejects(
    withForgeExtra(
      JSON.stringify({ id: 'x', package: '@edge-harness/dsh-plugin-x', server: 'yes', client: false }),
      () => loadManifestWithForgeExtra(dshEdgeDir),
    ),
    /must be booleans/,
  )
  await assert.rejects(
    withForgeExtra(
      JSON.stringify({ id: 'x', package: '@edge-harness/dsh-plugin-x', server: false, client: false }),
      () => loadManifestWithForgeExtra(dshEdgeDir),
    ),
    /at least one of server\/client/,
  )
})

test('заглушка source проходит parseManifest — подставленный манифест валиден для прод-гейта', async () => {
  const manifest = await withForgeExtra(NEW_PLUGIN, () => loadManifestWithForgeExtra(dshEdgeDir))
  // Ровно тот гейт, что гоняет CLI manifest.mjs в деплое и дыме: подставленный
  // состав обязан проходить его целиком, а не только «на глаз».
  const reparsed = parseManifest(JSON.stringify(manifest))
  assert.equal(reparsed.version, 1)
  assert.ok(reparsed.plugins.some((p) => p.id === 'forge-fixture'))
})
