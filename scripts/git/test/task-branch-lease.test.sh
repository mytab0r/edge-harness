#!/usr/bin/env bash
# Гвардия #356: scripts/git/task-branch арендует задачу (claim_task.py claim N)
# ДО создания ветки — забыть аренду для внешнего/ручного канала стало
# невозможно, потому что ветка создаётся только этим скриптом.
#
# Мутации, которыми доказан тест (каждая проверена прогоном: снял — красный,
# вернул — зелёный):
#   1) убери вызов claim из task-branch — сценарий 2 (занятая задача) краснеет,
#      потому что ветка создастся вопреки занятой аренде;
#   2) сними проверку «ветка уже существует» — сценарий 3d краснеет: claim
#      пройдёт под существующую ветку и замок осиротеет до TTL;
#   3) замени «предупреждение при rc=2» на отказ — сценарий 4b краснеет;
#   4) перенеси блок аренды ВЫШЕ гвардии эпика — сценарий 5 краснеет: замок
#      будет поставлен на эпик, отказ которого оставит его висеть до TTL.
#
# Никакой реальной сети: локальный bare-репозиторий как origin. Заглушки:
#  - `gh repo view` + `gh api repos/o/r/issues/N` — task-branch после ребейза
#    на main (#376) зовёт на каждом числовом входе эпик-гвардию
#    (scripts/lib/epic_guard.py → `gh repo view`, затем `gh api .../issues/N`
#    без --jq, ждёт настоящий JSON); тот же бинарь отвечает и на `gh api ...
#    --jq` проверки «открыта/метки» (#357/#358) — ветвление по наличию --jq,
#    как в task-branch.test.sh. Числовой вход при мёртвом/отсутствующем gh до
#    аренды не доходит (громкий отказ эпик-гвардии, сценарий 4) — «офлайн»
#    здесь имитируется отказом claim_task (rc=2, сценарий 4b), как и в проде.
#  - сам claim_task.py застаблен на уровне python3 — NB: claim_task.py вызывает
#    `gh api` через Python subprocess.run, а тот на Windows ищет исполняемый
#    файл через нативный CreateProcess (только автодобавление .exe, PATHEXT
#    игнорируется) — bash-скрипт без расширения там не находится в принципе.
#    HTTP-протокол claim'а (422 already exists, TTL и т.п.) уже покрыт
#    scripts/lib/test_claim_task.py на уровне unit-тестов; здесь проверяется
#    только контракт task-branch с CLI claim_task.py (rc 0/1/2 → аренда взята /
#    отказ останавливает ветку / предупреждение не блокирует).
#
# Запуск: bash scripts/git/test/task-branch-lease.test.sh
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$TEST_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

# ── origin: локальный bare-репозиторий с одним коммитом на main ──────────────
ORIGIN="$TMP/origin.git"
git init --quiet --bare "$ORIGIN"
SEED="$TMP/seed"
git clone --quiet "$ORIGIN" "$SEED"
git -C "$SEED" -c user.email=t@test -c user.name=t commit --quiet --allow-empty -m seed
git -C "$SEED" branch -M main
git -C "$SEED" push --quiet -u origin main
git --git-dir="$ORIGIN" symbolic-ref HEAD refs/heads/main

# ── заглушка gh: `gh repo view` + `gh api repos/o/r/issues/N` ────────────────
# Все номера, кроме 79, — обычные открытые задачи с меткой task (для --jq —
# TSV «open\ttask», для эпик-гвардии — JSON без признаков эпика: заголовок без
# префикса «ЭПИК», sub_issues_summary нулевой). №79 — эпик по ОБОИМ признакам
# is_epic_issue (scheduler.py): префикс «ЭПИК» и total > completed.
FAKEBIN="$TMP/bin"
mkdir -p "$FAKEBIN"
cat >"$FAKEBIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
if [ "$1" = "repo" ] && [ "$2" = "view" ]; then
  echo "o/r"
  exit 0
