# Дельта-спека: gh secret/variable set не несёт --body-file (класс #786)

Дополняет капабилити `provider-secrets-import`, введённую
`openspec/changes/provider-secrets-import-console-safe-exit/specs/
provider-secrets-import/spec.md`. Та дельта описывала безопасную печать
отчёта; эта — независимый класс отказа в тех же двух функциях
(`set_secret`/`set_variable`), закрываемый одновременно с переносом
`scripts/lib/anthropic_oauth_import.py` (issue #840, тот же класс).

## ADDED: значение секрета/переменной идёт только через stdin, без --body-file

Требование: `set_secret(repo, name, value)` и `set_variable(repo, name,
value)` вызывают `gh secret set`/`gh variable set` БЕЗ флага `--body-file`
в argv — установленная версия `gh` (2.85.0) не знает этот флаг у данных
двух подкоманд (`unknown flag`), запись падает целиком, и код возврата
ловится как обычный сбой, неотличимый от сетевого. Обе команды и так читают
значение из stdin, если `--body`/`--body-file` не заданы вовсе —
`input=value` в `subprocess.run` достаточно.

Сценарий: подмена `subprocess.run` фиксирует переданный `args` — ни
`set_secret`, ни `set_variable` не содержат литерал `--body-file`; значение
переданного секрета отсутствует в `args`, но присутствует в `input`.

## ADDED: гвардия по исходнику на весь `scripts/`, не только на эти две функции

Требование: `scripts/lib/test_gh_body_file_guard.py` сканирует ВЕСЬ
`scripts/` (`.py` argv-списки вида `["gh", "secret"|"variable", "set", ...]`
и `.sh`/бесрасширенные bash-файлы с командой `gh secret set`/`gh variable
set ... --body-file` в одну строку) — любой НОВЫЙ код с этим дефектом
красит тест сразу, не дожидаясь живого прогона (AGENTS.md, «Починил
случай — закрой класс»). Вызовы `--body-file` у ДРУГИХ подкоманд gh
(`gh issue create`, `gh pr create` — там флаг рабочий) вне области этой
гвардии и не матчатся её регулярками.

Сценарий (мутация): временный возврат `--body-file "-"` в `set_secret`
красит `test_gh_body_file_guard.py` с точным путём и номером строки; ревёрт
— снова зелено.

## Не изменяется

Семантика `render_report`/`select_accounts`/ранжирования тиров
(`TIER_TEMPORARY`/`TIER_SUSPECT`/`TIER_DISQUALIFIED`, #777) и безопасная
печать отчёта (`provider-secrets-import-console-safe-exit`) — эта дельта
трогает только argv двух функций записи, не логику вокруг них.
