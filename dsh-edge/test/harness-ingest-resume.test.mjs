// Поведенческие тесты #1049: appendHarnessEvents больше не должен делать
// O(вся история) skanshotEvents()-скан на каждый вызов ingest. Носитель
// исправления — planHarnessIngestResume/advanceHarnessIngestBaseTurn в
// dsh-edge/patches/0004-harness-ingest.patch, извлечённые ДОСЛОВНО из
// текущего патча (dsh-edge/verify-harness-ingest-resume.mjs) и выполненные
// как настоящий TypeScript-модуль под встроенным type-stripping Node 24 —
// не пересказ и не проверка «функция существует» (класс дефекта из
// AGENTS.md, «Поведенческий тест находит то, чего структурный не видит»,
// #891/#893): тест ловит именно поведение — сколько раз вызывающая сторона
// обязана платить холодным ресумом (O(N) чтение всего лога) на N батчей той
// же сессии, а не факт наличия имени функции в файле.
//
// Мутация (ручной прогон, зафиксирован в PR): временно заменить тело
// planHarnessIngestResume на "always entry: undefined" (эквивалент
// pre-#1049 поведения — cold resume на КАЖДЫЙ вызов) красит
// «повторный вызов той же сессии переиспользует хэндл» ниже; возврат тела —
// снова зелёный.
//
// Вторая половина покрытия — СТРУКТУРНАЯ (ревью PR #1057, блокер 2):
// проводка кэша внутри appendHarnessEvents недостижима поведенческим
// тестом (метод живёт на классе EdgeSessionStore, не инстанцируется
// изолированно), а рукописный патч переживает бампы апстрима — потеря
// проводки при ре-бампе (план вызван, результат выброшен, entry всегда
// undefined) проходила бы зелёным. assertHarnessIngestWiring ловит именно
// эту мутацию; сами проверки доказаны мутацией на синтетике ниже (и
// снятием проводки на реальном патче — прогон зафиксирован в PR).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  assertHarnessIngestWiring,
  extractPatchFunction,
  extractPatchMethod,
  loadHarnessIngestResumeModule,
} from '../verify-harness-ingest-resume.mjs'

const { planHarnessIngestResume, advanceHarnessIngestBaseTurn } = await loadHarnessIngestResumeModule()

/** Fake handle shaped like AgentHandle just enough for planHarnessIngestResume's contract. */
function fakeHandle(agentId) {
  let disposed = false
  return {
    agent: agentId,
    disposed: () => disposed,
    dispose: async () => { disposed = true },
  }
}

test('extractPatchFunction бросает громко, если маркер не найден (не тихий ноль)', () => {
  assert.throws(
    () => extractPatchFunction('+export function other() {\n+}\n', 'planHarnessIngestResume'),
    /не найден маркер/,
  )
})

test('первый вызов для новой сессии требует холодного ресума (entry === undefined)', () => {
  const cache = new Map()
  const plan = planHarnessIngestResume(cache, 's1', () => true, 1_000, 120_000)
  assert.equal(plan.entry, undefined, 'пустой кэш обязан требовать cold resume')
  assert.deepEqual(plan.evicted, [])
})

test('#1049 — ЖИВОЙ КЛАСС ДЕФЕКТА: повторный вызов той же сессии переиспользует уже открытый хэндл, не платит cold resume снова', () => {
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  cache.set('s1', { handle, baseTurn: 3, lastUsedMs: 1_000 })

  // Три последовательных батча ingest для ТОЙ ЖЕ сессии, разнесённые по
  // времени в пределах idle-окна: до фикса (#1049) КАЖДЫЙ из них требовал
  // openAgentForTurn -> agents.resume -> полное чтение лога. После фикса —
  // ни один: entry определён и совпадает с уже открытым хэндлом.
  let coldResumes = 0
  for (const nowMs of [2_000, 5_000, 30_000]) {
    const plan = planHarnessIngestResume(cache, 's1', cached => cached.handle === handle, nowMs, 120_000)
    if (plan.entry === undefined) coldResumes += 1
    else assert.equal(plan.entry.handle, handle, 'переиспользованный entry обязан указывать на тот же хэндл')
  }
  assert.equal(coldResumes, 0, 'три батча одной сессии не должны требовать ни одного холодного ресума')
  assert.equal(handle.disposed(), false, 'тёплый хэндл не диспоузится между батчами одной сессии')
})

