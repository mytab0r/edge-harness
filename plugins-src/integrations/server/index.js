/**
 * integrations: серверная половина плагина интеграций (#115).
 *
 * Cordis-плагин по образцу runner-bridge (#95): даёт агенту чата морды четыре
 * инструмента внешних систем, каждый — один REST-вызов (I/O, не CPU: тяжёлая
 * работа по-прежнему уезжает раннеру через runner_task, исполнение интеграций
 * в DO не тащится — 30-rejected п.6). Telegram в этом плагине отсутствует
 * намеренно: его транспорт — job'ы раннеров (эскалации #91), реестр объявляет
 * его с wired: "jobs".
 *
 *   - jira_issue      — статус и комментарии задачи из Jira Cloud (REST v3);
 *   - confluence_page — поиск CQL и чтение страницы Confluence (REST v1);
 *   - bitbucket_pr    — создание pull request в Bitbucket Cloud (REST 2.0);
 *   - slack_post      — отчёт в канал Slack (Web API chat.postMessage).
 *
 * Сквозной сценарий эпика собирается из уже установленных частей: jira_issue →
 * runner_task (работа на раннере) → bitbucket_pr → slack_post.
 *
 * Конфигурация — env воркера по именам из реестра dsh-edge/integrations.json
 * (синхронизирует deploy-dsh-edge.yml из секретов репозитория). Значения
 * секретов никогда не попадают в вывод инструментов: каждое сообщение проходит
 * scrub() (core.js), который снимает и значение, и его base64-производную
 * (GitHub маскирует в логах только точное совпадение — производные не светим).
 * Отсутствие конфигурации — громкий текст агенту, не исключение (он озвучит
 * пользователю, какая интеграция не настроена). Каждый fetch ограничен
 * AbortSignal.timeout и отменой хода агента — зависший вызов не держит turn.
 *
 * Чистая логика (пути, тела, рендеры, маскирование) живёт в ./core.js и
 * покрыта поведенческими тестами на прод-форме ответов API; здесь — только
 * проводка defineTool.
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import {
  basicAuthMask,
  bitbucketPrBody,
  bitbucketPrPath,
  bitbucketRepository,
  callSignal,
  confluencePagePath,
  confluenceSearchPath,
  configError,
  describeFailure,
  jiraIssuePath,
  jiraLatestCommentsPath,
  masksFor,
  needLatestComments,
  networkError,
  normalizeBaseUrl,
  readConfig,
  renderBitbucketPr,
  renderConfluencePage,
  renderConfluenceResults,
  renderJiraIssue,
  renderSlackResult,
  secretValuesOf,
  slackPostBody,
  SLACK_API,
} from './core.js'

const PLUGIN_VERSION = '0.1.1'
const TOOL_TIMEOUT_MS = 15_000
// Первый же комментарий-лимит: fields=comment отдаёт первую страницу
// (максимум из настроек задачи), latest добирается отдельным вызовом.
const JIRA_LATEST_LIMIT = 5
const CONFLUENCE_SEARCH_LIMIT = 5

const JIRA_NAMES = ['JIRA_BASE_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN']
const CONFLUENCE_NAMES = ['CONFLUENCE_BASE_URL', 'CONFLUENCE_EMAIL', 'CONFLUENCE_API_TOKEN']
const BITBUCKET_NAMES = ['BITBUCKET_USER', 'BITBUCKET_APP_PASSWORD']
const SLACK_NAMES = ['SLACK_BOT_TOKEN']

export default {
  name: 'edge-plugins:integrations',
  // cordis 4: сервис читается через ctx.<service> только при объявлении в
  // inject (иначе apply падает «cannot get property … without inject») —
  // контракт runner-bridge #95 и апстримных плагинов; ловится дымом
  // dsh-edge/smoke-edge-plugins.mjs на каждом деплое.
  inject: ['tools'],
  apply(ctx) {
    ctx.effect(() => ctx.tools.register(defineJiraIssueTool()), 'edge-plugins:integrations jira_issue tool')
    ctx.effect(() => ctx.tools.register(defineConfluencePageTool()), 'edge-plugins:integrations confluence_page tool')
    ctx.effect(() => ctx.tools.register(defineBitbucketPrTool()), 'edge-plugins:integrations bitbucket_pr tool')
    ctx.effect(() => ctx.tools.register(defineSlackPostTool()), 'edge-plugins:integrations slack_post tool')
    console.info(`edge-plugin:integrations installed v${PLUGIN_VERSION} (jira_issue, confluence_page, bitbucket_pr, slack_post tools registered)`)
  },
}

/** GET с заголовками, таймаутом и отменой от агентного цикла. */
function apiFetch(url, { headers, method, body, signal }) {
  return fetch(url, { headers, method, body, signal })
}

