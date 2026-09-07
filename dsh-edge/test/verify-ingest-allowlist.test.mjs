// Юнит-тесты чистых экстракторов dsh-edge/verify-ingest-allowlist.mjs (класс
// «тихий ноль»: маркер найден, но regex-извлечение типов даёт пустое
// множество — обе стороны читаются как «типов нет», расхождений «нет»,
// exit 0, хотя сверка не состоялась). Фикстуры — прод-форма: реальный
// синтаксис патча 0004 (unified diff, строки с `+`) и реального core.js
// (Object.freeze-массив), не абстрактный пересказ.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { extractPatchTypes, extractStreamerTypes, assertNonEmptyAllowlists } from '../verify-ingest-allowlist.mjs'

const PATCH_MARKER = '+const HARNESS_INGEST_EVENT_TYPES = new Set(['
const STREAMER_MARKER = 'export const ALLOWED_EVENT_TYPES = Object.freeze(['

// Прод-форма (реальный фрагмент unified diff патча 0004).
const REAL_PATCH_FRAGMENT = `--- a/apps/dsh-edge/src/session-store.ts
+++ b/apps/dsh-edge/src/session-store.ts
@@
+const HARNESS_INGEST_EVENT_TYPES = new Set([
+  'user/message',
+  'assistant/message',
+  'tool/call',
+])
+const OTHER = 1
`

// Прод-форма (реальный фрагмент scripts/dsh-hands-streamer/lib/core.js).
const REAL_STREAMER_FRAGMENT = `
export const ALLOWED_EVENT_TYPES = Object.freeze([
  'user/message',
  'assistant/message',
  'tool/call',
])
`

test('extractPatchTypes читает реальный набор типов из unified diff', () => {
  const types = extractPatchTypes(REAL_PATCH_FRAGMENT, PATCH_MARKER)
  assert.deepEqual([...types].sort(), ['assistant/message', 'tool/call', 'user/message'])
})

test('extractStreamerTypes читает реальный набор типов из Object.freeze-массива', () => {
  const types = extractStreamerTypes(REAL_STREAMER_FRAGMENT, STREAMER_MARKER)
  assert.deepEqual([...types].sort(), ['assistant/message', 'tool/call', 'user/message'])
})

test('extractPatchTypes бросает громко, если маркер не найден (не тихий ноль)', () => {
  assert.throws(
    () => extractPatchTypes('+const OTHER_CONST = new Set([\n+])', PATCH_MARKER),
    /не найден маркер/,
  )
})

// Живой класс дефекта: формат объявления сменился (двойные кавычки вместо
// одинарных) — маркер НАЙДЕН, но regex `/'([a-z]+\/[a-z-]+)'/g` матчит 0 строк.
// До фикса CLI читал это как route.size === 0 && spool.size === 0 — «типов
// нет с обеих сторон», расхождений «нет», exit 0. Экстрактор сам по себе
// корректно возвращает пустое множество (это его honest ceiling — regex не
// умеет угадывать смену формата кавычек), громкую проверку добавляет CLI
// (route.size === 0 → throw) — эта пара тестов доказывает именно предпосылку
// дефекта: маркер найден, множество пусто, throw не наступает на уровне
// экстрактора самого по себе.
test('extractPatchTypes: смена формата кавычек даёт пустое множество (не throw) — CLI обязан поймать это отдельно', () => {
  const doubleQuoted = `+const HARNESS_INGEST_EVENT_TYPES = new Set([\n+  "user/message",\n+])\n+const OTHER = 1\n`
  const types = extractPatchTypes(doubleQuoted, PATCH_MARKER)
  assert.equal(types.size, 0, 'экстрактор молча возвращает пустое множество на форме, которую не понимает — это и есть предпосылка тихого нуля, которую ловит CLI-проверка route.size === 0')
})

test('extractStreamerTypes: та же смена формата даёт пустое множество', () => {
  const doubleQuoted = `\nexport const ALLOWED_EVENT_TYPES = Object.freeze([\n  "user/message",\n])\n`
  const types = extractStreamerTypes(doubleQuoted, STREAMER_MARKER)
  assert.equal(types.size, 0)
})

// Прямая проверка фикса «тихого нуля»: до фикса CLI сравнивал route/spool
// без этой проверки — два пустых множества читались как «расхождений нет»,
// exit 0. assertNonEmptyAllowlists — ровно код, который CLI вызывает перед
// сравнением; кормим её реальным нулевым множеством (результат extractor'ов
// выше на форме с двойными кавычками), а не выдуманным Set().
test('assertNonEmptyAllowlists бросает громко, если route пуст (формат маршрута уехал)', () => {
  const emptyRoute = extractPatchTypes(
    `+const HARNESS_INGEST_EVENT_TYPES = new Set([\n+  "user/message",\n+])\n+const OTHER = 1\n`,
    PATCH_MARKER,
  )
  const nonEmptySpool = extractStreamerTypes(REAL_STREAMER_FRAGMENT, STREAMER_MARKER)
  assert.equal(emptyRoute.size, 0)
  assert.throws(() => assertNonEmptyAllowlists(emptyRoute, nonEmptySpool), /route.*пусто|извлечённое множество типов пусто/)
})

test('assertNonEmptyAllowlists бросает громко, если spool пуст (формат стримера уехал)', () => {
  const nonEmptyRoute = extractPatchTypes(REAL_PATCH_FRAGMENT, PATCH_MARKER)
  const emptySpool = extractStreamerTypes(
    `\nexport const ALLOWED_EVENT_TYPES = Object.freeze([\n  "user/message",\n])\n`,
    STREAMER_MARKER,
  )
  assert.equal(emptySpool.size, 0)
  assert.throws(() => assertNonEmptyAllowlists(nonEmptyRoute, emptySpool), /streamer core\.js/)
})

test('assertNonEmptyAllowlists не бросает на реальных непустых множествах (обеих сторон)', () => {
  const route = extractPatchTypes(REAL_PATCH_FRAGMENT, PATCH_MARKER)
  const spool = extractStreamerTypes(REAL_STREAMER_FRAGMENT, STREAMER_MARKER)
  assert.doesNotThrow(() => assertNonEmptyAllowlists(route, spool))
})
