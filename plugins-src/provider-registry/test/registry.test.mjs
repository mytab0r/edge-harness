// Юнит-проверки провайдер-реестра морды (#378). Фикстура — настоящие пакеты
// @deepseek-ai (tarball'ы с пином целостности, тот же supply-chain паттерн,
// что у streamer.test.mjs): plugin инсталлируется в настоящий cordis Context
// с настоящими LlmRuntime + SettingsProvider, не пересказ API.
// Bare-импорты кода плагина резолвятся в фикстуру resolve-хуком (node:module):
// у репозитория своего node_modules нет по построению (пакеты @deepseek-ai
// ставятся только tarball'ами).
// Запуск: node --test plugins-src/provider-registry/test/registry.test.mjs
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { register } from 'node:module';
import { pathToFileURL } from 'node:url';
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

// Пины фикстур: sha512 скачанных tarball'ов; расхождение — красный тест
// (подмена или перезапись релиза), не тихое чтение чужого кода.
// Версии сверены с package.json (dependencies) — issue #806/#507: пин фикстуры
// обязан идти следом за реальной версией, которую резолвит рантайм (пин
// dsh-edge/upstream.json), иначе тест зелёный на API, которого в проде уже
// нет (ровно так «settings namespace … is not registered» тихо проглотило
// бы регресс ctx.settings.installSection — метода нет в 0.1.1-rc.2).
//
// Пиннётся ЗАМКНУТИЕ, а не только прямые зависимости плагина (инцидент
// 2026-09-22, волна upstream rc.3, опубликована 05:40Z): npm авто-ставит
// peer-зависимости и резолвит их диапазоны `^0.1.5-rc.2` в НОВЕЙШУЮ
// подходящую версию с реестра. Пока апстрим не выпустил rc.3, плавающий
// dsh-fs резолвился в rc.2 и дерево было согласованным; после публикации
// rc.3 тот же диапазон стал приносить dsh-fs/dsh-sandbox rc.3 с peer
// `dsh-llm ^0.1.5-rc.3` — конфликт с запиненным tarball'ом rc.2, ERESOLVE,
// красный прогон на каждом PR. Поэтому в список входят ВСЕ пакеты
// @deepseek-ai, достижимые из фикстуры по deps+peers (замыкание посчитано
// по реестру), каждый прижат к rc.2 с собственным integrity: мир фикстуры
// не плавает внутри @deepseek-ai вовсе, и следующая волна апстрима не
// может сломать её молча — только громко, несовпадением integrity. Плавают
// по-прежнему только не-@deepseek транзитивки (zod, eventsource-parser) —
// их диапазоны ведут к чужим стабильным версиям, не к волне rc.
const FIXTURES = [
  // Пины 0.2.0-rc.1 — closure deps+peers по версии package.json плагина (пин
  // dsh-edge/upstream.json 0.19.0). dsh-settings оставлен в фикстуре только
  // ради redactSecrets для describe(); сам шов настроек здесь — MemoryEdgeSettings
  // ниже, зеркало контракта dsh-edge/src/edge-settings.ts (installSection в
  // 0.2.0-rc.1 снят целиком, красный деплой run 37497300822).
  ['@deepseek-ai/cordis', '4.0.4', 'sha512-obgyxqWAmFn3Re8kvsuUnyW+ihrz6eJCnJO4fh1cQzDtmPYz/zzVeUkH9R94I0OwSVOocK67Kgakm04j/oQXzg=='],
  ['@deepseek-ai/dsh-anonymous-user-id', '0.2.0-rc.1', 'sha512-FMR2+JfTLM4/xMREZeqXuKxSyegVBPjVuJPCumj0ZR9ZI+3R1lVQTM9cuz+Z3WnE1jtiryqnZ7KswldE45El0w=='],
  ['@deepseek-ai/dsh-atomic-write', '0.2.0-rc.1', 'sha512-xfqotH4A560oEQl+EzvyAJFjhiFF68ywiM/LuSuMQ2xvqLnaX50tJV/WCCBfVguM+4x/hObI0jz4oLDBs0d2Kg=='],
  ['@deepseek-ai/dsh-attachment', '0.2.0-rc.1', 'sha512-aKKFE9fGYYn1BCzDtun7IYkpiMANYt7s51vwPukkCnobNiLfxHrIyANLWNmMTSpq0iENfqREXPR805K/um8Hfw=='],
  ['@deepseek-ai/dsh-brand', '0.2.0-rc.1', 'sha512-TPeMaUt/Xev0X2TfIf6pSVVGiWAbFHy+VWCIW+w8UkC0yH1ep9302JQOBqhUgefAM4s/3W1R8lra/PMbFhjx2w=='],
  ['@deepseek-ai/dsh-credentials', '0.2.0-rc.1', 'sha512-XcAy6bK1lHLW3XWxxwKtrFdDurEvEvpBoqNhfR6PHTetd8wV/f9l06uNg0DO7xg1GUzkY8t88j2EXhg2z2nFrw=='],
  ['@deepseek-ai/dsh-deepseek-llm-api-extensions', '0.2.0-rc.1', 'sha512-IlXvOxcKeE1MWKGTEs/jOyMxcEF/bbJsX3FvSeRWx8d8iRSTis5iWvkJktyf6bX5aQ2zNEOfbZbZKXkWkaZMmw=='],
  ['@deepseek-ai/dsh-fs', '0.2.0-rc.1', 'sha512-IWjXn/BsoWwT5dti/Jfkrx49GDGi1H7B3cuTgwRvxIQl6o86eb7ns+3mQiBpFyrCrVOMV+1t8FC1ipiIxol+gA=='],
  ['@deepseek-ai/dsh-home-paths', '0.2.0-rc.1', 'sha512-QWHegbfNY4J7qZgBdiWshhhcF6fgybua6XKUaMIhxj4J+mqR9KEQOJ7hRjOOg8Yub3nGbBTyqpH10C44EGGroA=='],
  ['@deepseek-ai/dsh-invariants', '0.2.0-rc.1', 'sha512-sEeO6sRPGxgHChQsiU/V3LvelbVaLt7kb4WHVQqljZGgoAv3VZK0rzSlQaGfTCYJrThcoJ/a9mCT9pcZEZ3aDg=='],
  ['@deepseek-ai/dsh-launch-environment', '0.2.0-rc.1', 'sha512-ADTbIFhcVnFSYmycI4Ln4q9GGv5YS+MRg0ahCbn0+rDLcYyx4TT9B9Xyiz856YRRPpYRbYwQqZ+o14kCDpicQA=='],
  ['@deepseek-ai/dsh-llm', '0.2.0-rc.1', 'sha512-F5ZlBG8z8o5PfEWeEF/PN9t/A1N/oEErvqmpqE4J8f9mJR85DvBdV+rjDs7OjAbMJsJ8INjkbroruG5NNKDg5A=='],
  ['@deepseek-ai/dsh-llm-deepseek', '0.2.0-rc.1', 'sha512-ENUE7f58O3neU2G72IpZP5t/0GBoKr748KEcNIrccmO+kIENjAwuOM87QY2Uw1GPcSBfVUx/TELolgcyAPpCTg=='],
  ['@deepseek-ai/dsh-sandbox', '0.2.0-rc.1', 'sha512-NnEHqSf4SKoM5cIqtHg+utLUZYJX4r9u2PwQbEpYwiOYSvOW5G4DRtLRpIX7J6V2HOC6CXo2Xcm3DJBOsbKYOA=='],
  ['@deepseek-ai/dsh-scope', '0.2.0-rc.1', 'sha512-D1JeWIz40A62/nJIi7HwLsddmuDwdcxV9Lbx5IgBveWDZHGqPIP0/CPJ4o6XWr+C1qI/GBtdMkN2Z3F20Rlt7w=='],
  ['@deepseek-ai/dsh-session', '0.2.0-rc.1', 'sha512-KUDCUk8kmiJCwvV3gDbkUSpkyoGHdg36nIhKsEh6iBoYDuIQCuvX2htiVRXCm099XZO6rCBVInmm1193UoPYcA=='],
  ['@deepseek-ai/dsh-settings', '0.2.0-rc.1', 'sha512-kC7Cq6Ls6of8J9qY0/8oehLbf9AV62QVA9dL0YPOnvdvgaNJaRbLtXxwr+I36rjDVmOfUkqvW4nxBIjJmPSY/g=='],
  ['@deepseek-ai/dsh-timeout', '0.2.0-rc.1', 'sha512-SQpvDLIPU0EJP1lbphfuEkLrvnX+CE9u9ym1+L5z/8dYe9BCPbhgjQoRvDnyzKZqFyoXVvZFdqdAADlc6DX7Ig=='],
  ['@deepseek-ai/dsh-typert-protocol', '0.2.0-rc.1', 'sha512-MhpeimGP6E3MqSESLm32OmPgx5aNUG7yCJCgcIFw7XBWhQmKB9fU3CWuUxxbWZeC7eWkdJ93AG2ww/Qu6QWW9A=='],
  ['@deepseek-ai/dsh-util-crypto', '0.2.0-rc.1', 'sha512-BLBQVRGBlzhNprSpREiMDi1JwX0X8jKdEPWiVRKBnttlG9d33j1pLsaiH9HHLY/D10lXX/NagGia/Zz9mW4FBA=='],
  ['@deepseek-ai/dsh-util-values', '0.2.0-rc.1', 'sha512-Eh1HH0LFvztBwU2xotdfW5oCwLlWGjK2ygCC5+PnC3tkSDh9gvVQpEf0ezK+Kk6vpLz117V7VPehhR1+gs90+Q=='],
  ['@deepseek-ai/schemastery', '3.18.4', 'sha512-SSXO6tYuyrIqKVbmOnIq0s+riUywYzouFMcnBluHF9n4KMo2G8HvEzhCYj6pj2617/glOjbJuVInJ9hfONsjkg=='],
];const fixtureRoot = join(
  tmpdir(),
  `provider-registry-fixture-${createHash('sha256').update(FIXTURES.map((f) => f[2]).join('|')).digest('hex').slice(0, 8)}`,
);

