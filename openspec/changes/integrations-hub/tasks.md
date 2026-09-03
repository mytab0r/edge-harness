# Задачи: integrations-hub (#115)

- [x] Реестр `dsh-edge/integrations.json` + форма и CLI-гвардия проводки (`dsh-edge/integrations.mjs`)
- [x] Серверная половина: `server/core.js` (чистая логика, scrub) + `server/index.js` (jira_issue, confluence_page, bitbucket_pr, slack_post)
- [x] Клиентская половина: секция Settings → Интеграции (слот, словари en/zh/ru, статусы журнала) + `build.mjs` с гвардией «реестр ↔ тулы»
- [x] Общий транспорт статусов: `scripts/lib/journal_status.sh` + обёртки `plugin_status.sh` / `integration_status.sh` + smoke на заглушках
- [x] Деплой: синк секретов интеграций в воркер + `integration_status` (ready/not_configured/failed) в журнал
- [x] repo-ci: гвардия реестра, тесты core/client, bash -n scripts/plugins, smoke обёрток
- [x] ADR 0010 (REST-инструменты vs MCP), README плагина, дельта-спека журнала
- [ ] Пост-мерж: деплой морды с плагином (релиз + sha256 в `dsh-edge/plugins.json` до PR), в журнале — `integration_status` по всем интеграциям реестра, секция «Интеграции» в морде живая
- [ ] Владелец: добавить секреты Jira/Confluence/Bitbucket/Slack → следующий деплой переводит интеграции в `ready`; живой прогон сквозного сценария (jira_issue → runner_task → bitbucket_pr → slack_post)

Замечание: два последних пункта — вне диффа. Пункт про релиз выполняется
воркером до открытия PR (без него деплой не соберёт плагин), пункт про
секреты — только владелец: значения существуют вне хранилища репозитория.
