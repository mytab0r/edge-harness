#!/usr/bin/env node
// Generic e2e browser smoke морды dsh-edge (issue #502). Повод: раздел
// Settings → Integrations месяцами показывал «Journal unavailable:
// HTTP 404» (issue #501, Cloudflare error 1042) — ни один тест этого не
// поймал, потому что весь CI проверяет код и API, но никто не открывает
// интерфейс и не смотрит, что видит человек. Владелец дословно: «просто
// пройтись по всем вкладкам, понажимать там на все кнопки и везде
// проверять консоль и нетворк, на ошибки».
//
// Браузер: playwright-core (без бандла Chromium, ~3 MB) + системный Chrome
// GitHub-раннера (channel: 'chrome', уже установлен на ubuntu-latest —
// actions/runner-images включает Google Chrome stable) — не тянем ~130+ MB
// собственного браузера Playwright ради разового смоука после деплоя.
//
// Обход вкладок — СТРУКТУРНОЙ эвристикой, не захардкоженным списком имён:
// внутри диалога Settings ([role="dialog"], устойчивый ARIA-лендмарк —
// хеши CSS-модулей вроде "VOzbGW_navCell" меняются между сборками апстрима,
// поэтому классы НЕ используются) находится самая большая группа кнопок-
// сиблингов (общий родитель) с коротким однострочным текстом и без вложенных
// полей ввода — это и есть навигация по разделам. Новый раздел, добавленный
// апстримом или патчем, автоматически попадает в эту группу и в обход —
// доказано живым прогоном на проде (issue #502): нашёл ровно General/
// Models/Agent presets/DSH Edge/Integrations/Plugins, без единого имени в
// коде.
//
// Ошибки — консоль (console.error, pageerror) и сеть (HTTP-ответ ≥400) —
// собираются ВЕСЬ прогон, привязываются к разделу, на котором произошли (по
// временному окну между переключениями вкладок). Единственная легитимная
// категория — 401 ДО логина (анонимные первые запросы: auth останавливает их
// раньше бизнес-логики, это ожидаемо и не ошибка приложения) — EXPECTED_ERRORS
// ниже, короткий и обоснованный список, не глушилка "лови всё подряд".
//
// Использование: BASE_URL=https://dsh-edge.mytab0r.workers.dev \
//   DSH_EDGE_ACCESS_KEY=... node smoke.mjs
import { chromium } from 'playwright-core'

const BASE_URL = process.env.BASE_URL || 'https://dsh-edge.mytab0r.workers.dev'
const ACCESS_KEY = process.env.DSH_EDGE_ACCESS_KEY
if (!ACCESS_KEY) {
  console.error('::error::DSH_EDGE_ACCESS_KEY не задан — смоуку нечем логиниться')
  process.exit(1)
}

const startedAt = Date.now()

/**
 * Единственное место правды по ожидаемым ошибкам (см. заголовок файла).
 * Каждая запись — функция(finding) => boolean: "эта находка ожидаема".
 * Список короткий НАМЕРЕННО: расширять только с обоснованием в комментарии,
 * не превращать в глушилку.
 */
const EXPECTED_ERRORS = [
  {
    reason: 'анонимные запросы ДО логина отвечают 401 — auth стоит раньше бизнес-логики (research/12), это норма, не поломка',
    match: (finding) => finding.kind === 'network' && finding.status === 401 && !finding.loggedIn,
  },
]

function isExpected(finding) {
  return EXPECTED_ERRORS.some((rule) => {
    try {
      return rule.match(finding)
    } catch {
      return false
    }
  })
}

const findings = []
let loggedIn = false
let currentSection = 'boot'

function record(kind, detail) {
  const finding = { kind, section: currentSection, loggedIn, ...detail }
  if (isExpected(finding)) return
  findings.push(finding)
}

