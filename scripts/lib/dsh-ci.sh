#!/usr/bin/env bash
# Общая CI-механика DSH: установка tarball'ами с пином целостности, GLM-патч
# профиля, маскирование секретов. Единственное место правды — используется и
# руками (scripts/hands/dsh_task.sh), и автономным воркером (scripts/worker/task.sh).
# Обоснование механики и «почему так» — docs/research/10-dsh-architecture.md,
# слайс 1 (#48), живые прогоны 2026-08-30.
#
# Подключение: source "$(dirname "${BASH_SOURCE[0]}")/../lib/dsh-ci.sh"
# Рассчитано на bash с set -euo pipefail (источник задаёт).

# Пины версий и целостности (integrity = dist.integrity из metadata реестра),
# сверяются с фактически скачанным tarball'ом — несовпадение это громкий отказ,
# а не warning. Пакеты ставятся ТОЛЬКО tarball'ами: `npm install <pkg>` даёт 404.
# 0.0.1-rc.1 намеренно НЕ используется: тянет @deepseek-ai/dsh-code-runtime-worker,
# который в публичном npm отсутствует (tarball 404); с 0.0.1-rc.3 зависимость —
# dsh-code-runtime-worker-thread, она опубликована (проверено установкой, 475 пакетов).
DSH_VERSION="0.1.7-alpha.2"
DSH_INTEGRITY="sha512-uXuWobwmpNzqFOTFP61kgf0aH8IzU9LWPF9QWCUfv5DOtgtlm+6+zHvQMboEe0bCaitul61ySvUqd4mMNRedzw=="
DSH_HEADLESS_VERSION="0.1.7-alpha.2"
DSH_HEADLESS_INTEGRITY="sha512-kRaYTJ+U5z0IywMMpI77O0w8P+FDFepltMTaJKb+ZhIFtCdWwFcb/f1Ah156qOiRLvhJy+Y2OU+lEdnZq5/Riw=="

# Пин ТРАНЗИТИВНЫХ зависимостей по дате (#1467). Пины выше держат только два
# верхних пакета; их собственные зависимости объявлены ДИАПАЗОНАМИ, и каждый
# прогон резолвит их заново — то есть публикация в чужом реестре меняет то,
# что стоит в нашем job'е, без единого коммита у нас.
#
# ОПРОВЕРГНУТО (#1481). Первая редакция этого комментария называла виновником
# `@deepseek-ai/cordis 4.0.4` по совпадению дат публикации. Пин реально
# закрепил 4.0.3 — и dsh продолжил падать тем же «user patch-layer watching
# requires the Cordis HMR service». Причина оказалась другой и установлена
# локальным воспроизведением с инструментовкой, а не по датам:
# `dsh-app-boot@0.1.1-rc.2` зовёт `hmr.registerConfig(...)`, которого нет НИ В
# ОДНОЙ опубликованной версии `@deepseek-ai/cordis-plugin-hmr` (1.0.16…1.0.19),
# то есть путь `watchUserPatches` не мог отработать ни при каком выборе
# транзитивных версий. Лечится переходом на 0.1.7-alpha.2, где апстрим этот
# путь выкинул, а строка hmr переехала на свой пакет `@deepseek-ai/dsh-hmr`.
# Разбор целиком — закрытая #1467 и #1481.
#
# Сам механизм пина при этом остаётся нужным и ниже не отменяется: чужая
# публикация по-прежнему меняет содержимое нашего job'а без коммита у нас.
#
# `npm --before` резолвит ВСЕ версии так, как реестр выглядел на указанный
# момент, — это лечит класс (любая транзитивная зависимость), а не случай
# (cordis). Дату двигают руками вместе с пинами версий выше: обновление
# перестаёт быть событием чужого реестра и становится нашим коммитом.
DSH_RESOLVE_BEFORE="2026-09-23T00:00:00Z"

# Видимый результат установки, а не факт «шаг прошёл» (AGENTS.md). Ровно та
# версия, что реально встаёт с пинами выше — замерено установкой
# dsh@0.1.7-alpha.2 + dsh-headless@0.1.7-alpha.2 (#1481), а не взято из
# диапазона. Расхождение — громкий отказ: молча уехавшая транзитивная
# зависимость меняет наш job без коммита у нас.
DSH_EXPECTED_CORDIS="4.0.4"

# Ротация учёток — плагины владельца combo-router + anthropic-oauth-pool
# (#215). Публикуются релизными ассетами ЭТОГО репозитория (как forge-плагины
# — .github/workflows/plugin-forge.yml — tarball + .sha256, `gh release
# upload --clobber`), имена файлов объявляются здесь один раз.
PLUGINS_SUITE_COMBO_ASSET="dsh-combo-suite-0.1.0.tgz"
PLUGINS_SUITE_OAUTH_ASSET="dsh-anthropic-oauth-pool-0.1.0.tgz"

# Быстрый провайдер Claude — anthropic-oauth-pool НЕЗАВИСИМО от suite (#838).
# vars.PLUGINS_SUITE_URL снята вместе со сломанным combo-router (#790,
# NO_COMBO_ROUTE без газа) — но dsh-anthropic-oauth-pool САМОДОСТАТОЧЕН (свой
# loopback-прокси, свой failover между аккаунтами) и от combo-router не
# зависит. Ассет — ТОТ ЖЕ tgz, что уже публикует suite
# (PLUGINS_SUITE_OAUTH_ASSET выше, не дублируем имя файла); тег релиза —
# отдельный литеральный пин, не vars.PLUGINS_SUITE_URL (та переменная вернула
# бы в проводку сломанный combo-router). Пин версии — тот же ритуал, что
# DSH_VERSION/DSH_HEADLESS_VERSION выше: правится здесь при следующей сборке
# плагина владельцем.
ANTHROPIC_OAUTH_POOL_RELEASE="dsh-plugins-suite-v1"

# Секреты аккаунтов Claude — JSON ЦЕЛИКОМ в значении секрета, формат
# ~/.claude/.credentials.json (lib/accounts.js::importAccount плагина):
# {"claudeAiOauth": {"accessToken": "...", "refreshToken": "...", ...}}.
# Список — одно место правды для id секретов и порождаемых id аккаунтов пула
# (anthropic-1/anthropic-2, позиционно). Газ подключения пула — наличие ХОТЯ
# БЫ ОДНОГО из этих секретов, не отдельная vars-переменная (design.md
# anthropic-oauth-pool-standalone, «Гейт активации»): секрет уже несёт весь
# нужный сигнал, второй переключатель поверх него дублировал бы факт и мог
# рассинхрониться с ним.
ANTHROPIC_OAUTH_ACCOUNT_SECRETS=(ANTHROPIC_OAUTH_1 ANTHROPIC_OAUTH_2)

# Порт loopback-прокси пула — ФИКСИРОВАННЫЙ, не эфемерный (#1097, живой
# инцидент: прогон worker.yml 34746091297, `TypeError: Cannot read
# properties of undefined (reading 'update')`). Причина: плагин
# self-регистрирует свой провайдер в llm-pi-ai через `ctx.get('settings')`
# (lib/index.js::ensureProvider) — обращение к `.update` бросает TypeError
# ДО регистрации, `agent-default-model` резолвит несуществующего провайдера
# "anthropic-pool" → `NO_ADAPTER`.
#
# ОПРОВЕРГНУТО #1130: предыдущая версия этого комментария утверждала, что
# сервис settings НИКОГДА не смонтирован в headless. Живой прогон worker.yml
# 34753001158 (уже с фиксом #1097, ниже) сменил ошибку на `UNKNOWN_MODEL` —
# факт, невозможный при недоступном settings (провайдер не мог бы
# зарегистрироваться вовсе). Реальная причина: settings
# (`@deepseek-ai/dsh-settings-file`) смонтирован через `dsh-base` (общий
# нижний слой всех профилей) — `@deepseek-ai/dsh-headless` сам его не
# тянет, но это не то же самое, что «сервис недоступен вовсе». Разбор —
# docs/research/32-claude-oauth-provider.md, «Поправка 2026-09-13».
#
# Раз settings реально доступен, self-регистрация плагина продолжает
# работать ПАРАЛЛЕЛЬНО нашей статической регистрации (ниже) и гонится с
# ней: живой `discoverModels()` плагина (реальный `/v1/models` с реальными
# аккаунтами) способен перезаписать models settings-записью плагина позже
# нашей — см. `dsh_patch_anthropic_pool_plugin` (#1130), которая нейтрализует
# саму self-регистрацию, чтобы гонки не было вовсе, а не полагается на
# совпадение id моделей.
#
# HTTP-прокси плагина (аутентификация, failover между аккаунтами) при этом
# стартует и работает нормально — отказ ensureProvider() пойман `.catch()`
# внутри плагина и не роняет процесс. Фикс #1097 — статическая регистрация
# провайдера в cordis.patch.yml (_dsh_patch_profile_anthropic_pool ниже),
# тем же путём, что уже работает у combo-router (dsh_patch_profile выше) —
# в обход settings. Порт должен быть известен ДО старта dsh (баз-URL
# пишется в патч заранее), поэтому фиксирован, а не выбирается ОС: один
# job — один процесс dsh за раз, порт свободен (своя ВМ раннера).
ANTHROPIC_OAUTH_POOL_PORT=47291

# Потолок длины промпта в БАЙТАХ (#1315). Это не наша политика, а предел ядра
# Linux на ОДИН аргумент execve: MAX_ARG_STRLEN = 32 страницы = 131072 байта,
# величина отдельная от общего ARG_MAX и не поднимаемая ulimit'ом. dsh
# принимает задачу только позиционным аргументом (lib/startup.js пакета
# @deepseek-ai/dsh-headless: `[task...]`, ни --task-file, ни stdin), поэтому
# промпт длиннее физически не доедет ни до одного провайдера — execve вернёт
# E2BIG раньше сети.
#
# Берём с запасом 4 КиБ: на том же execve лежат ещё argv[0..2]
# (`timeout`, секунды, `dsh`, `--profile`, `headless`) и весь environment
# job'а, а E2BIG считает их вместе. Запас обоснован не «на глаз»: штатный
# промпт воркера ~111 КБ (строка «Промпт собран: … байт» в логах прогонов) —
# то есть до предела оставалось ~20 КБ, и первая же задача с крупным диффом
# его перешагнула (прогон 35046585539, задача #770).
DSH_PROMPT_MAX_BYTES="${DSH_PROMPT_MAX_BYTES:-126976}"

# #1318: тот же предел, но как размер ОДНОГО куска нарезки. Взят заметно ниже
# MAX_ARG_STRLEN (128 КиБ) не «на всякий случай», а потому что точка реза
# ищется НАЗАД от границы до подходящего пробела: запас гарантирует, что
# поиск не упрётся в начало куска на тексте с длинными строками.
DSH_PROMPT_CHUNK_BYTES="${DSH_PROMPT_CHUNK_BYTES:-100000}"

# Общий потолок на ВСЕ аргументы вместе (ARG_MAX, ~2 МиБ на Linux, считается
# вместе с environment). Величина другая, чем MAX_ARG_STRLEN, и нарезкой не
# лечится — поэтому у неё своя проверка и свой текст отказа. 1 МиБ — половина
# типичного ARG_MAX, запас на environment job'а GitHub Actions (там десятки
# переменных, включая секреты и каталоги путей).
DSH_PROMPT_TOTAL_MAX_BYTES="${DSH_PROMPT_TOTAL_MAX_BYTES:-1048576}"

# Имя apiKeyEnv-переменной провайдера llm-pi-ai.providers.anthropic-pool —
# ОДНО место правды для имени (используется и как ключ env, и в YAML-патче
# ниже). Значение — НЕ секрет: сама аутентификация идёт через реальный
# OAuth-токен аккаунта пула ВНУТРИ прокси (lib/index.js::oauthHeaders вырезает
# входящий authorization/x-api-key и подставляет свой Bearer), это поле лишь
# обязано быть непустым, чтобы адаптер llm-pi-ai согласился загрузить
# провайдер (сам плагин пытался прописать сюда то же самое через
# ctx.get('credentials').set(...) — сервис credentials, как и settings выше,
# реально смонтирован в headless через dsh-base (ОПРОВЕРГНУТО #1130: «тоже
# недоступно» было неверно), но это уже не важно — self-регистрация плагина
# нейтрализована патчем dsh_patch_anthropic_pool_plugin, эта строка кода
# внутри плагина больше не исполняется вовсе).
ANTHROPIC_OAUTH_POOL_APIKEY_ENV="ANTHROPIC_OAUTH_POOL_TOKEN"

# vars.PLUGINS_SUITE_URL — имя переменной унаследовано от design.md/tasks.md
# dsh-in-job (объявлено ещё до решения о механизме публикации), но её
# ЗНАЧЕНИЕ — тег релиза ЭТОГО репозитория, не сырой URL: скачивание идёт
# публичным HTTPS без токена (репозиторий публичный, AGENTS.md «Секреты»)
# стабильным путём `releases/download/<тег>/<ассет>`, а не `gh release
# download`/`gh api` — тот требует токен, которого нет у ai-review (шаг
# «Ревью агентом» намеренно без GH_TOKEN, trust-zone задачи #18); голый HTTPS
# работает одинаково во всех трёх зонах доверия (worker/hands с PAT,
# ai-review без токена вовсе).
dsh_require_plugins_suite_repo() {
  : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан — нужен для releases/download URL}"
}

# Провайдеры-кандидаты для маршрутов combo-router — alias/baseURL/имя
# env-переменной ключа/модель/contextWindow ВЗЯТЫ БУКВАЛЬНО из примера,
# который уже поставляется внутри самого suite
# (dsh-combo-router/examples/mytab0r.settings.yml, прочитано и подтверждено
# в #215) — не изобретены. Маршрут попадает в композицию, ТОЛЬКО если
# соответствующая apiKeyEnv-переменная фактически непуста в окружении job'а:
# владелец расширяет ротацию добавлением секрета с этим именем, без правки
# кода (одно место правды — этот список).
# Формат: "alias|baseURL|apiKeyEnvVar|model|contextWindow|displayName"
# Model id ниже (кроме zai-1, не менялся) подтверждены ЖИВЫМ discovery
# (scripts/measure/provider_model_discovery.py, #848) 2026-09-09: листинг
# /v1/models + живая верификация chat/completions (HTTP 200) на прогоне
# provider-latency-bench.yml (run 34415529070). Прежние значения
# (anthropic/claude-sonnet-4.6 у OpenRouter — платная модель не того тарифа;
# qwen3-coder:480b-cloud у Ollama Cloud — 410 Gone; deepseek-ai/deepseek-v3.2
# у NVIDIA NIM — 404) не были сверены с реальным каталогом при заведении
# (#215) и не отвечали на completions-пути (живой замер #836, run
# 34406807344). Дрейф версии у вендора — ожидаемый класс (см. комментарий у
# CODING_RANK_KEYWORDS в discovery-скрипте) — при повторном 404/410 сначала
# пере-сверить discovery, не переписывать id по памяти/примеру.
PLUGINS_SUITE_CANDIDATE_ROUTES=(
  "openrouter-1|https://openrouter.ai/api/v1|OPENROUTER_1_API_KEY|nvidia/nemotron-3-super-120b-a12b:free|262144|OpenRouter account 1"
  "openrouter-2|https://openrouter.ai/api/v1|OPENROUTER_2_API_KEY|nvidia/nemotron-3-super-120b-a12b:free|262144|OpenRouter account 2"
  "ollama-cloud-1|https://ollama.com/v1|OLLAMA_CLOUD_1_API_KEY|nemotron-3-ultra|262144|Ollama Cloud account 1"
  "ollama-cloud-2|https://ollama.com/v1|OLLAMA_CLOUD_2_API_KEY|nemotron-3-ultra|262144|Ollama Cloud account 2"
  "ollama-cloud-3|https://ollama.com/v1|OLLAMA_CLOUD_3_API_KEY|nemotron-3-ultra|262144|Ollama Cloud account 3"
  "nvidia-nim-1|https://integrate.api.nvidia.com/v1|NVIDIA_NIM_1_API_KEY|deepseek-ai/deepseek-v4-pro-0813|131072|NVIDIA NIM account 1"
  "nvidia-nim-2|https://integrate.api.nvidia.com/v1|NVIDIA_NIM_2_API_KEY|deepseek-ai/deepseek-v4-pro-0813|131072|NVIDIA NIM account 2"
  "zai-1|https://api.z.ai/api/coding/paas/v4|ZAI_1_API_KEY|glm-5|202752|Z.AI Coding Plan"
)

# Провайдер и модель — ровно одно место правды: vars.DEEPSEEK_BASE_URL /
# vars.DEEPSEEK_MODEL репозитория (#153). Зашитых фолбэков на конкретный
# эндпоинт/модель в коде больше нет нигде — их отсутствие обязано падать
# громко здесь, до любой дорогой работы (установка DSH, клоны, сессия морды),
# а не молча подставлять чужого провайдера. Разные причины — разные сообщения.
dsh_require_provider_env() {
  local missing=0
  if [ -z "${DEEPSEEK_BASE_URL:-}" ]; then
    echo "::error::не задан vars.DEEPSEEK_BASE_URL — эндпоинт провайдера объявляется только в vars репозитория, зашитых дефолтов больше нет" >&2
    missing=1
  fi
  if [ -z "${DEEPSEEK_MODEL:-}" ]; then
    echo "::error::не задан vars.DEEPSEEK_MODEL — модель объявляется только в vars репозитория, зашитых дефолтов больше нет" >&2
    missing=1
  fi
  if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "::error::DEEPSEEK_API_KEY не задан — DSH не сможет вызвать модель" >&2
    missing=1
  fi
  [ "$missing" = 0 ]
}

# GH маскирует секреты только в своих логах; всё, что уходит наружу (журнал DO,
# комментарии в задачах, Telegram), надо затирать до отправки. GitHub PAT
# (#95: токен теперь живёт и в морде — GH_RUNNER_TOKEN) маскируется
# производным паттерном формы токена: точное совпадение секрета GH покрывает
# только внутри своих логов.
redact() {
  sed -E -e 's/nvapi-[A-Za-z0-9_-]{4,}/nvapi-[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}/\1sk-[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_])ghp_[A-Za-z0-9]{20,}/\1ghp_[REDACTED]/g' \
         -e 's/(^|[^A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}/\1github_pat_[REDACTED]/g'
}

dsh_verify_integrity() { # file expected-integrity
  local actual
  actual="sha512-$(openssl dgst -sha512 -binary "$1" | openssl base64 -A)"
  if [ "$actual" != "$2" ]; then
    echo "::error::Integrity mismatch: $1 (ожидался $2, получен $actual)" >&2
    return 1
  fi
}

