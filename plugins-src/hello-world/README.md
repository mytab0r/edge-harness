# hello-world — PoC-плагин канала dsh-edge

Исходники плагина `@edge-harness/dsh-plugin-hello`, доказывающего канал
«манифест → бандл → деплой → морда» (задача #80, дизайн
[`openspec/changes/dsh-edge-plugin-system/`](../../openspec/changes/dsh-edge-plugin-system/design.md)).

Состав пакета:

- `server/index.js` — серверная половина: cordis-плагин (default export),
  регистрирует в Durable Object инструмент `plugin_hello`. Эффект в чате:
  модель вызывает инструмент и получает приветствие — доказательство, что
  плагин смонтирован.
- `client/client.js` — клиентская половина: готовый бандл в обёртке
  `window.__ModuleLoader__.load({ id, factory })` (та же форма, что строит
  tsdown для `dsh-edge-client-ui`). Экспортирует `inject: []` и `apply(ctx)`;
  при монтировании ставит `document.documentElement.dataset.edgePluginHello =
  "mounted"` и пишет в консоль браузера `[edge-plugin:hello] client plugin
  mounted` — проверяемая улика в морде после reload.
- `package.json` — декларация `dsh.client: { platform: "web" }` и экспорт
  `./client`: контракт ростера, который читает `assemble-standalone-web.mjs`.

## Пересборка tarball

```bash
cd plugins-src/hello-world
node --check server/index.js && node --check client/client.js   # синтаксис
npm pack                                                         # edge-harness-dsh-plugin-hello-0.1.1.tgz
sha256sum edge-harness-dsh-plugin-hello-0.1.1.tgz
```

Публикация: релиз **этого** репозитория с тегом `plugins-hello-v0.1.1` и
asset'ом `hello-world-0.1.1.tgz` (то же содержимое, имя asset'а фиксирует
манифест). Новый sha256 вписывается в `dsh-edge/plugins.json` — только PR,
merge = аппрув владельца.

Плагин зависимостей не имеет, кроме peer-зависимости `@deepseek-ai/dsh-tools`
(инструмент объявляется апстримным `defineTool`); пин версии peer-зависимости
переставляет `.pnpmfile.cjs` апстрима под свой `dshEdge.upstreamVersion`.

Серверная половина объявляет `inject: ['tools']` — контракт cordis 4: чтение
`ctx.<service>` в apply без объявления в `inject` бросает
`cannot get property … without inject` (класс ошибки #100; ловит дым инсталла
[`dsh-edge/smoke-edge-plugins.mjs`](../../dsh-edge/smoke-edge-plugins.mjs)).
