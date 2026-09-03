// Юнит-проверки провайдер-реестра морды (#114). Фикстура — настоящие пакеты
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
const FIXTURES = [
  ['@deepseek-ai/cordis', '4.0.1', 'sha512-YBdskTU2Po1kru3GgcUWUbkTsPMA9LkSQDAY8rBkFJeajdgcQad3QPJZE26JyK99Xb6HaASvoXg2DSUTeN/0Nw=='],
  ['@deepseek-ai/dsh-llm', '0.1.1-rc.2', 'sha512-ASJfjIdZbIXvLwi3rGo+eZb/GxMVV/WO5/XVD3B96mT8EIzrlw3+nMR6/CvmJVzcycKQ2XN0wj7jD6TasPRySA=='],
  ['@deepseek-ai/dsh-llm-deepseek', '0.1.1-rc.2', 'sha512-GH9AukC2kozv6Q8/9DDhACHSe7fpTG7o0iWGUEN/m7/qajCJ8abySOFi3N7otdVmolR6Mvz2GZDTn2HdHqkWWg=='],
  ['@deepseek-ai/dsh-settings', '0.1.1-rc.2', 'sha512-iGdKEt91Im3gE7xA9CzRfTJsPcFcxDeDOCLhAjzbpjEv7TAjx/EoYP7lPtiy+QmOjKSKy206SEtAKol9PXWbOw=='],
  ['@deepseek-ai/dsh-credentials', '0.1.1-rc.2', 'sha512-aeVBaH07rox7NuSNbSqbz8g0eNb2IIhNbrZngj/VxUsr/TR9TXOq7lm1CL6SBUDlstGw3vNWeXyhim/DmA5iSQ=='],
  ['@deepseek-ai/dsh-anonymous-user-id', '0.1.1-rc.2', 'sha512-ZQBsDhI0VuFwoDnq75VT2gPJdMPmBYfWM3EBDmUkwHM0E2dmyr+iLGXxp33a7M64r79x7kN+81KOTEL5LS0E8A=='],
  ['@deepseek-ai/schemastery', '3.18.1', 'sha512-Qn0FCSwCQnpnj6SB31I6i2sIKgKWnkbJM8O0EU91Gv2UsYVvtZTl6IA0sCwk2e2MZf5S8w5hpq9QkeVvK9qwxg=='],
];

const fixtureRoot = join(
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

const { Context } = await import('@deepseek-ai/cordis');
const { LlmRuntime } = await import('@deepseek-ai/dsh-llm');
const { default: SettingsProvider } = await import('@deepseek-ai/dsh-settings');
const { default: z } = await import('@deepseek-ai/schemastery');
const plugin = (await import('../server/index.js')).default;
const DIRECTORY = (await import('../directory.json', { with: { type: 'json' } })).default;

// In-memory провайдер настроек: persist — no-op, write() сам кладёт раздел
// в this.document после persist; load() возвращает текущий документ.
class MemorySettingsProvider extends SettingsProvider {
  writable = true
  async load() { return this.document }
  async persist() {}
}

/** Морда в миниатюре: LlmRuntime + Settings + реестр. */
async function mountMordre() {
  const ctx = new Context();
  await ctx.plugin(LlmRuntime);
  await ctx.plugin(MemorySettingsProvider);
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

  it('произвольный маршрут появляется в directory с declared (строка и Remove живы)', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'my-gateway'], value: { api: 'openai-completions', baseURL: 'https://gw.example/v1', models: [{ id: 'm1' }] } },
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
    await restarted.plugin(class extends MemorySettingsProvider {
      async load() { return persisted }
    });
    await restarted.plugin(plugin);
    assert.ok(activeRoutes(restarted).includes('zhipu'), 'после рестарта маршрут не поднялся из хранилища');
    assert.equal(namespaceView(restarted).value.providers.zhipu.apiKeyEnv, 'ZHIPU_API_KEY');
  });
});

describe('provider-registry: негатив — мусор не проходит и не роняет морду', () => {
  const rejected = async (route, value) => {
    const ctx = await mountMordre();
    await assert.rejects(
      () => ctx.settings.mutate('llm-pi-ai', [{ op: 'set', path: ['providers', route], value }], undefined),
      (error) => error instanceof Error && error.message.includes('provider-registry'),
    );
    return ctx;
  };

  it('маршрут deepseek-official занят штатным провайдером', async () => {
    await rejected('deepseek-official', { baseURL: 'https://x.example/v1', models: [{ id: 'm' }] });
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

  it('правка настроенного маршрута доходит до запроса без перерегистрации', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'zhipu'], value: { displayName: 'Z.ai (GLM)', api: 'openai-completions', baseURL: 'https://old.example/v1', models: [{ id: 'glm-4.6' }] } },
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
    await ctx.plugin(MemorySettingsProvider);
    await ctx.effect(() => ctx.provide('credentials', {
      resolve: async () => ({ value: 'stored-dummy-key-1234567890' }),
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
  });

  it('ход без ключа падает громко MISSING_CREDENTIAL, не молча (сеть не вызывается)', async () => {
    const ctx = await mountMordre();
    await ctx.settings.mutate('llm-pi-ai', [
      { op: 'set', path: ['providers', 'keyless'], value: { api: 'openai-completions', baseURL: 'https://keyless.example/v1', models: [{ id: 'm1' }] } },
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