// Сборка фикстуры — синхронная, на верхнем уровне: импорты пакетов ниже по
// коду обязаны видеть готовое node_modules (top-level await до хука before()
// node:test не упорядочен — проверено красным прогоном).
if (!existsSync(join(fixtureRoot, 'node_modules', '@deepseek-ai', 'dsh-settings', 'package.json'))) {
  rmSync(fixtureRoot, { recursive: true, force: true });
  mkdirSync(fixtureRoot, { recursive: true });
  writeFileSync(join(fixtureRoot, 'package.json'), JSON.stringify({ name: 'provider-registry-fixture', private: true }));
  const names = [];
  for (const [name, version, integrity] of FIXTURES) {
    const pack = spawnSync('npm', ['pack', `${name}@${version}`], {
      cwd: fixtureRoot, encoding: 'utf8', shell: process.platform === 'win32',
    });
    if (pack.status !== 0) throw new Error(`npm pack фикстуры ${name} упал: ${pack.stderr}`);
    const tgz = readdirSync(fixtureRoot).find((f) => f.endsWith('.tgz') && !names.includes(f));
    if (!tgz) throw new Error(`npm pack ${name} не оставил tarball`);
    const actual = 'sha512-' + createHash('sha512').update(readFileSync(join(fixtureRoot, tgz))).digest('base64');
    assert.equal(actual, integrity, `integrity mismatch: tarball ${name} не совпал с пином`);
    names.push(tgz);
  }
  const install = spawnSync('npm', ['install', '--no-audit', '--no-fund', ...names], {
    cwd: fixtureRoot, encoding: 'utf8', shell: process.platform === 'win32',
  });
  if (install.status !== 0) throw new Error(`npm install фикстур упал: ${install.stderr}`);
}

