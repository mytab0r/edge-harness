# Тестовый стек Cloudflare: vitest-plugin, что выяснилось при первом прогоне

> Исследовано 2026-08-28, в ходе walking-skeleton. Всё ниже проверено пробами, а не
> прочитано — каждая проверка стоила времени, поэтому записана. Смежное:
> [Cloudflare Free](20-cloudflare-free.md).

## TL;DR

- Интеграция тестов воркеров теперь — пакет **`@cloudflare/vitest-plugin`** (vitest ≥ 4),
  а не `@cloudflare/vitest-pool-workers`. API — Vite-плагин `cloudflareTest()`, экспорт
  `./config` и `defineWorkersConfig` исчезли.
- Типы для рантайма генерирует **`wrangler types`** → `worker-configuration.d.ts` (глобальный
  `Env`); отдельный `@cloudflare/workers-types` в tsconfig больше не нужен.
- **Хранилище DO НЕ изолируется между тестами одного файла** — состояние живёт до конца
  файла. Тесты обязаны работать со своими `task_id` и фильтровать утверждения.
- **Broadcast по гибернирующему WebSocket из другого вызова в тестовой петле не
  доставляется клиенту.** Сервер видит сокет (`getWebSockets()`), `send()` успешен —
  клиент ничего не получает. `hello` из самого upgrade-запроса и `close(1008)` из
  `webSocketMessage` доходят. Это ограничение петли: на настоящем workerd (`wrangler dev`)
  тот же код доставляет всё. Живой сокет проверять smoke-скриптом, не vitest'ом.
- `cursor.rowsWritten` у `INSERT … ON CONFLICT DO NOTHING` **не различает** «вставлено» и
  «проигнорировано» — после конфликта всё равно вернёт 1. Идемпотентность проверять
  предчтением существующих ключей (DO исполняет запросы последовательно — предчтение точно).
- Лимит **100 плейсхолдеров на statement** в DO SQLite: батч из N событий в одном
  multi-row INSERT упирается при 6N > 100.
- `SELF` из `cloudflare:test` — deprecated; заменa — `exports` из `cloudflare:workers`,
  `exports.default.fetch(input, init)`.
- Чтение attachment'а гибернирующего сокета — `deserializeAttachment()`; `serializeAttachment(x)`
  только пишет.
- **JSON-модуль с CRLF роняет workerd в crash-loop.** `import spec from "../api-spec.json"`,
  файл с `\r\n` (git на Windows без `.gitattributes`) → «Ready» и падение рантайма на
  каждый запрос без внятной ошибки в логе. LF — работает. Воспроизведено минимальным
  пробником; лечится `.gitattributes` (`* text=auto eol=lf`) в корне репозитория —
  источник закрывается, а не симптом.
- `Date.now()`/`performance.now()` **замирают во время синхронного кода** — цикл
  «до такого-то времени» в воркере бесконечен (1102). Циклить только по счётчику,
  время мерить до/после.
- API контракта воркера — `cf-worker/api-spec.json`: спека = роутер = доки = проверки
  ([ADR 0004](../decisions/0004-api-contract.md)).

## Как проверялось

Пробы — маленькие тесты с `console.log` против реального плагина (файлы удалены за
ненадобностью, вывод):

1. Повторный `POST /api/events` того же `(task_id, seq)`: `rowsWritten: 1`, в таблице одна
   строка → дедупликация на UNIQUE работает, счётчик врёт.
2. Тест B видит события теста A того же файла → изоляции хранилища нет.
3. Сокет открыт → `POST /api/events` → сервер залогировал успешный `send` → клиент 5 с
   ничего не получил. Тот же код через `wrangler dev` + `scripts/smoke-local.mjs` —
   сообщение доставлено мгновенно.

## Источники

- [Write your first test — vitest-integration](https://developers.cloudflare.com/workers/testing/vitest-integration/get-started/write-your-first-test/) —
  новый пакет и API плагина
- [Configuration — vitest-integration](https://developers.cloudflare.com/workers/testing/vitest-integration/configuration/)
- [Durable Objects — Limits](https://developers.cloudflare.com/durable-objects/platform/limits/) — 100 плейсхолдеров
