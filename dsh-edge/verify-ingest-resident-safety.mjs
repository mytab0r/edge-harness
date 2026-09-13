#!/usr/bin/env node
// #1163: dsh-edge 0.14.0 rebuilt the agent lifetime in `session-store.ts`
// around a resident cache (docs/research/11-dsh-edge.md, "resident agents";
// upstream docstring quoted there: "The agent stays alive across turns in
// idle phase; only session deletion or DO shutdown disposes it").
// `openAgentForTurn` is now a `@deprecated` alias for `getOrResumeAgent`,
// which hands back the SAME cached handle for a given sessionId on every
// call. Patch 0004's `appendHarnessEvents` used to dispose that handle in a
// `finally` after every ingest batch — safe on the pre-0.14.0 lifetime, but
// on 0.14.0 `dispose()` does not remove the entry from the resident cache
// (only the upstream `disposeResidentAgent(id)` does), so the NEXT call for
// that sessionId got the same, now-dead resident back — the live prod break
// (first source-build deploy on the 0.14.0 pin, run 34773180930): ingest
// batch 1 succeeds, batch 2 into the SAME session fails immediately with
// `session "..." is not live in this store`.
//
// This module extracts the REAL `appendHarnessEvents` method body verbatim
// out of the patch's unified diff (not a paraphrase) and executes it as a
// real TypeScript module (Node's built-in type-stripping — the method's own
// type annotations/casts are all erasable syntax) against a stub
// `EdgeSessionStore`-like harness that models the ONE fact this fix depends
// on: `openAgentForTurn` returns the same cached handle object across calls
// for the same sessionId, and a disposed handle is unusable afterwards.
//
// Honest boundary: the exact wording `"is not live in this store"` and the
// resident-cache internals are NOT verified byte-for-byte against the
// upstream `session-store.ts` source on the 0.14.0 pin — dsh-edge is not
// vendored in this repository (PATCHES.md, bump #1138 entry). The stub here
// is a faithful model of the mechanism as documented in research/11 (a
// literal upstream docstring quote), sufficient to reproduce the observed
// symptom (batch 1 OK, batch 2 on the same session fails immediately, no
// idle delay involved) and to prove the fix by mutation.
import { readFileSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
export const patchPath = join(root, 'dsh-edge', 'patches', '0004-harness-ingest.patch')

/**
 * Pull one class-method block added by the patch (a contiguous run of
 * `+`-prefixed lines), located by its declaration marker, by brace-depth
 * counting on the raw added lines. Throws loudly — not a silent empty string
 * — if the marker is missing or the block never closes within added lines:
 * either means the patch text moved and this extractor no longer reads what
 * it claims to (same "тихий ноль" class as verify-ingest-allowlist.mjs).
 * @param {string} patch - full unified diff text.
 * @param {string} marker - method declaration text to find, e.g. 'async appendHarnessEvents('.
 * @returns {string} the method's source, de-prefixed of the diff's leading `+`.
 */
export function extractPatchMethod(patch, marker) {
  const at = patch.indexOf(`\n+  ${marker}`)
  if (at === -1) throw new Error(`патч 0004: не найден маркер метода «${marker}» — патч уехал от этого экстрактора`)
  const lines = patch.slice(at + 1).split('\n')
  const collected = []
  let depth = 0
  let opened = false
  for (const line of lines) {
    if (!line.startsWith('+')) {
      throw new Error(`патч 0004: блок «${marker}» не закрылся внутри добавленных строк (не-diff-«+»-строка до баланса скобок) — патч уехал от этого экстрактора`)
    }
    const body = line.slice(1)
    collected.push(body)
    for (const ch of body) {
      if (ch === '{') { depth += 1; opened = true }
      else if (ch === '}') depth -= 1
    }
    if (opened && depth === 0) break
  }
  if (!opened || depth !== 0) {
    throw new Error(`патч 0004: блок «${marker}» не закрылся (незакрытая скобка) — патч уехал от этого экстрактора`)
  }
  return collected.join('\n')
}

/**
 * Structural guardrail (same class as #1049's own guard, `assertHarnessIngestWiring`
 * on PR #1057 — not merged, not reused here): the behavioral scenario below only
 * proves the CURRENT patch text is safe; it does not by itself stop someone from
 * reintroducing `handle.dispose()` in a way this extractor still parses but the
 * scenario happens not to exercise. Assert directly on the extracted text that the
 * dispose call from before this fix is GONE.
 * @param {string} body - extracted `appendHarnessEvents` method source.
 */
export function assertNoUnconditionalDispose(body) {
  if (body.includes('handle.dispose()')) {
    throw new Error(
      'патч 0004: appendHarnessEvents снова диспоузит хэндл после ingest-батча — '
      + 'на dsh-edge 0.14.0 это отравляет резидентный кэш апстрима для СЛЕДУЮЩЕГО '
      + 'вызова той же сессии (#1163, docs/research/11-dsh-edge.md)',
    )
  }
}

/**
 * Assemble a real, executable TypeScript module out of the extracted method
 * plus a minimal stub harness, and run the two-batch ingest scenario that
 * reproduces the live incident. Returns the outcome of both batches so the
 * test can assert on it; never swallows a scenario-setup error.
 * @param {string} patch - full unified diff text (defaults to the real patch on disk).
 * @returns {Promise<{batch1: {appended: number}, batch2Error: string|undefined, disposeCalls: number}>}
 */
export async function runTwoBatchIngestScenario(patch = readFileSync(patchPath, 'utf8')) {
  const methodBody = extractPatchMethod(patch, 'async appendHarnessEvents(')
  assertNoUnconditionalDispose(methodBody)

  const moduleSource = `
// Minimal stubs for the free variables appendHarnessEvents references that
// live OUTSIDE the extracted method (upstream classes/helpers this patch
// does not touch, and this fix does not touch either).
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
// Stand-in for the patch's own normalizeHarnessIngestEvent (unchanged by this
// fix, deliberately not re-extracted here): passthrough that renumbers turn
// the same way, enough to prove append/flush sequencing across two batches.
function normalizeHarnessIngestEvent(raw: any, baseTurn: number) {
  const data = { ...raw.data, turn: (raw.data.turn ?? 0) + baseTurn }
  return { type: raw.type, data, surface: true }
}

/**
 * Faithful stub of dsh-edge 0.14.0's resident-agent cache (research/11): one
 * shared handle per sessionId, returned as-is (even once disposed) on every
 * call — modeling upstream's own "cached !== undefined -> return cached".
 */
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
  return { batch1, batch2, batch2Error, disposeCalls: store.disposeCalls }
}
`
  const dir = mkdtempSync(join(tmpdir(), 'dsh-edge-ingest-resident-'))
  const modulePath = join(dir, 'scenario.ts')
  writeFileSync(modulePath, moduleSource)
  const mod = await import(pathToFileURL(modulePath).href)
  return mod.scenario()
}