// Resolve-хук: bare-спецификаторы кода плагина резолвятся из node_modules
// фикстуры. Хук ставится до первого импорта пакетов и кода плагина.
const hooksSource = `
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const require = createRequire(${JSON.stringify(join(fixtureRoot, 'package.json'))});
export async function resolve(specifier, context, nextResolve) {
  if (specifier.startsWith('@deepseek-ai/')) {
    return { url: pathToFileURL(require.resolve(specifier)).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
`;
const hooksPath = join(fixtureRoot, 'provider-registry-hooks.mjs');
writeFileSync(hooksPath, hooksSource);
register(pathToFileURL(hooksPath).href, import.meta.url);

const { Context, Service } = await import('@deepseek-ai/cordis');
const { LlmRuntime } = await import('@deepseek-ai/dsh-llm');
const { redactSecrets } = await import('@deepseek-ai/dsh-settings');
const { default: z } = await import('@deepseek-ai/schemastery');
const plugin = (await import('../server/index.js')).default;
const DIRECTORY = (await import('../directory.json', { with: { type: 'json' } })).default;

// In-memory стенд настроек — зеркало контракта dsh-edge/src/edge-settings.ts
// (шов, который Edge serve'ит на пине 0.19.0: installSection в
// @deepseek-ai/dsh-settings@0.2.0-rc.1 снят целиком — красный деплой
// run 37497300822). Поверхность: register(ns, schema, {base, validate}) →
// scope {get, watch, update, replace}; describe({redactSecrets}); mutate
// set|unset с отказом через validate. Хранение — this.document (load/persist —
// точки подмены, как DO KV в проде). Поведенческий гейт шва в проде —
// dsh-edge/registry-integration/check.mjs против собранного воркера.
class MemoryEdgeSettings extends Service {
  writable = true
  document = {}
  registrations = new Map()

