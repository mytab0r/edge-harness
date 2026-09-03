#!/usr/bin/env node
/**
 * Гвардия завязки реестра провайдеров на приватный литерал апстрима (#114,
 * change dsh-edge-provider-registry, design «Цена решения»): кнопка
 * «Add custom provider» штатного Settings → Models пишет settings.mutate
 * в namespace, чьё имя ЗАШИТО в клиентском бандле литералом NS$1 = "llm-pi-ai"
 * (dsh-client-ui-settings-models, CustomProviderCard). Апстрим вправе
 * переименовать его в любом релизе без semver-сигнала — тогда кнопка тихо
 * вернётся в мёртвое состояние при зелёной сборке. Гвардия требует литерал
 * в собранном dist и роняет деплой громко, если он исчез.
 *
 * Использование: node dsh-edge/verify-provider-namespace.mjs <dist-dir>
 *   dist-dir — deploy/dist (клиентские бандлы морды). Exit 0 — литерал на
 *   месте; exit 1 с ::error — апстрим сменил литерал, деплой запрещён.
 *
 * Мутационное доказательство самой гвардии — рядом:
 * verify-provider-namespace.test.mjs (repo-ci), фидит её копией dist с
 * переименованным литералом и требует красный прогон.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

const NAMESPACE_LITERAL = 'llm-pi-ai'

const distDir = process.argv[2]
if (!distDir) {
  process.stderr.write('Usage: node verify-provider-namespace.mjs <dist-dir>\n')
  process.exit(2)
}

/** Рекурсивный список файлов dist; имя файла — часть улики в сообщении. */
function walkFiles(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    const stats = statSync(path)
    if (stats.isDirectory()) out.push(...walkFiles(path))
    else out.push(path)
  }
  return out
}

let files
try {
  files = walkFiles(distDir)
} catch (error) {
  // Нет каталога/нет доступа — громкий отказ в формате деплоя, не сырой стек.
  process.stderr.write(`::error::Не удалось обойти ${distDir}: ${error instanceof Error ? error.message : String(error)}. Деплой остановлен.\n`)
  process.exit(1)
}
let hits = []
for (const path of files) {
  // Только UTF-8-декодируемые файлы: бандлы и HTML текстовые, шрифты — нет.
  let text
  try {
    text = readFileSync(path, 'utf8')
  } catch {
    continue
  }
  if (text.includes(NAMESPACE_LITERAL)) hits.push(path)
}

if (hits.length === 0) {
  process.stderr.write(
    `::error::Литерал "${NAMESPACE_LITERAL}" не найден ни в одном файле ${distDir} `
    + `(${files.length} файлов). Апстрим переименовал namespace-литерал кнопки `
    + `'Add custom provider' (NS$1 в dsh-client-ui-settings-models): кнопка `
    + `создания провайдера молча умрёт. Обновите schema-namespace плагина `
    + `provider-registry и эту гвардию вместе, осознанно (change `
    + `dsh-edge-provider-registry, design «Цена решения»). Деплой остановлен.\n`,
  )
  process.exit(1)
}

process.stdout.write(
  `verify-provider-namespace: литерал "${NAMESPACE_LITERAL}" на месте `
  + `(${hits.length} файл(ов), например ${hits[0].replace(distDir + '/', '')})\n`,
)