async function main() {
  const browser = await chromium.launch({ channel: 'chrome' })
  const context = await browser.newContext()
  const page = await context.newPage()

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      record('console', { text: msg.text().slice(0, 500) })
    }
  })
  page.on('pageerror', (err) => {
    record('pageerror', { text: String(err?.message ?? err).slice(0, 500) })
  })
  page.on('response', (response) => {
    const status = response.status()
    if (status >= 400) {
      record('network', { url: response.url(), status, method: response.request().method() })
    }
  })

  // ── Логин через реальную форму (не API-шорткат): ловит и регрессии страницы логина ──
  currentSection = 'login'
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle', timeout: 30_000 })
  const accessInput = page.locator('input').first()
  await accessInput.waitFor({ state: 'visible', timeout: 15_000 })
  await accessInput.fill(ACCESS_KEY)
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle', timeout: 30_000 }).catch(() => {}),
    page.getByRole('button', { name: /unlock|log ?in|sign ?in/i }).click()
      .catch(() => page.keyboard.press('Enter')),
  ])
  await page.waitForTimeout(1500)
  if (page.url().includes('/login')) {
    throw new Error(`логин не удался — остались на /login после отправки формы (url: ${page.url()})`)
  }
  loggedIn = true
  currentSection = 'root'
  console.log(`smoke: логин прошёл, url ${page.url()}`)

  // Первый визит показывает оверлей-тур ("Continue") поверх шелла — закрыть,
  // иначе клики по реальным элементам перехватывает маска.
  const tourContinue = page.getByRole('button', { name: 'Continue' })
  if (await tourContinue.count() > 0) {
    await tourContinue.click({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(500)
  }

  // ── Открыть Settings ──────────────────────────────────────────────────────
  currentSection = 'settings:open'
  try {
    await page.getByRole('button', { name: 'Settings' }).click({ timeout: 15_000 })
  } catch (error) {
    // Диагностика (#518): кнопка «Settings» не нашлась — печатаем видимые
    // кнопки шелла, чтобы не гадать вслепую при следующем прогоне (apstream
    // сменил разметку — точный accessible name важнее пересказа).
    const labels = await page.evaluate(() => Array.from(document.querySelectorAll('button'))
      .map((b) => (b.getAttribute('aria-label') || b.textContent || '').trim())
      .filter((text) => text.length > 0 && text.length <= 60))
    console.error(`::error::кнопка «Settings» не найдена; видимые кнопки шелла: ${JSON.stringify(labels)}`)
    throw error
  }
  const dialog = page.locator('[role="dialog"]')
  await dialog.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForTimeout(500)

  // ── Обход вкладок: структурная эвристика, без хардкода имён/классов ──────
  const tabLabels = await page.evaluate(() => {
    const dlg = document.querySelector('[role="dialog"]')
    if (!dlg) return []
    const buttons = Array.from(dlg.querySelectorAll('button'))
    const groups = new Map()
    for (const b of buttons) {
      const parent = b.parentElement
      if (!parent) continue
      const text = (b.textContent || '').trim()
      if (!text || text.length > 30) continue
      if (b.querySelector('input, textarea, select')) continue
      if (!groups.has(parent)) groups.set(parent, [])
      groups.get(parent).push(text)
    }
    let best = []
    for (const list of groups.values()) {
      if (list.length > best.length) best = list
    }
    return best
  })

  if (tabLabels.length === 0) {
    throw new Error('структурная эвристика не нашла ни одной вкладки в диалоге Settings — разметка морды сменилась несовместимо, обход невозможен')
  }
  console.log(`smoke: найдено вкладок в Settings: ${tabLabels.length} — ${JSON.stringify(tabLabels)}`)

  for (const label of tabLabels) {
    currentSection = `settings:${label}`
    const beforeCount = findings.length
    try {
      await dialog.getByRole('button', { name: label, exact: true }).click({ timeout: 10_000 })
    } catch (error) {
      record('click', { text: `не удалось кликнуть вкладку "${label}": ${error.message}`.slice(0, 500) })
      continue
    }
    // Сеть после клика (Integrations/Plugins грузят статусы асинхронно —
    // именно тут жил #501) — ждём затишья, не фиксированную паузу вслепую.
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {})
    await page.waitForTimeout(500)
    const newFindings = findings.length - beforeCount
    console.log(`smoke: вкладка "${label}" — ${newFindings === 0 ? 'чисто' : `${newFindings} находок`}`)
  }

  currentSection = 'settings:close'
  await page.getByRole('button', { name: 'Close' }).click({ timeout: 10_000 }).catch(() => {})
  await page.waitForTimeout(500)

  await context.close()
  await browser.close()
}

main()
  .then(() => {
    const elapsedMs = Date.now() - startedAt
    if (findings.length > 0) {
      console.error(`::error::e2e-смоук нашёл ${findings.length} ошибок (консоль/нетворк) за ${elapsedMs} мс:`)
      for (const f of findings) {
        if (f.kind === 'network') {
          console.error(`  [${f.section}] network ${f.method} ${f.status} ${f.url}`)
        } else {
          console.error(`  [${f.section}] ${f.kind}: ${f.text}`)
        }
      }
      process.exit(1)
    }
    console.log(`✅ e2e-смоук: интерфейс чист (консоль/нетворк без ошибок) за ${elapsedMs} мс`)
  })
  .catch((error) => {
    console.error(`::error::e2e-смоук упал: ${error?.stack ?? error}`)
    process.exit(1)
  })
