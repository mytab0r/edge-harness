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
// The exact wording `"is not live in this store"` and the throw site ARE an
// observed fact, not a guess: `gh run view 34773180930 --log-failed` shows
// `Error: session "ingest-check" is not live in this store` thrown from
// `Proxy.liveEntryFor`, called from `Proxy.flush`, called from
// `EdgeSessionStore.appendHarnessEvents` — batch 2 dies inside
// `sessions.flush(session)` (the live-downlink publish path), not while
// opening the handle. Honest boundary: WHY disposing the handle breaks that
// later flush — the resident-cache mechanism (openAgentForTurn now a
// @deprecated alias of getOrResumeAgent, which returns the same cached
// handle per sessionId; dispose() does not remove it from that cache) — is
// an explanatory model from docs/research/11-dsh-edge.md (a literal upstream
// docstring quote), NOT verified byte-for-byte against the upstream
// `session-store.ts` source on the 0.14.0 pin — dsh-edge is not vendored in
// this repository (PATCHES.md, bump #1138 entry). The stub below reproduces
// the OBSERVABLE effect (batch 1 OK, batch 2 on the same session fails
// immediately with the exact real error text, no idle delay involved) via a
// simplified stand-in (a disposed handle's `.agent` getter throws), not by
// literally re-implementing `liveEntryFor`/`flush` — sufficient to prove the
// fix (stop disposing) by mutation, without overclaiming an exact internal
// call-path match.
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
 * plus a minimal stub harness, and run a THREE-batch ingest scenario (kept the
 * name `runTwoBatchIngestScenario` from #1163 — the third batch, #1161, only
 * strengthens the same call, it does not change what the function proves)
 * against the SAME session. Returns the outcome of all three batches plus
 * three stub observables so the test can assert on:
 *   - `disposeCalls` — #1163 correctness property (no dead resident);
 *   - `coldLoads`    — resident REUSE within the stub (a property upstream
 *     0.14.x already guarantees via its own resident cache, research/11 —
 *     this counter does NOT measure real rows_read and cannot see upstream
 *     lifetime drift, see the honest boundary at the counter's definition);
 *   - `scans`        — how many times the EXTRACTED PATCH TEXT calls
 *     snapshotEvents(). This is the property the #1161 baseTurn cache
 *     actually changes: with the cache, one scan serves all three batches;
 *     with the cache reverted to an unconditional per-call rescan, scans is
 *     3 while coldLoads stays 1 — coldLoads alone does NOT see that mutation
 *     (verified by execution, ai-review PR #1173 round 3, finding 1).
 * With opts.foreignNativeTurnBetweenBatches, a NON-ingest writer appends a
 * native turn/start+turn/end pair (turn 50) straight to the resident session
 * log between batch 1 and batch 2 — the #572 "resumed for a native turn"
 * case — and `turns` (every stored event's turn, in order) lets the test pin
 * the cache's seq-keyed invalidation: batch 2 must renumber AFTER the foreign
 * turn, not rewind under it. Behavioral, not structural: the invariant lives
 * in the executed patch text. NOT re-confirmed as the dominant share of
 * #1161's measured 150,898 rows_read/run on the current pin — that
 * number/estimate lives in issue #1161, not here. Never swallows a
 * scenario-setup error.
 * @param {string} patch - full unified diff text (defaults to the real patch on disk).
 * @param {{foreignNativeTurnBetweenBatches?: boolean}} [opts]
 * @returns {Promise<{batch1: {appended: number}, batch2Error: string|undefined, batch3Error: string|undefined, disposeCalls: number, coldLoads: number, scans: number, turns: Array<number|undefined>}>}
 */
export async function runTwoBatchIngestScenario(
  patch = readFileSync(patchPath, 'utf8'),
  opts = { foreignNativeTurnBetweenBatches: false },
) {
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
  // Direct session registry for the scenario's readout: lets the stored-log
  // observation survive mutants that dispose the resident (the handle's
  // .agent getter throws on a disposed resident, which would mask the
  // batch-level observations the mutation recipe asserts on).
  sessions = new Map<string, any>()
  disposeCalls = 0
  // #1161: this counter tracks how many times the stub's openAgentForTurn
  // creates a fresh session (i.e. a "cold load" in the stub). In the real
  // upstream, the equivalent cost is do-session-persistence.ts::eventRows(id, 0)
  // which the resident cache pays at most once per sessionId per DO activation
  // (docs/research/11-dsh-edge.md, "resident agents"), not once per ingest call.
  // This stub counter DOES NOT measure real rows_read or upstream lifetime —
  // it only detects (a) edits to the extracted patch TEXT that reopen a fresh
  // handle per call, or (b) edits to this stub that stop caching.
  //
  // Honest boundary on detection power (ai-review PR #1173, round 2 finding):
  // this counter lives entirely in THIS hand-written stub — it reads nothing
  // from upstream (dsh-edge is not vendored here, PATCHES.md). It goes red
  // mechanically for exactly two things: (a) an edit to the extracted patch
  // TEXT that makes appendHarnessEvents open a fresh session/handle per call
  // again (same effect as the pre-#1163 per-batch dispose(), different code
  // path), or (b) an edit to this stub itself that stops caching in the
  // residents map above. It CANNOT see a real change to upstream's
  // resident-cache lifetime (e.g. a pin bump that shortens how long a
  // resident survives between calls) — upstream_drift.py's pin-bump
  // automation does not re-verify this stub against the new dsh-edge source;
  // that has to happen by hand at each bump (see docs/research/11-dsh-edge.md
  // before assuming this guard still matches reality on a new pin).
  coldLoads = 0
  // #1161 guard, round 3 (ai-review PR #1173, finding 1): counts calls to
  // session.snapshotEvents() made by the EXTRACTED PATCH TEXT — the property
  // the #1161 baseTurn cache in patch 0004 actually changes. coldLoads above
  // does NOT see a revert of that cache: upstream's resident cache already
  // keeps the handle warm (coldLoads stays 1) while appendHarnessEvents pays
  // the O(history) scan again on every batch. Verified by execution in review
  // round 3: reverting the cache to the unconditional pre-PR rescan passes
  // coldLoads === 1 green; scans goes 1 -> 3. Same honest boundary as
  // coldLoads: lives entirely in this stub, reads nothing from upstream —
  // it pins the PATCH TEXT, not real rows_read.
  scans = 0
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
      this.coldLoads += 1
      const store = this
      const session = {
        seq: 1,
        log: [] as Array<{ type: string; data: unknown }>,
        snapshotEvents(): Array<{ type: string; data: unknown }> {
          store.scans += 1
          return this.log
        },
        append(type: string, data: unknown, _opts?: unknown) {
          this.log.push({ type, data })
          const event = { type, data, seq: this.seq }
          this.seq += 1
          return event
        },
      }
      this.sessions.set(id, session)
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
  const opts = ${JSON.stringify(opts)}
  const batch1 = await store.appendHarnessEvents(sessionId, [
    { type: 'turn/start', data: { turn: 1 } },
    { type: 'turn/end', data: { turn: 1 } },
  ])
  // Foreign (non-ingest) writer between batches: a native turn in a resumed
  // viewer session (#572). Appended through the session log directly, NOT
  // through appendHarnessEvents — exactly what a native turn does upstream.
  if (opts.foreignNativeTurnBetweenBatches === true) {
    const resident = store.residents.get(sessionId)
    if (resident === undefined) throw new Error('сценарий: резидент не найден перед чужой записью')
    const foreignSession = resident.agent.session
    foreignSession.append('turn/start', { turn: 50 })
    foreignSession.append('turn/end', { turn: 50 })
  }
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
  // #1161: a third batch to the SAME session — one repeat is not enough to
  // tell "paid once" from "paid every other call"; a third call still
  // hitting the resident (coldLoads staying at 1) is the minimum needed to
  // distinguish those two.
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
  // Stored-log readout for the foreign-write invariant: every event's turn in
  // order, so the test can see renumbering (continue) or rewind directly.
  const turns = (store.sessions.get(sessionId).log as Array<{ data: { turn?: number } }>).map(
    (event) => event.data.turn,
  )
  return {
    batch1, batch2, batch2Error, batch3, batch3Error,
    disposeCalls: store.disposeCalls, coldLoads: store.coldLoads, scans: store.scans,
    turns,
  }
}
`
  const dir = mkdtempSync(join(tmpdir(), 'dsh-edge-ingest-resident-'))
  const modulePath = join(dir, 'scenario.ts')
  writeFileSync(modulePath, moduleSource)
  const mod = await import(pathToFileURL(modulePath).href)
  return mod.scenario()
}