  static inject = []
  constructor(ctx) { super(ctx, 'settings') }

  async* [Service.init]() {
    yield () => Promise.resolve()
    const stored = await this.load()
    this.document = stored ?? {}
  }

  async load() { return this.document }
  async persist(document) { this.document = document }

  register(name, schema, options = {}) {
    const registration = { ns: name, schema, base: options.base, validate: options.validate, resolved: undefined, revision: 0, watchers: new Set() }
    if (this.registrations.has(registration.ns)) throw new Error(`settings namespace "${registration.ns}" is already registered`)
    registration.resolved = this.resolveInternal(registration)
    this.registrations.set(registration.ns, registration)
    return {
      get: () => registration.resolved,
      watch: (callback) => {
        registration.watchers.add(callback)
        return () => registration.watchers.delete(callback)
      },
      update: (patch) => this.update(registration.ns, patch),
      replace: (section) => this.replace(registration.ns, section),
    }
  }

  describe(options) {
    return [...this.registrations.values()].map((registration) => {
      const user = this.document[registration.ns]
      const descriptor = {
        ns: registration.ns,
        autoGenerate: true,
        schema: registration.schema.toJSON(),
        value: registration.resolved,
        revision: registration.revision,
        ...(registration.base === undefined ? {} : { base: registration.base }),
        ...(user === undefined ? {} : { user }),
        applies: 'live',
      }
      if (options?.redactSecrets !== true) return descriptor
      const redacted = redactSecrets(registration.schema, registration.resolved)
      return { ...descriptor, value: redacted.value, secrets: redacted.secrets }
    })
  }

