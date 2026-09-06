/**
 * Канарейка коллизии имён с апстримом pawaca/dsh-edge (issue: сам этот change).
 *
 * ЗАЧЕМ: три раза одна и та же коллизия всплывала ПОСТФАКТУМ — красной сборкой
 * или жалобой владельца, — не проверкой:
 *  - неймспейс локали "settings.plugins" наш плагин-менеджер делил с
 *    апстримным пакетом @deepseek-ai/dsh-client-ui-settings-plugins →
 *    "locale namespace ... already has locale ..." при буте (issue #518,
 *    фикс — PR #544: неймспейс переехал на "settings.harnessPlugins");
 *  - видимая метка вкладки "Plugins" совпадала с апстримной, Playwright-локатор
 *    по тексту падал в strict mode (issue #547, фикс — PR #551: "Harness Plugins");
 *  - апстрим 0.10.0 убрал always-on пакет @deepseek-ai/dsh-client-runtime, а
 *    наш плагин продолжал объявлять его в dsh.client.inject — красный деплой
 *    (issue #518, класс дефекта из PR #521).
 *
 * Эта проверка читает РЕАЛЬНЫЙ апстримный артефакт на ТЕКУЩЕМ пине
 * (dsh-edge/upstream.json) — package.json пиновой @deepseek-ai/dsh-web-app,
 * npm-тарболы кандидатов, pnpm-lock.yaml апстримного standalone — и сверяет
 * его с тем, что регистрируют НАШИ клиентские плагины (plugins-src/*\/src/body.js).
 * Список апстримных кандидатов НЕ захардкожен: он вычисляется из реальных
 * зависимостей пиновой версии dsh-web-app на каждом запуске, поэтому не
 * протухает к следующему бампу пина сам по себе.
 *
 * Что проверяется (все четыре пункта задачи):
 *  1. Неймспейс локали каждого нашего плагина не занят ни одним апстримным
 *     кандидатом.
 *  2. Видимая label вкладки (nav) каждого нашего плагина не совпадает ни с
 *     одной label ни одного апстримного кандидата ни в одной локали.
 *  3. Пакеты из dsh.client.inject package.json каждого нашего плагина
 *     существуют в реальном pnpm-lock.yaml апстримного standalone на пине
 *     (класс дефекта PR #521).
 *  4. id/order слота settings.section каждого нашего плагина не совпадает с
 *     id/order ни одного апстримного кандидата.
 *
 * ЧЕСТНАЯ ГРАНИЦА (не маскируется): кандидаты апстримных «пакетов, что могут
 * коллизировать с разделом настроек» — ЭВРИСТИКА (прямые зависимости пиновой
 * dsh-web-app с именем /^@deepseek-ai\/dsh-client-ui-settings-/), не полный
 * скан всего дерева зависимостей апстрима (~90 пакетов — сетевая цена
 * несоразмерна). Эвристика покрывает ровно класс инцидентов #518/#547/#551
 * (пакеты, которые монтируют раздел в тот же слот "settings.section", что и
 * наши), но НЕ покрывает гипотетическую коллизию label/namespace с пакетом
 * вне этого имени (например, если апстрим когда-нибудь даст тому же тексту
 * другой, непохоже названный пакет). Список проверенных/непроверенных
 * кандидатов печатается в отчёт на каждом запуске — это не тихая неполнота.
 *
 * Использование: node dsh-edge/check-upstream-namespace-collision.mjs
 * (GH_TOKEN в окружении — опционально, поднимает лимит GitHub API).
 */

import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, existsSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = join(fileURLToPath(import.meta.url), '..', '..')
const GH_API = 'https://api.github.com'
const NPM_REGISTRY = 'https://registry.npmjs.org'

// Класс кандидатов: пакеты, которые могут монтировать свой собственный раздел
// в слот "settings.section" (тот же слот, что наши плагин-менеджер и
// интеграции) — см. «Честная граница» выше.
const CANDIDATE_NAME_PATTERN = /^@deepseek-ai\/dsh-client-ui-settings-.+/

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function ghHeaders() {
  const headers = {
    'user-agent': 'edge-harness-upstream-namespace-collision-check',
    accept: 'application/vnd.github.raw+json',
  }
  const token = process.env.GH_TOKEN || process.env.GITHUB_TOKEN
  if (token) headers.authorization = `Bearer ${token}`
  return headers
}