fi
if [ "$1" = "api" ]; then
  shift
  has_jq=0
  url=""
  for arg in "$@"; do
    case "$arg" in
      --jq) has_jq=1 ;;
      -*) : ;;
      *) [ -n "$url" ] || url="$arg" ;;
    esac
  done
  # graphql (открытые sub-issues эпика, best-effort) — пустой список узлов.
  case "$url" in
    graphql*)
      echo '{"data":{"repository":{"issue":{"subIssues":{"nodes":[]}}}}}'
      exit 0 ;;
  esac
  # Маршрут по наличию --jq ПРЕЖДЕ номера: проверка «открыта/метки» (#357/#358)
  # зовёт `gh api` с --jq и ждёт TSV, эпик-гвардия — без --jq и ждёт настоящий
  # JSON (той же схемой разветвляет task-branch.test.sh).
  if [ "$has_jq" = 1 ]; then
    printf 'open\ttask\n'
    exit 0
  fi
  if [ "${url##*/}" = "79" ]; then
    echo '{"state":"open","title":"ЭПИК: platform","labels":[{"name":"task"}],"sub_issues_summary":{"total":2,"completed":0}}'
    exit 0
  fi
  echo '{"state":"open","title":"т","labels":[{"name":"task"}],"sub_issues_summary":{"total":0,"completed":0}}'
  exit 0
fi
echo "заглушка gh: неподдержанная команда «$1» (ожидался repo view или api issues/N)" >&2
exit 1
FAKEGH
chmod +x "$FAKEBIN/gh"

# ── заглушка python3: контракт CLI claim_task.py (не сеть) ───────────────────
# `gh api`, который вызывает claim_task.py, идёт через Python subprocess.run —
# на Windows её саму заменить bash-скриптом без .exe нельзя (см. заголовок),
# поэтому подменяется на уровень выше: python3 → claim_task.py claim N.
# Каждый УСПЕШНО дошедший до заглушки claim пишется в $FAKE_CLAIM_LOG —
# сценарии «аренды быть не должно» (эпик, существующая ветка, офлайн) проверяют
# пустой лог, а не только отсутствие ветки: ветки могло не быть и при
# переставленных местами гейтах, а замок — уже быть.
REAL_PYTHON3="$(command -v python3)"
cat >"$FAKEBIN/python3" <<FAKEPY
#!/usr/bin/env bash
set -euo pipefail
script="\${1:-}"; shift || true
if [ "\$(basename "\$script")" != "claim_task.py" ]; then
  exec "$REAL_PYTHON3" "\$script" "\$@"
fi
cmd="\${1:-}"; num="\${2:-}"
if [ "\$cmd" != "claim" ]; then
  echo "заглушка python3/claim_task.py: команда «\$cmd» не поддержана тестом" >&2
  exit 2
fi
if [ -n "\${FAKE_CLAIM_LOG:-}" ]; then
  printf '%s\n' "\$num" >> "\$FAKE_CLAIM_LOG"
fi
if [ "\${FAKE_GH_CLAIM_BUSY:-0}" = "1" ]; then
  echo "задача #\$num уже занята (замок refs/locks/task-\$num)"
  exit 1
fi
if [ "\${FAKE_GH_CLAIM_BROKEN:-0}" = "1" ]; then
  echo "gh: HTTP 502: upstream connect error (заглушка поломки)"
  exit 2
fi
echo "замок refs/locks/task-\$num установлен"
exit 0
FAKEPY
chmod +x "$FAKEBIN/python3"

# Общий прогон: свежий клон, чистое окружение канала, лог claim'ов.
# LEASE_ALREADY_CLAIMED здесь НЕ вычёркивается: сценарии 3/3b/3c задают его
# префиксом, остальные — нормализуют пустым значением (для скрипта пустая
# строка и отсутствующая переменная неразличимы: ${LEASE_ALREADY_CLAIMED:-}).
run_lease_case() {
  local dir="$1" task="$2" log="$3"; shift 3
  local work="$TMP/$dir"
  git clone --quiet "$ORIGIN" "$work"
  : >"$log"
  (
    cd "$work"
    PATH="$FAKEBIN:$PATH" FAKE_CLAIM_LOG="$log" \
      env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA \
      "$REPO/scripts/git/task-branch" "$task" 2>&1
  )
}

