#!/usr/bin/env node
// e2e browser smoke морды dsh-edge НА PR, ДО мержа (issue #600). Гоняет ТУ
// ЖЕ проверку, что и прод-смоук (browser-walk.mjs — issue #502: обход
// вкладок Settings, сбор находок консоли/сети), но против ЛОКАЛЬНОГО
// эфемерного воркера, поднятого через `unstable_dev` — тем же приёмом, что
// уже используют dsh-edge/ingest-integration/check.mjs и
// dsh-edge/proxy-integration/check.mjs (оба вызываются deploy-dsh-edge.yml
// на собранном артефакте до деплоя).
//
// Почему локальный unstable_dev, а не preview-деплой на Cloudflare:
//   - ноль обращений к настоящему Cloudflare API/аккаунту — ноль расхода
//     суточной квоты чтений Durable Objects (сегодня уже была превышена,
//     149.8%) и ноль лишних воркеров на Free-плане, которые пришлось бы
//     заводить и убирать на каждый PR;
//   - ноль секретов: DSH_EDGE_ACCESS_KEY и журнал — фиктивные значения
//     (см. AUTH_DUMMY/JOURNAL_BEARER_DUMMY ниже), CLOUDFLARE_API_TOKEN не
//     нужен вообще — прогон работает и на PR из форка, где секретов
//     репозитория нет;
//   - приём уже доказан в этом репозитории (ingest/proxy check.mjs гоняются
//     в проде deploy-dsh-edge.yml на каждом деплое), это не новая
//     инфраструктура, а третье применение существующей.
//
// Уборка: всё, что создаёт этот скрипт (эфемерные workerd-процессы
// unstable_dev, tmp-директории persistTo), живёт только на время процесса
// Node и стирается вместе с раннером CI — на Cloudflare ничего не создаётся,
// убирать вручную нечего. `finally` останавливает оба воркера (заглушка
// журнала + сама морда) независимо от исхода проверки.
//
// Использование: node pr-check.mjs <APP_DIR>
//   APP_DIR — apps/dsh-edge клона апстрима на пине с применённой серией
//   патчей и установленными плагинами (в PR-workflow это
//   $GITHUB_WORKSPACE/clone/apps/dsh-edge — тот же каталог, что получают
//   ingest/proxy check.mjs). Ожидает собранные
//   standalone/worker/direct/index.js и standalone/dist, wrangler — в
//   standalone/node_modules (см. их докстринги — та же конвенция).
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createRequire } from 'node:module'
import { chromium } from 'playwright-core'
import { runBrowserSmoke, printFindings } from './browser-walk.mjs'

const appDir = process.argv[2]
if (!appDir) {
  console.error('Использование: node pr-check.mjs <APP_DIR (apps/dsh-edge клона)>')
  process.exit(1)
}
const standaloneDir = join(appDir, 'standalone')

// Wrangler пишет логи/конфиг в $XDG_CONFIG_HOME/.wrangler — направляем в tmp
// принудительно (та же причина, что у proxy-integration/check.mjs: EACCES на
// системном пути CI-образа выглядит как «воркер не поднялся»).
process.env.XDG_CONFIG_HOME = join(tmpdir(), 'wrangler-pr-check-home')

// wrangler резолвится ИЗ клона (у этого скрипта своего node_modules нет по
// построению — репозиторий не ставит wrangler), как у ingest/proxy check.mjs.
const standaloneRequire = createRequire(join(standaloneDir, 'package.json'))
const { unstable_dev } = standaloneRequire('wrangler')

// Фиктивные значения — не секреты (локальный unstable_dev, эфемерный
// процесс). ≥32 байт: resolveOwnerAuthConfig (auth.ts) при коротком ключе
// бросает 503 на КАЖДЫЙ маршрут раньше /api/health (класс #128, живая
// находка ingest-check).
const AUTH_DUMMY = ['pr-check', 'owner', 'access', 'key-0123456789abcdef'].join('-')
const JOURNAL_BEARER_DUMMY = ['pr-check', 'journal', 'bearer', '0123456789abcdef'].join('-')
const JOURNAL_STUB_NAME = 'dsh-edge-pr-check-journal-stub'

// ── Заглушка журнала: раздел Integrations дёргает /api/harness/events на
// старте — без биндинга получил бы 503 «not configured», который смоук
// честно (и ошибочно для целей PR-проверки) засчитал бы сетевой находкой.
// Форма ответа — как у настоящего журнала (cf-worker/src/harness.ts), тот же
// приём, что и в proxy-integration/check.mjs.
function writeJournalStubWorker(dir) {
  const src = `
export default {
  async fetch() {
    return new Response(JSON.stringify({ events: [], has_more: false, next_after: 0 }), {
      status: 200,
      headers: { 'content-type': 'application/json', 'x-has-more': 'false', 'x-next-after': '0' },
    })
  },
}
`
  const scriptPath = join(dir, 'journal-stub.mjs')
  writeFileSync(scriptPath, src)
  const configPath = join(dir, 'wrangler-journal-stub.jsonc')
  writeFileSync(configPath, JSON.stringify({
    name: JOURNAL_STUB_NAME,
    main: 'journal-stub.mjs',
    compatibility_date: '2026-08-14',
  }))
  return { scriptPath, configPath }
}

