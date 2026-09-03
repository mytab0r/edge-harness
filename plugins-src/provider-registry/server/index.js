/**
 * provider-registry: серверный плагин реестра LLM-провайдеров морды (#114,
 * change openspec/changes/dsh-edge-provider-registry).
 *
 * Занимает settings-namespace `llm-pi-ai` своей реализацией (без пакета
 * dsh-llm-pi-ai и его Node-only транспортных зависимостей):
 *
 *  - схема namespace (`providers.<route-id>`) оживляет кнопку «Add custom
 *    provider» штатного Settings → Models: клиент читает из неё выбор
 *    протоколов (protocolChoices);
 *  - directory-записи registerConfigurableProviders оживляют кнопку «Add»:
 *    готовые OpenAI-compat маршруты видны как незанятые строки каталога;
 *  - каждая настроенная запись становится живым маршрутом в пикере моделей:
 *    один переиспользованный DeepSeekAdapter (OpenAI-compat транспорт
 *    апстрима) на маршрут, ключи — штатным credentials-механизмом морды
 *    (DO storage, env-фолбэк), значения ключей через namespace не проходят.
 *
 * Контракт cordis-плагина — как у runner-bridge (inject + apply); контракт
 * чтения настроек — как у апстримного dsh-llm-deepseek: installSettingsSection
 * ставит источник-Thunk, каждое изменение раздела перестраивает набор
 * маршрутов (handle.replace / dispose), отказ одного маршрута не мешает
 * остальным и не роняет DO (прецедент изоляции патча 0002).
 */

import z from '@deepseek-ai/schemastery'
import { installSettingsSection, settingsNamespace } from '@deepseek-ai/dsh-settings'
import { DeepSeekAdapter, resolveAdapterOptions } from '@deepseek-ai/dsh-llm-deepseek'
import { LlmError, assertUsableApiKey } from '@deepseek-ai/dsh-llm'
import { credentialRef } from '@deepseek-ai/dsh-credentials'
import { getOrCreateAnonymousUserId } from '@deepseek-ai/dsh-anonymous-user-id'

const PLUGIN_VERSION = '0.1.0'
const NS = settingsNamespace('llm-pi-ai')
/** Маршрут штатного провайдера морды (session-store.ts): в реестре запрещён. */
const EDGE_PROVIDER = 'deepseek-official'
/**
 * Потолок ответа маршрута по умолчанию. Читается тот же env, что кормит
 * штатного провайдера морды (DEEPSEEK_MAX_OUTPUT_TOKENS в wrangler.jsonc
 * деплоя = 131072): у адаптера дефолт 256000, который текущий провайдер
 * владельца отклоняет. Отсутствие env — этот же потолок деплоя.
 */
const DEFAULT_MAX_TOKENS_FALLBACK = 131072
const DEFAULT_CONTEXT_WINDOW = 262144

/**
 * Directory-каталог реестра: готовые OpenAI-compat маршруты, которые кнопка
 * «Add» предлагает добавить, не набирая адрес руками. Канонические публичные
 * эндпоинты; каждый остаётся редактируемым в карточке провайдера. Состав —
 * провайдеры, названные в документах этого репозитория (runbook
 * switch-llm-provider, research/30), расширяется PR'ом.
 */
const DIRECTORY = [
  { route: 'zhipu', displayName: 'Z.ai (GLM)', baseURL: 'https://open.bigmodel.cn/api/paas/v4' },
  { route: 'nvidia-nim', displayName: 'NVIDIA NIM', baseURL: 'https://integrate.api.nvidia.com/v1' },
  { route: 'openrouter', displayName: 'OpenRouter', baseURL: 'https://openrouter.ai/api/v1' },
  { route: 'deepseek', displayName: 'DeepSeek API', baseURL: 'https://api.deepseek.com' },
]

/** Единственный протокол, который реально говорит переиспользуемый транспорт. */
const PROTOCOLS = ['openai-completions']

/** Маршрут, пригодный и ключом настроек, и основой имени креда (клиентский ROUTE_PATTERN). */
const ROUTE_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/

/** Тот же вывод имени креда, что у клиента (deriveKeyRef): <ROUTE>_API_KEY. */
function deriveKeyRef(route) {
  return credentialRef(`${route.toUpperCase().replace(/[^A-Z0-9]+/g, '_')}_API_KEY`)
}

const modelProfile = z.object({
  id: z.string().required(),
  name: z.string(),
  contextWindow: z.number().step(1).min(1),
  maxTokens: z.number().step(1).min(1),
})

const profile = z.object({
  apiKeyEnv: z.string().role('credential-ref'),
  displayName: z.string(),
  api: z.union(PROTOCOLS),
  baseURL: z.string(),
  models: z.array(modelProfile),
})

