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
 *
 * Чистая логика (fetch/error-handling/execute обоих инструментов) живёт в
 * ./core.js и покрыта поведенческими тестами без зависимости на
 * `@deepseek-ai/dsh-tools` (находка ревью PR #411 — по образцу integrations,
 * #115); здесь — только проводка defineTool. Ре-экспортов функций из core.js
 * здесь намеренно нет (чеклист ревью PR #411): тест импортирует core.js
 * напрямую, а строка ре-экспорта тянула бы peer-зависимость в каждого, кто
 * импортирует readRepo и компанию из index.js (например тест) — ровно то,
 * от чего расщепление избавило.
 */

import { defineTool } from '@deepseek-ai/dsh-tools'
import { runnerStatusToolConfig, runnerTaskToolConfig } from './core.js'

const PLUGIN_VERSION = '0.1.2'

export const defineRunnerTaskTool = () => defineTool(runnerTaskToolConfig())
export const defineRunnerStatusTool = () => defineTool(runnerStatusToolConfig())

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