async function bootJournalStub() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-edge-pr-check-journal-'))
  const { scriptPath, configPath } = writeJournalStubWorker(dir)
  return unstable_dev(scriptPath, {
    config: configPath,
    env: '',
    logLevel: 'warn',
    experimental: { disableExperimentalWarning: true, showInteractiveDevSession: false, watch: false },
  })
}

function writeConfig(dir) {
  // Тот же состав биндингов, что и у деплоя (deploy-dsh-edge.yml, шаг
  // «Конфиг воркера»), без Cloudflare-специфики (нет real account_id/токена —
  // unstable_dev не обращается к настоящему Cloudflare API).
  const config = `{
    "name": "dsh-edge-pr-check",
    "main": ${JSON.stringify(join(standaloneDir, 'worker', 'direct', 'index.js'))},
    "compatibility_date": "2026-08-14",
    "compatibility_flags": ["nodejs_compat"],
    "no_bundle": true,
    "services": [{ "binding": "HARNESS_SERVICE", "service": ${JSON.stringify(JOURNAL_STUB_NAME)} }],
    "assets": {
      "binding": "ASSETS",
      "directory": ${JSON.stringify(join(standaloneDir, 'dist'))},
      "not_found_handling": "single-page-application",
      "run_worker_first": ["/api/*", "/", "/login"]
    },
    "durable_objects": { "bindings": [{ "name": "DSH_EDGE_INSTANCE", "class_name": "DshEdgeInstance" }] },
    "migrations": [{ "tag": "v1", "new_sqlite_classes": ["DshEdgeInstance"] }]
  }`
  const configPath = join(dir, 'wrangler-pr-check.jsonc')
  writeFileSync(configPath, config)
  return configPath
}

async function bootWorker() {
  const persistedState = mkdtempSync(join(tmpdir(), 'dsh-edge-pr-check-'))
  return unstable_dev(join(standaloneDir, 'worker', 'direct', 'index.js'), {
    config: writeConfig(persistedState),
    env: '',
    persistTo: persistedState,
    vars: {
      DEEPSEEK_API_KEY: 'pr-check-unused',
      DSH_EDGE_ACCESS_KEY: AUTH_DUMMY,
      HANDS_TOKEN: JOURNAL_BEARER_DUMMY,
    },
    // warn, не error: бут-ошибки wrangler/workerd обязаны быть видны в логе
    // шага CI (ревью #128: с logLevel 'error' прогон не показал ни строки).
    logLevel: 'warn',
    experimental: {
      disableExperimentalWarning: true,
      showInteractiveDevSession: false,
      watch: false,
    },
  })
}

const READINESS_DEADLINE_MS = 90_000
const READINESS_STEP_MS = 5_000
async function waitReady(worker) {
  const entryOrigin = `http://${worker.address}:${worker.port}`
  const startedAt = Date.now()
  let attempt = 0
  let lastBody = ''
  while (Date.now() - startedAt < READINESS_DEADLINE_MS) {
    attempt += 1
    try {
      const probe = await fetch(`${entryOrigin}/api/health`, { signal: AbortSignal.timeout(5_000) })
      lastBody = (await probe.text()).slice(0, 400)
      console.log(`pr-check: попытка ${attempt}: /api/health → ${probe.status} ${lastBody}`)
      if (probe.status === 200 && JSON.parse(lastBody || '{}').ok === true) return entryOrigin
    } catch (error) {
      console.log(`pr-check: попытка ${attempt}: /api/health недоступна (${error?.cause?.code ?? error?.message})`)
    }
    await new Promise(resolve => setTimeout(resolve, READINESS_STEP_MS))
  }
  throw new Error(`воркер не ответил 200 ok от /api/health за 90 с; последний ответ: ${lastBody || '<ответа не было>'}`)
}

const startedAt = Date.now()
const journalStub = await bootJournalStub()
let worker
let browser
try {
  worker = await bootWorker()
  const entryOrigin = await waitReady(worker)
  console.log(`pr-check: локальный воркер жив на ${entryOrigin}`)

  browser = await chromium.launch({ channel: 'chrome' })
  const { findings } = await runBrowserSmoke({ browser, baseUrl: entryOrigin, accessKey: AUTH_DUMMY })
  const elapsedMs = Date.now() - startedAt
  if (findings.length > 0) {
    console.error(`::error::PR-смоук нашёл ${findings.length} ошибок (консоль/нетворк) за ${elapsedMs} мс:`)
    printFindings(findings)
    process.exitCode = 1
  } else {
    console.log(`✅ PR-смоук: интерфейс чист (консоль/нетворк без ошибок) за ${elapsedMs} мс`)
  }
} catch (error) {
  if (error?.findings?.length > 0) {
    console.error(`::error::находки (консоль/нетворк) до отказа (${error.findings.length}):`)
    printFindings(error.findings)
  }
  console.error(`::error::PR-смоук упал: ${error?.stack ?? error}`)
  process.exitCode = 1
} finally {
  // Уборка: оба локальных воркера остановлены независимо от исхода —
  // на Cloudflare ничего не создавалось, убирать больше нечего.
  await browser?.close().catch(() => {})
  await worker?.stop().catch(() => {})
  await journalStub.stop().catch(() => {})
}
