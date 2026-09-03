/**
 * Проверка совместимости плагина с Workers Free (design.md «Workers-совместимость»).
 * Запускается форжем перед сборкой tarball'а — красный выход = запрет публикации.
 *
 * Использование: node dsh-edge/check-plugin-compat.mjs <plugin-source-dir> [--client]
 */

import { readFile, readdir } from 'node:fs/promises'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const pluginDir = process.argv[2]
const checkClient = process.argv.includes('--client')
if (!pluginDir) {
  process.stderr.write('Usage: node dsh-edge/check-plugin-compat.mjs <plugin-source-dir> [--client]\n')
  process.exit(2)
}

const repoRoot = join(fileURLToPath(import.meta.url), '..', '..')
const absPluginDir = join(repoRoot, pluginDir)

async function main() {
  console.log(`plugin-compat: проверка ${pluginDir}${checkClient ? ' (client=true)' : ''}`)

  // 1. package.json — форма, dsh.client, экспорты
  await checkPackageJson()

  // 2. Запрещённые импорты во всех .js/.ts файлах плагина
  await checkForbiddenImports()

  // 3. Нет рантайм import() npm-спецификаторов
  await checkNoDynamicImports()

  // 4. Нет node:fs на горячем пути (проверяем, что нет синхронных fs вызовов в apply)
  await checkNoSyncFsInApply()

  // 5. Если client=true — проверка клиентского бандла
  if (checkClient) {
    await checkClientBundle()
  }

  // 6. Синтаксическая проверка всех JS файлов
  await checkSyntax()

  console.log('plugin-compat: ЗЕЛЁНЫЙ — все проверки совместимости пройдены')
}

async function checkPackageJson() {
  const pkgPath = join(absPluginDir, 'package.json')
  const pkg = JSON.parse(await readFile(pkgPath, 'utf8'))

  // dsh.client декларация
  if (checkClient) {
    if (pkg.dsh?.client?.platform !== 'web') {
      throw new Error('package.json: dsh.client.platform должен быть "web" для клиентских плагинов')
    }
    if (!pkg.exports?.['./client']) {
      throw new Error('package.json: нужен экспорт "./client" для клиентских плагинов')
    }
  }

  // peerDependencies на dsh-tools (ожидается для серверных плагинов с инструментами)
  const hasToolsPeer = pkg.peerDependencies?.['@deepseek-ai/dsh-tools']
  if (!hasToolsPeer) {
    console.warn('plugin-compat: предупреждение — нет peerDependencies на @deepseek-ai/dsh-tools')
  }

  console.log('  ✓ package.json: форма валидна')
}

// Разрешённые node:*-спецификаторы — одно место правды, читают и
// checkForbiddenImports (статические/require), и checkNoDynamicImports
// (динамический import()). Раньше список был локальным в checkForbiddenImports,
// и checkNoDynamicImports запрещал то же самое ещё раз без сверки со списком —
// разрешённый `import('node:crypto')` ловился как нарушение (находка AI-ревью
// PR #162, issue #265).
const ALLOWED_NODE_IMPORTS = [
  'node:path',
  'node:url',
  'node:util',
  'node:events',
  'node:stream',
  'node:crypto',
  'node:buffer',
  'node:assert',
  'node:querystring',
]