/**
 * Маски одной Basic-интеграции: значения секретов, их base64-формы и
 * ФАКТИЧЕСКОЕ значение Basic-заголовка (base64 пары user:secret — оно не
 * равно склейке base64 частей). auth строится из той же производной, поэтому
 * под маску попадает ровно то, что реально уезжает в заголовке.
 */
function basicAuth(values, names, userKey, secretKey) {
  const derived = basicAuthMask(values[userKey], values[secretKey])
  return {
    auth: `Basic ${derived}`,
    masks: masksFor(secretValuesOf(values, names), [derived]),
  }
}

function outputSchema() {
  return {
    type: 'object',
    additionalProperties: false,
    properties: {
      ok: { type: 'boolean', required: true },
      message: { type: 'string', required: true },
    },
  }
}

function textRender() {
  return (_args, value) => [{ type: 'text', text: value.message }]
}

// ── jira_issue ────────────────────────────────────────────────────────────────

function defineJiraIssueTool() {
  return defineTool({
    name: 'jira_issue',
    description: 'Читает задачу из Jira: заголовок, статус и последние комментарии. Зови, когда '
      + 'пользователь просит посмотреть задачу Jira (например «что в PROJ-42», «прочитай комментарии») '
      + 'или когда работа начинается с задачи, заведённой в Jira. Комментарии — только чтение; писать '
      + 'в Jira инструмент не умеет.',
    parameters: {
      issue: {
        type: 'string',
        required: true,
        description: 'Ключ задачи, например PROJ-42.',
      },
    },
    output: { schema: outputSchema(), render: textRender() },
    timeoutMs: 45_000,
    async execute(args, exec) {
      const { values, missing } = readConfig(process.env, JIRA_NAMES)
      if (missing.length > 0) return configError('jira_issue', missing, 'Jira')
      const baseUrl = normalizeBaseUrl(values.JIRA_BASE_URL)
      if (baseUrl === null) {
        return { ok: false, message: 'jira_issue: JIRA_BASE_URL не похож на URL сайта (https://<site>.atlassian.net).' }
      }
      if (typeof args.issue !== 'string' || args.issue.trim() === '') {
        return { ok: false, message: 'jira_issue: нужен ключ задачи, например PROJ-42.' }
      }
      const issue = args.issue.trim()
      const { auth, masks } = basicAuth(values, JIRA_NAMES, 'JIRA_EMAIL', 'JIRA_API_TOKEN')
      const signal = callSignal(exec, TOOL_TIMEOUT_MS)

      let data
      try {
        const response = await apiFetch(jiraIssuePath(baseUrl, issue), {
          headers: { Accept: 'application/json', Authorization: auth }, signal,
        })
        if (response.status === 404) {
          return { ok: false, message: `jira_issue: задачи ${issue} на этом сайте нет (HTTP 404).` }
        }
        if (response.status !== 200) {
          return { ok: false, message: `jira_issue: Jira не отдала задачу (${await describeFailure(response, masks)}).` }
        }
        data = await response.json()
      } catch (error) {
        return networkError('jira_issue', error, masks, TOOL_TIMEOUT_MS)
      }

      const rendered = renderJiraIssue(data)
      if (rendered === null) {
        return { ok: false, message: 'jira_issue: ответ Jira не похож на задачу, хотя статус был 200.' }
      }
      // fields=comment отдаёт первую страницу комментариев: если в задаче их
      // больше — добираем последнюю страницу, у агента должны быть свежайшие.
      const need = needLatestComments(data)
      if (need !== null) {
        try {
          const response = await apiFetch(
            jiraLatestCommentsPath(baseUrl, issue, need.total, JIRA_LATEST_LIMIT),
            { headers: { Accept: 'application/json', Authorization: auth }, signal: callSignal(exec, TOOL_TIMEOUT_MS) },
          )
          if (response.status === 200) {
            const page = await response.json()
            if (Array.isArray(page?.comments)) {
              return { ok: true, message: renderJiraIssue(data, page.comments) }
            }
          }
          // Не добралось — рендер первой страницы уже честно назвал «N из total».
        } catch {
          // Украшение не отменяет статус задачи: показываем то, что есть.
        }
      }
      return { ok: true, message: rendered }
    },
  })
}

// ── confluence_page ───────────────────────────────────────────────────────────

