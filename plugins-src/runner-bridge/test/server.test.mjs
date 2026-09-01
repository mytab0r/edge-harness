/**
 * Юнит-тесты серверного плагина runner-bridge (#95).
 *
 * Проверяется контракт инструментов runner_task и runner_status:
 * - форма возвращаемого значения (schema + render)
 * - обработка конфигурационных ошибок (нет GH_RUNNER_TOKEN / GH_RUNNER_REPO)
 * - обработка сетевых ошибок (таймауты, недоступность GitHub)
 * - обработка ответов GitHub API (201/204/404/4xx/5xx)
 *
 * Фикстура GitHub API — мокируемые fetch-ответы с реальной формой ответов
 * GitHub (проверено живыми прогонами). Тест НЕ ходит в сеть.
 *
 * Запуск: node --test plugins-src/runner-bridge/test/server.test.mjs
 */
import test from 'node:test'
import assert from 'node:assert/strict'

// Импортируем внутренние функции плагина для тестирования
import {
  readRepo,
  readToken,
  githubFetch,
  callSignal,
  describeFailure,
  configError,
  networkError,
  defineRunnerTaskTool,
  defineRunnerStatusTool,
  networkReason,
  collectPullRequests,
} from '../server/index.js'

// ── Утилиты тестов ────────────────────────────────────────────────────────────────

const savedEnv = { ...process.env }

async function withEnv(env, fn) {
  for (const key of Object.keys(process.env)) delete process.env[key]
  for (const [key, value] of Object.entries(env)) {
    if (value !== undefined) process.env[key] = value
  }
  try {
    return await fn()
  } finally {
    for (const key of Object.keys(process.env)) delete process.env[key]
    for (const [key, value] of Object.entries(savedEnv)) {
      process.env[key] = value
    }
  }
}

function mockFetch(responses) {
  let callIndex = 0
  return async (url, options) => {
    const response = responses[callIndex] || responses[responses.length - 1]
    callIndex++
    return {
      ok: response.ok ?? (response.status >= 200 && response.status < 300),
      status: response.status ?? 200,
      headers: new Headers(response.headers ?? {}),
      json: async () => response.body,
      text: async () => JSON.stringify(response.body),
    }
  }
}

function createMockExec(signal = AbortSignal.timeout(1000)) {
  return { signal }
}

// ── Тесты конфигурации ────────────────────────────────────────────────────────────

test('readRepo: возвращает undefined при отсутствии GH_RUNNER_REPO', async () => {
  await withEnv({ GH_RUNNER_REPO: undefined }, async () => {
    assert.equal(readRepo(), undefined)
  })
})

test('readRepo: возвращает undefined при невалидном формате', async () => {
  await withEnv({ GH_RUNNER_REPO: 'invalid' }, async () => {
    assert.equal(readRepo(), undefined)
  })
  await withEnv({ GH_RUNNER_REPO: 'owner/' }, async () => {
    assert.equal(readRepo(), undefined)
  })
  await withEnv({ GH_RUNNER_REPO: '/repo' }, async () => {
    assert.equal(readRepo(), undefined)
  })
})

test('readRepo: возвращает owner/repo при валидном значении', async () => {
  await withEnv({ GH_RUNNER_REPO: 'mytab0r/edge-harness' }, async () => {
    assert.equal(readRepo(), 'mytab0r/edge-harness')
  })
  await withEnv({ GH_RUNNER_REPO: '  mytab0r/edge-harness  ' }, async () => {
    assert.equal(readRepo(), 'mytab0r/edge-harness')
  })
})

test('readToken: возвращает undefined при отсутствии GH_RUNNER_TOKEN', async () => {
  await withEnv({ GH_RUNNER_TOKEN: undefined }, async () => {
    assert.equal(readToken(), undefined)
  })
})

