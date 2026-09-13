/**
 * Чистая логика инструментов интеграций (#115) — БЕЗ импортов, чтобы жила
 * и в воркере морды (esbuild бандлит относительный импорт), и в голом Node
 * тестов (node --test кормит её прод-формой ответов внешних API).
 *
 * Правила, общие с runner-bridge (#95) и усиленные для интеграций:
 *  - конфиг читается из env воркера по именам; наружу идут ТОЛЬКО имена,
 *    никогда значения;
 *  - каждое сообщение для агента проходит scrub(): значение секрета, его
 *    base64-форма и ФАКТИЧЕСКОЕ значение заголовка Basic (base64 пары
 *    user:секрет — оно не равно склейке base64 частей) заменяются маской,
 *    если вдруг всплывают в тексте ответа чужого API — GitHub маскирует
 *    только точное совпадение, поэтому производные не светятся уже на нашей
 *    стороне;
 *  - ошибки чужого API несут только HTTP-статус и сообщение провайдера.
 */

// ── Конфиг ────────────────────────────────────────────────────────────────────

/**
 * Читает имена из env; возвращает { values, missing }. Пустая строка = «нет»:
 * наполовину заполненный конфиг должен падать громко, а не ходить в API
 * с пустым паролем.
 */
export function readConfig(env, names) {
  const values = {}
  const missing = []
  for (const name of names) {
    const value = env?.[name]
    if (typeof value === 'string' && value.trim() !== '') {
      values[name] = value.trim()
    } else {
      missing.push(name)
    }
  }
  return { values, missing }
}

/** Нормализованный http(s) базовый URL без слэша на конце; null — невалиден. */
export function normalizeBaseUrl(raw) {
  if (typeof raw !== 'string' || raw.trim() === '') return null
  try {
    const url = new URL(raw.trim())
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null
    return url.origin + (url.pathname.replace(/\/+$/, ''))
  } catch {
    return null
  }
}

// ── Маскирование (правило критерия: производные секрета не светятся) ──────────

/**
 * Значение заголовка Basic-авг для пары user:secret — ФАКТИЧЕСКАЯ производная,
 * которую надо снять с текста (base64 пары НЕ равен склейке base64 частей —
 * выравнивание на блок base64 зависит от длины user).
 */
export function basicAuthMask(user, secret) {
  if (typeof user !== 'string' || typeof secret !== 'string' || user === '' || secret === '') return null
  return Buffer.from(`${user}:${secret}`, 'utf8').toString('base64')
}

/**
 * Снимает с текста каждый элемент masks (значение секрета, его base64-форму,
 * значение заголовка Basic). Вызывается на КАЖДОМ тексте, уходящем агенту.
 * Маски короче 8 символов пропускаются (маскирование короткой строки изрезало
 * бы обычный текст) — их защищает то, что в вывод они не попадают вовсе.
 */
export function scrub(text, masks) {
  let result = String(text)
  for (const mask of masks) {
    if (typeof mask !== 'string' || mask.length < 8) continue
    while (result.includes(mask)) result = result.replaceAll(mask, '***')
  }
  return result
}

/**
 * Полный список масок одной интеграции: значения секретов, их base64-формы
 * и производные пар (Basic-заголовок). Производные собирает вызывающий
 * (index.js знает пары user:secret), здесь — единая точка применения.
 */
export function masksFor(secretValues, derivedValues = []) {
  const masks = []
  for (const secret of secretValues) {
    if (typeof secret !== 'string') continue
    masks.push(secret)
    try {
      masks.push(Buffer.from(secret, 'utf8').toString('base64'))
    } catch {
      // Не кодируется — остаётся маскирование по значению.
    }
  }
  for (const derived of derivedValues) {
    if (typeof derived === 'string') masks.push(derived)
  }
  return masks
}

/** Секретные значения конфигурации одной интеграции (в маски, не в вывод). */
export function secretValuesOf(values, names) {
  return names.map((name) => values[name]).filter((value) => typeof value === 'string')
}

// ── Ошибки (общая форма всех инструментов) ────────────────────────────────────

/** Человекочитательная причина отказа чужого API: статус + сообщение, скрабится. */
export async function describeFailure(response, masks) {
  let detail = ''
  try {
    const data = await response.json()
    if (data !== null && typeof data === 'object') {
      // Формы ошибок: Jira {errorMessages: [], errors: {}}, Bitbucket
      // {error: {message}}, Confluence {message} или {data}..., Slack {error}.
      if (typeof data.message === 'string') detail = data.message
      else if (data.error !== null && typeof data.error === 'object' && typeof data.error.message === 'string') detail = data.error.message
      else if (typeof data.error === 'string') detail = data.error
      else if (Array.isArray(data.errorMessages) && data.errorMessages.length > 0) detail = data.errorMessages.join('; ')
    }
  } catch {
    // Тело не JSON — остаётся один статус; это не ошибка чтения.
  }
  return scrub(detail === '' ? `HTTP ${response.status}` : `HTTP ${response.status}: ${detail}`, masks)
}

