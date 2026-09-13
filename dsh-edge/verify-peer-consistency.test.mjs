// Мутационное доказательство гвардии консистентности peer-зависимостей
// (issue #1041): гвардия обязана быть зелёной на lockfile'е, где peer
// подставлен в допустимом MAJOR (даже при отставании patch/minor —
// намеренно НЕ находка, см. докстринг verify-peer-consistency.mjs), и
// красной с точным сообщением на lockfile'е, где peer подставлен ЧУЖИМ
// MAJOR (симуляция живого случая #1041: react-dom@19.2.8 получил react@18.3.1
// вместо требуемого ^19.2.8). Тест — часть repo-ci, не разовая ручная проверка.
// Запуск: node --test dsh-edge/verify-peer-consistency.test.mjs
import { spawnSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import {
  parseLock,
  checkMajorConsistency,
  acceptableMajors,
  majorOf,
  splitNameVersion,
  peersOfSnapshotKey,
} from './verify-peer-consistency.mjs'

const guard = fileURLToPath(new URL('./verify-peer-consistency.mjs', import.meta.url))

/** Фейковый clone-dir: apps/dsh-edge/standalone/pnpm-lock.yaml с заданным содержимым. */
function fakeClone(lockContents) {
  const dir = mkdtempSync(join(tmpdir(), 'peer-consistency-guard-'))
  const standaloneDir = join(dir, 'apps', 'dsh-edge', 'standalone')
  mkdirSync(standaloneDir, { recursive: true })
  writeFileSync(join(standaloneDir, 'pnpm-lock.yaml'), lockContents)
  return dir
}

function runGuard(cloneDir) {
  return spawnSync(process.execPath, [guard, cloneDir], { encoding: 'utf8' })
}

const CLEAN_LOCK = `
packages:

  react@18.3.1:
    resolution: {integrity: sha512-aaa==}
    engines: {node: '>=0.10.0'}

  react-dom@18.3.1:
    resolution: {integrity: sha512-bbb==}
    peerDependencies:
      react: ^18.2.0

  '@deepseek-ai/cordis-plugin-loader@1.0.2':
    resolution: {integrity: sha512-ccc==}
    peerDependencies:
      '@deepseek-ai/cordis': ^4.0.2

  '@deepseek-ai/cordis@4.0.2':
    resolution: {integrity: sha512-ddd==}

snapshots:

  react@18.3.1:
    dependencies:
      loose-envify: 1.4.0

  react-dom@18.3.1(react@18.3.1):
    dependencies:
      react: 18.3.1

  '@deepseek-ai/cordis-plugin-loader@1.0.2(@deepseek-ai/cordis@4.0.2)':
    dependencies:
      '@deepseek-ai/cordis': 4.0.2

  '@deepseek-ai/cordis@4.0.2': {}
`

const MAJOR_MISMATCH_LOCK = `
packages:

  react@18.3.1:
    resolution: {integrity: sha512-aaa==}
    engines: {node: '>=0.10.0'}

  react-dom@19.2.8:
    resolution: {integrity: sha512-bbb==}
    peerDependencies:
      react: ^19.2.8

snapshots:

  react@18.3.1:
    dependencies:
      loose-envify: 1.4.0

  react-dom@19.2.8(react@18.3.1):
    dependencies:
      react: 18.3.1
      scheduler: 0.27.0
`

const MINOR_LAG_ONLY_LOCK = `
packages:

  '@deepseek-ai/cordis-plugin-loader@1.0.2':
    resolution: {integrity: sha512-ccc==}
    peerDependencies:
      '@deepseek-ai/cordis': ^1.0.3

snapshots:

  '@deepseek-ai/cordis-plugin-loader@1.0.2(@deepseek-ai/cordis@1.0.2)':
    dependencies:
      '@deepseek-ai/cordis': 1.0.2
`

const OPTIONAL_PEER_MISSING_LOCK = `
packages:

  some-plugin@1.0.0:
    resolution: {integrity: sha512-eee==}
    peerDependencies:
      react-dom: ^19.0.0
    peerDependenciesMeta:
      react-dom:
        optional: true

snapshots:

  some-plugin@1.0.0: {}
`

const UNPARSEABLE_RANGE_LOCK = `
packages:

  some-workspace-pkg@1.0.0:
    resolution: {integrity: sha512-fff==}
    peerDependencies:
      host-app: 'workspace:*'

snapshots:

  some-workspace-pkg@1.0.0(host-app@2.0.0):
    dependencies:
      host-app: 2.0.0
`

describe('verify-peer-consistency (гвардия MAJOR-консистентности peer-зависимостей, #1041)', () => {
  it('зелёная: peer подставлен в допустимом MAJOR', () => {
    const clone = fakeClone(CLEAN_LOCK)
    try {
      const run = runGuard(clone)
      assert.equal(run.status, 0, `гвардия красная на консистентном lockfile: ${run.stderr}`)
      assert.match(run.stdout, /major-консистентны/)
    } finally {
      rmSync(clone, { recursive: true, force: true })
    }
  })

  it('красная с точным сообщением: MAJOR-мисматч (мутация — живой случай react-dom@19/react@18, #1041)', () => {
    const clone = fakeClone(MAJOR_MISMATCH_LOCK)
    try {
      const run = runGuard(clone)
      assert.equal(run.status, 1, 'гвардия пропустила MAJOR-мисматч — мутация не поймана')
      assert.match(run.stderr, /react-dom@19\.2\.8 объявляет peerDependencies\.react = "\^19\.2\.8"/)
      assert.match(run.stderr, /react@18\.3\.1 \(major 18\)/)
      assert.match(run.stderr, /issue #1041/)
      assert.match(run.stderr, /Сборка остановлена/)
    } finally {
      rmSync(clone, { recursive: true, force: true })
    }
  })

  it('зелёная: расхождение patch/minor В ОДНОМ major — не находка (намеренно, см. докстринг)', () => {
    const clone = fakeClone(MINOR_LAG_ONLY_LOCK)
    try {
      const run = runGuard(clone)
      assert.equal(run.status, 0, `гвардия ошибочно роняет билд на patch/minor-отставании: ${run.stderr}`)
    } finally {
      rmSync(clone, { recursive: true, force: true })
    }
  })

  it('зелёная: опциональный peer отсутствует в снимке — не находка', () => {
    const clone = fakeClone(OPTIONAL_PEER_MISSING_LOCK)
    try {
      const run = runGuard(clone)
      assert.equal(run.status, 0, `гвардия ошибочно требует опциональный peer: ${run.stderr}`)
    } finally {
      rmSync(clone, { recursive: true, force: true })
    }
  })

  it('зелёная: неразбираемый диапазон (workspace:) — консервативно пропущен, не находка', () => {
    const clone = fakeClone(UNPARSEABLE_RANGE_LOCK)
    try {
      const run = runGuard(clone)
      assert.equal(run.status, 0, `гвардия ошибочно фейлит на неразобранном диапазоне: ${run.stderr}`)
    } finally {
      rmSync(clone, { recursive: true, force: true })
    }
  })

  it('красная: pnpm-lock.yaml не найден — громкий отказ, не сырой стек', () => {
    const dir = mkdtempSync(join(tmpdir(), 'peer-consistency-guard-empty-'))
    try {
      const run = runGuard(dir)
      assert.equal(run.status, 1)
      assert.match(run.stderr, /::error::Не удалось прочитать/)
    } finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  it('красная: clone-dir не передан (usage)', () => {
    const run = spawnSync(process.execPath, [guard], { encoding: 'utf8' })
    assert.equal(run.status, 2)
    assert.match(run.stderr, /Использование:/)
  })
})

describe('verify-peer-consistency — юниты разбора', () => {
  it('acceptableMajors: OR-диапазон объединяет majors каждого дизъюнкта', () => {
    assert.deepEqual(acceptableMajors('^16.8.0 || ^17.0.0 || ^18.0.0 || ^19.0.0'), new Set([16, 17, 18, 19]))
  })

  it('acceptableMajors: каретка пинит major', () => {
    assert.deepEqual(acceptableMajors('^4.0.2'), new Set([4]))
  })

  it('acceptableMajors: голая версия (в т.ч. с пререлизом) — major сама версия', () => {
    assert.deepEqual(acceptableMajors('0.1.2-rc.1'), new Set([0]))
  })

  it('acceptableMajors: неразбираемый диапазон → null', () => {
    assert.equal(acceptableMajors('workspace:*'), null)
    assert.equal(acceptableMajors('>=18 <20'), null)
  })

  it('majorOf: ведущее целое версии', () => {
    assert.equal(majorOf('18.3.1'), 18)
    assert.equal(majorOf('0.1.2-rc.1'), 0)
    assert.equal(majorOf('not-a-version'), null)
  })

  it('splitNameVersion: @scope/name@version и отбрасывание собственного вложенного контекста', () => {
    assert.deepEqual(splitNameVersion('react-dom@19.2.8'), { name: 'react-dom', version: '19.2.8' })
    assert.deepEqual(
      splitNameVersion('@deepseek-ai/dsh-agent@0.1.2-rc.1(717cb1113bc0278a9ba75fdbe6b85d08)'),
      { name: '@deepseek-ai/dsh-agent', version: '0.1.2-rc.1' },
    )
    assert.equal(splitNameVersion('717cb1113bc0278a9ba75fdbe6b85d08'), null) // content-hash без '@'
    assert.equal(splitNameVersion('patch_hash=abc123'), null)
  })

  it('peersOfSnapshotKey: верхнеуровневые peer-пары, вложенные скобки не разбираются как отдельные peer', () => {
    const peers = peersOfSnapshotKey(
      "@tanstack/react-virtual@3.14.10(react-dom@19.2.8(react@18.3.1))(react@18.3.1)",
    )
    assert.deepEqual(peers, [
      { name: 'react-dom', version: '19.2.8' },
      { name: 'react', version: '18.3.1' },
    ])
  })

  it('parseLock + checkMajorConsistency: находит ровно ожидаемую находку на мутации', () => {
    const { packages, snapshotKeys } = parseLock(MAJOR_MISMATCH_LOCK)
    const violations = checkMajorConsistency(packages, snapshotKeys)
    assert.equal(violations.length, 1)
    assert.match(violations[0], /react-dom@19\.2\.8/)
  })
})
