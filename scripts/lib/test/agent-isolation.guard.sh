#!/usr/bin/env bash
# Гвардия изоляции адаптера модели (#140): dsh обязан идти под выделенным
# агент-юзером без группы docker. Гвардия исполняет ПРОДАКШН-путь
# (dsh_agent_isolation_prepare из scripts/lib/dsh-ci.sh) с настоящим sudo на
# одноразовом агент-юзере и проверяет свойства изоляции как факты окружения,
# а не шаги:
#   1. env_keep реально проводит секретные по ИМЕНИ переменные агент-юзеру
#      (фальшивые значения — не секреты; реальные живут только в памяти sudo);
#   2. environ процессов транспорта (другой uid) агент-юзеру не читается;
#   3. docker-сокет агент-юзеру недоступен (закрытый эскейп #140);
#   4. в режиме nogh у агента нет gh-авторизации (граница #18);
#   5. подготовка идемпотентна (переиспользуемый раннер).
# Класс: «транспорт запустил dsh мимо изоляции» — красный smoke на любом PR.
#
# Замер, который стоит за свойствами: docs/research/40-model-shell-key-exposure.md
# (прямой /proc-чтение закрыто yama ptrace_scope=1, docker-эскейп читал ключ живьём).
#
# Запуск: bash scripts/lib/test/agent-isolation.guard.sh  (нужны sudo и docker,
# как на GitHub-раннерах; локально без sudo гвардия честно падает, не молчит)
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"

# shellcheck source=../dsh-ci.sh
source "$REPO/scripts/lib/dsh-ci.sh"

sudo -n true 2>/dev/null \
  || { echo "::error::ГВАРДИЯ #140: sudo недоступен — изоляцию невозможно ни установить, ни проверить" >&2; exit 1; }

TMP="$(mktemp -d)"
GUARD_USER="dsh-agent-guard$$"   # ≤32 символов, одноразовый
GUARD_SUDOERS="/etc/sudoers.d/99-$GUARD_USER-env"
cleanup() {
  sudo userdel -r "$GUARD_USER" >/dev/null 2>&1 || true
  sudo rm -f "$GUARD_SUDOERS" || true
  rm -rf "$TMP"
}
trap cleanup EXIT

# Фальшивые значения секретных ПО ИМЕНИ переменных: проверяем проводку env_keep,
# а не секреты (в репозитории секретов нет ни в тестах, ни в примерах).
export DSH_AGENT_USER="$GUARD_USER"
export DEEPSEEK_API_KEY="guard-fake-key-value"
export DEEPSEEK_BASE_URL="https://guard.invalid/v1"
export DEEPSEEK_MODEL="guard-model"

WS="$TMP/ws"; AGENT_DIR="$TMP/agent"; LAUNCHER="$TMP/launcher.sh"
mkdir -p "$WS" "$AGENT_DIR"
printf 'guard\n' >"$WS/file.txt"

dsh_agent_isolation_prepare nogh "$WS" "$AGENT_DIR" "$LAUNCHER"

fail() { echo "::error::ГВАРДИЯ #140: $*" >&2; exit 1; }
# 1. Пользователь существует и НЕ в группе docker (ядро-факт, не настройка).
id -u "$GUARD_USER" >/dev/null || fail "агент-юзер не создан"
if id -nG "$GUARD_USER" | tr ' ' '\n' | grep -qx docker; then
  fail "агент-юзер в группе docker — эскейп #140 открыт"
fi

# 2. env_keep проводит секретные по имени переменные (фальшивые значения).
# Все проверки — через ПРОДАКШН-вход dsh_agent_run (настоящий sudo -u): то же
# ядро-граница, что у живого прогона. Без sudo домен агента неотличим от
# транспорта, и проверка вырождалась бы в пустой успех.
# shellcheck disable=SC2016
crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_API_KEY"')"
[ "$crossed" = "guard-fake-key-value" ] || fail "env_keep не провёл DEEPSEEK_API_KEY агенту (получено '$crossed')"
# shellcheck disable=SC2016
crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_MODEL"')"
[ "$crossed" = "guard-model" ] || fail "env_keep не провёл DEEPSEEK_MODEL агенту"

# 3. Environ транспорта (другой uid) не читается.
sleep 30 & probe=$!
# shellcheck disable=SC2016
if dsh_agent_run bash -c "cat /proc/$probe/environ" >/dev/null 2>&1; then
  kill "$probe" 2>/dev/null || true
  fail "агент прочитал environ транспорта — изоляция uid не работает"
fi
kill "$probe" 2>/dev/null || true

# 4. Docker недоступен (сокет есть на GitHub-раннерах; отказ обязан быть по правам).
if [ -S /var/run/docker.sock ]; then
  derr="$(dsh_agent_run timeout 10 docker version 2>&1 || true)"
  echo "$derr" | grep -qi "permission denied" || fail "docker у агента не отклонён по правам: $derr"
else
  echo "::note::docker-сокета нет — свойство проверяется только prepare"
fi

# 5. Режим nogh: у агента нет gh-авторизации (граница #18).
if dsh_agent_run test -e "$DSH_AGENT_HOME/.config/gh/hosts.yml"; then
  fail "у ревью-агента нашёлся hosts.yml — граница #18 нарушена"
fi

# 6. sudoers-файл установлен и валиден (visudo — арбитр, не наш grep).
sudo test -f "$GUARD_SUDOERS" || fail "sudoers env_keep файл не установлен"
sudo visudo -cf "$GUARD_SUDOERS" >/dev/null || fail "установленный sudoers-файл не валиден"

# 7. Идемпотентность: повторная подготовка на существующем юзере проходит.
dsh_agent_isolation_prepare nogh "$WS" "$AGENT_DIR" "$LAUNCHER" \
  || fail "повторный вызов prepare не идемпотентен"

echo "ГВАРДИЯ #140: изоляция адаптера доказана (uid-домен, docker-denied, env_keep, nogh-граница)"
