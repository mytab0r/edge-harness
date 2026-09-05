/**
 * Проверка совместимости плагина с Workers Free (design.md «Workers-совместимость»).
 * Запускается форжем перед сборкой tarball'а — красный выход = запрет публикации.
 *
 * Использование:
 *   node dsh-edge/check-plugin-compat.mjs <plugin-source-dir> [--client]
 *       [--bundle-meta <esbuild-metafile> --package <package-name>]
 *   node dsh-edge/check-plugin-compat.mjs <plugin-source-dir> --detect
 *
 *   --client       проверять клиентский бандл (плагин с client: true);
 *   --bundle-meta  замерить вклад пакета --package в серверный бандл по
 *                  esbuild-метафайлу пробной сборки (design.md «Размер»);
 *                  метафайл делает долговечным патч 0006;
 *   --detect       напечатать {server, client} из package.json и выйти —
 *                  единственное место правды конвенции «корневой экспорт "." =
 *                  серверный вход, dsh.client.platform = client»; форж берёт
 *                  отсюда флаги и для FORGE_EXTRA_PLUGIN, и для PR-манифеста.
 *
 * История слияния: версия из PR #271 (main) дала весь проверочный движок —
 * состав файлов из `npm pack --dry-run`, программная генерация паттернов
 * запрещённых импортов, stripComments, генеричный `fs.*Sync`,
 * checkToolSchemas; версия форжа (#162) добавила --detect/--bundle-meta и
 * библиотечный режим для юнит-тестов.
 */

import { existsSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { join, relative, resolve, isAbsolute } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { spawnSync } from 'node:child_process'
import { PLUGIN_BUNDLE_CONTRIBUTION_LIMIT_BYTES } from './manifest.mjs'

const args = process.argv.slice(2)
const pluginDir = args.find(a => !a.startsWith('--'))
const checkClient = args.includes('--client')
const detectOnly = args.includes('--detect')
const bundleMetaPath = argValue('--bundle-meta')
const packageName = argValue('--package')

function argValue(name) {
  const index = args.indexOf(name)
  return index !== -1 ? args[index + 1] : undefined
}

const repoRoot = join(fileURLToPath(import.meta.url), '..', '..')
// resolve(), не join(): join('<repo>', '/tmp/x') склеивает абсолютный второй
// аргумент как относительный сегмент ('<repo>/tmp/x') — вызывающий передал
// путь к реальному /tmp/x, а чекер молча полез в чужой каталог внутри репо.
// resolve() трактует абсолютный pluginDir как есть (стандартная семантика
// path.resolve), а относительный — от repoRoot, как и раньше. Отсутствие
// каталога проверяем явно (assertPluginDirExists, только в CLI-режиме: при
// импорте как библиотеки (юнит-тесты) pluginDir нет — модуль не должен падать
// на верхнем уровне), а не молчаливым ENOENT на первом чтении package.json.
const absPluginDir = pluginDir ? resolve(repoRoot, pluginDir) : undefined

function assertPluginDirExists() {
  if (!pluginDir) usage()
  if (!existsSync(absPluginDir)) {
    process.stderr.write(
      `plugin-compat: каталог плагина не найден: ${pluginDir}` +
      (isAbsolute(pluginDir) ? '' : ` (резолвится в ${absPluginDir}, относительно ${repoRoot})`) +
      '\n',
    )
    process.exit(2)
  }
}

/**
 * Конвенция «что в плагине серверное, а что клиентское» — по декларации
 * package.json, не по хардкоду: server = есть корневой экспорт "." (серверный
 * вход; кодогенератор для server:true рендерит `import <id> from '<package>'`,
 * который без корневого экспорта не резолвится), client = dsh.client.platform
 * === 'web'. Сверяется тестом с манифестом всех реальных плагинов репо.
 */
export function detectServerClient(pkg) {
  return {
    server: pkg.exports?.['.'] !== undefined,
    client: pkg.dsh?.client?.platform === 'web',
  }
}

/**
 * Вклад пакета в серверный бандл по esbuild-метафайлу: сумма bytesInOutput
 * входов пакета по всем выводам (та же прод-форма, что читает апстримный
 * reportLargestInputs). Входы плагина в pnpm-клоне лежат под
 * `…/node_modules/<package>/…`, поэтому признак — `/<package>/` в пути.
 */
export function sumBundleContribution(metafile, pkgName) {
  let bytes = 0
  for (const output of Object.values(metafile.outputs ?? {})) {
    for (const [path, detail] of Object.entries(output.inputs ?? {})) {
      if (path.includes(`/${pkgName}/`)) bytes += Number(detail.bytesInOutput ?? 0)
    }
  }
  return bytes
}

/**
 * Точка входа клиентского бандла — из exports['./client'] package.json
 * (может быть ./dist/client.js, ./build/index.js — что угодно), не хардкод.
 * Условные экспорты разрешаются в порядке апстримного exportPath
 * (worker → browser → import → default).
 */
export function resolveClientEntry(clientExport) {
  if (clientExport === undefined || clientExport === null) return undefined
  if (typeof clientExport === 'string') return clientExport
  if (typeof clientExport !== 'object') return undefined
  return resolveClientEntry(clientExport.worker)
    ?? resolveClientEntry(clientExport.browser)
    ?? resolveClientEntry(clientExport.import)
    ?? resolveClientEntry(clientExport.default)
}

async function main() {
  assertPluginDirExists()
  console.log(`plugin-compat: проверка ${pluginDir}${checkClient ? ' (client=true)' : ''}`)

  // 1. package.json — форма, dsh.client, экспорты
  const pkg = await checkPackageJson()

  // 2. Запрещённые импорты во всех .js/.ts файлах плагина
  await checkForbiddenImports()

  // 3. Нет рантайм import() npm-спецификаторов
  await checkNoDynamicImports()

  // 4. Нет node:fs на горячем пути (проверяем, что нет синхронных fs вызовов в apply)
  await checkNoSyncFsInApply()

  // 5. Если client=true — проверка клиентского бандла
  if (checkClient) {
    await checkClientBundle(pkg)
  }

  // 6. Синтаксическая проверка всех JS файлов
  await checkSyntax()

  // 7. Схемы инструментов не нарушают контракт cordis (required: false)
  await checkToolSchemas()

  // 8. Вклад в серверный бандл (design.md «Размер»), если дан метафайл
  if (bundleMetaPath) {
    await checkBundleSizeContribution(bundleMetaPath, packageName)
  }

  console.log('plugin-compat: ЗЕЛЁНЫЙ — все проверки совместимости пройдены')
}

function usage() {
  process.stderr.write(
    'Usage: node dsh-edge/check-plugin-compat.mjs <plugin-source-dir> [--client] '
    + '[--bundle-meta <esbuild-metafile> --package <package-name>] | --detect\n',
  )
  process.exit(2)
}

async function readPackageJson() {
  return JSON.parse(await readFile(join(absPluginDir, 'package.json'), 'utf8'))
}

async function checkPackageJson() {
  const pkg = await readPackageJson()

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
  return pkg
}

// design.md «Workers-совместимость» задаёт ЧЁРНЫЙ список запрещённых node:*
// (см. FORBIDDEN_NODE_BUILTINS ниже) и явно разрешает только node:path/node:url —
// но не запрещает остальные builtin'ы Workers nodejs_compat. Раньше здесь был
// отдельный WHITE-список (`ALLOWED_NODE_IMPORTS`) на девять модулей, строже
// самой спеки: легальный `node:zlib` (есть в nodejs_compat, не упомянут в
// design.md вовсе) красил бы прогон только потому, что не значился в списке
// разрешённых, а сопоставление шло через `startsWith` — `node:crypto-bogus`
// проходил как «разрешённый node:crypto» (находка AI-ревью PR #271). Единый
// источник правды теперь один — чёрный список FORBIDDEN_NODE_BUILTINS,
// используемый и здесь (dynamic import), и в checkForbiddenImports
// (static import/require), с точным сопоставлением имени модуля
// (см. isForbiddenNodeBuiltin).

// Запрещённые Node builtin-модули (обе формы — с префиксом node: и голая) и
// npm-пакеты нативных аддонов. Раньше каждая форма импорта прописывалась
// вручную парой from/require на модуль — третья форма, голый side-effect
// `import 'node:child_process'` (без from), не проверялась вовсе и давала
// ЗЕЛЁНЫЙ (находка AI-ревью PR #271, мутационно доказана). Паттерны теперь
// генерируются программно на все три формы разом — новый запрещённый
// спецификатор не может забыть форму импорта.
const FORBIDDEN_NODE_BUILTINS = [
  { name: 'fs', note: 'используйте VFS Computer /workspace' },
  { name: 'child_process', note: null },
  { name: 'net', note: null },
  { name: 'dgram', note: null },
  { name: 'module', note: 'динамические части недоступны' },
]

const FORBIDDEN_PACKAGES = [
  { name: 'koffi', note: 'нативные аддоны недоступны' },
  { name: 'node-pty', note: 'нативные аддоны недоступны' },
]

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * from/require/side-effect формы импорта одного спецификатора.
 * `allowSubpath`: для builtin'ов вида `node:fs` матчит и подпуть `node:fs/promises` —
 * иначе `import 'node:fs/promises'` (тот же запрещённый доступ к ФС) молча проходил бы,
 * потому что регекс требовал точного совпадения строки целиком.
 */
function importFormsFor(spec, label, { allowSubpath = false } = {}) {
  const esc = escapeRegExp(spec)
  const specPattern = allowSubpath ? `${esc}(?:/[^'"]*)?` : esc
  return [
    { pattern: new RegExp(`from\\s+['"]${specPattern}['"]`, 'g'), desc: label },
    { pattern: new RegExp(`require\\(['"]${specPattern}['"]\\)`, 'g'), desc: `${label} (require)` },
    { pattern: new RegExp(`import\\s+['"]${specPattern}['"]`, 'g'), desc: `${label} (side-effect import)` },
  ]
}

function buildForbiddenPatterns() {
  const patterns = []
  for (const { name, note } of FORBIDDEN_NODE_BUILTINS) {
    patterns.push(...importFormsFor(`node:${name}`, `node:${name}${note ? ` (${note})` : ''}`, { allowSubpath: true }))
    patterns.push(...importFormsFor(name, `${name} (bare import, недоступно в Workers${note ? ' — ' + note : ''})`, { allowSubpath: true }))
  }
  for (const { name, note } of FORBIDDEN_PACKAGES) {
    patterns.push(...importFormsFor(name, `${name}${note ? ` (${note})` : ''}`))
  }
  patterns.push({ pattern: /@deepseek-ai\/node-addon-landlock-run/g, desc: 'landlock-run (нативный аддон)' })
  return patterns
}

const FORBIDDEN_PATTERNS = buildForbiddenPatterns()

/**
 * Точное (с учётом подпути `node:name/sub`) сопоставление динамического
 * `import('node:...')` с чёрным списком FORBIDDEN_NODE_BUILTINS — единственное
 * место, решающее «этот node:-спецификатор запрещён», используется и здесь
 * (dynamic import), и подразумевается статическими паттернами выше.
 */
function isForbiddenNodeBuiltin(specifier) {
  if (!specifier.startsWith('node:')) return false
  const name = specifier.slice('node:'.length)
  return FORBIDDEN_NODE_BUILTINS.some(({ name: forbidden }) => name === forbidden || name.startsWith(`${forbidden}/`))
}

async function checkForbiddenImports() {
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

    // Динамический import('node:...') не матчится паттернами from/require/side-effect
    // выше (это вызов, а не одна из трёх статических форм) — проверяем его
    // отдельно против того же чёрного списка (isForbiddenNodeBuiltin).
    const dynamicNodeImports = [...content.matchAll(/import\s*\(\s*['"](node:[^'"]+)['"]\s*\)/g)]
      .map(m => m[1])
    for (const imp of dynamicNodeImports) {
      if (isForbiddenNodeBuiltin(imp)) {
        violations.push(`${relPath}: динамический import() ${imp} — запрещённый модуль (design.md «Workers-совместимость»)`)
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

    // Ищем import() с не-статическими аргументами (переменные, конкатенация).
    // Статические import('./relative-path') разрешены. Статические
    // import('node:builtin') тоже разрешены, ЕСЛИ builtin не в чёрном списке
    // FORBIDDEN_NODE_BUILTINS (isForbiddenNodeBuiltin) — тот же список, что и
    // checkForbiddenImports, единое место правды (иначе легальный
    // import('node:crypto') красит прогон — находка AI-ревью PR #162, #265;
    // запрещённый import('node:fs') обязан краснеть — ловится ниже, в
    // checkForbiddenImports, тем же isForbiddenNodeBuiltin). Любой другой
    // строковый литерал (npm-спецификатор без node:-префикса) design.md
    // запрещает явно («нет рантайм import() npm-спецификаторов») — такой
    // литерал тоже считается нарушением, не только переменная/конкатенация.
    //
    // Литерал обязан ЦЕЛИКОМ совпадать с аргументом (якоря ^...$ на весь
    // обрезанный текст, включая кавычки) — раньше `/^['"].\//`  проверяла
    // только ПРЕФИКС, поэтому `import('./' + name)` (конкатенация) проходила
    // как «относительный путь» (находка AI-ревью PR #271, мутационно
    // доказана: конкатенация — ровно то нестатическое поведение, которое
    // design требует запрещать).
    const RELATIVE_LITERAL = /^(['"])\.{1,2}\/[^'"]*\1$/
    const dynamicImports = [...content.matchAll(/import\s*\(\s*([^)]+)\s*\)/g)]
      .filter(m => {
        const arg = m[1].trim()
        if (RELATIVE_LITERAL.test(arg)) return false
        // Уже анкоренный на весь литерал (^...$) — конкатенация сюда не проходит.
        const nodeSpecifier = /^['"](node:[^'"]+)['"]$/.exec(arg)
        if (nodeSpecifier && !isForbiddenNodeBuiltin(nodeSpecifier[1])) return false
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
    //
    // Раньше список функций (readFileSync/writeFileSync/...) перечислялся
    // руками и был неполон: existsSync, accessSync, openSync, appendFileSync,
    // copyFileSync (и любая будущая *Sync-функция fs) проходили молча
    // (находка AI-ревью PR #271). fs.*Sync — это всегда синхронный вызов по
    // соглашению самого модуля node:fs, генеричный паттерн по суффиксу Sync
    // короче перечисления и не может забыть новую функцию.
    const syncFsCalls = [...content.matchAll(/\bfs\.\w*Sync\b/g)]
    if (syncFsCalls.length > 0) {
      violations.push(`${relPath}: синхронные fs вызовы (${syncFsCalls.length}) — в Edge ФС только VFS Computer'а`)
    }
  }

  if (violations.length > 0) {
    throw new Error('Синхронные fs вызовы:\n' + violations.map(v => `  - ${v}`).join('\n'))
  }
  console.log('  ✓ синхронные fs вызовы: не найдено')
}

// Красный прогон deploy-dsh-edge.yml дважды (2026-09-04, 2026-09-05, задача
// #314): плагин integrations объявлял опциональный параметр инструмента
// через `required: false`, схема-компилятор cordis 4 (@deepseek-ai/dsh-tools)
// принимает только `required: true` либо ПОЛНОЕ ОТСУТСТВИЕ ключа —
// `required: false` бросает `JsonSchemaError: unsupported JSON schema:
// parameters.<field>.required must be true when present` уже во время
// apply(), то есть в проде на каждом инсталле плагина. Раньше это ловил
// только dsh-edge/smoke-edge-plugins.mjs — рантайм-дым внутри
// deploy-dsh-edge.yml, ПОСЛЕ мержа PR и после сборки клона: контент плагина
// с этим дефектом мог висеть в main сутками, роняя каждый деплой по
// расписанию. Эта проверка — то же самое правило, но статически и до
// публикации/деплоя (форж вызывает check-plugin-compat.mjs перед сборкой
// tarball'а), чтобы дефектная схема не доезжала до рантайма вовсе.
//
// Проверка текстовая (regex), не AST: в этом файле уже так устроены
// checkForbiddenImports/checkNoDynamicImports/checkNoSyncFsInApply — общий
// стиль чекера. `required: false` не встречается ни в одном легальном месте
// схемы инструмента ни у одного плагина этого репозитория (hello-world,
// runner-bridge, plugin-manager): опциональное свойство параметров/output
// всегда объявляется отсутствием ключа required, а не false-значением,
// поэтому точное совпадение — не эвристика, а прямой признак дефекта этого
// класса. Комментарии вырезаются перед матчингом (см. stripComments): при
// починке этого самого дефекта пояснительный комментарий рядом с фиксом
// сам содержал искомую строку и красил чекер на собственном тексте —
// мутация поймана вживую, не гипотетически.
function stripComments(source) {
  // Черновой, не JS-парсер: строковые литералы с `//`/`/*` внутри (например,
  // URL в строке) теоретически могут исказить границу комментария, но для
  // цели чекера (не дать `required: false` просочиться в КОД) это приемлемо —
  // тот же компромисс, что и у соседних regex-проверок этого файла.
  return source
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/.*$/gm, '')
}

async function checkToolSchemas() {
  const files = await collectJsFiles(absPluginDir)
  let violations = []

  for (const file of files) {
    const content = stripComments(await readFile(file, 'utf8'))
    const relPath = relative(absPluginDir, file)

    const matches = [...content.matchAll(/required\s*:\s*false/g)]
    if (matches.length > 0) {
      violations.push(`${relPath}: required: false (${matches.length} вхождений) — контракт cordis 4 требует true либо отсутствие ключа`)
    }
  }

  if (violations.length > 0) {
    throw new Error(
      'Схема инструментов нарушает контракт cordis (UNSUPPORTED_SCHEMA в рантайме):\n'
      + violations.map(v => `  - ${v}`).join('\n'),
    )
  }
  console.log('  ✓ схемы инструментов: required: false не найдено')
}

async function checkClientBundle(pkg) {
  // Путь резолвится из package.json#exports['./client'] — прод-формы, по
  // которой реально резолвит потребитель пакета (require/import); условные
  // экспорты разрешает resolveClientEntry (worker → browser → import →
  // default). Раньше путь был захардкожен как client/client.js: плагин,
  // объявивший другой путь в exports, проверялся бы не по тому файлу вообще
  // (находка AI-ревью PR #271). checkPackageJson уже гарантирует наличие
  // exports['./client'], но здесь проверка независима (checkClientBundle
  // может быть вызвана и без неё в будущем).
  const entry = resolveClientEntry(pkg.exports?.['./client'])
  if (!entry) {
    throw new Error('package.json: нужен экспорт "./client" для клиентских плагинов')
  }
  if (!entry.endsWith('.js')) {
    throw new Error(`package.json: exports["./client"] должен указывать на .js бандл, найдено: ${JSON.stringify(pkg.exports['./client'])}`)
  }
  const clientPath = join(absPluginDir, entry)

  let content
  try {
    content = await readFile(clientPath, 'utf8')
  } catch {
    // Файла на диске нет — легально, ЕСЛИ он генерируется на этапе сборки:
    // либо npm-скрипт package.json#scripts.build, либо (прод-конвенция этого
    // репозитория, см. plugins-src/*/build.mjs) отдельный build.mjs в
    // каталоге плагина, запускаемый напрямую (node .../build.mjs), а не
    // через "npm run build". Раньше в этой ветке ставилась зелёная галочка
    // «будет собран на этапе сборки» просто по наличию exports['./client'] —
    // не проверяя, что механизм сборки вообще существует: плагин без единого
    // build-шага и без файла на диске проходил молча (находка AI-ревью PR
    // #271). Чекер сам сборку НЕ запускает (это дело реальной сборки
    // tarball'а в форже, не dev-гейта совместимости), поэтому в форже шаг
    // сборки клиентского бандла стоит ДО этого чекера: здесь форма
    // проверяется на реальном артефакте, а предупреждение достаётся только
    // ручным прогонам без сборки.
    const hasBuildScript = typeof pkg.scripts?.build === 'string'
    const hasBuildScriptFile = existsSync(join(absPluginDir, 'build.mjs'))
    if (!hasBuildScript && !hasBuildScriptFile) {
      throw new Error(
        `package.json: exports['./client'] указывает на ${entry}, файла нет на диске, ` +
        'и у плагина нет ни package.json#scripts.build, ни build.mjs — взяться файлу неоткуда',
      )
    }
    console.warn(
      `plugin-compat: предупреждение — ${entry} будет собран на этапе сборки, ` +
      'форма (window.__ModuleLoader__.load) не проверялась',
    )
    return
  }

  // Проверка обёртки ModuleLoader
  if (!content.includes('window.__ModuleLoader__.load')) {
    throw new Error(`${entry}: отсутствует обёртка window.__ModuleLoader__.load — невалидная форма ростера`)
  }

  // Проверка exports.inject: раньше условие было `!includes('exports.inject')
  // && !includes('export')` — вторая половина всегда true (подстрока 'export'
  // входит в саму 'exports.inject', и в любой бандл, где вообще есть export),
  // поэтому warn не срабатывал никогда (находка AI-ревью PR #271, мёртвый код).
  if (!content.includes('exports.inject')) {
    console.warn('plugin-compat: предупреждение — не найден exports.inject в клиентском бандле')
  }

  console.log(`  ✓ клиентский бандл: форма валидна (${entry})`)
}

/**
 * Вклад плагина в серверный бандл против порога-константы (design.md
 * «Размер»). Метафайл — esbuild bundle-meta.json пробной сборки; патч 0006
 * копирует его из эфемерного outdir апстрима рядом с артефактом. Наличие
 * плагина в бандле здесь не проверяется — это класс гвардии состава
 * (verify-edge-plugins.mjs); здесь — только размер.
 */
async function checkBundleSizeContribution(metaPath, pkgName) {
  if (!pkgName) usage()
  let metafile
  try {
    metafile = JSON.parse(await readFile(metaPath, 'utf8'))
  } catch (error) {
    throw new Error(
      `метафайл сборки ${metaPath} не читается (${error.message}) — патч 0006 не применён `
      + 'или пробная сборка не писала bundle-meta.json; мерить вклад нечем, падаю громко',
    )
  }
  const bytes = sumBundleContribution(metafile, pkgName)
  if (bytes > PLUGIN_BUNDLE_CONTRIBUTION_LIMIT_BYTES) {
    throw new Error(
      `вклад ${pkgName} в серверный бандл ${bytes} байт превышает потолок `
      + `${PLUGIN_BUNDLE_CONTRIBUTION_LIMIT_BYTES} (design.md «Размер»: gzip-бюджет direct-воркера `
      + '900 KiB общий) — уменьшите код плагина или вынесите тяжёлую зависимость',
    )
  }
  console.log(`  ✓ вклад в бандл: ${bytes}/${PLUGIN_BUNDLE_CONTRIBUTION_LIMIT_BYTES} байт (bytesInOutput)`)
}

async function checkSyntax() {
  const files = await collectJsFiles(absPluginDir)
  let skippedTs = 0
  for (const file of files) {
    if (file.endsWith('.ts')) {
      // node --check не парсит TypeScript-синтаксис (типы, generics, ...) —
      // легальный .ts-файл красил бы прогон бессмысленным SyntaxError
      // (находка AI-ревью PR #271, мутационно доказана: `const x: number = 1`
      // в реальном .ts плагине). В репозитории пока нет TS-плагинов; когда
      // появятся, здесь нужен tsc/esbuild --noEmit, а не node --check —
      // .ts осознанно пропускается, а не тихо считается прошедшим.
      skippedTs++
      continue
    }
    const result = spawnSync(process.execPath, ['--check', file], { stdio: 'pipe' })
    if (result.status !== 0) {
      throw new Error(`node --check не прошёл для ${relative(absPluginDir, file)}:\n${result.stderr.toString()}`)
    }
  }
  if (skippedTs > 0) {
    console.warn(`plugin-compat: предупреждение — ${skippedTs} .ts файл(ов) пропущено в проверке синтаксиса (node --check не понимает TypeScript)`)
  }
  console.log('  ✓ синтаксис: все файлы валидны')
}

// Кэш по каталогу плагина — collectJsFiles вызывается из пяти разных
// проверок с одним и тем же absPluginDir; без кэша npm pack --dry-run
// запускался бы 5 раз за прогон.
const jsFilesCache = new Map()

// Расширения, которые чекер реально разбирает (forbidden imports, dynamic
// import, sync fs, синтаксис). .cjs раньше сюда не входил — единственный
// канонический способ положить CommonJS-файл в tarball при "type": "module"
// целиком пропадал из ВСЕХ проверок разом, а не только из синтаксической
// (плагин с legacy.cjs, `require('child_process')` и `spawnSync` внутри,
// уходил зелёным: находка AI-ревью PR #271, мутационно доказана). node --check
// разбирает .cjs как обычный CommonJs-синтаксис, никакого спецкейса не нужно.
const CHECKED_EXTENSIONS = ['.js', '.mjs', '.cjs', '.ts']

// Остальные расширения одного семейства JS/TS, которые тоже могут нести
// исполняемый код, но чекер их пока не разбирает вовсе. Плагин с .cjs раньше
// пропадал из отчёта молча — единственный сигнал о непокрытом файле был для
// .ts (предупреждение о пропуске синтаксиса в checkSyntax). Любой файл,
// похожий на код, но не в CHECKED_EXTENSIONS, обязан быть назван вслух, а не
// просто исчезнуть из выборки — иначе «все файлы валидны» врёт о покрытии.
const UNCHECKED_CODE_EXTENSIONS = ['.jsx', '.tsx', '.mts', '.cts']

/**
 * Состав файлов плагина — берём из `npm pack --dry-run` (прод-форма: это
 * ровно то, что уедет в tarball, который форж публикует и который апстрим
 * реально ставит через pnpm add). Раньше состав вычислялся самодельным
 * minimatch поверх package.json#files — расходился с настоящей npm-семантикой
 * `**` (требовал разделитель там, где npm его не требует) и применял фильтр
 * только на верхнем уровне рекурсии (в подкаталогах package.json не находился
 * повторно, и туда утекала проверка всех файлов без фильтра). Итог: плагин с
 * `files: ["**\/*.js"]` и запрещённым импортом в корневом index.js проходил
 * зелёным (находка AI-ревью PR #271, мутационно доказана).
 *
 * `--offline --ignore-scripts`: чекер только читает состав, не должен ходить
 * в сеть или исполнять prepack/postpack-скрипты плагина (это дело реальной
 * сборки tarball'а в форже, не dev-гейта совместимости).
 */
async function collectJsFiles(dir) {
  if (jsFilesCache.has(dir)) return jsFilesCache.get(dir)

  // На Windows `npm` — это npm.cmd, а не PE-бинарник; spawnSync без shell:true
  // падает с ENOENT (проверено локально), а вызов 'npm.cmd' напрямую без shell
  // падает с EINVAL (известное ограничение Node на .cmd-файлах). CI-раннер —
  // Linux, где голый 'npm' работает без shell; shell:true нужен только на
  // win32 для локальной разработки, аргументы здесь — статические литералы,
  // так что DEP0190 (экранирование) не риск.
  const result = spawnSync('npm', ['pack', '--dry-run', '--json', '--offline', '--ignore-scripts'], {
    cwd: dir,
    encoding: 'utf8',
    shell: process.platform === 'win32',
  })
  if (result.status !== 0) {
    throw new Error(`npm pack --dry-run не смог перечислить состав пакета в ${relative(repoRoot, dir)}:\n${(result.stderr || result.error?.message || '').toString()}`)
  }

  let parsed
  try {
    parsed = JSON.parse(result.stdout)
  } catch (error) {
    throw new Error(`npm pack --dry-run вернул невалидный JSON в ${relative(repoRoot, dir)}: ${error.message}`)
  }
  const entry = parsed[0]
  if (!entry || !Array.isArray(entry.files)) {
    throw new Error(`npm pack --dry-run: неожиданная форма ответа в ${relative(repoRoot, dir)}`)
  }

  const allPaths = entry.files.map(f => f.path)

  const uncheckedCode = allPaths.filter(p => UNCHECKED_CODE_EXTENSIONS.some(ext => p.endsWith(ext)))
  for (const p of uncheckedCode) {
    console.warn(`plugin-compat: предупреждение — ${p} пропущен (расширение чекером не разбирается)`)
  }

  const files = allPaths
    .filter(p => CHECKED_EXTENSIONS.some(ext => p.endsWith(ext)))
    .map(p => join(dir, p))

  jsFilesCache.set(dir, files)
  return files
}

// CLI-режим — только при запуске напрямую (паттерн manifest.mjs): при импорте
// как библиотеки (юнит-тесты) модуль только экспортирует чистые функции.
const invokedDirectly = process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href
if (invokedDirectly) {
  if (detectOnly) {
    assertPluginDirExists()
    const pkg = await readPackageJson()
    process.stdout.write(`${JSON.stringify(detectServerClient(pkg))}\n`)
  } else {
    // Флаг без пары не имеет права тихо пропускать замер: argValue вернул бы
    // undefined, шаг 8 не выполнился, а чекер рапортовал бы «ЗЕЛЁНЫЙ» без
    // проверки размера — silent-skip там, где всё остальное падает громко.
    if (args.includes('--bundle-meta') && (bundleMetaPath === undefined || packageName === undefined)) {
      process.stderr.write(
        'plugin-compat: --bundle-meta требует пару аргументов: '
        + '--bundle-meta <esbuild-metafile> --package <package-name>\n',
      )
      process.exit(2)
    }
    main().catch(err => {
      console.error(`plugin-compat: ОШИБКА — ${err.message}`)
      process.exit(1)
    })
  }
}
