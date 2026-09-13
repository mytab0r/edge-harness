// Юнит-тесты чистого разбора dsh-edge/smoke-edge-plugins.mjs (класс «тихий
// ноль»: расхождение формата извлечения читалось как «кандидатов нет» и
// уходило зелёным exit 0). Фикстуры — прод-форма: дословный синтаксис
// edge-plugins.generated.ts, каким его эмитит codegen-edge-plugins.mjs
// ::renderServerModule (объявление массива с type-аннотацией одной строкой,
// именованный import, литерал `{ id: '...', plugin: X }`).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync, rmSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { parseGeneratedModule, classifyParsedModule } from '../smoke-edge-plugins.mjs'

// Дословная строка объявления из renderServerModule (codegen-edge-plugins.mjs):
// эмитится безусловно, даже при нуле серверных плагинов.
const CODEGEN_ARRAY_DECL = 'export const edgePlugins: { id: string; plugin: unknown }[] = ['

// Прод-форма: реальный вид сгенерированного модуля с двумя серверными
// плагинами (тот же синтаксис, что codegen-edge-plugins.mjs эмитит).
const REAL_GENERATED = `import p0 from '@deepseek-ai/dsh-plugin-a/server'
import p1 from '@edge-harness/plugin-b/server'

${CODEGEN_ARRAY_DECL}
  { id: 'plugin-a', plugin: p0 },
  { id: 'plugin-b', plugin: p1 },
]
`

const GENERATED_NO_PLUGINS = `${CODEGEN_ARRAY_DECL}
]
`

// Живой класс дефекта: форма import-строки поменялась (например codegen стал
// эмитить default-импорт с алиасом через `as`, или добавил комментарий на той
// же строке) — regex `/^import (\\w+) from '([^']+)'$/gmu` перестаёт матчить
// НИ ОДНУ строку, но `entries` (второй, независимый regex) по-прежнему видит
// обе записи реестра.
const GENERATED_IMPORT_FORMAT_DRIFT = `import p0 from '@deepseek-ai/dsh-plugin-a/server' // eslint-disable-line
import p1 from '@edge-harness/plugin-b/server' // eslint-disable-line

${CODEGEN_ARRAY_DECL}
  { id: 'plugin-a', plugin: p0 },
  { id: 'plugin-b', plugin: p1 },
]
`

// Односторонний дрейф: import-строки остались в прод-форме (регэксп
// извлечения импортов матчит обе), а форма ЗАПИСИ реестра уехала (двойные
// кавычки вместо одинарных — например codegen стал эмитить через
// JSON.stringify, а не литералом).
const GENERATED_REGISTRY_FORMAT_DRIFT = `import p0 from '@deepseek-ai/dsh-plugin-a/server'
import p1 from '@edge-harness/plugin-b/server'

${CODEGEN_ARRAY_DECL}
  { id: "plugin-a", plugin: p0 },
  { id: "plugin-b", plugin: p1 },
]
`

// Совместный дрейф (блокирующая находка ревью PR #640): ОДНО изменение стиля
// эмита ломает ОБА regex'а разом — renderServerModule эмитит import и запись
// из одного цикла одним стилем, поэтому самый вероятный дрейф совместный
// (здесь: кавычки сменились на двойные везде). До фикса: imports=0,
// entries=0 → ветка «обе-нуля» → 'no-plugins' → exit 0, два живых плагина
// молча выпадали из дыма. Различитель теперь — тело массива: оно непусто.
const GENERATED_JOINT_FORMAT_DRIFT = `import p0 from "@deepseek-ai/dsh-plugin-a/server"
import p1 from "@edge-harness/plugin-b/server"

${CODEGEN_ARRAY_DECL}
  { id: "plugin-a", plugin: p0 },
  { id: "plugin-b", plugin: p1 },
]
`

// Крайняя форма того же класса: объявление массива не найдено вовсе
// (codegen переименовал экспорт или поменял форму объявления). Объявление
// эмитится безусловно, так что его отсутствие — дрейф, а не «плагинов нет».
const GENERATED_NO_DECLARATION = `import p0 from '@deepseek-ai/dsh-plugin-a/server'

export default {}
`