  async update(ns, patch, expectedRevision) { return this.writeInternal(ns, 'merge', patch, expectedRevision) }
  async replace(ns, section, expectedRevision) { return this.writeInternal(ns, 'replace', section, expectedRevision) }
  async mutate(ns, ops, expectedRevision) {
    if (!Array.isArray(ops) || !ops.every((op) => op !== null && typeof op === 'object'
      && (op.op === 'set' || op.op === 'unset') && Array.isArray(op.path))) {
      return Promise.reject(new TypeError(`settings mutate for "${ns}" ops must be {op:'set'|'unset', path: string[]}`))
    }
    return this.writeInternal(ns, 'mutate', { ops }, expectedRevision)
  }

  async writeInternal(ns, mode, input, expectedRevision) {
    const registration = this.registrations.get(ns)
    if (registration === undefined) throw new Error(`settings namespace "${ns}" is not registered`)
    if (expectedRevision !== undefined && expectedRevision !== registration.revision) {
      throw new Error(`settings namespace "${ns}" was changed concurrently`)
    }
    const current = this.document[ns] ?? {}
    let section
    if (mode === 'replace') {
      section = structuredClone(input)
    } else {
      section = structuredClone(current)
      if (mode === 'merge') for (const [key, value] of Object.entries(input)) section[key] = value
      else for (const op of input.ops) {
        if (op.op === 'unset') {
          let node = section
          for (const key of op.path.slice(0, -1)) { node = node?.[key]; if (node === undefined) break }
          if (node !== undefined) delete node[op.path.at(-1)]
        } else {
          let node = section
          for (const key of op.path.slice(0, -1)) {
            if (node[key] === undefined || node[key] === null) node[key] = {}
            node = node[key]
          }
          node[op.path.at(-1)] = structuredClone(op.value)
        }
      }
    }
    const next = this.resolveInternal(registration, section)
    if (JSON.stringify(current) !== JSON.stringify(section)) {
      registration.revision += 1
      await this.persist({ ...this.document, [ns]: section })
    }
    this.commitInternal(registration, next)
  }

  resolveInternal(registration, sectionOverride) {
    const section = sectionOverride ?? this.document[registration.ns]
    const candidate = { ...(registration.base ?? {}), ...(section ?? {}) }
    const value = registration.schema(candidate)
    registration.validate?.(value)
    return value
  }

  commitInternal(registration, next) {
    registration.resolved = next
    for (const callback of [...registration.watchers]) void callback(next, next)
  }
}

/** Морда в миниатюре: LlmRuntime + Settings + реестр. */
async function mountMordre() {
  const ctx = new Context();
  await ctx.plugin(LlmRuntime);
  await ctx.plugin(MemoryEdgeSettings);
  await ctx.plugin(plugin);
  return ctx;
}

const namespaceView = (ctx) => ctx.settings
  .describe({ redactSecrets: true })
  .find((d) => d.ns === 'llm-pi-ai');

const directoryRoutes = (ctx) => ctx.llm.listConfigurableProviders().map((e) => e.provider);
const activeRoutes = (ctx) => ctx.llm.listProviders().map((p) => p.id);

// Тот же обход схемы, что у клиента (protocolChoices): object→dict→object→api.
function protocolChoices(view) {
  const root = new z(view.schema);
  let node = root;
  for (const key of ['providers', '\0probe', 'api']) {
    if (node.type === 'object') node = node.dict?.[key];
    else if (node.type === 'dict' || node.type === 'array') node = node.inner;
    else return [];
  }
  if (node?.type !== 'union' || !Array.isArray(node.list)) return [];
  return node.list.map((entry) => entry.value).filter((v) => typeof v === 'string');
}

