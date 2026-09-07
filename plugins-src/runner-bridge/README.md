# runner-bridge — первый настоящий плагин dsh-edge

Исходники плагина `@edge-harness/dsh-plugin-runner-bridge` (задача #95,
конвейер — [`openspec/changes/dsh-edge-plugin-system/`](../../openspec/changes/dsh-edge-plugin-system/design.md),
образец — [`plugins-src/hello-world`](../hello-world/README.md)). Серверный
плагин без клиентской части: эффект живёт в чате как два инструмента.

## Что делает

- `runner_task {title, body}` — создаёт issue с меткой `task` в репозитории
  пула и диспетчит `worker.yml` (`{ref: main, inputs: {task: "<номер>"}}`).
  Агент в чате получает номер задачи, ссылку и статус диспетча. Описание
  инструмента несёт правило маршрутизации («умный принцип»): звать при
  серверной работе (сборка/тесты/плагины/git/долгие задачи), лёгкое — в чате.
- `runner_status {issue}` — состояние задачи (open/closed), исполнитель,
  метка `blocked` и связанные PR (открыт / смержен / закрыт) через
  cross-references таймлайна issue.

## Конфигурация (env воркера)

| Переменная | Откуда | Смысл |
|---|---|---|
| `GH_RUNNER_TOKEN` | секрет воркера; деплой синхронизирует из секрета репозитория `GH_PIPELINE_PAT` (широкий PAT конвейера: issues + workflow dispatch — доказано оркестратором; узкий `GH_DISPATCH_TOKEN` морде не подходит) | вызовы GitHub API из инструментов |
| `GH_RUNNER_REPO` | переменная воркера (`owner/repo`), задаётся в `deploy-dsh-edge.yml` | репозиторий пула задач |

Чтение — через `process.env` воркера (заполнение секретами включено по
умолчанию для compatibility date ≥ 2025-04-01; у морды 2026-08-14). Отсутствие
или нечитаемость конфигурации — громкий текст агенту (он озвучит пользователю),
не исключение. Значение токена в вывод инструментов никогда не попадает:
ошибки несут только HTTP-статус и сообщение GitHub.

Каждый fetch ограничен `AbortSignal.timeout(15с)` и отменой хода агента —
зависший вызов GitHub не держит turn.

## Пересборка tarball

```bash
cd plugins-src/runner-bridge
node --check server/index.js server/core.js   # синтаксис
npm pack                                      # edge-harness-dsh-plugin-runner-bridge-0.1.2.tgz
sha256sum edge-harness-dsh-plugin-runner-bridge-0.1.2.tgz
```

Публикация: релиз **этого** репозитория с тегом `plugins-runner-v0.1.2` и
asset'ом `runner-bridge-0.1.2.tgz` (то же содержимое, имя asset'а фиксирует
манифест). Новый sha256 вписывается в `dsh-edge/plugins.json` — только PR,
merge = аппрув владельца.

**Находка ревью PR #411**: пин 0.1.1 в `dsh-edge/plugins.json` не был
подтверждён совпадением с реальным `npm pack` исходников (единственная
содержательная сверка — `sha256sum -c` в `deploy-dsh-edge.yml` на деплое,
`dsh-edge/manifest.mjs` проверяет только форму хеша, не совпадение). Бамп до
0.1.2 (а не перезалив asset'а 0.1.1) — сознательный выбор: не переписывать
уже опубликованный релиз. Пин в манифесте — реальный хеш `npm pack` этого
среза исходников; сборка определяется содержимым, не mtime файлов: повторный
`npm pack` в свежем чекауте и независимая сборка ревьюера дали побайтово
один и тот же tarball (node 24 / npm 11, тот же тулчейн, что у
`plugin-forge.yml`). Литерал хеша здесь не называется намеренно: README
входит в tarball, и правка этой строки меняла бы хеш по кругу. Если
пересборка разошлась с пином — пересобери и вставь актуальный в
`dsh-edge/plugins.json`.

**Окно красного деплоя и газ.** Мерж сам триггерит `deploy-dsh-edge.yml`
(push в main по путям `dsh-edge/**`), и первый прогон упадёт громко: релиза
`plugins-runner-v0.1.2` ещё нет; ежедневный крон 04:37 будет перекрашивать
деплой, пока релиз и пин не сойдутся. Прод при этом остаётся на последнем
удачном развёртывании. Газ: сразу после слияния запускается plugin-forge
(`workflow_dispatch`, тот же канал, что привёл plugin-manager 0.1.8:
`gh workflow run plugin-forge -f plugin_path=plugins-src/runner-bridge -f
plugin_id=runner-bridge -f task_issue=390`) — он собирает tarball, публикует
релиз и открывает авто-PR с пином реальной сборки; если байты релиза
разойдутся с пином выше, авто-PR форжа перепишет пин, и мерж авто-PR снова
триггерит деплой — на этот раз зелёный.

`"files"` в `package.json` обязан перечислять и `server/core.js` — забытое
поле молча упаковало бы tarball без файла, который `index.js` теперь
импортирует (`import ... from './core.js'`), и деплой сломался бы
`ERR_MODULE_NOT_FOUND` в проде, не в CI.

Плагин зависимостей не имеет, кроме peer-зависимости `@deepseek-ai/dsh-tools`
(инструменты объявляются апстримным `defineTool`) — она нужна ТОЛЬКО
`server/index.js`; вся логика без неё живёт в `server/core.js` и покрыта
юнит-тестами (`test/server.test.mjs`, подключены в `repo-ci.yml`).

## Контракт cordis: inject

Плагин объявляет `inject: ['tools']`: чтение `ctx.<service>` в apply без
объявления сервиса в `inject` cordis 4 пресекает —
`cannot get property "tools" without inject` (уплывало в #100: инсталл-цикл
изолировал отказ в статус `failed`, сборка оставалась зелёной, тулов нет).
Форма — как у апстримных плагинов (`dsh-tool-web` объявляет
`['tools', 'web', 'systemPrompt']`). Нарушение контракта ловит дым инсталла
[`dsh-edge/smoke-edge-plugins.mjs`](../../dsh-edge/smoke-edge-plugins.mjs) на
каждой сборке морды.
