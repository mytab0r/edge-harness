# Задачи: storage-readiness (#575, PR #587)

## Клиентские плагины

- [x] `plugins-src/shared/describe-response-error.js` — один общий помощник
      разбора тела ответа при `!response.ok` (прод-форма
      `{"error":{"code","message"}}`, фолбэк «HTTP N»).
- [x] `integrations/build.mjs` и `plugin-manager/build.mjs` инлайнят текст
      помощника в бандлы (пакеты независимы, общего рантайм-модуля нет).
- [x] Все три точки позвали помощник: `integrations/src/body.js` (журнал),
      `plugin-manager/src/body.js` (журнал и `rpcCall`); других голых
      `HTTP N` в клиентах нет (проверено грепом по репозиторию).

## Сервер (cf-worker)

- [x] `GET /api/ready` (`#checkStorageReady`/`#getReady`): один живой
      `SELECT 1 FROM heartbeat LIMIT 1`, auth как у всех `/api/*`, отказ тем
      же `storageErrorResponse`.
- [x] Таблица `storage_probe` (одна строка id=1): исход последнего тика
      (ts/ok/detail) + персистентный дедуп-флаг `alerted`.
- [x] `#tickStorageReadyAlert` в `alarm()` — зонд каждый тик, до ветки
      «секретов нет»; алерт перехода — `storageReadyAlertDecision(ok,
      rowsWritten)` (чистая функция): решение по итогу записи флага, ни
      одного SELECT прошлого состояния.
- [x] Запись флага упала (`rows_written`) — пропуск алерта тика с громким
      логом, догоняет первый удавшийся тик.
- [x] `canary-ui.mjs` дёргает `/api/ready` после каждого деплоя (вторая,
      деплойная линия, не единственная).

## Спека и доки

- [x] Дельта-спека `specs/journal-tasks-hands/spec.md` (23.5–23.7).
- [x] `cf-worker/api-spec.json` + перегенерация `docs/api.md` /
      `src/api-spec.ts` (ADR 0004: место правды — спека, производные из неё);
      формулировка обоснования зонда сверена с реальным поведением
      `#status()` (находка ревью PR #587: «кэширован, живых запросов не
      делает» было неверно).
- [x] CI: шаг для теста помощника (гвардия осиротевших тестов #583);
      `check-plugin-compat` громко пропускает каталоги без package.json
      (`plugins-src/shared` — не плагин).

## Проверка (мутационная)

- [x] Клиент: откат на `"HTTP " + status` краснит vm-тест помощника и
      клиентский тест plugin-manager.
- [x] Пульс: срыв дедуп-гварда краснит 4 теста; откат дедупа на чтение
      прошлого исхода краснит DO-сценарии «алерт ровно один раз» и «запись
      флага невозможна — тик не падает и не спамит».
- [x] `npx vitest run` (146), `npx tsc --noEmit`, `npm run check`,
      `node --test` по трём плагин-наборам (33+26+5) — зелёные.
