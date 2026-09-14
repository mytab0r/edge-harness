#!/usr/bin/env node
// ORPHAN-TEST-OK: фикстурный раннер, не самостоятельный node-тест — вызывается
// как subprocess (`node ... <patch-path>`) из scripts/lib/test_mutation_recipe_guard.py
// (mutation-recipe-execution-guard, #1194), покрытие которого CI видит как
// `pytest scripts/lib/test_mutation_recipe_guard.py`, не как `node --test`.
//
// #1194: standalone fixture that PROVES the mutation-recipe-execution-guard
// mechanism (scripts/lib/mutation_recipe_guard.py) using the REAL historical
// bug of PR #1173/#1165 as ground truth — without touching dsh-edge/ (owned
// by the in-flight PR #1173 at the time this file was written).
//
// Why this file exists instead of importing dsh-edge/verify-ingest-resident-
// safety.mjs's own `runTwoBatchIngestScenario` directly: that function calls
// `assertNoUnconditionalDispose(methodBody)` BEFORE building the scenario —
// a deliberate structural guardrail. Feeding it the pre-#1163 patch text (the
// one this fixture is asked to test) makes it throw the STRUCTURAL error
// immediately, never reaching the three-batch scenario at all — exactly the
// review finding quoted in issue #1194: "красным первым падает структурный
// assertNoUnconditionalDispose... до сценария дело не доходит". Observing the
// actual per-batch numbers (disposeCalls, whether batch3 runs) requires the
// same bypass the human reviewer did by hand ("extractPatchMethod в обход
// структурной проверки, ручная сборка модуля") — this file is that bypass,
// written once so a guard can run it instead of trusting a human's memory.
//
// This is a maintained DUPLICATE of the stub harness in dsh-edge/verify-
// ingest-resident-safety.mjs (three-batch shape, as of PR #1173's first
// commit cb854b49) — not a re-export, because the harness lives as an inline
// string template inside that file's function body, not as a separate
// export. Only `extractPatchMethod` is imported for real (it IS exported and
// stable) — the ResidentStubStore/module-assembly below is reproduced here
// on purpose, to run WITHOUT the structural bypass check.
import { readFileSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { extractPatchMethod } from '../../../dsh-edge/verify-ingest-resident-safety.mjs'

const patchFileArg = process.argv[2]
if (!patchFileArg) {
  console.error('usage: node ingest-mutation-scenario.mjs <path-to-0004-harness-ingest.patch>')
  process.exit(2)
}

async function runThreeBatchIngestScenarioUnsafe(patch) {
  // Deliberately no assertNoUnconditionalDispose call here — see file header.
  const methodBody = extractPatchMethod(patch, 'async appendHarnessEvents(')

  const moduleSource = `
export class EdgeSessionStoreError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}
export class LlmError extends Error {}
class DurableObjectSessionPersistence {
  hasSession(_id: unknown): boolean { return true }
  readBlankSession(_id: unknown): undefined { return undefined }
}
function normalizeHarnessIngestEvent(raw: any, baseTurn: number) {
  const data = { ...raw.data, turn: (raw.data.turn ?? 0) + baseTurn }
  return { type: raw.type, data, surface: true }
}

class ResidentStubStore {
  residents = new Map<string, any>()
  disposeCalls = 0
  context = { agentDefaultModel: { currentSelection: () => ({ model: 'stub-model' }) } }
  async services() {
    return { persistence: new DurableObjectSessionPersistence(), sessions: { flush: async (_session: any) => {} } }
  }
  async modelSelection(_id: unknown, fallbackModel: string) {
    return { model: fallbackModel }
  }
  async openAgentForTurn(id: string, _model: string) {
    let handle = this.residents.get(id)
    if (handle === undefined) {
      const session = {
        seq: 1,
        log: [] as Array<{ type: string; data: unknown }>,
        snapshotEvents(): Array<{ type: string; data: unknown }> { return this.log },
        append(type: string, data: unknown, _opts?: unknown) {
          this.log.push({ type, data })
          const event = { type, data, seq: this.seq }
          this.seq += 1
          return event
        },
      }
      const store = this
      handle = {
        disposed: false,
        get agent() {
          if (handle.disposed) {
            throw new Error(\`session "\${id}" is not live in this store\`)
          }
          return { session }
        },
        dispose: async () => { store.disposeCalls += 1; handle.disposed = true },
      }
      this.residents.set(id, handle)
    }
    return handle
  }

  ${methodBody}
}

export async function scenario() {
  const store = new ResidentStubStore()
  const sessionId = 'ingest-check'
  const batch1 = await store.appendHarnessEvents(sessionId, [
    { type: 'turn/start', data: { turn: 1 } },
    { type: 'turn/end', data: { turn: 1 } },
  ])
  let batch2: { appended: number } | undefined
  let batch2Error: string | undefined
  try {
    batch2 = await store.appendHarnessEvents(sessionId, [
      { type: 'turn/start', data: { turn: 1 } },
      { type: 'turn/end', data: { turn: 1 } },
    ])
  } catch (error) {
    batch2Error = error instanceof Error ? error.message : String(error)
  }
  let batch3: { appended: number } | undefined
  let batch3Error: string | undefined
  try {
    batch3 = await store.appendHarnessEvents(sessionId, [
      { type: 'turn/start', data: { turn: 1 } },
      { type: 'turn/end', data: { turn: 1 } },
    ])
  } catch (error) {
    batch3Error = error instanceof Error ? error.message : String(error)
  }
  return { batch1, batch2, batch2Error, batch3, batch3Error, disposeCalls: store.disposeCalls }
}
`
  const dir = mkdtempSync(join(tmpdir(), 'mutation-recipe-ingest-'))
  const modulePath = join(dir, 'scenario.ts')
  writeFileSync(modulePath, moduleSource)
  const mod = await import(pathToFileURL(modulePath).href)
  return mod.scenario()
}

const patchText = readFileSync(patchFileArg, 'utf8')
const result = await runThreeBatchIngestScenarioUnsafe(patchText)
console.log(
  `RESULT batch1.appended=${result.batch1.appended} `
  + `batch2Error=${JSON.stringify(result.batch2Error ?? null)} `
  + `batch3Error=${JSON.stringify(result.batch3Error ?? null)} `
  + `disposeCalls=${result.disposeCalls}`,
)
