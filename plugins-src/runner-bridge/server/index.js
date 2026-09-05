/**
 * runner-bridge: server half of the first real edge plugin (#95).
 *
 * A cordis plugin that gives the chat agent of dsh-edge two tools:
 *   - runner_task  — create a task in the edge-harness pool (issue with the
 *     `task` label) and dispatch the worker (GitHub Actions `worker.yml`).
 *     The runner-side DSH does the work with the full toolset and opens a PR;
 *     the chat agent reports the issue number and link back to the user.
 *   - runner_status — short status of a pooled task: issue state, assignee,
 *     `blocked` label and the PRs referencing it (open / merged / closed).
 *
 * The GitHub token is read from the worker env (`process.env.GH_RUNNER_TOKEN`,
 * synced by deploy-dsh-edge.yml from the repository secret GH_PIPELINE_PAT —
 * the same broad pipeline PAT the hands and the orchestrator already use; the
 * narrow GH_DISPATCH_TOKEN of the edge-harness morde lacks issues rights). The token value
 * never appears in tool output: errors carry only HTTP status and GitHub's
 * own message. Every fetch is time-boxed (AbortSignal.timeout), so a hung
 * GitHub call cannot hold the agent turn.
 */

import { defineTool } from '@deepseek-ai/dsh-tools'

const PLUGIN_VERSION = '0.1.1'
const GITHUB_API = 'https://api.github.com'
const GITHUB_TIMEOUT_MS = 15_000
const GITHUB_API_VERSION = '2022-11-28'
const TASK_LABEL = 'task'
const WORKER_WORKFLOW = 'worker.yml'
const WORKER_REF = 'main'
// Подробный статус запрашивается максимум для двух PR: обычно он один, две
// ссылки — уже нетипичный случай. Верхняя граница держит worst-case время
// инструмента под его timeoutMs.
const PR_DETAIL_LIMIT = 2

const REPO_PATTERN = /^[^/\s]+\/[^/\s]+$/

export { readRepo, readToken, githubFetch, callSignal, describeFailure, configError, networkError, networkReason, defineRunnerTaskTool, defineRunnerStatusTool, collectPullRequests };

export default {
  name: 'edge-plugins:runner-bridge',
  // cordis 4: сервис можно читать через ctx.<service> только если плагин
  // объявил его в inject (иначе apply падает «cannot get property … without
  // inject»). Контракт тех же апстримных плагинов (dsh-tool-web и др.).
  inject: ['tools'],
  apply(ctx) {
    ctx.effect(() => ctx.tools.register(defineRunnerTaskTool()), 'edge-plugins:runner-bridge runner_task tool')
    ctx.effect(() => ctx.tools.register(defineRunnerStatusTool()), 'edge-plugins:runner-bridge runner_status tool')
    console.info(`edge-plugin:runner-bridge installed v${PLUGIN_VERSION} (runner_task, runner_status tools registered)`)
  },
}

/** Валидированный `owner/repo` из env воркера; undefined — конфигурации нет. */
function readRepo() {
  const value = process.env.GH_RUNNER_REPO
  if (typeof value !== 'string' || !REPO_PATTERN.test(value.trim())) return undefined
  return value.trim()
}

/** Токен из env воркера; undefined — секрета нет. Значение наружу не выходит. */
function readToken() {
  const value = process.env.GH_RUNNER_TOKEN
  if (typeof value !== 'string' || value.trim() === '') return undefined
  return value.trim()
}

/**
 * Один вызов GitHub API с таймаутом и отменой от агентного цикла.
 * Возвращает response; тело не читается здесь — читает вызывающий.
 */