function defineConfluencePageTool() {
  return defineTool({
    name: 'confluence_page',
    description: 'Читает Confluence: по CQL-запросу находит страницы, по id отдаёт содержимое '
      + '(XHTML storage-формат). Зови, когда пользователю нужен документ/ранбук/заметки из Confluence '
      + 'или когда перед работой надо свериться с внутренней документацией. Только чтение.',
    parameters: {
      query: {
        type: 'string',
        // Опциональный параметр объявляется ОТСУТСТВИЕМ ключа required —
        // ставить его со значением "false" контракт схем cordis 4 отклоняет
        // (UNSUPPORTED_SCHEMA: «parameters.query.required must be true when
        // present»), см. дым dsh-edge/smoke-edge-plugins.mjs и гвардию
        // check-plugin-compat.mjs (класс #314).
        description: 'CQL-поиск, например type=page AND text~"runbook". Найдёт до 5 страниц.',
      },
      page_id: {
        type: 'string',
        description: 'Id страницы (из результата поиска) — тогда вернётся содержимое.',
      },
    },
    output: { schema: outputSchema(), render: textRender() },
    timeoutMs: 45_000,
    async execute(args, exec) {
      const { values, missing } = readConfig(process.env, CONFLUENCE_NAMES)
      if (missing.length > 0) return configError('confluence_page', missing, 'Confluence')
      const baseUrl = normalizeBaseUrl(values.CONFLUENCE_BASE_URL)
      if (baseUrl === null) {
        return { ok: false, message: 'confluence_page: CONFLUENCE_BASE_URL не похож на URL сайта (для cloud — с /wiki на конце).' }
      }
      const { auth, masks } = basicAuth(values, CONFLUENCE_NAMES, 'CONFLUENCE_EMAIL', 'CONFLUENCE_API_TOKEN')
      const wantsPage = typeof args.page_id === 'string' && args.page_id.trim() !== ''
      const wantsSearch = typeof args.query === 'string' && args.query.trim() !== ''
      if (!wantsPage && !wantsSearch) {
        return { ok: false, message: 'confluence_page: нужен query (CQL-поиск) или page_id (чтение страницы).' }
      }

      try {
        if (wantsPage) {
          const response = await apiFetch(confluencePagePath(baseUrl, args.page_id.trim()), {
            headers: { Accept: 'application/json', Authorization: auth },
            signal: callSignal(exec, TOOL_TIMEOUT_MS),
          })
          if (response.status === 404) {
            return { ok: false, message: `confluence_page: страницы id ${args.page_id.trim()} нет (HTTP 404).` }
          }
          if (response.status !== 200) {
            return { ok: false, message: `confluence_page: Confluence не отдала страницу (${await describeFailure(response, masks)}).` }
          }
          const page = await response.json()
          const rendered = renderConfluencePage(page)
          if (rendered === null) {
            return { ok: false, message: 'confluence_page: ответ Confluence не похож на страницу, хотя статус 200.' }
          }
          return { ok: true, message: rendered }
        }

        const response = await apiFetch(
          confluenceSearchPath(baseUrl, args.query.trim(), CONFLUENCE_SEARCH_LIMIT),
          { headers: { Accept: 'application/json', Authorization: auth }, signal: callSignal(exec, TOOL_TIMEOUT_MS) },
        )
        if (response.status !== 200) {
          return { ok: false, message: `confluence_page: поиск не прошёл (${await describeFailure(response, masks)}).` }
        }
        const data = await response.json()
        if (data === null || typeof data !== 'object' || !Array.isArray(data.results)) {
          return { ok: false, message: 'confluence_page: ответ поиска без массива results — форма не та, хотя статус 200.' }
        }
        return { ok: true, message: renderConfluenceResults(data.results) }
      } catch (error) {
        return networkError('confluence_page', error, masks, TOOL_TIMEOUT_MS)
      }
    },
  })
}

// ── bitbucket_pr ──────────────────────────────────────────────────────────────

