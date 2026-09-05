# Задачи: do-rows-read-quota (#320)

## Backend (cf-worker)

- [x] Индекс `tasks(status, dispatch_ts)` в SCHEMA.
- [x] Кэш `#taskCountsCache` (лениво, инвалидация записью) вместо GROUP BY
      на каждый `#status()`.
- [x] `classifyStorageError` + код ответа `storage_quota_exceeded` в общем
      catch `#fetch()`.
- [x] `alarm()` целиком в try/catch (первая строка — `setAlarm` — тоже).
- [x] Тесты: `classifyStorageError` (квота/не квота), регрессия кэша счётчиков
      по фактическим переходам статуса (мутационно проверена — снятие
      инвалидации красит тест).
- [x] `npx vitest run`, `npx tsc --noEmit`, `npm run check` — зелёные.

## Документация

- [x] `docs/research/20-cloudflare-free.md` — факт лимита rows_read + инцидент
      2026-09-03/2026-09-05, источник (письмо), новые пункты «не подтверждено».
- [x] Дельта-спека `openspec/changes/do-rows-read-quota/specs/journal-tasks-hands/spec.md`.

## Вне рамок этого change (в proposal.md, «Что вне рамок»)

- [ ] Ретеншн для `tasks` (класс #306/#305, отдельная задача при необходимости).
- [ ] Подтверждение точного текста ошибки квоты workerd на живом отказе.
