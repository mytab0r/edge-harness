// Юнит-тесты чистого разбора dsh-edge/smoke-edge-plugins.mjs (класс «тихий
// ноль»: OR из двух разных условий — «плагинов нет» и «формат import уехал» —
// читал второе как первое и выходил 0 ДО проверки unknown, которая как раз
// ловит несовпавший specifier). Фикстуры — прод-форма: реальный синтаксис
// edge-plugins.generated.ts, каким его пишет codegen-edge-plugins.mjs
// (именованный import + `{ id: '...', plugin: X }` литералы).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseGeneratedModule, classifyParsedModule } from '../smoke-edge-plugins.mjs'

// Прод-форма: реальный вид сгенерированного модуля с двумя серверными
// плагинами (тот же синтаксис, что codegen-edge-plugins.mjs эмитит).
const REAL_GENERATED = `import p0 from '@deepseek-ai/dsh-plugin-a/server'
import p1 from '@edge-harness/plugin-b/server'

export const edgePlugins = [
  { id: 'plugin-a', plugin: p0 },
  { id: 'plugin-b', plugin: p1 },
]
`

const GENERATED_NO_PLUGINS = `export const edgePlugins = [
]
`

// Живой класс дефекта: форма import-строки поменялась (например codegen стал
// эмитить default-импорт с алиасом через `as`, или добавил комментарий на той
// же строке) — regex `/^import (\\w+) from '([^']+)'$/gmu` перестаёт матчить
// НИ ОДНУ строку, но `entries` (второй, независимый regex) по-прежнему видит
// обе записи реестра.
const GENERATED_IMPORT_FORMAT_DRIFT = `import p0 from '@deepseek-ai/dsh-plugin-a/server' // eslint-disable-line
import p1 from '@edge-harness/plugin-b/server' // eslint-disable-line

export const edgePlugins = [
  { id: 'plugin-a', plugin: p0 },
  { id: 'plugin-b', plugin: p1 },
]
`

// Зеркальная сторона того же класса: import-строки остались в прод-форме
// (регэксп извлечения импортов матчит обе), а форма ЗАПИСИ реестра уехала
// (двойные кавычки вместо одинарных — например codegen стал эмитить через
// JSON.stringify, а не литералом). renderServerModule эмитит import и запись
// в одном цикле, поэтому «импорты есть, записей нет» в проде значит именно
// это — дрейф формата записи, не «плагинов нет».
const GENERATED_REGISTRY_FORMAT_DRIFT = `import p0 from '@deepseek-ai/dsh-plugin-a/server'
import p1 from '@edge-harness/plugin-b/server'

export const edgePlugins = [
  { id: "plugin-a", plugin: p0 },
  { id: "plugin-b", plugin: p1 },
]
`

test('parseGeneratedModule читает реальную форму (два плагина, два импорта)', () => {
  const { imports, entries } = parseGeneratedModule(REAL_GENERATED)
  assert.equal(imports.size, 2)
  assert.equal(entries.length, 2)
  assert.deepEqual(entries.map(e => e.id), ['plugin-a', 'plugin-b'])
  assert.ok(entries.every(e => e.specifier !== undefined))
})

test('classifyParsedModule: реестр пуст — легитимный no-plugins, не ошибка', () => {
  const parsed = parseGeneratedModule(GENERATED_NO_PLUGINS)
  assert.equal(parsed.entries.length, 0)
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'no-plugins')
})

// Прямая проверка фикса: до фикса `imports.size === 0 || entries.length === 0`
// читал этот случай (imports пуст, entries НЕ пуст) как «плагинов нет» и
// выходил 0 — теряя реальные два плагина из дыма молча. После фикса это
// отдельный kind, не 'no-plugins'.
test('classifyParsedModule: import-формат уехал, но записи реестра ЕСТЬ — не no-plugins', () => {
  const parsed = parseGeneratedModule(GENERATED_IMPORT_FORMAT_DRIFT)
  assert.equal(parsed.imports.size, 0, 'фикстура обязана воспроизводить разошедшийся regex импорта')
  assert.equal(parsed.entries.length, 2, 'записи реестра по-прежнему видны — именно это раньше терялось')
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'import-format-drift')
  assert.equal(classified.entriesCount, 2)
})

// Прямая проверка фикса второго гейта: до фикса единственная проверка
// `entries.length === 0` уходила в 'no-plugins' и печатала «плагинов нет —
// деградация в апстримную сборку», хотя оба импорта прод-формы целы — два
// реально сгенерированных плагина молча пропадали бы из дыма, exit 0.
test('classifyParsedModule: форма записи реестра уехала, но импорты ЕСТЬ — не no-plugins', () => {
  const parsed = parseGeneratedModule(GENERATED_REGISTRY_FORMAT_DRIFT)
  assert.equal(parsed.imports.size, 2, 'фикстура обязана воспроизводить целые импорты прод-формы')
  assert.equal(parsed.entries.length, 0, 'записи реестра не матчатся — именно это раньше терялось как no-plugins')
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'registry-format-drift')
  assert.equal(classified.importsCount, 2)
})

test('classifyParsedModule: специфайер не найден для части записей — unknown-specifier', () => {
  const generated = `import p0 from '@deepseek-ai/dsh-plugin-a/server'

export const edgePlugins = [
  { id: 'plugin-a', plugin: p0 },
  { id: 'plugin-b', plugin: p1 },
]
`
  const parsed = parseGeneratedModule(generated)
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'unknown-specifier')
  assert.deepEqual(classified.unknownIds, ['plugin-b'])
})

test('classifyParsedModule: реальная форма без дефектов — ok с обоими entries', () => {
  const parsed = parseGeneratedModule(REAL_GENERATED)
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'ok')
  assert.equal(classified.entries.length, 2)
})
