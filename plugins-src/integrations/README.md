# integrations — интеграции внешних систем как инструменты агента

Исходники плагина `@edge-harness/dsh-plugin-integrations` (задача #115,
эпик интеграций). Двухсторонний плагин по образцу
[`plugins-src/hello-world`](../hello-world/README.md) (сервер + клиент в одном
пакете): серверная половина — прецедент
[`runner-bridge`](../runner-bridge/README.md), клиентская — прецедент
[`plugin-manager`](../plugin-manager/README.md).

## Что делает

**Серверная половина** (`server/`) — четыре инструмента агента чата морды,
каждый — один REST-вызов (I/O, не CPU: тяжёлая работа по-прежнему уезжает
раннеру через `runner_task`, исполнение интеграций в DO не тащится —
[30-rejected п.6](../../docs/research/30-rejected-alternatives.md)):

- `jira_issue {issue}` — задача из Jira Cloud: заголовок, статус, последние
  комментарии (REST v3; `fields=comment` отдаёт первую страницу — свежайшие
  добираются отдельным вызовом по `startAt=total-limit`). Только чтение.
- `confluence_page {query | page_id}` — поиск по CQL (до 5 страниц) и чтение
  содержимого страницы, XHTML storage-формат (REST v1: работает и на cloud
  `/wiki`, и на Data Center). Только чтение.
- `bitbucket_pr {repository, title, source, destination, description?}` —
  создание pull request в Bitbucket Cloud между существующими ветками.
- `slack_post {channel, text}` — сообщение в канал Slack (Web API
  `chat.postMessage`).

Сквозной сценарий эпика собирается из уже установленных частей:
`jira_issue` → `runner_task` (работа на раннере) → `bitbucket_pr` →
`slack_post`. Telegram в плагине отсутствует намеренно: его транспорт — job'ы
раннеров (эскалации #91), в реестре он объявлен с `wired: "jobs"`.

**Клиентская половина** (`src/body.js` + `build.mjs`) — секция
**Settings → Интеграции** (слот `settings.section`, id `integrations`,
order 94, рядом с «Плагинами»): по строке на интеграцию из реестра — что
подключено, какими инструментами выражено, чей ключ (ИМЕНА секретов с
описаниями; значений секретов не существует ни в реестре, ни в бандле) и
живой статус из журнала: `GET /api/harness/events?task_id=integration:<id>`
(прокси морды, как у plugin-manager), последнее событие `integration_status`
побеждает, страницы проходятся до конца выборки. Ответ не по форме контракта —
ошибка секции, а не «статусов нет».

## Реестр — одно место правды

[`dsh-edge/integrations.json`](../../dsh-edge/integrations.json) декларирует:
id, человекочитаемое описание, имена инструментов, имена секретов с
описаниями («чей ключ»), `wired` (`morde` — секреты живут в воркере морды,
`jobs` — у job'ов раннеров) и ссылку на документацию API. Форма —
[`dsh-edge/integrations.mjs`](../../dsh-edge/integrations.mjs) (CLI = валидация
+ гвардия проводки: каждый секрет реестра обязан быть упомянут в workflow'е
своей проводки, иначе расхождение красит CI/деплой громко). Инструменты
реестра и `server/index.js` называют друг друга в обе стороны — это гвардия
сборки плагина (`build.mjs`).

Добавление интеграции = запись в реестр + секреты репозитория + проводка имён
в env шага деплоя (гвардия не даст забыть) — код агента не меняется.

## Конфигурация (секреты репозитория → воркер морды)

| Интеграция | Секреты | Примечание |
|---|---|---|
| `jira` | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Basic auth email:token |
| `confluence` | `CONFLUENCE_BASE_URL`, `CONFLUENCE_EMAIL`, `CONFLUENCE_API_TOKEN` | cloud-база с `/wiki` на конце |
| `bitbucket` | `BITBUCKET_USER`, `BITBUCKET_APP_PASSWORD` | Basic auth; app password с `pullrequest:write` |
| `slack` | `SLACK_BOT_TOKEN` | scope `chat:write`, бот приглашён в канал |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | уже живут в секретах репозитория, читаются job'ами (#91) |

Синк — шаг «Секреты интеграций морды» в `deploy-dsh-edge.yml`: присутствующие
секреты `wrangler secret put` в воркер, отсутствие — не красит деплой, а даёт
`integration_status` = `not_configured` с именами (видимость — журнал и
раздел «Интеграции», не тишина).

Маскирование (критерий #115): значения секретов в вывод инструментов не
попадают в принципе; сверх того каждый текст, уходящий агенту, проходит
`scrub()` (`server/core.js`) с полным списком масок интеграции — значение,
его base64-форма и ФАКТИЧЕСКОЕ значение заголовка Basic, то есть
`base64("user:секрет")` (оно не равно склейке base64 частей — выравнивание
на блок зависит от длины user; `basicAuthMask` строит его из той же пары,
из которой собирается `Authorization`, поэтому под маску попадает ровно то,
что реально уезжает в заголовке). GitHub маскирует в логах только точное
совпадение — производные не светим. Ошибки API несут только HTTP-статус и
сообщение провайдера.

Каждый fetch ограничен `AbortSignal.timeout(15с)` и отменой хода агента.

## Тесты

```bash
node --test plugins-src/integrations/test/core.test.mjs    # чистая логика, прод-форма API, маскирование
node --test plugins-src/integrations/test/client.test.mjs  # бандл, слот, статусы на прод-форме журнала
bash scripts/plugins/test/status-scripts.smoke.sh          # обёртки статусов журнала на заглушках
node dsh-edge/integrations.mjs                             # форма реестра + гвардия проводки
```

## Пересборка tarball

```bash
cd plugins-src/integrations
node build.mjs          # сгенерирует client/client.js + integrations.json, прогонит гвардии
node --check server/index.js
npm pack                # edge-harness-dsh-plugin-integrations-0.1.0.tgz
sha256sum edge-harness-dsh-plugin-integrations-0.1.0.tgz
```

Публикация (конвейер #80, по образцу plugin-manager): релиз **этого**
репозитория с тегом `plugins-integrations-v0.1.0`, asset
`integrations-0.1.0.tgz` (то же содержимое, что у npm-pack'а, имя asset'а
фиксирует манифест), затем sha256 — PR'ом в `dsh-edge/plugins.json`:

```bash
cp edge-harness-dsh-plugin-integrations-0.1.0.tgz integrations-0.1.0.tgz
gh release create plugins-integrations-v0.1.0 integrations-0.1.0.tgz \
  --title "plugins-integrations-v0.1.0 — integrations: инструменты Jira/Confluence/Bitbucket/Slack + раздел «Интеграции»" \
  --notes "Плагин #115: агентные инструменты (jira_issue, confluence_page, bitbucket_pr, slack_post) и секция Settings → Интеграции по реестру dsh-edge/integrations.json со статусами из журнала."
```

Сгенерированное (`client/`, `integrations.json`) в git не хранится: реестр
меняется — пересборка перед каждым `npm pack` обязательна.

## Не подтверждено

- Живые вызовы API не прогонялись: секретов Jira/Confluence/Bitbucket/Slack в
  репозитории ещё нет (они есть только у владельца). Формы запросов/ответов
  взяты из официальной документации и покрыты тестами как фикстуры; первый
  живой прогон — после добавления секретов владельцем и деплоя.
- Bitbucket Data Center (`rest/api/1.0`) не поддержан — только Cloud 2.0.
