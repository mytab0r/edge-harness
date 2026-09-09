# Anthropic OAuth Pool как независимый быстрый провайдер (без combo-router)

Задача: #838.

Design: [design.md](design.md).

## Summary

Владелец хочет использовать подписку Claude как быстрый провайдер для
ai-review/воркера/рук через плагин `dsh-anthropic-oauth-pool`. Сегодня этот
плагин монтируется ТОЛЬКО как часть suite ротации учёток
(`vars.PLUGINS_SUITE_URL`), а suite снята (#790: `NO_COMBO_ROUTE` роняет job
без отката) — вместе со сломанным `dsh-combo-router` она утянула за собой и
рабочий `dsh-anthropic-oauth-pool`. Этот change монтирует
`dsh-anthropic-oauth-pool` НЕЗАВИСИМО от `vars.PLUGINS_SUITE_URL`/combo-router
— тем же уже опубликованным релизным ассетом
(`dsh-anthropic-oauth-pool-0.1.0.tgz`, релиз `dsh-plugins-suite-v1`), не
трогая и не реанимируя `dsh-combo-router` (#216 остаётся заблокированной и не
переоткрывается).

## Problem / Motivation

- `vars.PLUGINS_SUITE_URL` не задана (снята при разборе #790) — сегодня
  `dsh-anthropic-oauth-pool` не монтируется вообще ни в одном из трёх
  каналов (`worker.yml`/`hands.yml`/`ai-review.yml`), хотя плагин
  самодостаточен и не зависит от `dsh-combo-router`: свой loopback-прокси
  `127.0.0.1`, Anthropic-совместимый (`api: anthropic-messages`), least-used
  балансировка между аккаунтами, авто-рефреш OAuth, failover 429/401/403
  ВНУТРИ одного вызова (`README.md` плагина, `lib/index.js::forward`).
- Существующая проводка (`scripts/lib/dsh-ci.sh::dsh_install_plugins_suite`/
  `dsh_mount_plugins_suite`) монтирует ОБА плагина suite одной функцией и
  гейтится ОДНОЙ переменной — самостоятельно включить только рабочую
  половину (oauth-pool) без сломанной (combo-router) сегодня нельзя.
- `DSH_PROVIDER_CHAIN`/манифест использования (`config/provider-usage.json`,
  #823) устроены вокруг OpenAI-completions-совместимых эндпоинтов
  (`base_url`+`secret_env`+`model`, читает `llm-deepseek` через
  `DEEPSEEK_BASE_URL`/`DEEPSEEK_API_KEY`) — пул регистрирует себя в
  `llm-pi-ai.providers` с `api: anthropic-messages` (`lib/index.js::
  ensureProvider`), другим протоколом. Прямая подстановка пула как ЭЛЕМЕНТА
  цепочки (запись `{base_url: "http://127.0.0.1:<port>", ...}`) не сработает:
  `llm-deepseek`-адаптер шлёт OpenAI-формат запросов на Anthropic-эндпоинт.
  Решение и обоснование — design.md, «Стык с цепочкой провайдеров».

## Scope

### In

- `scripts/lib/dsh-ci.sh`: `dsh_install_anthropic_pool`/
  `dsh_mount_anthropic_pool`/`dsh_import_anthropic_accounts` — независимые
  от `dsh_install_plugins_suite`/`dsh_mount_plugins_suite`, гейтятся
  наличием секретов аккаунтов (не `vars.PLUGINS_SUITE_URL`).
- Импорт аккаунтов из секретов `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`
  (JSON `{"claudeAiOauth": {"accessToken", "refreshToken", ...}}` целиком) —
  временный файл mode 0600, `node bin/dsh-anthropic-pool.js add <id> <файл>`.
- Выбор провайдера: `agent-default-model: {provider: anthropic-pool, model:
  claude-sonnet-4-5}` — пул пробуется ПЕРВЫМ, при отказе — существующая
  `dsh_run_with_provider_chain`/манифест использования (#823), без изменения
  её поведения при выключенном пуле.
- Проводка `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` в env шагов DSH
  `worker.yml`/`hands.yml`/`ai-review.yml`.
- Тест-гвардия (`scripts/lib/test/`), доказанная мутацией.

### Out

- Починка/реанимация `dsh-combo-router` (#216, `blocked`) — не трогается.
- Газ для `NO_COMBO_ROUTE` (#790) — про suite, не про эту независимую
  проводку.
- UI выбора аккаунтов/статуса пула в морде (плагин несёт свой `client.js`,
  но морда вне этого change).
- Живой прогон с реальными аккаунтами — блокирован секретами, которые
  кладёт владелец (см. tasks.md, «Закрывающая проверка»).

## Acceptance criteria

- `dsh --dump-config` профиля `headless` после монтажа несёт
  `- id: anthropic-oauth-pool` НЕЗАВИСИМО от того, задана ли
  `vars.PLUGINS_SUITE_URL`.
- Ни один из `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` не задан → пул не
  подключается, поведение (одиночный провайдер/цепочка) не меняется —
  доказано гвардией, не только прозой.
- Импорт секрета кладёт `~/.dsh/anthropic-accounts/<id>.json` с полем
  `claudeAiOauth.{accessToken,refreshToken}` — доказано гвардией на
  фейковом JSON.
- Отказ пула не ломает атрибуцию `DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED`
  цепочки-фоллбэка — доказано гвардией.
- Значений секретов нет ни в коде, ни в тестах, ни в этом change —
  только имена переменных (AGENTS.md, «Секреты»).

## References

- `scripts/lib/dsh-ci.sh:22-27,145-244` — suite ротации, откуда переиспользуется
  ассет `PLUGINS_SUITE_OAUTH_ASSET` и паттерн скачивания+sha256.
- Релиз `dsh-plugins-suite-v1` (ассет `dsh-anthropic-oauth-pool-0.1.0.tgz` +
  `.sha256`), инспектирован живьём 2026-09-09: `lib/index.js`,
  `lib/accounts.js`, `lib/pool.js`, `bin/dsh-anthropic-pool.js`, `README.md`.
- Issue #790 — `NO_COMBO_ROUTE` без газа (suite, не эта задача).
- Issue #216 — полноценный combo-router, заблокирован, не трогается здесь.
- `docs/runbooks/switch-llm-provider.md` — цепочка провайдеров, манифест
  использования (#823).
- `docs/research/10-dsh-architecture.md:293-344` — `llm-pi-ai`/
  `agent-default-model` контракт (`provider: <route-id>` из
  `llm-pi-ai.providers.<route-id>`).