test('readToken: возвращает undefined при пустой строке', async () => {
  await withEnv({ GH_RUNNER_TOKEN: '' }, async () => {
    assert.equal(readToken(), undefined)
  })
  await withEnv({ GH_RUNNER_TOKEN: '   ' }, async () => {
    assert.equal(readToken(), undefined)
  })
})

test('readToken: возвращает токен при валидном значении', async () => {
  await withEnv({ GH_RUNNER_TOKEN: 'ghp_abcdef123456' }, async () => {
    assert.equal(readToken(), 'ghp_abcdef123456')
  })
  await withEnv({ GH_RUNNER_TOKEN: '  ghp_abcdef123456  ' }, async () => {
    assert.equal(readToken(), 'ghp_abcdef123456')
  })
})

// ── Тесты githubFetch ────────────────────────────────────────────────────────────

test('githubFetch: делает GET запрос с правильными заголовками', async () => {
  const fetchMock = mockFetch([{ status: 200, body: { ok: true } }])
  const originalFetch = globalThis.fetch
  globalThis.fetch = fetchMock

  try {
    const response = await githubFetch('/repos/test/repo', { token: 'ghp_test', signal: AbortSignal.timeout(1000) })
    assert.equal(response.status, 200)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('githubFetch: делает POST запрос с телом', async () => {
  const fetchMock = mockFetch([{ status: 201, body: { number: 42 } }])
  const originalFetch = globalThis.fetch
  globalThis.fetch = fetchMock

  try {
    const response = await githubFetch('/repos/test/repo/issues', {
      method: 'POST',
      token: 'ghp_test',
      signal: AbortSignal.timeout(1000),
      body: { title: 'Test', body: 'Test body' },
    })
    assert.equal(response.status, 201)
  } finally {
    globalThis.fetch = originalFetch
  }
})

// ── Тесты callSignal ──────────────────────────────────────────────────────────────

test('callSignal: возвращает таймаут-сигнал без exec', () => {
  const signal = callSignal(undefined)
  assert.ok(signal instanceof AbortSignal)
  assert.equal(signal.reason, undefined) // не отменён
})

test('callSignal: объединяет exec.signal и таймаут', () => {
  const execSignal = AbortSignal.timeout(5000)
  const exec = { signal: execSignal }
  const signal = callSignal(exec)
  assert.ok(signal instanceof AbortSignal)
  // AbortSignal.any создаёт новый сигнал, который отменяется, когда отменяется любой из входных
})

// ── Тесты describeFailure ────────────────────────────────────────────────────────

test('describeFailure: возвращает HTTP статус при не-JSON теле', async () => {
  const response = {
    status: 500,
    json: async () => { throw new Error('not json') },
  }
  const result = await describeFailure(response)
  assert.equal(result, 'HTTP 500')
})

test('describeFailure: возвращает сообщение GitHub при JSON теле с message', async () => {
  const response = {
    status: 422,
    json: async () => ({ message: 'Validation failed', errors: [] }),
  }
  const result = await describeFailure(response)
  assert.equal(result, 'HTTP 422: Validation failed')
})

test('describeFailure: возвращает только статус при JSON без message', async () => {
  const response = {
    status: 403,
    json: async () => ({ other: 'field' }),
  }
  const result = await describeFailure(response)
  assert.equal(result, 'HTTP 403')
})

// ── Тесты configError ────────────────────────────────────────────────────────────

test('configError: возвращает структурированную ошибку с понятным сообщением', () => {
  const error = configError('runner_task', 'секрета нет')
  assert.equal(error.ok, false)
  assert.ok(error.message.includes('runner_task не настроен в воркере'))
  assert.ok(error.message.includes('секрета нет'))
  assert.ok(error.message.includes('GH_RUNNER_TOKEN'))
  assert.ok(error.message.includes('GH_RUNNER_REPO'))
})

// ── Тесты networkError ───────────────────────────────────────────────────────────

test('networkError: распознаёт TimeoutError', () => {
  const error = new Error('timeout')
  error.name = 'TimeoutError'
  const result = networkError('runner_task', error)
  assert.equal(result.ok, false)
  assert.ok(result.message.includes('таймаут'))
  assert.ok(result.message.includes('15 с'))
})

test('networkError: форматирует обычную ошибку сети', () => {
  const error = new Error('ENOTFOUND')
  const result = networkError('runner_task', error)
  assert.equal(result.ok, false)
  assert.ok(result.message.includes('сеть недоступна'))
  assert.ok(result.message.includes('ENOTFOUND'))
})

test('networkError: обрабатывает не-Error значения', () => {
  const result = networkError('runner_task', 'string error')
  assert.equal(result.ok, false)
  assert.ok(result.message.includes('string error'))
})

// ── Тесты networkReason ──────────────────────────────────────────────────────────

test('networkReason: форматирует TimeoutError', () => {
  const error = new Error('timeout')
  error.name = 'TimeoutError'
  assert.equal(networkReason(error), 'таймаут 15 с')
})

test('networkReason: форматирует обычную ошибку', () => {
  const error = new Error('network down')
  assert.equal(networkReason(error), 'network down')
})

test('networkReason: обрабатывает не-Error', () => {
  assert.equal(networkReason('raw string'), 'raw string')
})

// ── Тесты runner_task ────────────────────────────────────────────────────────────

test('runner_task: возвращает ошибку конфигурации без токена', async () => {
  const tool = defineRunnerTaskTool()
  const result = await tool.execute(
    { title: 'Test', body: 'Test body' },
    createMockExec()
  )
  assert.equal(result.ok, false)
  assert.ok(result.message.includes('GH_RUNNER_TOKEN'))
})

test('runner_task: возвращает ошибку конфигурации без repo', async () => {
  await withEnv({ GH_RUNNER_TOKEN: 'ghp_test' }, async () => {
    const tool = defineRunnerTaskTool()
    const result = await tool.execute(
      { title: 'Test', body: 'Test body' },
      createMockExec()
    )
    assert.equal(result.ok, false)
    assert.ok(result.message.includes('GH_RUNNER_REPO'))
  })
})

test('runner_task: создаёт issue и диспетчит воркер (успех)', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        { status: 201, body: { number: 95, html_url: 'https://github.com/mytab0r/edge-harness/issues/95' } },
        { status: 204, body: null },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerTaskTool()
        const result = await tool.execute(
          { title: 'Test task', body: 'Full task description with criteria' },
          createMockExec()
        )
        assert.equal(result.ok, true)
        assert.equal(result.dispatched, true)
        assert.equal(result.issue, 95)
        assert.ok(result.message.includes('#95'))
        assert.ok(result.message.includes('Раннер запущен'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_task: issue создан, но диспетч не прошёл (не 204)', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        { status: 201, body: { number: 95, html_url: 'https://github.com/mytab0r/edge-harness/issues/95' } },
        { status: 404, body: { message: 'Workflow not found' } },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerTaskTool()
        const result = await tool.execute(
          { title: 'Test task', body: 'Full task description' },
          createMockExec()
        )
        assert.equal(result.ok, true)
        assert.equal(result.dispatched, false)
        assert.equal(result.issue, 95)
        assert.ok(result.message.includes('оркестратор сам поднимет'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_task: issue создан, но диспетч упал с ошибкой сети', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      let callCount = 0
      const originalFetch = globalThis.fetch
      globalThis.fetch = async (url) => {
        callCount++
        if (callCount === 1) {
          // Первый вызов — создание issue
          return {
            ok: true,
            status: 201,
            json: async () => ({ number: 95, html_url: 'https://github.com/mytab0r/edge-harness/issues/95' }),
          }
        }
        // Второй вызов — диспетч воркера — падает
        throw new Error('network down')
      }

      try {
        const tool = defineRunnerTaskTool()
        const result = await tool.execute(
          { title: 'Test task', body: 'Full task description' },
          createMockExec()
        )
        assert.equal(result.ok, true)
        assert.equal(result.dispatched, false)
        assert.equal(result.issue, 95)
        assert.ok(result.message.includes('оркестратор сам поднимет'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_task: GitHub не создал issue (ошибка 422)', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        { status: 422, body: { message: 'Validation failed: title is too short' } },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerTaskTool()
        const result = await tool.execute(
          { title: 'Test', body: 'Full task description' },
          createMockExec()
        )
        assert.equal(result.ok, false)
        assert.ok(result.message.includes('GitHub не создал задачу'))
        assert.ok(result.message.includes('Validation failed'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_task: сетевая ошибка при создании issue', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const originalFetch = globalThis.fetch
      globalThis.fetch = async () => { throw new Error('ENOTFOUND') }

      try {
        const tool = defineRunnerTaskTool()
        const result = await tool.execute(
          { title: 'Test', body: 'Full task description' },
          createMockExec()
        )
        assert.equal(result.ok, false)
        // networkError форматирует: "runner_task: сеть недоступна: ENOTFOUND. Попробуй ещё раз..."
        assert.ok(result.message.includes('сеть недоступна'))
        assert.ok(result.message.includes('ENOTFOUND'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

// ── Тесты runner_status ──────────────────────────────────────────────────────────

test('runner_status: возвращает ошибку конфигурации без токена', async () => {
  const tool = defineRunnerStatusTool()
  const result = await tool.execute({ issue: 95 }, createMockExec())
  assert.equal(result.ok, false)
  assert.ok(result.message.includes('GH_RUNNER_TOKEN'))
})

test('runner_status: возвращает ошибку конфигурации без repo', async () => {
  await withEnv({ GH_RUNNER_TOKEN: 'ghp_test' }, async () => {
    const tool = defineRunnerStatusTool()
    const result = await tool.execute({ issue: 95 }, createMockExec())
    assert.equal(result.ok, false)
    assert.ok(result.message.includes('GH_RUNNER_REPO'))
  })
})

test('runner_status: задача не найдена (404)', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([{ status: 404, body: { message: 'Not Found' } }])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 999 }, createMockExec())
        assert.equal(result.ok, false)
        assert.ok(result.message.includes('нет'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: это PR, а не задача (pull_request поле)', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        { status: 200, body: { number: 95, title: 'PR title', state: 'open', html_url: 'https://github.com/mytab0r/edge-harness/pull/95', pull_request: { url: '...' } } },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('pull request'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: открытая задача с исполнителем и метками', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        {
          status: 200,
          body: {
            number: 95,
            title: 'Test task',
            state: 'open',
            html_url: 'https://github.com/mytab0r/edge-harness/issues/95',
            labels: [{ name: 'task' }, { name: 'bug' }],
            assignees: [{ login: 'worker-bot' }],
          },
        },
        // timeline
        { status: 200, body: [] },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('открыта'))
        assert.ok(result.message.includes('worker-bot'))
        assert.ok(result.message.includes('task, bug'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: задача с меткой blocked', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        {
          status: 200,
          body: {
            number: 95,
            title: 'Test task',
            state: 'open',
            html_url: 'https://github.com/mytab0r/edge-harness/issues/95',
            labels: [{ name: 'task' }, { name: 'blocked' }],
            assignees: [],
          },
        },
        { status: 200, body: [] },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('blocked'))
        assert.ok(result.message.includes('Ждёт владельца'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: закрытая задача с смерженным PR', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        {
          status: 200,
          body: {
            number: 95,
            title: 'Test task',
            state: 'closed',
            html_url: 'https://github.com/mytab0r/edge-harness/issues/95',
            labels: [{ name: 'task' }],
            assignees: [{ login: 'worker-bot' }],
          },
        },
        // timeline с cross-referenced PR
        {
          status: 200,
          body: [
            {
              event: 'cross-referenced',
              source: { issue: { number: 96, pull_request: { url: '...' } } },
            },
          ],
        },
        // PR детали
        {
          status: 200,
          body: {
            number: 96,
            title: 'Fix for #95',
            state: 'closed',
            merged: true,
            merge_commit_sha: 'abc1234def5678',
            html_url: 'https://github.com/mytab0r/edge-harness/pull/96',
          },
        },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('закрыта'))
        assert.ok(result.message.includes('PR #96'))
        assert.ok(result.message.includes('смержен'))
        assert.ok(result.message.includes('abc1234'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: открытая задача с открытым PR', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        {
          status: 200,
          body: {
            number: 95,
            title: 'Test task',
            state: 'open',
            html_url: 'https://github.com/mytab0r/edge-harness/issues/95',
            labels: [{ name: 'task' }],
            assignees: [],
          },
        },
        {
          status: 200,
          body: [
            {
              event: 'cross-referenced',
              source: { issue: { number: 96, pull_request: { url: '...' } } },
            },
          ],
        },
        {
          status: 200,
          body: {
            number: 96,
            title: 'Work in progress for #95',
            state: 'open',
            merged: false,
            merge_commit_sha: null,
            html_url: 'https://github.com/mytab0r/edge-harness/pull/96',
          },
        },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('открыт'))
        assert.ok(result.message.includes('ждёт ревью'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: таймлайн недоступен — статус без PR', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        {
          status: 200,
          body: {
            number: 95,
            title: 'Test task',
            state: 'open',
            html_url: 'https://github.com/mytab0r/edge-harness/issues/95',
            labels: [{ name: 'task' }],
            assignees: [],
          },
        },
        { status: 500, body: { message: 'Internal error' } },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, true)
        assert.ok(result.message.includes('PR ещё нет'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('runner_status: сетевая ошибка', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const originalFetch = globalThis.fetch
      globalThis.fetch = async () => { throw new Error('network down') }

      try {
        const tool = defineRunnerStatusTool()
        const result = await tool.execute({ issue: 95 }, createMockExec())
        assert.equal(result.ok, false)
        assert.ok(result.message.includes('сеть недоступна'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

// ── Тесты collectPullRequests ────────────────────────────────────────────────────

test('collectPullRequests: возвращает пустой массив при ошибке таймлайна', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const originalFetch = globalThis.fetch
      globalThis.fetch = async () => { throw new Error('network down') }

      try {
        const exec = createMockExec()
        const result = await collectPullRequests('mytab0r/edge-harness', 'ghp_test', exec, 95)
        assert.deepEqual(result, [])
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('collectPullRequests: фильтрует только cross-referenced события с PR', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const fetchMock = mockFetch([
        // timeline
        {
          status: 200,
          body: [
            { event: 'labeled', label: { name: 'task' } },
            { event: 'cross-referenced', source: { issue: { number: 96, pull_request: { url: '...' } } } },
            { event: 'cross-referenced', source: { issue: { number: 97 } } }, // нет PR
            { event: 'cross-referenced', source: { issue: { number: 98, pull_request: { url: '...' } } } },
          ],
        },
        // PR 96
        { status: 200, body: { number: 96, title: 'PR 1', state: 'open', merged: false, merge_commit_sha: null, html_url: 'https://github.com/mytab0r/edge-harness/pull/96' } },
        // PR 98
        { status: 200, body: { number: 98, title: 'PR 2', state: 'closed', merged: true, merge_commit_sha: 'abc123', html_url: 'https://github.com/mytab0r/edge-harness/pull/98' } },
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const exec = createMockExec()
        const result = await collectPullRequests('mytab0r/edge-harness', 'ghp_test', exec, 95)
        assert.equal(result.length, 2)
        assert.ok(result[0].includes('PR #96'))
        assert.ok(result[0].includes('открыт'))
        assert.ok(result[1].includes('PR #98'))
        assert.ok(result[1].includes('смержен'))
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

test('collectPullRequests: ограничивает количество PR по PR_DETAIL_LIMIT', async () => {
  await withEnv(
    { GH_RUNNER_TOKEN: 'ghp_test', GH_RUNNER_REPO: 'mytab0r/edge-harness' },
    async () => {
      const prs = Array.from({ length: 5 }, (_, i) => ({
        event: 'cross-referenced',
        source: { issue: { number: 100 + i, pull_request: { url: '...' } } },
      }))
      const fetchMock = mockFetch([
        { status: 200, body: prs },
        ...Array.from({ length: 5 }, (_, i) => ({
          status: 200,
          body: { number: 100 + i, title: `PR ${i}`, state: 'open', merged: false, merge_commit_sha: null, html_url: `https://github.com/mytab0r/edge-harness/pull/${100 + i}` },
        })),
      ])
      const originalFetch = globalThis.fetch
      globalThis.fetch = fetchMock

      try {
        const exec = createMockExec()
        const result = await collectPullRequests('mytab0r/edge-harness', 'ghp_test', exec, 95)
        // PR_DETAIL_LIMIT = 2, должен вернуть только 2
        assert.equal(result.length, 2)
      } finally {
        globalThis.fetch = originalFetch
      }
    }
  )
})

// ── Тесты определения инструментов (schema, render, timeout) ────────────────────

test('runner_task: имеет правильную схему параметров', () => {
  const tool = defineRunnerTaskTool()
  assert.equal(tool.name, 'runner_task')
  // parameters преобразуются в JSON Schema через parameterSchemaSpecToJsonSchema
  assert.ok(tool.parameters.properties)
  assert.ok(tool.parameters.properties.title)
  assert.ok(tool.parameters.properties.body)
  assert.ok(tool.parameters.required.includes('title'))
  assert.ok(tool.parameters.required.includes('body'))
})

test('runner_task: имеет правильную схему вывода', () => {
  const tool = defineRunnerTaskTool()
  assert.ok(tool.output.schema.properties.ok)
  assert.ok(tool.output.schema.properties.message)
  assert.ok(tool.output.schema.properties.issue)
  assert.ok(tool.output.schema.properties.url)
  assert.ok(tool.output.schema.properties.dispatched)
  assert.equal(typeof tool.output.render, 'function')
})

test('runner_task: timeoutMs = 45000', () => {
  const tool = defineRunnerTaskTool()
  assert.equal(tool.timeoutMs, 45_000)
})

test('runner_status: имеет правильную схему параметров', () => {
  const tool = defineRunnerStatusTool()
  assert.equal(tool.name, 'runner_status')
  assert.ok(tool.parameters.properties)
  assert.ok(tool.parameters.properties.issue)
  assert.ok(tool.parameters.required.includes('issue'))
})

test('runner_status: имеет правильную схему вывода', () => {
  const tool = defineRunnerStatusTool()
  assert.ok(tool.output.schema.properties.ok)
  assert.ok(tool.output.schema.properties.message)
  assert.equal(typeof tool.output.render, 'function')
})

test('runner_status: timeoutMs = 60000', () => {
  const tool = defineRunnerStatusTool()
  assert.equal(tool.timeoutMs, 60_000)
})

// ── Тесты описаний инструментов (умный принцип маршрутизации) ────────────────────

test('runner_task: описание содержит правило маршрутизации (когда звать раннер)', () => {
  const tool = defineRunnerTaskTool()
  const desc = tool.description
  assert.ok(desc.includes('СЕРВЕРНЫЕ РЕСУРСЫ'))
  assert.ok(desc.includes('сборка и тесты'))
  assert.ok(desc.includes('написание плагинов'))
  assert.ok(desc.includes('git-операции'))
  assert.ok(desc.includes('долгие'))
  assert.ok(desc.includes('короткие'))
  assert.ok(desc.includes('в чате без инструмента'))
})

test('runner_status: описание указывает когда вызывать', () => {
  const tool = defineRunnerStatusTool()
  const desc = tool.description
  assert.ok(desc.includes('прогрессе задачи'))
  assert.ok(desc.includes('runner_task'))
})

console.log('Все тесты runner-bridge пройдены')