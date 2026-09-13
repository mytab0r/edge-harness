#!/usr/bin/env node
/**
 * Гвардия консистентности peer-зависимостей в pnpm-lock.yaml собранного
 * standalone-дерева (issue #1041). Повод: e2e-смоук `settings.section` упал
 * `Minified React error #130` («Element type is invalid») на 7-й вкладке
 * Settings («DSH Edge») — расследование нашло в апстримном
 * `apps/dsh-edge/standalone/pnpm-lock.yaml` (пин из dsh-edge/upstream.json)
 * запись `react-dom@19.2.8(react@18.3.1)`: react-dom@19.2.8 объявляет
 * `peerDependencies: { react: "^19.2.8" }`, но pnpm фактически подставил
 * react@18.3.1 — версию, которая заведомо НЕ входит в этот диапазон (другой
 * MAJOR). pnpm по умолчанию не роняет установку на таком расхождении (только
 * предупреждает), поэтому несовместимая пара тихо доезжает до собранного
 * бандла и падает только на реальном браузерном рендере конкретного слота —
 * далеко от места и момента, где решение было принято.
 *
 * Что проверяет: КАЖДУЮ запись `packages:` с непустым `peerDependencies`
 * сверяет с тем, какой MAJOR версии peer'а pnpm реально подставил в
 * `snapshots:` (peer-контекст в скобках после версии) — не только react/
 * react-dom, любой пакет. Опциональные peer'ы (`peerDependenciesMeta.
 * <name>.optional: true`) не требуют присутствия в контексте.
 *
 * Порог — расхождение MAJOR-версии, не любое несовпадение диапазона: живой
 * прогон этой же гвардии на пине #1041 нашёл, ПОМИМО react-dom/react, восемь
 * пар вида `@deepseek-ai/cordis-plugin-loader@1.0.2` при требовании `^1.0.3`
 * — то же расхождение для КАЖДОГО прежнего (годами зелёного) прогона на этом
 * пине: не регрессия, а давно принятое отставание патч/минор в одном major,
 * где API стабилен по смыслу semver. Фейлить билд на них — шум без сигнала.
 * React error #130 — про несовместимость MAJOR (это и есть breaking change
 * по контракту semver) — только она роняет сборку.
 *
 * Разбор — свой минимальный (не полноценный YAML/semver): pnpm-lock.yaml
 * пишет сам pnpm в СТРОГО регулярной форме (не рукописный YAML), а полный
 * семвер-диапазон здесь не нужен — только «какой MAJOR допускает диапазон».
 * Stdlib-first (тот же принцип, что у всех остальных check.mjs этого
 * репозитория): внешний `yaml`/`semver` резолвился бы из node_modules
 * КЛОНА апстрима (транзитивная зависимость pnpm) и был бы недоступен для
 * юнит-теста, который не клонирует апстрим — собственный парсер тестируем
 * без внешнего состояния.
 *
 * Использование: node dsh-edge/verify-peer-consistency.mjs <clone-dir>
 *   clone-dir — $GITHUB_WORKSPACE/clone (апстрим на пине, standalone уже
 *   собран `pnpm add`). Читает clone/apps/dsh-edge/standalone/pnpm-lock.yaml.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

// ── Разбор pnpm-lock.yaml ────────────────────────────────────────────────

/**
 * Возвращает:
 *   packages — { [name@version]: { peerDependencies: {name: range},
 *                                   peerDependenciesMeta: {name: {optional}} } }
 *   snapshotKeys — string[] всех ключей верхнего уровня секции `snapshots:`
 *
 * Формат pnpm-lock.yaml (lockfileVersion 9, пишет сам pnpm): секции верхнего
 * уровня начинаются с колонки 0 (`packages:`, `snapshots:`, ...); внутри
 * секции запись пакета — строка с ДВУХпробельным отступом, оканчивающаяся
 * на `:` (ключ в кавычках или без, если не требует экранирования);
 * содержимое записи — строки с отступом ≥4 пробелов, пока не встретится
 * следующая 2-пробельная строка или конец секции (строка с отступом 0).
 */
