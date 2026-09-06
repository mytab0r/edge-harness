#!/usr/bin/env node
// Generic e2e browser smoke морды dsh-edge (issue #502) против ПРОДА
// (https://dsh-edge.mytab0r.workers.dev). Повод: раздел Settings →
// Integrations месяцами показывал «Journal unavailable: HTTP 404» (issue
// #501, Cloudflare error 1042) — ни один тест этого не поймал, потому что
// весь CI проверяет код и API, но никто не открывает интерфейс и не смотрит,
// что видит человек. Владелец дословно: «просто пройтись по всем вкладкам,
// понажимать там на все кнопки и везде проверять консоль и нетворк, на
// ошибки».
//
// Браузер: playwright-core (без бандла Chromium, ~3 MB) + системный Chrome
// GitHub-раннера (channel: 'chrome', уже установлен на ubuntu-latest —
// actions/runner-images включает Google Chrome stable) — не тянем ~130+ MB
// собственного браузера Playwright ради разового смоука после деплоя.
//
// Сама проверка (обход вкладок, сбор находок) живёт в browser-walk.mjs —
// issue #600 завёл туда же PR-смоук (pr-check.mjs, против локального
// unstable_dev ДО мержа), и обе точки входа обязаны гонять ОДНУ логику, а не
// две разошедшиеся копии.
//
// Использование: BASE_URL=https://dsh-edge.mytab0r.workers.dev \
//   DSH_EDGE_ACCESS_KEY=... node smoke.mjs
import { chromium } from 'playwright-core'
import { runBrowserSmoke, printFindings } from './browser-walk.mjs'

const BASE_URL = process.env.BASE_URL || 'https://dsh-edge.mytab0r.workers.dev'
const ACCESS_KEY = process.env.DSH_EDGE_ACCESS_KEY
if (!ACCESS_KEY) {
  console.error('::error::DSH_EDGE_ACCESS_KEY не задан — смоуку нечем логиниться')
  process.exit(1)
}

const startedAt = Date.now()

async function main() {
  const browser = await chromium.launch({ channel: 'chrome' })
  try {
    return await runBrowserSmoke({ browser, baseUrl: BASE_URL, accessKey: ACCESS_KEY })
  } finally {
    await browser.close()
  }
}

main()
  .then(({ findings }) => {
    const elapsedMs = Date.now() - startedAt
    if (findings.length > 0) {
      console.error(`::error::e2e-смоук нашёл ${findings.length} ошибок (консоль/нетворк) за ${elapsedMs} мс:`)
      printFindings(findings)
      process.exit(1)
    }
    console.log(`✅ e2e-смоук: интерфейс чист (консоль/нетворк без ошибок) за ${elapsedMs} мс`)
  })
  .catch((error) => {
    // Диагностика (#518): падение ДО конца main() обходит печать findings в
    // .then() выше — консольные/сетевые находки, собранные до отказа
    // (например, JS-ошибка бута шелла до самого клика), иначе теряются молча.
    if (error?.findings?.length > 0) {
      console.error(`::error::находки (консоль/нетворк) до отказа (${error.findings.length}):`)
      printFindings(error.findings)
    }
    console.error(`::error::e2e-смоук упал: ${error?.stack ?? error}`)
    process.exit(1)
  })
