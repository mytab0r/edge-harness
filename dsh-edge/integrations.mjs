/**
 * Единственное место правды о форме реестра интеграций (dsh-edge/integrations.json,
 * задача #115). Реестр декларирует, КАКИЕ интеграции внешних систем у харнеса есть,
 * какими инструментами агента они выражены и какие ИМЕНА секретов им нужны —
 * только имена, никаких значений: значения живут в секретах репозитория и
 * воркера (см. ADR 0010, docs/decisions/0010-integrations-edge-plugin-rest-tools.md).
 *
 * Читается:
 *   - сборкой клиентской половины плагина integrations (plugins-src/integrations/build.mjs) —
 *     реестр целиком вшивается в бандл раздела «Интеграции»;
 *   - deploy-dsh-edge.yml (шаги «Секреты интеграций» и «Статусы интеграций») —
 *     синк секретов в воркер и статусы в журнал по списку отсюда;
 *   - repo-ci.yml (CLI ниже) — громкая валидация формы и гвардия проводки.
 *
 * Гвардия проводки закрывает класс «интеграцию объявили в реестре, а секрет в
 * workflow не подвели» — иначе интеграция молча вечно not_configured при
 * существующем секрете. Для каждой интеграции поле wired называет workflow,
 * который ОБЯЗАН упоминать все её секреты: morde → deploy-dsh-edge.yml
 * (синк в воркер + проверка наличия), jobs → worker.yml (уже живой транспорт
 * эскалаций #91).
 *
 * Валидация громкая: любая ошибка формы — throw / ненулевой exit до любых
 * дорогих шагов. Шаблон id — тот же, что у плагинов (ID_PATTERN из manifest.mjs):
 * id интеграции становится псевдо-задачей журнала integration:<id>.
 */

import { readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { ID_PATTERN } from './manifest.mjs'

const INTEGRATIONS_VERSION = 1
const SECRET_NAME_PATTERN = /^[A-Z][A-Z0-9_]*$/
const TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]*$/
const WIRINGS = new Set(['morde', 'jobs'])
const WORKFLOW_OF_WIRING = { morde: 'deploy-dsh-edge.yml', jobs: 'worker.yml' }

/**
 * Форма реестра. credentials.secrets — объект «ИМЯ → описание для владельца»;
 * tools — имена инструментов агента (для wired: "jobs" закончен пустым —
 * у telegram инструментов в морде нет, его транспорт — job'ы раннеров).
 */
export function parseIntegrations(source) {
  let registry
  try {
    registry = JSON.parse(source)
  } catch (error) {
    throw new Error(`dsh-edge/integrations.json is not valid JSON: ${error.message}`)
  }
  if (registry === null || typeof registry !== 'object' || Array.isArray(registry)) {
    throw new Error('dsh-edge/integrations.json must be a JSON object.')
  }
  if (registry.version !== INTEGRATIONS_VERSION) {
    throw new Error(`dsh-edge/integrations.json version must be ${INTEGRATIONS_VERSION}, found ${JSON.stringify(registry.version)}.`)
  }
  if (!Array.isArray(registry.integrations)) {
    throw new Error('dsh-edge/integrations.json must carry an integrations array.')
  }
  if (registry.integrations.length === 0) {
    throw new Error('dsh-edge/integrations.json must declare at least one integration (an empty registry is a silent shrink of the section, not a state).')
  }
  const seenIds = new Set()
  const seenSecrets = new Map()
  for (const entry of registry.integrations) {
    const where = `integration ${JSON.stringify(entry?.id ?? entry)}`
    failUnless(objectShape(entry), `${where} must be an object.`)
    failUnless(typeof entry.id === 'string' && ID_PATTERN.test(entry.id), `${where}: id must match ${ID_PATTERN}.`)
    failUnless(!seenIds.has(entry.id), `duplicate integration id "${entry.id}".`)
    seenIds.add(entry.id)
    for (const field of ['title', 'summary']) {
      failUnless(typeof entry[field] === 'string' && entry[field].trim() !== '',
        `${where}: ${field} must be a non-empty string (it is shown in the morde section).`)
    }
    failUnless(Array.isArray(entry.tools), `${where}: tools must be an array of tool names.`)
    for (const tool of entry.tools) {
      failUnless(typeof tool === 'string' && TOOL_NAME_PATTERN.test(tool),
        `${where}: tool name must match ${TOOL_NAME_PATTERN}, found ${JSON.stringify(tool)}.`)
    }
    failUnless(entry.credentials === undefined || objectShape(entry.credentials), `${where}: credentials must be an object.`)
    if (entry.credentials !== undefined) {
      failUnless(objectShape(entry.credentials.secrets), `${where}: credentials.secrets must be an object (name → description).`)
      for (const [name, description] of Object.entries(entry.credentials.secrets)) {
        failUnless(SECRET_NAME_PATTERN.test(name), `${where}: secret name must match ${SECRET_NAME_PATTERN}, found ${JSON.stringify(name)}.`)
        failUnless(typeof description === 'string' && description.trim() !== '',
          `${where}: secret ${name} needs a non-empty description (the section shows whose key it is by it).`)
        failUnless(!seenSecrets.has(name), `secret ${name} is declared twice (${seenSecrets.get(name)} and ${entry.id}) — one name, one meaning.`)
        seenSecrets.set(name, entry.id)
      }
    }
    const wired = entry.wired ?? 'morde'
    failUnless(typeof wired === 'string' && WIRINGS.has(wired),
      `${where}: wired must be one of ${[...WIRINGS].join(', ')}, found ${JSON.stringify(entry.wired)}.`)
    failUnless(wired !== 'jobs' || entry.tools.length === 0,
      `${where}: wired "jobs" means job-side transport — tools of the morde agent are not allowed, declare them via wired "morde".`)
    if (entry.docs !== undefined) {
      failUnless(typeof entry.docs === 'string' && /^https:\/\/\S+$/.test(entry.docs),
        `${where}: docs must be an https URL, found ${JSON.stringify(entry.docs)}.`)
    }
  }
  return registry
}

