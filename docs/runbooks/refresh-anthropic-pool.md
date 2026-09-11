# Обновить учётки пула Claude из krouter-экспорта

Кому: владельцу. Когда: `dsh-anthropic-oauth-pool` (#838) отдаёт `401`/
`refresh failed` на всех аккаунтах, либо просто пора обновить набор
учёток пула из свежего экспорта krouter.

## Что это и что не это

Этот раздел — про **сам пул** (`dsh-anthropic-oauth-pool`, секреты
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`, независимая проводка #838). Это
**не** про `vars.DEEPSEEK_*`/`DSH_PROVIDER_CHAIN` — та цепочка описана в
[`switch-llm-provider.md`](switch-llm-provider.md) и обслуживает другой,
OpenAI-совместимый протокол (`api: openai-completions`). Пул Claude
регистрируется в `llm-pi-ai` со своим протоколом (`api: anthropic-messages`)
и пробуется первым; при отказе всех его аккаунтов раннер откатывается на
цепочку `DEEPSEEK_*` без изменения её собственного поведения. Пул смонтирован
только в `worker.yml`/`hands.yml` — в `ai-review.yml` его нет структурно
(#860, решение владельца 2026-09-10: недельная квота Claude — ресурс
разработки, дневная GLM/ZAI — ревью; детали в
[`switch-llm-provider.md`](switch-llm-provider.md), раздел про пул).

## Источник данных

Секреты `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` несут JSON вида
`{"claudeAiOauth": {"accessToken": ..., "refreshToken": ..., "expiresAt": ...,
"scopes": [...], "subscriptionType": ...}}` — ровно формат, который читает
`dsh-anthropic-pool add` (`lib/accounts.js` плагина). Заполняются одним из
двух путей:

1. **Из krouter-бэкапа** (массово, обе учётки за один прогон) — берёт живые
   OAuth-учётки Claude (`provider: claude`, `authType: oauth`, `isActive`) из
   `providerConnections` бэкапа роутера учёток владельца, по возрастанию
   `priority`, до `--max-slots` штук:

   ```bash
   python scripts/lib/anthropic_oauth_import.py --from-krouter <путь-к-krouter-backup.json> --apply
   ```

   Без `--apply` — сухой прогон: печатает, что нашёл и куда записал бы, ни
   одного секрета не трогает.

2. **Из одиночного credentials.json** (один аккаунт за раз, например второй
   логин Claude Code на другой машине):

   ```bash
   python scripts/lib/anthropic_oauth_import.py --slot 2 --source /путь/credentials.json --apply
   ```

Оба пути — тот же инвариант, что и у `provider_secrets_import.py`: значение
токена никогда не печатается и не проходит через argv (`gh secret set`
кормится через stdin), путь к файлу-источнику — только через флаг/аргумент,
не литерал в коде.

## Почему accessToken из бэкапа может быть уже мёртв — это нормально

`accessToken` в Claude OAuth живёт часы, `refreshToken` — недели. krouter-
бэкап обычно снят раньше, чем используется, поэтому `accessToken` в нём,
скорее всего, УЖЕ истёк на момент импорта. Это не ошибка и не повод не
импортировать: `dsh-anthropic-oauth-pool` сам рефрешит доступ по
`refreshToken` при первом использовании аккаунта (`lib/pool.js`/
`lib/accounts.js` плагина). Обязателен именно `refreshToken` — скрипт валит
импорт (`LoudError`), если его нет, чтобы не положить в секрет заведомо
нерабочий аккаунт (плагин отверг бы его сам: «Source has no usable
claudeAiOauth credentials»).

**Если рефреш тоже не проходит** (`refreshToken` протух — типичный срок
жизни недели, не месяцы) — единственное лечение: свежий экспорт krouter
(переавторизовать аккаунт в krouter, снять новый бэкап) и повторный прогон
команды выше. Автоматического продления `refreshToken` без нового логина не
существует.

## Проверка видимого результата, не зелёного шага

```bash
gh secret list --repo mytab0r/edge-harness | grep ANTHROPIC_OAUTH_
```

— секреты должны обновить `Updated` (не создаться заново — GitHub не
показывает значения, только факт и дату записи). Дальше — запустить
воркер/ai-review и убедиться, что вызов реально прошёл через пул: событие
`agent_answer` журнала несёт `provider`/`model`, а не сразу откат на цепочку
`DEEPSEEK_*` (см. `switch-llm-provider.md`, «Область охвата»).

## Имена секретов, не значения

`ANTHROPIC_OAUTH_1`, `ANTHROPIC_OAUTH_2` — GitHub Actions secrets этого
репозитория. Значения не попадают ни в репозиторий, ни в документацию, ни в
логи (AGENTS.md, «Секреты» — репозиторий публичный).
