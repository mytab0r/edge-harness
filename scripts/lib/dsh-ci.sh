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

# Выбор модели и лимит ответа — через родной settings-слой профиля, НЕ env:
# адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
dsh_patch_profile() { # $1 — имя профиля (обычно headless); [$2 — куда писать файл патча]
  # $2 нужен при изоляции агента (#140): транспорт пишет патч в СВОЙ каталог,
  # а в дом агента его ставит сам агент (dsh_agent_run install) — транспорт не
  # имеет прав на запись в /home/<agent>. По умолчанию (без $2) — прежнее
  # поведение для локальных запусков без изоляции. Выставляет DSH_MODEL/DSH_MAX_TOKENS.
  local profile=$1
  local out="${2:-$HOME/.dsh/profiles/$profile/cordis.patch.yml}"
  # Модель обязана прийти из окружения (vars.DEEPSEEK_MODEL, #153) — здесь
  # больше нет зашитого дефолта. Вызывающий обязан вызвать
  # dsh_require_provider_env раньше и упасть громко, если модель не задана.
  : "${DEEPSEEK_MODEL:?DEEPSEEK_MODEL не задан — dsh_require_provider_env должен был отказать раньше}"
  DSH_MODEL="$DEEPSEEK_MODEL"
  DSH_MAX_TOKENS="${DSH_MAX_TOKENS:-131072}"
  mkdir -p "$(dirname "$out")"
  cat >"$out" <<PATCH
- id: agent-default-model
  config:
    provider: deepseek-official
    model: $DSH_MODEL
- id: llm-deepseek
  config:
    maxTokens: $DSH_MAX_TOKENS
PATCH
}

# ── Изоляция адаптера модели: отдельный uid для dsh (#140) ─────────────────────────
# Замер 2026-09-05 живым прогоном из model-shell
# (docs/research/40-model-shell-key-exposure.md): вырезание env (*KEY*/*TOKEN*/
# *SECRET*) из model-shell вызовов НЕ гарантирует «агент ключ не видит».
# Прямое чтение /proc/<pid-dsh>/environ блокирует ядро (yama ptrace_scope=1 на
# GitHub-раннерах: environ читается только у собственных потомков), НО доступный
# model-shell сокет docker даёт полный обход того же класса: контейнер
# `--pid=host --cap-add SYS_PTRACE --security-opt apparmor=unconfined` читает
# environ любого процесса хоста, включая dsh. Подтверждено прогоном: в environ
# dsh лежат DEEPSEEK_API_KEY, GH_TOKEN, HANDS_TOKEN, TELEGRAM_BOT_TOKEN,
# DSH_EDGE_ACCESS_KEY.
#
# Решение — «отдельный uid» из критерия #140: dsh (держатель ключа) стартует под
# выделенным непривилегированным пользователем БЕЗ группы docker. model-shell
# наследует этот uid, поэтому: сокет docker (root:docker 660) агенту недоступен
# (EACCES), environ процессов транспорта (другой uid) недоступен, /proc/<pid-dsh>/
# environ по-прежнему закрыт yama. Ключ по-прежнему нужен только dsh-процессу и
# передаётся ТОЛЬКО ему — через sudoers env_keep (память sudo), не argv и не файлы:
# argv читаем всеми через /proc/*/cmdline, файл в домене агента читал бы сам агент.
#
# Gas (правило «тормоз без газа»): агент-юзер без docker — осознанный тормоз.
# Возврат: задача, которой нужен docker на раннере, эскалирует владельцу
# (blocked), потому что при ключе в env dsh доступ к docker = доступ к ключу.
# Снять тормоз может только вынос ключа с раннера (ключ держит процесс на другой
# машине / платная песочница) — решение владельца, не воркера.

DSH_AGENT_USER="${DSH_AGENT_USER:-dsh-agent}"
DSH_AGENT_HOME=""      # заполняет dsh_agent_isolation_prepare
DSH_AGENT_LAUNCHER=""  # заполняет dsh_agent_isolation_prepare