test('parseGeneratedModule читает реальную форму (два плагина, два импорта, непустое тело)', () => {
  const { imports, entries, registryBody } = parseGeneratedModule(REAL_GENERATED)
  assert.equal(imports.size, 2)
  assert.equal(entries.length, 2)
  assert.deepEqual(entries.map(e => e.id), ['plugin-a', 'plugin-b'])
  assert.ok(entries.every(e => e.specifier !== undefined))
  assert.ok(registryBody.includes("id: 'plugin-a'"), 'тело массива обязано быть извлечено — на нём держится различение «плагинов нет» и совместного дрейфа')
})

test('classifyParsedModule: тело массива пусто и импортов нет — легитимный no-plugins', () => {
  const parsed = parseGeneratedModule(GENERATED_NO_PLUGINS)
  assert.equal(parsed.entries.length, 0)
  assert.equal(parsed.registryBody, '', 'renderServerModule эмитит объявление и скобку безусловно — при нуле плагинов тело пусто')
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
  assert.equal(classified.registryBody, 'non-empty')
})

// Прямая проверка блокирующей находки ревью PR #640: совместный дрейф обоих
// regex'ов (imports=0, entries=0) обязан быть registry-format-drift, а не
// 'no-plugins' — различитель тело массива, а не счётчик импортов.
test('classifyParsedModule: совместный дрейф (не матчит ни импорты, ни записи) — не no-plugins', () => {
  const parsed = parseGeneratedModule(GENERATED_JOINT_FORMAT_DRIFT)
  assert.equal(parsed.imports.size, 0, 'фикстура воспроизводит совместный дрейф: импорты тоже не матчатся')
  assert.equal(parsed.entries.length, 0)
  assert.ok(parsed.registryBody.includes('id: "plugin-a"'), 'записи физически в тексте есть — тело непусто, это и ловит дрейф')
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'registry-format-drift')
  assert.equal(classified.importsCount, 0)
  assert.equal(classified.registryBody, 'non-empty')
})

test('classifyParsedModule: объявление массива не найдено — registry-format-drift, не no-plugins', () => {
  const parsed = parseGeneratedModule(GENERATED_NO_DECLARATION)
  assert.equal(parsed.registryBody, null)
  const classified = classifyParsedModule(parsed)
  assert.equal(classified.kind, 'registry-format-drift')
  assert.equal(classified.registryBody, 'missing')
})

test('classifyParsedModule: специфайер не найден для части записей — unknown-specifier', () => {
  const generated = `import p0 from '@deepseek-ai/dsh-plugin-a/server'

${CODEGEN_ARRAY_DECL}
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

// Видимый результат фикса — EXIT процесса, а не только классификация:
// кормим CLI реальным файлом сгенерированного модуля в структуре клона.
// Совместный дрейф обязан дать exit 2 с громким сообщением (до фикса —
// exit 0 «серверных плагинов нет»). Классификация происходит ДО записи
// бутстрапа и spawn cordis, поэтому настоящий клон/зависимости не нужны.
test('CLI: совместный дрейф формата — громкий exit 2, не зелёный 0', () => {
  const dir = mkdtempSync(join(tmpdir(), 'smoke-edge-plugins-'))
  try {
    const srcDir = join(dir, 'apps', 'dsh-edge', 'src')
    mkdirSync(srcDir, { recursive: true })
    writeFileSync(join(srcDir, 'edge-plugins.generated.ts'), GENERATED_JOINT_FORMAT_DRIFT)
    const run = spawnSync(process.execPath, [join(import.meta.dirname, '..', 'smoke-edge-plugins.mjs'), dir], { encoding: 'utf8' })
    assert.equal(run.status, 2, `stderr: ${run.stderr}`)
    assert.match(run.stderr, /registry-format-drift|0 сматченных записей реестра/)
    assert.match(run.stderr, /НЕПУСТО/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('CLI: легитимный ноль плагинов (пустое тело) — зелёный exit 0', () => {
  const dir = mkdtempSync(join(tmpdir(), 'smoke-edge-plugins-'))
  try {
    const srcDir = join(dir, 'apps', 'dsh-edge', 'src')
    mkdirSync(srcDir, { recursive: true })
    writeFileSync(join(srcDir, 'edge-plugins.generated.ts'), GENERATED_NO_PLUGINS)
    const run = spawnSync(process.execPath, [join(import.meta.dirname, '..', 'smoke-edge-plugins.mjs'), dir], { encoding: 'utf8' })
    assert.equal(run.status, 0, `stderr: ${run.stderr}`)
    assert.match(run.stdout, /серверных плагинов нет/)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
