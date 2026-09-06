## Инцидент
После бампа пина dsh-edge 0.8.0 → 0.11.1 (#505/#513, доводка #518) морда отдаёт `/api/health` `{"ok":true,"status":"ready"}`, логин проходит, но интерфейс не отрисовывается: `bodyLength:612`, `rootChildren:1`. e2e-смоук (#502/#541) падает на `getByRole('button', { name: 'Settings' })` и печатает найденную консольную ошибку.

## Точная причина (улика — прогон deploy-dsh-edge.yml #34047489609, 2026-09-06 17:06)
```
[login] console: Error: failed to apply loader entry ef503874 (@deepseek-ai/dsh-client-ui-settings-plugins):
locale namespace "settings.plugins" already has locale "zh"
  at $t (…/assets/index-Df-65__b.js:17:259)
  at uu._init (…/assets/index-Df-65__b.js:17:4021)
```

Апстрим 0.11.0 добавил собственную секцию Settings → Plugins (issue pawaca/dsh-edge#137,
release notes 0.11.0: «`dsh-client-ui-settings-plugins` and `dsh-client-ui-settings-plugin-inventory`
join the Web assembly»). Наш клиентский плагин `@edge-harness/dsh-plugin-manager`
(plugins-src/plugin-manager/src/body.js, раздел «Плагины» #102) регистрирует
локаль-неймспейс **с тем же именем**: `ctx.locale.register("settings.plugins", dictionaries)`
(plugins-src/plugin-manager/src/body.js:620). До 0.11.0 апстрим этот неймспейс не занимал —
коллизия появилась именно с новой upstream-фичей, не с нашей веткой ростера (патч
0003-web-roster-manifest.patch применяется и работает штатно, дело не в нём).

`ctx.locale.register` бросает на дублирующий неймспейс — исключение внутри `ctx.effect`
callback'а одного loader entry обрывает материализацию всего клиентского бандла (boot
получил фазы `bootstrap`/`application` в 0.11.x — падение одной записи роняет остальные),
поэтому шелл остаётся пустым при полностью загруженном документе.

## Почему это не поймали раньше
`docs/research` (dsh-edge/PATCHES.md, «Бамп #505») уже называл этот класс риска
заранее: ни `verify-edge-plugins.mjs`, ни `smoke-edge-plugins.mjs` не проверяют, что
браузер реально материализует React-дерево клиентского плагина — только состав бандла
и серверный cordis-инсталл. e2e-смоук (#502/#541) — первая проверка, которая смотрит
в реальный DOM, и она поймала это в первом же прогоне после бампа.

## Фикс
Переименовать неймспейс локали нашего plugin-manager в уникальное имя (не пересекающееся
с зарезервированным апстримом), например `settings.edgeHarnessPlugins`, бампнуть версию
пакета и прогнать конвейер #80 (plugin-forge → релиз → PR в dsh-edge/plugins.json → деплой).
Откат пина ниже 0.11.0 не рассматривается: он вернул бы уже исправленный в 0.11.1 баг
(#146/#145 апстрима — пустая карточка Plugin configuration на `*.workers.dev`).

## Критерий приёмки
- `plugins-src/plugin-manager` использует уникальный неймспейс локали, версия бампнута.
- Новый релиз плагина прошёл конвейер форжа (чеклист совместимости + интеграционный дым).
- `dsh-edge/plugins.json` обновлён с новым sha256.
- Живой e2e-смоук (`dsh-edge/e2e-smoke/smoke.mjs`) проходит на проде: кнопка «Settings»
  видна, вкладки шелла отрисованы.
- Ручная проверка Playwright с `DSH_EDGE_ACCESS_KEY` подтверждает интерфейс.
