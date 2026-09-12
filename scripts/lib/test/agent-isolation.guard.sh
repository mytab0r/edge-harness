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
#   5. подготовка идемпотентна (переиспользуемый раннер);
#   6. живой замер НАСТОЯЩЕЙ пары «model-shell → environ dsh» (#140): потомок
#      stand-in dsh читает environ родителя в продакшн exec-цепочке
#      (sudo → launcher → timeout → fork → exec образа), с фейстурным ключом.
# Класс: «транспорт запустил dsh мимо изоляции» — красный smoke на любом PR.
#
# Замер, который стоит за свойствами: docs/research/40-model-shell-key-exposure.md
# (прямой /proc-чтение предков не подтвердился в живом замере, но механизм запрета
# не атрибутирован — за средой следит зонд prepare; docker-эскейп читал ключ живьём).
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
chmod 755 "$TMP"   # агент-юзер обязан проходить к лаунчеру и каталогам (mktemp даёт 700)
GUARD_USER="dsh-agent-guard$$"   # ≤32 символов, одноразовый
GUARD_SUDOERS="/etc/sudoers.d/99-$GUARD_USER-env"
cleanup() {
  dsh_provider_proxy_stop 2>/dev/null || true   # держатель ключа (#140)
  [ -n "${STUB_UP_PID:-}" ] && kill "$STUB_UP_PID" 2>/dev/null || true
  sudo userdel -r "$GUARD_USER" >/dev/null 2>&1 || true
  sudo rm -f "$GUARD_SUDOERS" || true
  sudo rm -rf "$TMP"   # часть дерева принадлежит агент-юзеру — сносит только root
}
trap cleanup EXIT

# Фальшивые значения секретных ПО ИМЕНИ переменных: проверяем проводку env_keep,
# а не секреты (в репозитории секретов нет ни в тестах, ни в примерах). Значение
# DEEPSEEK_API_KEY короче 20 символов: детерминированный гейт сканирует
# добавленные строки паттерном KEY="<20+ символов>" — фикстура не должна в него
# попадать (та же причина, что у dsh-clients.smoke.sh).
export DSH_AGENT_USER="$GUARD_USER"
export DEEPSEEK_API_KEY="guard-fake-key"
export DEEPSEEK_BASE_URL="https://guard.invalid/v1"
export DEEPSEEK_MODEL="guard-model"

WS="$TMP/ws"; AGENT_DIR="$TMP/agent"; LAUNCHER="$TMP/launcher.sh"
mkdir -p "$WS" "$AGENT_DIR"
printf 'guard\n' >"$WS/file.txt"

# Прокси-держатель ключа (#140, замер 5) — часть продакшн-пути: prepare без
# него обязан отказать (проверяем это отдельной мутацией ниже), поэтому сначала
# доказываем отказ, потом запускаем прокси и идём по основному пути.
if dsh_agent_isolation_prepare nogh "$WS" "$AGENT_DIR" "$LAUNCHER" 2>/dev/null; then
  fail "prepare прошёл БЕЗ прокси-держателя ключа — запуск с реальным ключом в env dsh не остановлен (замер 5: агент читает environ родителя)"
fi

dsh_provider_proxy_start "$TMP"
dsh_agent_isolation_prepare nogh "$WS" "$AGENT_DIR" "$LAUNCHER"

fail() { echo "::error::ГВАРДИЯ #140: $*" >&2; exit 1; }
# 1. Пользователь существует и НЕ в группе docker (ядро-факт, не настройка).
id -u "$GUARD_USER" >/dev/null || fail "агент-юзер не создан"
if id -nG "$GUARD_USER" | tr ' ' '\n' | grep -qx docker; then
  fail "агент-юзер в группе docker — эскейп #140 открыт"
fi

# 2. env_keep проводит переменные по имени. С замером 5 (#140) агенту едет
# ПОДМЕНА: DEEPSEEK_API_KEY = подменный ключ прокси (не реальная фикстура),
# DEEPSEEK_BASE_URL = 127.0.0.1 прокси; DEEPSEEK_MODEL — как есть. Все
# проверки — через ПРОДАКШН-вход dsh_agent_run (настоящий sudo -u).
# shellcheck disable=SC2016
crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_API_KEY"')"
[ "$crossed" = "$DSH_PROXY_DUMMY_KEY" ] || fail "агент получил не подменный ключ прокси (получено '$crossed')"
[ "$crossed" != "guard-fake-key" ] || fail "агент получил РЕАЛЬНУЮ фикстуру ключа — прокси-подмена не сработала (#140)"
case "$crossed" in "$DSH_PROXY_DUMMY_PREFIX"-*) : ;; *) fail "подменный ключ не той формы: $crossed" ;; esac
# shellcheck disable=SC2016
crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_MODEL"')"
[ "$crossed" = "guard-model" ] || fail "env_keep не провёл DEEPSEEK_MODEL агенту"
# shellcheck disable=SC2016
crossed="$(dsh_agent_run bash -c 'printf %s "$DEEPSEEK_BASE_URL"')"
case "$crossed" in http://127.0.0.1:*) : ;; *) fail "агент получил не локальный эндпоинт прокси (получено '$crossed')" ;; esac

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

