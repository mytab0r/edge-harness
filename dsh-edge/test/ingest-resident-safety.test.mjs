// #1163: behavioral guard for appendHarnessEvents (dsh-edge/patches/
// 0004-harness-ingest.patch) against dsh-edge 0.14.0's resident-agent cache.
// Runs the REAL, extracted patch method (not a paraphrase) against a stub
// that models the one upstream fact this fix depends on: openAgentForTurn
// returns the SAME cached handle for a sessionId across calls, and a
// disposed handle is dead. See dsh-edge/verify-ingest-resident-safety.mjs
// for the honest boundary on what is/isn't verified against real upstream
// source.
//
// #1161: TWO stub observables below carry properties DISTINCT from #1163's,
// and each names what it actually measures (ai-review PR #1173, rounds 2-3):
//   - `coldLoads` — the stub's resident handle is REUSED across calls. This
//     is a property upstream 0.14.x already guarantees via its own resident
//     cache (docs/research/11-dsh-edge.md, "resident agents": the cold
//     do-session-persistence.ts::eventRows(id, 0) rescan is paid once per
//     session per DO activation, not once per ingest call); the ≈427
//     rows_read/event figure in docs/research/20-cloudflare-free.md predates
//     the 0.14.0 pin and its resident-agent model — NOT re-verified against
//     the current pin, and this file does not claim it as the confirmed
//     dominant term (issue #1161 carries the number/estimate discussion).
//   - `scans` — how many times the EXTRACTED PATCH TEXT calls
//     snapshotEvents() across the three batches. THIS is what the #1161
//     baseTurn cache in patch 0004 changes: without it every ingest call
//     rescans the resident history (CPU per call, and the per-call history
//     pass #1049/#1161 were written against). Reverting the cache leaves
//     coldLoads at 1 (resident still reused) — only scans sees it.
// Honest boundary (round 2 finding, kept): both counters live entirely in
// the hand-written stub — they read nothing from upstream, so they catch
// edits to the patch TEXT or to the stub itself, not a real drift in
// dsh-edge's resident-cache lifetime after a pin bump (upstream_drift.py's
// pin-bump automation does not re-verify this guard; see the fuller boundary
// note in verify-ingest-resident-safety.mjs next to the counters).
//
// The third test exercises the cache's INVALIDATION: with
// opts.foreignNativeTurnBetweenBatches a foreign (non-ingest) writer appends
// a native turn 50 between batches, and the stored turns must CONTINUE after
// it (51, 51, ...) — the method's own docstring contract "a reused session
// never rewinds" (ai-review PR #1173, round 3, finding 2: the first cache
// shape, a bare cached turn with no seq, silently rewound the log under the
// foreign turn).
//
// Mutation proof (do this by hand before trusting the guard, per AGENTS.md
// "поведенческий тест находит то, чего структурный не видит" — RUN it, do
// not infer the numbers from reading the diff; that inference is exactly
// what went stale here once before, see PR #1173 review history). Steps 1-5
// below were EXECUTED 2026-09-17 on this PR (verbatim outputs in the PR
// body); steps 1-2 mutate different layers, steps 3-5 mutate the patch's
// cache. Step 1 needs the bypass runner from
// scripts/lib/test/ingest-mutation-scenario.mjs; steps 2-5 are plain text
// edits + `node --test`:
//   1. Reintroduce `finally { await handle.dispose()... }` around the tail of
//      appendHarnessEvents in the patch text (the pre-#1163 shape, git
//      history d239e324~1). LITERAL FORM (matching `assertNoUnconditionalDispose`
//      substring): structural guard `assertNoUnconditionalDispose` throws
//      immediately — behavioural assertions not reached. Bypass it the way
//      scripts/lib/test/ingest-mutation-scenario.mjs does (same extractor,
//      structural check off, harness kept) — EXECUTED 2026-09-17 against
//      d239e324~1 with that runner: batch2Error and batch3Error are both
//      `session "ingest-check" is not live in this store`, disposeCalls = 3
//      (finally runs on every batch, batch3 EXECUTES and fails — no
//      short-circuit). The same historical pair is executed mechanically in
//      CI by scripts/ci/guards/mutation-recipe-execution-guard.sh (#1194).
//      NOTE: a NON-finally dispose placed before `sessions.flush` instead
//      counts 1 — batches 2-3 die at `handle.agent` before reaching it; the
//      number 3 refers to the historical finally shape.
//   2. Instead, keep the fix but make the stub's own `openAgentForTurn`
//      always create a fresh session (drop the `if (handle === undefined)`
//      short-circuit in ResidentStubStore, i.e. stop caching in `residents`)
//      -> the #1161 assertion goes red: coldLoads is 3, not 1 (scans is 3
//      too — every fresh session scans once), while the #1163 assertions
//      stay green (disposeCalls is still 0, no batch throws) — proving the
//      two properties are independent and the second one needs its own
//      guard, not a restatement of the first.
//   3. PATCH-level (round 3, finding 1): revert the baseTurn cache inside
//      appendHarnessEvents to the pre-PR unconditional rescan (e.g.
//      `const cachedBaseTurn = sessionAny.__harnessBaseTurnCache` ->
//      `const cachedBaseTurn = undefined`, dropping the cache hit) ->
//      scans is 3, not 1: the scans assertion in test 2 goes red, while
//      coldLoads stays 1 and the #1163 assertions stay green — the coldLoads
//      counter alone does NOT catch this mutant.
//   4. PATCH-level (round 3, finding 2): break the seq-keyed invalidation
//      (e.g. `cachedBaseTurn.seq === session.seq` -> `true` — cache hits
//      regardless of foreign writes) and rerun test 3 (foreign write) ->
//      stored turns rewind under the foreign turn 50 (batch 2 numbered
//      2, 2 instead of 51, 51): the foreign-write test goes red.
//   5. PATCH-level (round 4 checklist — seq fail-safe): collapse the
//      `seqIsCursor` definition to a constant (`const seqIsCursor = true` —
//      BOTH the read gate and the cache write lose the type check) and rerun
//      the missing-seq scenario (test 4) -> the cache stores
//      { seq: undefined, ... } after batch 1 and then hits on
//      `undefined === undefined` forever: turns rewind under the foreign
//      turn (1,1,50,50,2,2,3,3) and the test goes red on `turns` (the scans
//      check after it would fail too — the mutant scans once — but `turns`
//      fires first). NOTE: reverting ONLY the read-side predicate to the
//      bare `cachedBaseTurn.seq === session.seq` stays GREEN — the write
//      side still gates on seqIsCursor and never stores a cursorless entry;
//      the single `seqIsCursor` definition is the one place both gates share
//      (executed 2026-09-17: that partial mutant passed all four tests).
//   6. Restore the fix -> all assertions green again.
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

