/**
 * Поведенческая гвардия чистой логики инструментов интеграций (#115).
 *
 * Кормится ПРОД-ФОРМОЙ ответов внешних API (реальные формы: Jira Cloud REST v3
 * с Atlassian Document Format, Confluence REST v1 с _links, Slack Web API,
 * Bitbucket Cloud 2.0), а не пересказом. Правила, которые здесь держатся:
 *  - значения секретов и их base64-производная (Basic-авг) не выживают ни в
 *    одном тексте, уходящем агенту (критерий #115: GitHub маскирует только
 *    точное совпадение);
 *  - ошибки конфигурации громкие и называют только ИМЕНА;
 *  - чужой ответ не по форме → null (вызывающий отвечает громким сообщением),
 *    а не выдуманный рендер.
 *
 * Запуск: node --test plugins-src/integrations/test/core.test.mjs
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import {
  adfToText,
  basicAuthMask,
  bitbucketPrBody,
  bitbucketPrPath,
  bitbucketRepository,
  configError,
  describeFailure,
  jiraIssuePath,
  jiraLatestCommentsPath,
  masksFor,
  needLatestComments,
  normalizeBaseUrl,
  readConfig,
  renderBitbucketPr,
  renderConfluencePage,
  renderConfluenceResults,
  renderJiraComment,
  renderJiraIssue,
  renderSlackResult,
  scrub,
  secretValuesOf,
} from '../server/core.js'

// Фикстуры — ЗАВЕДОМО ненастоящие строки без форматов реальных токенов
// (без префиксов xoxb-/ATATT и подобной длины), чтобы гвардить маскирование
// и не цеплять GitHub secret scanning: маскированию форма не важна.
const TOKEN = 'slack-fixture-token-115-not-real'
const APP_PASSWORD = 'atlassian-fixture-app-password-115-not-real'
// Email с длиной, НЕ кратной 3: base64('user:secret') не равен склейке
// base64('user:')+base64('secret') — именно этот случай ловит тест (ревью #115:
// выровненная фикстура маскировала дыру, где полный Basic-заголовок выживал).
const USER = 'owner@atlassian.com'

// ── Конфиг ────────────────────────────────────────────────────────────────────

test('readConfig: полный конфиг читается, половинчатый — громкий список имён', () => {
  const names = ['JIRA_BASE_URL', 'JIRA_EMAIL', 'JIRA_API_TOKEN']
  const full = readConfig({ JIRA_BASE_URL: ' https://team.atlassian.net/ ', JIRA_EMAIL: 'owner@example.com', JIRA_API_TOKEN: APP_PASSWORD }, names)
  assert.deepEqual(full.missing, [])
  assert.equal(full.values.JIRA_BASE_URL, 'https://team.atlassian.net/', 'пробелы обрезаются; слэш остаётся — его снимает normalizeBaseUrl')

  const partial = readConfig({ JIRA_BASE_URL: 'https://team.atlassian.net', JIRA_EMAIL: '' }, names)
  assert.deepEqual(partial.missing, ['JIRA_EMAIL', 'JIRA_API_TOKEN'], 'пустая строка = секрета нет (fail loud)')

  const absent = readConfig(undefined, names)
  assert.deepEqual(absent.missing, names)
})

test('normalizeBaseUrl: слэш срезается, путь (/wiki) сохраняется, мусор → null', () => {
  assert.equal(normalizeBaseUrl('https://team.atlassian.net/'), 'https://team.atlassian.net')
  assert.equal(normalizeBaseUrl('https://team.atlassian.net/wiki///'), 'https://team.atlassian.net/wiki')
  assert.equal(normalizeBaseUrl('ftp://team.atlassian.net'), null)
  assert.equal(normalizeBaseUrl('не URL'), null)
  assert.equal(normalizeBaseUrl(undefined), null)
})

// ── Маскирование ──────────────────────────────────────────────────────────────

const BASIC_DERIVED = basicAuthMask(USER, APP_PASSWORD)

test('basicAuthMask: производная — это base64 ФАКТИЧЕСКОЙ пары, не склейка частей', () => {
  const full = Buffer.from(`${USER}:${APP_PASSWORD}`, 'utf8').toString('base64')
  assert.equal(BASIC_DERIVED, full)
  const glued = Buffer.from(`${USER}:`, 'utf8').toString('base64')
    + Buffer.from(APP_PASSWORD, 'utf8').toString('base64')
  assert.notEqual(BASIC_DERIVED, glued, 'на невыровненной длине склейка ≠ производной — иначе тест ничего не ловит')
})

test('scrub: значение секрета, его base64 и полный Basic-заголовок не выживают', () => {
  const masks = masksFor([TOKEN, APP_PASSWORD], [BASIC_DERIVED])
  const text = `ошибка у значения ${TOKEN}, у basic ${BASIC_DERIVED} и у base64 значения `
    + `${Buffer.from(APP_PASSWORD, 'utf8').toString('base64')}, дальше чисто`
  const clean = scrub(text, masks)
  assert.ok(!clean.includes(TOKEN), 'значение секрета осталось в тексте')
  assert.ok(!clean.includes(BASIC_DERIVED), 'фактическая производная Basic осталась в тексте')
  assert.ok(!clean.includes(Buffer.from(APP_PASSWORD, 'utf8').toString('base64')), 'base64 значения остался в тексте')
  assert.ok(clean.includes('***'))
  assert.ok(clean.includes('дальше чисто'), 'обычный текст не пострадал')
})

test('scrub: производная для выровненного случая тоже снимается', () => {
  // Граница выравнивания: user с длиной, кратной 3, — здесь склейка совпадает
  // с производной, но честная маска снимает её независимо от арифметики.
  const alignedUser = 'abc'
  const derived = basicAuthMask(alignedUser, APP_PASSWORD)
  const clean = scrub(`Bearer ${derived} — утечка`, masksFor([APP_PASSWORD], [derived]))
  assert.ok(!clean.includes(derived))
})

test('scrub: короткие значения не маскируются по вхождению (изрезали бы текст)', () => {
  assert.equal(scrub('abc и остальное', ['abc']), 'abc и остальное')
})

test('secretValuesOf: отдаёт только строковые значения названных имён', () => {
  assert.deepEqual(
    secretValuesOf({ A: 'value-115-secret', B: undefined }, ['A', 'B']),
    ['value-115-secret'],
  )
})

// ── Ошибки ────────────────────────────────────────────────────────────────────

function stubResponse({ status = 400, body }) {
  return {
    status,
    json: async () => {
      if (body === 'NOT_JSON') throw new SyntaxError('Unexpected token')
      return body
    },
  }
}

test('describeFailure: формы ошибок Jira/Bitbucket/Slack читаются, секрет скрабится', async () => {
  const jira = await describeFailure(
    stubResponse({ status: 403, body: { errorMessages: ['You do not have permission'], errors: {} } }),
    [APP_PASSWORD],
  )
  assert.equal(jira, 'HTTP 403: You do not have permission')

  const bitbucket = await describeFailure(
    stubResponse({ status: 400, body: { type: 'error', error: { message: `Bad request near ${APP_PASSWORD}, header ${BASIC_DERIVED}` } } }),
    masksFor([APP_PASSWORD], [BASIC_DERIVED]),
  )
  assert.ok(!bitbucket.includes(APP_PASSWORD), 'секрет из сообщения чужого API дошёл до агента')
  assert.ok(!bitbucket.includes(BASIC_DERIVED), 'производная Basic из сообщения чужого API дошёл до агента')
  assert.ok(bitbucket.includes('***'))
  assert.ok(bitbucket.startsWith('HTTP 400:'))

  const slack = await describeFailure(stubResponse({ status: 500, body: { error: 'internal_error' } }), [])
  assert.equal(slack, 'HTTP 500: internal_error')

  const notJson = await describeFailure(stubResponse({ status: 502, body: 'NOT_JSON' }), [])
  assert.equal(notJson, 'HTTP 502', 'не-JSON тело — честный голый статус')
})

test('configError: только имена секретов и название интеграции, без значений', () => {
  const message = configError('jira_issue', ['JIRA_EMAIL', 'JIRA_API_TOKEN'], 'Jira').message
  assert.ok(message.includes('JIRA_EMAIL') && message.includes('JIRA_API_TOKEN'))
  assert.ok(message.includes('Jira'))
  assert.ok(!message.includes(APP_PASSWORD))
  assert.equal(configError('jira_issue', ['X'], 'Jira').ok, false)
})

// ── Jira ──────────────────────────────────────────────────────────────────────

// Прод-форма ADF (Atlassian Document Format): абзац со разметкой, список, код.
const ADF_COMMENT = {
  type: 'doc',
  version: 1,
  content: [
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Воспроизвёл на ' },
        { type: 'text', text: 'стенде', marks: [{ type: 'strong' }] },
        { type: 'text', text: ', лог в приложении.' },
      ],
    },
    {
      type: 'bulletList',
      content: [
        { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'шаг 1' }] }] },
        { type: 'listItem', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'шаг 2' }] }] },
      ],
    },
  ],
}

test('adfToText: реальный ADF разворачивается в плоский текст', () => {
  const text = adfToText(ADF_COMMENT)
  assert.ok(text.includes('Воспроизвёл на '), 'инлайновые узлы склеиваются без переносов')
  assert.ok(text.includes('стенде'))
  assert.ok(text.includes('шаг 1\nшаг 2'), 'список — построчно')
})

test('adfToText: неизвестный узел проходится рекурсивно, пустые не падают', () => {
  assert.equal(adfToText({ type: 'status', attrs: { text: 'NEW' }, content: [{ type: 'text', text: 'NEW' }] }), 'NEW')
  assert.equal(adfToText(null), '')
  assert.equal(adfToText(undefined), '')
  assert.equal(adfToText(42), '')
})

test('renderJiraComment: автор, текст, дата', () => {
  const line = renderJiraComment({
    id: '10001',
    author: { displayName: 'Andrew', accountId: '5f8a1d' },
    body: ADF_COMMENT,
    created: '2026-09-01T10:00:00.000+0200',
    jsdPublic: true,
  })
  assert.ok(line.startsWith('Andrew: '))
  assert.ok(line.includes('шаг 2'))
  assert.ok(line.endsWith('(2026-09-01T10:00:00.000+0200)'))
})

test('renderJiraIssue: прод-форма GET /issue — ключ, статус, комментарии с честным «N из total»', () => {
  const issue = {
    expand: 'renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations',
    id: '10001',
    key: 'PROJ-42',
    fields: {
      summary: 'Сломан экспорт',
      status: { name: 'In Progress', id: '10001', statusCategory: { key: 'indeterminate', name: 'In Progress' } },
      comment: {
        comments: [{
          author: { displayName: 'Andrew' },
          body: { type: 'doc', version: 1, content: [{ type: 'paragraph', content: [{ type: 'text', text: 'взял в работу' }] }] },
          created: '2026-09-01T10:00:00.000+0200',
        }],
        maxResults: 5,
        total: 7,
        startAt: 0,
      },
    },
  }
  const rendered = renderJiraIssue(issue)
  assert.ok(rendered.includes('PROJ-42'))
  assert.ok(rendered.includes('«Сломан экспорт»'))
  assert.ok(rendered.includes('Статус: In Progress'))
  assert.ok(rendered.includes('последние 1 из 7'), 'страница комментариев честно названа частью целого')
  assert.ok(rendered.includes('взял в работу'))

  const withLatest = renderJiraIssue(issue, [
    { author: { displayName: 'Maria' }, body: { type: 'doc', version: 1, content: [{ type: 'paragraph', content: [{ type: 'text', text: 'готово, проверяй' }] }] }, created: '2026-09-02T09:00:00.000+0200' },
  ])
  assert.ok(withLatest.includes('Maria'))
  assert.ok(withLatest.includes('последние 1 из 7'))
})

test('renderJiraIssue: чужой ответ → null, пустые комментарии — явно', () => {
  assert.equal(renderJiraIssue({ fields: {} }), null)
  assert.equal(renderJiraIssue(null), null)
  const rendered = renderJiraIssue({ key: 'PROJ-1', fields: { summary: 'x', status: {}, comment: { comments: [], total: 0 } } })
  assert.ok(rendered.includes('Комментариев нет.'))
  assert.ok(rendered.includes('Статус: не читается'), 'нет статуса — честное «не читается», не выдумка')
})

test('needLatestComments: добор нужен только когда total больше страницы', () => {
  const issue = (total, have) => ({ fields: { comment: { comments: new Array(have).fill({}), total } } })
  assert.deepEqual(needLatestComments(issue(7, 5)), { total: 7 })
  assert.equal(needLatestComments(issue(5, 5)), null)
  assert.equal(needLatestComments(issue(3, 5)), null, 'страница больше total — добора нет')
  assert.equal(needLatestComments({ fields: {} }), null)
})

test('пути Jira: ключ кодируется, последняя страница комментариев стартует с total-limit', () => {
  assert.equal(
    jiraIssuePath('https://team.atlassian.net', 'PROJ-42'),
    'https://team.atlassian.net/rest/api/3/issue/PROJ-42?fields=summary,status,comment',
  )
  assert.equal(
    jiraIssuePath('https://team.atlassian.net', 'PROJ/42'),
    'https://team.atlassian.net/rest/api/3/issue/PROJ%2F42?fields=summary,status,comment',
  )
  assert.equal(
    jiraLatestCommentsPath('https://team.atlassian.net', 'PROJ-42', 7, 5),
    'https://team.atlassian.net/rest/api/3/issue/PROJ-42/comment?startAt=2&maxResults=5',
  )
  assert.equal(
    jiraLatestCommentsPath('https://team.atlassian.net', 'PROJ-42', 3, 5),
    'https://team.atlassian.net/rest/api/3/issue/PROJ-42/comment?startAt=0&maxResults=5',
    'startAt не уходит в минус',
  )
})

// ── Confluence ────────────────────────────────────────────────────────────────

test('renderConfluenceResults: прод-форма content/search — заголовок, id, ссылка из _links', () => {
  const rendered = renderConfluenceResults([
    {
      id: '123456',
      type: 'page',
      status: 'current',
      title: 'Runbook деплоя',
      _expandable: { container: '', metadata: '' },
      _links: {
        webui: '/spaces/OPS/pages/123456/Runbook',
        base: 'https://team.atlassian.net/wiki',
      },
    },
  ])
  assert.ok(rendered.includes('Runbook деплоя'))
  assert.ok(rendered.includes('id 123456'))
  assert.ok(rendered.includes('https://team.atlassian.net/wiki/spaces/OPS/pages/123456/Runbook'))
  assert.ok(renderConfluenceResults([]).includes('не нашлось'))
  assert.ok(renderConfluenceResults(undefined).includes('не нашлось'))
})

test('renderConfluencePage: страница с body.storage, объявленная обрезка длинного тела', () => {
  const page = {
    id: '123456',
    type: 'page',
    status: 'current',
    title: 'Runbook деплоя',
    body: { storage: { value: '<p>Шаги деплоя…</p>', representation: 'storage' } },
    version: { number: 4, when: '2026-09-01T10:00:00.000Z' },
    _links: { webui: '/spaces/OPS/pages/123456/Runbook', base: 'https://team.atlassian.net/wiki' },
  }
  const rendered = renderConfluencePage(page)
  assert.ok(rendered.includes('«Runbook деплоя»'))
  assert.ok(rendered.includes('версия 4'))
  assert.ok(rendered.includes('<p>Шаги деплоя…</p>'))

  const longBody = 'x'.repeat(24_000) + 'ХВОСТ-КОТОРОГО-БЫТЬ-НЕ-ДОЛЖНО'
  const truncated = renderConfluencePage({ ...page, body: { storage: { value: longBody, representation: 'storage' } } })
  assert.ok(truncated.includes(`…[обрезано, всего символов: ${longBody.length}]`), 'обрезка объявлена, не молчаливая')
  assert.ok(!truncated.includes('ХВОСТ-КОТОРОГО-БЫТЬ-НЕ-ДОЛЖНО'))

  assert.equal(renderConfluencePage({ id: '1' }), null, 'без title — не страница')
  const noBody = renderConfluencePage({ id: '1', title: 'Пустая', body: {} })
  assert.ok(noBody.includes('Тело не читается'))
})

// ── Slack ─────────────────────────────────────────────────────────────────────

test('renderSlackResult: прод-форма chat.postMessage — ok:true и ok:false', () => {
  const ok = renderSlackResult({ ok: true, channel: 'C0123456789', ts: '1657042776.538519', message: {} })
  assert.ok(ok.includes('C0123456789'))
  assert.ok(ok.includes('1657042776.538519'))

  const fail = renderSlackResult({ ok: false, error: 'channel_not_found' })
  assert.ok(fail.includes('channel_not_found'))
  assert.ok(fail.includes('chat:write'), 'подсказка про scope живёт рядом с причиной')

  assert.equal(renderSlackResult({ channel: 'C1' }), null, 'нет поля ok — форма не та')
  assert.equal(renderSlackResult('ok'), null)
})

// ── Bitbucket ─────────────────────────────────────────────────────────────────

test('bitbucketRepository: только форма workspace/slug', () => {
  assert.equal(bitbucketRepository(' myteam/service '), 'myteam/service')
  assert.equal(bitbucketRepository('myteam'), null)
  assert.equal(bitbucketRepository('my team/service'), null)
  assert.equal(bitbucketRepository(undefined), null)
})

test('bitbucketPrPath и body: ветки и опциональное описание', () => {
  assert.equal(bitbucketPrPath('myteam/service'), 'https://api.bitbucket.org/2.0/repositories/myteam/service/pullrequests')
  assert.deepEqual(
    bitbucketPrBody({ title: 'Fix', source: 'feature/115', destination: 'main', description: 'Что сделано' }),
    {
      title: 'Fix',
      source: { branch: { name: 'feature/115' } },
      destination: { branch: { name: 'main' } },
      description: 'Что сделано',
    },
  )
  const body = bitbucketPrBody({ title: 'Fix', source: 'f', destination: 'm', description: '' })
  assert.ok(!('description' in body), 'пустое описание не уезжает в API')
})

test('renderBitbucketPr: прод-форма 201 — номер, заголовок, ссылка', () => {
  const rendered = renderBitbucketPr({
    id: 42,
    title: 'Интеграции: первый шаг',
    state: 'OPEN',
    links: { html: { href: 'https://bitbucket.org/myteam/service/pull-requests/42' } },
    source: { branch: { name: 'feature' } },
    destination: { branch: { name: 'main' } },
  })
  assert.ok(rendered.includes('#42'))
  assert.ok(rendered.includes('https://bitbucket.org/myteam/service/pull-requests/42'))
  assert.equal(renderBitbucketPr({ title: 'нет id' }), null)
})