/** Громкий отказ конфигурации — агент озвучит это пользователю дословно. */
export function configError(tool, missing, integration) {
  return {
    ok: false,
    message: `${tool} не настроен: в воркере нет ${missing.map((name) => name).join(', ')} `
      + `(интеграция «${integration}» в разделе «Интеграции» морды). `
      + 'Починить может только владелец: добавить секреты репозитория с этими именами и передеплоить морду.',
  }
}

export function networkError(tool, error, masks, timeoutMs) {
  const timedOut = error instanceof Error && error.name === 'TimeoutError'
  const reason = timedOut
    ? `таймаут ${timeoutMs / 1000} с`
    : `сеть недоступна: ${error instanceof Error ? error.message : String(error)}`
  return { ok: false, message: `${tool}: ${scrub(reason, masks)}. Попробуй ещё раз позже.` }
}

/** Сигнал вызова: отмена хода агента + жёсткий таймаут на каждый fetch. */
export function callSignal(exec, timeoutMs) {
  const timeout = AbortSignal.timeout(timeoutMs)
  return exec?.signal ? AbortSignal.any([exec.signal, timeout]) : timeout
}

// ── Jira (Cloud REST v3, Basic auth email:token) ──────────────────────────────

export function jiraIssuePath(baseUrl, issue) {
  return `${baseUrl}/rest/api/3/issue/${encodeURIComponent(issue)}?fields=summary,status,comment`
}

/** Последняя страница комментариев (fields=comment отдаёт первую): startAt = total - limit. */
export function jiraLatestCommentsPath(baseUrl, issue, total, limit) {
  const startAt = Math.max(0, total - limit)
  return `${baseUrl}/rest/api/3/issue/${encodeURIComponent(issue)}/comment?startAt=${startAt}&maxResults=${limit}`
}

/**
 * Atlassian Document Format → плоский текст. Прод-форма узлов:
 * {type:"doc"|"paragraph"|"bulletList"|…, content:[…], text?}. Инлайновые
 * содержимые (массивы text-узлов) склеиваются встык, блочные — переносами
 * строк. Неизвестный узел рекурсивно проходится — формат расширяется
 * Atlassian-ом без спроса.
 */
export function adfToText(node) {
  if (node === null || node === undefined) return ''
  if (typeof node === 'string') return node
  if (Array.isArray(node)) {
    const parts = node.map(adfToText).filter((part) => part !== '')
    const inline = node.length > 0 && node.every((child) => child !== null && typeof child === 'object' && typeof child.text === 'string')
    return parts.join(inline ? '' : '\n')
  }
  if (typeof node !== 'object') return ''
  if (typeof node.text === 'string') return node.text
  if (Array.isArray(node.content)) return adfToText(node.content)
  return ''
}

/** Строка «автор: текст (дата)» из комментария прод-формы. */
export function renderJiraComment(comment) {
  const author = comment?.author?.displayName ?? 'автор не назван'
  const text = adfToText(comment?.body).trim()
  const when = typeof comment?.created === 'string' ? comment.created : ''
  return `${author}: ${text === '' ? '(пустой комментарий)' : text}${when === '' ? '' : ` (${when})`}`
}

/**
 * Рендер задачи из прод-формы GET /issue/{key}. Комментарии рендерятся как
 * пришли; когда в задаче их больше, чем на странице, вызывающий добирает
 * последнюю страницу (jiraLatestCommentsPath) и передаёт её вторым аргументом.
 */
export function renderJiraIssue(issue, latestComments) {
  if (issue === null || typeof issue !== 'object' || typeof issue.key !== 'string') {
    return null
  }
  const fields = issue.fields ?? {}
  const lines = []
  lines.push(`Задача ${issue.key}${typeof fields.summary === 'string' ? ` «${fields.summary}»` : ''}.`)
  const status = fields.status?.name
  lines.push(`Статус: ${typeof status === 'string' ? status : 'не читается'}.`)
  const page = fields.comment ?? {}
  const comments = Array.isArray(latestComments) ? latestComments : (Array.isArray(page.comments) ? page.comments : [])
  const total = typeof page.total === 'number' ? page.total : comments.length
  if (comments.length === 0) {
    lines.push('Комментариев нет.')
  } else {
    lines.push(`Комментарии (последние ${comments.length} из ${total}):`)
    for (const comment of comments) lines.push(`- ${renderJiraComment(comment)}`)
  }
  return lines.join('\n')
}

/**
 * Комментарии добраны, если страница полная относительно total — иначе рендер
 * уже честно назвал «N из total», и второй вызов не нужен.
 */
