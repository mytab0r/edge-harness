# Claude OAuth access-токен как провайдер DSH: нативно или через плагин

> Исследовано 2026-09-10. Источники — живой код tarball'ов `@deepseek-ai/dsh-llm-pi-ai` и `@earendil-works/pi-ai` (версия из CI и последняя на npm), код плагина `dsh-anthropic-oauth-pool`, живой прогон Bearer-запроса к api.anthropic.com.

## TL;DR
- **Нативно можно, без loopback-прокси плагина.** `@earendil-works/pi-ai` (транспорт `dsh-llm-pi-ai`) сам детектит OAuth access-токен по подстроке `sk-ant-oat` в `apiKey` и переключает Anthropic-протокол на `Authorization: Bearer` + автоматически ставит оба беты (`claude-code-20250219`, `oauth-2025-04-20`).
- Подтверждено на версии, что реально ставит CI: `DSH_VERSION=0.1.1-rc.2` (`scripts/lib/dsh-ci.sh:17`) → `dsh-llm-pi-ai@^0.1.1-rc.2` → `@earendil-works/pi-ai@^0.82.1`. Логика в `dist/api/anthropic-messages.js` (0.82.1 строки 636-679; 0.85.1 — 688-723), идентична.
- Минимальный конфиг (без прокси, без плагина): `llm-pi-ai.providers.anthropic.apiKeyEnv: ANTHROPIC_OAUTH_TOKEN` + `agent-default-model: {provider: anthropic, model: claude-sonnet-4-5}`. Каталог моделей зашит в pi-ai (`dist/providers/data/anthropic.json`), `api`/`baseURL` берутся из встроенного каталога route `anthropic`.
- Значение резолвится через `ctx.credentials` ИЛИ прямо из env процесса (`dsh-llm-pi-ai/lib/index.js:2408-2414`) — обычный GitHub Actions secret с именем из `apiKeyEnv` работает.
- **Рекомендация:** для одного долгоживущего access-токена без ротации — нативный путь строго проще (нет loopback-сервера, нет лишнего процесса, нет класса хрупкости плагина). Плагин `dsh-anthropic-oauth-pool` нужен ТОЛЬКО ради того, что он даёт сверх голого моста: ротация нескольких аккаунтов, cooldown на 401/403/429, `/api/anthropic-pool/status`. Это решения ДВУХ разных задач, обе используют один и тот же нативный Bearer-детект pi-ai.
- **TTL access-токена — не подтверждено кодом:** ни pi-ai, ни плагин его не хардкодят, `expires_in` приходит от сервера. Наблюдение `expiresIn: 28800` (8ч) из krouter-дампа — эмпирика одного дампа, «около года» от владельца тоже не проверено.

## Ключевой код
`@earendil-works/pi-ai/dist/api/anthropic-messages.js` (0.82.1): `isOAuthToken(apiKey)=apiKey.includes("sk-ant-oat")` (636-638); ветка Bearer (665-680) создаёт `new Anthropic({apiKey:null, authToken:apiKey, defaultHeaders:{... "anthropic-beta":["claude-code-20250219","oauth-2025-04-20",...]}})`. Путь значения: `dsh-llm-pi-ai/lib/index.js:2408-2414` `resolveApiKey` → `PiAiAdapter` `options.apiKey` → `createClient()`. Поле кастомных заголовков `headers?` есть (`config.d.ts:112-113`), но для OAuth не нужно — беты ставятся автоматически.

## Внешние решения
LiteLLM — нет обработки `sk-ant-oat` (и отвергнут отдельно, Python не встаёт на Workers). `claude-code-proxy`/`claude-code-router` решают ОБРАТНУЮ задачу (увести Claude Code на других провайдеров). Специализированного «внешний сервис берёт готовый Claude OAuth токен провайдером» не нашлось. Обзор не исчерпывающий (точечные запросы).

## Не подтверждено
TTL токена; нужен ли `x-anthropic-billing-header` (плагин ставит, pi-ai нет; живой Bearer+betas без него давал 200 на /messages,/models, но стриминг/tool-calls отдельно не проверялись); полнота обзора внешних решений; валидация формата в `assertUsableApiKey()`.

## Дополнение 2026-09-13 (#1097): почему self-регистрация плагина никогда не работала в headless

