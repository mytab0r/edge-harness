## Почему

Пин апстрима поднят до dsh-edge-v0.26.0 (#1636, PR #1637). Апстрим монтирует
settings-namespace `llm-pi-ai` сам — `apps/dsh-edge/src/edge-llm-settings.ts::
mountPiAiProvider` (пакет `@deepseek-ai/dsh-llm-pi-ai@0.2.0-rc.2`). Повторная
регистрация того же namespace плагином `provider-registry` падает
(`settings namespace "llm-pi-ai" is already registered`, `edge-settings.ts::
register`): плагин уходит в карантин, и весь путь Settings → Models обслуживает
апстримная схема (union из трёх протоколов), а не реестровая.

Исходный дефект #378 (мёртвые кнопки Settings → Models) закрыт апстримом —
плагин был затычкой, чьё окно закрылось; по правилу «не чини затычку» она
снимается, а не улучшается. Живой красный деплой после бампа пина
(run 38120576685) — честный сигнал этого шва.

## Что меняется

1. Запись `provider-registry` убрана из `dsh-edge/plugins.json`; исходники
   `plugins-src/provider-registry` удалены; юнит-тест шага в `repo-ci.yml` снят.
2. Релизы-ростеры `plugin-manager` и `agents-tasks` (единственные, кто вшивает
   `manifest.json`) перевыпущены с составом из 5 плагинов:
   `plugins-plugin-manager-v0.1.11`, `plugins-agents-tasks-v0.1.4`;
   `dsh-edge/plugins.json` ссылается на них с новыми sha256.
3. `dsh-edge/registry-integration/check.mjs` переведён на родной механизм:
   namespace смонтирован апстримом, protocolChoices = три протокола, directory
   готовых маршрутов больше не проверяется; вызов в деплое без второго
   аргумента.
4. Прод-канарейка деплоя проверяет namespace и штатного `deepseek-official`,
   без directory-маршрутов.
5. `docs/research/11-dsh-edge.md` («Реакция #378» и гейт-список) приведён к
   новой форме.

## Регресс (принят явно)

Directory готовых маршрутов («Add» с пречищенными строками `zhipu`/
`nvidia-nim`/`openrouter`/`deepseek`) апстрим не публикует — провайдер
добавляется вручную через «Add custom provider». Засев прод-маршрутов остаётся
за `seed-provider-registry.yml` (#1330): форма профиля апстрима
(`api`/`baseURL`/`displayName`/`models`/`apiKeyEnv`) совместима с тем, что сеет
воркфлоу.
