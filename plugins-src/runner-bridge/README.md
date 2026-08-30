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
| `GH_RUNNER_TOKEN` | секрет воркера; деплой синхронизирует из секрета репозитория `GH_DISPATCH_TOKEN` (тот же PAT: issues + workflow dispatch у него уже есть — доказано оркестратором) | вызовы GitHub API из инструментов |
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
node --check server/index.js            # синтаксис
npm pack                                # edge-harness-dsh-plugin-runner-bridge-0.1.0.tgz
sha256sum edge-harness-dsh-plugin-runner-bridge-0.1.0.tgz
```

Публикация: релиз **этого** репозитория с тегом `plugins-runner-v0.1.0` и
asset'ом `runner-bridge-0.1.0.tgz` (то же содержимое, имя asset'а фиксирует
манифест). Новый sha256 вписывается в `dsh-edge/plugins.json` — только PR,
merge = аппрув владельца.

Плагин зависимостей не имеет, кроме peer-зависимости `@deepseek-ai/dsh-tools`
(инструменты объявляются апстримным `defineTool`).
