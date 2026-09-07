/**
 * Юнит-тест общего разбора тела ответа (issue #575, вторая половина —
 * «показывать причину, не голый HTTP N»). Файл-источник не экспортирует
 * функцию (он инлайнится сборкой обоих плагинов как голый текст в скоуп
 * фабрики — см. build.mjs integrations/plugin-manager), поэтому тест
 * исполняет тот же текст в vm-песочнице и достаёт функцию оттуда: это
 * ровно тот код, который реально едет в оба бандла, не пересказ.
 *
 * Запуск: node --test plugins-src/shared/test/describe-response-error.test.mjs
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = await readFile(join(here, '..', 'describe-response-error.js'), 'utf8')

function load() {
  const sandbox = {}
  vm.createContext(sandbox)
  vm.runInContext(source + '\nthis.describeResponseError = describeResponseError;', sandbox)
  return sandbox.describeResponseError
}

function responseStub(status, jsonResult) {
  return {
    status,
    json: async () => {
      if (jsonResult instanceof Error) throw jsonResult
      return jsonResult
    },
  }
}

test('тело {"error":{"message"}} — сообщение показывается как есть', async () => {
  const describeResponseError = load()
  const text = await describeResponseError(
    responseStub(500, { error: { code: 'storage_quota_exceeded', message: 'суточная квота хранилища исчерпана, сброс в 00:00 UTC' } }),
  )
  assert.equal(text, 'суточная квота хранилища исчерпана, сброс в 00:00 UTC')
})

test('тело без .error.message (пустой объект) — фолбэк "HTTP N"', async () => {
  const describeResponseError = load()
  assert.equal(await describeResponseError(responseStub(503, {})), 'HTTP 503')
})

test('тело не JSON (response.json() бросает) — фолбэк "HTTP N", секция не падает', async () => {
  const describeResponseError = load()
  assert.equal(await describeResponseError(responseStub(502, new SyntaxError('Unexpected token <'))), 'HTTP 502')
})

test('тело — не объект (строка, число) — фолбэк "HTTP N"', async () => {
  const describeResponseError = load()
  assert.equal(await describeResponseError(responseStub(500, 'plain text')), 'HTTP 500')
  assert.equal(await describeResponseError(responseStub(500, null)), 'HTTP 500')
})

test('error.message не строка — фолбэк "HTTP N", не выдумываем текст', async () => {
  const describeResponseError = load()
  assert.equal(await describeResponseError(responseStub(500, { error: { message: 42 } })), 'HTTP 500')
})