test('#1161: три ingest-батча в одну сессию сканируют историю один раз (кэш baseTurn), резидент переиспользуется', async () => {
  const { batch3, batch3Error, coldLoads, scans } = await runTwoBatchIngestScenario()
  assert.equal(batch3Error, undefined, `батч 3 в ту же сессию обязан пройти: ${batch3Error}`)
  assert.equal(batch3?.appended, 2, 'батч 3: оба события приняты')
  assert.equal(coldLoads, 1, 'стаб резидентного кэша обязан обслужить все три батча ОДНИМ "холодным" openAgentForTurn — рост счётчика означает, что ЭКСТРАГИРОВАННЫЙ ТЕКСТ ПАТЧА снова открывает сессию/хэндл на каждый вызов (сам счётчик не читает апстрим и не ловит его дрейф; про реальные rows_read см. verify-ingest-resident-safety.mjs) (#1161)')
  assert.equal(scans, 1, 'история сессии сканируется ОДИН раз на три батча: кэш baseTurn в патче 0004 переиспользуется между вызовами. Рост до 3 = кэш откатился/вырезан и ЭКСТРАГИРОВАННЫЙ ТЕКСТ ПАТЧА снова делает O(история) проход на КАЖДЫЙ батч — coldLoads этот мутант НЕ видит (резидентов уже обеспечивает апстрим 0.14.x). Счётчик живёт в стабе и не измеряет реальные rows_read (#1161, ai-review PR #1173 круг 3)')
})

test('#1161: чужая запись в сессию между батчами инвалидирует кэш baseTurn — нумерация продолжается, не откатывается', async () => {
  const { batch2Error, batch3Error, turns, scans } = await runTwoBatchIngestScenario(undefined, { foreignNativeTurnBetweenBatches: true })
  assert.equal(batch2Error, undefined, `батч 2 после чужой записи обязан пройти: ${batch2Error}`)
  assert.equal(batch3Error, undefined, `батч 3 после чужой записи обязан пройти: ${batch3Error}`)
  assert.deepEqual(
    turns,
    [1, 1, 50, 50, 51, 51, 52, 52],
    `журнал обязан продолжиться после чужого поворота 50 (батч 2 = 51,51; батч 3 = 52,52; пары turn/start+turn/end одного поворота сохраняются), а не откатиться под него; получено: ${JSON.stringify(turns)} — контракт докстринга метода "a reused session never rewinds" (#1161, ai-review PR #1173 круг 3)`,
  )
  assert.equal(scans, 2, 'ровно ДВА скана истории: холодный на батче 1 + довызов после чужой записи; батч 3 обязан взять кэш (не 3 — кэш работает, не 1 — инвалидация по seq работает)')
})

test('#1161: кэш baseTurn полностью выключается, когда session.seq не курсор (fail-safe от переименования в апстриме)', async () => {
  // ai-review PR #1173, round 4 checklist: голый предикат
  // `cachedBaseTurn.seq === session.seq` молча зелёный, если апстрим
  // переименует/уберёт `seq` (undefined === undefined → вечный hit протухшего
  // кэша). Контракт: без валидного курсора кэш ВЫКЛЮЧЕН полностью — нумерация
  // продолжается за чужой записью (безусловный ре-скан, до-кэшное поведение),
  // а не откатывается под неё.
  const { batch2Error, batch3Error, turns, scans } = await runTwoBatchIngestScenario(undefined, { foreignNativeTurnBetweenBatches: true, missingSeqOnSession: true })
  assert.equal(batch2Error, undefined, `батч 2 после чужой записи обязан пройти: ${batch2Error}`)
  assert.equal(batch3Error, undefined, `батч 3 после чужой записи обязан пройти: ${batch3Error}`)
  assert.deepEqual(
    turns,
    [1, 1, 50, 50, 51, 51, 52, 52],
    `без валидного session.seq нумерация ОБЯЗАНА продолжиться после чужого поворота 50 (fail-safe: кэш выключен → безусловный ре-скан видит весь журнал), а не откатиться под неё через вечный hit протухшего кэша на undefined === undefined; получено: ${JSON.stringify(turns)} (#1161, ai-review PR #1173 круг 4)`,
  )
  assert.equal(scans, 3, 'кэш обязан быть ПОЛНОСТЬЮ выключен без валидного курсора: каждый из трёх батчей сканирует историю заново (scans 3, не 1) — выключенный кэш это наблюдаемое состояние фейл-сейфа, а не молчаливая деградация (#1161, ai-review PR #1173 круг 4)')
})
