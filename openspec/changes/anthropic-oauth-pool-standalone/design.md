# Design: Anthropic OAuth Pool как независимый быстрый провайдер

## Секреты в доверенной границе ai-review (#18) — паттерн вырезания env не покрывает имена владельца

`scripts/review/ai_dsh.sh` документирует доверенную границу: у этого шага
НЕТ `GH_TOKEN`, и DSH «вырезает env `*TOKEN*`/`*KEY*`/`*SECRET*` из
model-shell вызовов — агент и его не видит» (комментарий в файле,
подтверждено живым прогоном 2026-08-30). `DEEPSEEK_API_KEY`/`NVIDIA_API_KEY`
полагаются ровно на это совпадение имени с паттерном.

`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2` (имена заданы владельцем в задаче,
не переименовываются здесь) НЕ содержат `KEY`/`TOKEN`/`SECRET` — не попадают
под тот же паттерн. Если бы значение секрета (живой OAuth JSON
`accessToken`/`refreshToken`) оставалось в окружении процесса `dsh` дольше
момента импорта, агент ai-review (работающий над НЕДОВЕРЕННЫМ диффом PR)
теоретически мог бы прочитать его своим же shell-тулом (`env`/`printenv`) —
ровно та утечка, которую отсутствие `GH_TOKEN` в этом шаге призвано
предотвращать для других секретов.