# 5б. Handover: воркспейс передан агенту (транспорт после этого в нём не пишет).
# Сам вызов — часть продакшн-пути: prepare сознательно НЕ трогает воркспейс
# (между prepare и прогоном транспорт исполняет свои git-команды в своём uid).
dsh_agent_handover || fail "handover упал"
[ "$(stat -c %u "$WS")" = "$(id -u "$GUARD_USER")" ] \
  || fail "handover не передал воркспейс агент-юзеру"

# 6. sudoers-файл установлен и валиден (visudo — арбитр, не наш grep).
sudo test -f "$GUARD_SUDOERS" || fail "sudoers env_keep файл не установлен"
sudo visudo -cf "$GUARD_SUDOERS" >/dev/null || fail "установленный sudoers-файл не валиден"

# 7. Идемпотентность: повторная подготовка на существующем юзере проходит.
dsh_agent_isolation_prepare nogh "$WS" "$AGENT_DIR" "$LAUNCHER" \
  || fail "повторный вызов prepare не идемпотентен"

# 8. ЖИВОЙ ЗАМЕР НАСТОЯЩЕЙ ПАРЫ «model-shell → environ dsh» (#140; требование
# ревью #395). Это НЕ прокси-пара зонда 8г prepare: там родитель читающего —
# первый post-sudo образ (на одну exec мельче), здесь stand-in dsh запущен той
# же продакшн-командой dsh_agent_run timeout … и читающий — его прямой потомок,
# то есть точная пара из названия задачи, только с фейстурным ключом вместо
# реального (проводка env_keep доказана в п.2 — механика та же). Результат —
# запись, а не ассерт: ожидание фиксируется ПО результату этого замера
# (research/40), ассерт добавляется следом отдельным коммитом, чтобы гвардия
# не «подглядывала» ответ до замера.
PAIR_DIR="$TMP/pair"; mkdir -p "$PAIR_DIR"
cat >"$PAIR_DIR/pair-child.sh" <<'PAIR_CHILD'
#!/usr/bin/env bash
# Роль model-shell: прямой потомок stand-in dsh. Читает environ РОДИТЕЛЯ —
# ровно вектор из названия задачи #140. Печатает только вердикт и факт
# непустоты значения по ИМЕНИ, никогда — значения.
set -u
verdict() {
  local pid=$1 label=$2
  # ВАЖНО: без $(cat) — command substitution выбрасывает NUL-байты (находка
  # контрольно-контейнерного замера #140), после неё tr '\0' '\n' нечего
  # разбивать и имя переменной находится, только если оно первое. Читаем пайпом.
  if ! cat "/proc/$pid/environ" 2>/dev/null | grep -qa ''; then
    echo "PAIR-VERDICT target=$label pid=$pid DENIED"
    return
  fi
  if cat "/proc/$pid/environ" 2>/dev/null | tr '\0' '\n' | grep -q '^DEEPSEEK_API_KEY=..*'; then
    echo "PAIR-VERDICT target=$label pid=$pid READABLE DEEPSEEK_API_KEY=present-nonempty"
  else
    echo "PAIR-VERDICT target=$label pid=$pid READABLE DEEPSEEK_API_KEY=absent-or-empty"
  fi
}
me=$$
parent="$(awk '{print $4}' "/proc/$me/stat")"
verdict "$parent" "dsh-standin(parent-of-this-model-shell)"
grandparent="$(awk '{print $4}' "/proc/$parent/stat" 2>/dev/null || echo 0)"
if [ "${grandparent:-0}" -ge 1 ] 2>/dev/null; then
  verdict "$grandparent" "timeout(grandparent)"
fi
PAIR_CHILD
cat >"$PAIR_DIR/pair-standin.sh" <<'PAIR_STANDIN'
#!/usr/bin/env bash
# Роль dsh (bash-образ): финальный exec тот же глубины, что у продакшн-прогона
# (launcher → timeout → fork → exec). Спавнит model-shell-роль и печатает факт
# наличия ключа в СВОЕЙ env (без значения).
set -u
bash "$(dirname "$0")/pair-child.sh"
echo "PAIR-STANDIN image=bash own-env DEEPSEEK_API_KEY present=$([ -n "${DEEPSEEK_API_KEY:-}" ] && echo yes || echo no)"
PAIR_STANDIN
cat >"$PAIR_DIR/pair-standin.js" <<'PAIR_STANDIN_JS'
// Роль dsh (node-образ, как живой dsh): та же exec-глубина, что у продакшна.
const { spawnSync } = require('child_process');
const fs = require('fs');
const stat = fs.readFileSync(`/proc/${process.pid}/stat`, 'utf8');
const parent = Number(stat.slice(stat.lastIndexOf(')') + 2).trim().split(' ')[1]);
const r = spawnSync('bash', ['-c',
  'if ! cat /proc/$PPID/environ 2>/dev/null | grep -qa ""; then ' +
  'echo "PAIR-VERDICT target=node-dsh-standin(parent) pid=$PPID DENIED"; ' +
  'elif cat /proc/$PPID/environ 2>/dev/null | tr "\\0" "\\n" | grep -q "^DEEPSEEK_API_KEY=..*"; then ' +
  'echo "PAIR-VERDICT target=node-dsh-standin(parent) pid=$PPID READABLE DEEPSEEK_API_KEY=present-nonempty"; ' +
  'else echo "PAIR-VERDICT target=node-dsh-standin(parent) pid=$PPID READABLE DEEPSEEK_API_KEY=absent-or-empty"; fi'],
  { stdio: 'inherit' });
