#!/usr/bin/env node
// #1049: appendHarnessEvents used to cold-resume (openAgentForTurn -> agents.resume
// -> upstream persistence.prepare -> loadStored, which reads the WHOLE stored
// session log — do-session-persistence.ts::eventRows issues an unconditional
// `SELECT ... WHERE session_id = ? ORDER BY seq` with no LIMIT) on EVERY single
// ingest call, then disposed the handle in a `finally` — so the read cost grew
// with total session length on every batch, not just the first. The fix keeps a
// resumed handle warm across batches for the same session
// (dsh-edge/patches/0004-harness-ingest.patch, planHarnessIngestResume) and
// advances baseTurn incrementally instead of rescanning the log.
//
// This module extracts the two pure functions that carry that decision
// (planHarnessIngestResume, advanceHarnessIngestBaseTurn) VERBATIM out of the
// patch's unified diff — not a paraphrase — so a behavioral test
// (dsh-edge/test/harness-ingest-resume.test.mjs) can import and execute the
// REAL patch source under Node's built-in TypeScript type-stripping (Node 24;
// both functions use only erasable generics/annotations, no runtime-relevant
// type-only construct).
//
// Coverage is two halves, stated honestly (ревью PR #1057, блокер 2):
// 1. BEHAVIORAL — the literal extracted functions are executed, so their
//    semantics (cold resume only on the first call, warm reuse, idle
//    eviction, exact baseTurn) cannot pass against a mutated implementation.
// 2. STRUCTURAL — assertHarnessIngestWiring reads the appendHarnessEvents
//    method body out of the same patch and checks the wiring that no unit
//    test can reach (the method lives on the upstream EdgeSessionStore class,
//    un-instantiable in isolation): planHarnessIngestResume is CALLED, its
//    result is CONSUMED (plan.entry), the entry is CACHED via
//    harnessIngestHandles.set, baseTurn is advanced incrementally, and the
//    pre-#1049 `finally { …handle.dispose() }` after-batch shape is GONE.
//    Without this half, a patch that survives an upstream rebase with the
//    wiring silently rolled back (plan called, result ignored, entry always
//    undefined — cold resume on EVERY call again) passes the behavioral half
//    green (класс #891/#893, «гвардия ложно-зелёная»); the structural half
//    catches exactly that mutation.
import { readFileSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
export const patchPath = join(root, 'dsh-edge', 'patches', '0004-harness-ingest.patch')

/**
 * Pull one top-level `export function <name>(...) { ... }` block added by the
 * patch (a contiguous run of `+`-prefixed lines in the unified diff), by
 * brace-depth counting on the raw added lines. Throws loudly — not a silent
 * empty string — if the marker is missing or the block never closes within
 * added lines: either means the patch text moved and this extractor no longer
 * reads what it claims to (same "тихий ноль" class as verify-ingest-allowlist.mjs).
 * @param {string} patch - full unified diff text.
 * @param {string} name - exported function name (no `export function` prefix).
 * @returns {string} the function's source, de-prefixed of the diff's leading `+`.
 */
export function extractPatchFunction(patch, name) {
  const marker = `\n+export function ${name}`
  const at = patch.indexOf(marker)
  if (at === -1) throw new Error(`патч 0004: не найден маркер «export function ${name}» — патч уехал от этого экстрактора`)
  const lines = patch.slice(at + 1).split('\n')
  const collected = []
  let depth = 0
  let opened = false
  for (const line of lines) {
    if (!line.startsWith('+')) {
      throw new Error(`патч 0004: блок «${name}» не закрылся внутри добавленных строк (не-diff-«+»-строка до баланса скобок) — патч уехал от этого экстрактора`)
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
    throw new Error(`патч 0004: блок «${name}» не закрылся (незакрытая скобка) — патч уехал от этого экстрактора`)
  }
  return collected.join('\n')
}

/**
 * Pull one class-method block added by the patch (a contiguous run of
 * `+`-prefixed lines), located by its declaration marker — e.g.
 * `async appendHarnessEvents(` — by brace-depth counting on the raw added
 * lines. Same loud-failure contract as extractPatchFunction: a missing
 * marker, a non-diff line before brace balance, or an unbalanced block each
 * throw — the patch text moved and this extractor no longer reads what it
 * claims to. Note the brace counting tolerates `${…}` interpolations (each
 * contributes one balanced `{`/`}` pair) — the method's template literals
 * carry no other brace-bearing syntax.
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
 * Structural half of the #1049 guard (ревью PR #1057, блокер 2): the wiring
 * of the warm-handle cache INSIDE appendHarnessEvents. The behavioral tests
 * execute the two pure functions verbatim, but the method that CALLS them
 * lives on the upstream EdgeSessionStore class and cannot be instantiated in
 * isolation — and patch 0004 is hand-written, it survives upstream re-bumps,
 * so losing the wiring there (plan called but result ignored — entry always
 * undefined, cache never filled, handle disposed after every batch again) is
 * the most likely regression path, and it passed green without this check.
 * Throws loudly naming the broken requirement; returns the method body so the
 * caller can chain further assertions.
 * @param {string} patch - full unified diff text (defaults to the real patch on disk).
 * @returns {string} the extracted appendHarnessEvents method source.
 */
export function assertHarnessIngestWiring(patch = readFileSync(patchPath, 'utf8')) {
  const body = extractPatchMethod(patch, 'async appendHarnessEvents(')
  const requirements = [
    [`planHarnessIngestResume(`, 'план ресума запрошен (planHarnessIngestResume вызван)'],
    [`let entry = plan.entry`, 'результат плана ПОТРЕБЛЁН (let entry = plan.entry) — «вызвал и выбросил» = cold resume на каждый вызов, pre-#1049'],
    [`this.harnessIngestHandles.set(id, entry)`, 'entry положен в кэш тёплых хэндлов (harnessIngestHandles.set)'],
    [`entry.baseTurn = advanceHarnessIngestBaseTurn(`, 'baseTurn двинут инкрементально (advanceHarnessIngestBaseTurn), а не пересканирован по истории'],
    [`await releaseStale(plan.staleForId)`, 'вытесненная запись ТЕКУЩЕЙ сессии диспоузится ДО холодного ресума (await releaseStale(plan.staleForId)) — иначе reopen того же id может получить BUSY на цикл (ревью PR #1057, чеклист)'],
    [`this.harnessIngestHandles.delete(id)`, 'сбой между append и flush выселяет тёплый хэндл из кэша (catch { … harnessIngestHandles.delete(id) }) — иначе ретрай дрена того же батча дописывает его в ту же in-memory сессию второй раз, и следующий успешный flush персистит обе копии (ревью PR #1057, второй раунд, блокер 2)'],
  ]
  for (const [needle, message] of requirements) {
    if (!body.includes(needle)) {
      throw new Error(`патч 0004: проводка #1049 в appendHarnessEvents сломана — ${message}`)
    }
  }
  if (body.includes('finally')) {
    throw new Error('патч 0004: проводка #1049 в appendHarnessEvents сломана — в методе снова finally { …dispose() } (pre-#1049 shape: хэндл диспоузится после каждого батча)')
  }
  return body
}

/**
 * Materialize the extracted functions as a real, importable module — a temp
 * `.ts` file Node's own type-stripping loader executes directly (Node 24, no
 * build step, no reimplementation). Both functions are self-contained (no
 * imports from the rest of session-store.ts), so concatenation is sufficient.
 * @param {string} patch - full unified diff text (defaults to the real patch on disk).
 * @returns {Promise<{ planHarnessIngestResume: Function, advanceHarnessIngestBaseTurn: Function }>}
 */
export async function loadHarnessIngestResumeModule(patch = readFileSync(patchPath, 'utf8')) {
  const source = [
    extractPatchFunction(patch, 'planHarnessIngestResume'),
    extractPatchFunction(patch, 'advanceHarnessIngestBaseTurn'),
  ].join('\n\n')
  const dir = mkdtempSync(join(tmpdir(), 'harness-ingest-resume-'))
  const file = join(dir, 'extracted.ts')
  writeFileSync(file, source, 'utf8')
  return import(pathToFileURL(file).href)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const patch = readFileSync(patchPath, 'utf8')
  const plan = extractPatchFunction(patch, 'planHarnessIngestResume')
  const advance = extractPatchFunction(patch, 'advanceHarnessIngestBaseTurn')
  const method = assertHarnessIngestWiring(patch)
  console.log(`Извлечено planHarnessIngestResume (${plan.split('\n').length} строк), advanceHarnessIngestBaseTurn (${advance.split('\n').length} строк), appendHarnessEvents (${method.split('\n').length} строк, проводка #1049 проверена) из ${patchPath}`)
}