function githubFetch(path, { method = 'GET', body, token, signal }) {
  return fetch(`${GITHUB_API}${path}`, {
    method,
    headers: {
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': GITHUB_API_VERSION,
      Authorization: `Bearer ${token}`,
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal,
  })
}

/** Сигнал вызова: отмена хода агента + жёсткий таймаут на каждый fetch. */
function callSignal(exec) {
  const timeout = AbortSignal.timeout(GITHUB_TIMEOUT_MS)
  return exec?.signal ? AbortSignal.any([exec.signal, timeout]) : timeout
}

/** Человекочитательная причина отказа GitHub: статус + сообщение, без заголовков. */
async function describeFailure(response) {
  let detail = ''
  try {
    const data = await response.json()
    if (data !== null && typeof data === 'object' && typeof data.message === 'string') {
      detail = data.message
    }
  } catch {
    // Тело не JSON — остаётся один статус; это не ошибка чтения.
  }
  return detail === '' ? `HTTP ${response.status}` : `HTTP ${response.status}: ${detail}`
}

/** Громкий отказ конфигурации — агент озвучит это пользователю дословно. */
function configError(tool, what) {
  return {
    ok: false,
    message: `${tool} не настроен в воркере: ${what}. `
      + 'Починить может только владелец: секрет GH_RUNNER_TOKEN (в deploy-dsh-edge.yml '
      + 'он синхронизируется из секрета репозитория GH_PIPELINE_PAT) и переменная '
      + 'воркера GH_RUNNER_REPO (owner/repo). Задачу создать не удалось.',
  }
}

function networkError(tool, error) {
  const timedOut = error instanceof Error && error.name === 'TimeoutError'
  const reason = timedOut
    ? `GitHub не ответил за ${GITHUB_TIMEOUT_MS / 1000} с (таймаут)`
    : `сеть недоступна: ${error instanceof Error ? error.message : String(error)}`
  return { ok: false, message: `${tool}: ${reason}. Попробуй ещё раз или проверь статус GitHub.` }
}

// ── runner_task ───────────────────────────────────────────────────────────────

function defineRunnerTaskTool() {
  return defineTool({
    name: 'runner_task',
    description: 'Делегирует работу серверному раннеру: создаёт задачу в пуле edge-harness '
      + '(issue с меткой task) и поднимает воркера (GitHub Actions) — раннерский агент выполнит '
      + 'работу полным набором тулов (git, сборка, тесты) и откроет PR. ЗОВАТЬ ЕГО НАДО, когда '
      + 'запросу нужны СЕРВЕРНЫЕ РЕСУРСЫ: сборка и тесты, написание плагинов, git-операции над '
      + 'репозиторием, долгие (>5 минут) задачи. Всё остальное — вопросы, объяснения, короткие '
      + 'ответы — делай в чате без инструмента. В body пиши полное ТЗ с критерием готовности: '
      + 'раннерский агент видит только его, тебя — нет. Вызов вернёт номер задачи и ссылку — '
      + 'передай их пользователю; результат придёт в задачу и в чат.',
    parameters: {
      title: {
        type: 'string',
        required: true,
        description: 'Короткий заголовок задачи (как заголовок коммита): что сделать.',
      },
      body: {
        type: 'string',
        required: true,
        description: 'Полное тело задачи: цель, шаги, критерий готовности, ограничения. '
          + 'Раннерский агент работает только по этому тексту.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          message: { type: 'string', required: true },
          issue: { type: 'integer' },
          url: { type: 'string' },
          dispatched: { type: 'boolean' },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.message }],
    },
    timeoutMs: 45_000,
    async execute(args, exec) {
      const token = readToken()
      if (token === undefined) return configError('runner_task', 'секрета GH_RUNNER_TOKEN нет в env воркера')
      const repo = readRepo()
      if (repo === undefined) return configError('runner_task', 'в env воркера нет валидного GH_RUNNER_REPO (owner/repo)')
      const signal = callSignal(exec)

      let issue
      try {
        const response = await githubFetch(`/repos/${repo}/issues`, {
          method: 'POST',
          token,
          signal,
          body: { title: args.title, body: args.body, labels: [TASK_LABEL] },
        })
        if (response.status !== 201) {
          const reason = await describeFailure(response)
          return {
            ok: false,
            message: `runner_task: GitHub не создал задачу (${reason}). `
              + 'Пользователю это надо озвучить; повторная попытка уместна, если причина временная.',
          }
        }
        issue = await response.json()
      } catch (error) {
        return networkError('runner_task', error)
      }

      const number = issue.number
      const url = issue.html_url
      try {
        const response = await githubFetch(`/repos/${repo}/actions/workflows/${WORKER_WORKFLOW}/dispatches`, {
          method: 'POST',
          token,
          signal: callSignal(exec),
          body: { ref: WORKER_REF, inputs: { task: String(number) } },
        })
        if (response.status === 204) {
          return {
            ok: true,
            dispatched: true,
            issue: number,
            url,
            message: `Задача #${number} создана: ${url}. Раннер запущен — результат (PR или комментарий) `
              + 'придёт в задачу. Скажи пользователю номер, ссылку и что работа идёт на раннере.',
          }
        }
        const reason = await describeFailure(response)
        return {
          ok: true,
          dispatched: false,
          issue: number,
          url,
          message: `Задача #${number} создана: ${url}. Диспетч воркера не прошёл (${reason}) — `
            + 'оркестратор сам поднимет воркера по пульсу (до 15 минут). Скажи пользователю номер и ссылку.',
        }
      } catch (error) {
        return {
          ok: true,
          dispatched: false,
          issue: number,
          url,
          message: `Задача #${number} создана: ${url}. Диспетч воркера упал (${networkReason(error)}) — `
            + 'оркестратор сам поднимет воркера по пульсу (до 15 минут). Скажи пользователю номер и ссылку.',
        }
      }
    },
  })
}