/** Схема namespace — форма апстримного llm-pi-ai: корень = providers-словарь. */
const Config = z.object({ providers: z.dict(profile).default({}) })

/**
 * Валидатор раздела: мусор отказывается ПРИ ЗАПИСИ — settings-rejected с
 * этим текстом клиент показывает в карточке (критерий «ошибка видна в
 * Settings»), а не молча ломает реестр. Занятие маршрута штатного
 * провайдера морды отказано здесь же, до регистрации адаптеров.
 */
function assertServiceable(config) {
  const providers = config?.providers
  if (providers === undefined || providers === null) return
  for (const [route, source] of Object.entries(providers)) {
    if (route === EDGE_PROVIDER) {
      throw new Error(`provider-registry: маршрут "${route}" занят штатным провайдером морды; выбери другое имя`)
    }
    if (!ROUTE_PATTERN.test(route)) {
      throw new Error(`provider-registry: имя маршрута "${route}" должно быть lowercase kebab-case (буквы/цифры, дефис-разделители)`)
    }
    if (source.baseURL !== undefined && !/^https?:\/\/\S+/.test(source.baseURL)) {
      throw new Error(`provider-registry: у провайдера "${route}" baseURL должен быть http(s)-адресом`)
    }
    if (source.displayName !== undefined && source.displayName.length === 0) {
      throw new Error(`provider-registry: у провайдера "${route}" displayName не может быть пустым`)
    }
    const ids = new Set()
    for (const model of source.models ?? []) {
      if (typeof model.id !== 'string' || model.id.length === 0) {
        throw new Error(`provider-registry: у провайдера "${route}" есть модель с пустым id`)
      }
      if (ids.has(model.id)) {
        throw new Error(`provider-registry: провайдер "${route}" перечисляет модель "${model.id}" дважды`)
      }
      ids.add(model.id)
    }
  }
}

/** Потолок ответа маршрута: env морды, иначе её же деплой-потолок. */
function routeMaxTokens() {
  const raw = process.env.DEEPSEEK_MAX_OUTPUT_TOKENS
  const parsed = raw === undefined ? Number.NaN : Number(raw)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : DEFAULT_MAX_TOKENS_FALLBACK
}

/** Профиль → соединение DeepSeekAdapter; отказ resolve = отказ будущего хода. */
function connectionOf(route, profileValue) {
  return resolveAdapterOptions({
    apiKeyEnv: typeof profileValue.apiKeyEnv === 'string' && profileValue.apiKeyEnv.length > 0
      ? profileValue.apiKeyEnv
      : deriveKeyRef(route),
    baseURL: profileValue.baseURL,
    models: (profileValue.models ?? []).map(model => ({
      id: model.id,
      ...(model.name === undefined ? {} : { name: model.name }),
      ...(model.contextWindow === undefined ? {} : { contextWindow: model.contextWindow }),
      ...(model.maxTokens === undefined ? {} : { maxTokens: model.maxTokens }),
    })),
    maxTokens: routeMaxTokens(),
    defaultContextWindow: DEFAULT_CONTEXT_WINDOW,
  })
}

/**
 * Громкий отказ без ключа — тот же класс, что у штатного провайдера: ход
 * падает с внятным текстом, а не молча ходит с чужим ключом.
 */
function missingCredential(ref) {
  return new LlmError(
    `provider-registry: для маршрута нет API-ключа; задай его в Settings → Models `
    + `(хранится в credential-хранилище воркера) или секретом воркера ${ref}`,
    'MISSING_CREDENTIAL',
  )
}