function parseLock(text) {
  const lines = text.split(/\r?\n/)
  const packages = {}
  const snapshotKeys = []

  let section = null // 'packages' | 'snapshots' | null (прочее/корень)
  let currentKey = null // текущий разбираемый packages: ключ
  let inPeerDeps = false
  let inPeerDepsMeta = false
  let peerDepsMetaCurrentName = null

  for (const rawLine of lines) {
    if (rawLine.trim() === '' || rawLine.trimStart().startsWith('#')) continue
    const indent = rawLine.length - rawLine.trimStart().length
    const line = rawLine.trimEnd()

    if (indent === 0) {
      const topMatch = /^([A-Za-z_][\w-]*):/.exec(line)
      section = topMatch ? topMatch[1] : null
      currentKey = null
      inPeerDeps = false
      inPeerDepsMeta = false
      peerDepsMetaCurrentName = null
      continue
    }

    if (section === 'packages') {
      if (indent === 2) {
        const keyMatch = /^ {2}(['"]?)(.+?)\1:\s*$/.exec(line)
        if (keyMatch) {
          currentKey = keyMatch[2]
          packages[currentKey] = { peerDependencies: {}, peerDependenciesMeta: {} }
          inPeerDeps = false
          inPeerDepsMeta = false
          peerDepsMetaCurrentName = null
        }
        continue
      }
      if (!currentKey) continue
      if (indent === 4) {
        inPeerDeps = /^ {4}peerDependencies:\s*$/.test(line)
        inPeerDepsMeta = /^ {4}peerDependenciesMeta:\s*$/.test(line)
        peerDepsMetaCurrentName = null
        continue
      }
      if (indent === 6 && inPeerDeps) {
        const peerMatch = /^ {6}(['"]?)([^:'"]+)\1:\s*(.+?)\s*$/.exec(line)
        if (peerMatch) packages[currentKey].peerDependencies[peerMatch[2]] = stripQuotes(peerMatch[3])
        continue
      }
      if (indent === 6 && inPeerDepsMeta) {
        const nameMatch = /^ {6}(['"]?)([^:'"]+)\1:\s*$/.exec(line)
        if (nameMatch) peerDepsMetaCurrentName = nameMatch[2]
        continue
      }
      if (indent === 8 && inPeerDepsMeta && peerDepsMetaCurrentName) {
        const optMatch = /^ {8}optional:\s*(true|false)\s*$/.exec(line)
        if (optMatch) {
          packages[currentKey].peerDependenciesMeta[peerDepsMetaCurrentName] = { optional: optMatch[1] === 'true' }
        }
        continue
      }
      continue
    }

    if (section === 'snapshots') {
      if (indent === 2) {
        const keyMatch = /^ {2}(['"]?)(.+?)\1:\s*(\{\})?\s*$/.exec(line)
        if (keyMatch) snapshotKeys.push(keyMatch[2])
      }
    }
  }

  return { packages, snapshotKeys }
}

function stripQuotes(text) {
  if ((text.startsWith("'") && text.endsWith("'")) || (text.startsWith('"') && text.endsWith('"'))) {
    return text.slice(1, -1)
  }
  return text
}

// ── Разбор ключей snapshot: name@version(peer1@v1)(peer2@v2(sub@v)) ────────

/** npm-имя пакета (простое или @scope/name) — без этого фильтра совпадает мусор. */
const PACKAGE_NAME_RE = /^(@[a-z0-9][\w.-]*\/)?[a-z0-9][\w.-]*$/i

/**
 * `@scope/name@1.2.3` (возможно с собственным вложенным `(...)`, который
 * отбрасывается) → {name, version} либо null, если это не пара name@version
 * — pnpm схлопывает часто повторяющиеся peer-комбинации в анонимные
 * content-hash идентификаторы (32-hex или `patch_hash=...`, без единого
 * валидного имени пакета перед последним '@') — разобрать их без повторной
 * реализации алгоритма резолвинга pnpm нельзя, такие группы пропускаются.
 */
function splitNameVersion(rawText) {
  const ownParen = rawText.indexOf('(')
  const text = ownParen === -1 ? rawText : rawText.slice(0, ownParen)
  const at = text.lastIndexOf('@')
  if (at <= 0) return null // '@' на позиции 0 — сам scope-маркер, не разделитель версии
  const name = text.slice(0, at)
  const version = text.slice(at + 1)
  if (!PACKAGE_NAME_RE.test(name)) return null
  return { name, version }
}

/** Содержимое каждой сбалансированной верхнеуровневой группы `(...)` в строке. */
function splitTopLevelParens(text) {
  const groups = []
  let depth = 0
  let start = -1
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '(') {
      if (depth === 0) start = i + 1
      depth++
    } else if (text[i] === ')') {
      depth--
      if (depth === 0) groups.push(text.slice(start, i))
    }
  }
  return groups
}

/** Верхнеуровневые peer-пары {name, version} ключа снимка (после name@version головы). */
function peersOfSnapshotKey(key) {
  const parenIndex = key.indexOf('(')
  if (parenIndex === -1) return []
  const peers = []
  for (const group of splitTopLevelParens(key.slice(parenIndex))) {
    const parsed = splitNameVersion(group)
    if (parsed) peers.push(parsed)
  }
  return peers
}

// ── Минимальный семвер: только «какой(ие) MAJOR допускает диапазон» ────────

/** Ведущее целое до первой точки — сам MAJOR версии ("18.3.1" → 18, "0.1.2-rc.1" → 0). */
function majorOf(version) {
  const match = /^(\d+)\./.exec(version) ?? /^(\d+)$/.exec(version)
  return match ? Number(match[1]) : null
}

/**
 * Множество MAJOR-версий, допустимых диапазоном, или null — «диапазон не
 * умеем разобрать, не наша забота» (workspace:, git-ссылка, `*`, сложные
 * составные диапазоны через дефис и т.п.) — намеренно консервативно: лучше
 * пропустить находку, чем сфабриковать её на диапазоне, который не поняли.
 * Поддержаны: OR через `||`, `^X...`/`~X...` (major = X), голая версия
 * (major = её собственный), одиночные операторы отклоняются (не наша забота).
 */
function acceptableMajors(range) {
  const clauses = range.split('||').map((s) => s.trim()).filter(Boolean)
  if (clauses.length === 0) return null
  const majors = new Set()
  for (const clause of clauses) {
    const m = /^[\^~]?(\d+)(?:\.\d+)?(?:\.\d+)?/.exec(clause)
    if (!m || m[0].length !== clause.length && !/^[\^~]?\d+(\.\d+){0,2}(-[\w.]+)?$/.test(clause)) return null
    majors.add(Number(m[1]))
  }
  return majors
}

function checkMajorConsistency(packages, snapshotKeys) {
  const violations = []
  const sortedSnapshotKeys = snapshotKeys // порядок как в файле — детерминированный вывод

  for (const [pkgKey, pkgVal] of Object.entries(packages)) {
    const peerDeps = pkgVal.peerDependencies
    if (Object.keys(peerDeps).length === 0) continue
    const declared = splitNameVersion(pkgKey)
    if (!declared) continue

    const matchingSnapshotKeys = sortedSnapshotKeys.filter((key) => {
      if (!key.startsWith(pkgKey)) return false
      const rest = key.slice(pkgKey.length)
      return rest === '' || rest.startsWith('(')
    })

    for (const snapKey of matchingSnapshotKeys) {
      const peers = peersOfSnapshotKey(snapKey)
      for (const [peerName, range] of Object.entries(peerDeps)) {
        if (pkgVal.peerDependenciesMeta[peerName]?.optional) continue
        const found = peers.find((p) => p.name === peerName)
        // Peer не найден как чистая пара name@version — content-hash группа
        // или peer действительно отсутствует в этом снимке; оба случая
        // неотличимы без повторной реализации резолвинга pnpm — не гадаем.
        if (!found) continue
        const majors = acceptableMajors(range)
        if (majors === null) continue // диапазон не разобран — не наша забота
        const actualMajor = majorOf(found.version)
        if (actualMajor === null || majors.has(actualMajor)) continue
        violations.push(
          `${declared.name}@${declared.version} объявляет peerDependencies.${peerName} = "${range}" `
          + `(допускает major ${[...majors].join('/')}), но pnpm подставил ${peerName}@${found.version} `
          + `(major ${actualMajor}) — снимок '${snapKey}' — несовместимая пара может рушить рантайм `
          + `несвязанным способом (живой случай: React error #130 на слоте 'settings.section', issue #1041)`,
        )
      }
    }
  }
  return violations
}

export { parseLock, checkMajorConsistency, acceptableMajors, majorOf, splitNameVersion, peersOfSnapshotKey }

// ── CLI-точка входа: только при прямом запуске, не при импорте тестом ─────

import { pathToFileURL } from 'node:url'
const isMain = Boolean(process.argv[1]) && import.meta.url === pathToFileURL(process.argv[1]).href
if (isMain) {
  const cloneDir = process.argv[2]
  if (!cloneDir) {
    process.stderr.write('Использование: node verify-peer-consistency.mjs <clone-dir>\n')
    process.exit(2)
  }
  const lockPath = join(cloneDir, 'apps', 'dsh-edge', 'standalone', 'pnpm-lock.yaml')

  let lockText
  try {
    lockText = readFileSync(lockPath, 'utf8')
  } catch (error) {
    process.stderr.write(`::error::Не удалось прочитать ${lockPath}: ${error instanceof Error ? error.message : String(error)}\n`)
    process.exit(1)
  }

  const { packages, snapshotKeys } = parseLock(lockText)
  const violations = checkMajorConsistency(packages, snapshotKeys)

  if (violations.length > 0) {
    process.stderr.write(
      `::error::verify-peer-consistency: несовместимых peer-пар (major-версия) — ${violations.length}, в ${lockPath}:\n`,
    )
    for (const v of violations) process.stderr.write(`  - ${v}\n`)
    process.stderr.write(
      'Сборка остановлена: несовместимая peer-пара обычно не даёт ошибки на этом шаге, '
      + 'а тихо доезжает до конкретного компонента в браузере. Почини диапазон/пин конфликтующего '
      + 'пакета (patchedDependencies/overrides в pnpm-workspace.yaml апстрима, патч-серия '
      + 'dsh-edge/patches, либо апстрим-issue) прежде чем продолжать.\n',
    )
    process.exit(1)
  }

  process.stdout.write(
    `verify-peer-consistency: ${Object.keys(packages).length} записей packages: с peerDependencies — все major-консистентны\n`,
  )
}
