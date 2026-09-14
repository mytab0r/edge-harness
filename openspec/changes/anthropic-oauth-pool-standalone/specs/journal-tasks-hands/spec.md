# Дельта-спека: быстрый провайдер Claude anthropic-oauth-pool (#838)

Дополняет suite ротации учёток (`dsh-in-job`, «ADDED: Suite ротации учёток
combo-router + anthropic-oauth-pool») путём монтажа того же плагина
`dsh-anthropic-oauth-pool`, не затрагивая `dsh-combo-router` (#216,
заблокирован) и не реанимируя `vars.PLUGINS_SUITE_URL` (снята при #790).
Пути монтажа независимы В ЧАСТИ СКАЧИВАНИЯ/ПРОВЕРКИ (#838) — но НЕ в части
самого монтажа: с #1130 `dsh_mount_plugins_suite` спрашивает состояние
standalone-пути (`DSH_ANTHROPIC_POOL_ACTIVE`) перед своим `dsh plugin add`
для того же плагина (см. «ADDED (#1130): Патч плагина перед монтажом»
ниже) — иначе смонтировались бы две копии одного плагина, одна из них в
обход патча #1130.

## ADDED: Монтаж anthropic-oauth-pool независимо от suite

Требование: `scripts/lib/dsh-ci.sh::dsh_install_anthropic_pool` скачивает и
проверяет sha256 релизного ассета `dsh-anthropic-oauth-pool-0.1.0.tgz`
(пин тега релиза `ANTHROPIC_OAUTH_POOL_RELEASE`, литерал в коде, независимый
от `vars.PLUGINS_SUITE_URL`) НЕ дожидаясь и не проверяя состояние suite.
Гейт активации — наличие хотя бы одного из секретов
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`; ни один не задан → скачивание не
происходит, `DSH_ANTHROPIC_POOL_ACTIVE=0`, поведение не меняется.

Требование (MODIFIED, #1130): `dsh_mount_anthropic_pool` монтирует НЕ
оригинальный скачанный ассет, а РЕЗУЛЬТАТ обязательного патч-шага
(`dsh_patch_anthropic_pool_plugin`, следующая секция) — репак патченного
`package/`. sha256-проверка из `dsh_install_anthropic_pool` остаётся
привязана к ОРИГИНАЛЬНОМУ (ещё не патченному) скачанному файлу — это якорь
целостности загрузки, а не якорь монтируемого артефакта; монтируемый
артефакт — локально пересобранный tgz, у которого своей sha256-проверки
нет и не может быть (содержимое меняется патчем намеренно). Структурное
подтверждение монтажа — строка `- id: anthropic-oauth-pool` в
`dsh --dump-config`, тем же способом, что уже доказан для suite
(`dsh_mount_plugins_suite`). Сбой скачивания/sha256 оригинала, сбой
патч-шага или сбой монтажа — fail loud, в каждом случае с разным
сообщением, называющим именно тот шаг.

## ADDED (#1130): Патч плагина перед монтажом — self-регистрация и рефреш долгоживущих токенов

Требование: `dsh_patch_anthropic_pool_plugin` (`scripts/lib/dsh-ci.sh`)
вызывается МЕЖДУ `dsh_install_anthropic_pool`/`dsh_import_anthropic_accounts`
и монтажом (`dsh_mount_plugins_suite`/`dsh_mount_anthropic_pool`) в ОБОИХ
потребителях (`scripts/worker/task.sh`, `scripts/hands/dsh_task.sh`).
Применяет `scripts/lib/patch_anthropic_pool_plugin.py` к уже скачанному и
sha256-проверенному каталогу (`DSH_ANTHROPIC_POOL_EXTRACTED`), результат
репакует в новый tgz и перепривязывает `DSH_ANTHROPIC_POOL_PKG` на него —
именно этот репак монтирует `dsh_mount_anthropic_pool` следующим шагом (см.
MODIFIED выше). Функция — no-op (`return 0`), если пул неактивен
(`DSH_ANTHROPIC_POOL_ACTIVE` не `1`).

Требование: патч-скрипт правит ДВА файла плагина ЧЕТЫРЬМЯ ПАТЧАМИ (ШЕСТЬЮ
точечными правками, единица счёта — одна замена `exact string match`; патч 4
складывается из трёх точечных правок разом, см. его пункт ниже), все —
exact string match с fail loud при несовпадении формы (апстрим плагина
сменился), все шесть точечных правок проверяются на исходном содержимом ДО
записи ЛЮБОГО файла (частичное применение невозможно):
1. `lib/index.js::ensureProvider` — тело нейтрализуется целиком (`return`
   без действий). Плагин пытался зарегистрировать себя в `llm-pi-ai` через
   `ctx.get('settings').update(...)`; сервис `settings` реально смонтирован
   в headless (через `dsh-base`), поэтому эта self-регистрация гонится с
   нашей статической (`_dsh_patch_profile_anthropic_pool`) и способна
   перезаписать `models` живым каталогом `discoverModels()` —
   нейтрализация убирает гонку у корня.
2. `lib/pool.js::createRefreshCoordinator` — условие пропуска превентивного
   рефреша меняется с `if (oauth.expiresAt && ...) return account` на
   `if (!oauth.expiresAt || ...) return account`: отсутствие поля
   `expiresAt` трактуется как «токен валиден», не «токен истёк». Токены с
   явным `expiresAt` (короткоживущие) продолжают рефрешиться по сроку без
   изменений — правится только ветка «поля нет».
3. `lib/index.js::forward`, 401/403-ветка — добавляется реактивный
   рефреш-и-повтор: помечает аккаунт на диске как просроченный
   (`expiresAt = Date.now() - 1`) и вызывает `ensureFresh()` ещё раз (та же
   функция, что патч 2 правит) перед единственным повтором того же
   запроса; неудача — прежнее поведение (`cooldownUntil` + следующий
   аккаунт). Обязательная пара к патчу 2: без неё РЕАЛЬНО истёкший токен
   без `expiresAt` не восстановился бы вовсе.
4. (#1192, ТРИ точечные правки разом: `lib/pool.js` — новый экспорт;
   `lib/index.js` — правка строки `import`; `lib/index.js` — правка
   else-ветки `forward()`) `lib/pool.js` получает экспортированную ЧИСТУЮ
   функцию `classifyPoolUnavailable(accounts)` (без сети/файлов — читает
   только уже записанные `account.lastStatus`/`account.lastError`),
   `lib/index.js` добавляет её в существующий `import` из `./pool.js` и
   вызывает в `forward()`, ветка `else` (все аккаунты исчерпаны в рамках
   одного HTTP-вызова): результат (`reason` — `auth_rejected`/`rate_limited`/
   `network_error`/`unknown`, и `accounts` — список по каждому аккаунту)
   попадает в то же JSON-тело `pool_unavailable`, рядом с уже существующими
   `message`/`retryAt`. До этого патча `pool_unavailable` был одинаковым
   текстом и при 401/403 (креды отвергнуты Anthropic, нужен перевыпуск
   секретов ВЛАДЕЛЬЦЕМ), и при 429 (лимит, само пройдёт) — AGENTS.md,
   «Алерт не гадает». `auth_rejected` приоритетнее `rate_limited`, если оба
   класса присутствуют одновременно (владелец не должен быть пропущен
   только потому, что другой аккаунт всего лишь упёрся в лимит).

Требование (#1192): `dsh_run_with_pool_then_chain` (`scripts/lib/dsh-ci.sh`)
читает `reason` из `err_file` ДО того, как цепочка его перезапишет (тот же
приём, что уже применяется к самому тексту отказа, см. #1067 в коде), и
добавляет к обязательному `::warning::` отдельный факт от
`dsh_pool_unavailable_owner_note`: «владелец нужен, перевыпуск секретов
ANTHROPIC_OAUTH_*» для `auth_rejected`, «владелец НЕ нужен, ретрай в
<время>» для `rate_limited`. Поле `reason` отсутствует (патч не применился
или апстрим сменил форму ответа) — сообщение обязано признать пробел («не
классифицирована»/«не подтверждено»), не подставлять одну из двух гипотез
вместо честного пробела. Откат на цепочку при отказе пула (см. ADDED
«Выбор провайдера — пул первым, цепочка фоллбэком» ниже) не меняется.

Требование (MODIFIED, суть суперсети из ADDED «Монтаж независимо от suite»
и `dsh-in-job::ADDED: Suite ротации учёток`): `dsh_mount_plugins_suite`
пропускает СВОЙ собственный `dsh plugin add` для оригинального (непатченного)
ассета `dsh-anthropic-oauth-pool`, если `DSH_ANTHROPIC_POOL_ACTIVE=1` —
единственную, уже патченную копию монтирует `dsh_mount_anthropic_pool`
следующим шагом. Без этого правила при одновременной активации suite и
standalone-пути смонтировались бы ДВЕ копии плагина, одна из них
непатченная, и self-регистрация из патча 1 выше снова гонится со
статической регистрацией. При `DSH_ANTHROPIC_POOL_ACTIVE=0` (обычный
случай без секретов пула) suite продолжает монтировать свою копию как
раньше — путь независимых плагинов из #215 не меняется.

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
подпроцессов, запускаемых shell-тулом модели.

Оговорка (#860, решение владельца 2026-09-10): после сплита потребителей
квоты это требование защищает ТОЛЬКО `worker.yml`/`hands.yml` — только там
секрет проходит через окружение job'а, в котором работает shell-тул агента.
Канал `ai-review` секрет не получает вовсе: `ai-review.yml` не прокидывает
`ANTHROPIC_OAUTH_*` в env шага, а `scripts/review/ai_dsh.sh` не зовёт
импорт (см. «MODIFIED: Проводка секретов» ниже) — unset там нечему
выполнять, и мотивация «виден агенту ai-review» из первой версии этого
change больше не действует.

## ADDED: Выбор провайдера — пул первым, цепочка фоллбэком

Область действия: `worker.yml`/`scripts/worker/task.sh` и `hands.yml`/
`scripts/hands/dsh_task.sh` — единственные вызывающие
`dsh_run_with_pool_then_chain` (#860: `ai-review.yml`/`scripts/review/
ai_dsh.sh` зовёт `dsh_run_with_provider_chain` напрямую, минуя пул, см.
«MODIFIED: Проводка секретов» ниже).

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


Было (первая версия этого change): `.github/workflows/worker.yml`, `hands.yml`,
`ai-review.yml` передавали `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` из
`secrets.*` в env шага, вызывающего DSH, во всех трёх каналах.

Стало (решение владельца, 2026-09-10, #860): секреты пула проброшены ТОЛЬКО
в `.github/workflows/worker.yml` и `hands.yml` — недельная квота Claude
нужна разработке. `.github/workflows/ai-review.yml` секреты
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` в env шага НЕ получает, и
`scripts/review/ai_dsh.sh` не вызывает `dsh_install_anthropic_pool`/
`dsh_import_anthropic_accounts`/`dsh_mount_anthropic_pool` вовсе — ревью
идёт напрямую `dsh_run_with_provider_chain` дневной цепочкой GLM/ZAI
(`config/provider-usage.json`, `default-chain`, #857), не разделяя трафик с
разработкой на один и тот же лимит.