dsh_agent_isolation_prepare() { # MODE(gh|nogh) WORKSPACE AGENT_DIR LAUNCHER_FILE
  # MODE=gh — агенту нужен gh/git-push (worker, hands): зеркало gh-конфига.
  # MODE=nogh — доверенная граница #18 (ai-review): gh-авторизации у агента
  # быть НЕ должно, протухшее зеркало сносится, отсутствие проверяется.
  local mode=$1 workspace=$2 agent_dir=$3 launcher=$4
  DSH_AGENT_HOME="/home/$DSH_AGENT_USER"
  DSH_AGENT_LAUNCHER=$launcher
  [ -d "$workspace" ] || { echo "::error::изоляция #140: нет каталога воркспейса $workspace" >&2; return 1; }
  # Секреты едут к агенту через env_keep, модель обязана быть задана раньше.
  [ -n "${DEEPSEEK_MODEL:-}" ] || { echo "::error::DEEPSEEK_MODEL не задан — dsh_require_provider_env должен был отказать раньше" >&2; return 1; }
  if ! sudo -n true 2>/dev/null; then
    echo "::error::sudo недоступен — изоляция адаптера модели (#140) невозможна, а запуск dsh без неё запрещён: model-shell с доступом к docker читает ключ из environ dsh (docs/research/40-model-shell-key-exposure.md). Газ: вынос ключа с раннера — решение владельца" >&2
    return 1
  fi

  # 1. Пользователь: создать или переиспользовать (идемпотентно на живом раннере).
  if ! id -u "$DSH_AGENT_USER" >/dev/null 2>&1; then
    sudo useradd -m -s /bin/bash "$DSH_AGENT_USER" \
      || { echo "::error::не смог создать агент-юзера $DSH_AGENT_USER" >&2; return 1; }
    local created_uid
    created_uid="$(id -u "$DSH_AGENT_USER" 2>/dev/null || true)"
    echo "Агент-юзер $DSH_AGENT_USER создан${created_uid:+ (uid $created_uid)}"
  fi
  # 1а. Дом транспорта обязан пропускать «только проход» (x без r): воркспейс,
  # RUNNER_TEMP и лаунчер лежат под $HOME (/home/runner на GitHub-раннерах,
  # дефолт 750) — без этого агент физически не дойдёт до своего cwd, спула и
  # лаунчера. Найдено CI-гвардией #140 на настоящем sudo; Smoke это показать
  # не может (sudo заглушен — uid-граница отсутствует).
  if [ "$(stat -c %u "$HOME" 2>/dev/null)" = "$(id -u)" ] \
      && [ "$(stat -c %a "$HOME" | cut -c3)" != 7 ]; then
    sudo chmod 711 "$HOME"
    echo "::warning::$HOME был без прохода для других uid — открыт 711: агенту нужен проход до воркспейса, спула и лаунчера (#140)"
  fi
  # 2. Группа docker агенту запрещена: на этой машине она равна доступу к ключу
  # (#140). Чиним сами (самовосстановление на переиспользуемом раннере), не молча.
  if id -nG "$DSH_AGENT_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    sudo gpasswd -d "$DSH_AGENT_USER" docker \
      || { echo "::error::не смог убрать $DSH_AGENT_USER из группы docker — изоляция #140 неполна" >&2; return 1; }
    echo "::warning::$DSH_AGENT_USER состоял в группе docker — убран (изоляция #140)"
  fi
  # 3. pnpm для `dsh plugin add` под агентом: action-setup кладёт его в
  # /home/runner (750) — агенту туда не пройти. Ставим ту же версию в общий
  # префикс node (доступен агенту на чтение/исполнение), лаунчер ставит его
  # первым в PATH.
  if [ "$mode" = gh ]; then
    command -v pnpm >/dev/null || { echo "::error::pnpm не найден — dsh plugin add без него не работает" >&2; return 1; }
    npm install -g "pnpm@$(pnpm --version)" >/dev/null \
      || { echo "::error::не смог поставить pnpm в общий префикс node для агент-юзера" >&2; return 1; }
  fi
  # 4. gh-конфиг: зеркало для gh/nogh-снос для nogh.
  if [ "$mode" = gh ]; then
    [ -f "$HOME/.config/gh/hosts.yml" ] \
      || { echo "::error::нет $HOME/.config/gh/hosts.yml — агент без gh-авторизации не откроет PR; шаг gh auth login обязан идти раньше" >&2; return 1; }
    sudo mkdir -p "$DSH_AGENT_HOME/.config"
    sudo cp -r "$HOME/.config/gh" "$DSH_AGENT_HOME/.config/gh"
    [ -f "$HOME/.gitconfig" ] && sudo cp "$HOME/.gitconfig" "$DSH_AGENT_HOME/.gitconfig"
  else
    # Протухшее зеркало на переиспользуемом раннере нарушило бы границу #18.
    sudo rm -rf "$DSH_AGENT_HOME/.config/gh"
  fi
  sudo chown -R "$DSH_AGENT_USER:$DSH_AGENT_USER" "$DSH_AGENT_HOME"
  # 5. Секреты — только через env_keep (память sudo). Валидация до установки.
  local sudoers_file="/etc/sudoers.d/99-$DSH_AGENT_USER-env"
  local sudoers_tmp; sudoers_tmp="$(mktemp)"
  cat >"$sudoers_tmp" <<SUDOERS
# Изоляция адаптера модели (#140): секреты и спул едут в $DSH_AGENT_USER
# через env_keep (память sudo), не через argv (читаем всем через
# /proc/*/cmdline) и не через файлы (домен агента читает сам агент).
Defaults env_keep += "DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL HANDS_SPOOL GH_REPO"
SUDOERS
  if ! sudo visudo -cf "$sudoers_tmp" >/dev/null; then
    rm -f "$sudoers_tmp"
    echo "::error::sudoers-файл env_keep не валиден — изоляция #140 не установлена" >&2
    return 1
  fi
  sudo install -m 440 "$sudoers_tmp" "$sudoers_file"
  rm -f "$sudoers_tmp"
  # 6. Лаунчер: PATH/HOME/флаг pnpm — не секреты, задаются здесь явно. Секреты
  # через него не идут. Файл в домене транспорта: агент читает, но не пишет —
  # менять его агенту выгоды нет, он и так исполняется его uid.
  local node_bin; node_bin="$(dirname "$(command -v node)")"
  mkdir -p "$agent_dir"
  cat >"$launcher" <<LAUNCHER
#!/usr/bin/env bash
# Мост окружения транспорт → агент-юзер (#140). Секретов здесь нет: они едут
# через sudoers env_keep. PATH/HOME/флаг pnpm — не секреты.
set -euo pipefail
# PATH транспорта первым (заглушки теста обязаны выигрывать у реальных
# бинарников), каталог node — в хвосте как страховка на минимальный PATH.
export PATH="\$1:$node_bin"; shift
export HOME="$DSH_AGENT_HOME" DSH_HOME="$DSH_AGENT_HOME/.dsh"
export npm_config_ignore_workspace_root_check=true
exec "\$@"
LAUNCHER
  # 7. Воркспейс и агент-каталог — во владение агенту: модель пишет файлы
  # (git, спул) своим uid, транспорт после прогона читает их (644).
  sudo chown "$DSH_AGENT_USER:$DSH_AGENT_USER" "$agent_dir"
  sudo chown -R "$DSH_AGENT_USER:$DSH_AGENT_USER" "$workspace"
  # 8. Доказательства изоляции — до запуска dsh, каждое громкое.
  # 8а. Позитив: env_keep реально проводит переменные (механика sudo работает).
  # Одинарные кавычки ниже обязательны: раскрыть переменную обязан
  # агент-домен, не транспорт.
  # shellcheck disable=SC2016
  local crossed
  crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_MODEL"')"
  if [ "$crossed" != "$DEEPSEEK_MODEL" ]; then
    echo "::error::env_keep не провёл DEEPSEEK_MODEL агент-юзеру (получено '$crossed') — запуск без ключа молча бы сломал модель" >&2
    return 1
  fi
  # 8а-бис. Агент обязан достигать воркспейса (свой cwd): проход по $HOME
  # и правам на каталоги — иначе dsh стартует в недостижимом каталоге.
  if ! dsh_agent_run test -d "$workspace"; then
    echo "::error::агент-юзер не видит воркспейс $workspace (проход по $HOME/каталогам?) — cwd dsh был бы недостижим" >&2
    return 1
  fi
  # 8б. Негатив: environ процессов транспорта агенту не читается. Проверка
  # осмыслена ТОЛЬКО на настоящей смене uid (ядро запрещает читать environ
  # процесса другого uid без CAP_SYS_PTRACE). Под заглушкой sudo (smoke) домен
  # агента неотличим от транспорта по uid — там проверять нечего, и «пропуск»
  # не считается успехом: прода и CI-гвардия (agent-isolation.guard.sh)
  # проверяют её на настоящем uid-барьере.
  local agent_uid
  agent_uid="$(id -u "$DSH_AGENT_USER" 2>/dev/null || true)"
  if [ -n "$agent_uid" ] && [ "$agent_uid" != "$(id -u)" ]; then
    sleep 30 & local probe=$!
    # cat обязан исполниться в домене агента, не в транспорте.
    # shellcheck disable=SC2016
    if dsh_agent_run bash -c "cat /proc/$probe/environ" >/dev/null 2>&1; then
      kill "$probe" 2>/dev/null || true
      echo "::error::агент-юзер прочитал environ транспорта — изоляция uid не состоялась, запуск запрещён (#140)" >&2
      return 1
    fi
    kill "$probe" 2>/dev/null || true
  else
    echo "::note::uid транспорта совпадает с агент-юзером (sudo заглушен?) — негативная проверка environ пропущена, её накрывает гвардия CI"
  fi
  # 8в. Негатив: docker недоступен агенту (закрытый эскейп #140). Три исхода:
  # permission denied — защита доказана; «cannot connect» — демона нет, маршрут
  # закрыт тем более (uid-барьер держит и при последующем старте демона, сокет
  # по-прежнему 660 root:docker); иное — неизвестная среда, громкий отказ.
  if [ -S /var/run/docker.sock ]; then
    local derr
    derr="$(dsh_agent_run timeout 10 docker version 2>&1 || true)"
    if echo "$derr" | grep -qi "permission denied"; then
      echo "Изоляция #140 доказана: docker у агент-юзера — permission denied"
    elif echo "$derr" | grep -qi "cannot connect"; then
      echo "::note::docker-демон не отвечает — эскейп-маршрут закрыт вдвойне; uid-барьер проверит гвардия CI"
    else
      echo "::error::docker у агент-юзера упал не по правам доступа — изоляция не доказана: $derr" >&2
      return 1
    fi
  else
    echo "::note::docker-сокета на раннере нет — негативная проверка docker пропущена"
  fi
  # 9. Режимные проверки: gh работает у агента (gh) / gh-конфига нет (nogh).
  if [ "$mode" = gh ]; then
    dsh_agent_run gh auth status >/dev/null 2>&1 \
      || { echo "::error::gh под агент-юзером не авторизован — зеркало gh-конфига не сработало" >&2; return 1; }
  else
    dsh_agent_run test ! -e "$DSH_AGENT_HOME/.config/gh/hosts.yml" \
      || { echo "::error::у ревью-агента нашёлся hosts.yml — доверенная граница #18 нарушена" >&2; return 1; }
  fi
  local final_uid
  final_uid="$(id -u "$DSH_AGENT_USER" 2>/dev/null || true)"
  echo "Изоляция адаптера модели установлена: dsh пойдёт под $DSH_AGENT_USER${final_uid:+ (uid $final_uid)}, docker и environ транспорта ему недоступны (#140)"
}

dsh_agent_run() { # cmd args... — исполнить команду от агент-юзера (изоляция #140)
  : "${DSH_AGENT_LAUNCHER:?dsh_agent_isolation_prepare не вызван}"
  : "${DSH_AGENT_USER:?DSH_AGENT_USER не задан}"
  sudo -n -H -u "$DSH_AGENT_USER" bash "$DSH_AGENT_LAUNCHER" "$PATH" "$@"
}
