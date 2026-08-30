/**
 * Единственное место правды о форме dsh-edge/plugins.json.
 *
 * Читается кодогенератором (codegen-edge-plugins.mjs) и гвардией состава
 * (verify-edge-plugins.mjs). Валидация громкая: любая ошибка формы — throw
 * до любых дорогих шагов сборки. Существование релиза и совпадение sha256
 * здесь не проверяются (скрипт офлайн) — их громко доказывает шаг
 * скачивания релизного asset'а в deploy-dsh-edge.yml, который обязан
 * пройти до кодогенерации.
 */

import { readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const MANIFEST_VERSION = 1
export const PLUGIN_SCOPE = '@edge-harness'
const ID_PATTERN = /^[a-z][a-z0-9-]*$/
const PACKAGE_PATTERN = /^@edge-harness\/[a-z0-9][a-z0-9.-]*$/
const RELEASE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/
const SHA256_PATTERN = /^[0-9a-f]{64}$/

export function parseManifest(source) {
  let manifest
  try {
    manifest = JSON.parse(source)
  } catch (error) {
    throw new Error(`dsh-edge/plugins.json is not valid JSON: ${error.message}`)
  }
  if (manifest === null || typeof manifest !== 'object' || Array.isArray(manifest)) {
    throw new Error('dsh-edge/plugins.json must be a JSON object.')
  }
  if (manifest.version !== MANIFEST_VERSION) {
    throw new Error(`dsh-edge/plugins.json version must be ${MANIFEST_VERSION}, found ${JSON.stringify(manifest.version)}.`)
  }
  if (!Array.isArray(manifest.plugins)) {
    throw new Error('dsh-edge/plugins.json must carry a plugins array.')
  }
  const seen = new Set()
  for (const plugin of manifest.plugins) {
    const where = `plugin ${JSON.stringify(plugin?.id ?? plugin)}`
    failUnless(objectShape(plugin), `${where} must be an object.`)
    failUnless(ID_PATTERN.test(plugin.id), `${where}: id must match ${ID_PATTERN}.`)
    failUnless(!seen.has(plugin.id), `duplicate plugin id "${plugin.id}".`)
    seen.add(plugin.id)
    failUnless(PACKAGE_PATTERN.test(plugin.package), `${where}: package must be in the ${PLUGIN_SCOPE} scope and match ${PACKAGE_PATTERN} (plugins come only from this repo's releases, never URLs).`)
    failUnless(objectShape(plugin.source), `${where}: source must be an object { release, asset, sha256 }.`)
    failUnless(RELEASE_PATTERN.test(plugin.source.release) && !plugin.source.release.includes('//'),
      `${where}: source.release must be a release tag of this repo matching ${RELEASE_PATTERN}, not a URL.`)
    failUnless(RELEASE_PATTERN.test(plugin.source.asset) && plugin.source.asset.endsWith('.tgz'),
      `${where}: source.asset must be a release asset name ending in .tgz, not a path or URL.`)
    failUnless(SHA256_PATTERN.test(plugin.source.sha256),
      `${where}: source.sha256 must be 64 lowercase hex characters.`)
    failUnless(typeof plugin.server === 'boolean' && typeof plugin.client === 'boolean',
      `${where}: server and client must be booleans.`)
    failUnless(plugin.server || plugin.client, `${where}: at least one of server/client must be true.`)
  }
  return manifest
}

/** Читает и валидирует манифест из каталога репозитория (рядом с этим скриптом). */
export async function loadManifest(repoRoot) {
  return parseManifest(await readFile(join(repoRoot, 'plugins.json'), 'utf8'))
}

/** Каталог dsh-edge/, в котором лежит этот модуль. */
export function manifestDirectory() {
  return dirname(fileURLToPath(import.meta.url))
}

/** Пин апстрима из upstream.json — { repo, sha }. */
export async function loadUpstream(repoDir) {
  const upstream = JSON.parse(await readFile(join(repoDir, 'upstream.json'), 'utf8'))
  if (typeof upstream?.repo !== 'string' || upstream.repo === '') {
    throw new Error('dsh-edge/upstream.json: repo обязателен.')
  }
  if (!/^[0-9a-f]{40}$/.test(upstream.sha ?? '')) {
    throw new Error('dsh-edge/upstream.json: sha обязан быть 40 hex-символами (полный SHA коммита).')
  }
  return upstream
}

function objectShape(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function failUnless(condition, message) {
  if (!condition) throw new Error(`dsh-edge/plugins.json: ${message}`)
}

// CLI-режим: единственная точка валидации для workflow до любых дорогих шагов.
// Печатает проверенный состав манифеста и пин апстрима в stdout как JSON;
// любая ошибка формы — ненулевой exit БЕЗ частичного вывода.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const repoDir = manifestDirectory()
    const manifest = await loadManifest(repoDir)
    const upstream = await loadUpstream(repoDir)
    process.stdout.write(`${JSON.stringify({
      upstream,
      count: manifest.plugins.length,
      ids: manifest.plugins.map(p => p.id),
      plugins: manifest.plugins.map(p => ({
        id: p.id,
        package: p.package,
        release: p.source.release,
        asset: p.source.asset,
        sha256: p.source.sha256,
        server: p.server,
        client: p.client,
      })),
    }, null, 2)}\n`)
  } catch (error) {
    process.stderr.write(`::error::${error.message}\n`)
    process.exit(1)
  }
}
