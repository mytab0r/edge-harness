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
// once per ingest call". That collapse is issue #1161's own dominant
// rows_read contributor (docs/research/20-cloudflare-free.md, "Причина цены
// ≈427 rows_read/событие... appendHarnessEvents на КАЖДЫЙ вызов... делает
// полный проход по всей истории сессии"). A fix could satisfy #1163 (handle
// never dies) while still opening a FRESH session/handle on every call
// (still correct, still O(history) per call, rows_read unchanged) — the
// coldLoads assertion is what tells those two apart; disposeCalls alone does
// not.
//
// Mutation proof (do this by hand before trusting the guard, per AGENTS.md
// "поведенческий тест находит то, чего структурный не видит"):
//   1. Reintroduce `finally { await handle.dispose()... }` around the tail of
//      appendHarnessEvents in the patch text (the pre-#1163 shape) -> the
//      #1163 assertions go red: batch2Error is the "not live" message, not
//      undefined, disposeCalls is 1 (not 0), and batch3 never runs (batch2
//      already threw when preparing session.append after a dead resident).
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
