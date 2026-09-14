// #1163: behavioral guard for appendHarnessEvents (dsh-edge/patches/
// 0004-harness-ingest.patch) against dsh-edge 0.14.0's resident-agent cache.
// Runs the REAL, extracted patch method (not a paraphrase) against a stub
// that models the one upstream fact this fix depends on: openAgentForTurn
// returns the SAME cached handle for a sessionId across calls, and a
// disposed handle is dead. See dsh-edge/verify-ingest-resident-safety.mjs
// for the honest boundary on what is/isn't verified against real upstream
// source.
//
// #1161: the second assertion below (coldLoads) is a DIFFERENT property from
// #1163's — not "the handle stays alive" but "the O(full session history)
// cold-resume load this fix collapses is paid at most once per session, not
// once per ingest call". docs/research/11-dsh-edge.md ("resident agents")
// documents the 0.14.0 mechanism this relies on (upstream's own resident
// cache pays the cold-resume cost once per session per DO activation); the
// ≈427 rows_read/event figure in docs/research/20-cloudflare-free.md predates
// the 0.14.0 pin and its resident-agent model — NOT re-verified against the
// current pin, so this comment does not claim it as the confirmed dominant
// term, only as the mechanism this guard targets (issue #1161 carries the
// number/estimate discussion, not this file). A fix could satisfy #1163
// (handle never dies) while still opening a FRESH session/handle on every
// call (still correct, still O(history) per call, rows_read unchanged) — the
// coldLoads assertion is what tells those two apart; disposeCalls alone does
// not.
//
// Mutation proof (do this by hand before trusting the guard, per AGENTS.md
// "поведенческий тест находит то, чего структурный не видит" — RUN it, do
// not infer the numbers from reading the diff; that inference is exactly
// what went stale here once before, see PR #1173 review history):
//   1. Reintroduce `finally { await handle.dispose()... }` around the tail of
//      appendHarnessEvents in the patch text (the pre-#1163 shape, git
//      history d239e324~1) -> the #1163 assertions go red: batch2Error is
//      the "not live" message (not undefined), and disposeCalls is 3 (not
//      0) — the pre-#1163 `finally` sits around the WHOLE try body (session
//      read, append, flush), so it fires on batch1 (clean), then AGAIN on
//      batch2 and batch3 even though each of those throws before reaching
//      flush (dispose still runs in `finally` on the exception path).
//      batch3 is NOT skipped: it runs against the same already-disposed
//      cached handle and fails with the same "not live" message as batch2 —
//      there is no short-circuit between batches in the scenario.
//   2. Instead, keep the fix but make the stub's own `openAgentForTurn`
//      always create a fresh session (drop the `if (handle === undefined)`
//      short-circuit in ResidentStubStore, i.e. stop caching in `residents`)
//      -> the #1161 assertion goes red: coldLoads is 3, not 1, while the
//      #1163 assertions stay green (disposeCalls is still 0, no batch
//      throws) — proving the two properties are independent and the second
//      one needs its own guard, not a restatement of the first.
//   3. Restore the fix -> all assertions green again.
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

test('#1161: три ingest-батча в одну сессию платят полный ре-скан истории один раз, не три', async () => {
  const { batch3, batch3Error, coldLoads } = await runTwoBatchIngestScenario()
  assert.equal(batch3Error, undefined, `батч 3 в ту же сессию обязан пройти: ${batch3Error}`)
  assert.equal(batch3?.appended, 2, 'батч 3: оба события приняты')
  assert.equal(coldLoads, 1, 'резидентный кэш обязан обслужить все три батча ОДНИМ холодным резюме сессии — рост здесь означает, что ingest снова платит rows_read полного ре-скана истории на каждый вызов (#1161)')
})