Живой инцидент — плагин `dsh-anthropic-oauth-pool` отказывал на КАЖДОМ прогоне
`worker.yml`/`hands.yml` за ~1с: `TypeError: Cannot read properties of undefined
(reading 'update')` в `lib/index.js::ensureProvider`. Причина установлена
разборкой `dsh-anthropic-oauth-pool-0.1.0.tgz` (релиз `dsh-plugins-suite-v1`) —
`ensureProvider()` пытается прописать себя в `llm-pi-ai` через
`ctx.get('settings').update('llm-pi-ai', {...})` (Cordis service `settings`,
пакет `@deepseek-ai/dsh-settings`). Этот сервис **никогда не смонтирован в
профиле `headless`**: `@deepseek-ai/dsh-headless/cordis.patch.yml` явно
описывает себя как «no Host, HTTP server, Web runtime, or browser plugin», и
ни один пакет, реально идущий в headless-профиль (`@deepseek-ai/dsh`,
`dsh-headless`, `dsh-code-runtime-worker-thread`), не тянет
`@deepseek-ai/dsh-settings` как рантайм-зависимость — только как devDependency
`@deepseek-ai/dsh` и peerDependency `dsh-agent-default-model`, обе бездействуют
без явной composition-строки. `ctx.get('settings')` в headless возвращает
`undefined` детерминированно, а не по версии/дрейфу зависимостей: `DSH_VERSION`
(`0.1.1-rc.2`) не менялся с 2026-08-30, integrity-хэши `@deepseek-ai/dsh` и
`@deepseek-ai/dsh-headless` совпали байт-в-байт с закреплёнными в
`scripts/lib/dsh-ci.sh` при повторном `npm pack` 2026-09-13.

HTTP-прокси плагина (аутентификация, ротация аккаунтов, failover — сама ценность
плагина сверх голого нативного моста из TL;DR выше) при этом стартует и
работает нормально: `ensureProvider()` вызывается АСИНХРОННО после
`server.listen`, её отказ ловится `.catch()` внутри плагина и не роняет процесс
— ломается только регистрация в `llm-pi-ai`.

Фикс (`scripts/lib/dsh-ci.sh::_dsh_patch_profile_anthropic_pool`) — статическая
регистрация `llm-pi-ai.providers.anthropic-pool` в `cordis.patch.yml`, тем же
путём, что уже работает у combo-router (`dsh_patch_profile`), в обход
сломанного settings-сервиса; порт прокси зафиксирован (`DSH_ANTHROPIC_POOL_PORT`)
— иначе `baseURL` нельзя узнать до старта процесса, выбирающего порт сам.

## Поправка 2026-09-13 (#1130): предыдущее дополнение (#1097) ошибочно утверждало отсутствие settings в headless