test('идле-таймаут вытесняет тёплый хэндл и требует нового холодного ресума', () => {
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  cache.set('s1', { handle, baseTurn: 0, lastUsedMs: 1_000 })

  const idleMs = 120_000
  const stillWarm = planHarnessIngestResume(cache, 's1', () => true, 1_000 + idleMs - 1, idleMs)
  assert.notEqual(stillWarm.entry, undefined, 'на грани idle-окна хэндл ещё тёплый')

  const afterIdle = planHarnessIngestResume(cache, 's1', () => true, 1_000 + idleMs + 1, idleMs)
  assert.equal(afterIdle.entry, undefined, 'после idle-окна кэш обязан требовать cold resume')
  assert.equal(afterIdle.evicted.length, 1, 'вытесненный хэндл обязан вернуться вызывающей стороне для dispose()')
  assert.equal(afterIdle.evicted[0].handle, handle)
})

test('хэндл, переставший быть живым владельцем (архивирован/форкнут/выигран нативным ходом), не переиспользуется', () => {
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  cache.set('s1', { handle, baseTurn: 0, lastUsedMs: 1_000 })

  // isLiveOwner отражает agents.get(id) !== handle.agent — внешняя
  // диспоузация (архив/форк/выигранный гонкой нативный ход) сделала
  // закэшированный хэндл более не владельцем живой сессии.
  const plan = planHarnessIngestResume(cache, 's1', () => false, 1_500, 120_000)
  assert.equal(plan.entry, undefined, 'протухший (не-live-owner) хэндл не должен переиспользоваться')
  assert.equal(plan.evicted.length, 1, 'протухший хэндл обязан быть вытеснен и отдан на dispose()')
  assert.equal(cache.has('s1'), false, 'протухшая запись обязана быть удалена из кэша')
})

test('вытесняются ТОЛЬКО простроченные записи — соседняя активная сессия не трогается', () => {
  const cache = new Map()
  const stale = fakeHandle('STALE')
  const fresh = fakeHandle('FRESH')
  cache.set('stale-session', { handle: stale, baseTurn: 0, lastUsedMs: 1_000 })
  cache.set('fresh-session', { handle: fresh, baseTurn: 0, lastUsedMs: 100_000 })

  const plan = planHarnessIngestResume(cache, 'fresh-session', () => true, 130_000, 120_000)
  assert.equal(plan.evicted.length, 1)
  assert.equal(plan.evicted[0].handle, stale)
  assert.notEqual(plan.entry, undefined, 'соседняя свежая сессия остаётся тёплой')
  assert.equal(cache.has('fresh-session'), true)
  assert.equal(cache.has('stale-session'), false)
})

test('advanceHarnessIngestBaseTurn — ТОЧНЫЙ номер хода: максимум по батчу, никогда не назад', () => {
  assert.equal(advanceHarnessIngestBaseTurn(0, [0, 0, 1, 1, 2]), 2, 'обычный батч из нескольких ходов')
  assert.equal(advanceHarnessIngestBaseTurn(5, []), 5, 'батч без событий с полем turn не двигает baseTurn')
  assert.equal(advanceHarnessIngestBaseTurn(10, [3, 4]), 10, 'defensive max — не откатывается назад на меньших значениях')
})

test('advanceHarnessIngestBaseTurn — точность через несколько последовательных батчей (без пересчёта истории)', () => {
  // Симуляция трёх ingest-батчей одной сессии: каждый использует baseTurn,
  // унаследованный от предыдущего, БЕЗ единого обращения к прошлым событиям —
  // именно это заменяет O(N) snapshotEvents()-скан на каждый вызов.
  let baseTurn = 0
  baseTurn = advanceHarnessIngestBaseTurn(baseTurn, [0, 0, 1]) // batch 1: local turns 0..1
  assert.equal(baseTurn, 1)
  baseTurn = advanceHarnessIngestBaseTurn(baseTurn, [2, 2, 3]) // batch 2: continues from offset baseTurn=1
  assert.equal(baseTurn, 3)
  baseTurn = advanceHarnessIngestBaseTurn(baseTurn, [4]) // batch 3: single turn
  assert.equal(baseTurn, 4)
})

// --- Структурная половина: проводка #1049 внутри appendHarnessEvents ---

/** Wrap method body lines as a minimal fake diff the extractor can read.
 *  Несёт и фейковый planHarnessIngestResume: структурная половина гвардии
 *  проверяет проводку и в план-функции (in-flight-охрана idle-вытеснения),
 *  экстрактор обязан найти её маркер в любом синтетическом патче. */
