Задача: #838.

# Tasks: anthropic-oauth-pool-standalone

- [x] 1. `scripts/lib/dsh-ci.sh`: `ANTHROPIC_OAUTH_POOL_RELEASE` (пин тега
      релиза, литерал по образцу `DSH_VERSION`), `dsh_install_anthropic_pool`
      (скачивание+sha256+распаковка ассета `PLUGINS_SUITE_OAUTH_ASSET`,
      независимо от `vars.PLUGINS_SUITE_URL`; гейт — наличие хотя бы одного
      из `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`, иначе notice+return 0),
      `dsh_mount_anthropic_pool` (`dsh plugin add` + структурная проверка
      `dsh --dump-config` на `^- id: anthropic-oauth-pool$`, fail loud).
- [x] 2. `dsh_import_anthropic_accounts` — для каждого заданного секрета
      пишет значение во временный файл mode 0600, зовёт
      `node <extracted>/bin/dsh-anthropic-pool.js add anthropic-N <файл>`,
      удаляет временный файл; fail loud при сбое импорта. **Плюс `unset`
      секрета из окружения сразу после импорта** — находка design.md
      («Секреты в доверенной границе ai-review»): `ANTHROPIC_OAUTH_1/2` не
      match-ат паттерн `*_KEY`/`*_TOKEN`/`*_SECRET`, которым DSH иначе сам
      прячет секреты от shell-тула модели в `ai-review` (trust-zone #18) —
      без явного `unset` живой OAuth JSON виден агенту, ревьюющему
      недоверенный PR, через `env`/`printenv`.
- [x] 3. `_dsh_patch_profile_anthropic_pool` + `dsh_run_with_pool_then_chain`
      (пул первым через `dsh_run_with_retry`, при отказе — существующая
      `dsh_run_with_provider_chain` без изменений; пул неактивен → сразу
      цепочка). `DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED`/`DSH_RUN_FAILURE_REASON`
      остаются честными в обоих путях.
- [x] 4. Подключить в `scripts/worker/task.sh`, `scripts/hands/dsh_task.sh`,
      `scripts/review/ai_dsh.sh`: вызов `dsh_install_anthropic_pool` +
      `dsh_import_anthropic_accounts` + `dsh_mount_anthropic_pool headless`
      в ту же точку, где уже стоят `dsh_install_plugins_suite`/
      `dsh_mount_plugins_suite`; замена финального
      `dsh_run_with_provider_chain` на `dsh_run_with_pool_then_chain`.
- [x] 5. `.github/workflows/worker.yml`, `hands.yml`, `ai-review.yml`:
      `ANTHROPIC_OAUTH_1: ${{ secrets.ANTHROPIC_OAUTH_1 }}`,
      `ANTHROPIC_OAUTH_2: ${{ secrets.ANTHROPIC_OAUTH_2 }}` в env шага DSH.
- [x] 6. Тест-гвардия `scripts/lib/test/dsh-anthropic-pool.guard.sh`,
      доказана мутацией:
      (а) нет секретов → `dsh_install_anthropic_pool` не качает, не падает,
          `DSH_ANTHROPIC_POOL_ACTIVE=0`;
      (б) секрет задан → `dsh_import_anthropic_accounts` кладёт
          `~/.dsh/anthropic-accounts/<id>.json` с `claudeAiOauth.
          {accessToken,refreshToken}` из фейкового JSON, И секретная
          переменная окружения отсутствует (`unset`) после возврата функции
          (доказательство находки «Секреты в доверенной границе ai-review»);
      (в) `dsh_run_with_pool_then_chain`: пул отвечает успехом → цепочка не
          запускается, `DSH_CHAIN_PROVIDER=anthropic-oauth-pool`; пул
          отказывает → откат на цепочку с честными
          `DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED` (включая
          `anthropic-oauth-pool` в `DSH_CHAIN_TRIED`); пул неактивен →
          поведение цепочки идентично состоянию ДО этого change.
      Регистрация шагом в `repo-ci.yml`, тем же местом, что соседние
      гвардии (`dsh-suite-chain-conflict.guard.sh` и т.п.).
- [x] 7. Дельта-спека `specs/journal-tasks-hands/spec.md` (ADDED-требование).

## Доработка (#859, живой инцидент PR #858, 2026-09-10)

- [x] 8. `dsh_import_anthropic_accounts` изолирует каждый секрет независимо
      (BOM-strip + jq-валидация JSON/полей ДО вызова `node ... add`) —
      битый секрет пропускается с `::warning::`, не роняет шаг; ни одного
      валидного аккаунта → пул тихо отключает себя
      (`DSH_ANTHROPIC_POOL_ACTIVE=0`), не падает. См. design.md, «Изоляция
      битого секрета».
- [x] 9. Тест-гвардия расширена секциями 6-8 (BOM восстанавливается и
      импортируется; сосед с битым JSON пропущен; все секреты биты →
      сквозной путь до цепочки), доказано мутацией (откат фикса
      воспроизводит исходный `SyntaxError` из живого инцидента).

## Доработка (#860, решение владельца 2026-09-10): сплит потребителей квоты

- [x] 10. Убрать пул из `ai-review.yml`/`scripts/review/ai_dsh.sh`: секреты
      `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` больше не в env шага DSH,
      `ai_dsh.sh` не вызывает `dsh_install_anthropic_pool`/
      `dsh_import_anthropic_accounts`/`dsh_mount_anthropic_pool` — прогон
      идёт напрямую `dsh_run_with_provider_chain` (дневная цепочка GLM/ZAI,
      `config/provider-usage.json`, #857). `worker.yml`/`hands.yml` не
      тронуты — пул там остаётся первым перед цепочкой, как в п.3-5 выше.
      Причина: недельная квота Claude (разработка) и дневная квота GLM/ZAI
      (ревью) не должны жечься одним потребителем.
- [x] 11. Дельта-спека `specs/journal-tasks-hands/spec.md` — MODIFIED-секции
      «Проводка секретов» (было: три канала, стало: только worker/hands) и
      уточнена область действия «Выбор провайдера — пул первым».

## Доработка (#1097, живой инцидент, прогон worker.yml 34746091297, 2026-09-13)

- [x] 12. Причина найдена фактом: `ctx.get('settings')` в профиле `headless`
      всегда `undefined` (settings-сервис не смонтирован ни одним пакетом
      headless-профиля) — `ensureProvider()` плагина бросает `TypeError` ДО
      регистрации провайдера на КАЖДОМ прогоне, не иногда/по гонке (снимает
      «Не подтверждено» design.md, п. «Гонка регистрации»). HTTP-прокси
      плагина при этом работает нормально — падает только self-регистрация.
- [x] 13. Фикс: `_dsh_patch_profile_anthropic_pool` (`scripts/lib/dsh-ci.sh`)
      теперь сама статически пишет `llm-pi-ai.providers.anthropic-pool` в
      `cordis.patch.yml` (тем же путём, что уже работает у combo-router),
      порт прокси зафиксирован (`ANTHROPIC_OAUTH_POOL_PORT`/
      `DSH_ANTHROPIC_POOL_PORT`) — регистрация больше не зависит от
      `ctx.get('settings')`.
- [x] 14. Гвардия `scripts/lib/test/dsh-anthropic-pool.guard.sh`, секция 10:
      проверяет содержимое `cordis.patch.yml` (секция `llm-pi-ai`, баз-URL на
      фиксированный порт, непустой `apiKeyEnv`), доказано мутацией (снят
      provider-блок фикса → секция красная с точным сообщением; фикс возвращён
      → зелёная).
- [x] 15. `docs/research/32-claude-oauth-provider.md` — раздел «Дополнение
      2026-09-13» с точным разбором причины и фиксом.
- [ ] 16. Видимость деградации (алерт «пул отказывал N прогонов подряд») —
      вынесена в отдельную задачу #1098 (репозиторные инварианты заняты
      двумя параллельными PR на момент этого фикса, см. `AGENTS.md`
      «КОНКУРЕНЦИЯ» в промпте задачи).

## Доработка (#1130, живой инцидент второй заход, прогон worker.yml 34753001158, 2026-09-13)

- [x] 17. Причина найдена фактом: `settings` (`@deepseek-ai/dsh-settings-file`)
      РЕАЛЬНО смонтирован в headless (через `dsh-base`, не `dsh-headless`) —
      утверждение обратного в #1097 (задача 12/13 выше) было неполным.
      Self-регистрация плагина (`ensureProvider()`) продолжает работать
      ПАРАЛЛЕЛЬНО нашей статической (#1097) и гонится с ней: живой
      `discoverModels()` (реальные аккаунты) может перезаписать наш
      статический `models` (MERGE, но массивы заменяются целиком) каталогом
      без буквального id `claude-sonnet-4-5` → `UNKNOWN_MODEL`.
- [x] 18. Фикс: `scripts/lib/patch_anthropic_pool_plugin.py` — точечно
      нейтрализует ТЕЛО `ensureProvider()` (exact string match, fail loud на
      несовпадении формы), `dsh_patch_anthropic_pool_plugin`
      (`scripts/lib/dsh-ci.sh`) применяет патч к распакованному плагину
      (после sha256), репакует и монтирует ВМЕСТО оригинала. Подключено в
      `scripts/worker/task.sh` и `scripts/hands/dsh_task.sh` между
      `dsh_import_anthropic_accounts` и монтажом.
- [x] 19. Гвардия `scripts/lib/test/dsh-anthropic-pool.guard.sh`, секции
      11a/11b: happy path (патч на прод-форме `ensureProvider`, скопированной
      из released tgz, убирает живой вызов `ctx.get('settings')`, оставляя
      функцию) + мутация (искажённая форма → `PATCH_MARKER_NOT_FOUND`, файл
      не тронут). Доказано мутацией самого патч-скрипта (снят →
      секция 11 красная; возвращён → зелёная).
- [x] 20. `docs/research/32-claude-oauth-provider.md` — раздел «Поправка
      2026-09-13» исправляет ошибочное утверждение прошлого дополнения;
      `design.md` — «ВТОРАЯ ПОПРАВКА #1130» тем же приёмом.
- [ ] 21. Видимость деградации — по-прежнему #1098 (не расширена этим PR).

## Доработка (#1130): превентивный рефреш долгоживущих токенов

Вопрос, процитированный оркестратором как вопрос владельца (передан агенту
рабочей перепиской конвейера, без машиночитаемого артефакта решения по
AGENTS.md — см. design.md, раздел «Превентивный рефреш долгоживущих
токенов», честная оговорка об атрибуции): «Какой опус он там пытается
рефрешить, если мы поставили долгоживущие OAuth токены, которые не надо
рефрешить?». Разбор —
`lib/pool.js::createRefreshCoordinator` — нашёл: условие пропуска рефреша
`if (oauth.expiresAt && oauth.expiresAt - Date.now() > REFRESH_SKEW_MS)
return account` ложно И при отсутствующем `expiresAt`, И при просроченном —
плагин путает «срок неизвестен» (долгоживущий accessToken без явного
`expiresAt`) с «срок истёк» и рефрешит превентивно на КАЖДЫЙ запрос
(`lib/index.js`: `forward()`/`discoverModels()`).

- [x] 22. `scripts/lib/patch_anthropic_pool_plugin.py` расширен ДО трёх
      точечных патчей (интерфейс сменился: принимает каталог `package/`, не
      путь к одному файлу): `PATCH_POOL_SKIP_CONDITION` (`lib/pool.js`) —
      отсутствие `expiresAt` больше не триггерит рефреш, короткоживущие
      токены (`expiresAt` задан) ведут себя как раньше; `PATCH_REACTIVE_REFRESH`
      (`lib/index.js`, 401/403-ветка `forward()`) — обязательная пара: без
      неё реально протухший токен без `expiresAt` не восстановился бы вовсе
      (401/403 просто уходил в cooldown 60с). Реактивный рефреш помечает
      аккаунт на диске как просроченный и зовёт `ensureFresh()` ещё раз —
      переиспользует существующий `refreshToken()`/`writeAccount()`/
      single-flight `pending`, не дублирует код рефреша.
- [x] 23. Все три патча проверяются ДО записи любого файла (атомарно) —
      несовпадение формы ЛЮБОГО из трёх откатывает весь вызов, не оставляет
      плагин наполовину пропатченным.
- [x] 24. Гвардия: секции 11a/11b/13 расширены на pool.js (фикстура —
      точная копия `createRefreshCoordinator`, не пересказ), плюс НОВАЯ
      секция 14 — поведенческое доказательство через реальный вызов
      патченного `pool.js` в node с синтетическими
      `readAccount`/`writeAccount`/`refreshToken`: без `expiresAt` рефреша
      нет, с `expiresAt` в прошлом — есть, с `expiresAt` далеко в будущем —
      нет (короткоживущие токены не задеты). Доказано мутацией (откат
      условия в `PATCH_POOL_SKIP_CONDITION_NEW` на старое/баговое → секция
      11 красная с точным сообщением; возвращено → зелёная).
- [ ] 25. Живая проверка (после мержа, требует реальных секретов
      владельца) — по логу видно, что рефреш срабатывает только на реальный
      401 от Anthropic, не на каждый запрос долгоживущего токена без
      `expiresAt`. Fail loud без секретов не проверяется здесь же.

## Доработка (#1192, живой инцидент, прогон worker.yml 34792555573, 2026-09-14)

Живой прогон (00:23Z, уже с фиксом #1130): пул честно перебрал все аккаунты
за 17с и ответил `pool_unavailable` — агрегатом «ни один аккаунт не
доступен», без причины. Тот же текст получился бы и при 401/403 (креды
отвергнуты Anthropic, нужен перевыпуск секретов ВЛАДЕЛЬЦЕМ), и при 429
(лимит, само пройдёт), хотя `account.lastStatus` уже нёс нужный код — см.
design.md, «pool_unavailable — причина отказа (#1192)».

- [x] 26. Патч 4 плагина (три точечные правки разом, см. spec.md, ADDED
      (#1130), пункт 4): `lib/pool.js::classifyPoolUnavailable(accounts)` —
      чистая функция без сети/файлов, классы `auth_rejected`/`rate_limited`/
      `network_error`/`unknown` по `lastStatus`/`lastError`, приоритет у
      `auth_rejected`; `lib/index.js` — import + вызов в else-ветке
      `forward()`, `reason`/`accounts` в JSON-теле `pool_unavailable`.
- [x] 27. `scripts/lib/dsh-ci.sh::dsh_pool_unavailable_owner_note` — читает
      `reason` ИЗ ТЕЛА `pool_unavailable` (не из всего `err_file` — чужой
      JSON со своим `reason` не должен быть принят за причину отказа пула),
      формулирует владельцу разный текст на каждый класс; поле/тело
      отсутствует — честный пробел («не классифицирована»), без подстановки
      гипотезы. Подключена в `dsh_run_with_pool_then_chain`, откат на
      цепочку не меняется.
- [x] 28. Гвардия `scripts/lib/test/dsh-anthropic-pool.guard.sh`, секции
      15-19: happy path патча 4 на прод-форме, мутация (искажённая форма
      else-ветки → `PATCH_MARKER_NOT_FOUND`, атомарность), поведенческое
      доказательство `classifyPoolUnavailable` через node-импорт патченного
      `pool.js`, исходы `dsh_pool_unavailable_owner_note` на прод-форме
      stderr (после раунда 4 — восемь stderr-форм, включая «тела
      pool_unavailable нет вовсе» и «чужой reason до/после тела»),
      сквозной `dsh_run_with_pool_then_chain`.
- [x] 29. `docs/research/32-claude-oauth-provider.md` — дополнение
      «2026-09-14 (#1192)»; `design.md` — раздел «pool_unavailable —
      причина отказа (#1192)»; `spec.md` — ADDED (#1130), пункт 4 патча +
      требование к `dsh_run_with_pool_then_chain`.
- [ ] 30. Живая проверка (после мержа, требует реальных секретов владельца
      или живого отказа пула) — по логу видно поле `reason` в ответе
      `pool_unavailable` и владельческий факт («владелец нужен»/«владелец НЕ
      нужен») в `::warning::`. Fail loud без живого отказа не проверяется
      здесь же — тот же класс ограничения, что у задачи 25 выше.
- [x] 31. Четвёртый раунд ai-review (PR #1193, находка блокирующая + чеклист):
      вырезка тела `pool_unavailable` держится в пределах СТРОКИ тела
      (`grep -oE` построчен; прежний `tr '\n' ' '` схлопывал весь `err_file`,
      жадный `.*` забирал хвост — чужой `reason` из любой последующей строки
      принимался за причину пула); секция 18 гвардии — кейсы «чужой reason
      после тела» и «чужой reason до тела»; формулировка `unknown` нейтральная
      (класс шире «ни разу не пробовался» — записанный `lastStatus` вне
      {401,403,429}, например 200, тоже unknown; доказано кейсом `stale200`
      в секции 17 и докстрингом патча); обёртка патча (`::group::`/`::error::`
      в `dsh-ci.sh`) называет и патч 4. Доказательство мутацией: старая
      `tr`-строка возвращена → гвардия красная ровно на секции 18 («чужой
      reason ПОСЛЕ тела принят за причину пула») → фикс возвращён → все 19
      секций зелёные.

## Закрывающая проверка (после этого PR, требует секретов владельца)

- [ ] Владелец кладёт `ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`
      (`gh secret set`, формат — README PR/issue #838). Живой прогон
      `worker.yml`/`hands.yml`: в логе видно монтаж `anthropic-oauth-pool`,
      импорт аккаунтов, попытку пула ПЕРВОЙ; при успехе —
      `DSH_CHAIN_PROVIDER=anthropic-oauth-pool` в отчёте прогона.
      `ai-review.yml` в эту проверку НЕ входит (#860) — там пула нет
      структурно. Без секретов эта проверка невозможна — не входит в
      критерий готовности самого PR (Scope/Out), фиксируется отдельным
      комментарием в #838 после того, как секреты появятся.
