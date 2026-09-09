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
DSH_VERSION="0.1.1-rc.2"
DSH_INTEGRITY="sha512-UP1UIh6q3Gme/yXRn/QL2P8IsVlv8Shpg22TRJIZPsCRWLm4CBiA1MUvXmJAfsOEETBMLAl+xWPtFw6ICsN3wg=="
DSH_HEADLESS_VERSION="0.1.1-rc.2"
DSH_HEADLESS_INTEGRITY="sha512-Pk50xwmUUehOxNe8DJ2/tThj7Aw1MmJQeUkfAQh9miF7Tm+WOOxiOOei/H4wjH9cf+FuqtbLDw6jrHmGotfhjw=="

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
    npm pack "@deepseek-ai/dsh@$DSH_VERSION" "@deepseek-ai/dsh-headless@$DSH_HEADLESS_VERSION"
    local dsh_tgz="deepseek-ai-dsh-$DSH_VERSION.tgz"
    local hl_tgz="deepseek-ai-dsh-headless-$DSH_HEADLESS_VERSION.tgz"
    [ -f "$dsh_tgz" ] || dsh_tgz=$(find . -maxdepth 1 -name "*dsh-$DSH_VERSION.tgz" | head -1)
    [ -f "$hl_tgz" ] || hl_tgz=$(find . -maxdepth 1 -name "*dsh-headless-$DSH_HEADLESS_VERSION.tgz" | head -1)
    dsh_verify_integrity "$dsh_tgz" "$DSH_INTEGRITY"
    dsh_verify_integrity "$hl_tgz" "$DSH_HEADLESS_INTEGRITY"
    npm install -g ./*.tgz
  )
  command -v dsh >/dev/null
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
  if ! dsh plugin --profile "$profile" add "$DSH_PLUGINS_SUITE_OAUTH_PKG"; then
    echo "::error::dsh plugin add не смонтировал dsh-anthropic-oauth-pool — suite не смонтировался (#215)"
    echo "::endgroup::"; return 1
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
  if ! grep -q '^- id: anthropic-oauth-pool$' <<<"$dump"; then
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
dsh_import_anthropic_accounts() {
  [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ] || return 0
  echo "::group::Импорт аккаунтов Anthropic OAuth pool (#838)"
  local secret_name value creds_file imported=0 idx=0 account_id
  for secret_name in "${ANTHROPIC_OAUTH_ACCOUNT_SECRETS[@]}"; do
    idx=$((idx + 1))
    account_id="anthropic-$idx"
    value="${!secret_name:-}"
    if [ -z "$value" ]; then
      continue
    fi
    creds_file=$(mktemp)
    chmod 600 "$creds_file"
    printf '%s' "$value" >"$creds_file"
    if ! node "$DSH_ANTHROPIC_POOL_EXTRACTED/bin/dsh-anthropic-pool.js" add "$account_id" "$creds_file"; then
      rm -f "$creds_file"
      unset "$secret_name"
      echo "::error::dsh-anthropic-pool add $account_id не смог импортировать секрет $secret_name — быстрый провайдер Claude не подключён (#838)"
      echo "::endgroup::"; return 1
    fi
    rm -f "$creds_file"
    unset "$secret_name"
    imported=$((imported + 1))
    echo "аккаунт $account_id импортирован из секрета $secret_name (значение удалено из окружения)"
  done
  if [ "$imported" = 0 ]; then
    # Недостижимо в норме: dsh_install_anthropic_pool уже проверил has_secret
    # ДО того, как выставил DSH_ANTHROPIC_POOL_ACTIVE=1. Fail loud на
    # несоответствие инварианта, а не тихий проход с пустым пулом.
    echo "::error::DSH_ANTHROPIC_POOL_ACTIVE=1, но ни один секрет не дал импортируемый аккаунт — рассинхрон инварианта (#838)"
    echo "::endgroup::"; return 1
  fi
  echo "::endgroup::"
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
# регистрирует себя отдельным провайдером в llm-pi-ai.providers.anthropic-pool
# (lib/index.js::ensureProvider плагина, PROVIDER_KEY='anthropic-pool',
# api: 'anthropic-messages' — другой протокольный путь, не openai-completions
# llm-deepseek). "claude-sonnet-4-5" — статический дефолт из models плагина
# (lib/index.js), используется как id модели для agent-default-model; живым
# прогоном с реальными аккаунтами не подтверждено (design.md, «Не
# подтверждено» — гонка ensureProvider()/discoverModels() относительно
# резолва agent-default-model на первом реальном запросе).
_dsh_patch_profile_anthropic_pool() { # $1 — профиль
  local profile=$1
  local patch="$HOME/.dsh/profiles/$profile/cordis.patch.yml"
  mkdir -p "$(dirname "$patch")"
  cat >"$patch" <<PATCH
- id: agent-default-model
  config:
    provider: anthropic-pool
    model: claude-sonnet-4-5
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
dsh_run_with_retry() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  local max_wait="${DSH_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
  local delay="${DSH_RATE_LIMIT_INITIAL_DELAY_SECS:-30}"
  local max_delay="${DSH_RATE_LIMIT_MAX_DELAY_SECS:-300}"
  local timeout_secs="${DSH_TIMEOUT_SECS:-3600}"
  local waited=0 attempt=1 wait_left rc
  DSH_RUN_FAILURE_REASON=""
  while :; do
    echo "dsh: попытка $attempt (суммарно уже ждал ${waited}с из бюджета ${max_wait}с)"
    set +e
    timeout "$timeout_secs" dsh --profile headless "$prompt_text" \
      >"$answer_file" 2>"$err_file"
    rc=$?
    set -e
    echo "dsh завершился с кодом $rc (попытка $attempt)"

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
}

# Класс отказа → переход на следующего провайдера. failure_reason — уже
# посчитанный dsh_run_with_retry (quota_exhausted/rate_limit_retry_budget_
# exceeded/пусто); при пустом — второй, более грубый признак: буквальный
# текст HTTP_404/EMPTY_RESPONSE в stderr (прод-форма — «dsh: HTTP_404:
# modelCode does not exist», scripts/lib/test/dsh-clients.smoke.sh). Любая
# другая нераспознанная ошибка (ключ битый, DSH сам сломан, битый запрос) —
# «stop», цепочка НЕ идёт дальше: следующий провайдер тем же кодом/запросом
# не спасёт, а время/квоту потратит.
#
# #737 (живой случай — прогон 34188152283, NVIDIA rc=1, НИ ОДНОГО байта в
# stderr): третий, отдельный признак — сигнала нет вообще (stderr пуст или
# состоит только из пробелов). Заведомо-нелечимый сменой провайдера отказ
# (битый ключ, нарушение контракта вердикта, битый аргумент запроса) ВСЕГДА
# печатает диагностическую строку — тишина неотличима от временного
# транспортного сбоя, ради которого цепочка и строилась (proposal.md,
# «повторяемый транспортный отказ… переключают… без участия человека»). Цена
# ошибки при выборе «переключаемый» здесь — потратить время следующего
# провайдера; цена выбора «стоп» — молчать всю оставшуюся цепочку без единой
# причины, как и случилось. По умолчанию — переключаемся.
#
# DSH_CHAIN_CLASS_NOTE (переменная, не возврат) — человекочитаемая причина
# решения для сообщения вызывающего (правило AGENTS.md «Алерт не гадает»):
# имя признанной причины, литеральный маркер stderr, факт «stderr пуст» или
# первая строка нераспознанной диагностики (стоп-класс).
#
# Ветка «стоп-класс» ниже — единственная, что кладёт в переменную СЫРОЙ
# фрагмент stderr клиента модели, а не заранее известную безопасную строку
# (имя причины/литеральный маркер/факт пустоты) — и именно она обязана идти
# через redact() (#743): начало ответа 401 у провайдера типично содержит эхо
# заголовка Authorization, GitHub маскирует только точное совпадение секрета,
# производное (подстрока/префикс) — нет. Маскируем здесь, у источника, ОДИН
# раз — оба места печати (::warning:: и ::error:: ниже) читают уже
# замаскированную переменную, второй копии redact на каждую точку вывода не
# нужно (то же место правды, что redact() выше в этом файле).
dsh_chain_should_advance() { # err_file failure_reason
  local err_file=$1 reason=$2
  case "$reason" in
    quota_exhausted|rate_limit_retry_budget_exceeded)
      DSH_CHAIN_CLASS_NOTE="$reason"
      return 0 ;;
  esac
  if grep -qE 'HTTP_404:|EMPTY_RESPONSE:' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="HTTP_404/EMPTY_RESPONSE в stderr"
    return 0
  fi
  if [ ! -s "$err_file" ] || ! grep -qE '[^[:space:]]' "$err_file"; then
    DSH_CHAIN_CLASS_NOTE="stderr пуст — диагностику дать не может, класс не установить, консервативно пробую следующего"
    return 0
  fi
  DSH_CHAIN_CLASS_NOTE="$(tr '\n' ' ' <"$err_file" | cut -c1-200 | redact)"
  return 1
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

# Прогон по цепочке: пробует провайдеров ПО ПОРЯДКУ, пока один не ответит
# (rc=0) или список не кончится. Ретрай короткого RATE_LIMIT ВНУТРИ одного
# провайдера — не отменяется, остаётся заботой dsh_run_with_retry; чейн решает
# только «пробовать ли СЛЕДУЮЩЕГО».
#
# Использование:
#   dsh_run_with_provider_chain <answer_file> <err_file> <prompt_text>
# (вызывающий обязан вызвать dsh_require_provider_chain раньше и упасть
# громко, если vars.DSH_PROVIDER_CHAIN не задан — тот же контракт, что у
# dsh_require_provider_env/dsh_run_with_retry.)
#
# Результат (переменные, вызывающий печатает свой отчёт):
#   DSH_RUN_RC              — код возврата ПОСЛЕДНЕЙ попытки (как у dsh_run_with_retry)
#   DSH_RUN_FAILURE_REASON  — "" на успехе | quota_exhausted/rate_limit_retry_budget_exceeded
#                             последнего провайдера | all_providers_exhausted (список кончился)
#   DSH_CHAIN_PROVIDER      — имя провайдера, ответившего успехом (пусто на отказе)
#   DSH_CHAIN_TRIED         — имена всех опробованных провайдеров через ", "
#   DSH_CHAIN_RESET_HINT    — "имя: дата" для каждого провайдера с известной датой
#                             сброса, через "; " (пусто — ни один не назвал дату)
dsh_run_with_provider_chain() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  local count i=0 stop=0 entry name base_url model secret_env max_tokens key reset_hint
  count=$(jq 'length' <<<"$DSH_PROVIDER_CHAIN")
  DSH_CHAIN_PROVIDER=""
  DSH_CHAIN_TRIED=""
  DSH_CHAIN_RESET_HINT=""
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
  while [ "$i" -lt "$count" ] && [ "$stop" -eq 0 ]; do
    entry=$(jq -c ".[$i]" <<<"$DSH_PROVIDER_CHAIN")
    name=$(jq -r '.name' <<<"$entry")
    base_url=$(jq -r '.base_url' <<<"$entry")
    model=$(jq -r '.model' <<<"$entry")
    secret_env=$(jq -r '.secret_env' <<<"$entry")
    max_tokens=$(jq -r '.max_output_tokens // 131072' <<<"$entry")
    DSH_CHAIN_TRIED="${DSH_CHAIN_TRIED:+$DSH_CHAIN_TRIED, }$name"
    key="${!secret_env:-}"
    if [ -z "$key" ]; then
      echo "::warning::цепочка провайдеров: $name пропущен — секрет $secret_env не передан этим workflow'ом" >&2
      i=$((i + 1))
      continue
    fi
    if ! dsh_model_confirmed "$model"; then
      echo "::error::цепочка провайдеров: $name пропущен — id модели '$model' НЕ подтверждён живым запросом к /v1/models (реестр $DSH_CONFIRMED_MODELS_FILE не несёт его хэш). Рунбук (docs/runbooks/switch-llm-provider.md, «Узнать точный id модели») запрещает экстраполяцию — сверь буква-в-букву и допиши подтверждение в реестр." >&2
      i=$((i + 1))
      continue
    fi
    echo "цепочка провайдеров: пробую $name ($base_url, $model)"
    export DEEPSEEK_BASE_URL="$base_url" DEEPSEEK_MODEL="$model" DEEPSEEK_API_KEY="$key"
    DSH_MAX_TOKENS="$max_tokens" dsh_patch_profile headless
    dsh_run_with_retry "$answer_file" "$err_file" "$prompt_text"
    if [ "$DSH_RUN_RC" -eq 0 ]; then
      DSH_CHAIN_PROVIDER="$name"
      DSH_RUN_FAILURE_REASON=""
      stop=1
      continue
    fi
    reset_hint=$(dsh_extract_reset_hint "$err_file")
    [ -n "$reset_hint" ] && DSH_CHAIN_RESET_HINT="${DSH_CHAIN_RESET_HINT:+$DSH_CHAIN_RESET_HINT; }$name: $reset_hint"
    if dsh_chain_should_advance "$err_file" "$DSH_RUN_FAILURE_REASON"; then
      echo "::warning::цепочка провайдеров: $name — rc=$DSH_RUN_RC, класс отказа: $DSH_CHAIN_CLASS_NOTE — пробую следующего" >&2
      i=$((i + 1))
      continue
    fi
    echo "::error::цепочка провайдеров: $name — rc=$DSH_RUN_RC, класс НЕ переключаемый (stderr: $DSH_CHAIN_CLASS_NOTE), дальше по цепочке не иду (следующие провайдеры не тронуты)" >&2
    stop=1
  done
  if [ -z "$DSH_CHAIN_PROVIDER" ] && [ "$i" -ge "$count" ]; then
    DSH_RUN_FAILURE_REASON="all_providers_exhausted"
    echo "::error::цепочка провайдеров исчерпана целиком ($DSH_CHAIN_TRIED)${DSH_CHAIN_RESET_HINT:+ — сброс: $DSH_CHAIN_RESET_HINT}" >&2
  fi
  DSH_CHAIN_ACTIVE="$_prev_chain_active"
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
# dsh_run_with_provider_chain вызывается КАК ЕСТЬ, без изменений.
#
# Пул неактивен (DSH_ANTHROPIC_POOL_ACTIVE=0, нет секретов) — сразу цепочка,
# нулевое изменение поведения для конфигурации без пула.
#
# Использование и результат — тот же контракт, что у dsh_run_with_provider_chain
# (DSH_RUN_RC/DSH_RUN_FAILURE_REASON/DSH_CHAIN_PROVIDER/DSH_CHAIN_TRIED/
# DSH_CHAIN_RESET_HINT) — вызывающие (worker/hands/ai-review) читают ровно те
# же переменные, что и раньше, независимо от того, ответил пул или цепочка.
dsh_run_with_pool_then_chain() { # answer_file err_file prompt_text
  local answer_file=$1 err_file=$2 prompt_text=$3
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ]; then
    echo "быстрый провайдер: пробую Anthropic OAuth Pool (failover между аккаунтами — внутри одного вызова, lib/index.js плагина)"
    _dsh_patch_profile_anthropic_pool headless
    dsh_run_with_retry "$answer_file" "$err_file" "$prompt_text"
    if [ "$DSH_RUN_RC" -eq 0 ]; then
      DSH_CHAIN_PROVIDER="anthropic-oauth-pool"
      DSH_CHAIN_TRIED="anthropic-oauth-pool"
      DSH_CHAIN_RESET_HINT=""
      DSH_RUN_FAILURE_REASON=""
      return 0
    fi
    echo "::warning::быстрый провайдер Claude (anthropic-oauth-pool) отказал (rc=$DSH_RUN_RC) — пробую цепочку vars.DSH_PROVIDER_CHAIN/манифеста использования (#838)"
  fi
  dsh_run_with_provider_chain "$answer_file" "$err_file" "$prompt_text"
  if [ "${DSH_ANTHROPIC_POOL_ACTIVE:-0}" = "1" ]; then
    DSH_CHAIN_TRIED="anthropic-oauth-pool, ${DSH_CHAIN_TRIED}"
  fi
}