const FAKE_PLAN_SOURCE = [
  // Ведущий \n обязателен: extractPatchFunction ищет маркер «\n+export function …».
  '\n+export function planHarnessIngestResume(cache, id, isLiveOwner, nowMs, idleMs) {',
  '+  const evicted = []',
  '+  let staleForId',
  '+  let staleForIdReason',
  '+  for (const [cachedId, cached] of cache) {',
  '+    if (nowMs - cached.lastUsedMs < idleMs) continue',
  '+    if (cached.inFlight) continue',
  '+    cache.delete(cachedId)',
  '+    evicted.push(cached)',
  '+    if (cachedId === id) { staleForId = cached; staleForIdReason = "idle" }',
  '+  }',
  '+  let entry = cache.get(id)',
  '+  if (entry !== undefined && !isLiveOwner(entry)) {',
  '+    cache.delete(id)',
  '+    evicted.push(entry)',
  '+    staleForId = entry',
  '+    staleForIdReason = "not-live-owner"',
  '+    entry = undefined',
  '+  }',
  '+  return { entry, evicted, staleForId, staleForIdReason }',
  '+}',
].join('\n')

function fakePatchWithMethod(lines) {
  return [
    FAKE_PLAN_SOURCE,
    '\n+  async appendHarnessEvents() {',
    ...lines.map(l => `+${l}`),
    '+  }',
    '+',
  ].join('\n')
}

const WIRING_OK = [
  '    const plan = planHarnessIngestResume(this.harnessIngestHandles, id, live, now, idle)',
  '    const releaseStale = async (stale) => { try { await stale.handle.dispose() } catch (e) {} }',
  '    for (const stale of plan.evicted) { if (stale === plan.staleForId) continue; void releaseStale(stale) }',
  '    if (plan.staleForId !== undefined) {',
  '      await releaseStale(plan.staleForId, plan.staleForIdReason ?? "unknown")',
  '    }',
  '    let entry = plan.entry',
  '    if (entry !== undefined && entry.inFlight) { throw new EdgeSessionStoreError("BUSY", "busy") }',
  '    if (entry === undefined) {',
  '      entry = { handle: await this.openAgentForTurn(id), baseTurn: 0, lastUsedMs: 0, inFlight: false }',
  '      this.harnessIngestHandles.set(id, entry)',
  '    }',
  '    entry.inFlight = true',
  '    try {',
  '      entry.baseTurn = advanceHarnessIngestBaseTurn(entry.baseTurn, turnsWritten)',
  '      await sessions.flush(session)',
  '    } catch (error) {',
  '      if (this.harnessIngestHandles.get(id) === entry) this.harnessIngestHandles.delete(id)',
  '      await releaseStale(entry)',
  '      throw error',
  '    }',
]

test('assertHarnessIngestWiring — РЕАЛЬНЫЙ патч проходит структурную проверку проводки', () => {
  const body = assertHarnessIngestWiring()
  assert.ok(body.includes('async appendHarnessEvents('), 'экстрактор обязан читать именно метод appendHarnessEvents')
})

test('extractPatchMethod бросает громко на отсутствующем маркере (не тихий ноль)', () => {
  assert.throws(() => extractPatchMethod('+export function other() {\n+}\n', 'async appendHarnessEvents('), /не найден маркер метода/)
})