**Решение**, не переименование секретов (имена — часть контракта задачи
#838, их поменять — значит разойтись с тем, что положит владелец):
`dsh_import_anthropic_accounts` (`scripts/lib/dsh-ci.sh`) делает `unset`
каждого секрета СРАЗУ после того, как значение попало в файл на диске
(`node bin/dsh-anthropic-pool.js add`) — до того, как хоть один `dsh`
процесс (в том числе тот, что видит агент ai-review) вообще стартует.
Секрет живёт в окружении bash-процесса ровно от момента чтения `${!secret_name}`
до записи в файл, счёт на миллисекунды одного `printf`, а не на всё время
job'а. Одно место правды (`dsh_import_anthropic_accounts`), а не три копии
в worker/hands/ai-review — worker.yml/hands.yml тоже выигрывают от этого
как defense-in-depth, хотя их trust-zone не требует этого так же строго,
как ai-review.

## Стык с цепочкой провайдеров (`DSH_PROVIDER_CHAIN`/манифест #823)

**Вопрос.** Можно ли просто дописать пул ЭЛЕМЕНТОМ существующей цепочки
(`{"name": "anthropic-pool", "base_url": "http://127.0.0.1:<port>", "model":
"claude-sonnet-4-5", "secret_env": "..."}`), не трогая
`dsh_run_with_provider_chain`?

**Нет — разный протокол, не просто разный URL.** Два факта из исходников
плагина (инспектирован живьём, релиз `dsh-plugins-suite-v1`):

1. `lib/index.js::ensureProvider` регистрирует себя в
   `llm-pi-ai.providers.anthropic-pool` с полем `api: 'anthropic-messages'`.
   `docs/research/10-dsh-architecture.md:305-319` документирует ровно это
   поле у `llm-pi-ai.providers.<id>` как переключатель ДИАЛЕКТА API —
   `openai-completions` и `anthropic-messages` не совместимы форматом
   запроса/ответа.
2. Элементы `DSH_PROVIDER_CHAIN` (`scripts/lib/dsh-ci.sh::
   dsh_run_with_provider_chain`) идут через СОВСЕМ ДРУГОЙ путь — не
   `llm-pi-ai` вовсе: `dsh_patch_profile`/`_dsh_patch_profile_plain`
   экспортируют `DEEPSEEK_BASE_URL`/`DEEPSEEK_API_KEY` и патчат
   `agent-default-model: {provider: deepseek-official, ...}` —
   `deepseek-official` это адаптер `llm-deepseek`
   (`docs/research/10-dsh-architecture.md:333`), который ВСЕГДА говорит
   OpenAI Chat Completions на любой `DEEPSEEK_BASE_URL` (NVIDIA/GLM/
   OpenRouter/Ollama Cloud — все OpenAI-совместимы, это и держит цепочку
   единообразной).

Направить `llm-deepseek` на loopback-прокси пула означало бы отправить
OpenAI-формат запроса на эндпоинт, который понимает только формат
Anthropic Messages — это не «ещё один OpenAI-совместимый провайдер», а
структурная несовместимость на уровне протокола, не URL/ключа.

**Решение — вариант (а): пул первым, цепочка фоллбэком, оба остаются
собственными путями выбора провайдера.** Новая функция-обёртка
`dsh_run_with_pool_then_chain` (`scripts/lib/dsh-ci.sh`):

1. Если `DSH_ANTHROPIC_POOL_ACTIVE=1` (аккаунты импортированы) — патчит
   профиль под пул (`agent-default-model: {provider: anthropic-pool, model:
   claude-sonnet-4-5}`, СВОЯ функция `_dsh_patch_profile_anthropic_pool`, не
   трогает `_dsh_patch_profile_plain`) и делает ОДИН прогон через
   `dsh_run_with_retry` (не `dsh_run_with_provider_chain` — пул уже несёт
   собственный многоаккаунтный failover ВНУТРИ вызова, `lib/index.js::
   forward`: перебирает все непопробованные аккаунты на 401/403/429 прежде
   чем вернуть ответ наружу; повторять этот перебор снаружи бессмысленно).
2. Успех (`rc=0`) → готово, `DSH_CHAIN_PROVIDER="anthropic-oauth-pool"`,
   цепочка вообще не запускается — холостой ход не платит цену второго
   провайдера (симметрично тому, как suite/цепочка уже не платят цену друг
   друга, `dsh_require_provider_chain`).
3. Отказ (`rc≠0`, все аккаунты пула недоступны/`503 pool_unavailable`,
   сеть) → `::warning::` с фактом и откат на штатную
   `dsh_run_with_provider_chain` — существующий механизм НЕ переписывается,
   вызывается как есть.
4. Пул не активен (`DSH_ANTHROPIC_POOL_ACTIVE=0`, обычный случай без
   секретов) → сразу `dsh_run_with_provider_chain`, нулевое изменение
   поведения для конфигурации, которая есть сегодня.

Это НЕ комбинация «suite внутри цепочки» (design.md `dsh-in-job`, «Стык
suite и цепочки провайдеров») — там обе стороны решали ОДИН И ТОТ ЖЕ вопрос
(«кого пробовать дальше между провайдерами/учётками одного слоя») на разных
уровнях абстракции одного и того же `llm-pi-ai`. Здесь пул и цепочка решают
РАЗНЫЕ вопросы на разных протокольных путях (`llm-pi-ai`/anthropic-messages
против `llm-deepseek`/openai-completions) — конфликта атрибуции нет,
`DSH_CHAIN_PROVIDER`/`DSH_CHAIN_TRIED` при отказе пула остаются ТОЧНО тем,
что уже возвращает `dsh_run_with_provider_chain`, только с приставкой
`anthropic-oauth-pool, ` в `DSH_CHAIN_TRIED`, если пул пробовался и отказал
(честная атрибуция «пул тоже пробовали»).

## Гейт активации — секреты, не отдельная vars-переменная

`vars.PLUGINS_SUITE_URL` — тег релиза suite (`dsh_install_plugins_suite`),
завязанный на combo-router+oauth-pool ВМЕСТЕ. Заново заводить второй
"тег релиза" var для ОДНОГО и того же уже опубликованного ассета
(`dsh-anthropic-oauth-pool-0.1.0.tgz` из релиза `dsh-plugins-suite-v1`) не
нужно: релиз пока один, ассет пока один — тег пинуется литералом
`ANTHROPIC_OAUTH_POOL_RELEASE` в `dsh-ci.sh`, тем же способом, что уже
пинуются `DSH_VERSION`/`DSH_HEADLESS_VERSION` в этом файле (правка при
следующей сборке плагина владельцем — тот же ритуал, что бамп версии DSH).

Газ включения — присутствие хотя бы одного из
`ANTHROPIC_OAUTH_1`/`ANTHROPIC_OAUTH_2`: секреты уже несут весь нужный
сигнал («владелец хочет пул» = «владелец положил креды»), второй
переключатель поверх них дублировал бы этот же факт и мог рассинхрониться с
ним (секрет есть, а флаг забыли включить, или наоборот) — тот самый класс
двух мест правды, который запрещён `AGENTS.md`. Симметрично критерию 4
`#215` («не задана vars → не подключено, поведение как раньше»), только
газ — секрет, а не vars.

## Монтаж — тот же паттерн, что уже доказан для suite

`dsh_install_anthropic_pool`/`dsh_mount_anthropic_pool` копируют форму
`dsh_install_plugins_suite`/`dsh_mount_plugins_suite` (скачивание+sha256,
`dsh plugin add`, структурная проверка `dsh --dump-config` на строку
`^- id: anthropic-oauth-pool$`) — тот же порядок вызовов (монтаж ПОСЛЕ
первого `dsh_patch_profile`, чтобы `initProfile` не создал файл патча сам,
см. комментарий у `dsh_install_plugins_suite` в `dsh-ci.sh`), без изменений
в самом suite-коде.

Отличие: наша функция ДОПОЛНИТЕЛЬНО извлекает tgz в scratch-каталог (не
только передаёт путь в `dsh plugin add`), потому что импорт аккаунтов идёт
`node <extracted>/bin/dsh-anthropic-pool.js add <id> <файл>` НАПРЯМУЮ, а не
через бинарник `dsh-anthropic-pool`, который `dsh plugin add` может не
выставить в `PATH` (сам `README.md` плагина честно об этом предупреждает:
«If the DSH installer does not expose package binaries, run
`bin/dsh-anthropic-pool.js` directly with Node»). `bin/dsh-anthropic-pool.js`
— самодостаточный ES-модуль без внешних зависимостей (`package.json:
engines.node >=22`, `node --test` из README), пишет напрямую в
`~/.dsh/anthropic-accounts/` (`lib/accounts.js::poolDir`, по умолчанию
`os.homedir()`) — тот же `$HOME`, что видит смонтированный плагин в том же
job'е, порядок вызова (импорт до/после монтажа) не имеет значения для
результата, но естественно идёт ДО первого прогона `dsh`.

## Не подтверждено

- **Гонка регистрации провайдера при первом реальном запросе.**
  `ensureProvider()` (`lib/index.js`) вызывает `discoverModels()` — сетевой
  запрос к Anthropic `/v1/models` — ПЕРЕД тем, как зарегистрировать
  провайдера в `llm-pi-ai.providers` через `settings.update`. Оба вызова
  асинхронны относительно `server.listen()`, который сам по себе не
  блокирует загрузку остальных плагинов хоста. Успеет ли эта регистрация
  завершиться раньше, чем `agent-default-model` попробует резолвить
  `provider: anthropic-pool` в РЕАЛЬНОМ (не `--dump-config`) прогоне
  `dsh --profile headless "<текст>"` — не проверено без живых аккаунтов
  (закрывающая проверка, tasks.md). Структурная проверка монтажа
  (`dsh --dump-config` на id-строку из `cordis.patch.yml`) от этой гонки не
  зависит — та строка приходит из статического bundle-патча, не из
  асинхронной регистрации в `llm-pi-ai.providers`.
- Точный набор `id` моделей, которые реально отдаёт `/v1/models` живых
  аккаунтов владельца — статический дефолт плагина `claude-sonnet-4-5`
  используется как id для `agent-default-model.model`; если `discoverModels()`
  успеет подменить список ДО резолва модели и в нём не окажется этого id —
  запрос отклонится с «модель не найдена». Проверяется только живым
  прогоном.
- Поведение `dsh plugin add` при повторном монтаже ОБОИХ независимых
  плагинов (suite позже включат снова, #790 решат) в один профиль — два
  разных `dsh plugin add` на разные tgz одного плагина `anthropic-oauth-pool`
  (наш путь и suite-путь) в теории могут конфликтовать по `id` бандла; вне
  бюджета этого change (suite сегодня выключена).

## Отвергнутые варианты

- **Пул как элемент `DSH_PROVIDER_CHAIN`.** Отвергнут — протокольная
  несовместимость `anthropic-messages`/`openai-completions`, см. выше.
- **Второй `vars`-тег релиза для того же ассета.** Отвергнуто — не нужно
  второе место правды для одного и того же уже опубликованного файла;
  литеральный пин в коде дешевле и честнее (тот же паттерн, что версии DSH).
- **Возврат `vars.PLUGINS_SUITE_URL` целиком (реанимация combo-router).**
  Отвергнуто явно владельцем — #216 остаётся заблокированной, #790
  (`NO_COMBO_ROUTE` без газа) не решается этим change.