export default {
  name: 'edge-plugins:provider-registry',
  inject: ['llm'],
  apply(ctx) {
    // Живой раздел namespace: installSettingsSection ставит сюда Thunk
    // () => resolved (у апстримного dsh-llm-deepseek: current = source).
    let section = () => ({ providers: {} })
    // route → () => void: снятие регистрации живого маршрута.
    const routes = new Map()
    // directory-канал реестра; регистрируется первой синхронизацией.
    let directory = null
    // Последний удачный набор directory-записей: отказ replace оставляет
    // канал живым на предыдущем наборе, а не пустым.
    let lastGoodEntries = []

    /** Живое соединение маршрута: перечитывает раздел на каждую операцию. */
    const routeOptions = (route) => {
      let lastGood
      let lastRaw
      return () => {
        const raw = section().providers?.[route]
        if (raw === lastRaw && lastGood !== undefined) return lastGood
        try {
          lastGood = connectionOf(route, raw)
          lastRaw = raw
          return lastGood
        } catch (error) {
          if (lastGood === undefined) throw error
          console.error(`edge-plugin:provider-registry: раздел маршрута "${route}" стал невалидным, работает последняя удачная конфигурация`, error)
          return lastGood
        }
      }
    }

    const registerRoute = (route, source) => {
      const options = routeOptions(route)
      // Первый resolve обязан пройти: маршрут без baseURL не регистрируется.
      options()
      const displayName = typeof source.displayName === 'string' && source.displayName.length > 0
        ? source.displayName
        : route
      // Группа провайдера в пикере моделей зовётся именем записи, а не
      // «DeepSeek» (providerInfo апстримного класса зашит на своё имя).
      class RegistryAdapter extends DeepSeekAdapter {
        providerInfo(provider) {
          return { id: provider, name: displayName }
        }
      }
      const adapter = new RegistryAdapter({
        options,
        resolveApiKey: async (connection) => {
          const credentials = ctx.get('credentials')
          if (credentials !== undefined) {
            const hit = await credentials.resolve(connection.apiKeyEnv)
            if (hit !== undefined) return assertUsableApiKey(hit.value, 'provider-registry', connection.apiKeyEnv)
          }
          const ambient = process.env[connection.apiKeyEnv]
          if (typeof ambient === 'string' && ambient.length > 0) {
            return assertUsableApiKey(ambient, 'provider-registry', connection.apiKeyEnv)
          }
          throw missingCredential(connection.apiKeyEnv)
        },
        resolveUserId: () => getOrCreateAnonymousUserId(),
        resolveAttachments: () => ctx.get('attachments'),
      })
      const registration = ctx.llm.registerAdapter([route], adapter)
      routes.set(route, () => registration())
      console.info(`edge-plugin:provider-registry: маршрут "${route}" зарегистрирован`
        + ` (${options().models.length} моделей, ${options().baseURL})`)
    }

    /** Directory = каталог готовых маршрутов + все настроенные (и «custom»). */
    const directoryEntries = () => {
      const providers = section().providers ?? {}
      const entries = new Map()
      for (const item of DIRECTORY) {
        const source = providers[item.route]
        entries.set(item.route, {
          provider: item.route,
          displayName: source !== undefined && typeof source.displayName === 'string' && source.displayName.length > 0
            ? source.displayName
            : item.displayName,
          settingsNs: NS,
          settingsPath: ['providers', item.route],
        })
      }
      for (const [route, source] of Object.entries(providers)) {
        if (entries.has(route)) continue
        entries.set(route, {
          provider: route,
          displayName: typeof source.displayName === 'string' && source.displayName.length > 0 ? source.displayName : route,
          settingsNs: NS,
          settingsPath: ['providers', route],
          declared: true,
        })
      }
      return [...entries.values()]
    }

    const sync = () => {
      // 1. Снять маршруты, которых в разделе больше нет.
      for (const [route, dispose] of [...routes]) {
        if (section().providers?.[route] !== undefined) continue
        try {
          dispose()
          console.info(`edge-plugin:provider-registry: маршрут "${route}" снят`)
        } catch (error) {
          console.error(`edge-plugin:provider-registry: не удалось снять маршрут "${route}"`, error)
        }
        routes.delete(route)
      }
      // 2. Поднять новые маршруты; отказ одного не мешает остальным.
      for (const [route, source] of Object.entries(section().providers ?? {})) {
        if (routes.has(route)) continue
        try {
          registerRoute(route, source)
        } catch (error) {
          console.error(`edge-plugin:provider-registry: маршрут "${route}" не зарегистрирован; остальные продолжают работать`, error)
        }
      }
      // 3. Directory — атомарной подменой набора; отказ оставляет предыдущий.
      const entries = directoryEntries()
      try {
        if (directory === null) directory = ctx.llm.registerConfigurableProviders(entries)
        else directory.replace(entries)
        lastGoodEntries = entries
      } catch (error) {
        console.error('edge-plugin:provider-registry: не удалось обновить directory-каталог; работает предыдущий набор', error)
        if (directory === null && lastGoodEntries.length > 0) {
          try {
            directory = ctx.llm.registerConfigurableProviders(lastGoodEntries)
          } catch (retryError) {
            console.error('edge-plugin:provider-registry: directory-канал не восстановлен', retryError)
          }
        }
      }
    }

    installSettingsSection(ctx, NS, Config, { providers: {} }, {
      validate: assertServiceable,
      setSource: (source) => {
        section = source
      },
      onChange: sync,
    })

    console.info(`edge-plugin:provider-registry installed v${PLUGIN_VERSION} (namespace ${NS})`)
  },
}
