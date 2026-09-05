# Задачи: do-sqlite-retention (#306, упоминает #305)

## Backend (cf-worker)

- [x] `RETENTION` в `src/config.ts` — пороги (`batchSize`, `eventsMaxAgeMs`,
      `tasksMaxAgeMs`, `backlogStreakThreshold`), одно место правды.
- [x] `RETENTION_TABLES` в `src/harness.ts` — конфигурация вместо кода на
      каждую таблицу; `#pruneRetention` вызывается из уже существующего
      `alarm()`, до ветки «секретов нет».
- [x] Индексы `events_by_ts` и `tasks_status_created` в `SCHEMA` — пакетное
      удаление по индексу, не полный скан.
- [x] `retentionBacklog()` — чистая функция порога «не успеваем», тот же приём,
      что `pulseHealthy`/`pulseStale`.
- [x] `Status.retention` — новое поле, предвычислено сервером; видимость через
      существующий бейдж-канал (`#retention` в морде, по образцу
      `#watchdog`/`#pulse`), не новый механизм оповещения.
- [x] Тесты: `retentionBacklog` (граница порога), интеграционные — старые
      события уходят/свежие остаются, пачка ограничена `batchSize` (мутационно
      доказано: снятие `LIMIT` красит тест), терминальные задачи чистятся,
      активные (`queued`/`dispatched`/`running`) — нет независимо от возраста
      (мутационно доказано: снятие фильтра по `status` красит тест),
      `/api/status.retention` виден после тика.
- [x] `npx vitest run`, `npx tsc --noEmit`, `npm run check` — зелёные.

## Документация

- [x] `docs/research/20-cloudflare-free.md` — раздел «DO SQLite»: фактические/
      оценённые размеры таблиц на 2026-09-05, обоснование ретеншена как защиты
      горизонта (не только острого инцидента #320); новый пункт «не
      подтверждено» (нет прямого доступа для чтения таблиц DO SQLite).
- [x] Дельта-спека `specs/journal-tasks-hands/spec.md`.

## Вне рамок этого change (в proposal.md, «Что вне рамок»)

- [ ] Ретеншн для `messages` (#305) — таблицы нет в `main` (PR #173 не слит).
- [ ] Подключение `retention.backlog` к `pulse_guard.escalate` — вероятно #324.