export function needLatestComments(issue) {
  const page = issue?.fields?.comment
  if (page === null || typeof page !== 'object') return null
  const total = typeof page.total === 'number' ? page.total : null
  const have = Array.isArray(page.comments) ? page.comments.length : null
  if (total === null || have === null) return null
  return total > have ? { total } : null
}

// ── Confluence (REST v1 /rest/api, работает и на cloud /wiki, и на DC) ────────

export function confluenceSearchPath(baseUrl, cql, limit) {
  return `${baseUrl}/rest/api/content/search?cql=${encodeURIComponent(cql)}&limit=${limit}`
}

export function confluencePagePath(baseUrl, pageId) {
  return `${baseUrl}/rest/api/content/${encodeURIComponent(pageId)}?expand=body.storage,version`
}

/** Ссылка на страницу из прод-формы _links {webui, base}. */
function confluenceLink(page) {
  const base = page?._links?.base
  const webui = page?._links?.webui
  if (typeof base === 'string' && typeof webui === 'string') return base + webui
  return null
}

export function renderConfluenceResults(results) {
  if (!Array.isArray(results) || results.length === 0) return 'Страниц по CQL не нашлось.'
  const lines = ['Найденные страницы:']
  for (const page of results) {
    const link = confluenceLink(page)
    lines.push(`- ${typeof page?.title === 'string' ? page.title : 'без названия'} (id ${page?.id ?? '?'})`
      + `${link === null ? '' : ` — ${link}`}`)
  }
  return lines.join('\n')
}

// Потолок тела страницы: инструмент не должен тащить в контекст агента мегабайт
// XHTML. Обрезка объявленная: хвост помечен явно, не молча.
const MAX_PAGE_BODY_CHARS = 20_000

export function renderConfluencePage(page) {
  if (page === null || typeof page !== 'object' || typeof page.title !== 'string') return null
  const version = page.version?.number
  const link = confluenceLink(page)
  const body = page.body?.storage?.value
  const lines = [`Страница «${page.title}» (id ${page.id ?? '?'}, версия ${typeof version === 'number' ? version : 'не читается'}).`]
  if (link !== null) lines.push(`Ссылка: ${link}`)
  if (typeof body === 'string' && body !== '') {
    lines.push('Тело (XHTML storage-формат):')
    lines.push(body.length > MAX_PAGE_BODY_CHARS
      ? `${body.slice(0, MAX_PAGE_BODY_CHARS)}\n…[обрезано, всего символов: ${body.length}]`
      : body)
  } else {
    lines.push('Тело не читается (body.storage пуст).')
  }
  return lines.join('\n')
}

// ── Slack (Web API chat.postMessage) ──────────────────────────────────────────

export const SLACK_API = 'https://slack.com/api/chat.postMessage'

export function slackPostBody(args) {
  return { channel: args.channel, text: args.text }
}

/**
 * Slack отвечает HTTP 200 даже на ошибку — решает поле ok прод-формы
 * ({ok:true, channel, ts} | {ok:false, error:"channel_not_found"}).
 */
export function renderSlackResult(data) {
  if (data === null || typeof data !== 'object' || typeof data.ok !== 'boolean') return null
  if (data.ok === true) {
    const channel = typeof data.channel === 'string' ? data.channel : 'канал не назван'
    return `Сообщение отправлено в ${channel}${typeof data.ts === 'string' ? ` (ts ${data.ts})` : ''}.`
  }
  const reason = typeof data.error === 'string' ? data.error : 'причина не названа'
  return `Slack не принял сообщение: ${reason}. Проверь канал (приглашён ли бот) и token scope chat:write.`
}

// ── Bitbucket Cloud (REST 2.0, Basic auth user:app_password) ──────────────────

const BITBUCKET_API = 'https://api.bitbucket.org/2.0'

/** repository обязан быть формой workspace/slug — часть пути, не query. */
export function bitbucketRepository(repository) {
  if (typeof repository !== 'string') return null
  const trimmed = repository.trim()
  if (!/^[^/\s]+\/[^/\s]+$/.test(trimmed)) return null
  return trimmed
}

export function bitbucketPrPath(repository) {
  return `${BITBUCKET_API}/repositories/${repository}/pullrequests`
}

export function bitbucketPrBody(args) {
  return {
    title: args.title,
    source: { branch: { name: args.source } },
    destination: { branch: { name: args.destination } },
    ...(typeof args.description === 'string' && args.description !== '' ? { description: args.description } : {}),
  }
}

export function renderBitbucketPr(pull) {
  if (pull === null || typeof pull !== 'object' || typeof pull.id !== 'number') return null
  const link = pull.links?.html?.href
  return `PR #${pull.id} «${typeof pull.title === 'string' ? pull.title : ''}» создан: `
    + `${typeof link === 'string' ? link : 'ссылка не читается'}. Скажи пользователю ссылку.`
}