async function fetchGithubFile(repo, sha, path) {
  const url = `${GH_API}/repos/${repo}/contents/${encodeURIComponent(path).replace(/%2F/g, '/')}?ref=${sha}`
  const res = await fetch(url, { headers: ghHeaders() })
  if (!res.ok) {
    throw new Error(`GitHub API ${url} -> HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`)
  }
  return res.text()
}

async function npmPackageMeta(name) {
  // name приходит из внешних данных (ключ dependencies апстримного пакета на
  // npm registry) — кодируем ПОЛНОСТЬЮ (encodeURIComponent), затем возвращаем
  // читаемыми только "@" и "/" (npm registry принимает обе формы для scoped-
  // имени). Замена ГЛОБАЛЬНАЯ (/g): строковый .replace(str, ...) заменяет
  // только первое вхождение и оставляет второе "%2F"/"%40" percent-encoded
  // внутри URL для имени с более чем одним "@"/"/" (CodeQL js/incomplete-
  // sanitization, было здесь — та же схема, что уже верно сделана в
  // fetchGithubFile выше, строка 80).
  const res = await fetch(`${NPM_REGISTRY}/${encodeURIComponent(name).replace(/%40/g, '@').replace(/%2F/g, '/')}`)
  if (!res.ok) throw new Error(`npm registry ${name} -> HTTP ${res.status}`)
  return res.json()
}

/** Диапазон вида "^0.1.2-rc.1" или "0.1.2-rc.1" -> точную версию. Резолвер
 * простой (обрезка префикса ^/~) и намеренно не понимает произвольный семвер-
 * диапазон: этот монорепозиторий релизит все свои пакеты в лок-шаге одной и
 * той же prerelease-версией (проверено на пине этого коммита), и любое другое
 * значение обязано упасть громко, а не тихо выбрать не ту версию. */
function stripRangePrefix(range) {
  return range.replace(/^[\^~]/, '')
}

/** Скачивает npm-тарбол и распаковывает его системным `tar` (тот же приём,
 * что и check-plugin-compat.mjs использует для npm pack --dry-run) — не
 * пишем свой парсер gzip+tar ради разбора четырёх пакетов на прогон. */
async function downloadAndExtractTarball(tarballUrl, destDir) {
  const res = await fetch(tarballUrl)
  if (!res.ok) throw new Error(`tarball ${tarballUrl} -> HTTP ${res.status}`)
  const buf = Buffer.from(await res.arrayBuffer())
  const tgzPath = join(destDir, 'pkg.tgz')
  writeFileSync(tgzPath, buf)
  // --force-local: без него GNU tar на Windows иногда читает путь с буквой
  // диска как "host:path" удалённого архива (git-bash tar, наблюдалось
  // локально) — флаг безвреден на обычном Linux tar (ubuntu-latest CI).
  const result = spawnSync('tar', ['xzf', tgzPath, '-C', destDir, '--force-local'], { encoding: 'utf8' })
  if (result.status !== 0) {
    throw new Error(`tar xzf ${tgzPath} не распаковался: ${result.stderr || result.error?.message}`)
  }
  return join(destDir, 'package')
}

// ── Извлечение реальных фактов из исходника (нашего body.js ИЛИ бандла
// апстримного lib/client.js — один и тот же набор regex-извлекателей для
// обеих сторон: сравниваем то, что реально нашли, а не то, что предположили). ──

/** Неймспейсы, зарегистрированные через ctx.locale.register(...). Литеральная
 * форма (наш body.js) читается напрямую; форма через переменную (минифицированный
 * client.js апстрима — `const NS = "..."; ctx.locale.register(NS, ...)`)
 * резолвится по присваиванию той же переменной в файле. */
function extractLocaleNamespaces(source) {
  const namespaces = new Set()
  for (const m of source.matchAll(/\.locale\.register\(\s*"([^"]+)"/g)) namespaces.add(m[1])
  for (const m of source.matchAll(/\.locale\.register\(\s*([A-Za-z_$][\w$]*)\s*,/g)) {
    const ident = m[1]
    const assign = new RegExp(`\\b(?:const|let|var)\\s+${ident}\\s*=\\s*"([^"]*)"`).exec(source)
    if (assign) namespaces.add(assign[1])
  }
  return [...namespaces]
}

/** Все значения ключа `nav:` в словарях локалей — видимая label вкладки. */
function extractNavLabels(source) {
  const labels = new Set()
  for (const m of source.matchAll(/\bnav:\s*"((?:\\.|[^"\\])*)"/g)) labels.add(m[1])
  return [...labels]
}

/** id/order слота "settings.section": окно в 80 символов после поля name
 * покрывает и наш форматированный body.js, и минифицированный client.js
 * апстрима (проверено на живых тарболах dsh-client-ui-settings-plugins,
 * dsh-client-ui-settings-models, dsh-client-ui-settings-general). */
function extractSettingsSectionSlots(source) {
  const slots = []
  const pattern = /name:\s*"settings\.section"[\s\S]{0,80}?id:\s*"([^"]+)"[\s\S]{0,80}?order:\s*(-?\d+)/g
  for (const m of source.matchAll(pattern)) slots.push({ id: m[1], order: Number(m[2]) })
  return slots
}

