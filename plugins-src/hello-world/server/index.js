/**
 * hello-world: server half of the PoC edge plugin.
 *
 * A cordis plugin whose only job is to prove the channel: the dsh-edge plugin
 * system (generated composition + install loop) must mount this in the worker's
 * Durable Object so that the `plugin_hello` tool becomes available in chat.
 * The tool registration is the visible effect the PoC evidence is built on.
 */

import { defineTool } from '@deepseek-ai/dsh-tools'

const GREETING = 'Hello from the dsh-edge plugin system (hello-world v0.1.1, @edge-harness/dsh-plugin-hello).'

export default {
  name: 'edge-plugins:hello',
  // cordis 4: сервис можно читать через ctx.<service> только если плагин
  // объявил его в inject (иначе apply падает «cannot get property … without
  // inject»). Контракт тех же апстримных плагинов (dsh-tool-web и др.).
  inject: ['tools'],
  apply(ctx) {
    ctx.effect(() => ctx.tools.register(defineHelloTool()), 'edge-plugins:hello tool')
    console.info('edge-plugin:hello installed (plugin_hello tool registered)')
  },
}

function defineHelloTool() {
  return defineTool({
    name: 'plugin_hello',
    description: 'PoC edge-plugin probe: returns a greeting proving the hello-world plugin is alive in this worker.',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: { text: { type: 'string', required: true } },
      },
      render: () => [{ type: 'text', text: GREETING }],
    },
    async execute() {
      return { text: GREETING }
    },
  })
}
