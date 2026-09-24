// #1376: исполняемая обвязка для гвардии карантина немигрируемых сессий.
//
// Извлекает РЕАЛЬНЫЙ текст двух методов, добавленных
// dsh-edge/patches/0007-quarantine-unmigratable-session.patch, и исполняет их
// против стаба — не пересказ. Тот же приём и тот же экстрактор, что у
// dsh-edge/verify-ingest-resident-safety.mjs (#1163): пересказанный метод
// зеленел бы, даже если патч давно уехал.
//
// Что здесь доказывается (класс, а не случай):
//   1. отказ ОДНОЙ сессии не выносит преflight остальных — они мигрируют;
//   2. отказ НЕ того класса (порча, битый заголовок) по-прежнему валит старт,
//      потому что порча и «этот билд не умеет читать такой лог» лечатся
//      по-разному, и превращать первое в тихий пропуск нельзя;
//   3. список карантина переписывается целиком на КАЖДОМ старте, включая
//      пустой — иначе протухшая запись пережила бы свою причину.
//
// Честная граница. Стаб моделирует ровно то, от чего зависит фикс: что
// prepareMigration может бросить, и что storage.sql.exec — единственный путь
// к хранилищу. Он НЕ проверяет, что апстримный postInitialize по-прежнему
// зовёт migrateStoredSessions внутри одной транзакции и что путь rebuild
// устроен так, как описано в ADR 0027, — это свойства апстрима, и после
// бампа пина их надо перечитать глазами (upstream_drift.py эту гвардию не
// перепроверяет). Структурные утверждения ниже ловят уход самого патча.

import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
export const patchPath = join(
  root, 'dsh-edge', 'patches', '0007-quarantine-unmigratable-session.patch',
)

/**
 * Восстановить ПОСТ-ПАТЧЕВЫЙ текст файла из хунков диффа: строки `-` выбрасываются,
 * ` ` и `+` остаются. Это и есть то, что окажется в исходнике после `git apply`, —
 * в отличие от «сплошного прогона +-строк» такой разбор не спотыкается о то, что
 * git переиспользует уже существующую закрывающую скобку как КОНТЕКСТНУЮ строку
 * (живой случай: `recordQuarantinedSessions` в этом самом патче).
 * @param {string} patch - полный текст unified diff.
 * @returns {string} склейка пост-патчевых кусков файла, по хунку на кусок.
 */
export function patchedHunkText(patch) {
  const out = []
  let inHunk = false
  for (const line of patch.split('\n')) {
    if (line.startsWith('@@')) { inHunk = true; out.push('\u0000HUNK\u0000'); continue }
    if (!inHunk) continue
    if (line.startsWith('diff --git') || line.startsWith('--- ') || line.startsWith('+++ ')) {
      inHunk = false
      continue
    }
    if (line.startsWith('-') || line.startsWith('\\')) continue
    if (line.startsWith('+') || line.startsWith(' ')) { out.push(line.slice(1)); continue }
    if (line === '') { out.push('') ; continue }
    inHunk = false
  }
  return out.join('\n')
}

/**
 * Вытащить один метод из ПОСТ-ПАТЧЕВОГО текста по маркеру объявления, считая
 * баланс скобок. Громко падает, если маркера нет или метод не закрылся внутри
 * восстановленного куска: и то и другое значит, что патч уехал, а экстрактор
 * молча читает не то, что заявляет («тихий ноль», тот же класс, что в
 * verify-ingest-allowlist.mjs).
 * @param {string} patch - полный текст unified diff.
 * @param {string} marker - объявление метода, например 'private preflightOutdatedSessions('.
 * @returns {string} исходник метода после наложения патча.
 */
export function extractPatchMethod(patch, marker) {
  const text = patchedHunkText(patch)
  const at = text.indexOf(`  ${marker}`)
  if (at === -1) {
    throw new Error(`патч 0007: не найден маркер метода «${marker}» — патч уехал от этого экстрактора`)
  }
  const lines = text.slice(at).split('\n')
  const collected = []
  let depth = 0
  let opened = false
  for (const line of lines) {
    if (line.includes('\u0000HUNK\u0000')) {
      throw new Error(
        `патч 0007: блок «${marker}» не закрылся внутри одного хунка — `
        + 'патч уехал от этого экстрактора, и склейка через границу хунка читала бы не тот код',
      )
    }
    collected.push(line)
    for (const ch of line) {
      if (ch === '{') { depth += 1; opened = true }
      else if (ch === '}') depth -= 1
    }
    if (opened && depth === 0) break
  }
  if (!opened || depth !== 0) {
    throw new Error(`патч 0007: блок «${marker}» не закрылся (незакрытая скобка) — патч уехал от этого экстрактора`)
  }
  return collected.join('\n')
}

