# anthropic-oauth-import-body-file-fix: durable-скрипт экспорта Claude OAuth + закрытие класса --body-file

Задача: #840.
Дельта-спека: `specs/anthropic-oauth-import/spec.md` (новая узкая
капабилити для `scripts/lib/anthropic_oauth_import.py`) и
`specs/provider-secrets-import/spec.md` (ADDED-требование к уже
существующей капабилити `provider-secrets-import`, см.
`openspec/changes/provider-secrets-import-console-safe-exit/`).
Related: #216 (combo-router, не трогается), #838 (подключение
dsh-anthropic-oauth-pool — уже смёржено, отдельный deliverable), #786 (тот
же класс `--body-file`, объём уже открытой задачи — только один файл; эта
задача закрывает класс шире и заведена отдельно, см. issue #840 для
обоснования не-дубля).

## Класс закрываемой ошибки

Владелец написал, прогнал на живом krouter-бэкапе и проверил
`scripts/lib/anthropic_oauth_import.py` — экспорт OAuth-учёток Claude
(`providerConnections`, `provider=claude, authType=oauth`) в секреты
`ANTHROPIC_OAUTH_<slot>` для `dsh-anthropic-oauth-pool` (#838). Скрипт и
тест лежали незакоммиченными в главном дереве — этот change переносит их
как durable-артефакт, по образцу `scripts/lib/provider_secrets_import.py`.

Второй, независимый класс (#786, живая находка): установленная версия `gh`
(2.85.0) не знает флаг `--body-file` у `gh secret set`/`gh variable set`
(`unknown flag`) — оба подкоманды читают значение из **stdin**, когда
`--body`/`--body-file` не передан вовсе. `scripts/lib/
provider_secrets_import.py::set_secret`/`set_variable` несли
`--body-file "-"` в argv и падали бы на этом при повторном запуске — новый
скрипт написан сразу без этого дефекта (только stdin), а фикс закрывает
класс и во втором, уже существующем файле, плюс ставит гвардию по
исходнику на весь `scripts/`, а не только на два известных места
(AGENTS.md, «Починил случай — закрой класс»).

## Решение

1. `scripts/lib/anthropic_oauth_import.py` + `scripts/lib/
   test_anthropic_oauth_import.py` (19 тестов на фейковых токенах,
   инвариант «токены не печатаются в stdout») — перенесены дословно,
   подключают канонический `console_utf8.ensure_utf8_stdio()`
   (`BOOTSTRAP_BLOCK_SAME_DIR`, файл лежит в `scripts/lib`).
2. `scripts/lib/provider_secrets_import.py::set_secret`/`set_variable`
   больше не передают `--body-file` в argv `gh` — значение только через
   `input=` (stdin), как было задумано изначально.
3. `scripts/lib/test_gh_body_file_guard.py` — гвардия по исходнику: ни один
   вызов `gh secret set`/`gh variable set` в `.py`/`.sh`/бесрасширенных
   bash-файлах `scripts/` не несёт литерал `--body-file`. Доказана
   мутацией (см. tasks.md).
4. `docs/runbooks/refresh-anthropic-pool.md` — как обновить учётки пула из
   krouter-экспорта, почему истёкший `accessToken` из бэкапа не проблема
   (пул рефрешит по `refreshToken`), когда нужен новый экспорт krouter.
5. Оба новых теста подключены к обязательному CI через каталог гвардий
   `scripts/ci/guards/` (`anthropic-oauth-import-guard.sh`,
   `gh-body-file-guard.sh`; перебор #749). Канарейка осиротевших тестов
   (#583) видит файлы каталога как псевдо-шаги с их содержимым — гвардии
   нужна реальная `run:`-команда, а не факт существования файла теста.

## Что вне рамок

- Починка/реанимация `dsh-combo-router` (#216, заблокирован) — не
  трогается.
- Повторный импорт живых учёток в секреты — уже сделан владельцем вручную
  до этой задачи (#838); здесь только durable-код.
- Правка `dsh-ci.sh`/монтажа пула (`agent-default-model`, `dsh plugin add`)
  — область уже смёрженного `openspec/changes/anthropic-oauth-pool-
  standalone/`, не дублируется здесь.