Живой прогон worker.yml **34753001158** (после мержа фикса #1097/#1099) сменил
ошибку с `NO_ADAPTER` на `dsh: UNKNOWN_MODEL: pi-ai provider "anthropic-pool"
has no configured model "claude-sonnet-4-5"`. Это доказывает: провайдер
регистрируется (фикс #1097 работает), но что-то ПЕРЕЗАПИСЫВАЕТ список
моделей.

Разбор нашёл ошибку в предыдущем дополнении: **сервис `settings`
(`@deepseek-ai/dsh-settings-file`) ДЕЙСТВИТЕЛЬНО смонтирован в headless** —
живой `dsh --profile headless --dump-config` (те же `@deepseek-ai/dsh@0.1.1-rc.2`
+ `@deepseek-ai/dsh-headless@0.1.1-rc.2`, установлены СПОСОБОМ `dsh_install`)
показывает строку `- id: settings / name: '@deepseek-ai/dsh-settings-file'` в
композиции `dsh-base`, на которой стоят все профили кроме `sdk-minimal`
(research/10, §9). Предыдущий вывод («settings никогда не монтируется в
headless») был неполным: он верно описал, что `@deepseek-ai/dsh-headless`
САМ не тянет `dsh-settings`, но не учёл, что `dsh-base` (нижний, разделяемый
слой) тянет `dsh-settings-file` через СВОЮ собственную цепочку зависимостей
— настоящую причину исходного `TypeError` (первого захода #1097) это делает
скорее гонкой (`ensureProvider()` вызывается асинхронно из
`server.listen()`, до того как фибер settings успел стать готовым), чем
структурным отсутствием сервиса — но само это уже не важно для второго
инцидента: раз `settings` реально доступен, self-регистрация плагина
(`ensureProvider()`) продолжает выполняться ПАРАЛЛЕЛЬНО нашей статической
регистрации (#1097), и её `settings.update('llm-pi-ai', {providers: {...}})`
— MERGE-патч (`@deepseek-ai/dsh-settings::mergeLayers`): объекты сливаются
рекурсивно, но МАССИВЫ заменяются ЦЕЛИКОМ. `ensureProvider()` СНАЧАЛА зовёт
`discoverModels()` — реальный `GET /v1/models` с реальными аккаунтами (в CI
секреты `ANTHROPIC_OAUTH_1/2` настоящие) — и если этот каталог не содержит
буквального id `claude-sonnet-4-5` (наш статический дефолт, он же дефолт
самого плагина), поздняя запись плагина стирает наш массив `models`, и
следующий резолв модели агентом получает `UNKNOWN_MODEL`.

Подтверждено локально (та же версия `0.1.1-rc.2`, DSH_HOME изолирован от
реального `$HOME` — Node's `os.homedir()` на Windows читает `USERPROFILE`,
не `HOME`, поэтому нужен именно `DSH_HOME`): с СИНТЕТИЧЕСКИМ (пустым) пулом
аккаунтов `discoverModels()` возвращает рано (`if (!account) return`) —
self-запись плагина никогда не происходит, гонка не воспроизводится (это и
объясняет, почему первый локальный прогон #1097/#1099 её не поймал). Фикс
#1130 — нейтрализация `ensureProvider()` целиком точечным патчем
(`scripts/lib/patch_anthropic_pool_plugin.py`, exact string match, fail
loud при несовпадении формы), применяемым к распакованному плагину ПОСЛЕ
проверки sha256; результат репакуется и монтируется вместо оригинального
ассета. Наша статическая регистрация (#1097) остаётся ЕДИНСТВЕННЫМ
источником правды — плагину больше нечего писать в settings, гонка снята у
корня, а не подавлена совпадением id.

## Дополнение 2026-09-13 (#1130): превентивный рефреш путает «срок неизвестен» со «срок истёк»

Вопрос владельца: «Какой опус он там пытается рефрешить, если мы поставили
долгоживущие OAuth токены, которые не надо рефрешить?» Разбор
`lib/pool.js::createRefreshCoordinator` (тот же релиз плагина): условие
пропуска превентивного рефреша — `if (oauth.expiresAt && oauth.expiresAt -
Date.now() > REFRESH_SKEW_MS) return account` (`REFRESH_SKEW_MS = 5 * 60 *
1000`). Условие ложно и когда `expiresAt` ОТСУТСТВУЕТ, и когда оно в
прошлом — плагин не различает «поле не задано» (долгоживущий accessToken
без явного срока) и «поле задано и истекло». Проверяется на КАЖДЫЙ запрос
через прокси (`lib/index.js::forward`/`discoverModels`), не один раз при
старте.

Плагин НЕ имел ветки «получили 401/403 от Anthropic → рефрешим и повторяем
тот же запрос» (`forward()`, ветка `if ([401, 403].includes(response.status))`
— только `cooldownUntil` + переход к следующему аккаунту) — это означает,
что отключение превентивного рефреша БЕЗ добавления реактивного сломало бы
восстановление реально истёкшего токена без `expiresAt`. Фикс #1130 —
патч в двух местах разом (см. `openspec/changes/anthropic-oauth-pool-standalone/design.md`,
раздел «Превентивный рефреш долгоживущих токенов»): `pool.js` перестаёт
считать отсутствие `expiresAt` признаком истечения, `index.js` получает
реактивный рефреш-и-повтор на реальный 401/403 (метит аккаунт на диске как
просроченный и зовёт `ensureFresh()` снова — переиспользует существующую
логику рефреша, не дублирует её).

## Источники
`npm pack @deepseek-ai/dsh-llm-pi-ai@0.1.1-rc.2` / `@earendil-works/pi-ai@0.82.1`; `scripts/lib/dsh-ci.sh:17,19`; плагин `dsh-anthropic-oauth-pool-0.1.0.tgz`; github 1rgs/claude-code-proxy, musistudio/claude-code-router, BerriAI/litellm (WebFetch 2026-09-10); дополнение 2026-09-13 (#1097) — `npm pack @deepseek-ai/dsh@0.1.1-rc.2 @deepseek-ai/dsh-headless@0.1.1-rc.2 @deepseek-ai/dsh-settings@0.1.1-rc.2 @deepseek-ai/dsh-code-runtime-worker-thread@0.1.1-rc.2 @deepseek-ai/dsh-agent-default-model@0.1.1-rc.2`, релиз `dsh-plugins-suite-v1` (issue #1097); поправка 2026-09-13 (#1130) — живой `npm install -g` того же набора tarball'ов + `dsh --profile headless --dump-config` (подтвердил монтаж `dsh-settings-file`), `@earendil-works/pi-ai@0.82.1` `dist/models.js` (`createProvider`/`Models.getModel`), `@deepseek-ai/dsh-settings@0.1.1-rc.2` `lib/index.js` (`mergeLayers`), живой прогон worker.yml 34753001158; дополнение 2026-09-13 (#1130, превентивный рефреш) — `lib/pool.js`/`lib/index.js` плагина (релиз `dsh-plugins-suite-v1`), живой node-тест `createRefreshCoordinator` с синтетическими `readAccount`/`writeAccount`/`refreshToken` (`scripts/lib/test/dsh-anthropic-pool.guard.sh`, секция 14).
