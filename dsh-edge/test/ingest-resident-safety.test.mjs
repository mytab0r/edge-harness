// #1163: behavioral guard for appendHarnessEvents (dsh-edge/patches/
// 0004-harness-ingest.patch) against dsh-edge 0.14.0's resident-agent cache.
// Runs the REAL, extracted patch method (not a paraphrase) against a stub
// that models the one upstream fact this fix depends on: openAgentForTurn
// returns the SAME cached handle for a sessionId across calls, and a
// disposed handle is dead. See dsh-edge/verify-ingest-resident-safety.mjs
// for the honest boundary on what is/isn't verified against real upstream
// source.
//
// Mutation proof (do this by hand before trusting the guard, per AGENTS.md
// "поведенческий тест находит то, чего структурный не видит"):
//   1. Reintroduce `finally { await handle.dispose()... }` around the tail of
//      appendHarnessEvents in the patch text (the pre-#1163 shape) -> both
//      tests below go red: batch2Error is the "not live" message, not
//      undefined, and disposeCalls is 1, not 0.
//   2. Restore the fix -> both tests green again.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { runTwoBatchIngestScenario } from '../verify-ingest-resident-safety.mjs'

test('второй ingest-батч в ту же сессию не получает мёртвый резидент', async () => {
  const { batch1, batch2, batch2Error, disposeCalls } = await runTwoBatchIngestScenario()
  assert.equal(batch1.appended, 2, 'батч 1: оба события приняты')
  assert.equal(batch2Error, undefined, `батч 2 в ту же сессию обязан пройти, а не воспроизвести #1163: ${batch2Error}`)
  assert.equal(batch2?.appended, 2, 'батч 2: оба события приняты')
  assert.equal(disposeCalls, 0, 'appendHarnessEvents не диспоузит резидентный хэндл апстрима (0.14.0)')
})