/** Пакет упомянут в pnpm-lock.yaml — по имени с последующим `'` (форма
 * importers "'@scope/name':") или `@` (форма packages "'@scope/name@1.2.3':"). */
function lockHasPackage(lockText, pkgName) {
  return new RegExp(`${escapeRegExp(pkgName)}['@]`).test(lockText)
}

function collectOurPlugins() {
  const pluginsDir = join(repoRoot, 'plugins-src')
  const result = []
  for (const name of readdirSync(pluginsDir)) {
    const bodyPath = join(pluginsDir, name, 'src', 'body.js')
    if (!existsSync(bodyPath)) continue // серверные-только плагины (hello-world/server, runner-bridge) не участвуют
    const source = readFileSync(bodyPath, 'utf8')
    const pkgPath = join(pluginsDir, name, 'package.json')
    const pkg = existsSync(pkgPath) ? JSON.parse(readFileSync(pkgPath, 'utf8')) : {}
    result.push({
      name,
      namespaces: extractLocaleNamespaces(source),
      navLabels: extractNavLabels(source),
      slots: extractSettingsSectionSlots(source),
      inject: pkg?.dsh?.client?.inject ?? [],
    })
  }
  return result
}

async function collectUpstreamCandidates(repo, sha, tmpRoot) {
  const standalonePkg = JSON.parse(
    await fetchGithubFile(repo, sha, 'apps/dsh-edge/standalone/package.json'))
  const webAppRange = standalonePkg.dependencies?.['@deepseek-ai/dsh-web-app']
  if (!webAppRange) {
    throw new Error('apps/dsh-edge/standalone/package.json (апстрим, пин): нет зависимости @deepseek-ai/dsh-web-app — упрощение резолвера кандидатов сломано')
  }
  const webAppVersion = stripRangePrefix(webAppRange)
  const webAppMeta = await npmPackageMeta('@deepseek-ai/dsh-web-app')
  const webAppVerMeta = webAppMeta.versions?.[webAppVersion]
  if (!webAppVerMeta) {
    throw new Error(`npm @deepseek-ai/dsh-web-app@${webAppVersion} (из пина ${repo}@${sha}) не найден в registry — версия ещё не опубликована или резолвер диапазона ошибся`)
  }

  const candidateNames = Object.keys(webAppVerMeta.dependencies || {})
    .filter((n) => CANDIDATE_NAME_PATTERN.test(n))
    .sort()

  const candidates = []
  const skipped = []
  for (const candName of candidateNames) {
    const range = webAppVerMeta.dependencies[candName]
    const version = stripRangePrefix(range)
    const candMeta = await npmPackageMeta(candName)
    const candVerMeta = candMeta.versions?.[version]
    if (!candVerMeta) {
      throw new Error(`npm ${candName}@${version} (диапазон ${range} из dsh-web-app@${webAppVersion}) не найден в registry`)
    }
    const tarballUrl = candVerMeta.dist?.tarball
    if (!tarballUrl) throw new Error(`npm ${candName}@${version}: в метаданных нет dist.tarball`)
    const destDir = mkdtempSync(join(tmpRoot, 'pkg-'))
    const pkgDir = await downloadAndExtractTarball(tarballUrl, destDir)
    const localPkg = JSON.parse(readFileSync(join(pkgDir, 'package.json'), 'utf8'))
    if (localPkg?.dsh?.client?.platform !== 'web') {
      skipped.push(`${candName}@${version}: не клиентский плагин (dsh.client.platform !== "web") — пропущен`)
      continue
    }
    const clientExport = localPkg.exports?.['./client']
    const clientRel = typeof clientExport === 'string' ? clientExport : clientExport?.default
    if (!clientRel) {
      skipped.push(`${candName}@${version}: package.json#exports['./client'] отсутствует или не строка — пропущен, форма непроверяема`)
      continue
    }
    const clientPath = join(pkgDir, clientRel)
    if (!existsSync(clientPath)) {
      skipped.push(`${candName}@${version}: ${clientRel} не найден в тарболе — пропущен`)
      continue
    }
    const source = readFileSync(clientPath, 'utf8')
    candidates.push({
      name: candName,
      version,
      namespaces: extractLocaleNamespaces(source),
      navLabels: extractNavLabels(source),
      slots: extractSettingsSectionSlots(source),
    })
  }

  return { candidates, skipped, webAppVersion }
}

