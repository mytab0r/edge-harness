#!/usr/bin/env node
/**
 * Гвардия «наш каталог не отравляет гвардию литерала namespace» (находка
 * ревью PR #453, п.2). verify-provider-namespace.mjs доказывает деплою, что
 * приватный литерал апстрима NS$1 = "llm-pi-ai" ещё жив в клиентском
 * бандле — но эта проверка ищет литерал ПОДСТРОКОЙ по всему dist, а
 * dsh-edge/plugins-catalog.json рендерится в клиентский бандл плагином
 * plugin-manager (client:true) — брифы каталога попадают в dist БУКВАЛЬНО.
 * Живой прецедент: plugin-manager 0.1.4 вшил в свой собственный каталог
 * бриф со строкой "llm-pi-ai" — итоговый dist прошёл гвардию литерала
 * зелёным ДАЖЕ если бы упстрим в тот же момент переименовал NS$1, потому
 * что мимо литерала прошла ЧУЖАЯ, случайно совпавшая строка, а не признак
 * живой кнопки. Починено бампом на 0.1.5 (этот PR), но сам класс — «наш
 * текст в каталоге может случайно повторить приватный литерал апстрима» —
 * не привязан к конкретному релизу plugin-manager: эта гвардия не даёт
 * рецидиву пройти мимо CI незамеченным.
 *
 * Использование: node dsh-edge/verify-catalog-namespace-literal.mjs
 *   Без аргументов — сканирует dsh-edge/plugins.json и
 *   dsh-edge/plugins-catalog.json относительно cwd репозитория.
 */
import { readFileSync } from 'node:fs'

export const NAMESPACE_LITERAL = 'llm-pi-ai'

/** Файлы, чьё содержимое рендерится в клиентский бандл через plugin-manager
 * (client:true) и потому обязано оставаться без приватного литерала
 * апстрима — иначе оно ложно подтверждает гвардию верификации namespace. */
export const CATALOG_FILES = [
  'dsh-edge/plugins.json',
  'dsh-edge/plugins-catalog.json',
]

/** files — необязательный список путей (для теста); по умолчанию CATALOG_FILES. */
export function findLiteralHits(files = CATALOG_FILES) {
  const hits = []
  for (const path of files) {
    let text
    try {
      text = readFileSync(path, 'utf8')
    } catch (error) {
      hits.push({ path, error: error instanceof Error ? error.message : String(error) })
      continue
    }
    if (text.includes(NAMESPACE_LITERAL)) hits.push({ path })
  }
  return hits
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const hits = findLiteralHits()
  const broken = hits.filter(h => h.error)
  if (broken.length > 0) {
    for (const h of broken) {
      process.stderr.write(`::error::Не удалось прочитать ${h.path}: ${h.error}\n`)
    }
    process.exit(1)
  }
  if (hits.length > 0) {
    process.stderr.write(
      `::error::Литерал "${NAMESPACE_LITERAL}" найден в ${hits.map(h => h.path).join(', ')} — `
      + 'эти файлы рендерятся в клиентский бандл плагином plugin-manager и попадают в dist. '
      + 'Случайное совпадение с приватным литералом NS$1 апстрима (dsh-client-ui-settings-models) '
      + 'молча отравит verify-provider-namespace.mjs: гвардия останется зелёной, даже если '
      + 'упстрим переименует NS$1 (живой прецедент — plugin-manager 0.1.4, находка ревью PR #453). '
      + 'Перефразируй текст без точного совпадения строки.\n',
    )
    process.exit(1)
  }
  process.stdout.write(
    `verify-catalog-namespace-literal: литерал "${NAMESPACE_LITERAL}" отсутствует в ${CATALOG_FILES.join(', ')}\n`,
  )
}
