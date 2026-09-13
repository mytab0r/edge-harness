#!/usr/bin/env node
// Сверка allowlist'ов ingest-шва (#119, ревью М3): словарь событий допускается
// в двух местах — маршрут морды (патч 0004, HARNESS_INGEST_EVENT_TYPES в
// session-store.ts) и спул стримера (ALLOWED_EVENT_TYPES в
// scripts/dsh-hands-streamer/lib/core.js). Единого места правды между
// репозиториями нет, поэтому расхождение ловит ЭТОТ тест: стример шлёт событие,
// которого нет в маршруте — живой job краснеет 400-м; событие есть в маршруте,
// но стример его не шлёт — мёртвая поверхность, о которой никто не знает.
// По образцу verify-edge-plugins.mjs: красный CI вместо тихой расинхронизации.
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const patchPath = join(root, 'dsh-edge', 'patches', '0004-harness-ingest.patch')
const streamerPath = join(root, 'scripts', 'dsh-hands-streamer', 'lib', 'core.js')

/** Все кавыч-строки вида 'xxx/yyy' в куске исходника после маркера до следующего '+const'. */
export function extractPatchTypes(patch, marker) {
  const at = patch.indexOf(marker)
  if (at === -1) throw new Error(`патч 0004: не найден маркер «${marker}» — апстрим/патч уехал от пина`)
  const rest = patch.slice(at)
  const end = rest.indexOf('\n+const', 1)
  const block = end === -1 ? rest : rest.slice(0, end)
  return new Set([...block.matchAll(/'([a-z]+\/[a-z-]+)'/g)].map(m => m[1]))
}

/** Все кавыч-строки в Array-литерале после маркера до '])'. */
export function extractStreamerTypes(source, marker) {
  const at = source.indexOf(marker)
  if (at === -1) throw new Error(`streamer core.js: не найден маркер «${marker}»`)
  const rest = source.slice(at)
  const end = rest.indexOf('])')
  if (end === -1) throw new Error('streamer core.js: не найден конец массива ALLOWED_EVENT_TYPES')
  return new Set([...rest.slice(0, end).matchAll(/'([a-z]+\/[a-z-]+)'/g)].map(m => m[1]))
}

/**
 * Класс «тихий ноль»: маркер найден, но regex-извлечение типов событий из
 * найденного блока даёт пустое множество (например формат объявления
 * сменился с одинарных кавычек на двойные, или список стал многострочным
 * иначе) — обе стороны читаются как «типов нет», расхождений «нет», exit 0,
 * хотя на самом деле сверка не состоялась вовсе. По образцу
 * check-upstream-namespace-collision.mjs/integrations.mjs: пустое множество —
 * не состояние «совпадений нет», а сигнал, что регэксп извлечения разошёлся
 * с реальным форматом источника — бросаем громко. Чистая функция (без IO) —
 * юнит-тест кормит её реальным нулевым множеством, которое отдаёт
 * extractPatchTypes/extractStreamerTypes на форме с двойными кавычками.
 */
export function assertNonEmptyAllowlists(route, spool) {
  if (route.size === 0) {
    throw new Error(`патч 0004: маркер HARNESS_INGEST_EVENT_TYPES найден, но извлечённое множество типов пусто — формат объявления (кавычки/переносы строк) разошёлся с regex'ом extractPatchTypes, сверка не состоялась`)
  }
  if (spool.size === 0) {
    throw new Error(`streamer core.js: маркер ALLOWED_EVENT_TYPES найден, но извлечённое множество типов пусто — формат объявления разошёлся с regex'ом extractStreamerTypes, сверка не состоялась`)
  }
}

// CLI-режим: только при прямом вызове, не при импорте extractPatchTypes/
// extractStreamerTypes/assertNonEmptyAllowlists юнит-тестом (тот же приём,
// что integrations.mjs).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const route = extractPatchTypes(
    readFileSync(patchPath, 'utf8'),
    '+const HARNESS_INGEST_EVENT_TYPES = new Set([',
  )
  const spool = extractStreamerTypes(
    readFileSync(streamerPath, 'utf8'),
    'export const ALLOWED_EVENT_TYPES = Object.freeze([',
  )

  assertNonEmptyAllowlists(route, spool)

  const inRouteOnly = [...route].filter(t => !spool.has(t)).sort()
  const inSpoolOnly = [...spool].filter(t => !route.has(t)).sort()

  if (inRouteOnly.length || inSpoolOnly.length) {
    console.error('::error::Allowlist ингеста разошёлся со спулом стримера (#119):')
    if (inRouteOnly.length) console.error(`  только в маршруте морды: ${inRouteOnly.join(', ')}`)
    if (inSpoolOnly.length) console.error(`  только в спуле стримера:  ${inSpoolOnly.join(', ')}`)
    console.error('Сведи списки: патч 0004 (session-store.ts) и scripts/dsh-hands-streamer/lib/core.js.')
    process.exit(1)
  }

  console.log(`Allowlist ингеста = allowlist спула стримера (${route.size} типов): ${[...route].sort().join(', ')}`)
}