dsh_install() { # $1 — рабочий каталог для tarball'ов (создаётся)
  local pkgs=$1
  mkdir -p "$pkgs"
  (
    cd "$pkgs" || exit 1
    npm pack --before="$DSH_RESOLVE_BEFORE" \
      "@deepseek-ai/dsh@$DSH_VERSION" "@deepseek-ai/dsh-headless@$DSH_HEADLESS_VERSION"
    local dsh_tgz="deepseek-ai-dsh-$DSH_VERSION.tgz"
    local hl_tgz="deepseek-ai-dsh-headless-$DSH_HEADLESS_VERSION.tgz"
    [ -f "$dsh_tgz" ] || dsh_tgz=$(find . -maxdepth 1 -name "*dsh-$DSH_VERSION.tgz" | head -1)
    [ -f "$hl_tgz" ] || hl_tgz=$(find . -maxdepth 1 -name "*dsh-headless-$DSH_HEADLESS_VERSION.tgz" | head -1)
    dsh_verify_integrity "$dsh_tgz" "$DSH_INTEGRITY"
    dsh_verify_integrity "$hl_tgz" "$DSH_HEADLESS_INTEGRITY"
    npm install -g --before="$DSH_RESOLVE_BEFORE" ./*.tgz
  )
  command -v dsh >/dev/null
  dsh_verify_resolved_deps
}

# Что РЕАЛЬНО установилось, а не что мы просили (#1467). Пин по дате — это
# просьба к npm; доказательство — версия на диске.
dsh_verify_resolved_deps() {
  local root manifest got
  root=$(npm root -g 2>/dev/null) || root=""
  manifest="$root/@deepseek-ai/dsh/node_modules/@deepseek-ai/cordis/package.json"
  if [ ! -f "$manifest" ]; then
    # Вложенная копия может не появиться, если npm поднял cordis на верхний
    # уровень — это нормальная дедупликация, не отказ. Ищем там.
    manifest="$root/@deepseek-ai/cordis/package.json"
  fi
  if [ ! -f "$manifest" ]; then
    echo "::error::dsh_install: не найден манифест @deepseek-ai/cordis под $root — проверить, ЧТО установилось, нечем (#1467)" >&2
    return 1
  fi
  got=$(node -e "process.stdout.write(require('$manifest').version)")
  if [ "$got" != "$DSH_EXPECTED_CORDIS" ]; then
    echo "::error::dsh_install: @deepseek-ai/cordis $got вместо $DSH_EXPECTED_CORDIS — транзитивная зависимость уехала, несмотря на --before=$DSH_RESOLVE_BEFORE (#1467/#1481). Что именно сломается от этого расхождения — заранее не известно, поэтому отказ громкий, а не предсказание. Газ: сверить дату пина и ожидаемую версию здесь же, в scripts/lib/dsh-ci.sh" >&2
    return 1
  fi
  echo "dsh_install: транзитивный пин держит — @deepseek-ai/cordis $got (--before=$DSH_RESOLVE_BEFORE)"
  # Что РЕАЛЬНО встало, а не что мы просили (#1467, второй заход). Первый
  # диагноз назвал виновником cordis по совпадению дат — пин его закрепил,
  # а dsh продолжил падать тем же «user patch-layer watching requires the
  # Cordis HMR service». Значит уехал кто-то другой, и гадать, кто именно,
  # нельзя: список версий печатается целиком, один раз, и следующий разбор
  # начинается с факта, а не с гипотезы.
  local root_dir
  root_dir=$(npm root -g 2>/dev/null || true)
  if [ -n "$root_dir" ]; then
    echo "dsh_install: что установилось (@deepseek-ai/*):"
    node -e '
      const fs = require("fs"), path = require("path");
      const roots = [process.argv[1] + "/@deepseek-ai",
                     process.argv[1] + "/@deepseek-ai/dsh/node_modules/@deepseek-ai"];
      const seen = new Map();
      for (const dir of roots) {
        let names = [];
        try { names = fs.readdirSync(dir); } catch { continue; }
        for (const name of names) {
          const manifest = path.join(dir, name, "package.json");
          try {
            const version = JSON.parse(fs.readFileSync(manifest, "utf8")).version;
            if (!seen.has(name)) seen.set(name, version);
          } catch {}
        }
      }
      for (const name of [...seen.keys()].sort())
        console.log("  @deepseek-ai/" + name + " " + seen.get(name));
    ' "$root_dir" || echo "  (перечислить не удалось)"
  fi
}

# Скачивание и проверка целостности suite ротации учёток (#215). Единственное
# место правды: зовут worker/task.sh, hands/dsh_task.sh, review/ai_dsh.sh —
# как и dsh_install/dsh_patch_profile. Вызывать ПОСЛЕ dsh_install (не строго
# обязательно, но естественный порядок) и ДО dsh_patch_profile — та читает
# DSH_PLUGINS_SUITE_ACTIVE, выставляемый здесь.
#
# ТОЛЬКО скачивание+проверка+распаковка — ни одной команды `dsh` в этой
# функции. Монтаж (`dsh plugin add`) — отдельно, dsh_mount_plugins_suite,
# и обязан вызываться ПОСЛЕ dsh_patch_profile: доказанный живым прогоном
# порядок этого же репозитория (hands/dsh_task.sh, «Плагин стрима»,
# 2026-08-30 — «initProfile пишет файлы профиля только при отсутствии — наш
# патч при dsh plugin add не перезаписывается»), обратный порядок при
# локальной проверке #215 не подтвердил активацию надёжно.
#
# vars.PLUGINS_SUITE_URL не задана → видимая строка «не подключена», return 0,
# поведение как раньше (одиночный провайдер) — обратная совместимость,
# критерий 4 приёмки #215.
#
# vars.PLUGINS_SUITE_URL задана → скачивание обоих ассетов + .sha256
# (публичный HTTPS, см. комментарий у PLUGINS_SUITE_COMBO_ASSET выше), сверка
# целостности, распаковка обёртки dsh-combo-suite. Любой сбой (сеть, sha256,
# форма ассета) — громкий возврат 1: «suite не установился» из критерия 6
# #215, вызывающий обязан упасть.
dsh_install_plugins_suite() { # $1 — рабочий каталог
  local dir=$1
  DSH_PLUGINS_SUITE_ACTIVE=0
  DSH_PLUGINS_SUITE_COMBO_PKG=""
  DSH_PLUGINS_SUITE_OAUTH_PKG=""
  if [ -z "${PLUGINS_SUITE_URL:-}" ]; then
    echo "::notice::ротация учёток не подключена: vars.PLUGINS_SUITE_URL не задана — используется единственный провайдер из vars.DEEPSEEK_* (#215)"
    return 0
  fi
  dsh_require_plugins_suite_repo
  mkdir -p "$dir"
  echo "::group::Скачивание suite ротации учёток (релиз ${PLUGINS_SUITE_URL})"
  local base="https://github.com/${GITHUB_REPOSITORY}/releases/download/${PLUGINS_SUITE_URL}"
  local asset
  for asset in "$PLUGINS_SUITE_COMBO_ASSET" "$PLUGINS_SUITE_OAUTH_ASSET"; do
    if ! curl -fsSL --retry 3 --retry-delay 2 -o "$dir/$asset" "$base/$asset"; then
      echo "::error::vars.PLUGINS_SUITE_URL=${PLUGINS_SUITE_URL} задана, но ассет $asset недоступен ($base/$asset) — suite не установился (#215)"
      echo "::endgroup::"; return 1
    fi
    if ! curl -fsSL --retry 3 --retry-delay 2 -o "$dir/$asset.sha256" "$base/$asset.sha256"; then
      echo "::error::vars.PLUGINS_SUITE_URL=${PLUGINS_SUITE_URL} — в релизе нет ${asset}.sha256, целостность не проверить — suite не установился (#215)"
      echo "::endgroup::"; return 1
    fi
    if ! (cd "$dir" && sha256sum -c "$asset.sha256"); then
      echo "::error::sha256 $asset не сошёлся с $asset.sha256 — возможна подмена релиза или неполная закачка — suite не установился (#215)"
      echo "::endgroup::"; return 1
    fi
  done

  # dsh-combo-suite-*.tgz — не npm-пакет сам по себе (нет package.json в
  # корне), а обёртка: README + дубликат oauth-pool + вложенный
  # dsh-combo-router-*.tgz внутри каталога dsh-combo-router/ (проверено
  # распаковкой при подготовке #215). Реальный устанавливаемый пакет —
  # вложенный tgz.
  local combo_extract="$dir/combo-suite-extracted"
  mkdir -p "$combo_extract"
  tar -xzf "$dir/$PLUGINS_SUITE_COMBO_ASSET" -C "$combo_extract"
  local combo_pkg
  combo_pkg=$(find "$combo_extract" -name 'dsh-combo-router-*.tgz' | head -1)
  if [ -z "$combo_pkg" ]; then
    echo "::error::в $PLUGINS_SUITE_COMBO_ASSET не нашёлся вложенный dsh-combo-router-*.tgz — форма ассета изменилась, suite не установился (#215)"
    echo "::endgroup::"; return 1
  fi

  DSH_PLUGINS_SUITE_COMBO_PKG="$combo_pkg"
  DSH_PLUGINS_SUITE_OAUTH_PKG="$dir/$PLUGINS_SUITE_OAUTH_ASSET"
  DSH_PLUGINS_SUITE_ACTIVE=1
  echo "suite скачан и проверен (sha256 ок) — монтаж следующим шагом, после патча профиля"
  echo "::endgroup::"
}

# Монтаж suite в профиль — ОБЯЗАН вызываться ПОСЛЕ dsh_patch_profile (см.
# обоснование порядка в комментарии dsh_install_plugins_suite выше). Монтаж —
# через официальный документированный `dsh plugin --profile <p> add <spec>`
# (docs/research/10-dsh-architecture.md, «Монтаж плагина в профиль headless»
# — spec принимает путь к tarball'у, живым прогоном подтверждено 2026-08-30 в
# исследовании И повторно локально при подготовке #215). Факт монтажа
# проверяется командой (`dsh --profile <p> --dump-config`), не предположением.
#
# Сбой install/mount (сеть, dsh plugin add, отсутствие id в dump-config) —
# громкий возврат 1, критерий 6 #215. Отдельно от этого: если МОНТАЖ прошёл,
# но dump-config не подтверждает активацию (agent-default-model = combo/auto)
# — это НЕ критерий 6 (тот про install/mount), а мягкая деградация: откат на
# безопасный одиночный провайдер тем же патчем, видимый предупреждением, а не
# тихий возврат «как получилось».
dsh_mount_plugins_suite() { # $1 — профиль (headless)
  local profile=$1
  [ "${DSH_PLUGINS_SUITE_ACTIVE:-0}" = "1" ] || return 0
  echo "::group::Монтаж suite ротации учёток (профиль $profile)"
  if ! dsh plugin --profile "$profile" add "$DSH_PLUGINS_SUITE_COMBO_PKG"; then
    echo "::error::dsh plugin add не смонтировал dsh-combo-router — suite не смонтировался (#215)"
    echo "::endgroup::"; return 1
  fi
  # #1130 (находка ai-review PR #1132): свой собственный ассет suite —
  # ТОТ ЖЕ дистрибутив dsh-anthropic-oauth-pool, что и standalone-путь
  # (комментарий у PLUGINS_SUITE_OAUTH_ASSET выше), но НЕ пропущен через
  # dsh_patch_anthropic_pool_plugin (#1097/#1130) — self-регистрация в нём
  # активна и гонится со статической. Если standalone-путь УЖЕ активен
  # (DSH_ANTHROPIC_POOL_ACTIVE=1, значит секреты аккаунтов заданы и
  # dsh_patch_anthropic_pool_plugin уже отработал раньше по порядку вызовов
  # в worker/task.sh и hands/dsh_task.sh), suite НЕ монтирует свою
  # непатченную копию вовсе — dsh_mount_anthropic_pool (следующий шаг у
  # обоих вызывающих) смонтирует ЕДИНСТВЕННУЮ, уже патченную. Без секретов
  # (DSH_ANTHROPIC_POOL_ACTIVE=0) suite продолжает монтировать свою копию
  # как раньше — сегодня этот путь целиком дремлет (vars.PLUGINS_SUITE_URL
  # снята, #790), но останется корректным, когда suite вернут.
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" != "1" ]; then
    if ! dsh plugin --profile "$profile" add "$DSH_PLUGINS_SUITE_OAUTH_PKG"; then
      echo "::error::dsh plugin add не смонтировал dsh-anthropic-oauth-pool — suite не смонтировался (#215)"
      echo "::endgroup::"; return 1
    fi
  else
    echo "standalone-путь (#838/#1097/#1130) уже активен — suite пропускает свой (непатченный) oauth-add, dsh_mount_anthropic_pool смонтирует патченную копию следующим шагом"
  fi

  local dump
  if ! dump=$(dsh --profile "$profile" --dump-config 2>&1); then
    echo "::error::dsh --dump-config упал после монтажа suite — монтаж не подтверждён (#215): $dump"
    echo "::endgroup::"; return 1
  fi
  if ! grep -q '^- id: combo-router$' <<<"$dump"; then
    echo "::error::combo-router не найден в собранной композиции (dsh --dump-config) после dsh plugin add — монтаж не подтверждён (#215)"
    echo "::endgroup::"; return 1
  fi
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" != "1" ] && ! grep -q '^- id: anthropic-oauth-pool$' <<<"$dump"; then
    echo "::error::anthropic-oauth-pool не найден в собранной композиции (dsh --dump-config) после dsh plugin add — монтаж не подтверждён (#215)"
    echo "::endgroup::"; return 1
  fi

  if grep -q 'provider: combo' <<<"$dump" && grep -q 'model: auto' <<<"$dump"; then
    echo "ротация подключена: combo-router + anthropic-oauth-pool смонтированы, agent-default-model=combo/auto подтверждён dump-config"
  else
    echo "::warning::suite смонтирован, но dsh --dump-config не подтвердил активацию provider:combo/model:auto — откат на одиночный провайдер, ротация не подключена в этом прогоне (#215)"
    _dsh_patch_profile_plain "$profile"
  fi
  echo "::endgroup::"
}

# ── Быстрый провайдер Claude: anthropic-oauth-pool, независимо от suite ─────
# (#838, design.md anthropic-oauth-pool-standalone). НЕ читает
# vars.PLUGINS_SUITE_URL и не трогает DSH_PLUGINS_SUITE_*-переменные —
# отдельный набор DSH_ANTHROPIC_POOL_*, гейт — секреты аккаунтов, не vars.
#
# ТОЛЬКО скачивание+проверка+распаковка, ни одной команды `dsh` — тот же
# принцип разделения, что у dsh_install_plugins_suite/dsh_mount_plugins_suite
# выше. Ни один секрет ANTHROPIC_OAUTH_ACCOUNT_SECRETS не задан → notice,
# return 0, DSH_ANTHROPIC_POOL_ACTIVE=0 — поведение как раньше (одиночный
# провайдер/цепочка), симметрично критерию 4 #215, только газ — секрет.
#
# Распаковываем tgz В ДОПОЛНЕНИЕ к сохранению самого файла (для dsh plugin
# add): импорт аккаунтов (dsh_import_anthropic_accounts ниже) зовёт
# bin/dsh-anthropic-pool.js напрямую через node, а не через бинарник
# dsh-anthropic-pool, который `dsh plugin add` может не выставить в PATH
# (README плагина честно предупреждает об этом же).
dsh_install_anthropic_pool() { # $1 — рабочий каталог
  local dir=$1 secret_name has_secret=0
  DSH_ANTHROPIC_POOL_ACTIVE=0
  DSH_ANTHROPIC_POOL_PKG=""
  DSH_ANTHROPIC_POOL_EXTRACTED=""
  for secret_name in "${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[@]}"; do
    [ -n "${!secret_name:-}" ] && has_secret=1
  done
  if [ "$has_secret" != 1 ]; then
    echo "::notice::быстрый провайдер Claude (anthropic-oauth-pool) не подключён: ни один из секретов ${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[*]} не задан — используется цепочка/одиночный провайдер как раньше (#838)"
    return 0
  fi
  dsh_require_plugins_suite_repo
  mkdir -p "$dir"
  echo "::group::Скачивание dsh-anthropic-oauth-pool (релиз ${ANTHROPIC_OAUTH_POOL_RELEASE}, #838)"
  local base="https://github.com/${GITHUB_REPOSITORY}/releases/download/${ANTHROPIC_OAUTH_POOL_RELEASE}"
  if ! curl -fsSL --retry 3 --retry-delay 2 -o "$dir/$PLUGINS_SUITE_OAUTH_ASSET" "$base/$PLUGINS_SUITE_OAUTH_ASSET"; then
    echo "::error::ассет $PLUGINS_SUITE_OAUTH_ASSET недоступен ($base/$PLUGINS_SUITE_OAUTH_ASSET) — быстрый провайдер Claude не установился (#838)"
    echo "::endgroup::"; return 1
  fi
  if ! curl -fsSL --retry 3 --retry-delay 2 -o "$dir/$PLUGINS_SUITE_OAUTH_ASSET.sha256" "$base/$PLUGINS_SUITE_OAUTH_ASSET.sha256"; then
    echo "::error::в релизе ${ANTHROPIC_OAUTH_POOL_RELEASE} нет ${PLUGINS_SUITE_OAUTH_ASSET}.sha256 — целостность не проверить, быстрый провайдер Claude не установился (#838)"
    echo "::endgroup::"; return 1
  fi
  if ! (cd "$dir" && sha256sum -c "$PLUGINS_SUITE_OAUTH_ASSET.sha256"); then
    echo "::error::sha256 $PLUGINS_SUITE_OAUTH_ASSET не сошёлся с $PLUGINS_SUITE_OAUTH_ASSET.sha256 — возможна подмена релиза или неполная закачка (#838)"
    echo "::endgroup::"; return 1
  fi
  local extract_dir="$dir/anthropic-oauth-pool-extracted"
  mkdir -p "$extract_dir"
  if ! tar -xzf "$dir/$PLUGINS_SUITE_OAUTH_ASSET" -C "$extract_dir"; then
    echo "::error::tar не распаковал $PLUGINS_SUITE_OAUTH_ASSET (#838)"
    echo "::endgroup::"; return 1
  fi
  if [ ! -f "$extract_dir/package/bin/dsh-anthropic-pool.js" ]; then
    echo "::error::в $PLUGINS_SUITE_OAUTH_ASSET не нашёлся package/bin/dsh-anthropic-pool.js — форма ассета изменилась, быстрый провайдер Claude не установился (#838)"
    echo "::endgroup::"; return 1
  fi
  DSH_ANTHROPIC_POOL_PKG="$dir/$PLUGINS_SUITE_OAUTH_ASSET"
  DSH_ANTHROPIC_POOL_EXTRACTED="$extract_dir/package"
  DSH_ANTHROPIC_POOL_ACTIVE=1
  echo "быстрый провайдер Claude (anthropic-oauth-pool) скачан и проверен (sha256 ок)"
  echo "::endgroup::"
}

# Нейтрализация self-регистрации плагина в llm-pi-ai (#1097 второй заход,
# #1130, живой прогон worker.yml 34753001158) + фикс превентивного рефреша
# долгоживущих токенов (#1130, доработка по решению владельца).
#
# Патч 1 (ensureProvider, lib/index.js): плагин пишет провайдера в llm-pi-ai
# через ctx.get('settings') — сервис settings ДЕЙСТВИТЕЛЬНО смонтирован в
# headless (проверено живым `dsh --dump-config`: `@deepseek-ai/dsh-settings-file`
# — прежнее утверждение обратного в design.md было неверным, см.
# docs/research/32-claude-oauth-provider.md, «Поправка 2026-09-13»), поэтому
# self-регистрация РАБОТАЕТ и гонится с нашей статической регистрацией
# llm-pi-ai.providers.anthropic-pool (_dsh_patch_profile_anthropic_pool
# ниже): если discoverModels() плагина успевает получить РЕАЛЬНЫЙ каталог
# моделей аккаунта раньше первого запроса агента, settings.update() (MERGE
# по объектам, но ЗАМЕНА массивов целиком, dsh-settings::mergeLayers)
# перезаписывает наш models статическим — при отсутствии в реальном
# каталоге буквального id "claude-sonnet-4-5" результат — UNKNOWN_MODEL.
# Фикс — вырезать саму self-регистрацию: наша статическая регистрация
# полная и единственная, плагину незачем писать в settings вовсе.
#
# Патчи 2+3 (lib/pool.js + lib/index.js): плагин трактовал ОТСУТСТВИЕ
# `oauth.expiresAt` как «токен истёк» (`createRefreshCoordinator`,
# pool.js) — превентивный рефреш на КАЖДЫЙ запрос для любого аккаунта без
# явного expiresAt, в том числе для долгоживущего accessToken владельца,
# которому вообще не нужен рефреш. Патч 2 переворачивает условие: нет
# expiresAt → токен считается валидным, короткоживущие токены (expiresAt
# задан) ведут себя как раньше. Патч 3 — обязательная пара: реактивный
# рефреш на РЕАЛЬНЫЙ 401/403 (без него патч 2 убрал бы единственный путь
# восстановления по-настоящему протухшего токена без expiresAt).
#
# Все патчи — exact string match в scripts/lib/patch_anthropic_pool_plugin.py,
# fail loud при несовпадении формы — апстрим сменился, применяются к УЖЕ
# распакованному и sha256-проверенному каталогу (DSH_ANTHROPIC_POOL_EXTRACTED,
# после dsh_install_anthropic_pool), результат репакуется в НОВЫЙ tgz —
# DSH_ANTHROPIC_POOL_PKG после этой функции указывает на патченный архив,
# dsh_mount_anthropic_pool монтирует именно его.
dsh_patch_anthropic_pool_plugin() {
  [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ] || return 0
  echo "::group::Патч плагина anthropic-oauth-pool: нейтрализация self-регистрации + фикс рефреша долгоживущих токенов + причина pool_unavailable (#1097/#1130/#1192)"
  local script_dir patched_tgz
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ ! -d "$DSH_ANTHROPIC_POOL_EXTRACTED" ]; then
    echo "::error::$DSH_ANTHROPIC_POOL_EXTRACTED не найден — dsh_install_anthropic_pool не отработал раньше (#1130)"
    echo "::endgroup::"; return 1
  fi
  if ! python3 "$script_dir/patch_anthropic_pool_plugin.py" "$DSH_ANTHROPIC_POOL_EXTRACTED"; then
    echo "::error::патч плагина не применился (см. вывод выше) — НЕ пофиксены: self-регистрация и/или превентивный рефреш долгоживущих токенов (#1097/#1130) и/или причина pool_unavailable не прокидывается (#1192)"
    echo "::endgroup::"; return 1
  fi
  patched_tgz="$(dirname "$DSH_ANTHROPIC_POOL_EXTRACTED")/dsh-anthropic-oauth-pool-patched.tgz"
  if ! tar -czf "$patched_tgz" -C "$(dirname "$DSH_ANTHROPIC_POOL_EXTRACTED")" "$(basename "$DSH_ANTHROPIC_POOL_EXTRACTED")"; then
    echo "::error::репак патченного плагина в $patched_tgz не удался (#1130)"
    echo "::endgroup::"; return 1
  fi
  DSH_ANTHROPIC_POOL_PKG="$patched_tgz"
  echo "плагин патчен и репакован: $patched_tgz (dsh_mount_anthropic_pool смонтирует его вместо оригинального ассета)"
  echo "::endgroup::"
}

# Импорт аккаунтов из секретов (#838) — вызывать ПОСЛЕ dsh_install_anthropic_pool
# (нужен DSH_ANTHROPIC_POOL_EXTRACTED), в любой момент до первого прогона dsh:
# bin/dsh-anthropic-pool.js пишет напрямую в ~/.dsh/anthropic-accounts (lib/
# accounts.js::poolDir, по умолчанию $HOME) — тот же $HOME, что видит
# смонтированный плагин в этом job'е, порядок относительно монтажа не важен.
#
# Значение секрета НИКОГДА не печатается и не проходит через echo/интерполяцию
# в аргументы команды — только файл mode 0600 (AGENTS.md, «Секреты»: репозиторий
# публичный, производное секрета GitHub не маскирует). id аккаунта — позиционный
# (anthropic-1 для ANTHROPIC_OAUTH_1, anthropic-2 для ANTHROPIC_OAUTH_2).
#
# unset КАЖДОГО секрета после использования — НЕ просто гигиена. Имена
# ANTHROPIC_OAUTH_1/2 (заданы владельцем, не переименовываются здесь) не
# содержат подстрок *_KEY/*_TOKEN/*_SECRET — паттерна, которым DSH вырезает
# переменные из окружения ПОДПРОЦЕССОВ, запускаемых shell-тулом самой модели
# (см. комментарий в scripts/review/ai_dsh.sh, «Доверенная граница задачи
# #18»: DEEPSEEK_API_KEY УЖЕ полагается на этот паттерн, ANTHROPIC_OAUTH_1/2 —
# НЕТ). ai-review запускает `dsh` над НЕДОВЕРЕННЫМ диффом PR без GH_TOKEN
# ИМЕННО чтобы агент не мог ничего слить наружу через свой же shell-тул —
# живой OAuth-JSON (accessToken/refreshToken) в этом окружении был бы
# читаем `env`/`printenv` изнутри агента, если бы остался в процессе `dsh`
# дольше момента импорта. Импорт (dsh-anthropic-pool add, отдельный
# `node`-процесс) уже прочитал значение и записал его в файл на диске —
# после этого секрет из окружения ЭТОГО bash-процесса (и, следовательно, из
# окружения любого дочернего `dsh`, стартующего позже) больше не нужен.
#
# Изоляция битого аккаунта (#859, живой инцидент PR #858, 2026-09-10): в
# ANTHROPIC_OAUTH_1 попал ведущий UTF-8 BOM (EF BB BF) — `node ... add`
# падал SyntaxError ДО того, как строка дошла до JSON.parse, старая версия
# этой функции отвечала return 1 и роняла ВЕСЬ шаг ai-review дважды подряд,
# хотя цепочка из 7 живых провайдеров (config/provider-usage.json) рядом и
# рабочая. Пул — НЕ критичный провайдер (design.md
# anthropic-oauth-pool-standalone): один битый секрет обязан быть пропущен с
# внятным ::warning::, а не ронять потребителя. BOM снимается здесь же, у
# единственного места, где секрет читается как JSON. Разбор/валидация идут
# через jq ЗДЕСЬ, в bash, а не полагаются на текст stderr node — так лог не
# рискует напечатать производное секрета (AGENTS.md «Секреты»: GitHub
# маскирует только точное совпадение значения).
dsh_import_anthropic_accounts() {
  [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ] || return 0
  echo "::group::Импорт аккаунтов Anthropic OAuth pool (#838)"
  local secret_name value creds_file imported=0 skipped=0 idx=0 account_id
  for secret_name in "${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[@]}"; do
    idx=$((idx + 1))
    account_id="anthropic-$idx"
    value="${!secret_name:-}"
    if [ -z "$value" ]; then
      continue
    fi
    # Снять ведущий UTF-8 BOM (EF BB BF), если он есть — живая форма
    # инцидента #859. Байтовый префикс, не зависит от locale.
    value="${value#$'\xef\xbb\xbf'}"

    if ! jq -e . >/dev/null 2>&1 <<<"$value"; then
      echo "::warning::секрет $secret_name — невалидный JSON (даже после снятия BOM), аккаунт $account_id пропущен, пул продолжает с остальными аккаунтами/цепочкой (#859)"
      unset "$secret_name"
      skipped=$((skipped + 1))
      continue
    fi
    # #1311: обязателен ТОЛЬКО accessToken. refreshToken до этой правки был
    # обязательным здесь, в bin/dsh-anthropic-pool.js (accounts.js плагина) и
    # в pool.js — три гейта подряд отвергали долгоживущий токен без рефреша,
    # хотя ровно такой токен работает (docs/research/32-claude-oauth-provider.md:
    # llm-pi-ai держит `sk-ant-oat` Bearer'ом без всякого рефреша; #1130:
    # «долгоживущий accessToken владельца, которому вообще не нужен рефреш»).
    # Два нижних гейта снимают патчи 5/6 (patch_anthropic_pool_plugin.py),
    # этот — здесь.
    if ! jq -e '(.claudeAiOauth // .oauth // {}) as $o | ($o.accessToken // "") | length > 0' >/dev/null 2>&1 <<<"$value"; then
      echo "::warning::секрет $secret_name — валидный JSON, но без claudeAiOauth.accessToken, аккаунт $account_id пропущен (#859/#1311)"
      unset "$secret_name"
      skipped=$((skipped + 1))
      continue
    fi
    if ! jq -e '(.claudeAiOauth // .oauth // {}) as $o | ($o.refreshToken // "") | length > 0' >/dev/null 2>&1 <<<"$value"; then
      # Не отказ: факт называется, чтобы при будущем 401 было видно, что
      # автоматического восстановления у этого аккаунта нет по построению.
      echo "::notice::секрет $secret_name несёт accessToken без refreshToken — это рабочий случай долгоживущего токена (#1311); автоматического обновления у аккаунта $account_id не будет, при отказе доступа нужен новый токен от владельца"
    fi

    creds_file=$(mktemp)
    chmod 600 "$creds_file"
    printf '%s' "$value" >"$creds_file"
    if ! node "$DSH_ANTHROPIC_POOL_EXTRACTED/bin/dsh-anthropic-pool.js" add "$account_id" "$creds_file" >/dev/null 2>&1; then
      # Защитная ветка: базовая проверка выше уже отсекла BOM/невалидный
      # JSON/отсутствующие поля — сюда попадает то, что jq не проверяет
      # (например safeId плагина). Тот же приём: пропустить, не падать.
      rm -f "$creds_file"
      unset "$secret_name"
      echo "::warning::dsh-anthropic-pool add $account_id отказал, хотя секрет $secret_name прошёл базовую проверку — аккаунт пропущен, пул продолжает (#859)"
      skipped=$((skipped + 1))
      continue
    fi
    rm -f "$creds_file"
    unset "$secret_name"
    imported=$((imported + 1))
    echo "аккаунт $account_id импортирован из секрета $secret_name (значение удалено из окружения)"
  done
  if [ "$imported" = 0 ]; then
    # Раньше считалось недостижимым (dsh_install_anthropic_pool уже проверил
    # has_secret) — теперь достижимо: has_secret проверяет только непустоту,
    # не валидность. Все секреты оказались битыми (skipped>0) — пул тихо
    # отключается для ЭТОГО прогона, потребитель уходит на цепочку/одиночный
    # провайдер как раньше, не падает (#859).
    echo "::warning::ни один секрет ${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[*]} не дал импортируемый аккаунт ($skipped пропущено) — быстрый провайдер Claude отключается для этого прогона, используется цепочка/одиночный провайдер (#838, #859)"
    DSH_ANTHROPIC_POOL_ACTIVE=0
    echo "::endgroup::"
    return 0
  fi
  echo "::endgroup::"
}

# ── Предполётная проверка аккаунтов пула (#1311) ───────────────────────────
#
# Вызывать ПОСЛЕ dsh_import_anthropic_accounts (нужны файлы аккаунтов) и до
# первого прогона. Отвечает в логе на вопрос, на который агрегат
# `pool_unavailable`/`reason` ответить не может в принципе: КАКОЙ ИМЕННО из
# аккаунтов пригоден сейчас и почему непригодны остальные.
#
# Живая цена отсутствия: прогоны worker.yml 34942030597/35010410097
# (2026-09-15) час подряд печатали `reason: rate_limited` и звали «ждать
# сброса квоты», в то время как владелец в это же время работал на втором
# аккаунте. Класс отказа «аккаунт выбыл на рефреше» (ветка catch вокруг
# ensureFresh, lib/index.js плагина: cooldown 15с + lastError) стоит в
# приоритете classifyPoolUnavailable НИЖЕ `rate_limited` соседа и снаружи
# неотличим от квоты.
#
# НЕ гейт: любой исход возвращает 0 и ничего не роняет — пул не критичный
# провайдер (design.md anthropic-oauth-pool-standalone), а проверка нужна
# ради факта в логе и ради того, чтобы пул не тратил попытку на заведомо
# непригодный аккаунт. Отказ самой проверки (node упал, сеть недоступна) —
# ::warning::, работа продолжается ровно как раньше.
dsh_pool_preflight() {
  [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ] || return 0
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo "::group::Предполётная проверка аккаунтов Claude (#1311)"
  if ! node "$script_dir/anthropic_pool_preflight.mjs" "$DSH_ANTHROPIC_POOL_EXTRACTED"; then
    echo "::warning::предполётная проверка аккаунтов пула не отработала — прогон продолжается как раньше, но какой из аккаунтов пригоден, в логе не сказано (#1311)"
  fi
  echo "::endgroup::"
  return 0
}

# Монтаж — тот же паттерн, что dsh_mount_plugins_suite (структурная проверка
# dsh --dump-config), НЕЗАВИСИМО от неё: своя переменная активации, свой tgz,
# монтируется даже когда suite выключена целиком. Вызывать ПОСЛЕ первого
# dsh_patch_profile этого job'а — то же обоснование порядка (initProfile
# пишет файлы профиля только при отсутствии), что у dsh_mount_plugins_suite.
dsh_mount_anthropic_pool() { # $1 — профиль (headless)
  local profile=$1
  [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ] || return 0
  echo "::group::Монтаж быстрого провайдера Claude (профиль $profile, #838)"
  if ! dsh plugin --profile "$profile" add "$DSH_ANTHROPIC_POOL_PKG"; then
    echo "::error::dsh plugin add не смонтировал dsh-anthropic-oauth-pool — быстрый провайдер Claude не подключён (#838)"
    echo "::endgroup::"; return 1
  fi
  local dump
  if ! dump=$(dsh --profile "$profile" --dump-config 2>&1); then
    echo "::error::dsh --dump-config упал после монтажа anthropic-oauth-pool — монтаж не подтверждён (#838): $dump"
    echo "::endgroup::"; return 1
  fi
  if ! grep -q '^- id: anthropic-oauth-pool$' <<<"$dump"; then
    echo "::error::anthropic-oauth-pool не найден в собранной композиции (dsh --dump-config) после dsh plugin add — монтаж не подтверждён (#838)"
    echo "::endgroup::"; return 1
  fi
  echo "быстрый провайдер Claude (anthropic-oauth-pool) смонтирован — структурная проверка dump-config подтверждена"
  echo "::endgroup::"
}

# Плоский (без combo-router) патч профиля — поведение «как раньше», один
# источник для ОБЕИХ ситуаций, где он нужен: suite не запрошен вовсе, и
# suite смонтирован, но dump-config не подтвердил активацию (мягкий откат,
# dsh_mount_plugins_suite). Не публичная функция первого выбора — дергать
# напрямую нет смысла вне этих двух мест, но и не re-declare внутри каждого.
_dsh_patch_profile_plain() { # $1 — профиль
  local profile=$1
  local patch="$HOME/.dsh/profiles/$profile/cordis.patch.yml"
  mkdir -p "$(dirname "$patch")"
  cat >"$patch" <<PATCH
- id: agent-default-model
  config:
    provider: deepseek-official
    model: $DSH_MODEL
- id: llm-deepseek
  config:
    maxTokens: $DSH_MAX_TOKENS
PATCH
}

# Патч профиля под быстрый провайдер Claude (#838, design.md
# anthropic-oauth-pool-standalone «Стык с цепочкой провайдеров»). НЕ
# использует _dsh_patch_profile_plain/llm-deepseek вовсе — anthropic-pool
# живёт как отдельный провайдер llm-pi-ai.providers.anthropic-pool,
# api: 'anthropic-messages' — другой протокольный путь, не openai-completions
# llm-deepseek. "claude-sonnet-4-5" — статический дефолт из models плагина
# (lib/index.js), используется как id модели для agent-default-model; после
# #1130 (нейтрализация self-регистрации, см. dsh_patch_anthropic_pool_plugin
# ниже) с этим id больше некому конкурировать — он остаётся единственным
# источником каталога моделей провайдера, а не гонится с живым discoverModels().
#
# #1097 (живой инцидент, прогон worker.yml 34746091297): раньше эта функция
# писала ТОЛЬКО agent-default-model и полагалась на то, что сам плагин
# пропишет llm-pi-ai.providers.anthropic-pool через
# ctx.get('settings').update(...) в своём ensureProvider() (lib/index.js
# плагина) — та запись падала TypeError ДО регистрации провайдера, и
# agent-default-model резолвил несуществующего "anthropic-pool" → NO_ADAPTER.
#
# ОПРОВЕРГНУТО #1130: первая версия этого разбора утверждала, что сервис
# settings НИКОГДА не смонтирован в headless (якобы ни dsh-headless, ни
# что-либо ещё, реально идущее в headless-бандл, не тянет dsh-settings как
# рантайм-зависимость). Живой прогон worker.yml 34753001158 (уже с фиксом
# ниже) сменил ошибку на UNKNOWN_MODEL — факт, недостижимый при недоступном
# settings. Реальная причина: settings (@deepseek-ai/dsh-settings-file)
# смонтирован через dsh-base (общий нижний слой всех профилей, не через
# dsh-headless) — разбор: docs/research/32-claude-oauth-provider.md,
# «Поправка 2026-09-13». Сам HTTP-прокси плагина (server.listen,
# аутентификация/failover между аккаунтами) при этом всегда стартовал и
# работал нормально — ensureProvider() вызывается АСИНХРОННО после listen,
# её отказ ловится .catch() плагина и не роняет процесс.
#
# Фикс #1097 — статическая регистрация провайдера ЗДЕСЬ, тем же путём, что
# уже использует dsh_patch_profile для combo-router (llm-pi-ai.config.providers
# напрямую в cordis.patch.yml). Порт прокси ФИКСИРОВАН
# (ANTHROPIC_OAUTH_POOL_PORT, объявлен у констант выше) — иначе baseURL
# нельзя узнать ДО старта процесса dsh (сервер сам выбирает порт в момент
# listen, а патч пишется ДО того, как этот процесс стартовал). apiKeyEnv —
# переменная с заведомо непустым значением (ANTHROPIC_OAUTH_POOL_APIKEY_ENV),
# реальная авторизация решается прокси-сервером плагина, не этим полем (см.
# комментарий у константы).
#
# Фикс #1130 — раз settings реально доступен (см. «ОПРОВЕРГНУТО» выше),
# self-регистрация плагина продолжала работать ПАРАЛЛЕЛЬНО этой статической
# и гналась с ней (settings.update — merge по объектам, но models как
# массив заменяется ЦЕЛИКОМ): dsh_patch_anthropic_pool_plugin нейтрализует
# ensureProvider() плагина целиком, эта функция остаётся ЕДИНСТВЕННЫМ
# источником регистрации.
_dsh_patch_profile_anthropic_pool() { # $1 — профиль
  local profile=$1
  local patch="$HOME/.dsh/profiles/$profile/cordis.patch.yml"
  mkdir -p "$(dirname "$patch")"
  export DSH_ANTHROPIC_POOL_PORT="$ANTHROPIC_OAUTH_POOL_PORT"
  export "${ANTHROPIC_OAUTH_POOL_APIKEY_ENV}=managed-by-anthropic-pool"
  cat >"$patch" <<PATCH
- id: agent-default-model
  config:
    provider: anthropic-pool
    model: claude-sonnet-4-5
- id: llm-pi-ai
  config:
    providers:
      anthropic-pool:
        displayName: "Anthropic OAuth Pool (loopback-прокси плагина, #1097)"
        api: anthropic-messages
        baseURL: http://127.0.0.1:$ANTHROPIC_OAUTH_POOL_PORT
        apiKeyEnv: $ANTHROPIC_OAUTH_POOL_APIKEY_ENV
        models:
          - id: claude-sonnet-4-5
            name: Claude Sonnet 4.5
            contextWindow: 200000
            maxTokens: 64000
PATCH
}

# Окно контекста env-provider'а для combo-router — ОТДЕЛЬНАЯ величина от
# DSH_MAX_TOKENS (тот — потолок ДЛИНЫ ОТВЕТА, adapter-конфиг llm-deepseek
# maxTokens; #789, живой прогон: contextWindow=DSH_MAX_TOKENS=131072 у
# glm-5.3-flash при реальном окне 1000000 отбрасывал маршрут вчетверо раньше
# нужного — combo-router::compatible() кидает NO_COMBO_ROUTE при
# contextTokens > contextWindow*0.92). Источник — vars.DSH_EDGE_MODEL_CATALOG:
# та же переменная, которой уже требует присутствие vars.DEEPSEEK_MODEL
# каталог морды (deploy-dsh-edge.yml, «Патч каталога моделей под реального
# провайдера») — не второе место правды, то же самое значение. Модель вне
# каталога (запись отсутствует, например при ручном тесте другой модели) —
# консервативный фолбэк на DSH_MAX_TOKENS: не хуже прежнего поведения (оно и
# было таким для всех моделей) и не занижает относительно старого поведения,
# только не расширяет окно за пределы неподтверждённого значения.
dsh_model_context_window() { # $1 — id модели
  local model=$1 cw=""
  if [ -n "${DSH_EDGE_MODEL_CATALOG:-}" ]; then
    cw=$(jq -r --arg id "$model" \
      '([.[]? | select(.id == $id) | .contextWindow][0]) // empty' \
      <<<"$DSH_EDGE_MODEL_CATALOG" 2>/dev/null || true)
  fi
  if [ -z "$cw" ] || [ "$cw" = "null" ]; then
    echo "::warning::окно контекста модели '$model' не найдено в vars.DSH_EDGE_MODEL_CATALOG — использую консервативный фолбэк $DSH_MAX_TOKENS (потолок вывода, может быть меньше реального окна контекста)" >&2
    cw="$DSH_MAX_TOKENS"
  fi
  echo "$cw"
}

# Выбор модели и лимит ответа — через родной settings-слой профиля, НЕ env:
# адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
#
# Вызывать ДО dsh_mount_plugins_suite (порядок обоснован там) — сам патч
# читает DSH_PLUGINS_SUITE_ACTIVE, выставляемый dsh_install_plugins_suite
# (скачивание), который в свою очередь обязан отработать раньше.
#
# СТЫК с цепочкой провайдеров (#727/#737, dsh_run_with_provider_chain ниже):
# та зовёт ЭТУ ЖЕ функцию НА КАЖДОЙ попытке провайдера, экспортировав перед
# этим DEEPSEEK_BASE_URL/MODEL/API_KEY КОНКРЕТНОГО провайдера — ожидая, что
# патч сконфигурирует именно его, а не какой-то другой. Suite (#215) решает
# ТОТ ЖЕ вопрос («кого пробовать») на своём уровне — комбинировать их напрямую
# нельзя: если бы combo/auto включался и внутри цепочки, DSH_CHAIN_PROVIDER/
# DSH_CHAIN_TRIED атрибутировали бы попытку не тому провайдеру, который её
# реально обслужил, а реестр подтверждённых моделей (#737, dsh_model_confirmed)
# вообще не видит маршруты suite — то есть цепочка и suite решают одно и то
# же на разных уровнях, и слепое объединение тихо ломает оба тормоза чейна.
# Решение (design.md dsh-in-job, «Стык suite и цепочки провайдеров»): ВНУТРИ
# цепочки suite всегда глушится — dsh_run_with_provider_chain выставляет
# DSH_CHAIN_ACTIVE=1 на время своего цикла, и эта функция читает его ниже,
# независимо от DSH_PLUGINS_SUITE_ACTIVE. Вне цепочки (#797: сегодня это
# только hands.yml — worker.yml с этой задачи тоже зовёт
# dsh_run_with_provider_chain) suite работает как раньше.
# Дополнительный тормоз — dsh_require_provider_chain отказывает громко, если
# vars.PLUGINS_SUITE_URL и vars.DSH_PROVIDER_CHAIN заданы одновременно: молчаливого
# приоритета одной переменной над другой быть не должно.
dsh_patch_profile() { # $1 — имя профиля (обычно headless); выставляет DSH_MODEL/DSH_MAX_TOKENS
  local profile=$1
  # Модель обязана прийти из окружения (vars.DEEPSEEK_MODEL, #153) — здесь
  # больше нет зашитого дефолта. Вызывающий обязан вызвать
  # dsh_require_provider_env раньше и упасть громко, если модель не задана.
  : "${DEEPSEEK_MODEL:?DEEPSEEK_MODEL не задан — dsh_require_provider_env должен был отказать раньше}"
  DSH_MODEL="$DEEPSEEK_MODEL"
  DSH_MAX_TOKENS="${DSH_MAX_TOKENS:-131072}"
  local patch="$HOME/.dsh/profiles/$profile/cordis.patch.yml"
  mkdir -p "$(dirname "$patch")"

  if [ "${DSH_CHAIN_ACTIVE:-0}" = "1" ] && [ "${DSH_PLUGINS_SUITE_ACTIVE:-0}" = "1" ]; then
    # Защитный, а не ожидаемый путь: dsh_require_provider_chain уже должен был
    # отказать раньше, если обе переменные заданы разом (см. комментарий выше).
    # Если сюда всё же дошли — не молчим о том, что suite для этой попытки
    # игнорируется, а не тихо подменяет провайдера цепочки.
    echo "::warning::цепочка провайдеров (#727) игнорирует suite ротации учёток (#215) внутри своего цикла — комбинация не поддержана конструктивно (design.md dsh-in-job, «Стык suite и цепочки провайдеров»), пишу плоский патч на $DSH_MODEL" >&2
  fi

  if [ "${DSH_PLUGINS_SUITE_ACTIVE:-0}" != "1" ] || [ "${DSH_CHAIN_ACTIVE:-0}" = "1" ]; then
    # suite не скачан (переменная не задана либо dsh_install_plugins_suite
    # ещё не вызывалась/не удался), ЛИБО эта попытка идёт внутри цепочки
    # провайдеров — поведение как раньше, без combo-router.
    _dsh_patch_profile_plain "$profile"
    return 0
  fi

  # Suite скачан и проверен (dsh_install_plugins_suite отработал, монтаж
  # dsh_mount_plugins_suite — следующим шагом, ПОСЛЕ этой функции). Контракт
  # combo-router прочитан из его исходников (#215, не угадан):
  # - виртуальная модель — provider: combo / model: auto
  #   (dsh-combo-router/lib/index.js: ComboAdapter.providerInfo/resolveModel);
  # - маршруты идут через дремлющий базовый сервис llm-pi-ai (id подтверждён
  #   живым `dsh --dump-config`, не предположением) — providers.<id>.apiKeyEnv
  #   называет ИМЯ переменной окружения, не значение;
  # - `enabled: true` с пустым routes падает конструктором роутера
  #   («combo-router: enabled router needs at least one route»,
  #   dsh-combo-router/lib/router.js) — поэтому здесь ВСЕГДА есть маршрут
  #   env-provider (переиспользует уже обязательные DEEPSEEK_*, см.
  #   dsh_require_provider_env) плюс любые маршруты из
  #   PLUGINS_SUITE_CANDIDATE_ROUTES, чей apiKeyEnv фактически задан —
  #   владелец добавляет провайдера созданием секрета с этим именем, без
  #   правки кода.
  local providers_yaml routes_yaml entry alias url keyenv model ctx label keyval
  local env_ctx_window
  env_ctx_window=$(dsh_model_context_window "$DSH_MODEL")
  providers_yaml="      env-provider:
        displayName: \"vars.DEEPSEEK_BASE_URL (env-provider)\"
        api: openai-completions
        baseURL: $DEEPSEEK_BASE_URL
        apiKeyEnv: DEEPSEEK_API_KEY
        models:
          - id: $DSH_MODEL
            contextWindow: $env_ctx_window"
  routes_yaml="      env-provider:
        provider: env-provider
        model: $DSH_MODEL
        tasks: [general, coding, research, reasoning, simple, long-context]
        quality: 80
        cost: 0.3
        latency: 0.4
        contextWindow: $env_ctx_window
        tools: true
        group: env-provider"

  for entry in "${PLUGINS_SUITE_CANDIDATE_ROUTES[@]}"; do
    IFS='|' read -r alias url keyenv model ctx label <<<"$entry"
    keyval="${!keyenv:-}"
    [ -n "$keyval" ] || continue
    providers_yaml="$providers_yaml
      $alias:
        displayName: \"$label\"
        api: openai-completions
        baseURL: $url
        apiKeyEnv: $keyenv
        models:
          - id: $model
            contextWindow: $ctx"
    routes_yaml="$routes_yaml
      $alias:
        provider: $alias
        model: $model
        tasks: [general, coding, research, reasoning, simple, long-context]
        quality: 80
        cost: 0.3
        latency: 0.4
        contextWindow: $ctx
        tools: true
        group: $alias"
  done

  cat >"$patch" <<PATCH
- id: agent-default-model
  config:
    provider: combo
    model: auto
- id: llm-deepseek
  config:
    maxTokens: $DSH_MAX_TOKENS
- id: llm-pi-ai
  config:
    providers:
$providers_yaml
- id: combo-router
  config:
    enabled: true
    mode: auto
    virtualProvider: combo
    virtualModel: auto
    failureThreshold: 2
    cooldownMs: 120000
    authCooldownMs: 3600000
    maxFallbacks: 9
    routes:
$routes_yaml
PATCH
}

# Ретрай временного RATE_LIMIT провайдера вокруг ОДНОГО прогона dsh (#422,
# наследует механизм #419/#421 — там он появился только у ai-review, хотя
# первым от лимита падает автономный воркер: живой факт — прогоны worker.yml
# 34007508064/34006554580 упали с «dsh: RATE_LIMIT: Rate limit reached for
# requests», ai-review в тот момент ретраить ещё не умел вовсе). Единственное
# место правды: ai_dsh.sh (ревью), worker/task.sh, hands/dsh_task.sh зовут эту
# функцию вместо копии цикла — три расходящиеся копии дороже одной.
#
# Классификация по тексту stderr (docs/runbooks/switch-llm-provider.md):
#   «RATE_LIMIT: Weekly/Monthly Limit Exhausted…» — квота на дни, ретраить
#     внутри прогона бессмысленно — падаем сразу (failure_reason=quota_exhausted).
#   «RATE_LIMIT: …» без этой формы — короткое окно, снимается ожиданием:
#     экспоненциальная пауза с потолком и общим бюджетом ожидания.
#   Нет строки RATE_LIMIT вовсе — настоящая ошибка (ключ/модель/битый
#     запрос/сеть) — падаем сразу, как и раньше.
#
# Использование:
#   dsh_run_with_retry <answer_file> <err_file> <prompt_text>
# Настройки — через env (у каждого вызывающего свой бюджет и обоснование,
# см. worker.yml/hands.yml/ai-review.yml):
#   DSH_TIMEOUT_SECS               — таймаут КАЖДОЙ попытки (вызывающий уже задаёт)
#   DSH_RATE_LIMIT_MAX_WAIT_SECS   — суммарный бюджет ожидания (по умолчанию 1800 — 30 мин)
#   DSH_RATE_LIMIT_INITIAL_DELAY_SECS / DSH_RATE_LIMIT_MAX_DELAY_SECS — старт/потолок паузы
# Результат (переменные, не stdout — вызывающий печатает свой отчёт):
#   DSH_RUN_RC              — код возврата ПОСЛЕДНЕЙ попытки dsh
#   DSH_RUN_FAILURE_REASON  — "" (успех/обычный транспортный отказ) |
#                             quota_exhausted | rate_limit_retry_budget_exceeded
#   DSH_RUN_WAITED_SECS     — суммарно проспано в ретрае RATE_LIMIT за ЭТОТ
#                             вызов (#877): цепочка провайдеров читает его,
#                             чтобы уменьшать ОБЩИЙ бюджет ожидания на
#                             следующего провайдера, а не выдавать каждому
#                             полный бюджет заново (см. dsh_run_with_provider_chain)
# ── Промпт несколькими аргументами, без потери байта (#1318) ───────────────
#
# Заполняет массив DSH_PROMPT_ARGV кусками промпта так, чтобы
# `args.join(" ")` на стороне dsh-headless (lib/startup.js: задача объявлена
# `[task...]` и собирается именно join'ом) дал ИСХОДНЫЙ текст байт-в-байт.
#
# Отсюда единственно допустимая точка реза — ОДИНОЧНЫЙ пробел: он снимается
# здесь и возвращается join'ом. Резать по переводу строки нельзя: join вернул
# бы на его место пробел, и текст изменился бы молча (ровно тот silent-wrong,
# который в этом репозитории дороже падения).
#
# Второе ограничение — кусок не должен начинаться с `-`: commander на стороне
# dsh принял бы его за неизвестную опцию. Поэтому подходящим считается пробел,
# за которым идёт НЕ дефис.
#
# Промпт короче куска — ровно один элемент массива, то есть побайтно прежний
# вызов: до #1318 так шли все прогоны, и эта ветка сохраняет их поведение.
dsh_prompt_argv_chunks() { # prompt_text -> заполняет массив DSH_PROMPT_ARGV
  local text=$1 chunk_max="${DSH_PROMPT_CHUNK_BYTES:-100000}"
  DSH_PROMPT_ARGV=()
  DSH_PROMPT_SPLIT_FAILED=0
  # LC_ALL=C: длины и индексы обязаны быть В БАЙТАХ, а не в символах —
  # предел ядра байтовый, а промпт кириллический (символ = два байта).
  local LC_ALL=C LANG=C
  while [ "${#text}" -gt "$chunk_max" ]; do
    local cut=-1 i
    # Назад от границы до пробела, за которым не дефис.
    for (( i = chunk_max; i > 0; i-- )); do
      if [ "${text:i:1}" = " " ] && [ "${text:i+1:1}" != "-" ]; then
        cut=$i
        break
      fi
    done
    if [ "$cut" -lt 1 ]; then
      # Кусок без единого годного пробела — резать нечем, не режем молча.
      DSH_PROMPT_SPLIT_FAILED=1
      DSH_PROMPT_ARGV=("$1")
      return 0
    fi
    DSH_PROMPT_ARGV+=("${text:0:cut}")
    text="${text:cut+1}"
  done
  DSH_PROMPT_ARGV+=("$text")
}

dsh_run_with_retry() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  local max_wait="${DSH_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
  local delay="${DSH_RATE_LIMIT_INITIAL_DELAY_SECS:-30}"
  local max_delay="${DSH_RATE_LIMIT_MAX_DELAY_SECS:-300}"
  local timeout_secs="${DSH_TIMEOUT_SECS:-3600}"
  local waited=0 attempt=1 wait_left rc
  local attempt_start attempt_elapsed
  DSH_RUN_FAILURE_REASON=""
  # #1315 (живой инцидент, прогон worker.yml 35046585539, задача #770): промпт
  # уходит в dsh ПОЗИЦИОННЫМ АРГУМЕНТОМ, а Linux ограничивает ОДИН аргумент
  # 128 КиБ (MAX_ARG_STRLEN = 32 страницы, отдельно от общего ARG_MAX). Промпт
  # воркера штатно ~111 КБ — то есть впритык; на задаче с крупным диффом он
  # перевалил, execve вернул E2BIG, и это выглядело так:
  #   dsh завершился с кодом 126 … /usr/bin/timeout: Argument list too long
  # Восемь провайдеров подряд «отказали» за 78 секунд, не дойдя до сети ни
  # разу, а итог честно сказал «транзиентных отказов: 8, повтор ИМЕЕТ смысл» —
  # и это была неправда: повтор не поможет никогда, промпт не станет короче.
  # Отказ НАШ (форма вызова), а не провайдеров, поэтому ловим его ДО первой
  # попытки и не тратим на него ни одного провайдера.
  #
  # #1318: первая версия этой проверки (#1315) считала предел непреодолимым и
  # отказывала. Замер показал, что тогда конвейер просто умирает на обычных
  # задачах: фиксированная часть промпта (AGENTS.md 36674 + PROTOCOL.md 53937
  # + WORKER-PLAYBOOK.md 17055 = 107666 байт) съедает бюджет почти целиком, и
  # на саму задачу остаётся ~19 КБ — задача #1184 дала 133199 байт и легла
  # (прогоны worker.yml 35072416907/35074809844/35079314952 подряд).
  #
  # Резать содержимое не нужно: задача у dsh-headless объявлена ВАРИАДИКОМ
  # (`.argument("[task...]")`) и собирается обратно как `program.args.join(" ")`
  # — lib/startup.js пакета @deepseek-ai/dsh-headless 0.1.1-rc.2. Предел
  # MAX_ARG_STRLEN действует на ОДИН аргумент, поэтому промпт уходит
  # НЕСКОЛЬКИМИ аргументами (dsh_prompt_argv_chunks), а join склеивает их
  # обратно байт-в-байт: резать разрешено только по ОДИНОЧНОМУ пробелу,
  # который join и вернёт на место.
  #
  # Проверка ниже остаётся страховкой на то, чего нарезка не лечит: общий
  # ARG_MAX (все аргументы плюс environment) — величина другая и больше на
  # порядок, поэтому и порог у неё свой.
  local prompt_bytes
  prompt_bytes=$(printf '%s' "$prompt_text" | wc -c)
  if [ "$prompt_bytes" -gt "$DSH_PROMPT_TOTAL_MAX_BYTES" ]; then
    DSH_RUN_RC=126
    DSH_RUN_FAILURE_REASON="prompt_too_long"
    DSH_RUN_WAITED_SECS=0
    DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS=0
    DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS="$timeout_secs"
    echo "::error::промпт ${prompt_bytes} байт при общем пределе ${DSH_PROMPT_TOTAL_MAX_BYTES} на ВСЕ аргументы командной строки (ARG_MAX ядра за вычетом запаса на environment) — нарезка по аргументам (#1318) этот предел не лечит, он про сумму. Это НАШ отказ, не провайдера: ни одна попытка не делается, повтор без укорачивания промпта бессмыслен. Лечение — сократить источник промпта (docs/runbooks/switch-llm-provider.md, #1315/#1318)" >&2
    return 0
  fi
  # #1318: нарезка считается ОДИН раз на вызов, а не на каждую попытку —
  # промпт между попытками не меняется.
  dsh_prompt_argv_chunks "$prompt_text"
  if [ "${DSH_PROMPT_SPLIT_FAILED:-0}" = "1" ] && [ "$prompt_bytes" -gt "$DSH_PROMPT_MAX_BYTES" ]; then
    DSH_RUN_RC=126
    DSH_RUN_FAILURE_REASON="prompt_too_long"
    DSH_RUN_WAITED_SECS=0
    DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS=0
    DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS="$timeout_secs"
    echo "::error::промпт ${prompt_bytes} байт не режется на аргументы: в первых ${DSH_PROMPT_CHUNK_BYTES} байтах нет ни одного пробела, по которому можно резать без потери байта (за пробелом не должно идти '-'). Резать по переводу строки нельзя — join на стороне dsh поставил бы туда пробел и тихо изменил текст. Это НАШ отказ, не провайдера: ни одной попытки не делается (#1318)" >&2
    return 0
  fi
  if [ "${#DSH_PROMPT_ARGV[@]}" -gt 1 ]; then
    echo "промпт ${prompt_bytes} байт передан ${#DSH_PROMPT_ARGV[@]} аргументами (предел ядра ${DSH_PROMPT_MAX_BYTES} на ОДИН аргумент; dsh склеит их обратно join(\" \"), #1318)"
  fi
  while :; do
    echo "dsh: попытка $attempt (суммарно уже ждал ${waited}с из бюджета ${max_wait}с)"
    attempt_start=$(date +%s)
    set +e
    timeout "$timeout_secs" dsh --profile headless "${DSH_PROMPT_ARGV[@]}" \
      >"$answer_file" 2>"$err_file"
    rc=$?
    set -e
    attempt_elapsed=$(( $(date +%s) - attempt_start ))
    echo "dsh завершился с кодом $rc (попытка $attempt, длилась ${attempt_elapsed}с)"

    [ "$rc" -eq 0 ] && break

    if grep -q 'RATE_LIMIT: Weekly/Monthly Limit Exhausted' "$err_file"; then
      DSH_RUN_FAILURE_REASON="quota_exhausted"
      echo "::warning::квота провайдера исчерпана надолго (Weekly/Monthly) — повтор внутри прогона бессмысленен, падаю сразу (docs/runbooks/switch-llm-provider.md)"
      break
    fi

    if ! grep -q 'RATE_LIMIT:' "$err_file"; then
      # Настоящая ошибка провайдера/транспорта (ключ, модель, битый запрос,
      # сеть) — ждать её повтором нет смысла, падаем сразу, как и раньше.
      break
    fi

    wait_left=$((max_wait - waited))
    if [ "$wait_left" -le 0 ]; then
      DSH_RUN_FAILURE_REASON="rate_limit_retry_budget_exceeded"
      echo "::warning::бюджет ожидания временного RATE_LIMIT (${max_wait}с) исчерпан — сдаюсь"
      break
    fi
    [ "$delay" -gt "$wait_left" ] && delay=$wait_left
    echo "::warning::временный RATE_LIMIT провайдера — жду ${delay}с и повторяю (в сумме ждал ${waited}с)"
    sleep "$delay"
    waited=$((waited + delay))
    delay=$((delay * 2))
    [ "$delay" -gt "$max_delay" ] && delay=$max_delay
    attempt=$((attempt + 1))
  done
  DSH_RUN_RC=$rc
  DSH_RUN_WAITED_SECS=$waited
  # #877/#880 (некритичная находка ai-review, «rc=124 приписывается нашему
  # ножу без проверки, что это наш нож»): rc=124 у GNU `timeout` означает и
  # «убил по таймауту», и «сам ребёнок вышел с 124» — по коду возврата эти
  # формы неразличимы. Замер elapsed ПОСЛЕДНЕЙ попытки — дешёвое различение:
  # dsh_chain_should_advance сравнивает его с DSH_TIMEOUT_SECS ПЕРЕД тем, как
  # утверждать «наш нож» (см. её комментарий).
  DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS=$attempt_elapsed
  DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS=$timeout_secs
}

# ── Цепочка провайдеров (#727): автопереход по классу отказа ────────────────
#
# quota_exhausted (RATE_LIMIT: Weekly/Monthly Limit Exhausted, #422 выше) и
# повторяемый транспортный сбой (HTTP_404/EMPTY_RESPONSE — класс, названный
# AGENTS.md, живой случай — перемежающийся HTTP_404 NVIDIA NIM, уложивший
# воркер и пять прогонов ai-review 2026-09-02) переключают на следующего
# провайдера БЕЗ участия человека. Ошибка контракта вердикта (модель ответила
# не по формату, rc=0) сюда не попадает вовсе — она решается выше по стеку
# (ai_review.py::parse_verdict), чейн её не видит и не трогает: иначе цепочка
# сожгла бы все учётки на одном сломанном PR (AGENTS.md, «квота — это ВОЗМОЖНОСТИ
# нет, контракт — возможность есть, но сломана»).
#
# Одно место правды на упорядоченный список — vars.DSH_PROVIDER_CHAIN
# репозитория (тот же принцип, что #153: провайдер объявляется в vars, не
# зашитым фолбэком в коде — гвардия provider-default.guard.sh сканирует
# scripts/**/docs/agents/** на литералы конкретных провайдеров, поэтому
# список НЕ хранится файлом в дереве репозитория, а живёт переменной, как уже
# делает vars.DSH_EDGE_MODEL_CATALOG для каталога морды). Формат — JSON-массив,
# порядок = приоритет:
#   [{"name":"GLM","base_url":"...","model":"...","secret_env":"DEEPSEEK_API_KEY",
#     "max_output_tokens":131072}, {"name":"NVIDIA","base_url":"...", ...}]
# secret_env — ИМЯ переменной окружения, в которую workflow уже положил ключ
# этого провайдера (secrets.<X>, литеральным именем в workflow — не индексацией
# через vars.DSH_PROVIDER_KEY_SECRET, #716: тому механизму нужен РОВНО один
# активный секрет в её, этому — ключи ВСЕХ провайдеров цепочки одновременно,
# разные задачи, не дублируют друг друга).

# ── Манифест использования (openspec/changes/llm-provider-usage-manifest) ──
#
# Одно место конфигурации «кто каким комбо провайдеров пользуется» —
# config/provider-usage.json репозитория (вне scripts/**, .github/workflows/**,
# docs/agents/** — гвардия provider-default.guard.sh, класс #153, эти пути не
# сканирует). Источник правды на его СОДЕРЖИМОЕ по design.md этого change —
# Settings морды dsh-edge (пуш при изменении настройки, Этап 2 tasks.md, ещё
# не подключён); эта функция только ЧИТАЕТ файл из уже сделанного checkout'а —
# сети не трогает, правило владельца «не дёргать морду на каждый прогон
# пайплайна» (proposal.md, Scope/Out).
#
# Манифест ПРИОРИТЕТНЕЕ vars.DSH_PROVIDER_CHAIN, если файл присутствует:
# присутствует, но нет записи потребителя (или запись ссылается на
# несуществующую/пустую цепочку) — fail loud, а не тихий фоллбэк на vars.
# Ровно класс, который пропустили с воркером на 72 задачи (#727 -> #797,
# proposal.md «Problem») — «у кого-то нет валидного назначения» обязано
# падать здесь же, на прогоне, не только в CI-инварианте репозитория
# (repo_invariants.py::check_provider_usage_manifest — та же проверка над
# статичным файлом, без сети).
#
# Файла нет вовсе (старый checkout без него, локальный запуск смок-теста без
# манифеста) — тихий проход, вызывающий использует свой прежний
# vars.DSH_PROVIDER_CHAIN как есть (design.md «Потребители»: выбор между
# «манифест — единственный источник» и «фоллбэк на переходный период» здесь
# решён в пользу фоллбэка ТОЛЬКО на случай отсутствия файла, не на случай его
# неполноты — иначе манифест с дырой в usage был бы неотличим от манифеста,
# который ещё не появился).
DSH_PROVIDER_USAGE_MANIFEST="${DSH_PROVIDER_USAGE_MANIFEST:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/config/provider-usage.json}"

dsh_load_provider_chain_from_manifest() { # consumer_id
  local consumer=$1 manifest="$DSH_PROVIDER_USAGE_MANIFEST" raw chain_name chain count
  [ -f "$manifest" ] || return 0
  raw=$(cat "$manifest") || {
    echo "::error::манифест использования провайдеров $manifest не читается" >&2
    return 1
  }
  if ! jq -e . >/dev/null 2>&1 <<<"$raw"; then
    echo "::error::манифест использования провайдеров $manifest — невалидный JSON" >&2
    return 1
  fi
  chain_name=$(jq -r --arg id "$consumer" '.usage[$id] // empty' <<<"$raw")
  if [ -z "$chain_name" ]; then
    echo "::error::манифест $manifest не назначает цепочку потребителю '$consumer' (ключ .usage[\"$consumer\"] отсутствует) — назначь цепочку в манифесте (Settings морды dsh-edge, когда Этап 2 подключён; сейчас — правкой файла), openspec/changes/llm-provider-usage-manifest" >&2
    return 1
  fi
  chain=$(jq -c --arg name "$chain_name" '.chains[$name] // empty' <<<"$raw")
  if [ -z "$chain" ] || [ "$chain" = "null" ]; then
    echo "::error::манифест $manifest: usage[\"$consumer\"] ссылается на несуществующую цепочку '$chain_name' в .chains" >&2
    return 1
  fi
  count=$(jq 'length' <<<"$chain" 2>/dev/null) || count=0
  if [ -z "$count" ] || [ "$count" -lt 1 ]; then
    echo "::error::манифест $manifest: цепочка '$chain_name' (потребитель '$consumer') пуста" >&2
    return 1
  fi
  export DSH_PROVIDER_CHAIN="$chain"
  echo "манифест использования провайдеров: '$consumer' -> цепочка '$chain_name' ($count провайдер(ов)), источник $manifest"
}

dsh_require_provider_chain() { # [consumer_id]
  if [ -n "${1:-}" ]; then
    dsh_load_provider_chain_from_manifest "$1" || return 1
  fi
  # Стык с suite ротации учёток (#215, design.md dsh-in-job «Стык suite и
  # цепочки провайдеров»): цепочка владеет провайдером КАЖДОЙ попытки
  # (атрибуция DSH_CHAIN_PROVIDER/DSH_CHAIN_TRIED, реестр подтверждённых
  # моделей #737), suite вращает УЧЁТКИ внутри одного провайдера через
  # combo-router и не проходит через эти проверки. Включить оба сразу значило
  # бы либо тихо игнорировать suite (сейчас так и сделано ниже по стеку,
  # dsh_patch_profile), либо тихо обойти гейт подтверждённых моделей —
  # запрещаем комбинацию явно, а не полагаемся на то, что ни один вызывающий
  # не прокинет обе переменные разом.
  if [ -n "${PLUGINS_SUITE_URL:-}" ]; then
    echo "::error::vars.PLUGINS_SUITE_URL и vars.DSH_PROVIDER_CHAIN заданы одновременно — комбинация не поддержана (design.md dsh-in-job, «Стык suite и цепочки провайдеров»): цепочка (#727) сама решает, какого провайдера пробовать на каждой попытке, suite (#215) решает тот же вопрос на своём уровне — выбери одно. Сейчас suite нужен только hands.yml (там цепочки нет, #797) — ai-review и worker всегда идут цепочкой." >&2
    return 1
  fi
  if [ -z "${DSH_PROVIDER_CHAIN:-}" ]; then
    echo "::error::не задан vars.DSH_PROVIDER_CHAIN — цепочка провайдеров объявляется только в vars репозитория (#727), зашитого списка в коде нет" >&2
    return 1
  fi
  local count
  count=$(jq 'length' <<<"$DSH_PROVIDER_CHAIN" 2>/dev/null) || {
    echo "::error::vars.DSH_PROVIDER_CHAIN не парсится как JSON-массив" >&2
    return 1
  }
  if [ -z "$count" ] || [ "$count" -lt 1 ]; then
    echo "::error::vars.DSH_PROVIDER_CHAIN пуст — нужен хотя бы один провайдер" >&2
    return 1
  fi
  # #1309: элемент обязан нести хотя бы один непустой id модели — в поле
  # `model` (прежняя форма) или в списке `models`. Без этой проверки запись с
  # опечаткой в имени поля доезжала бы до цикла и тратила попытку на модель
  # "null", а сообщение говорило бы «id не подтверждён» — формально верно, но
  # уводит от настоящей причины (AGENTS.md, «Fail loud, не silent-wrong»).
  local bad
  bad=$(jq -r '
    to_entries[]
    | .key as $i | .value as $e
    | (if ($e.models? | type) == "array"
       then [ $e.models[] | if type == "string" then . else (.id // .model) end ]
       else [ $e.model ] end) as $ids
    | select(($ids | map(select(type == "string" and length > 0)) | length) == 0)
    | "#\($i) (\($e.name // "без имени"))"' <<<"$DSH_PROVIDER_CHAIN" 2>/dev/null) || bad=""
  if [ -n "$bad" ]; then
    echo "::error::цепочка провайдеров: у элемента(ов) $(tr '\n' ' ' <<<"$bad") нет ни одного id модели — нужно поле \"model\" либо непустой список \"models\" (docs/runbooks/switch-llm-provider.md, «Несколько моделей на один ключ», #1309)" >&2
    return 1
  fi
}

# Класс отказа → переход на следующего провайдера. failure_reason — уже
# посчитанный dsh_run_with_retry (quota_exhausted/rate_limit_retry_budget_
# exceeded/пусто); при пустом — второй, более грубый признак: буквальный
# текст HTTP_404/EMPTY_RESPONSE/STREAM_CLOSED в stderr (прод-формы — «dsh:
# HTTP_404: modelCode does not exist», scripts/lib/test/dsh-clients.smoke.sh;
# «dsh: STREAM_CLOSED: SSE stream ended without [DONE]», #1084 ниже).
#
# #1084 (живой инцидент — прогон worker.yml 2026-09-13T09:39Z, задача #1055/
# #1087): УМОЛЧАНИЕ этой функции ПЕРЕВЁРНУТО. До этой правки каждый НЕ
# распознанный явно текст падал в стоп-класс — список «переключаемых» рос
# построчно, литеральной строкой на каждый новый прогон (#737/#877/#1062
# ниже — три отдельных инцидента ради трёх отдельных строк), и до появления
# своей строки новый транспортный сбой ВСЕГДА стопорил всю цепочку целиком,
# как и случилось с STREAM_CLOSED у GLM: единственного реально отвечавшего
# провайдера на тот момент (медиана ~1207с, 14 успешных завершений за
# последние сутки — остальные либо молчат, либо падают раньше), цепочка
# остановилась НЕ дойдя до пяти непопробованных записей (Ollama-2/3/1,
# NVIDIA-NIM-1/2). Разбор живых прогонов той же ночи (00:07Z, OpenRouter-2,
# тот же текст STREAM_CLOSED) показывает, что причина не привязана к
# конкретному провайдеру — обрыв SSE-потока без терминального [DONE] это
# transient-сбой соединения/прокси, тот же класс, что уже признан
# переключаемым для HTTP_404/EMPTY_RESPONSE (docs/runbooks/
# switch-llm-provider.md), а не признак «эта же ошибка повторится у ЛЮБОГО
# следующего провайдера».
#
# Не подтверждено (честно, ai-review нашёл #1084 при доводке): ДО этой правки
# функция несла именованный «стоп-класс» на литерал `INVALID_API_KEY:` —
# рассуждение было «явный отказ credentials этой записи не лечится сменой
# провайдера». Проверка по репозиторию (`grep -rn INVALID_API_KEY`) показала,
# что этот текст НИ РАЗУ не встречается ни в одном живом прогоне/
# docs/research — он существовал только как синтетическая заглушка смоука с
# самого #727 (представитель абстрактной «настоящей ошибки», не цитата
# ответа реального dsh), тот же класс, что AGENTS.md называет «тест кормит
# прод-форму, а не пересказ» (#891/#893). Держать единственный оставшийся
# стоп-класс на непроверенной строке значило бы: (а) реальный 401/403 у
# провайдера с ДРУГИМ текстом молча провалился бы в «класс не распознан»
# ниже и всё равно переключился бы — расхождение с тем, что обещает
# комментарий; (б) гвардия смоука кормится тем же вымышленным литералом и
# остаётся зелёной, ничего не доказывая про прод. Решение: стоп-класс СНЯТ
# целиком, пока не появится живая цитата отказа credentials из реального
# прогона (тем же порядком, что HTTP_404/RATE_LIMIT/INVALID_REQUEST/
# STREAM_CLOSED — прод-форма, номер прогона, тогда и только тогда — именованная
# ветка с `return 1`). Явный отказ credentials СЕЙЧАС относится к общей
# ветке «класс не распознан» ниже (advance, с честной пометкой «класс не
# распознан», не «стоп»).
#
# Именно поэтому дальше по умолчанию НЕ стоп, а «пробуем следующего» —
# симметрично уже принятому в #737 доводу (ниже) для пустого stderr:
# цена ошибки «переключились зря» — потратить время следующего провайдера
# (секунды-минуты, при 6-часовом бюджете job'а и цепочке из 8 записей — это
# ограниченная, не бесконечная трата: количество попыток равно длине
# DSH_PROVIDER_CHAIN, не циклу); цена ошибки «остановились зря» — потерять
# ВСЮ оставшуюся цепочку и весь прогон целиком, как и случилось 2026-09-13.
# Частичный результат агента (уже сделанные коммиты в ветке задачи) при этом
# не теряется — критерий успеха воркера уже давно смотрит на origin, не на
# факт «эта попытка ответила» (#935/#937), так что более длинный перебор
# провайдеров при реальном сбое не рискует затереть то, что агент успел
# сохранить. Повтор У ТОГО ЖЕ провайдера здесь не делается: dsh_run_with_retry
# уже отвечает за все ретраи ВНУТРИ одной попытки (её же RATE_LIMIT-бэкофф);
# если бы слепой повтор помогал при обрыве потока, отдельный переход по
# цепочке для HTTP_404/EMPTY_RESPONSE тоже был бы не нужен — практика этого
# файла уже выбрала «дальше по списку», не «ещё раз здесь».
#
# Граница переворота МЕЖДУ прогонами: детерминированный отказ ВСЕЙ цепочки
# (сломанный на нашей стороне dsh/конфиг, а не один провайдер) заканчивается
# all_providers_exhausted — worker/hands возвращают задачу в пул, и следующий
# прогон сжигает цепочку заново, до порога предохранителя конвейера
# (#120/#226). Тормоз этого класса есть и автоматичен, газ — тот же
# предохранитель (возврат после серии провалов подряд): худший случай —
# ограниченная серия полных переборов, не бесконечная петля.
#
# #737 (живой случай — прогон 34188152283, NVIDIA rc=1, НИ ОДНОГО байта в
# stderr): третий, отдельный признак — сигнала нет вообще (stderr пуст или
# состоит только из пробелов). Заведомо-нелечимый сменой провайдера отказ
# ВСЕГДА печатает диагностическую строку — тишина неотличима от временного
# транспортного сбоя, ради которого цепочка и строилась (proposal.md,
# «повторяемый транспортный отказ… переключают… без участия человека»). По
# умолчанию — переключаемся; тот же довод #1084 выше распространяет это на
# ЛЮБОЙ нераспознанный, но НЕПУСТОЙ текст, не только на пустоту.
#
# #877 (живой инцидент — прогон worker.yml 34498185823, задача #140): rc=124
# (`timeout` в dsh_run_with_retry убил процесс SIGTERM) раньше проходил через
# ТУ ЖЕ ветку «stderr пуст» выше — `timeout` не пишет в stderr ни строки,
# поэтому наш собственный нож (DSH_TIMEOUT_SECS истёк) был неотличим от
# настоящего молчания провайдера (#737). AGENTS.md, «Fail loud, не
# silent-wrong»: убийство ПО НАШЕМУ таймауту — не отказ провайдера, сообщение
# обязано называть это прямо, а не растворяться в «класс не установить».
# Решение «переключаемся» остаётся тем же (следующий провайдер всё равно
# стоит попробовать) — меняется только текст причины, RC проверяется ДО
# ветки «stderr пуст», чтобы не спутать эти два разных факта.
#
# #1062 (живой инцидент — прогон worker.yml 34730173870): Ollama-2 отказал с
# «dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output
# tokens (65536) for model nemotron-3-ultra» — наша же запись
# config/provider-usage.json несла неверный max_output_tokens для ЭТОЙ
# записи. Раньше это падало в стоп-класс ниже (нераспознанная строка) и
# останавливало ВСЮ цепочку — Ollama-3/Ollama-1/NVIDIA-NIM-1/OpenRouter-1/
# NVIDIA-NIM-2 не пробовались вовсе, хотя их max_output_tokens не связан со
# сломанной записью. AGENTS.md, «возможность есть, но сломана» ≠ «возможности
# нет»: ошибка ПАРАМЕТРОВ запроса (не ключа, не модели, не контракта ответа)
# у ОДНОЙ записи цепочки не означает, что у следующей записи (свой
# base_url/model/max_output_tokens) тот же запрос тоже невалиден —
# переключаемся, но сообщение обязано прямо назвать «конфиг этого провайдера
# неверен», не гадать причину так, как гадает общий стоп-класс ниже.
#
# DSH_CHAIN_CLASS_NOTE (переменная, не возврат) — человекочитаемая причина
# решения для сообщения вызывающего (правило AGENTS.md «Алерт не гадает»):
# имя признанной причины, литеральный маркер stderr, факт «stderr пуст»,
# факт «наш таймаут» или факт «класс не распознан» (переключаемся по #1084;
# на 2026-09-13 в функции нет ветки, возвращающей стоп, — см. «Не
# подтверждено» выше).
#
# Ветка «класс не распознан» ниже — единственная, что кладёт в переменную
# СЫРОЙ фрагмент stderr клиента модели, а не заранее известную безопасную
# строку (имя причины/литеральный маркер/факт пустоты) — обязана идти через
# redact() (#743): начало ответа 401 у провайдера типично содержит эхо
# заголовка Authorization, GitHub маскирует только точное совпадение
# секрета, производное (подстрока/префикс) — нет. Маскируем здесь, у
# источника, ОДИН раз — оба места печати (::warning:: и ::error:: ниже)
# читают уже замаскированную переменную, второй копии redact на каждую
# точку вывода не нужно (то же место правды, что redact() выше в этом
# файле).
dsh_chain_should_advance() { # err_file failure_reason rc
  local err_file=$1 reason=$2 rc=$3
  case "$reason" in
    quota_exhausted|rate_limit_retry_budget_exceeded)
      DSH_CHAIN_CLASS_NOTE="$reason"
      return 0 ;;
    prompt_too_long)
      # #1315: отказ НАШ и одинаковый для всех — промпт не поместился в
      # аргумент execve. Следующий провайдер получит ровно тот же промпт и
      # ровно тот же E2BIG, поэтому переход по цепочке — чистая трата восьми
      # попыток (живой прогон 35046585539: 8 «транзиентных» за 78с, ни одна
      # не дошла до сети). Единственная в этой функции ветка «стоп»: она про
      # НАС, не про провайдера, — именно то различие, которого #1084 не
      # находил среди провайдерских классов.
      DSH_CHAIN_CLASS_NOTE="промпт не помещается в аргумент командной строки — отказ наш, одинаковый у всех провайдеров (#1315)"
      return 1 ;;
  esac
  # #1315, страховка: если предварительная проверка длины почему-то не
  # сработала (промпт собран иначе, предел ядра другой), живая прод-форма
  # отказа execve видна в stderr дословно — не считаем её транзиентом.
  if grep -qE 'Argument list too long' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="execve отверг аргументы (Argument list too long) — отказ наш, одинаковый у всех провайдеров (#1315)"
    return 1
  fi
  # #880 (некритичная находка ai-review): GNU `timeout` возвращает 124 и
  # когда САМ убивает процесс по сроку, и когда ребёнок дсш сам вышел с
  # кодом 124 по собственной причине — по коду возврата эти формы
  # неразличимы. Замер elapsed последней попытки (dsh_run_with_retry,
  # DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS/_TIMEOUT_SECS) — дешёвое различение:
  # «наш нож» утверждается ТОЛЬКО когда попытка реально длилась не меньше
  # заданного ей таймаута, иначе rc=124 идёт в общую недиагностируемую
  # ветку ниже (не гадаем, AGENTS.md «Алерт не гадает»).
  if [ "$rc" = "124" ] && [ "${DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS:-0}" -ge "${DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS:-999999999}" ]; then
    DSH_CHAIN_CLASS_NOTE="наш таймаут (${DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS}с истекли, попытка длилась ${DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS}с) — НЕ отказ провайдера, убит по времени (#877/#880)"
    return 0
  fi
  if grep -qE 'HTTP_404:|EMPTY_RESPONSE:' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="HTTP_404/EMPTY_RESPONSE в stderr"
    return 0
  fi
  # #1084: живой инцидент — прогон worker.yml 2026-09-13T09:39Z (задача
  # #1055/#1087) — GLM (на тот момент единственный реально отвечающий
  # провайдер цепочки) оборвал SSE-поток без терминального [DONE]. Тот же
  # текст видели у OpenRouter-2 той же ночью (00:07Z) — не привязан к одному
  # провайдеру, классический transient-обрыв соединения/прокси, тот же класс,
  # что уже переключаемый HTTP_404/EMPTY_RESPONSE выше. Переключаемся на
  # следующего, не повторяем у того же — см. развёрнутый довод в комментарии
  # над функцией.
  if grep -qE 'STREAM_CLOSED:' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="STREAM_CLOSED (SSE-поток оборвался без [DONE]) в stderr — transient-обрыв соединения, не привязан к конкретному провайдеру (#1084)"
    return 0
  fi
  # #1062: max_tokens конфига (config/provider-usage.json::max_output_tokens)
  # превышает реальный потолок ответа МОДЕЛИ этой конкретной записи —
  # ошибка параметров запроса ОДНОГО провайдера, не признак того, что
  # дальше пробовать бессмысленно (следующая запись несёт свой собственный
  # max_output_tokens и может быть верной). Прод-форма дословно (Ollama
  # Cloud, прогон 34730173870): «dsh: INVALID_REQUEST: max_tokens (131072)
  # exceeds model's maximum output tokens (65536) for model nemotron-3-ultra».
  if grep -qE "INVALID_REQUEST:.*max_tokens \([0-9]+\) exceeds model.s maximum output tokens \([0-9]+\)" "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="конфиг ЭТОГО провайдера неверен — max_output_tokens в config/provider-usage.json превышает реальный потолок модели ($(tr '\n' ' ' <"$err_file" | cut -c1-200 | redact)); возможность у следующего провайдера не исключена (свой лимит) — пробую дальше, но эту запись стоит поправить (#1062)"
    return 0
  fi
  if [ ! -s "$err_file" ] || ! grep -qE '[^[:space:]]' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="stderr пуст — диагностику дать не может, класс не установить, консервативно пробую следующего"
    return 0
  fi
  # #1084: КЛАСС не распознан (не входит ни в один явный признак выше,
  # включая явный отказ credentials — см. «Не подтверждено» в комментарии
  # над функцией: именованного стоп-класса на этот случай сейчас нет, ни
  # один живой прогон его текст не подтвердил) — умолчание перевёрнуто с
  # «стоп» на «пробуем следующего»: см. развёрнутый довод в комментарии над
  # функцией. Раньше именно эта ветка возвращала 1 (стоп) для ЛЮБОГО
  # непойманного текста — новый провайдер узнавался только из уже упавшего
  # прогона (#737/#877/#1062/STREAM_CLOSED выше — четыре инцидента ради
  # четырёх строк). Теперь новый нераспознанный текст не останавливает
  # цепочку целиком сам по себе — на 2026-09-13 в этой функции ВООБЩЕ нет
  # ветки с `return 1`: quota/rate_limit в начале функции решены иначе
  # намеренно (advance, не stop), а не потому что стоп-класс существует и
  # просто не сработал здесь.
  DSH_CHAIN_CLASS_NOTE="класс не распознан ($(tr '\n' ' ' <"$err_file" | cut -c1-200 | redact)) — консервативно пробую следующего (#1084)"
  return 0
}

# Дата сброса квоты — прод-форма «Your limit will reset at 2026-09-10
# 08:51:55» (run 34176910458) и ISO-форма «...reset at 2026-09-10T00:00:00Z»
# (смоук-фикстура) — обе покрыты одним разбором: всё после "reset at" до
# конца строки, минус хвостовые точки/пробелы. Пусто — дата не названа
# (не quota_exhausted, либо провайдер сформулировал иначе).
dsh_extract_reset_hint() { # err_file
  local line
  line=$(grep -oE 'reset at .*' "$1" 2>/dev/null | head -1) || true
  line="${line#reset at }"
  line="${line%.}"
  line="${line% }"
  printf '%s' "$line"
}

# ── Персистентное состояние квоты провайдеров (#857) ─────────────────────────
#
# Класс: dsh_extract_reset_hint выше ловит дату сброса из ответа провайдера
# ВНУТРИ одного прогона, но это знание не переживает конец эфемерной джобы
# (ai-review.yml/worker.yml/hands.yml — все три живут только на время job'а).
# Следующий прогон, стартовавший минутой позже, снова тратит первую попытку
# на провайдера, чью квоту предыдущий прогон УЖЕ видел исчерпанной с
# известной датой сброса — тормоз без газа (AGENTS.md): знание есть, а
# перенести его в следующий job нечем.
#
# Носитель — vars.DSH_PROVIDER_QUOTA_UNTIL (JSON {provider_name: reset_iso}),
# design.md openspec/changes/provider-quota-gating обосновывает выбор: та же
# репо-переменная, что читается контекстом workflow (`${{ vars.X }}`) БЕЗ
# токена — тот же путь, что уже несёт vars.DSH_PROVIDER_CHAIN, включая
# недоверенный DSH-шаг ai-review.yml (#18), у которого GitHub-токена нет
# вовсе. ЭТА функция и весь bash-слой — ТОЛЬКО ЧТЕНИЕ: пишет состояние
# исключительно пульс оркестратора (scripts/orchestra/provider_quota_state.py,
# вызывается из scheduler.py::sync_provider_quota_state) — гвардия границы
# доверия в scripts/lib/test_provider_quota_state_guard.py.
#
# Разбирает состояние ОДИН раз за весь прогон цепочки (не на каждой
# итерации): переменная не задана — гейт не срабатывает никогда (обратная
# совместимость); задана, но не JSON-объект — ::warning:: и фейл-открыто
# (работоспособность цепочки важнее гейта квоты, сломанное персистентное
# состояние не должно ронять прод).
#
# #1121/#1127 (разобрано, НЕ чинится здесь): гейт узнаёт о quota_exhausted
# ТОЛЬКО из reset-at факта в комментарии ai-review на УЖЕ существующем PR
# (trigger_ai_review в scheduler.py) — если провайдер отказал классом
# quota_exhausted у ВОРКЕРА/hands ДО того, как для задачи открыт PR (частый
# случай — отказ на этапе «Задача через DSH headless»), это состояние
# структурно не может достичь пульса и переменной. Не «сломан» (проверено:
# ни один ai:failed PR с 2026-09-10 не нёс quota_exhausted/reset-at —
# staleness согласуется с дизайном, не с поломкой), а УЖЕ спроектированная
# граница шире того, что покрывает: см. #1127 для канала worker/hands →
# пульс. Границу доверия (только пульс пишет, см. test_provider_quota_
# state_guard.py) эта задача не трогает.
dsh_quota_state_validate() { # -> печатает в stdout валидный JSON-объект или пусто
  local raw="${DSH_PROVIDER_QUOTA_UNTIL:-}"
  [ -n "$raw" ] || return 0
  if jq -e 'type == "object"' >/dev/null 2>&1 <<<"$raw"; then
    printf '%s' "$raw"
  else
    echo "::warning::vars.DSH_PROVIDER_QUOTA_UNTIL задана, но не JSON-объект — гейтирование по персистентной квоте пропущено в этом прогоне (фейл-открыто, #857)" >&2
  fi
}

# $1 — имя провайдера, $2 — валидированный JSON-объект состояния (может быть
# пустой строкой). Возврат 0 — провайдера следует ПРОПУСТИТЬ (квота ещё не
# сброшена), DSH_QUOTA_GATE_RESET несёт дату; возврат 1 — пробовать как
# обычно (нет записи, дата нераспознана, либо срок уже прошёл).
dsh_provider_quota_gate_skip() { # name state_json
  local name=$1 state_json=$2 reset_iso reset_epoch now_epoch
  DSH_QUOTA_GATE_RESET=""
  [ -n "$state_json" ] || return 1
  reset_iso=$(jq -r --arg n "$name" '.[$n] // empty' <<<"$state_json" 2>/dev/null) || return 1
  [ -n "$reset_iso" ] || return 1
  reset_epoch=$(date -u -d "$reset_iso" +%s 2>/dev/null) || {
    echo "::warning::цепочка провайдеров: quota-состояние '$name' содержит нераспознанную дату '$reset_iso' (vars.DSH_PROVIDER_QUOTA_UNTIL) — гейт пропущен для этого провайдера, пробую как обычно" >&2
    return 1
  }
  now_epoch=$(date -u +%s)
  if [ "$now_epoch" -lt "$reset_epoch" ]; then
    DSH_QUOTA_GATE_RESET="$reset_iso"
    return 0
  fi
  return 1
}

# ── Реестр подтверждённых id моделей (#737) ──────────────────────────────────
#
# Рунбук (docs/runbooks/switch-llm-provider.md, «Узнать точный id модели»)
# прямо запрещает экстраполяцию id и требует сверки буква-в-букву с ответом
# `/v1/models` — правило было прозой без носителя. Живая цена: id
# `nvidia/nemotron-3-super-120b-a12b` вписан в vars.DSH_PROVIDER_CHAIN
# 2026-09-08T04:38:51Z без единой проверки и дал 37 минут молчания
# (прогон 34188152283).
#
# Реестр хранит sha256 подтверждённой СТРОКИ id, не саму строку: провайдер-
# дефолт-гвардия (scripts/lib/test/provider-default.guard.sh, класс #153)
# сканирует scripts/**/docs/agents/** на литералы конкретных провайдеров/
# моделей (glm-N, nemotron, …) — хэш ей не виден и не обязан быть, это не
# второе место правды о ТЕКУЩЕМ провайдере (им остаётся vars.
# DSH_PROVIDER_CHAIN), а список «эту строку кто-то сверил живым запросом».
DSH_CONFIRMED_MODELS_FILE="${DSH_CONFIRMED_MODELS_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/confirmed-provider-models.json}"

dsh_model_confirmed() { # model_id
  local model=$1 hash
  [ -f "$DSH_CONFIRMED_MODELS_FILE" ] || return 1
  hash=$(printf '%s' "$model" | sha256sum | cut -d' ' -f1)
  jq -e --arg h "$hash" 'any(.[]?; .model_sha256 == $h)' "$DSH_CONFIRMED_MODELS_FILE" >/dev/null 2>&1
}

# ── Кандидаты моделей ОДНОГО элемента цепочки (#1309) ───────────────────────
#
# Один элемент цепочки = один аккаунт (base_url + secret_env). До #1309 у
# него была РОВНО одна модель, и снятый провайдером id (живой класс: NVIDIA
# NIM отдавал HTTP 410 Gone на `deepseek-ai/deepseek-v4-pro-0813`, прогоны
# worker.yml 34991038287/35010410097 2026-09-15) сжигал весь элемент целиком —
# хотя АККАУНТ был жив и на том же эндпоинте лежали другие рабочие модели.
# Ротация аккаунтов (следующий элемент цепочки) и ротация МОДЕЛЕЙ внутри
# аккаунта — разные оси отказа: «этот ключ исчерпан» лечится сменой ключа,
# «этого id больше нет в каталоге» — сменой id, и подменять одно другим
# значит терять живой аккаунт на каждой ротации каталога провайдера.
#
# Форма элемента РАСШИРЕНА, не заменена — обе читаются одним местом правды:
#   {"model": "id", "max_output_tokens": N}                  — как было (один кандидат)
#   {"models": ["id1", "id2"], "max_output_tokens": N}       — общий потолок на всех
#   {"models": [{"id": "id1", "max_output_tokens": N1}, …]}  — свой потолок у каждой
# Смешение допустимо: `models` побеждает, `model` при её наличии не читается
# вовсе (одно место правды на элемент — иначе два списка кандидатов в одной
# записи расходятся молча). Отсутствие `max_output_tokens` у кандидата —
# значение элемента, отсутствие у элемента — 131072 (прежний дефолт, не
# новая величина).
dsh_entry_model_candidates() { # entry_json -> JSON-массив [{model,max_output_tokens}]
  jq -c '
    (.max_output_tokens // 131072) as $dflt
    | if (.models? | type) == "array" and ((.models | length) > 0) then
        [ .models[]
          | if type == "string" then {model: ., max_output_tokens: $dflt}
            else {model: (.id // .model), max_output_tokens: (.max_output_tokens // $dflt)}
            end ]
      else
        [ {model: .model, max_output_tokens: $dflt} ]
      end' <<<"$1"
}

# Первая модель первого элемента цепочки — та, которой воркер/руки
# ЗАТРАВЛИВАЮТ профиль до первого `dsh` и которую bootstrap-событие журнала
# называет кандидатом (#805). Отдельная функция, а не `jq -r '.[0].model'` по
# месту: с #1309 у элемента может не быть поля `model` вовсе, и три копии
# разбора в task.sh/dsh_task.sh разошлись бы молча (AGENTS.md, «Одно место
# правды»).
dsh_chain_head_model() { # chain_json -> id модели chain[0]
  dsh_entry_model_candidates "$(jq -c '.[0]' <<<"$1")" | jq -r '.[0].model'
}

dsh_chain_head_max_tokens() { # chain_json -> потолок ответа для chain[0]
  dsh_entry_model_candidates "$(jq -c '.[0]' <<<"$1")" | jq -r '.[0].max_output_tokens'
}

# Отказ привязан к ID МОДЕЛИ, а не к аккаунту (#1309). Разделение осей: пока
# провайдер отвечает «такой модели нет / она снята / её параметры не те» —
# аккаунт жив, и правильный следующий шаг это ДРУГАЯ МОДЕЛЬ того же
# аккаунта, а не следующий аккаунт. Прод-формы (все четыре — дословно из
# живых прогонов, не пересказ):
#   dsh: HTTP_410: DeepSeek API error (HTTP 410)      — id снят провайдером
#                                                       (worker.yml 35010410097)
#   dsh: HTTP_404: modelCode does not exist           — id не существует
#                                                       (прогон 33572445063, PR #190)
#   dsh: INVALID_REQUEST: max_tokens (131072) exceeds model's maximum output
#     tokens (65536) for model nemotron-3-ultra       — потолок ЭТОЙ модели
#                                                       (worker.yml 34730173870)
#   UNKNOWN_MODEL                                     — id не принят каталогом
#                                                       (worker.yml 34753001158, #1130)
#
# Функция НЕ решает, переключаться ли на следующего ПРОВАЙДЕРА — это
# по-прежнему dsh_chain_should_advance, и её решение не меняется ни для
# одного класса: при единственном кандидате (форма `model`, вся цепочка до
# #1309) поведение побайтно прежнее — модельного шага просто нет.
_dsh_failure_is_model_scoped() { # err_file
  grep -qE 'HTTP_410:|HTTP_404:|UNKNOWN_MODEL|INVALID_REQUEST: max_tokens' "$1" 2>/dev/null
}

# Исход ОДНОГО провайдера — машиночитаемая запись для итоговой сводки
# (#1307). Пишется РОВНО один раз на провайдера, в том же месте, где принято
# решение о нём: иначе сводка считалась бы вторым разбором тех же строк
# (AGENTS.md, «Классификация классов дефектов — в источнике, не
# постфактум-кластеризацией», ADR 0025).
_dsh_chain_record_outcome() { # name class detail
  DSH_CHAIN_OUTCOMES="${DSH_CHAIN_OUTCOMES}${1}	${2}	${3}
"
}

# Прогон по цепочке: пробует провайдеров ПО ПОРЯДКУ, пока один не ответит
# (rc=0) или список не кончится. Ретрай короткого RATE_LIMIT ВНУТРИ одного
# провайдера — не отменяется, остаётся заботой dsh_run_with_retry; чейн решает
# только «пробовать ли СЛЕДУЮЩЕГО».
#
# #877 (находка ai-review PR #880): DSH_RATE_LIMIT_MAX_WAIT_SECS — ОБЩИЙ
# бюджет ожидания на ВЕСЬ прогон цепочки, не полный бюджет заново на КАЖДОГО
# провайдера. Без этого арифметика бюджета таймаута (10 попыток × 20 мин
# укладываются в 280-минутный job) не учитывала бы, что застрявший в коротком
# RATE_LIMIT провайдер способен добавить к своему таймауту ещё до
# DSH_RATE_LIMIT_MAX_WAIT_SECS (по умолчанию 1800с/30 мин) ожидания — 10
# провайдеров × (20 мин таймаут + 30 мин ретрая) = 500 мин ≫ 280-минутного
# бюджета. Общий бюджет читается ОДИН раз в начале прогона цепочки (значение
# вызывающего), дальше расходуется по факту (DSH_RUN_WAITED_SECS от каждой
# попытки), не восстанавливается для следующего провайдера — «не начинать
# следующего провайдера с нуля впустую» в терминах ожидания RATE_LIMIT.
# Собственный таймаут КАЖДОГО провайдера (DSH_TIMEOUT_SECS, зависание) этим
# общим бюджетом не ограничен — это отдельная, независимая ось отказа.
#
# #1062 (рассмотрено, оставлено БЕЗ ИЗМЕНЕНИЙ): живой прогон worker.yml
# 34730173870 показал OpenRouter-2, потративший ВЕСЬ общий бюджет (1800с из
# 1800с) на свои ретраи — Ollama-2 после него стартовал с 0с. Разделить
# бюджет поровну на провайдера означало бы вернуть арифметику, которую этот
# же #877 закрывал: N провайдеров × собственный полный бюджет ожидания
# способны накопить существенно больше 280-минутного бюджета job'а (та же
# явная арифметика, что и выше). Поведение «провайдер с нулевым остатком
# делает РОВНО ОДНУ попытку без ретрая, вместо мгновенного отказа» — уже
# доказано сценарием 13 scripts/lib/test/dsh-provider-chain.smoke.sh: чейн
# продолжает пробовать СЛЕДУЮЩИХ провайдеров и с нулевым бюджетом (не
# «тормоз без газа» — газ есть, это одна попытка на провайдера, не полный
# отказ). Реальная причина остановки цепочки в этом инциденте — не
# исчерпание бюджета, а классификатор (см. #1062 в dsh_chain_should_advance
# ниже), который был чинён отдельно. Общий бюджет остаётся общим.
#
# #1121/#1124 (находка 3, живые прогоны worker.yml 34735752165/34739313568,
# 2026-09-13): подтверждено — OpenRouter-2 (третье звено) тратит ретраем ВЕСЬ
# общий бюджет (1650-1800с из 1800с) сам, следующему звену (OpenRouter-1)
# достаётся буквально 0с. Это НЕ то же самое, что #1062 отверг: #1062
# отверг СБРОС бюджета заново на каждого провайдера (та арифметика остаётся
# отвергнутой). Здесь — ПОТОЛОК на долю ОДНОГО провайдера ИЗ ТОГО ЖЕ общего
# пула (provider_wait_cap ниже): сумма по всем провайдерам физически
# ограничена тем же chain_rl_budget, как и раньше — ни один провайдер
# больше не может забрать его целиком один. Дефолт 300с (5 мин) — покрывает
# 4 шага экспоненциального бэкоффа (30+60+120+90=300, `DSH_RATE_LIMIT_INITIAL_
# DELAY_SECS`/`_MAX_DELAY_SECS` по умолчанию), при 1800с общего бюджета это
# даёт ДО 6 провайдеров (1800/300) реальный многошаговый шанс вместо одного
# монополиста и пяти «нулевых» смежников. Сообщение обязано честно называть
# И общий остаток, И применённый потолок — AGENTS.md «Алерт не гадает»,
# молчаливое урезание было бы тем же классом дефекта, что немой стоп-класс.
#
# Использование:
#   dsh_run_with_provider_chain <answer_file> <err_file> <prompt_text> [initial_rl_used]
# (вызывающий обязан вызвать dsh_require_provider_chain раньше и упасть
# громко, если vars.DSH_PROVIDER_CHAIN не задан — тот же контракт, что у
# dsh_require_provider_env/dsh_run_with_retry.)
#
# DSH_RATE_LIMIT_PROVIDER_CAP_SECS (необязательный, по умолчанию 300 — #1121/
# #1124) — потолок доли ОДНОГО провайдера из общего chain_rl_budget, см.
# комментарий выше. Не отдельный бюджет — ограничение сверху на ту же
# переменную, общий потолок (chain_rl_budget) не меняется.
#
# initial_rl_used (необязательный, по умолчанию 0) — сколько из общего
# бюджета RATE_LIMIT УЖЕ потрачено ДО этого вызова (находка ai-review PR
# #880): dsh_run_with_pool_then_chain пробует anthropic-oauth-pool ОДНИМ
# вызовом dsh_run_with_retry ДО цепочки, тем же общим DSH_RATE_LIMIT_MAX_WAIT_SECS
# — без передачи его собственного DSH_RUN_WAITED_SECS сюда цепочка получала
# бы общий бюджет заново, хотя пул — 1-я из 10 попыток прогона, не отдельная
# ось. Без этого поля комментарии «расходуется РОВНО один раз на весь
# прогон» были бы ложью применительно к прогонам с активным пулом (#838).
#
# Результат (переменные, вызывающий печатает свой отчёт):
#   DSH_RUN_RC              — код возврата ПОСЛЕДНЕЙ попытки (как у dsh_run_with_retry)
#   DSH_RUN_FAILURE_REASON  — "" на успехе | quota_exhausted/rate_limit_retry_budget_exceeded
#                             последнего провайдера | all_providers_exhausted (список кончился)
#   DSH_CHAIN_PROVIDER      — имя провайдера, ответившего успехом (пусто на отказе)
#   DSH_CHAIN_MODEL         — id модели, ответившей успехом (#1309; пусто на отказе)
#   DSH_CHAIN_TRIED         — имена всех опробованных провайдеров через ", "
#   DSH_CHAIN_MODELS_TRIED  — "провайдер/модель" каждой реальной попытки через ", "
#                             (#1309 — у элемента может быть несколько моделей;
#                             DSH_CHAIN_TRIED намеренно остаётся списком АККАУНТОВ)
#   DSH_CHAIN_RESET_HINT    — "имя: дата" для каждого провайдера с известной датой
#                             сброса, через "; " (пусто — ни один не назвал дату)
#   DSH_CHAIN_OUTCOMES      — по строке на провайдера: "имя<TAB>класс<TAB>деталь"
#   DSH_CHAIN_OUTCOME_SUMMARY — одна строка с ЧИСЛАМИ по классам (#1307)
#   DSH_CHAIN_RETRY_USEFUL  — 1, если хотя бы один провайдер не получил
#                             настоящей попытки (наш бюджет/транзиент) — повтор
#                             имеет смысл; 0 — все реально без квоты (#1307)
dsh_run_with_provider_chain() { # answer_file err_file prompt_text [initial_rl_used]
  local answer_file=$1 err_file=$2 prompt_text=$3 initial_rl_used="${4:-0}"
  local count i=0 stop=0 entry name base_url secret_env key reset_hint cap_note
  local quota_state_json
  local candidates cand_count ci cand model max_tokens
  local confirmed_any model_scoped_last provider_outcome provider_rl_used
  count=$(jq 'length' <<<"$DSH_PROVIDER_CHAIN")
  DSH_CHAIN_PROVIDER=""
  DSH_CHAIN_MODEL=""
  DSH_CHAIN_TRIED=""
  DSH_CHAIN_MODELS_TRIED=""
  DSH_CHAIN_RESET_HINT=""
  DSH_CHAIN_OUTCOMES=""
  DSH_CHAIN_OUTCOME_SUMMARY=""
  DSH_CHAIN_RETRY_USEFUL=0
  # #857: персистентное состояние квоты, разобрано ОДИН раз за весь прогон
  # цепочки (не на каждой итерации) — см. dsh_quota_state_validate выше.
  quota_state_json=$(dsh_quota_state_validate)
  # Как и dsh_run_with_retry, эта функция НИКОГДА не возвращает ненулевой код
  # сама (иначе `set -e` вызывающего оборвал бы скрипт ДО того, как он успеет
  # прочитать DSH_RUN_RC/DSH_RUN_FAILURE_REASON и напечатать свой отчёт) —
  # исход виден только через переменные, тот же контракт, что уже есть.
  DSH_RUN_RC=1
  DSH_RUN_FAILURE_REASON=""
  # Тормоз стыка с suite (#215, см. комментарий над dsh_patch_profile): пока
  # цикл цепочки идёт, dsh_patch_profile ВСЕГДА пишет плоский патч на
  # конкретного провайдера этой попытки, независимо от DSH_PLUGINS_SUITE_ACTIVE.
  # Восстанавливаем предыдущее значение на выходе — на случай, если эта
  # функция когда-нибудь будет вызвана изнутри другого чейна (сейчас не
  # вызывается, но не полагаемся на это).
  local _prev_chain_active="${DSH_CHAIN_ACTIVE:-}"
  DSH_CHAIN_ACTIVE=1
  # #877: общий бюджет ожидания RATE_LIMIT на весь прогон цепочки — см.
  # комментарий выше у объявления функции. Значение вызывающего читается
  # РОВНО один раз здесь, до цикла; rl_remaining ниже — то, что осталось.
  local chain_rl_budget="${DSH_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
  local chain_rl_used="$initial_rl_used" rl_remaining rl_true_remaining
  # #1121/#1124: потолок на долю ОДНОГО провайдера из общего пула — см.
  # комментарий выше у объявления функции. Читается ОДИН раз, как и
  # chain_rl_budget — не второй, независимый бюджет, а ограничение сверху
  # НА ТУ ЖЕ самую переменную rl_remaining.
  local provider_wait_cap="${DSH_RATE_LIMIT_PROVIDER_CAP_SECS:-300}"
  local provider_cap_left
  while [ "$i" -lt "$count" ] && [ "$stop" -eq 0 ]; do
    entry=$(jq -c ".[$i]" <<<"$DSH_PROVIDER_CHAIN")
    name=$(jq -r '.name' <<<"$entry")
    base_url=$(jq -r '.base_url' <<<"$entry")
    secret_env=$(jq -r '.secret_env' <<<"$entry")
    # #857: персистентная квота ДО секрета/подтверждения модели — самый
    # дешёвый гейт первым, не тратит jq-разбор на провайдера, который всё
    # равно будет пропущен. continue (не stop=1) — "all_providers_exhausted"
    # по-прежнему считается только когда i>=count, весь список пройден
    # (включая пропущенных гейтом) — правило AGENTS.md «Алерт не гадает»
    # держится тем же кодом, что и раньше (design.md, «Алерт уже не гадает»).
    if dsh_provider_quota_gate_skip "$name" "$quota_state_json"; then
      echo "::notice::цепочка провайдеров: $name пропущен — квота до $DSH_QUOTA_GATE_RESET (vars.DSH_PROVIDER_QUOTA_UNTIL, #857)" >&2
      DSH_CHAIN_TRIED="${DSH_CHAIN_TRIED:+$DSH_CHAIN_TRIED, }$name (пропущен: квота до $DSH_QUOTA_GATE_RESET)"
      DSH_CHAIN_RESET_HINT="${DSH_CHAIN_RESET_HINT:+$DSH_CHAIN_RESET_HINT; }$name: $DSH_QUOTA_GATE_RESET"
      _dsh_chain_record_outcome "$name" "quota_gate" "квота до $DSH_QUOTA_GATE_RESET"
      i=$((i + 1))
      continue
    fi
    DSH_CHAIN_TRIED="${DSH_CHAIN_TRIED:+$DSH_CHAIN_TRIED, }$name"
    key="${!secret_env:-}"
    if [ -z "$key" ]; then
      echo "::warning::цепочка провайдеров: $name пропущен — секрет $secret_env не передан этим workflow'ом" >&2
      _dsh_chain_record_outcome "$name" "no_secret" "секрет $secret_env не передан"
      i=$((i + 1))
      continue
    fi
    # #1309: кандидаты моделей ЭТОГО аккаунта по порядку. Прежняя форма
    # элемента (одна `model`) даёт ровно один кандидат — весь цикл ниже
    # вырождается в то же самое, чем он был до #1309.
    candidates=$(dsh_entry_model_candidates "$entry")
    cand_count=$(jq 'length' <<<"$candidates")
    confirmed_any=0
    model_scoped_last=0
    provider_outcome=""
    # #1309 + #1121: потолок доли считается на ПРОВАЙДЕРА, а не на кандидата
    # модели — иначе запись с тремя моделями получила бы три потолка подряд
    # (3×300с) и вернула бы ровно ту монополию бюджета, которую #1121
    # закрывал. Обнуляется на каждом провайдере, растёт по кандидатам.
    provider_rl_used=0
    ci=0
    while [ "$ci" -lt "$cand_count" ]; do
      cand=$(jq -c ".[$ci]" <<<"$candidates")
      model=$(jq -r '.model' <<<"$cand")
      max_tokens=$(jq -r '.max_output_tokens // 131072' <<<"$cand")
      ci=$((ci + 1))
      if ! dsh_model_confirmed "$model"; then
        echo "::error::цепочка провайдеров: $name пропущен — id модели '$model' НЕ подтверждён живым запросом к /v1/models (реестр $DSH_CONFIRMED_MODELS_FILE не несёт его хэш). Рунбук (docs/runbooks/switch-llm-provider.md, «Узнать точный id модели») запрещает экстраполяцию — сверь буква-в-букву и допиши подтверждение в реестр." >&2
        continue
      fi
      confirmed_any=1
      model_scoped_last=0
      DSH_CHAIN_MODELS_TRIED="${DSH_CHAIN_MODELS_TRIED:+$DSH_CHAIN_MODELS_TRIED, }$name/$model"
      rl_true_remaining=$((chain_rl_budget - chain_rl_used))
      [ "$rl_true_remaining" -lt 0 ] && rl_true_remaining=0
      rl_remaining="$rl_true_remaining"
      cap_note=""
      provider_cap_left=$((provider_wait_cap - provider_rl_used))
      [ "$provider_cap_left" -lt 0 ] && provider_cap_left=0
      if [ "$rl_remaining" -gt "$provider_cap_left" ]; then
        rl_remaining="$provider_cap_left"
        cap_note=", провайдеру выделено не больше ${rl_remaining}с (потолок ${provider_wait_cap}с на провайдера, #1121)"
      fi
      echo "цепочка провайдеров: пробую $name ($base_url, $model), остаток общего бюджета RATE_LIMIT: ${rl_true_remaining}с из ${chain_rl_budget}с (#877)${cap_note}"
      export DEEPSEEK_BASE_URL="$base_url" DEEPSEEK_MODEL="$model" DEEPSEEK_API_KEY="$key"
      DSH_MAX_TOKENS="$max_tokens" dsh_patch_profile headless
      DSH_RATE_LIMIT_MAX_WAIT_SECS="$rl_remaining" dsh_run_with_retry "$answer_file" "$err_file" "$prompt_text"
      chain_rl_used=$((chain_rl_used + ${DSH_RUN_WAITED_SECS:-0}))
      provider_rl_used=$((provider_rl_used + ${DSH_RUN_WAITED_SECS:-0}))
      if [ "$DSH_RUN_RC" -eq 0 ]; then
        DSH_CHAIN_PROVIDER="$name"
        DSH_CHAIN_MODEL="$model"
        DSH_RUN_FAILURE_REASON=""
        provider_outcome="ok"
        stop=1
        break
      fi
      reset_hint=$(dsh_extract_reset_hint "$err_file")
      [ -n "$reset_hint" ] && DSH_CHAIN_RESET_HINT="${DSH_CHAIN_RESET_HINT:+$DSH_CHAIN_RESET_HINT; }$name: $reset_hint"
      # #1309: отказ, привязанный к ID МОДЕЛИ, а не к аккаунту — следующая
      # модель ТОГО ЖЕ провайдера, аккаунт не расходуется. Ключевое: это
      # проверяется ДО dsh_chain_should_advance, но НЕ подменяет её — если
      # кандидаты кончились, решение о переходе к следующему аккаунту
      # принимает она же, тем же кодом, что и до #1309.
      if _dsh_failure_is_model_scoped "$err_file"; then
        model_scoped_last=1
        if [ "$ci" -lt "$cand_count" ]; then
          echo "::warning::цепочка провайдеров: $name — модель '$model' отвергнута самим провайдером (rc=$DSH_RUN_RC, отказ привязан к id модели, не к аккаунту): пробую следующую модель ЭТОГО же провайдера, аккаунт не расходуется (#1309)" >&2
          continue
        fi
      fi
      if dsh_chain_should_advance "$err_file" "$DSH_RUN_FAILURE_REASON" "$DSH_RUN_RC"; then
        echo "::warning::цепочка провайдеров: $name — rc=$DSH_RUN_RC, класс отказа: $DSH_CHAIN_CLASS_NOTE — пробую следующего" >&2
      else
        echo "::error::цепочка провайдеров: $name — rc=$DSH_RUN_RC, класс НЕ переключаемый (stderr: $DSH_CHAIN_CLASS_NOTE), дальше по цепочке не иду (следующие провайдеры не тронуты)" >&2
        provider_outcome="stop"
        stop=1
      fi
      break
    done
    # Исход провайдера классифицируется ЗДЕСЬ, в источнике решения (#1307).
    if [ -z "$provider_outcome" ]; then
      if [ "$confirmed_any" = 0 ]; then
        provider_outcome="unconfirmed_model"
      elif [ "$model_scoped_last" = 1 ]; then
        provider_outcome="dead_model"
      else
        case "$DSH_RUN_FAILURE_REASON" in
          quota_exhausted) provider_outcome="quota" ;;
          rate_limit_retry_budget_exceeded) provider_outcome="our_budget" ;;
          *) provider_outcome="transient" ;;
        esac
      fi
    fi
    _dsh_chain_record_outcome "$name" "$provider_outcome" "${DSH_CHAIN_CLASS_NOTE:-}"
    # i НЕ растёт при stop=1: признак «весь список пройден» ниже (i >= count)
    # обязан остаться ложным, когда цепочка остановлена решением, а не
    # исчерпанием списка — та же семантика, что была до #1307/#1309.
    if [ "$stop" -eq 0 ]; then
      i=$((i + 1))
    fi
  done
  if [ -z "$DSH_CHAIN_PROVIDER" ] && [ "$i" -ge "$count" ]; then
    DSH_RUN_FAILURE_REASON="all_providers_exhausted"
    _dsh_chain_report_exhausted "$count"
  fi
  DSH_CHAIN_ACTIVE="$_prev_chain_active"
}

# Итог цепочки числами, а не одной фразой (#1307, живой разбор прогона
# ai-review 34965204800 и worker.yml 35010410097). Прежнее сообщение —
# «цепочка провайдеров исчерпана целиком (GLM, OpenRouter-2, …)» — склеивало
# несовместимые классы: из восьми провайдений РЕАЛЬНО без квоты был ОДИН
# (GLM, сброс 2026-09-17), пять отвалились по НАШЕМУ исчерпанному бюджету
# ожидания (пул съел его целиком, см. dsh_run_with_pool_then_chain), два
# несли мёртвый id модели. Владелец читал «исчерпана целиком» и делал вывод
# «ждать сброса до 17-го», хотя шесть из восьми не были опробованы
# по-настоящему. AGENTS.md, «Алерт не гадает»: данные для различения уже
# лежат в DSH_CHAIN_OUTCOMES — сообщение обязано их назвать, а не предлагать
# читателю догадаться.
# Один и тот же локальный отказ, повторённый по числу провайдеров, — это НЕ
# восемь отказов провайдеров (#1470). Класс уже стоил репозиторию дважды:
#
#   #1315 (прогон worker.yml 35046585539): промпт не влез в аргумент, execve
#   вернул E2BIG, восемь провайдеров «отказали» за 78 секунд, не дойдя до сети
#   ни разу, а итог сказал «транзиентных отказов: 8, повтор ИМЕЕТ смысл».
#   Починили СЛУЧАЙ — поймали E2BIG до первой попытки. Класс остался открыт.
#
#   #1467 (2026-09-22, прогоны ai-review 35752743682/35752746475/35752840263):
#   транзитивная зависимость dsh обновилась в чужом реестре, dsh стал падать
#   на старте с «user patch-layer watching requires the Cordis HMR service»
#   за 0–1 секунду, ДО любого сетевого вызова. Итог снова: «транзиентных
#   отказов: 8 … Действие: повторить прогон». Совет был заведомо пустой —
#   я лично перезапустил три прогона, прежде чем прочитал стек.
#
# Признак не выдуман: он уже лежит в DSH_CHAIN_OUTCOMES. `DSH_CHAIN_CLASS_NOTE`
# — выдержка из stderr, и у настоящих отказов провайдеров она РАЗНАЯ (разные
# хосты, коды, тексты). Побайтно одинаковая выдержка у ВСЕХ опробованных — это
# один и тот же наш отказ, отпечатанный N раз.
#
# Условие намеренно узкое: совпасть обязаны ВСЕ опробованные (transient ==
# total), их не меньше двух, и выдержка непуста. Два провайдера из восьми с
# похожим таймаутом под это не попадут — и не должны: там повтор как раз
# осмыслен.
_dsh_chain_all_transient_alike() { # -> печатает общую выдержку, rc 0; иначе rc 1
  local nm cls note first="" seen=0 differ=0
  while IFS=$'\t' read -r nm cls note; do
    [ -n "$nm" ] || continue
    [ "$cls" = "transient" ] || return 1   # есть класс кроме transient — не наш случай
    [ -n "$note" ] || return 1             # пустая выдержка ничего не доказывает
    if [ "$seen" -eq 0 ]; then first="$note"; else
      [ "$note" = "$first" ] || differ=1
    fi
    seen=$((seen + 1))
  done <<<"$DSH_CHAIN_OUTCOMES"
  [ "$seen" -ge 2 ] || return 1
  [ "$differ" -eq 0 ] || return 1
  printf '%s' "$first"
}

_dsh_chain_report_exhausted() { # count
  local total=$1 quota=0 budget=0 config=0 transient=0 other=0
  local names_quota="" names_budget="" names_config="" names_transient=""
  local cls nm
  while IFS=$'\t' read -r nm cls _; do
    [ -n "$nm" ] || continue
    case "$cls" in
      quota|quota_gate)
        quota=$((quota + 1)); names_quota="${names_quota:+$names_quota, }$nm" ;;
      our_budget)
        budget=$((budget + 1)); names_budget="${names_budget:+$names_budget, }$nm" ;;
      no_secret|unconfirmed_model|dead_model)
        config=$((config + 1)); names_config="${names_config:+$names_config, }$nm ($cls)" ;;
      transient)
        transient=$((transient + 1)); names_transient="${names_transient:+$names_transient, }$nm" ;;
      *)
        other=$((other + 1)) ;;
    esac
  done <<<"$DSH_CHAIN_OUTCOMES"
  DSH_CHAIN_OUTCOME_SUMMARY="реально без квоты: $quota из $total; не пробованы по-настоящему (наш бюджет ожидания исчерпан): $budget; мёртвая конфигурация (нет секрета/неподтверждённый id/снятая моделью): $config; транзиентных отказов: $transient"
  # Проверяется ДО расчёта «повтор осмыслен»: иначе восемь отпечатков одного
  # нашего отказа продолжали бы поднимать флаг повтора (#1470).
  local alike=""
  if [ "$transient" -eq "$total" ] && alike=$(_dsh_chain_all_transient_alike); then
    DSH_CHAIN_RETRY_USEFUL=0
    DSH_RUN_FAILURE_REASON="tool_did_not_start"
    DSH_CHAIN_OUTCOME_SUMMARY="инструмент не стартовал: все $total опробованных отказали ОДИНАКОВО"
    echo "::error::инструмент не стартовал — это НАШ отказ, а не провайдеров: все $total опробованных ($DSH_CHAIN_TRIED) вернули одну и ту же ошибку «$alike». Повтор не поможет: провайдеры не при чём, падает то, что их зовёт. Действие: читать первую строку stderr выше и чинить инструмент/окружение (класс #1470, живые случаи #1315 и #1467)" >&2
    return 0
  fi
  if [ "$((budget + transient))" -gt 0 ]; then
    DSH_CHAIN_RETRY_USEFUL=1
  else
    DSH_CHAIN_RETRY_USEFUL=0
  fi
  if [ "$quota" -eq "$total" ]; then
    # Единственный случай, где прежняя формулировка верна буквально.
    echo "::error::цепочка провайдеров исчерпана целиком ($DSH_CHAIN_TRIED)${DSH_CHAIN_RESET_HINT:+ — сброс: $DSH_CHAIN_RESET_HINT} — все $total реально без квоты, повтор до сброса бессмысленен" >&2
    return 0
  fi
  local action=""
  [ "$budget" -gt 0 ] && action="${action:+$action; }освободить бюджет ожидания RATE_LIMIT (DSH_RATE_LIMIT_MAX_WAIT_SECS/DSH_RATE_LIMIT_PROVIDER_CAP_SECS) — у $budget провайдер(а/ов) ($names_budget) лимит не снялся в отведённой им доле бюджета: это НАШ тормоз, а не их квота"
  [ "$config" -gt 0 ] && action="${action:+$action; }починить конфигурацию: $names_config (docs/runbooks/switch-llm-provider.md, «Узнать точный id модели»)"
  [ "$transient" -gt 0 ] && action="${action:+$action; }повторить прогон — $transient транзиентный(х) отказ(ов) ($names_transient)"
  [ "$quota" -gt 0 ] && action="${action:+$action; }дождаться сброса квоты у: $names_quota${DSH_CHAIN_RESET_HINT:+ ($DSH_CHAIN_RESET_HINT)}"
  echo "::error::цепочка провайдеров не дала ответа, но НЕ «исчерпана целиком»: $DSH_CHAIN_OUTCOME_SUMMARY. Опробованы: $DSH_CHAIN_TRIED. Действие: ${action:-причину установить не удалось — ни один класс исхода не распознан, см. лог выше}" >&2
}

# #1288 (живой инцидент, прогон worker.yml 34893177035, 20:28:57→23:39:14 =
# 11417с/3ч10мин): пул отказал в 20:30:30 телом
# `{"type":"error","error":{"type":"pool_unavailable","message":"No Anthropic
# account is available","retryAt":1789418114634}}` — `retryAt` (мс с эпохи)
# называл момент возврата 20:35:14, через 284с/4м44с. Отказ был прочитан
# только как факт «пул недоступен» — момент возврата, лежавший в том же теле
# ответа, никто не разобрал, и прогон вместо ожидания 284с потратил все
# 11417с на резервную цепочку (три провайдера дали rate_limit_retry_budget_
# exceeded, GLM провисел полный DSH_TIMEOUT_SECS=7200с). AGENTS.md, «Алерт не
# гадает»: пул сам назвал факт, его нужно читать, не переспрашивать отказом.
#
# `retryAt` разбирается ТОЛЬКО из тела с `"type":"pool_unavailable"` (не из
# произвольного JSON, где поле `retryAt` могло бы значить что угодно другое)
# — см. `_dsh_pool_retry_at_wait_secs` ниже. Четыре исхода вместо одного:
#   1. Пул отказал НЕ формой pool_unavailable (сеть, 401/403, битый JSON,
#      просто текст) — как и раньше: одна попытка, откат на цепочку.
#   2. pool_unavailable + retryAt, момент близко (≤ DSH_POOL_RETRY_AT_MAX_
#      WAIT_SECS остатка бюджета ожидания) — ждём РОВНО названное время и
#      повторяем ТОТ ЖЕ пул (не цепочку) — прод-сценарий #1288 живёт здесь;
#      повторов НЕ БОЛЬШЕ DSH_POOL_RETRY_AT_MAX_RETRIES (см. ниже).
#   3. pool_unavailable + retryAt, момент дальше бюджета ожидания, ИЛИ поле
#      отсутствует/не число — откат на цепочку, но сообщение честно называет,
#      какой из двух случаев это был (не молчит, AGENTS.md «Алерт не гадает»).
#   4. Повторы из п.2 исчерпаны (пул продолжает отвечать занятостью — в т.ч.
#      с retryAt в прошлом/нуле, чьё «ожидание» не тратит бюджет вовсе) —
#      откат на цепочку, сообщение называет потолок повторов (блокер ai-review
#      PR #1292, вердикт rework: без потолка цикл не кончается никогда).
#
# DSH_POOL_RETRY_AT_MAX_WAIT_SECS (необязательный, по умолчанию 300с) —
# СУММАРНЫЙ бюджет ожидания retryAt за весь вызов этой функции (не за одну
# попытку — пул может отдать НОВЫЙ retryAt на повторе, цикл ниже вычитает
# уже проспанное, как dsh_run_with_retry вычитает RATE_LIMIT-ожидание из
# max_wait). Порог 300с — тот же уже принятый в этом файле для «сколько
# разумно ждать один провайдер, прежде чем считать его подвисшим»
# (DSH_RATE_LIMIT_PROVIDER_CAP_SECS, #1121, тот же дефолт 300с) — не новая
# цифра «на глаз» (AGENTS.md, «Порог обоснуй замером»), а переиспользование
# уже обоснованного порядка величины. Обоснование числом на живых данных:
# наблюдаемый retryAt инцидента #1288 — 284с, порог даёт ~16с запаса поверх
# единственного известного образца (честная оговорка — второго образца
# retryAt в репозитории нет, «не подтверждено» для распределения этой
# величины в общем случае). Цена ожидания против цены отказа от него —
# несимметрична на два порядка: до 300с ожидания против 11417с (3ч10мин),
# которые тот же прогон #1288 потратил на резервную цепочку без этого фикса
# — даже если бы бюджет ожидания был выжжен впустую (пул НЕ ответил и на
# повторе), проигрыш ограничен бюджетом (≤300с ожидания) ПЛЮС попытками
# пула — названы ОБЕ границы: норма по #1288/#1067 — секунды-десятки секунд
# на попытку; худший случай — DSH_POOL_RETRY_AT_MAX_RETRIES+1 полных попыток
# dsh_run_with_retry, каждая до DSH_TIMEOUT_SECS (7200с в worker.yml), т.е.
# ЧАСЫ (см. следующий абзац про «три полные попытки»), и сверку с
# 68-минутным запасом worker.yml надо считать по этой, большей, цифре.
# Общий
# 340-минутный потолок job'а (worker.yml, обоснование #1160/PR #1247: худший
# легитимный прогон ≈272 мин, ~68 мин запаса) этот бюджет не задевает — 300с
# на два порядка меньше свободного запаса.
#
# DSH_POOL_RETRY_AT_MAX_RETRIES (необязательный, по умолчанию 2) — потолок
# ПОВТОРОВ пула, отдельный от бюджета ожидания: retryAt в прошлом/нуле
# клампится к нулю, `sleep 0` НЕ расходует суммарный бюджет, и без отдельного
# счётчика цикл «пул занят → повтор немедленно» не кончается никогда (блокер
# ai-review PR #1292, вердикт rework; живой замер ревьюера: заглушка, вечно
# отвечающая телом инцидента с retryAt = now−5с, дала 582 вызова пула за
# 15с — процесс убит по таймауту, до цепочки дело не дошло). Значение 2 —
# исходная попытка плюс два повтора: закрывает прод-сценарий #1288 (одно
# ожидание 284с + повтор) с запасом ещё на одну занятость, худший случай
# ограничен тремя полными попытками dsh_run_with_retry. Замерной базы для
# распределения ИМЕННО ЧИСЛА повторов нет, «не подтверждено» — потолок
# переопределяется переменной без правки кода.
#
# Честная граница: `retryAt` — заявление ПУЛА, не факт, который мы можем
# проверить иначе, кроме как повторным запросом. Пул может ошибиться или
# сообщить время, которое всё равно окажется занятым, — цикл ниже не ждёт
# ДОЛЬШЕ суммарного бюджета И не делает БОЛЬШЕ DSH_POOL_RETRY_AT_MAX_RETRIES
# повторов ни при каких повторных retryAt (два газа на одном тормозе,
# AGENTS.md «тормоз без газа не принимается») и, исчерпав любой из них,
# честно откатывается на цепочку — не то же самое, что «ждать/повторять,
# пока пул не ответит», а именно ограниченная, разово обоснованная попытка
# не тратить резерв зря.
# Единая вырезка тела pool_unavailable из stderr (класс «жадная вырезка»,
# раунд 4 PR #1193): ПОСТРОЧНО, якорем `"type":"pool_unavailable"`, последнее
# вхождение — grep физически не выходит за пределы строки тела, чужой JSON
# до/после тела не прилипает. Одно место правды на
# dsh_pool_unavailable_owner_note и _dsh_pool_retry_at_wait_secs. redact()
# первым звеном маскирует производные секрета (#743), структуру тела не ломает.
_dsh_pool_unavailable_body() { # err_file -> подстрока от якоря до конца строки тела
  redact <"$1" 2>/dev/null | grep -oE '"type":"pool_unavailable".*' 2>/dev/null | tail -1
}

_dsh_pool_retry_at_wait_secs() { # err_file -> секунды до retryAt на stdout, rc=0 если разобрано
  local err_file=$1 pool_body retry_at_ms now_ms
  pool_body=$(_dsh_pool_unavailable_body "$err_file")
  [ -n "$pool_body" ] || return 1
  # Поля читаются тем же приёмом, что reason в dsh_pool_unavailable_owner_note:
  # вырезанная строка НЕ валидный JSON (обрезана якорем с обеих сторон), jq
  # сюда не годится — только ограниченные грепы по конкретным полям.
  retry_at_ms=$(printf '%s' "$pool_body" | grep -oE '"retryAt":[0-9]+' | head -1 | grep -oE '[0-9]+') || retry_at_ms=""
  [[ "$retry_at_ms" =~ ^[0-9]+$ ]] || return 1
  now_ms=$(( $(date +%s) * 1000 ))
  echo $(( (retry_at_ms - now_ms) / 1000 ))
  return 0
}

# ── Быстрый провайдер первым, цепочка — фоллбэком (#838) ────────────────────
#
# design.md anthropic-oauth-pool-standalone, «Стык с цепочкой провайдеров»:
# anthropic-oauth-pool и DSH_PROVIDER_CHAIN решают РАЗНЫЕ вопросы на разных
# протокольных путях (llm-pi-ai/anthropic-messages против llm-deepseek/
# openai-completions) — не комбинируются на одном уровне, как suite/цепочка
# (dsh_patch_profile выше), а идут ПОСЛЕДОВАТЕЛЬНО: пул пробуется первым
# ОДНИМ прогоном (сам пул уже перебирает все свои аккаунты на 429/401/403
# внутри одного HTTP-вызова, lib/index.js::forward плагина — повторять этот
# перебор снаружи циклом бессмысленно), при отказе — штатная
# dsh_run_with_provider_chain вызывается КАК ЕСТЬ, без изменений. Отдельно —
# #1288 выше: явный `retryAt` пула повторяет ТОТ ЖЕ пул, не переходит на
# цепочку раньше времени.
#
# Пул неактивен (DSH_ANTHROPIC_POOL_ACTIVE=0, нет секретов) — сразу цепочка,
# нулевое изменение поведения для конфигурации без пула.
#
# Использование и результат — тот же контракт, что у dsh_run_with_provider_chain
# (DSH_RUN_RC/DSH_RUN_FAILURE_REASON/DSH_CHAIN_PROVIDER/DSH_CHAIN_TRIED/
# DSH_CHAIN_RESET_HINT) — вызывающие (worker/hands/ai-review) читают ровно те
# же переменные, что и раньше, независимо от того, ответил пул или цепочка.
# ── Причина pool_unavailable — различить, нужен ли владелец (#1192) ─────────
#
# «pool_unavailable» сам по себе — агрегат «ни один аккаунт не доступен», он
# НЕ говорит, почему: 401/403 (креды отвергнуты Anthropic, нужен перевыпуск
# секретов ВЛАДЕЛЬЦЕМ) и 429 (лимит Anthropic, само пройдёт) выглядели
# снаружи одинаковым текстом. Патч плагина (classifyPoolUnavailable,
# lib/pool.js, scripts/lib/patch_anthropic_pool_plugin.py) кладёт
# машиночитаемое поле `reason` прямо в JSON-тело ответа — читаем его отсюда,
# не гадаем по тексту (AGENTS.md, «Алерт не гадает»). Поле извлекается
# регэкспом по СЫРОМУ stderr (не через jq на всю строку) — префикс "dsh:
# SERVER: 503 " и то, что JSON лежит внутри чужого текстового сообщения, не
# гарантируют валидный самостоятельный JSON-объект при наивной вырезке
# подстроки.
#
# Четыре исхода, каждый — ФАКТ, не гипотеза:
#   auth_rejected  — аккаунт(ы) отвергнуты Anthropic (401/403), владелец нужен;
#   rate_limited   — аккаунт(ы) исчерпали лимит (429), само пройдёт;
#   network_error  — исключение при попытке (не HTTP-ответ), не подтверждено;
#   unknown — в записанном состоянии аккаунтов нет ни 401/403, ни 429, ни
#     ошибки (аккаунт мог ни разу не пробоваться, либо последний ответ был
#     вне этих классов), не подтверждено;
#   поле reason отсутствует в теле — плагин сам не смог классифицировать,
#     либо патч не применился/апстрим сменил форму — так и сказано, без
#     подстановки одной из гипотез выше вместо честного пробела.
#
# Отказ пула МОЖЕТ вообще не быть отказом `pool_unavailable` (таймаут,
# ошибка соединения с локальным прокси, что угодно другое) — сообщение
# «поле reason отсутствует» иначе утверждало бы факт (тело pool_unavailable
# было и не несло reason), который в этом случае не проверялся: сначала
# смотрим, есть ли в stderr вообще тело `pool_unavailable`, и только тогда
# говорим про отсутствующее поле — иначе честно называем, что тела не нашли.
#
# `reason` вырезается ИЗ ТЕЛА pool_unavailable, не из всего err_file целиком
# (находка ai-review PR #1193, третий раунд): stderr может нести и чужой,
# не относящийся к пулу JSON/текст со СВОИМ полем `reason` — поиск по всему
# файлу подобрал бы его, приписав пулу чужой факт. `pool_body` — строка,
# содержащая `"type":"pool_unavailable"` (прод-форма — один
# `dsh: SERVER: 503 {...}` на строку); grep ПОСТРОЧЕН, поэтому `.*` в
# регэкспе физически не может выйти за пределы строки тела — находка
# ai-review PR #1193, четвёртый раунд: прежний `tr '\n' ' '` схлопывал весь
# err_file в ОДНУ строку ДО вырезки, и жадный `.*` забирал хвост файла,
# принимая чужой `reason` из любой последующей строки stderr за причину
# пула. `reason`/`retryAt` читаются только из вырезанной строки тела.
dsh_pool_unavailable_owner_note() { # err_file
  local err_file=$1 pool_reason pool_retry_at retry_note note has_body=0 pool_body
  # Вырезка тела — общая с _dsh_pool_retry_at_wait_secs (#1288): одно место
  # правды, построчный якорь вместо жадной вырезки (раунд 4 PR #1193).
  pool_body=$(_dsh_pool_unavailable_body "$err_file") || pool_body=""
  if [ -n "$pool_body" ]; then
    has_body=1
    pool_reason=$(printf '%s' "$pool_body" | grep -oE '"reason":"[a-z_]+"' | head -1 | sed -E 's/.*"reason":"([a-z_]+)".*/\1/') || pool_reason=""
  else
    pool_reason=""
  fi
  case "$pool_reason" in
    auth_rejected)
      note="ПРИЧИНА: аккаунт(ы) пула отвергнуты Anthropic (401/403) — нужен перевыпуск секретов ${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[*]}, владелец нужен"
      ;;
    rate_limited)
      pool_retry_at=$(printf '%s' "$pool_body" | grep -oE '"retryAt":[0-9]+' | head -1 | grep -oE '[0-9]+') || pool_retry_at=""
      retry_note=""
      if [ -n "$pool_retry_at" ]; then
        retry_note=", ретрай ~$(jq -nr --argjson ms "$pool_retry_at" '($ms/1000)|gmtime|strftime("%Y-%m-%d %H:%M:%SZ")' 2>/dev/null || printf '%s' "$pool_retry_at")"
      fi
      note="ПРИЧИНА: аккаунт(ы) пула исчерпали лимит Anthropic (429)${retry_note}, само пройдёт, владелец НЕ нужен"
      ;;
    network_error)
      note="ПРИЧИНА: сетевая ошибка при обращении к Anthropic (не 401/403/429) — владелец, вероятно, не нужен, но не подтверждено (проверь сеть/таймауты)"
      ;;
    unknown)
      # Нейтральная формулировка (находка ai-review PR #1193, раунд 4): класс
      # unknown в classifyPoolUnavailable шире, чем «ни разу не пробовался», —
      # он покрывает и записанный lastStatus вне {401,403,429} (200/5xx из
      # прошлых вызовов живут в runtime-состоянии так же, как 401). Формулировка
      # называет ТО, что реально проверено по записанному состоянию.
      note="причина не установлена: в записанном состоянии аккаунтов пула нет ни 401/403, ни 429, ни ошибки (не подтверждено, нужен ли владелец)"
      ;;
    "")
      if [ "$has_body" = 1 ]; then
        note="причина не классифицирована — тело pool_unavailable есть, но без поля reason (патч classifyPoolUnavailable не применился либо форма ответа изменилась), не подтверждено, нужен ли владелец"
      else
        note="причина не классифицирована — тело pool_unavailable в stderr не найдено (отказ пула, возможно, по другой причине), не подтверждено, нужен ли владелец"
      fi
      ;;
    *)
      note="причина не распознана (reason='$pool_reason', неизвестный класс) — не подтверждено, нужен ли владелец"
      ;;
  esac
  printf '%s' "$note"
}