# ── Сценарий 1: задача свободна → ветка создаётся, "Аренда взята" в выводе ───
LOG1="$TMP/claim1.log"
out1="$(LEASE_ALREADY_CLAIMED= run_lease_case work1 1-free-task "$LOG1")" \
  || fail "сценарий 1 (свободная задача) должен завершиться успехом:\n$out1"
printf '%s\n' "$out1" | grep -q "Аренда взята" \
  || fail "сценарий 1: нет строки «Аренда взята» в выводе:\n$out1"
grep -qx "1" "$LOG1" \
  || fail "сценарий 1: claim #1 не дошёл до claim_task (лог: $(cat "$LOG1"))"
branch1="$(git -C "$TMP/work1" branch --show-current)"
[ "$branch1" = "agent/1-free-task" ] \
  || fail "сценарий 1: ожидал ветку agent/1-free-task, получил «$branch1»"
echo "OK: сценарий 1 (свободная задача) — ветка создана, аренда взята"

# ── Сценарий 2: задача занята (FAKE_GH_CLAIM_BUSY=1) → отказ, ветка НЕ создана ──
# Это и есть мутационная гвардия: без вызова claim в task-branch этот сценарий
# не отличил бы «занято» от «свободно» и создал бы ветку — тест покраснеет.
LOG2="$TMP/claim2.log"
set +e
out2="$(LEASE_ALREADY_CLAIMED= FAKE_GH_CLAIM_BUSY=1 run_lease_case work2 2-busy-task "$LOG2")"
rc2=$?
set -e
[ "$rc2" -ne 0 ] || fail "сценарий 2 (занятая задача) должен завершиться отказом (rc!=0), получил rc=0:\n$out2"
printf '%s\n' "$out2" | grep -qi "занят" \
  || fail "сценарий 2: в отказе нет упоминания «занята»:\n$out2"
printf '%s\n' "$out2" | grep -qi "assignee\|🔒" \
  || fail "сценарий 2: отказ не называет, где искать держателя (assignee/🔒):\n$out2"
printf '%s\n' "$out2" | grep -qi "другую" \
  || fail "сценарий 2: отказ не говорит, что делать (взять другую задачу):\n$out2"
# Замок не хранит держателя (#121) — task-branch не отличит свой прошлый замок
# от чужого, поэтому отказ обязан называть выход для СВОЕГО протухшего замка
# (release N), иначе агент уходит, оставляя замок до TTL (находка ревью #360).
printf '%s\n' "$out2" | grep -qi "release" \
  || fail "сценарий 2: отказ не называет release как выход для своего же замка с прошлого прогона:\n$out2"
branch2="$(git -C "$TMP/work2" branch --show-current)"
[ "$branch2" = "main" ] \
  || fail "сценарий 2: ветка НЕ должна была создаться, но текущая ветка «$branch2»"
[ -z "$(git -C "$TMP/work2" branch --list 'agent/2-busy-task')" ] \
  || fail "сценарий 2: ветка agent/2-busy-task не должна существовать"
echo "OK: сценарий 2 (занятая задача) — ветка не создана, отказ внятный, с выходом для своего замка"

# ── Сценарий 3: транспорт уже арендовал ЭТУ ЖЕ задачу (LEASE_ALREADY_CLAIMED=3,
# совпадает с номером ветки) → task-branch не арендует повторно, даже если бы
# повторный claim был бы отклонён ─────────────────────────────────────────────
LOG3="$TMP/claim3.log"
set +e
out3="$(LEASE_ALREADY_CLAIMED=3 run_lease_case work3 3-transport-claimed "$LOG3")"
rc3=$?
set -e
[ "$rc3" -eq 0 ] || fail "сценарий 3 (LEASE_ALREADY_CLAIMED=3) должен создать ветку без повторного claim:\n$out3"
printf '%s\n' "$out3" | grep -qi "не повторяет" \
  || fail "сценарий 3: нет отметки, что повторный claim пропущен:\n$out3"
[ ! -s "$LOG3" ] \
  || fail "сценарий 3: повторный claim не должен был вызываться (лог: $(cat "$LOG3"))"