async function checkForbiddenImports() {
  const FORBIDDEN_PATTERNS = [
    { pattern: /from\s+['"]node:fs['"]/g, desc: 'node:fs (используйте VFS Computer /workspace)' },
    { pattern: /require\(['"]node:fs['"]\)/g, desc: 'node:fs (require)' },
    { pattern: /from\s+['"]fs['"]/g, desc: 'fs (bare import, недоступно в Workers — используйте VFS Computer /workspace)' },
    { pattern: /require\(['"]fs['"]\)/g, desc: 'fs (bare require, недоступно в Workers)' },
    { pattern: /from\s+['"]node:child_process['"]/g, desc: 'node:child_process' },
    { pattern: /require\(['"]node:child_process['"]\)/g, desc: 'node:child_process (require)' },
    { pattern: /from\s+['"]child_process['"]/g, desc: 'child_process (bare import, недоступно в Workers)' },
    { pattern: /require\(['"]child_process['"]\)/g, desc: 'child_process (bare require, недоступно в Workers)' },
    { pattern: /from\s+['"]node:net['"]/g, desc: 'node:net' },
    { pattern: /require\(['"]node:net['"]\)/g, desc: 'node:net (require)' },
    { pattern: /from\s+['"]net['"]/g, desc: 'net (bare import, недоступно в Workers)' },
    { pattern: /require\(['"]net['"]\)/g, desc: 'net (bare require, недоступно в Workers)' },
    { pattern: /from\s+['"]node:dgram['"]/g, desc: 'node:dgram' },
    { pattern: /require\(['"]node:dgram['"]\)/g, desc: 'node:dgram (require)' },
    { pattern: /from\s+['"]dgram['"]/g, desc: 'dgram (bare import, недоступно в Workers)' },
    { pattern: /require\(['"]dgram['"]\)/g, desc: 'dgram (bare require, недоступно в Workers)' },
    { pattern: /from\s+['"]node:module['"]/g, desc: 'node:module (динамические части недоступны)' },
    { pattern: /require\(['"]node:module['"]\)/g, desc: 'node:module (require)' },
    { pattern: /from\s+['"]module['"]/g, desc: 'module (bare import, недоступно в Workers)' },
    { pattern: /require\(['"]module['"]\)/g, desc: 'module (bare require, недоступно в Workers)' },
    { pattern: /from\s+['"]koffi['"]/g, desc: 'koffi (нативные аддоны недоступны)' },
    { pattern: /require\(['"]koffi['"]\)/g, desc: 'koffi (require)' },
    { pattern: /from\s+['"]node-pty['"]/g, desc: 'node-pty (нативные аддоны недоступны)' },
    { pattern: /require\(['"]node-pty['"]\)/g, desc: 'node-pty (require)' },
    { pattern: /@deepseek-ai\/node-addon-landlock-run/g, desc: 'landlock-run (нативный аддон)' },
  ]

  const files = await collectJsFiles(absPluginDir)
  let violations = []

  for (const file of files) {
    const content = await readFile(file, 'utf8')
    const relPath = relative(absPluginDir, file)

    for (const { pattern, desc } of FORBIDDEN_PATTERNS) {
      const matches = [...content.matchAll(pattern)]
      if (matches.length > 0) {
        violations.push(`${relPath}: ${desc} (${matches.length} вхождений)`)
      }
    }

    // Проверка node:* импортов — разрешаем только ALLOWED_NODE_IMPORTS
    const nodeImports = [...content.matchAll(/from\s+['"](node:[^'"]+)['"]/g)]
      .concat([...content.matchAll(/require\(['"](node:[^'"]+)['"]\)/g)])
      .map(m => m[1])
    // Также проверяем import() с node: спецификаторами
    const dynamicNodeImports = [...content.matchAll(/import\s*\(\s*['"](node:[^'"]+)['"]\s*\)/g)]
      .map(m => m[1])
    for (const imp of dynamicNodeImports) {
      if (!ALLOWED_NODE_IMPORTS.some(allowed => imp.startsWith(allowed))) {
        violations.push(`${relPath}: динамический import() ${imp} не в списке разрешённых (${ALLOWED_NODE_IMPORTS.join(', ')})`)
      }
    }
    for (const imp of nodeImports) {
      if (!ALLOWED_NODE_IMPORTS.some(allowed => imp.startsWith(allowed))) {
        violations.push(`${relPath}: импорт ${imp} не в списке разрешённых (${ALLOWED_NODE_IMPORTS.join(', ')})`)
      }
    }
  }

  if (violations.length > 0) {
    throw new Error('Запрещённые импорты найдены:\n' + violations.map(v => `  - ${v}`).join('\n'))
  }
  console.log('  ✓ запрещённые импорты: не найдено')
}

async function checkNoDynamicImports() {
  const files = await collectJsFiles(absPluginDir)
  let violations = []

  for (const file of files) {
    const content = await readFile(file, 'utf8')
    const relPath = relative(absPluginDir, file)

    // Ищем import() с не-статическими аргументами (переменные, конкатенация)
    // Статические import('./relative-path') разрешены. Статические
    // import('node:allowed-specifier') тоже разрешены — checkForbiddenImports
    // уже проверяет их состав против ALLOWED_NODE_IMPORTS; здесь нельзя
    // запрещать повторно то, что там явно допущено (иначе легальный
    // import('node:crypto') красит прогон — находка AI-ревью PR #162, #265).
    const dynamicImports = [...content.matchAll(/import\s*\(\s*([^)]+)\s*\)/g)]
      .filter(m => {
        const arg = m[1].trim()
        // Разрешаем строковые литералы с относительными путями
        if (/^['"].\//.test(arg) || /^['"]\.\.\//.test(arg)) return false
        // Разрешаем строковые литералы с допущенными node:*-спецификаторами
        const nodeSpecifier = /^['"](node:[^'"]+)['"]$/.exec(arg)
        if (nodeSpecifier && ALLOWED_NODE_IMPORTS.some(allowed => nodeSpecifier[1].startsWith(allowed))) {
          return false
        }
        return true
      })

    if (dynamicImports.length > 0) {
      violations.push(`${relPath}: динамический import() с не-статическим аргументом (${dynamicImports.length} вхождений)`)
    }
  }

  if (violations.length > 0) {
    throw new Error('Динамические import() найдены:\n' + violations.map(v => `  - ${v}`).join('\n'))
  }
  console.log('  ✓ динамические import(): не найдено')
}

async function checkNoSyncFsInApply() {
  const files = await collectJsFiles(absPluginDir)
  let violations = []

  for (const file of files) {
    const content = await readFile(file, 'utf8')
    const relPath = relative(absPluginDir, file)

    // Ищем синхронные fs.*Sync вызовы по всему файлу плагина (не только внутри
    // apply) — строгость оправдана: node:fs в плагине вообще не место (Workers
    // не даёт настоящей ФС), точечный "контекст apply" ничего бы не смягчил.
    const syncFsCalls = [...content.matchAll(/\bfs\.(readFileSync|writeFileSync|statSync|readdirSync|mkdirSync|rmSync|unlinkSync|realpathSync)\b/g)]
    if (syncFsCalls.length > 0) {
      violations.push(`${relPath}: синхронные fs вызовы (${syncFsCalls.length}) — в Edge ФС только VFS Computer'а`)
    }
  }

  if (violations.length > 0) {
    throw new Error('Синхронные fs вызовы:\n' + violations.map(v => `  - ${v}`).join('\n'))
  }
  console.log('  ✓ синхронные fs вызовы: не найдено')
}

async function checkClientBundle() {
  const clientPath = join(absPluginDir, 'client', 'client.js')
  try {
    await readFile(clientPath, 'utf8')
  } catch {
    // Клиентский бандл может генерироваться на этапе сборки (например, plugin-manager)
    // В этом случае проверяем, что package.json объявляет экспорт ./client
    const pkgPath = join(absPluginDir, 'package.json')
    const pkg = JSON.parse(await readFile(pkgPath, 'utf8'))
    if (!pkg.exports?.['./client']) {
      throw new Error('package.json: нужен экспорт "./client" для клиентских плагинов')
    }
    console.log('  ✓ клиентский бандл: будет собран на этапе сборки (export ./client объявлен)')
    return
  }

  // Проверка обёртки ModuleLoader
  const content = await readFile(clientPath, 'utf8')
  if (!content.includes('window.__ModuleLoader__.load')) {
    throw new Error('client/client.js: отсутствует обёртка window.__ModuleLoader__.load — невалидная форма ростера')
  }

  // Проверка экспортов inject и apply
  if (!content.includes('exports.inject') && !content.includes('export')) {
    console.warn('plugin-compat: предупреждение — не найден exports.inject в клиентском бандле')
  }

  console.log('  ✓ клиентский бандл: форма валидна')
}

async function checkSyntax() {
  const files = await collectJsFiles(absPluginDir)
  for (const file of files) {
    const result = spawnSync(process.execPath, ['--check', file], { stdio: 'pipe' })
    if (result.status !== 0) {
      throw new Error(`node --check не прошёл для ${relative(absPluginDir, file)}:\n${result.stderr.toString()}`)
    }
  }
  console.log('  ✓ синтаксис: все файлы валидны')
}

async function collectJsFiles(dir) {
  const files = []
  const pkgPath = join(dir, 'package.json')
  let pkgFiles = null
  try {
    const pkg = JSON.parse(await readFile(pkgPath, 'utf8'))
    pkgFiles = pkg.files
  } catch {
    // package.json не читается — проверяем всё
  }

  const entries = await readdir(dir, { withFileTypes: true })
  for (const entry of entries) {
    const fullPath = join(dir, entry.name)
    if (entry.isDirectory()) {
      // Пропускаем служебные каталоги и тесты
      if (!['node_modules', '.git', 'dist', 'build', 'test', 'tests', '__tests__'].includes(entry.name)) {
        files.push(...await collectJsFiles(fullPath))
      }
    } else if (entry.name.endsWith('.js') || entry.name.endsWith('.ts') || entry.name.endsWith('.mjs')) {
      // Если в package.json есть files — проверяем только то, что туда входит
      if (pkgFiles && Array.isArray(pkgFiles) && pkgFiles.length > 0) {
        const relPath = relative(dir, fullPath)
        const included = pkgFiles.some(pattern => {
          if (pattern.endsWith('/')) {
            return relPath.startsWith(pattern) || relPath === pattern.slice(0, -1)
          }
          return relPath === pattern || minimatch(relPath, pattern)
        })
        if (included) files.push(fullPath)
      } else {
        files.push(fullPath)
      }
    }
  }
  return files
}

function minimatch(path, pattern) {
  // Простая реализация minimatch для базовых паттернов.
  // Сначала экранируем ВСЕ спецсимволы regex (включая сам `\`, иначе
  // произвольный `\` в паттерне из чужого package.json меняет смысл
  // следующего символа в собранном regex — CodeQL js/incomplete-sanitization),
  // и только потом раскрываем `**`/`*` обратно в свои конструкции.
  const escaped = pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const regex = escaped
    .replace(/\\\*\\\*/g, '.*')
    .replace(/\\\*/g, '[^/]*')
  return new RegExp(`^${regex}$`).test(path)
}

main().catch(err => {
  console.error(`plugin-compat: ОШИБКА — ${err.message}`)
  process.exit(1)
})