test('МУТАЦИЯ ревью PR #1057: план вызван, результат выброшен — гвардия красная (pre-#1049 не проходит зелёным)', () => {
  // Точная мутация ревьюера: обе чистые функции нетронуты, проводка
  // откатена — entry всегда undefined, кэш не наполняется, хэндл
  // диспоузится после батча. Поведенческая половина здесь зелёная,
  // структурная обязана упасть.
  const mutated = fakePatchWithMethod([
    '    planHarnessIngestResume(this.harnessIngestHandles, id, live, now, idle)',
    '    const handle = await this.openAgentForTurn(id)',
    '    let baseTurn = 0',
    '    for (const event of handle.agent.session.snapshotEvents()) { /* полный скан на каждый вызов */ }',
    '    try { /* append events */ } finally { handle.dispose() }',
  ])
  assert.throws(() => assertHarnessIngestWiring(mutated), /проводка #1049 в appendHarnessEvents сломана/)
})

test('МУТАЦИЯ: entry не кладётся в harnessIngestHandles — гвардия красная', () => {
  const withoutSet = fakePatchWithMethod(WIRING_OK.filter(l => !l.includes('harnessIngestHandles.set')))
  assert.throws(() => assertHarnessIngestWiring(withoutSet), /harnessIngestHandles\.set/)
})

test('МУТАЦИЯ: baseTurn снова пересканируется (advance выкинут) — гвардия красная', () => {
  const withoutAdvance = fakePatchWithMethod(WIRING_OK.filter(l => !l.includes('advanceHarnessIngestBaseTurn')))
  assert.throws(() => assertHarnessIngestWiring(withoutAdvance), /advanceHarnessIngestBaseTurn/)
})

test('МУТАЦИЯ: вернулся pre-#1049 finally { …dispose() } — гвардия красная', () => {
  const withFinally = fakePatchWithMethod([...WIRING_OK, '    try { /* append */ } finally { entry.handle.dispose() }'])
  assert.throws(() => assertHarnessIngestWiring(withFinally), /finally \{ …dispose\(\) \}/)
})

test('МУТАЦИЯ: await-dispose вытесненной записи текущего id выкинут — гвардия красная (окно BUSY вернулось)', () => {
  const withoutAwait = fakePatchWithMethod(
    WIRING_OK.filter(l => !l.includes('await releaseStale(plan.staleForId')),
  )
  assert.throws(() => assertHarnessIngestWiring(withoutAwait), /releaseStale\(plan\.staleForId[,)]/)
})

test('МУТАЦИЯ: сбой между append и flush не выселяет тёплый хэндл из кэша — гвардия красная (блокер 2, ревью PR #1057, второй раунд)', () => {
  const withoutEviction = fakePatchWithMethod(
    WIRING_OK.filter(l => !l.includes('harnessIngestHandles.delete(id)')),
  )
  assert.throws(() => assertHarnessIngestWiring(withoutEviction), /harnessIngestHandles\.delete\(id\)/)
})

test('МУТАЦИЯ: in-flight-охрана выкинута целиком (и проверка, и выставление) — гвардия красная (блокер 1, ревью PR #1057, третий раунд)', () => {
  const withoutInFlight = fakePatchWithMethod(WIRING_OK.filter(l => !l.includes('entry.inFlight')))
  assert.throws(() => assertHarnessIngestWiring(withoutInFlight), /entry\.inFlight/)
})

test('МУТАЦИЯ: ТОЛЬКО проверка in-flight выкинута, entry.inFlight = true осталась — гвардия красная (живая находка: снятие только throw-ветки не ловится проверкой на голое вхождение подстроки)', () => {
  const withoutCheckOnly = fakePatchWithMethod(
    WIRING_OK.filter(l => !l.includes('entry !== undefined && entry.inFlight')),
  )
  assert.throws(() => assertHarnessIngestWiring(withoutCheckOnly), /проверен ПЕРЕД использованием/)
})

test('МУТАЦИЯ: idle-цикл снова вытесняет in-flight записи — гвардия красная (блокер, ревью PR #1057, четвёртый раунд)', () => {
  // Точная мутация ревьюера: из planHarnessIngestResume удалена строка
  // if (cached.inFlight) continue — idle-вытеснение снова смотрит только
  // на возраст, и dispose уходит под живой append/flush долгого батча.
  const withoutInFlightGuard = fakePatchWithMethod(WIRING_OK).replace(
    '+    if (cached.inFlight) continue\n',
    '',
  )
  assert.throws(
    () => assertHarnessIngestWiring(withoutInFlightGuard),
    /idle-вытеснение обязано пропускать in-flight записи/,
  )
})

test('planHarnessIngestResume возвращает staleForId — вытесненная из-под этого id запись помечена для await-dispose', () => {
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  const entry = { handle, baseTurn: 0, lastUsedMs: 1_000 }
  cache.set('s1', entry)

  const plan = planHarnessIngestResume(cache, 's1', () => false, 1_500, 120_000)
  assert.equal(plan.staleForId, entry, 'запись, вытесненная из-под ТЕКУЩЕГО id, обязана попасть в staleForId (её dispose ждут до холодного ресума)')
  assert.equal(plan.evicted[0], entry)
})

test('planHarnessIngestResume: idle-вытеснение ЧУЖИХ записей не попадает в staleForId (гонки с этим вызовом нет)', () => {
  const cache = new Map()
  const old = fakeHandle('OLD')
  cache.set('other-session', { handle: old, baseTurn: 0, lastUsedMs: 1_000 })

  const plan = planHarnessIngestResume(cache, 's1', () => true, 200_000, 120_000)
  assert.equal(plan.evicted.length, 1, 'простроченная чужая запись вытеснена')
  assert.equal(plan.evicted[0].handle, old)
  assert.equal(plan.staleForId, undefined, 'чужая idle-запись не в гонке с этим вызовом — dispose fire-and-forget')
})

test('planHarnessIngestResume: idle-вытеснение СВОЕЙ записи попадает в staleForId (блокер 1, ревью PR #1057, второй раунд)', () => {
  // До фикса staleForId выставлялся ТОЛЬКО в ветке isLiveOwner (тест выше,
  // «вытесненная из-под этого id»); симметричный случай — та же запись
  // вытеснена idle-таймаутом, не отказом isLiveOwner, — оставался
  // непокрытым, и staleForId для него молча оставался undefined: caller
  // диспоузил бы её fire-and-forget и тут же холодно ресумился тем же id,
  // получая BUSY на цикл (ровно найденная ревьюером гонка).
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  const entry = { handle, baseTurn: 0, lastUsedMs: 1_000 }
  cache.set('s1', entry)

  const idleMs = 120_000
  // isLiveOwner всегда true — единственная причина вытеснения здесь именно
  // idle-таймаут, не протухшее владение (та ветка покрыта тестом выше).
  const plan = planHarnessIngestResume(cache, 's1', () => true, 1_000 + idleMs + 1, idleMs)
  assert.equal(plan.entry, undefined, 'простроченная запись этого же id не переиспользуется')
  assert.equal(
    plan.staleForId, entry,
    'idle-вытеснение СВОЕЙ записи обязано попасть в staleForId так же, как isLiveOwner-отказ',
  )
  assert.equal(
    plan.staleForIdReason, 'idle',
    'причина вытеснения названа фактом, не угадана (алерт не гадает)',
  )
})

test('planHarnessIngestResume: isLiveOwner-отказ называет причину not-live-owner (не idle)', () => {
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  const entry = { handle, baseTurn: 0, lastUsedMs: 1_000 }
  cache.set('s1', entry)

  const plan = planHarnessIngestResume(cache, 's1', () => false, 1_500, 120_000)
  assert.equal(plan.staleForId, entry)
  assert.equal(plan.staleForIdReason, 'not-live-owner', 'isLiveOwner-ветка не маскируется под idle')
})

test('ЖИВОЙ КЛАСС ДЕФЕКТА (блокер, ревью PR #1057, четвёртый раунд): батч в полёте НЕ вытесняется по idle, даже если живёт дольше idleMs', () => {
  // lastUsedMs ставится ОДИН раз в НАЧАЛЕ батча (appendHarnessEvents), поэтому
  // батч, исполняющийся дольше idle-окна, «прострочен» по возрасту — но его
  // хэндл прямо сейчас гонит append/flush: dispose под живым батчем — тот же
  // класс «тихая гонка тёплого хэндла», что блокеры двух предыдущих раундов.
  // До фикса первый же входящий ingest ЛЮБОЙ другой сессии вытеснял такую
  // запись и диспоузил её хэндл (в лучшем случае спорный 5xx и пустой ретрай
  // дрена, в худшем — частичный батч сохранён и ретрай дописывает его второй
  // раз: silent-wrong в хранимом логе).
  const cache = new Map()
  const handle = fakeHandle('AGENT-1')
  const busy = { handle, baseTurn: 0, lastUsedMs: 1_000, inFlight: true }
  cache.set('busy-session', busy)
  // Соседняя простроченная запись без in-flight вытесняется как раньше —
  // фикс не замораживает кэш целиком.
  const idle = fakeHandle('IDLE')
  cache.set('idle-session', { handle: idle, baseTurn: 0, lastUsedMs: 1_000, inFlight: false })

  const idleMs = 120_000
  const plan = planHarnessIngestResume(cache, 'third-party', () => true, 1_000 + idleMs + 1_000, idleMs)
  assert.equal(plan.evicted.length, 1, 'вытеснена только простроченная НЕ in-flight запись')
  assert.equal(plan.evicted[0].handle, idle)
  assert.equal(cache.has('busy-session'), true, 'in-flight запись остаётся в кэше')
  assert.equal(handle.disposed(), false, 'хэндл живого батча не диспоузится')

  // Флаг — дискриминатор, не возраст: как только батч завершился
  // (inFlight = false), та же запись по тому же возрасту вытесняется
  // обычным порядком.
  busy.inFlight = false
  const after = planHarnessIngestResume(cache, 'third-party', () => true, 1_000 + idleMs + 2_000, idleMs)
  assert.equal(after.evicted.length, 1)
  assert.equal(after.evicted[0].handle, handle)
  assert.equal(cache.has('busy-session'), false)
})