branch3="$(git -C "$TMP/work3" branch --show-current)"
[ "$branch3" = "agent/3-transport-claimed" ] \
  || fail "сценарий 3: ожидал ветку agent/3-transport-claimed, получил «$branch3»"
echo "OK: сценарий 3 (транспорт уже арендовал ЭТУ задачу) — task-branch не арендует повторно"

# ── Сценарий 3b (гвардия #356-фоллоуап): транспорт арендовал ДРУГУЮ задачу
# (LEASE_ALREADY_CLAIMED=50), в этом же прогоне task-branch зовут для #77 →
# номер не совпал, аренда #77 ДОЛЖНА быть взята заново, а не пропущена.
# Мутация: верни булеву проверку ("${LEASE_ALREADY_CLAIMED:-0}" = 1) — этот
# сценарий покраснеет, потому что «Аренда взята» пропадёт из вывода: claim
# #77 молча не возьмётся, хотя #50 никак не арендует #77.
LOG3B="$TMP/claim3b.log"
set +e
out3b="$(LEASE_ALREADY_CLAIMED=50 run_lease_case work3b 77-other-task "$LOG3B")"
rc3b=$?
set -e
[ "$rc3b" -eq 0 ] || fail "сценарий 3b (номер флага не совпал) должен создать ветку и взять аренду #77:\n$out3b"
printf '%s\n' "$out3b" | grep -q "Аренда взята" \
  || fail "сценарий 3b: LEASE_ALREADY_CLAIMED=50 не должен подавлять аренду #77 (номер не совпал), но «Аренда взята» нет в выводе:\n$out3b"
grep -qx "77" "$LOG3B" \
  || fail "сценарий 3b: claim должен был вызваться для #77 (лог: $(cat "$LOG3B"))"
printf '%s\n' "$out3b" | grep -qi "не к этой ветке" \
  || fail "сценарий 3b: нет предупреждения, что флаг относится к другой задаче:\n$out3b"
branch3b="$(git -C "$TMP/work3b" branch --show-current)"
[ "$branch3b" = "agent/77-other-task" ] \
  || fail "сценарий 3b: ожидал ветку agent/77-other-task, получил «$branch3b»"
echo "OK: сценарий 3b (флаг несёт номер ДРУГОЙ задачи) — аренда #77 взята заново, не пропущена"

# ── Сценарий 3c: LEASE_ALREADY_CLAIMED — мусор (не число, устаревший булев
# флаг «1» без нового контракта или битые данные) → громкий отказ, ветка НЕ
# создаётся: тихо угадывать смысл мусорного флага опаснее, чем остановиться.
LOG3C="$TMP/claim3c.log"
set +e
out3c="$(LEASE_ALREADY_CLAIMED=true run_lease_case work3c 78-garbage-flag "$LOG3C")"
rc3c=$?
set -e
[ "$rc3c" -ne 0 ] || fail "сценарий 3c (мусор в LEASE_ALREADY_CLAIMED) должен завершиться отказом:\n$out3c"
printf '%s\n' "$out3c" | grep -qi "не номер задачи" \
  || fail "сценарий 3c: в отказе нет объяснения, что флаг должен быть номером:\n$out3c"
[ ! -s "$LOG3C" ] || fail "сценарий 3c: при мусорном флаге claim вызван не должен (лог: $(cat "$LOG3C"))"
[ -z "$(git -C "$TMP/work3c" branch --list 'agent/78-garbage-flag')" ] \
  || fail "сценарий 3c: ветка НЕ должна была создаться при мусорном флаге"
echo "OK: сценарий 3c (мусор в LEASE_ALREADY_CLAIMED) — громкий отказ, ветка не создана"