# ── Поаккаунтная разбивка пула доходит до лога (#1310) ─────────────────────
#
# #1192 положил в тело `pool_unavailable` машиночитаемую разбивку `accounts`
# ([{id,class,lastStatus,cooldownUntil}]) — ровно то, что отвечает на вопрос
# «какой из двух ключей ещё живой». В лог она не попадала НИ РАЗУ: текстовый
# хвост (`pool_err_note`, 200 символов — #1067) обрывается буквально на
# `"accounts":[{"id":"anthropic-1","c` — дословно так в живых прогонах
# worker.yml 34942030597 (2026-09-15T08:04:16Z) и 35010410097
# (2026-09-15T19:28:38Z). То есть поле, добавленное ради различения
# «отвергнут» от «исчерпан», физически не доезжало до читателя, и
# агрегатный `reason` (плагин считает его через `some()`) оставался
# единственным, что видно: «rate_limited» верен и когда 429 у ОДНОГО
# аккаунта, а второй в этом процессе не пробовался вовсе.
#
# Разбивка вырезается ОТДЕЛЬНО от текстового хвоста и печатается целиком —
# секретов в ней нет по построению: только id аккаунта, класс и числовой код
# ответа (classifyPoolUnavailable, scripts/lib/patch_anthropic_pool_plugin.py).
# Вырезка идёт из уже redact-нутого тела (_dsh_pool_unavailable_body) и
# ограничена одним `[...]` без вложенных скобок — массив объектов без
# вложенных массивов, прод-форма плагина.
dsh_pool_accounts_note() { # err_file -> "anthropic-1: rate_limited (HTTP 429), anthropic-2: unknown"
  local pool_body accounts rendered
  pool_body=$(_dsh_pool_unavailable_body "$1") || pool_body=""
  if [ -z "$pool_body" ]; then printf '%s' ""; return 0; fi
  accounts=$(printf '%s' "$pool_body" | grep -oE '"accounts":\[[^]]*\]' | head -1) || accounts=""
  if [ -z "$accounts" ]; then printf '%s' ""; return 0; fi
  rendered=$(printf '%s' "${accounts#\"accounts\":}" | jq -r '
      [ .[]
        | "\(.id): \(.class)"
          + (if .lastStatus then " (HTTP \(.lastStatus))" else "" end)
      ] | join(", ")' 2>/dev/null) || rendered=""
  if [ -z "$rendered" ]; then
    rendered="разбивка по аккаунтам в теле есть, но не разобралась — форма ответа плагина изменилась (#1310)"
  fi
  printf '%s' "$rendered"
  return 0
}

dsh_run_with_pool_then_chain() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  local pool_rl_used=0 pool_err_note pool_reason_note pool_accounts_note
  # #1307 (живой инцидент, прогоны worker.yml 34942030597 и 35010410097
  # 2026-09-15): пул шёл через dsh_run_with_retry БЕЗ потолка доли
  # провайдера — тем же вызовом, что цепочка делает с
  # DSH_RATE_LIMIT_PROVIDER_CAP_SECS (#1121), но без него. Тело отказа пула
  # (`dsh: RATE_LIMIT: 503 {"type":"error",…"pool_unavailable"…}`) несёт
  # литерал `RATE_LIMIT:`, поэтому dsh_run_with_retry считал его обычным
  # временным лимитом и выжигал ВЕСЬ общий бюджет (1800с) на десяти
  # попытках; ветка retryAt (#1288) запускала пул ЗАНОВО — и снова с полным
  # бюджетом, потому что DSH_RATE_LIMIT_MAX_WAIT_SECS в этом цикле не
  # уменьшался. Замер: 07:31:18→08:41:56 = 70 минут сна до первой попытки
  # цепочки, после чего все восемь провайдеров получили «остаток общего
  # бюджета RATE_LIMIT: 0с из 1800с» и сдавались на первом же ответе
  # (`rate_limit_retry_budget_exceeded` у пяти из восьми) — комментарий ниже
  # обещал «пул — 1-я из 10 попыток прогона, не отдельная ось», код этого не
  # обеспечивал.
  #
  # Теперь пул — такой же потребитель ОДНОЙ доли, как любой элемент цепочки:
  # не больше DSH_RATE_LIMIT_PROVIDER_CAP_SECS (300с) СУММАРНО за все свои
  # попытки, включая повторы по retryAt, и не больше того, что осталось от
  # общего бюджета. Остаток уходит цепочке ровно тем же initial_rl_used, что
  # и раньше.
  local pool_rl_budget="${DSH_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
  local pool_wait_cap="${DSH_RATE_LIMIT_PROVIDER_CAP_SECS:-300}"
  local pool_share pool_budget_left
  local pool_retry_at_budget="${DSH_POOL_RETRY_AT_MAX_WAIT_SECS:-300}"
  local pool_retry_at_max_retries="${DSH_POOL_RETRY_AT_MAX_RETRIES:-2}"
  local pool_retry_at_waited=0 pool_retry_at_retries=0 retry_at_wait budget_left
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ]; then
    while :; do
      pool_budget_left=$((pool_rl_budget - pool_rl_used))
      [ "$pool_budget_left" -lt 0 ] && pool_budget_left=0
      pool_share=$((pool_wait_cap - pool_rl_used))
      [ "$pool_share" -lt 0 ] && pool_share=0
      [ "$pool_share" -gt "$pool_budget_left" ] && pool_share="$pool_budget_left"
      echo "быстрый провайдер: пробую Anthropic OAuth Pool (failover между аккаунтами — внутри одного вызова, lib/index.js плагина), доля общего бюджета RATE_LIMIT: ${pool_share}с (потолок ${pool_wait_cap}с на провайдера из ${pool_rl_budget}с, остальное остаётся цепочке, #1307)"
      _dsh_patch_profile_anthropic_pool headless
      DSH_RATE_LIMIT_MAX_WAIT_SECS="$pool_share" dsh_run_with_retry "$answer_file" "$err_file" "$prompt_text"
      if [ "$DSH_RUN_RC" -eq 0 ]; then
        DSH_CHAIN_PROVIDER="anthropic-oauth-pool"
        DSH_CHAIN_TRIED="anthropic-oauth-pool"
        DSH_CHAIN_RESET_HINT=""
        DSH_RUN_FAILURE_REASON=""
        return 0
      fi
      # #877/#880: пул — 1-я из 10 попыток прогона, не отдельная ось бюджета
      # RATE_LIMIT — потраченное им ожидание обязано вычитаться из общего
      # бюджета цепочки, иначе она получит полный бюджет заново (находка
      # ai-review PR #880 на первой версии этого фикса).
      pool_rl_used=$(( pool_rl_used + ${DSH_RUN_WAITED_SECS:-0} ))
      # #1067 (живой инцидент, прогон worker.yml 34735752165): раньше это
      # сообщение называло только rc — сам stderr пула читался бы из ТОГО ЖЕ
      # $err_file, что цепочка ниже перезаписывает на своей первой попытке
      # (dsh_run_with_retry всегда открывает err_file `>`, не дописывает) —
      # причина отказа пула терялась НАВСЕГДА, ни в одном логе прогона её не
      # найти (AGENTS.md, «Алерт не гадает»: rc=1 без единого слова причины —
      # то же самое гадание, только без вопросительного знака). Хвост читаем
      # ЗДЕСЬ, до перезаписи, тем же приёмом, что dsh_chain_should_advance уже
      # применяет к провайдерам цепочки (200 символов, redact).
      pool_err_note=$(tr '\n' ' ' <"$err_file" | cut -c1-200 | redact)
      [ -n "$pool_err_note" ] || pool_err_note="stderr пуст — диагностику дать не может"
      # #1192: reason читаем ДО той же перезаписи err_file, которой посвящён
      # комментарий #1067 выше — тем же приёмом («здесь, до перезаписи»).
      # Ребейз на c1df957 (#1192/#1193): note владельца печатается на КАЖДОМ
      # исходе пула, а не только на финальном откате — владелец видит
      # auth_rejected («владелец нужен») уже в первом предупреждении. ЭТО
      # НЕ ГЕЙТ: код ниже reason не читает и ожидание по нему не отменяет —
      # при auth_rejected ожидание/повторы могут оказаться заведомо
      # безнадёжными, вред ограничен потолками (бюджет 300с, ≤2 повтора);
      # гейт по reason — отдельное решение, не сделано в этом PR.
      pool_reason_note=$(dsh_pool_unavailable_owner_note "$err_file")
      # #1310: разбивка по аккаунтам — читается ЗДЕСЬ же, до перезаписи
      # err_file цепочкой, тем же приёмом, что reason выше.
      pool_accounts_note=$(dsh_pool_accounts_note "$err_file")
      [ -n "$pool_accounts_note" ] && pool_accounts_note=" — аккаунты: $pool_accounts_note"
      if retry_at_wait=$(_dsh_pool_retry_at_wait_secs "$err_file"); then
        budget_left=$((pool_retry_at_budget - pool_retry_at_waited))
        if [ "$budget_left" -gt 0 ] && [ "$retry_at_wait" -le "$budget_left" ]; then
          # Блокер ai-review PR #1292: retryAt в прошлом/нуле клампится к нулю
          # ниже, `sleep 0` не тратит бюджет — бюджет здесь НЕ ограничивает
          # цикл; единственный газ на этом пути — счётчик повторов.
          if [ "$pool_retry_at_retries" -ge "$pool_retry_at_max_retries" ]; then
            echo "::warning::быстрый провайдер Claude (anthropic-oauth-pool) занят (rc=$DSH_RUN_RC), причина: $pool_err_note — $pool_reason_note$pool_accounts_note — пул сам назвал момент возврата через ${retry_at_wait}с, исчерпан потолок повторов пула ($pool_retry_at_retries из $pool_retry_at_max_retries, остаток бюджета ожидания ${budget_left}с из ${pool_retry_at_budget}с) — не жду, пробую цепочку vars.DSH_PROVIDER_CHAIN/манифеста использования (#1288)"
            break
          fi
          [ "$retry_at_wait" -gt 0 ] || retry_at_wait=0
          echo "::warning::быстрый провайдер Claude (anthropic-oauth-pool) занят (rc=$DSH_RUN_RC), причина: $pool_err_note — $pool_reason_note$pool_accounts_note — пул сам назвал момент возврата через ${retry_at_wait}с (бюджет ожидания ${pool_retry_at_budget}с, уже ждал ${pool_retry_at_waited}с) — жду и повторяю тем же провайдером (#1288)"
          sleep "$retry_at_wait"
          pool_retry_at_waited=$((pool_retry_at_waited + retry_at_wait))
          pool_retry_at_retries=$((pool_retry_at_retries + 1))
          continue
        fi
        echo "::warning::быстрый провайдер Claude (anthropic-oauth-pool) занят (rc=$DSH_RUN_RC), причина: $pool_err_note — $pool_reason_note$pool_accounts_note — пул назвал момент возврата через ${retry_at_wait}с, это дольше остатка бюджета ожидания (${budget_left}с из ${pool_retry_at_budget}с, уже ждал ${pool_retry_at_waited}с) — не жду, пробую цепочку vars.DSH_PROVIDER_CHAIN/манифеста использования (#1288)"
        break
      fi
      echo "::warning::быстрый провайдер Claude (anthropic-oauth-pool) отказал (rc=$DSH_RUN_RC), причина: $pool_err_note — $pool_reason_note$pool_accounts_note — пробую цепочку vars.DSH_PROVIDER_CHAIN/манифеста использования (#838)"
      break
    done
  fi
  dsh_run_with_provider_chain "$answer_file" "$err_file" "$prompt_text" "$pool_rl_used"
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ]; then
    DSH_CHAIN_TRIED="anthropic-oauth-pool, ${DSH_CHAIN_TRIED}"
  fi
}