function defineBitbucketPrTool() {
  return defineTool({
    name: 'bitbucket_pr',
    description: 'Создаёт pull request в Bitbucket Cloud между двумя существующими ветками '
      + 'репозитория workspace/slug. Зови, когда работа сделана в ветках Bitbucket-репозитория и '
      + 'нужен PR (завершающий шаг сценария «задача Jira → работа → PR Bitbucket → отчёт Slack»). '
      + 'Ветки не создаёт: и source, и destination должны существовать.',
    parameters: {
      repository: {
        type: 'string',
        required: true,
        description: 'Репозиторий в форме workspace/slug, например myteam/service.',
      },
      title: { type: 'string', required: true, description: 'Заголовок PR.' },
      source: { type: 'string', required: true, description: 'Ветка-источник (существующая).' },
      destination: { type: 'string', required: true, description: 'Ветка-приёмник (существующая).' },
      // required не объявлен: опциональный параметр — отсутствием ключа, не
      // значением "false" (класс #314, см. комментарий у confluence_page выше).
      description: { type: 'string', description: 'Описание PR (markdown Bitbucket).' },
    },
    output: { schema: outputSchema(), render: textRender() },
    timeoutMs: 45_000,
    async execute(args, exec) {
      const { values, missing } = readConfig(process.env, BITBUCKET_NAMES)
      if (missing.length > 0) return configError('bitbucket_pr', missing, 'Bitbucket')
      const repository = bitbucketRepository(args.repository)
      if (repository === null) {
        return { ok: false, message: 'bitbucket_pr: repository обязан быть формой workspace/slug, например myteam/service.' }
      }
      for (const field of ['title', 'source', 'destination']) {
        if (typeof args[field] !== 'string' || args[field].trim() === '') {
          return { ok: false, message: `bitbucket_pr: не хватает ${field}.` }
        }
      }
      const { auth, masks } = basicAuth(values, BITBUCKET_NAMES, 'BITBUCKET_USER', 'BITBUCKET_APP_PASSWORD')

      let pull
      try {
        const response = await apiFetch(bitbucketPrPath(repository), {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            Authorization: auth,
          },
          body: JSON.stringify(bitbucketPrBody({
            title: args.title.trim(),
            source: args.source.trim(),
            destination: args.destination.trim(),
            description: typeof args.description === 'string' ? args.description : undefined,
          })),
          signal: callSignal(exec, TOOL_TIMEOUT_MS),
        })
        if (response.status !== 201) {
          return { ok: false, message: `bitbucket_pr: Bitbucket не создал PR (${await describeFailure(response, masks)}).` }
        }
        pull = await response.json()
      } catch (error) {
        return networkError('bitbucket_pr', error, masks, TOOL_TIMEOUT_MS)
      }
      const rendered = renderBitbucketPr(pull)
      if (rendered === null) {
        return { ok: false, message: 'bitbucket_pr: ответ 201, но форма PR не читается — проверь PR в Bitbucket вручную.' }
      }
      return { ok: true, message: rendered }
    },
  })
}

// ── slack_post ────────────────────────────────────────────────────────────────

function defineSlackPostTool() {
  return defineTool({
    name: 'slack_post',
    description: 'Пишет сообщение в канал Slack от имени бота. Зови, когда надо отчитаться о '
      + 'результате в канал (финальный шаг сценария «Jira → работа → PR Bitbucket → отчёт Slack») '
      + 'или уведомить команду. Текст — обычный разметка Slack (mrkdwn).',
    parameters: {
      channel: {
        type: 'string',
        required: true,
        description: 'Канал: имя (#general) или id (C0123456789). Бот должен быть приглашён в канал.',
      },
      text: { type: 'string', required: true, description: 'Текст сообщения (mrkdwn Slack).' },
    },
    output: { schema: outputSchema(), render: textRender() },
    timeoutMs: 45_000,
    async execute(args, exec) {
      const { values, missing } = readConfig(process.env, SLACK_NAMES)
      if (missing.length > 0) return configError('slack_post', missing, 'Slack')
      // Bearer-заголовок несёт сам токен — его покрывает маскирование по значению;
      // отдельной производной (как у Basic) здесь нет.
      const masks = masksFor(secretValuesOf(values, SLACK_NAMES))
      if (typeof args.channel !== 'string' || args.channel.trim() === '' || typeof args.text !== 'string' || args.text.trim() === '') {
        return { ok: false, message: 'slack_post: нужны channel и text.' }
      }
      try {
        const response = await apiFetch(SLACK_API, {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json; charset=utf-8',
            Authorization: `Bearer ${values.SLACK_BOT_TOKEN}`,
          },
          body: JSON.stringify(slackPostBody({ channel: args.channel.trim(), text: args.text })),
          signal: callSignal(exec, TOOL_TIMEOUT_MS),
        })
        if (response.status !== 200) {
          return { ok: false, message: `slack_post: Slack ответил не-200 (${await describeFailure(response, masks)}).` }
        }
        const data = await response.json()
        const rendered = renderSlackResult(data)
        if (rendered === null) {
          return { ok: false, message: 'slack_post: ответ Slack без поля ok — форма не та.' }
        }
        return { ok: data.ok === true, message: rendered }
      } catch (error) {
        return networkError('slack_post', error, masks, TOOL_TIMEOUT_MS)
      }
    },
  })
}
