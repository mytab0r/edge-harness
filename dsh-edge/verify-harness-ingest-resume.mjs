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
// type-only construct). By construction this test cannot pass against a patch
// that reverted to "always cold-resume": the mutation proof is running the
// literal extracted function, not asserting the function's name exists (the
// class of gap "Поведенческий тест находит то, чего структурный не видит",
// AGENTS.md, #891/#893).
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
  console.log(`Извлечено planHarnessIngestResume (${plan.split('\n').length} строк), advanceHarnessIngestBaseTurn (${advance.split('\n').length} строк) из ${patchPath}`)
}