# ── Критерий «работа сделана В ЭТОМ ПРОГОНЕ» (issue #876, живой инцидент) ────
#
# Прогон worker.yml 34498185823 (задача #140, 2026-09-10) отрапортовал в
# задачу «🤖 Автономный воркер справился (провайдер: ?)», хотя dsh упал на
# ВСЕХ провайдерах цепочки этого прогона (rc=1, WORKER_CHAIN_PROVIDER пусто).
# Причина — старый критерий успеха worker/task.sh смотрел ТОЛЬКО на факт «PR
# по ветке существует» (pr_outcome.py), а PR #395 существовал с 2026-09-05
# (создан предыдущим прогоном) — предсуществующий артефакт был принят за
# результат текущего прогона. Пустой провайдер в тексте «справился» — само по
# себе противоречие: сообщение об успехе не может ссылаться на неизвестного
# провайдера.
#
# Критерий успеха — конъюнкция трёх фактов, ни один не достаточен в одиночку:
#   1. dsh ЭТОГО прогона завершился кодом 0 (rc == 0);
#   2. цепочка знает, КАКОЙ провайдер ответил (chain_provider непуст) —
#      dsh_run_with_provider_chain/dsh_run_with_pool_then_chain выставляют
#      его ТОЛЬКО при DSH_RUN_RC==0 (см. оба тела выше), поэтому это
#      утверждение логически эквивалентно (1), но проверяется отдельно как
#      защита от рассинхрона переменных в будущем — самой ошибке 34498185823
#      предшествовал именно рассинхрон (WORKER_CHAIN_PROVIDER читался ПОСЛЕ
#      того, как исход уже решался мимо него);
#   3. появился хотя бы один новый коммит за время этого прогона — В
#      РАБОЧЕМ ДЕРЕВЕ воркера (branch_start_sha != branch_end_sha) ИЛИ НА
#      ORIGIN ветки (origin_branch_start_sha != origin_branch_end_sha).
#      Второе — фикс регрессии #878 (задача #935, восемь провалов подряд
#      2026-09-11, идентичная сигнатура «PR ... существует, но не доказывает
#      работу этого прогона: новых коммитов в ветке за этот прогон нет» на
#      разных задачах): локальный чекаут воркера — линкованный git worktree,
#      чей admin-каталог (`.git/worktrees/<name>/`) лежит СНАРУЖИ рабочего
#      дерева, в общем commondir; файловая песочница dsh блокирует запись
#      именно туда (дословно из логов прогонов 34557341675/34559273918/
#      34578536716: «разделяемый .git главного чекаута оказался закрыт на
#      запись файловой песочницей»). Агент обходит блокировку отдельным
#      клоном и пушит НАПРЯМУЮ в origin — локальный HEAD воркера в таком
#      прогоне не двигается вовсе, хотя работа доезжает (живой пример — PR
#      #412 слит В РАМКАХ прогона 34550467839, локальные start/end SHA
#      совпали, а гейт до фикса всё равно кричал «нет коммитов»). Origin —
#      это то, что видно СНАРУЖИ независимо от того, как именно агент обошёл
#      песочницу, поэтому «ни один из двух не сдвинулся» — единственный
#      случай, который остаётся гэпом; если сдвинулся хотя бы один, coммит
#      этого прогона доказан. Предсуществующий PR, чью ветку этот прогон НЕ
#      трогал ни локально, ни на origin (живая форма #876), гэпом остаётся —
#      именно это #878 и закрывал, регрессия здесь не возвращается.
#
# Наличие PR по ветке (pr_outcome.py, #413) остаётся ОТДЕЛЬНЫМ, необходимым,
# но БОЛЬШЕ НЕ достаточным условием — вызывающий обязан проверить оба
# (пример — scripts/worker/task.sh, шаг «8. Пост-обработка»).
#
# Не подтверждено (находка ai-review PR #880, второй раунд; #910 п.4): ни
# локальный, ни origin-признак не отличают НОВЫЙ коммит от переписанной
# истории — `git rebase`/`commit --amend` тоже двигают SHA (и локально, и на
# origin после пуша) без единой строки новой работы. Маршрут воркера
# предписывает DSH ребейзить при дрейфе main (шаг 3 `route_pr_step`), и
# такой ребейз внутри прогона теоретически мог бы дать тот же зелёный
# вердикт без работы этого прогона — узкая форма того же класса #876, НЕ
# закрытая этим фиксом (#935 её тоже не закрывает — см. proposal.md).
# Усиление признака (датировка коммитов относительно начала прогона, сверка
# с головой PR) — не сделано, честно названо, а не тихо допущено.
#
# DSH_WORKER_RUN_GATE_GAPS (переменная, не возврат) — единственное место
# правды на ТЕКСТ несработавших конъюнктов (находка ai-review PR #880:
# раньше task.sh пересчитывал те же три условия ВТОРОЙ копией для
# диагностики — при эволюции гейта список гэпов молча протухал бы). Текст
# гэпа #3 называет ОБА факта (локальный HEAD и голову ветки на origin) —
# не гадает, какой из двух источников агент использовал (AGENTS.md
# «Алерт не гадает»). Пустая строка на успехе; иначе — предложения через
# ", ", каждое называет КОНКРЕТНО несработавшее условие.
dsh_worker_run_is_success() { # rc chain_provider branch_start_sha branch_end_sha origin_branch_start_sha origin_branch_end_sha
  local rc=$1 chain_provider=$2 start_sha=$3 end_sha=$4 origin_start=$5 origin_end=$6
  local gaps=""
  [ "$rc" -eq 0 ] \
    || gaps="${gaps:+$gaps, }dsh этого прогона завершился кодом $rc (не 0)"
  [ -n "$chain_provider" ] \
    || gaps="${gaps:+$gaps, }ни один провайдер не ответил успехом"
  if [ "$start_sha" = "$end_sha" ] && [ "$origin_start" = "$origin_end" ]; then
    local origin_note="не изменилась ($origin_start)"
    [ -n "$origin_start" ] || origin_note="так и не появилась"
    gaps="${gaps:+$gaps, }новых коммитов за этот прогон нет ни в рабочем дереве воркера (HEAD не сдвинулся: $start_sha), ни в ветке на origin ($origin_note)"
  fi
  DSH_WORKER_RUN_GATE_GAPS="$gaps"
  [ -z "$gaps" ]
}
