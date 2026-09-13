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

## Источники
`npm pack @deepseek-ai/dsh-llm-pi-ai@0.1.1-rc.2` / `@earendil-works/pi-ai@0.82.1`; `scripts/lib/dsh-ci.sh:17,19`; плагин `dsh-anthropic-oauth-pool-0.1.0.tgz`; github 1rgs/claude-code-proxy, musistudio/claude-code-router, BerriAI/litellm (WebFetch 2026-09-10).
