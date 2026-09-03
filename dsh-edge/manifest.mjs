/**
 * Единственное место правды о форме двух файлов dsh-edge:
 *
 *  - plugins.json        — установленные плагины (манифест морды);
 *  - plugins-catalog.json — доступные к ЗАКАЗУ (задача #113): что ещё можно
 *    заказать конвейеру #80. Каталог НЕ дублирует манифест: в нём нет
 *    package/source/sha256/flags — состав установленных читается только из
 *    манифеста, а «доступные» получаются вычитанием манифеста из каталога
 *    на клиенте (plugin-manager), не в данных.
 *
 * Читается кодогенератором (codegen-edge-plugins.mjs), гвардией состава
 * (verify-edge-plugins.mjs) и сборкой plugin-manager (build.mjs). Валидация
 * громкая: любая ошибка формы — throw до любых дорогих шагов сборки.
 * Существование релиза и совпадение sha256 здесь не проверяются (скрипт
 * офлайн) — их громко доказывает шаг скачивания релизного asset'а в
 * deploy-dsh-edge.yml, который обязан пройти до кодогенерации.
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

const CATALOG_VERSION = 1
const REPO_PATH_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9/._-]*$/

/**
 * Форма каталога доступных к заказу плагинов (dsh-edge/plugins-catalog.json).
 * id — тот же слаг, что в манифесте (по нему вычитание и заказ в журнал);
 * title/summary/brief — обязательные человекочитаемые поля (brief — готовая
 * формулировка заказа агенту); spec/sources — необязательные пути ВНУТРИ
 * репозитория: URL и абсолютные пути в каталоге не легальны по той же
 * причине, что и URL в манифесте (источник — только этот репозиторий).
 */
export function parseCatalog(source) {
  let catalog
  try {
    catalog = JSON.parse(source)
  } catch (error) {
    throw new Error(`dsh-edge/plugins-catalog.json is not valid JSON: ${error.message}`)
  }
  if (catalog === null || typeof catalog !== 'object' || Array.isArray(catalog)) {
    throw new Error('dsh-edge/plugins-catalog.json must be a JSON object.')
  }
  if (catalog.version !== CATALOG_VERSION) {
    throw new Error(`dsh-edge/plugins-catalog.json version must be ${CATALOG_VERSION}, found ${JSON.stringify(catalog.version)}.`)
  }
  if (!Array.isArray(catalog.plugins)) {
    throw new Error('dsh-edge/plugins-catalog.json must carry a plugins array.')
  }
  const seen = new Set()
  for (const entry of catalog.plugins) {
    const where = `plugin ${JSON.stringify(entry?.id ?? entry)}`
    failCatalogUnless(objectShape(entry), `${where} must be an object.`)
    failCatalogUnless(ID_PATTERN.test(entry.id), `${where}: id must match ${ID_PATTERN}.`)
    failCatalogUnless(!seen.has(entry.id), `duplicate catalog id "${entry.id}".`)
    seen.add(entry.id)
    for (const field of ['title', 'summary', 'brief']) {
      failCatalogUnless(typeof entry[field] === 'string' && entry[field].trim() !== '',
        `${where}: ${field} must be a non-empty string (it is shown in the morde and feeds the order text).`)
    }
    for (const field of ['spec', 'sources']) {
      if (entry[field] === undefined) continue
      failCatalogUnless(typeof entry[field] === 'string' && REPO_PATH_PATTERN.test(entry[field]) && !entry[field].includes('..'),
        `${where}: ${field} must be a repo-relative path without "..", not a URL or absolute path.`)
    }
  }
  return catalog
}

/** Читает и валидирует каталог заказа из каталога dsh-edge (рядом с манифестом). */
export async function loadCatalog(repoDir) {
  return parseCatalog(await readFile(join(repoDir, 'plugins-catalog.json'), 'utf8'))
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

function failCatalogUnless(condition, message) {
  if (!condition) throw new Error(`dsh-edge/plugins-catalog.json: ${message}`)
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
