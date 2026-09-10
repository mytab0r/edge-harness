# Дельта-спека: быстрый провайдер Claude anthropic-oauth-pool (#838)

Дополняет suite ротации учёток (`dsh-in-job`, «ADDED: Suite ротации учёток
combo-router + anthropic-oauth-pool») НЕЗАВИСИМЫМ путём монтажа того же
плагина `dsh-anthropic-oauth-pool`, не затрагивая `dsh-combo-router` (#216,
заблокирован) и не реанимируя `vars.PLUGINS_SUITE_URL` (снята при #790).

## ADDED: Монтаж anthropic-oauth-pool независимо от suite

Требование: `scripts/lib/dsh-ci.sh::dsh_install_anthropic_pool` скачивает и
проверяет sha256 релизного ассета `dsh-anthropic-oauth-pool-0.1.0.tgz`
(пин тега релиза `ANTHROPIC_OAUTH_POOL_RELEASE`, литерал в коде, независимый
от `vars.PLUGINS_SUITE_URL`) НЕ дожидаясь и не проверяя состояние suite.
Гейт активации — наличие хотя бы одного из секретов
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`; ни один не задан → скачивание не
происходит, `DSH_ANTHROPIC_POOL_ACTIVE=0`, поведение не меняется.

Требование: `dsh_mount_anthropic_pool` монтирует плагин (`dsh plugin add`)
и структурно подтверждает монтаж строкой `- id: anthropic-oauth-pool` в
`dsh --dump-config` — тем же способом, что уже доказан для suite
(`dsh_mount_plugins_suite`). Сбой скачивания/sha256/монтажа — fail loud.

## ADDED: Импорт аккаунтов из секретов

Требование: `dsh_import_anthropic_accounts` пишет значение каждого заданного
секрета (JSON `{"claudeAiOauth": {"accessToken", "refreshToken", ...}}`) во
временный файл mode 0600 и вызывает `node <extracted>/bin/dsh-anthropic-pool.js
add anthropic-N <файл>` (прод-код плагина, не переписанный). Файл удаляется
сразу после вызова.

Требование: секретная переменная окружения удаляется (`unset`) сразу после
использования, ДО первого запуска `dsh` в этом job'е. Причина: имена
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` не подпадают под паттерн
`*_KEY`/`*_TOKEN`/`*_SECRET`, которым DSH вырезает переменные окружения из
подпроцессов, запускаемых shell-тулом модели — без явного `unset` живой
OAuth JSON был бы виден агенту `ai-review`, работающему над недоверенным
диффом PR без `GH_TOKEN` (доверенная граница задачи #18).

## ADDED: Выбор провайдера — пул первым, цепочка фоллбэком

Требование: `dsh_run_with_pool_then_chain` — при активном пуле патчит
профиль (`agent-default-model: {provider: anthropic-pool, model:
claude-sonnet-4-5}`) и делает ОДИН прогон через `dsh_run_with_retry` (пул
несёт собственный failover между аккаунтами внутри одного HTTP-вызова).
Успех → `DSH_CHAIN_PROVIDER=anthropic-oauth-pool`, штатная
`dsh_run_with_provider_chain` не вызывается. Отказ → `::warning::` и вызов
`dsh_run_with_provider_chain` как есть, с добавлением `anthropic-oauth-pool`
первым элементом в `DSH_CHAIN_TRIED`. Пул неактивен → сразу
`dsh_run_with_provider_chain`, поведение идентично состоянию до этого change.

Не входит: комбинация пула и `DSH_PROVIDER_CHAIN`/манифеста использования
(#823) НА ОДНОМ уровне (как элемент одного и того же массива провайдеров) —
протокольно несовместимо (`anthropic-messages` vs `openai-completions`, см.
design.md), пул и цепочка остаются двумя последовательными, а не
объединёнными механизмами.

## ADDED: Изоляция битого секрета аккаунта (#859)

Требование: `dsh_import_anthropic_accounts` обрабатывает каждый заданный
секрет НЕЗАВИСИМО — снимает ведущий UTF-8 BOM, если он есть, и валидирует
JSON/обязательные поля (`claudeAiOauth.accessToken`/`refreshToken`) ДО
вызова `node ... add`. Секрет не проходит проверку (BOM после снятия не
делает JSON валидным, JSON битый, поля отсутствуют/пустые, либо сам `node
... add` всё же отказал) → аккаунт пропускается с `::warning::`, секрет
удаляется из окружения (`unset`), обработка ОСТАЛЬНЫХ секретов продолжается
без прерывания.

Требование: если по итогу обработки не импортирован ни один аккаунт, пул
отключает себя для этого прогона (`DSH_ANTHROPIC_POOL_ACTIVE=0`) и функция
возвращает 0 (не `1`) — `dsh_run_with_pool_then_chain` в этом случае идёт
сразу на `dsh_run_with_provider_chain`, как если бы ни один секрет не был
задан вовсе. Функция никогда не возвращает `1` из-за содержимого секрета —
только из-за структурных проблем окружения, которых у этой функции нет
(единственный сбой, который распространяется наружу, — сбой в
`dsh_install_anthropic_pool`/`dsh_mount_anthropic_pool` до/после неё).

## ADDED: Проводка секретов в трёх каналах

Требование: `.github/workflows/worker.yml`, `hands.yml`, `ai-review.yml`
передают `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` из `secrets.*` в env шага,
вызывающего DSH — тем же местом, где уже проброшены `PLUGINS_SUITE_URL` и
ключи `DSH_PROVIDER_CHAIN`.