/**
 * Структурная страховка поверх поведенческой (тот же класс, что
 * assertNoUnconditionalDispose в гвардии #1163): сценарий ниже доказывает
 * безопасность ТЕКУЩЕГО текста, но сам по себе не мешает вернуть в
 * migrateStoredSessions ловлю всех ошибок подряд или включить путь rebuild
 * при непустом карантине. Эти два свойства живут в правках СУЩЕСТВУЮЩЕГО
 * метода — сплошным `+`-блоком их не извлечь, поэтому они проверяются прямо
 * по тексту диффа.
 * @param {string} patch - полный текст unified diff.
 */
export function assertQuarantineWiring(patch) {
  if (!patch.includes('+      const rebuild = refused.size === 0 && this.isEventTableRebuildCheaper(legacy)')) {
    throw new Error(
      'патч 0007: путь rebuild больше не выключается при непустом карантине — '
      + 'он копирует в новую таблицу только строки текущей версии и дропает старую, '
      + 'то есть СТЁР БЫ события карантинной сессии (ADR 0027)',
    )
  }
  if (!patch.includes('+      for (const row of migratable) {')) {
    throw new Error(
      'патч 0007: второй проход снова идёт по outdated, а не по migratable — '
      + 'карантинная сессия попадёт в migrateSession и бросит из фазы записи',
    )
  }
  if (!patch.includes('+    this.recordQuarantinedSessions(quarantined)')) {
    throw new Error(
      'патч 0007: список карантина больше не пишется в конце migrateStoredSessions — '
      + 'снаружи DO его будет не прочитать',
    )
  }
}

/**
 * Отказ, который ЕДИНСТВЕННЫЙ подлежит изоляции. Имя класса — то самое, по
 * которому патч различает ветки через instanceof; объект создаётся внутри
 * исполняемого модуля (см. runQuarantineScenario), потому что instanceof
 * сверяет прототип, а не строку.
 */
export const UNSUPPORTED_MIGRATION = 'unsupported-migration'

/** Порча: НЕ изолируется, старт обязан упасть. */
export const CORRUPTION = 'corruption'

/**
 * Собрать исполняемый модуль из извлечённых методов плюс минимальный стаб и
 * прогнать преflight над набором сессий.
 *
 * @param {object} opts
 * @param {Array<{id: string, version: number, rows: number, throws?: string}>} opts.sessions
 *   строки dsh_sessions: `throws` — вид отказа prepareMigration на этой сессии
 *   (UNSUPPORTED_MIGRATION или CORRUPTION), отсутствует — сессия мигрирует.
 * @param {string} [opts.patch] - текст диффа (по умолчанию настоящий, с диска).
 * @returns {{legacy: number, quarantined: Array, sql: Array<{query: string, args: Array}>,
 *            errors: Array<string>, thrown: Error|undefined}}
 */