/** Читает и валидирует реестр из каталога dsh-edge (рядом с этим скриптом). */
export async function loadIntegrations(repoDir) {
  return parseIntegrations(await readFile(join(repoDir, 'integrations.json'), 'utf8'))
}

/**
 * Гвардия проводки. Морде-интеграции: секрет обязан быть упомянут в env
 * КОНКРЕТНОГО шага синка («Секреты интеграций морды (#115)») в
 * deploy-dsh-edge.yml — проверка по блоку этого шага, а не по всему файлу:
 * имя, упомянутое где угодно ещё, воркеру ничего не даёт. Job'овые
 * интеграции (wired: "jobs"): транспорт — workflow целиком, достаточно
 * упоминания в любом месте worker.yml. GitHub не отдаёт список секретов
 * динамически — имена перечисляются в env явно, поэтому расхождение
 * реестра и workflow ловится только такой сверкой текста.
 */
/** Имя шага синка в deploy-dsh-edge.yml — единственное место проводки morde-секретов. */
const SYNC_STEP_NAME = 'Секреты интеграций морды (#115)'

/** Блок шага с данным name в YAML workflow: от строки с name до следующего `- name:` того же уровня. */
function workflowStepBlock(workflowText, stepName) {
  const lines = workflowText.split('\n')
  const start = lines.findIndex((line) => line.trim() === `- name: ${stepName}`)
  if (start < 0) return null
  const indent = lines[start].indexOf('-')
  let end = lines.length
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i]
    if (line.trim().startsWith('- name:') && line.indexOf('-') === indent) {
      end = i
      break
    }
  }
  return lines.slice(start, end).join('\n')
}

export async function verifyWiring(repoRoot, registry) {
  const problems = []
  const cache = new Map()
  const readWorkflow = async (workflow) => {
    if (cache.has(workflow)) return cache.get(workflow)
    const path = join(repoRoot, '.github', 'workflows', workflow)
    try {
      const text = await readFile(path, 'utf8')
      cache.set(workflow, text)
      return text
    } catch {
      problems.push(`${workflow} не читается (${path}) — проводку сверить нельзя`)
      return null
    }
  }
  for (const entry of registry.integrations) {
    const secrets = Object.keys(entry.credentials?.secrets ?? {})
    if (secrets.length === 0) continue
    const wired = entry.wired ?? 'morde'
    const workflow = WORKFLOW_OF_WIRING[wired]
    const text = await readWorkflow(workflow)
    if (text === null) continue
    if (wired === 'morde') {
      const block = workflowStepBlock(text, SYNC_STEP_NAME)
      if (block === null) {
        problems.push(`в deploy-dsh-edge.yml нет шага «${SYNC_STEP_NAME}» — синк секретов интеграций и гвардия проводки потеряны`)
        continue
      }
      for (const name of secrets) {
        if (!block.includes(`secrets.${name}`)) {
          problems.push(`${entry.id}: секрет ${name} не подведён в env шага «${SYNC_STEP_NAME}» `
            + `(deploy-dsh-edge.yml) — интеграция молча останется not_configured`)
        }
      }
    } else {
      for (const name of secrets) {
        if (!text.includes(`secrets.${name}`)) {
          problems.push(`${entry.id}: секрет ${name} не подведён в .github/workflows/${workflow} `
            + `(нет references secrets.${name}) — транспорт job'ов останется без ключа`)
        }
      }
    }
  }
  return problems
}

function objectShape(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function failUnless(condition, message) {
  if (!condition) throw new Error(`dsh-edge/integrations.json: ${message}`)
}

// CLI-режим: громкая валидация формы + гвардия проводки до любых дорогих шагов
// (repo-ci.yml и deploy-dsh-edge.yml зовут его одним и тем же способом).
// Печатает проверенный состав в stdout как JSON; любая ошибка — ненулевой exit.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const repoDir = dirname(fileURLToPath(import.meta.url))
    const repoRoot = join(repoDir, '..')
    const registry = await loadIntegrations(repoDir)
    const wiringProblems = await verifyWiring(repoRoot, registry)
    if (wiringProblems.length > 0) {
      for (const problem of wiringProblems) process.stderr.write(`::error::${problem}\n`)
      process.exit(1)
    }
    process.stdout.write(`${JSON.stringify({
      count: registry.integrations.length,
      integrations: registry.integrations.map(entry => ({
        id: entry.id,
        title: entry.title,
        tools: entry.tools,
        wired: entry.wired ?? 'morde',
        secrets: Object.keys(entry.credentials?.secrets ?? {}),
      })),
    }, null, 2)}\n`)
  } catch (error) {
    process.stderr.write(`::error::${error.message}\n`)
    process.exit(1)
  }
}