describe('provider-registry: монтирование', () => {
  it('инсталлируется и публикует namespace llm-pi-ai', async () => {
    const ctx = await mountMordre();
    const view = namespaceView(ctx);
    assert.ok(view !== undefined, 'namespace llm-pi-ai не появился в settings.describe');
    assert.equal(typeof view.revision, 'number');
    assert.deepEqual(view.value, { providers: {} });
  });

  it('оживляет protocolChoices: кнопка создания провайдера видит протокол', async () => {
    const ctx = await mountMordre();
    assert.deepEqual(protocolChoices(namespaceView(ctx)), ['openai-completions']);
  });

  it('directory содержит готовые маршруты, ни один не активен', async () => {
    const ctx = await mountMordre();
    const routes = directoryRoutes(ctx);
    // Состав каталога — из directory.json (одно место правды), не пересказ.
    for (const { route: expected } of DIRECTORY) {
      assert.ok(routes.includes(expected), `в directory нет ${expected}`);
    }
    assert.deepEqual(activeRoutes(ctx), [], 'ненастроенные маршруты не имеют права быть активными');
  });
});

describe('provider-registry: добавление провайдера (как CustomProviderCard)', () => {
  const ZHIPU_PROFILE = {
    displayName: 'Z.ai (GLM)',
    apiKeyEnv: 'ZHIPU_API_KEY',
    api: 'openai-completions',
    baseURL: 'https://open.bigmodel.cn/api/paas/v4',
    models: [{ id: 'glm-4.6', name: 'GLM-4.6', contextWindow: 204800, maxTokens: 131072 }],
  };

  it('settings.mutate делает маршрут активным и видимым в пикере', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: ZHIPU_PROFILE },
    ], undefined);
    assert.ok(activeRoutes(ctx).includes('zhipu'), 'маршрут не зарегистрирован');
    const group = ctx.llm.listProviders().find((p) => p.id === 'zhipu');
    assert.equal(group.name, 'Z.ai (GLM)', 'группа пикера должна зваться displayName записи');
    const models = await ctx.llm.listModels('zhipu');
    assert.deepEqual(models.map((m) => m.id), ['glm-4.6']);
  });

  it('partial-профиль catalog-маршрута без displayName зовётся ОДИНАКОВО в пикере и directory', async () => {
    // Находка ревью PR #453, п.4: registerRoute (пикер) и directoryEntries
    // (Settings) считали фолбэк имени независимо и расходились — profile
    // без displayName для catalog-route (zhipu) давал «Z.ai (GLM)» в
    // directory (из directory.json), но голый route «zhipu» в пикере.
    const ctx = await mountMordre();
    const { displayName: _omit, ...partial } = ZHIPU_PROFILE;
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: partial },
    ], undefined);
    const group = ctx.llm.listProviders().find((p) => p.id === 'zhipu');
    const entry = ctx.llm.listConfigurableProviders().find((e) => e.provider === 'zhipu');
    assert.equal(group.name, 'Z.ai (GLM)', 'пикер обязан взять имя из directory.json, не голый route');
    assert.equal(entry.displayName, group.name, 'directory и пикер обязаны звать маршрут одинаково');
  });

  it('произвольный маршрут появляется в directory с declared (строка и Remove живы)', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'my-gateway'], value: { apiKeyEnv: 'MY_GATEWAY_API_KEY', api: 'openai-completions', baseURL: 'https://gw.example/v1', models: [{ id: 'm1' }] } },
    ], undefined);
    const entry = ctx.llm.listConfigurableProviders().find((e) => e.provider === 'my-gateway');
    assert.ok(entry !== undefined, 'настроенного маршрута нет в directory — UI не покажет строку и Remove');
    assert.equal(entry.declared, true);
    // Класс клиента: removable = путь есть в user-слое и в base его нет.
    const view = namespaceView(ctx);
    assert.equal(view.user.providers?.['my-gateway'] !== undefined, true);
  });

  it('удаление маршрута (unset, как removeProviderProfile) снимает его с регистрации', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: ZHIPU_PROFILE },
    ], undefined);
    await ctx.settings.mutate('llm-pi-ai', [{ op: 'unset', path: ['providers', 'zhipu'] }], undefined);
    assert.ok(!activeRoutes(ctx).includes('zhipu'), 'маршрут остался активным после удаления');
    assert.deepEqual(activeRoutes(ctx), []);
  });

  it('настройка переживает рестарт DO: новый Context на том же документе', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: ZHIPU_PROFILE },
    ], undefined);
    const persisted = JSON.parse(JSON.stringify(ctx.settings.document));

    const restarted = new Context();
    await restarted.plugin(LlmRuntime);
    await restarted.plugin(class extends MemoryEdgeSettings {
      async load() { return persisted }
    });
    await restarted.plugin(plugin);
    assert.ok(activeRoutes(restarted).includes('zhipu'), 'после рестарта маршрут не поднялся из хранилища');
    assert.equal(namespaceView(restarted).value.providers.zhipu.apiKeyEnv, 'ZHIPU_API_KEY');
  });
});