if (r.error) { console.error(`pair-probe: child failed: ${r.error}`); process.exit(1); }
console.log(`PAIR-STANDIN image=node own-env DEEPSEEK_API_KEY present=${process.env.DEEPSEEK_API_KEY ? 'yes' : 'no'}`);
PAIR_STANDIN_JS

echo "--- Живой замер пары #140 (bash-образ) ---"
pair_out="$(dsh_agent_run timeout 30 bash "$PAIR_DIR/pair-standin.sh" 2>&1 || true)"
echo "$pair_out"
grep -q "PAIR-VERDICT target=dsh-standin" <<<"$pair_out" \
  || fail "живой замер пары не состоялся (bash-образ): $pair_out"
if command -v node >/dev/null 2>&1; then
  echo "--- Живой замер пары #140 (node-образ, как живой dsh) ---"
  node_pair_out="$(dsh_agent_run timeout 30 node "$PAIR_DIR/pair-standin.js" 2>&1 || true)"
  echo "$node_pair_out"
  grep -q "PAIR-VERDICT target=node-dsh-standin" <<<"$node_pair_out" \
    || fail "живой замер пары не состоялся (node-образ): $node_pair_out"
else
  echo "::note::node не найден в окружении гвардии — node-образ пары не замерялся (bash-образ замерен)"
fi
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Замер #140: настоящая пара model-shell → environ dsh (новая конфигурация)"
    echo ""
    echo '```'
    echo "$pair_out"
    [ -n "${node_pair_out:-}" ] && echo "$node_pair_out"
    echo '```'
  } >>"$GITHUB_STEP_SUMMARY"
fi

# 9. Механика прокси-держателя (#140): upstream-заглушка видит РЕАЛЬНЫЙ ключ
# (файл состояния), а не подмену агента; тело и путь проходят насквозь.
# Заглушка upstream — node однострочник на 127.0.0.1 (порт фиксированный,
# коллизия на одноразовом раннере маловероятна; занят — тест честно красный).
STUB_UP_FILE="$TMP/stub-upstream-seen.txt"
export STUB_UP_FILE
STUB_UP_PORT=47821
node -e "const http=require('http');http.createServer((q,r)=>{let b='';q.on('data',c=>b+=c);q.on('end',()=>{require('fs').writeFileSync(process.env.STUB_UP_FILE, 'AUTH='+q.headers.authorization+' BODY='+b);r.writeHead(200,{'content-type':'text/plain'});r.end('up-ok')})}).listen($STUB_UP_PORT,'127.0.0.1',()=>console.log('stub-up-ready'))" >/dev/null 2>&1 &
STUB_UP_PID=$!
up_ready=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS --max-time 1 "http://127.0.0.1:$STUB_UP_PORT/poll" >/dev/null 2>&1; then up_ready=1; break; fi
  sleep 0.2
done
[ -n "$up_ready" ] || fail "upstream-заглушка прокси-проверки (#140) не поднялась"
# В файл состояния кладём РЕАЛЬНЫЙ ключ фикстуры (DSH_PROXY_REAL_KEY): после
# proxy_start в env оболочки уже подмена — а upstream обязан увидеть реальный.
printf '%s\n%s\n' "http://127.0.0.1:$STUB_UP_PORT/v1" "$DSH_PROXY_REAL_KEY" >"$DSH_PROXY_STATE_FILE"
up_body="$(curl -fsS --max-time 10 -H "Authorization: Bearer ${DSH_PROXY_DUMMY_KEY}" \
  -H 'content-type: application/json' -d '{"model":"guard","messages":[]}' \
  "http://127.0.0.1:${DSH_PROXY_PORT}/chat/completions")"
[ "$up_body" = "up-ok" ] || fail "прокси не вернул ответ upstream (механика #140): $up_body"
seen="$(cat "$STUB_UP_FILE")"
echo "$seen" | grep -qF "AUTH=Bearer guard-fake-key" \
  || fail "upstream увидел не реальный ключ — подмена Authorization сломана (#140): $seen"
echo "$seen" | grep -qF "BODY=" || fail "тело запроса не дошло до upstream (#140)"
echo "ГВАРДИЯ #140: прокси-держатель ключа доказан (upstream видит реальный ключ, агенту едет подмена)"
echo "ГВАРДИЯ #140: изоляция адаптера доказана (uid-домен, docker-denied, env_keep, nogh-граница)"