export async function runQuarantineScenario(opts) {
  const patch = opts.patch ?? readFileSync(patchPath, 'utf8')
  const preflight = extractPatchMethod(patch, 'private preflightOutdatedSessions(')
  const record = extractPatchMethod(patch, 'private recordQuarantinedSessions(')
  // #1514: третий метод — память вердиктов. Без него сценарий исполнял бы
  // preflight без той самой ветки, ради которой правка и сделана.
  const known = extractPatchMethod(patch, 'private knownQuarantine(')

  const sql = []
  const errors = []
  const source = `
const SESSION_FORMAT_VERSION = 3
// #1514: то же значение, что в патче. Сценарий «вердикт протух» двигает не
// часы, а observed_at сохранённой строки — так тест не зависит от таймеров.
const QUARANTINE_RECHECK_MS = 60 * 60 * 1000
// Тот же класс отказа, что ловит патч. Имя — единственное, по чему instanceof
// различает изолируемый отказ формата и всё остальное; стаб обязан нести
// РОВНО его, иначе сценарий проверял бы не ту ветку.
class SessionFormatUnsupportedMigrationError extends Error {
  constructor(message) { super(message); this.name = 'SessionFormatUnsupportedMigrationError' }
}
class Harness {
  constructor(sessions, sql, stored) {
    this.sessions = sessions
    // #1514: минимальная модель таблицы карантина — ровно то, что знает
    // knownQuarantine: SELECT отдаёт сохранённые строки, DROP/CREATE/INSERT
    // их переписывают. Заглушкой «вечно пустой toArray» проверить
    // переиспользование вердикта нельзя: память обязана быть настоящей.
    this.stored = stored ?? []
    this.storage = { sql: { exec: (query, ...args) => {
      sql.push({ query, args })
      if (/^\\s*SELECT[\\s\\S]*dsh_edge_quarantined_sessions/.test(query)) {
        if (this.stored.some(r => r.format_target === undefined)) {
          throw new Error('no such column: format_target')
        }
        return { toArray: () => this.stored.slice() }
      }
      if (/DROP TABLE/.test(query)) this.stored = []
      if (/^\\s*INSERT/.test(query)) {
        const [id, stored_version, format_target, reason, observed_at] = args
        this.stored.push({ id, stored_version, format_target, reason, observed_at })
      }
      return { toArray: () => [] }
    } } }
    this._sql = sql
  }
  prepareMigration(id, row) {
    const entry = this.sessions.find(s => s.id === id)
    if (entry.throws === 'unsupported-migration') {
      throw new SessionFormatUnsupportedMigrationError(
        'format v2 surface before first step cannot acquire a system head without changing chronology')
    }
    if (entry.throws === 'corruption') {
      const error = new Error('stored log failed validation')
      error.name = 'SessionPersistenceCorruptionError'
      throw error
    }
    return { physicalRows: entry.rows }
  }
${preflight}
${record}
${known}
}
export { Harness }
`
  const url = 'data:text/javascript;base64,'
    + Buffer.from(stripTypes(source), 'utf8').toString('base64')
  const { Harness } = await import(url)

  const realError = console.error
  console.error = (...args) => errors.push(args.join(' '))
  const harness = new Harness(opts.sessions, sql, opts.stored)
  let result
  let thrown
  try {
    result = harness.preflightOutdatedSessions(
      opts.sessions.map(s => ({ id: s.id, version: s.version })),
    )
    harness.recordQuarantinedSessions(result.quarantined)
  } catch (error) {
    thrown = error
  } finally {
    console.error = realError
  }
  return {
    legacy: result?.legacy,
    quarantined: result?.quarantined ?? [],
    sql,
    errors,
    thrown,
  }
}

/**
 * Снять TypeScript-аннотации, которых нет в JS. Не полноценный компилятор:
 * извлечённые методы несут ровно две формы — аннотации параметров и типы
 * возврата, — и обе снимаются построчно. Любая третья форма упадёт при
 * импорте громко (SyntaxError), а не молча пройдёт мимо проверки.
 * @param {string} source - исходник с аннотациями.
 * @returns {string} исходник без них.
 */
function stripTypes(source) {
  return source
    .replace(/private preflightOutdatedSessions\(outdated: readonly HeaderRow\[\]\): \{\n\s*legacy: number\n\s*quarantined: readonly QuarantinedSession\[\]\n\s*\} \{/,
      'preflightOutdatedSessions(outdated) {')
    .replace(/private recordQuarantinedSessions\(entries: readonly QuarantinedSession\[\]\): void \{/,
      'recordQuarantinedSessions(entries) {')
    .replace(/const quarantined: QuarantinedSession\[\] = \[\]/, 'const quarantined = []')
    .replace(/row\.id as SessionId/g, 'row.id')
    // #1514: сигнатура памяти вердиктов — две строки generic'ов, снимаются
    // так же построчно, как и две формы выше.
    .replace(/private knownQuarantine\(\): Map<string, \{\n[\s\S]*?\n\s*\}> \{/, 'knownQuarantine() {')
    .replace(/const known = new Map<string, \{\n[\s\S]*?\n\s*\}>\(\)/, 'const known = new Map()')
}
