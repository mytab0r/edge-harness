// #1376: поведенческая гвардия карантина немигрируемых сессий.
//
// Живой случай: прогон deploy-dsh-edge.yml 35441352792 (2026-09-19). Морда на
// 0.15.0 не поднималась вовсе — одна историческая сессия формата v2, чей
// первый surface-евент стоит раньше первого step/start, роняла старт всего
// Durable Object, потому что апстримный postInitialize коммитит апгрейд одной
// транзакцией («An incompatible later session must also roll back earlier
// sessions»). Каждый запрос отвечал 500, канарейка краснела, автооткат
// возвращал 0.14.1 — сутки подряд.
//
// Гвардия исполняет РЕАЛЬНЫЙ текст патча 0007 (извлечённый из диффа), а не
// пересказ: пересказ зеленел бы и после того, как патч уехал.
// Разбор решения — docs/decisions/0027-quarantine-unmigratable-session.md.
//
// Запуск: node --test dsh-edge/test/quarantine-unmigratable.test.mjs

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  CORRUPTION,
  UNSUPPORTED_MIGRATION,
  assertQuarantineWiring,
  extractPatchMethod,
  patchPath,
  runQuarantineScenario,
} from '../verify-quarantine-unmigratable.mjs'

test('отказ одной сессии не мешает мигрировать остальным', async () => {
  const result = await runQuarantineScenario({
    sessions: [
      { id: 'harness-11', version: 2, rows: 40 },
      { id: 'harness-22', version: 2, rows: 7, throws: UNSUPPORTED_MIGRATION },
      { id: 'harness-33', version: 2, rows: 60 },
    ],
  })

  assert.equal(result.thrown, undefined,
    'отказ формата обязан быть изолирован, а не вынесен наружу: он валит старт всего DO')
  assert.deepEqual(result.quarantined.map(e => e.id), ['harness-22'])
  assert.equal(result.legacy, 100,
    'строки карантинной сессии не должны попадать в счётчик legacy — по нему выбирается путь rebuild')
})

test('карантинная сессия названа поимённо, с версией и причиной', async () => {
  const result = await runQuarantineScenario({
    sessions: [{ id: 'harness-22', version: 2, rows: 7, throws: UNSUPPORTED_MIGRATION }],
  })

  const [entry] = result.quarantined
  assert.equal(entry.id, 'harness-22')
  assert.equal(entry.storedVersion, 2)
  assert.match(entry.reason, /surface before first step/,
    'причина обязана доехать дословно: без неё читатель не отличит «не умеем читать» от «сломалось»')

  const said = result.errors.join('\n')
  assert.match(said, /harness-22/, 'id сессии обязан попасть в лог воркера')
  assert.match(said, /v2/, 'сохранённая версия обязана попасть в лог воркера')
  assert.match(said, /surface before first step/, 'причина обязана попасть в лог воркера')
})

test('порча по-прежнему валит старт — её нельзя превращать в тихий пропуск', async () => {
  const result = await runQuarantineScenario({
    sessions: [
      { id: 'harness-11', version: 2, rows: 40 },
      { id: 'harness-99', version: 2, rows: 3, throws: CORRUPTION },
    ],
  })

  assert.notEqual(result.thrown, undefined,
    'повреждённый лог — это damage, а не «этот билд не умеет читать»: лечится по-другому и обязан быть громким')
  assert.equal(result.thrown.name, 'SessionPersistenceCorruptionError')
  assert.deepEqual(result.quarantined, [],
    'порча не карантинится: иначе повреждение данных прошло бы молча')
})

test('список карантина переписывается целиком даже когда он пуст', async () => {
  const result = await runQuarantineScenario({
    sessions: [{ id: 'harness-11', version: 2, rows: 40 }],
  })

  const queries = result.sql.map(call => call.query.replace(/\s+/g, ' ').trim())
  assert.ok(queries.some(q => q.startsWith('CREATE TABLE IF NOT EXISTS dsh_edge_quarantined_sessions')),
    'таблица карантина обязана создаваться до записи')
  assert.ok(queries.some(q => q === 'DELETE FROM dsh_edge_quarantined_sessions'),
    'пустой список обязан ОЧИЩАТЬ таблицу: иначе запись переживёт свою причину, '
    + 'и починенная сессия навсегда останется «карантинной» (тормоз без газа)')
  assert.equal(queries.filter(q => q.startsWith('INSERT INTO dsh_edge_quarantined_sessions')).length, 0)
})

test('непустой список карантина доезжает до таблицы строкой на сессию', async () => {
  const result = await runQuarantineScenario({
    sessions: [
      { id: 'harness-22', version: 2, rows: 7, throws: UNSUPPORTED_MIGRATION },
      { id: 'harness-44', version: 2, rows: 9, throws: UNSUPPORTED_MIGRATION },
    ],
  })

  const inserts = result.sql.filter(call => call.query.includes('INSERT INTO dsh_edge_quarantined_sessions'))
  assert.equal(inserts.length, 2)
  assert.deepEqual(inserts.map(call => call.args[0]), ['harness-22', 'harness-44'])
  assert.deepEqual(inserts.map(call => call.args[1]), [2, 2])
})

test('правки существующего метода на месте: rebuild выключен, второй проход по migratable', () => {
  assertQuarantineWiring(readFileSync(patchPath, 'utf8'))
})

test('экстрактор падает громко, а не отдаёт тихий ноль', () => {
  assert.throws(
    () => extractPatchMethod('diff --git a/x b/x\n+  private somethingElse() {}\n', 'private preflightOutdatedSessions('),
    /не найден маркер метода/,
    'исчезнувший маркер обязан быть громким: иначе гвардия молча проверяла бы пустую строку',
  )
})
