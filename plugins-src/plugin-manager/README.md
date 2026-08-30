# plugin-manager — раздел «Плагины» в настройках морды

Клиентский плагин `@edge-harness/dsh-plugin-manager` (задача #102): секция
**Settings → Плагины** в ростере dsh-edge. Клиент-only: `server: false`,
`client: true` в [`dsh-edge/plugins.json`](../../dsh-edge/plugins.json).

Состав секции:

- **Список плагинов из манифеста** — id и флаги server/client. Манифест
  вшивается в бандл при сборке релиза: `build.mjs` читает
  `dsh-edge/plugins.json` (валидация — `parseManifest` из
  [`dsh-edge/manifest.mjs`](../../dsh-edge/manifest.mjs), одно место правды
  формы), кладёт байт-в-байт копию в пакет как `manifest.json` и вшивает
  срез `[id, server, client]` в бандл константой `MANIFEST`.
- **Статусы из журнала** — по каждому плагину
  `GET /api/events?task_id=plugin:<id>&limit=10&after=<next_after>` с проходом
  до конца выборки (`has_more`/`next_after`): страницы отдаются от старейших,
  и свежайший статус может лежать за пределами первой страницы — каждый деплой
  пишет 2+ события `plugin_status`
  (контракт [`openspec/specs/journal-tasks-hands.md`](../../openspec/specs/journal-tasks-hands.md)).
  Берётся последнее событие `plugin_status` (`state: ready|deploying|failed`,
  плюс `building|built`; detail — в tooltip). Браузер ходит сессионной кукой
  (`credentials: include`), Bearer у браузера нет. Ответ, не совпавший по
  форме контракта, — ошибка секции, а не «статусов нет»: чужой origin может
  ответить другим API, и тихий «установлен» по нему — silent-wrong. Нет
  событий при живом журнале — честное «установлен» (факт из манифеста).
- **Подсказка** — «Новые плагины: попроси агента в чате (умный принцип) —
  раннер соберёт и установит».

Известное ограничение (белое пятно): журнал живёт в воркере `edge-harness`,
морда — в воркере `dsh-edge`; секция ходит по относительному пути `/api/events`
того же origin, где стоит страница, — а этот путь в морде не проксирован к
журналу. Страница получает 404/HTML не-журнальной формы, и content-type-гвардия
превращает это в громкую ошибку статусов; список при этом работает. Разбор и
варианты закрытия (проксирование журнала в origin морды) — issue «белое
пятно» от #102.

## Как устроен бандл

`src/body.js` — тело фабрики; обёртку и константу `MANIFEST` дописывает
`build.mjs`. Форма обёртки — как у продовых бандлов ростера
(`window.__ModuleLoader__.load({ id: "<package>", factory })`, у фабрики
экспортируются `inject` и `apply`). Модули берутся из seed-карты шелла
(`react`, `@deepseek-ai/dsh-client-ui-primitives` — список зашит в
`build.mjs` и проверяется при сборке), сервисы `ctx.slots`/`ctx.locale`
приносят пакеты из `dsh.client.inject` (`dsh-client-runtime`,
`dsh-client-locale`) — их assemble-standalone-web.mjs проверяет по ростеру
и строит порядок загрузки.

Слот — `settings.section` (списочный), id `plugin-manager`, order 95, по
образцу ui-edge. Словари регистрируются в namespace `settings.plugins`
(en/zh/ru, наборы ключей одинаковы).

## Пересборка tarball

```bash
cd plugins-src/plugin-manager
node build.mjs          # сгенерирует client/client.js + manifest.json, прогонит гвардии
npm pack                # edge-harness-dsh-plugin-manager-0.1.1.tgz
```

Публикация (конвейер #80, по образцу hello-world): релиз **этого**
репозитория с тегом `plugins-manager-v0.1.1`, asset
`plugin-manager-0.1.1.tgz` (то же содержимое, что у npm-pack'а, имя asset'а
фиксирует манифест), затем sha256 — PR'ом в `dsh-edge/plugins.json`:

```bash
cp edge-harness-dsh-plugin-manager-0.1.1.tgz plugin-manager-0.1.1.tgz
sha256sum plugin-manager-0.1.1.tgz
gh release create plugins-manager-v0.1.1 plugin-manager-0.1.1.tgz \
  --title "plugins-manager-v0.1.1 — plugin-manager: раздел «Плагины» в морде" \
  --notes "Клиентский плагин #102: список плагинов из манифеста, статусы из журнала, подсказка о заказе новых."
```

Цикличность sha (принято, по образцу hello/runner): вшитая `manifest.json`
в пакете содержит каталог с sha256 **этого же** пакета — финальной своей sha
там быть не может. Проверяется всегда sha артефакта против каталога репо;
вшитая копия нужна ради ростера `[id, server, client]`, который циклы не имеет.

Сгенерированное (`client/`, `manifest.json`) в git не хранится: манифест
меняется — пересборка перед каждым `npm pack` обязательна, иначе в бандл
уйдёт устаревший состав. Гард-рейл сборки: свежий `parseManifest` и
проверка seed-require'ов на каждом запуске `build.mjs`.

## Тесты

```bash
node --test plugins-src/plugin-manager/test/client.test.mjs
```

Поведенческая гвардия (`test/client.test.mjs`, `node --test`, без
зависимостей): обёртка бандла, монтаж `settings.section`, паритет ключей
словарей и ячейки статусов на прод-форме ответа журнала — `{events: [{id,
kind, data}], has_more, next_after}`; последнее событие побеждает, пустой
журнал = «установлен», отказ и чужой ответ = громкая ошибка. Шаг включён в
`repo-ci.yml`. Мутационная проверка: подмена «последнего события» на
«первое» красит набор.