function networkReason(error) {
  return error instanceof Error && error.name === 'TimeoutError'
    ? `таймаут ${GITHUB_TIMEOUT_MS / 1000} с`
    : error instanceof Error ? error.message : String(error)
}

// ── runner_status ─────────────────────────────────────────────────────────────

function defineRunnerStatusTool() {
  return defineTool({
    name: 'runner_status',
    description: 'Короткий статус задачи из пула edge-harness, отданной раннеру: состояние issue '
      + '(open/closed), исполнитель, метка blocked (ждёт владельца) и связанные PR (открыт / '
      + 'смержен / закрыт). Зови, когда пользователь спрашивает о прогрессе задачи, созданной '
      + 'через runner_task.',
    parameters: {
      issue: {
        type: 'integer',
        required: true,
        description: 'Номер задачи (issue number), например 95.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean', required: true },
          message: { type: 'string', required: true },
        },
      },
      render: (_args, value) => [{ type: 'text', text: value.message }],
    },
    timeoutMs: 60_000,
    async execute(args, exec) {
      const token = readToken()
      if (token === undefined) return configError('runner_status', 'секрета GH_RUNNER_TOKEN нет в env воркера')
      const repo = readRepo()
      if (repo === undefined) return configError('runner_status', 'в env воркера нет валидного GH_RUNNER_REPO (owner/repo)')
      const signal = callSignal(exec)

      let data
      try {
        const response = await githubFetch(`/repos/${repo}/issues/${args.issue}`, { token, signal })
        if (response.status === 404) {
          return { ok: false, message: `runner_status: задачи #${args.issue} в ${repo} нет.` }
        }
        if (response.status !== 200) {
          return { ok: false, message: `runner_status: GitHub не отдал задачу #${args.issue} (${await describeFailure(response)}).` }
        }
        data = await response.json()
      } catch (error) {
        return networkError('runner_status', error)
      }

      if (data.pull_request !== undefined) {
        return { ok: true, message: `#${args.issue} — это pull request, а не задача пула: ${data.html_url}` }
      }

      const labels = Array.isArray(data.labels) ? data.labels.map(label => label.name) : []
      const assignees = Array.isArray(data.assignees) ? data.assignees.map(person => person.login) : []
      const lines = []
      lines.push(`Задача #${data.number} «${data.title}»: ${data.state === 'closed' ? 'закрыта' : 'открыта'}.`)
      if (assignees.length > 0) lines.push(`Исполнитель: ${assignees.join(', ')}.`)
      if (labels.length > 0) lines.push(`Метки: ${labels.join(', ')}.`)
      if (labels.includes('blocked')) lines.push('Ждёт владельца (метка blocked) — подробности в комментариях задачи.')

      const prLines = await collectPullRequests(repo, token, exec, data.number)
      if (prLines.length > 0) {
        lines.push(...prLines)
      } else if (data.state !== 'closed') {
        lines.push('PR ещё нет: задача либо ждёт воркера, либо в работе (подробности — в комментариях задачи).')
      }
      lines.push(`Задача: ${data.html_url}`)
      return { ok: true, message: lines.join(' ') }
    },
  })
}

/** Ссылки на задачу в открытых PR (timeline cross-references — любое
 * упоминание `#N`, украшение статуса, не декларация; узкое правило
 * объявлений — `scripts/lib/task_ref.py::declared_tasks`) и их состояния. */
async function collectPullRequests(repo, token, exec, issueNumber) {
  let refs
  try {
    const response = await githubFetch(
      `/repos/${repo}/issues/${issueNumber}/timeline?per_page=100`,
      { token, signal: callSignal(exec) },
    )
    if (response.status !== 200) return []
    const timeline = await response.json()
    if (!Array.isArray(timeline)) return []
    refs = timeline
      .filter(event => event?.event === 'cross-referenced' && event?.source?.issue?.pull_request !== undefined)
      .map(event => event.source.issue.number)
  } catch {
    // Таймлайн — украшение статуса, не его суть: не доехал — статус без PR-строк.
    return []
  }
  const lines = []
  for (const number of refs.slice(0, PR_DETAIL_LIMIT)) {
    try {
      const response = await githubFetch(`/repos/${repo}/pulls/${number}`, { token, signal: callSignal(exec) })
      if (response.status !== 200) continue
      const pull = await response.json()
      const state = pull.merged === true
        ? `смержен в main (${pull.merge_commit_sha?.slice(0, 7) ?? 'sha не читается'})`
        : pull.state === 'open' ? 'открыт, ждёт ревью/слияния' : 'закрыт без слияния'
      lines.push(`PR #${number} «${pull.title}» — ${state}: ${pull.html_url}`)
    } catch {
      // Один недоехавший PR не отменяет остального статуса.
    }
  }
  return lines
}