# ── Сценарий 3d: ветка agent/N-slug уже существует на origin → отказ ДО claim'а
# (находка ревью #360): иначе claim проходит, `git switch -c` падает на
# существующем имени — замок осиротевает до TTL-сборщика (24 ч). Первый прогон
# умер после claim'а → повторный должен получить внятный отказ, а не «занята»
# от собственного замка. Мутация: сними проверку существующей ветки из
# task-branch — этот сценарий краснеет (лог claim'а перестанет быть пустым).
WORK3D="$TMP/work3d"
git clone --quiet "$ORIGIN" "$WORK3D"
git -C "$WORK3D" push --quiet origin origin/main:refs/heads/agent/9-taken-branch
LOG3D="$TMP/claim3d.log"
set +e
out3d="$(LEASE_ALREADY_CLAIMED= run_lease_case work3d-run 9-taken-branch "$LOG3D")"
rc3d=$?
set -e
[ "$rc3d" -ne 0 ] || fail "сценарий 3d (ветка уже существует) должен завершиться отказом:\n$out3d"
printf '%s\n' "$out3d" | grep -qi "уже существует" \
  || fail "сценарий 3d: отказ не называет причину (ветка уже существует):\n$out3d"
printf '%s\n' "$out3d" | grep -qi "checkout" \
  || fail "сценарий 3d: отказ не называет путь доводки существующей ветки (checkout):\n$out3d"
[ ! -s "$LOG3D" ] \
  || fail "сценарий 3d: claim под существующую ветку взят не должен (лог: $(cat "$LOG3D")) — осиротевший замок до TTL"
[ -z "$(git -C "$TMP/work3d-run" branch --list 'agent/9-taken-branch')" ] \
  || fail "сценарий 3d: вторую ветку с тем же именем заводить нельзя"
echo "OK: сценарий 3d (ветка уже существует) — отказ до аренды, замок не тронут"

# ── Сценарий 4: gh недоступен (офлайн) → громкий отказ входа, ветки нет ──────
# После ребейза на main (#376, эпик-гвардия) числовой вход в задачу при
# недоступном gh до аренды не доходит: эпик-гвардия объявляет офлайн громким
# отказом входа целиком (тот же task-branch, тот же PATH). Сценарий охраняет
# ПОРЯДОК: блок аренды не должен стоять выше гвардии и брать замок там, где
# вход всё равно откажет. Вычитание каталога gh из PATH одной строкой
# (grep -vF "$gh_dir") ненадёжно на GitHub-раннере: gh лежит в /usr/bin вместе
# с git/grep/sed — комбинация safe_path + зеркальный shim, как в случае 7
# task-branch.test.sh (зеркалится КАЖДЫЙ каталог с gh целиком, кроме самого
# gh — список инструментов копией неизбежно расходится со скриптом).
WORK4="$TMP/work4"
git clone --quiet "$ORIGIN" "$WORK4"
safe_path=""
IFS=':' read -ra _dirs <<<"$PATH"
for _d in "${_dirs[@]}"; do
  [ -n "$_d" ] || continue
  case "$_d" in
    "$FAKEBIN") continue ;;
  esac
  if [ ! -e "$_d/gh" ] && [ ! -e "$_d/gh.exe" ]; then
    safe_path="$safe_path:$_d"
  fi
