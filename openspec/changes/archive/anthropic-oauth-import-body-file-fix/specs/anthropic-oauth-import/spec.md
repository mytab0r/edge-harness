# Дельта-спека: экспорт Anthropic OAuth-учётки Claude в секрет GitHub

Новая узкая капабилити-спека для `scripts/lib/anthropic_oauth_import.py`
(файл ранее не описывался ни в одной дельте openspec — проверено
`grep -rl anthropic_oauth_import openspec/`, пусто). Мержится в
`openspec/specs/journal-tasks-hands.md` не требуется — скрипт не часть
конвейера пула/hands, это одноразовый CLI оператора (тот же класс, что
`scripts/lib/provider_secrets_import.py`).

## ADDED: экспорт из krouter-бэкапа по слотам

Требование: `--from-krouter <backup.json>` читает `providerConnections`
бэкапа, фильтрует записи с `provider == "claude"`, `authType == "oauth"`,
`isActive == true`, `isPermanentlyBanned != true`, обоими токенами
(`accessToken`, `refreshToken`) непустыми — и сортирует по возрастанию
`priority`. Первые `--max-slots` (по умолчанию 2) записываются в секреты
`ANTHROPIC_OAUTH_1`, `ANTHROPIC_OAUTH_2`, … по порядку. Ни одной подходящей
учётки — громкий отказ (`LoudError`), не пустой список секретов молча.

Сценарий: бэкап несёт 7 учёток (одна забаненная, одна неактивная, одна без
`refreshToken`, одна не-oauth, одна не-claude) → отбираются ровно 2
подходящие, в секреты уходят в порядке `priority` возрастания.

## ADDED: экспорт из одиночного credentials-файла

Требование: `--slot N --source <credentials.json>` (источник по умолчанию
`~/.claude/.credentials.json`) достаёт объект `claudeAiOauth` (либо `oauth`
как алиас) и записывает в секрет `ANTHROPIC_OAUTH_<N>`. Отсутствие файла,
отсутствие ключа `claudeAiOauth`/`oauth`, отсутствие `accessToken` или
`refreshToken` — громкий отказ с текстом, называющим ЧТО именно отсутствует
(не общее «invalid input»).

## ADDED: формат секрета — то, что читает `dsh-anthropic-pool add`

Требование: значение секрета — JSON `{"claudeAiOauth": {...}}`, где `{...}`
несёт ТОЛЬКО поля учётки (`accessToken`, `refreshToken`, опционально
`expiresAt`, `scopes`, `subscriptionType`, `rateLimitTier`) — посторонние
top-level ключи исходного файла (например `mcpOAuth`) в секрет не попадают.

## ADDED: инвариант — токены никогда не в stdout

Требование: ни при сухом прогоне, ни при `--apply` значения `accessToken`/
`refreshToken` не появляются в печатаемом отчёте — ни целиком, ни как
подстрока. Значение уходит в `gh secret set` ТОЛЬКО через stdin
(`subprocess.run(..., input=value)`), не через argv — `--body-file` не
передаётся (gh 2.85 не знает этот флаг у `gh secret set`, см. дельту
`provider-secrets-import` в этом же change, #786).

Сценарий: `--apply` с моком `set_secret` → вызов зафиксирован с реальным
токеном в `input`, но stdout не содержит ни `accessToken`, ни
`refreshToken` ни в каком виде.

## ADDED: дефолт — сухой прогон

Требование: без флага `--apply` секрет не записывается ни при одном из
двух путей (`--slot`/`--from-krouter`) — отчёт печатает, что БЫЛО бы
сделано, явно помечая «сухой прогон».