describe('provider-registry: негатив — мусор не проходит и не роняет морду', () => {
  // messagePattern сужает проверку до КОНКРЕТНОЙ причины отказа (по умолчанию —
  // общий префикс модуля). Находка ревью PR #453, п.3: тестовый профиль ниже
  // не несёт apiKeyEnv — общая `/provider-registry/` осталась бы зелёной и на
  // СОВСЕМ ДРУГОМ отказе (отсутствие apiKeyEnv, тоже throw с этим префиксом),
  // если гвардию коллизии с deepseek-official вообще убрать.
  const rejected = async (route, value, messagePattern = /provider-registry/) => {
    const ctx = await mountMordre();
    await assert.rejects(
      () => ctx.settings.mutate('llm-pi-ai', [{ op: 'set', path: ['providers', route], value }], undefined),
      (error) => error instanceof Error && messagePattern.test(error.message),
    );
    return ctx;
  };

  it('маршрут deepseek-official занят штатным провайдером', async () => {
    await rejected(
      'deepseek-official', { baseURL: 'https://x.example/v1', models: [{ id: 'm' }] },
      /занят штатным/,
    );
  });

  it('baseURL не-http(s) отказан при записи (ошибка видна в Settings)', async () => {
    const ctx = await rejected('bad-route', { baseURL: 'not a url at all', models: [{ id: 'm' }] });
    // Отказ записи не сломал реестр: directory жив.
    assert.ok(directoryRoutes(ctx).includes('zhipu'));
  });

  it('пустой id модели и дубликат моделей отказаны', async () => {
    await rejected('r1', { baseURL: 'https://x.example/v1', models: [{ id: '' }] });
    await rejected('r2', { baseURL: 'https://x.example/v1', models: [{ id: 'm' }, { id: 'm' }] });
  });

  it('route-паттерн совпадает с клиентским (иначе клиент не сможет адресовать)', async () => {
    await rejected('Bad_Route', { baseURL: 'https://x.example/v1', models: [{ id: 'm' }] });
  });

  it('профиль без baseURL отказан (иначе транспорт молча унёс бы ключ на api.deepseek.com)', async () => {
    const ctx = await rejected('no-url', { models: [{ id: 'm' }] });
    // Маршрут не зарегистрирован: отказ записи не оставил живого маршрута.
    assert.ok(!activeRoutes(ctx).includes('no-url'));
  });

  it('профиль без apiKeyEnv отказан (находка ревью #453 — сервер не выводит имя ref сам)', async () => {
    const ctx = await rejected('no-key', { baseURL: 'https://x.example/v1', models: [{ id: 'm' }] });
    // Маршрут не зарегистрирован: без apiKeyEnv отказ записи, не тихая
    // регистрация с угаданным именем ref'а.
    assert.ok(!activeRoutes(ctx).includes('no-key'));
  });

  it('правка настроенного маршрута доходит до запроса без перерегистрации', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: { displayName: 'Z.ai (GLM)', apiKeyEnv: 'ZHIPU_API_KEY', api: 'openai-completions', baseURL: 'https://old.example/v1', models: [{ id: 'glm-4.6' }] } },
    ], undefined);
    assert.deepEqual((await ctx.llm.listModels('zhipu')).map((m) => m.id), ['glm-4.6']);
    // Владелец правит каталог маршрута (та же карточка, тот же route-id):
    // перерегистрации нет, живой options() перечитывает раздел.
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu', 'models'], value: [{ id: 'glm-4.7' }] },
    ], undefined);
    assert.deepEqual((await ctx.llm.listModels('zhipu')).map((m) => m.id), ['glm-4.7'],
      'клив живого чтения раздела: список моделей не обновился');
  });

  it('ключ, записанный в credential-хранилище, разрешается (ветка ctx.credentials)', async () => {
    // Морда с credentials-сервисом (в проде — EdgeCredentialProvider: DO KV,
    // env-фолбэк). Ход к маршруту на закрытом порту: отказ ТРАНСПОРТА, не
    // MISSING_CREDENTIAL — значит ключ из хранилища разрешился до fetch.
    const ctx = new Context();
    await ctx.plugin(LlmRuntime);
    await ctx.plugin(MemoryEdgeSettings);
    // Стаб запоминает аргумент: контракт формата ссылки обязан быть закреплён
    // тестом (находка ревью PR #453) — сервер зовёт resolve РОВНО с apiKeyEnv
    // из профиля, никакой деривации имени ссылки на сервере нет.
    const resolvedRefs = [];
    await ctx.effect(() => ctx.provide('credentials', {
      resolve: async (ref) => {
        resolvedRefs.push(ref);
        return { value: 'stored-dummy-key-1234567890' };
      },
    }), 'fixture credentials');
    await ctx.plugin(plugin);
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: { apiKeyEnv: 'ZHIPU_API_KEY', api: 'openai-completions', baseURL: 'https://127.0.0.1:9/v1', models: [{ id: 'glm-4.6' }] } },
    ], undefined);
    let terminal;
    for await (const chunk of ctx.llm.stream({ provider: 'zhipu', model: 'glm-4.6', messages: [] })) {
      terminal = chunk;
      break;
    }
    assert.equal(terminal.type, 'finish');
    assert.equal(terminal.reason.kind, 'error');
    assert.notEqual(terminal.reason.failure.code, 'MISSING_CREDENTIAL');
    assert.doesNotMatch(terminal.reason.failure.message, /нет API-ключа/);
    assert.deepEqual(resolvedRefs, ['ZHIPU_API_KEY'],
      `credentials.resolve звали с ${JSON.stringify(resolvedRefs)} — ожидался ровно apiKeyEnv профиля без деривации`);
  });

  it('ход без ключа падает громко MISSING_CREDENTIAL, не молча (сеть не вызывается)', async () => {
    const ctx = await mountMordre();
    // apiKeyEnv указан (обязателен, находка ревью #453 — сервер больше не
    // выводит имя ref'а сам), но ни credentials-сервиса, ни env-переменной
    // с таким именем нет: ref не резолвится, ход обязан упасть громко.
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'keyless'], value: { apiKeyEnv: 'KEYLESS_API_KEY', api: 'openai-completions', baseURL: 'https://keyless.example/v1', models: [{ id: 'm1' }] } },
    ], undefined);
    assert.ok(activeRoutes(ctx).includes('keyless'));
    // Ход к маршруту без ключа: терминальный error-чunk с кодом и именем
    // ссылки (тот же класс отказа, что у штатного провайдера). До HTTP не
    // доходит: resolveApiKey бросает раньше запроса.
    let terminal;
    for await (const chunk of ctx.llm.stream({ provider: 'keyless', model: 'm1', messages: [] })) {
      terminal = chunk;
      break;
    }
    assert.equal(terminal.type, 'finish');
    assert.equal(terminal.reason.kind, 'error');
    assert.equal(terminal.reason.failure.code, 'MISSING_CREDENTIAL');
    assert.match(terminal.reason.failure.message, /KEYLESS_API_KEY/);
  });
});