done
safe_path="${safe_path#:}"
shim4="$TMP/shim-no-gh"
mkdir -p "$shim4"
for _d in "${_dirs[@]}"; do
  [ -n "$_d" ] || continue
  if [ -e "$_d/gh" ] || [ -e "$_d/gh.exe" ]; then
    for entry in "$_d"/*; do
      [ -e "$entry" ] || [ -L "$entry" ] || continue
      name="$(basename "$entry")"
      case "$name" in
        gh|gh.exe) continue ;;
      esac
      [ -e "$shim4/$name" ] || ln -sf "$entry" "$shim4/$name" 2>/dev/null || true
    done
  fi
done
# Абсолютный путь к bash РЕЗОЛВИТСЯ ДО подмены PATH и передаётся напрямую
# (не через шебанг скрипта): шебанг зовёт `env bash`, а bash в шиме не положен,
# если он живёт в одном каталоге с gh.
bash4="$(command -v bash)"
LOG4="$TMP/claim4.log"
: >"$LOG4"
set +e
out4="$(cd "$WORK4" && PATH="$safe_path:$shim4" FAKE_CLAIM_LOG="$LOG4" \
        env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA -u LEASE_ALREADY_CLAIMED \
        "$bash4" "$REPO/scripts/git/task-branch" 4-offline-task 2>&1)"
rc4=$?
set -e
[ "$rc4" -ne 0 ] \
  || fail "сценарий 4 (gh недоступен): вход обязан отказать громко (эпик-гвардия #376, офлайн-режима у входа нет), получил rc=0:\n$out4"
printf '%s\n' "$out4" | grep -q "epic_guard" \
  || fail "сценарий 4: нет громкого отказа эпик-гвардии:\n$out4"
[ ! -s "$LOG4" ] \
  || fail "сценарий 4: при недоступном gh claim вызван не должен (лог: $(cat "$LOG4"))"
[ -z "$(git -C "$WORK4" branch --list 'agent/4-offline-task')" ] \
  || fail "сценарий 4: ветка при недоступном gh создаваться не должна (вход отказывает)"
echo "OK: сценарий 4 (gh недоступен) — громкий отказ входа до аренды, замок не ставится"

# ── Сценарий 4b: claim_task сломался (rc=2) при живом gh — «поломка, не
# занято» → предупреждение, ветка всё равно создаётся. Это единственное новое
# «не блокирующее» поведение task-branch (находка ревью #360: сценария не
# было). Мутация: замени предупреждение на отказ — сценарий краснеет.
LOG4B="$TMP/claim4b.log"
set +e
out4b="$(LEASE_ALREADY_CLAIMED= FAKE_GH_CLAIM_BROKEN=1 run_lease_case work4b 8-broken-claim "$LOG4B")"
rc4b=$?
set -e
[ "$rc4b" -eq 0 ] \
  || fail "сценарий 4b (claim_task сломался, rc=2) должен создать ветку с предупреждением, не блокировать:\n$out4b"
printf '%s\n' "$out4b" | grep -q "claim_task сломался (rc=2)" \
  || fail "сценарий 4b: нет предупреждения «claim_task сломался (rc=2)»:\n$out4b"
printf '%s\n' "$out4b" | grep -q "Аренда взята" \
  && fail "сценарий 4b: «Аренда взята» не должна печататься при rc=2:\n$out4b" || true
grep -qx "8" "$LOG4B" \
  || fail "сценарий 4b: claim #8 должен был дойти до claim_task (лог: $(cat "$LOG4B"))"
branch4b="$(git -C "$TMP/work4b" branch --show-current)"
[ "$branch4b" = "agent/8-broken-claim" ] \
  || fail "сценарий 4b: ожидал ветку agent/8-broken-claim, получил «$branch4b»"
echo "OK: сценарий 4b (claim_task сломался, rc=2) — предупреждение, ветка создана"

# ── Сценарий 5: задача — эпик (№79) → громкий отказ гвардии эпика, и аренда
# НЕ берётся: блок аренды обязан стоять ПОСЛЕ гвардии, иначе замок будет
# поставлен на эпик, отказ которого оставит его висеть до TTL (24 ч) — ровно
# тот осиротевший замок, от которого #356 защищает. Мутация: перенеси блок
# аренды выше гвардии эпика — этот сценарий краснеет по непустому логу claim'а.
LOG5="$TMP/claim5.log"
set +e
out5="$(LEASE_ALREADY_CLAIMED= run_lease_case work5 79-epic-task "$LOG5")"
rc5=$?
set -e
[ "$rc5" -ne 0 ] || fail "сценарий 5 (эпик) должен завершиться отказом:\n$out5"
printf '%s\n' "$out5" | grep -qi "эпик" \
  || fail "сценарий 5: в отказе нет «эпик»:\n$out5"
[ ! -s "$LOG5" ] \
  || fail "сценарий 5: аренда на эпик ставиться не должна (лог: $(cat "$LOG5")) — блок аренды обязан идти после гвардии эпика"
[ -z "$(git -C "$TMP/work5" branch --list 'agent/79-epic-task')" ] \
  || fail "сценарий 5: ветку на эпик заводить нельзя"
echo "OK: сценарий 5 (эпик) — отказ до аренды, замок на эпик не ставится"

echo "task-branch-lease.test.sh: все сценарии зелёные"