async function main() {
  const upstream = JSON.parse(readFileSync(join(repoRoot, 'dsh-edge', 'upstream.json'), 'utf8'))
  const { repo, sha } = upstream
  console.log(`upstream-namespace-collision: пин ${repo}@${sha}`)

  const ourPlugins = collectOurPlugins()
  if (ourPlugins.length === 0) throw new Error('plugins-src/*/src/body.js: ни одного клиентского плагина не найдено — проверять нечего')
  for (const p of ourPlugins) {
    console.log(`  наш плагин ${p.name}: namespaces=${JSON.stringify(p.namespaces)} nav=${JSON.stringify(p.navLabels)} slots=${JSON.stringify(p.slots)} inject=${JSON.stringify(p.inject)}`)
  }

  const tmpRoot = mkdtempSync(join(tmpdir(), 'upstream-collision-'))
  const { candidates, skipped, webAppVersion } = await collectUpstreamCandidates(repo, sha, tmpRoot)
  console.log(`  апстримная @deepseek-ai/dsh-web-app@${webAppVersion}: ${candidates.length} кандидат(ов) settings-раздела проверено, ${skipped.length} пропущено`)
  for (const c of candidates) {
    console.log(`  апстрим ${c.name}@${c.version}: namespaces=${JSON.stringify(c.namespaces)} nav=${JSON.stringify(c.navLabels)} slots=${JSON.stringify(c.slots)}`)
  }
  for (const s of skipped) console.log(`  ⚠️ ${s}`)

  const standaloneLock = await fetchGithubFile(repo, sha, 'apps/dsh-edge/standalone/pnpm-lock.yaml')

  const violations = []

  // Пункты 1/2/4: наш плагин против каждого апстримного кандидата.
  for (const p of ourPlugins) {
    for (const c of candidates) {
      for (const ns of p.namespaces) {
        if (c.namespaces.includes(ns)) {
          violations.push(`неймспейс локали "${ns}" нашего плагина ${p.name} совпал с апстримным ${c.name}@${c.version} (issue #518)`)
        }
      }
      for (const label of p.navLabels) {
        if (c.navLabels.includes(label)) {
          violations.push(`видимая label "${label}" нашего плагина ${p.name} совпала с апстримной label ${c.name}@${c.version} (issue #547)`)
        }
      }
      for (const slot of p.slots) {
        for (const candSlot of c.slots) {
          if (slot.id === candSlot.id) {
            violations.push(`id слота settings.section "${slot.id}" нашего плагина ${p.name} совпал с апстримным ${c.name}@${c.version}`)
          }
          if (slot.order === candSlot.order) {
            violations.push(`order слота settings.section ${slot.order} нашего плагина ${p.name} (id "${slot.id}") совпал с апстримным ${c.name}@${c.version} (id "${candSlot.id}")`)
          }
        }
      }
    }
  }

  // Пункт 3: dsh.client.inject против реального pnpm-lock.yaml апстрима.
  const injected = new Map()
  for (const p of ourPlugins) {
    for (const pkgName of p.inject) {
      if (!injected.has(pkgName)) injected.set(pkgName, [])
      injected.get(pkgName).push(p.name)
    }
  }
  for (const [pkgName, owners] of injected) {
    if (!lockHasPackage(standaloneLock, pkgName)) {
      violations.push(`пакет "${pkgName}" из dsh.client.inject плагина(ов) ${owners.join(', ')} не найден в apps/dsh-edge/standalone/pnpm-lock.yaml апстрима на пине ${sha} — класс дефекта PR #521`)
    }
  }

  if (violations.length > 0) {
    console.error('upstream-namespace-collision: КРАСНЫЙ — коллизия(и) с апстримом:')
    for (const v of violations) console.error(`  - ${v}`)
    process.exit(1)
  }

  console.log('upstream-namespace-collision: ЗЕЛЁНЫЙ — коллизий не найдено (см. «Честная граница» в докстринге файла о непокрытом)')
}

main().catch((err) => {
  console.error(`upstream-namespace-collision: ОШИБКА — ${err.message}`)
  process.exit(1)
})
