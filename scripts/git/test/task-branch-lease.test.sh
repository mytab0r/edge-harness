#!/usr/bin/env bash
# Гвардия #356: scripts/git/task-branch арендует задачу (claim_task.py claim N)
# ДО создания ветки — забыть аренду для внешнего/ручного канала стало
# невозможно, потому что ветка создаётся только этим скриптом. Мутация:
# убери вызов claim из task-branch — сценарий 2 (занятая задача) покраснеет,
# потому что ветка создастся вопреки занятой аренде.
#
# Никакой реальной сети: локальный bare-репозиторий как origin; `gh repo view`
# застаблен bash-заглушкой (task-branch вызывает её сама, PATH-подмена
# работает — bash исполняет шебанг-скрипт без расширения напрямую); сам
# claim_task.py застаблен на уровне python3 — NB: claim_task.py вызывает
# `gh api` через Python subprocess.run, а тот на Windows ищет исполняемый
# файл через нативный CreateProcess (только автодобавление .exe, PATHEXT
# игнорируется) — bash-скрипт без расширения там не находится в принципе.
# HTTP-протокол claim'а (422 already exists, TTL и т.п.) уже покрыт
# scripts/lib/test_claim_task.py на уровне unit-тестов; здесь проверяется
# только контракт task-branch с CLI claim_task.py (rc 0/1/2 → аренда взята /
# отказ останавливает ветку / предупреждение не блокирует).
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

# ── заглушка gh: только `gh repo view` (вызов из bash самого task-branch) ────
FAKEBIN="$TMP/bin"
mkdir -p "$FAKEBIN"
cat >"$FAKEBIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
case "$1" in
  repo) echo "o/r" ;;
  *) echo "заглушка gh: неподдержанная команда «$1» (ожидался repo view)" >&2; exit 1 ;;
esac
FAKEGH
chmod +x "$FAKEBIN/gh"

# ── заглушка python3: контракт CLI claim_task.py (не сеть) ───────────────────
# `gh api`, который вызывает claim_task.py, идёт через Python subprocess.run —
# на Windows её саму заменить bash-скриптом без .exe нельзя (см. заголовок),
# поэтому подменяется на уровень выше: python3 → claim_task.py claim N.
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
if [ "\${FAKE_GH_CLAIM_BUSY:-0}" = "1" ]; then
  echo "задача #\$num уже занята (замок refs/locks/task-\$num)"
  exit 1
fi
echo "замок refs/locks/task-\$num установлен"
exit 0
FAKEPY
chmod +x "$FAKEBIN/python3"

# ── Сценарий 1: задача свободна → ветка создаётся, "Аренда взята" в выводе ───
WORK1="$TMP/work1"
git clone --quiet "$ORIGIN" "$WORK1"
out1="$(cd "$WORK1" && PATH="$FAKEBIN:$PATH" env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA -u LEASE_ALREADY_CLAIMED \
        "$REPO/scripts/git/task-branch" 1-free-task 2>&1)" \
  || fail "сценарий 1 (свободная задача) должен завершиться успехом:\n$out1"
printf '%s\n' "$out1" | grep -q "Аренда взята" \
  || fail "сценарий 1: нет строки «Аренда взята» в выводе:\n$out1"
branch1="$(git -C "$WORK1" branch --show-current)"
[ "$branch1" = "agent/1-free-task" ] \
  || fail "сценарий 1: ожидал ветку agent/1-free-task, получил «$branch1»"
echo "OK: сценарий 1 (свободная задача) — ветка создана, аренда взята"

# ── Сценарий 2: задача занята (FAKE_GH_CLAIM_BUSY=1) → отказ, ветка НЕ создана ──
# Это и есть мутационная гвардия: без вызова claim в task-branch этот сценарий
# не отличил бы «занято» от «свободно» и создал бы ветку — тест покраснеет.
WORK2="$TMP/work2"
git clone --quiet "$ORIGIN" "$WORK2"
set +e
out2="$(cd "$WORK2" && PATH="$FAKEBIN:$PATH" FAKE_GH_CLAIM_BUSY=1 \
        env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA -u LEASE_ALREADY_CLAIMED \
        "$REPO/scripts/git/task-branch" 2-busy-task 2>&1)"
rc2=$?
set -e
[ "$rc2" -ne 0 ] || fail "сценарий 2 (занятая задача) должен завершиться отказом (rc!=0), получил rc=0:\n$out2"
printf '%s\n' "$out2" | grep -qi "занят" \
  || fail "сценарий 2: в отказе нет упоминания «занята»:\n$out2"
printf '%s\n' "$out2" | grep -qi "assignee\|🔒" \
  || fail "сценарий 2: отказ не называет, где искать держателя (assignee/🔒):\n$out2"
printf '%s\n' "$out2" | grep -qi "другую" \
  || fail "сценарий 2: отказ не говорит, что делать (взять другую задачу):\n$out2"
branch2="$(git -C "$WORK2" branch --show-current)"
[ "$branch2" = "main" ] \
  || fail "сценарий 2: ветка НЕ должна была создаться, но текущая ветка «$branch2»"
[ -z "$(git -C "$WORK2" branch --list 'agent/2-busy-task')" ] \
  || fail "сценарий 2: ветка agent/2-busy-task не должна существовать"
echo "OK: сценарий 2 (занятая задача) — ветка не создана, отказ внятный"

# ── Сценарий 3: транспорт уже арендовал (LEASE_ALREADY_CLAIMED=1) → task-branch
# не арендует повторно, даже если бы повторный claim был бы отклонён ────────────
WORK3="$TMP/work3"
git clone --quiet "$ORIGIN" "$WORK3"
out3="$(cd "$WORK3" && PATH="$FAKEBIN:$PATH" FAKE_GH_CLAIM_BUSY=1 LEASE_ALREADY_CLAIMED=1 \
        env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA \
        "$REPO/scripts/git/task-branch" 3-transport-claimed 2>&1)" \
  || fail "сценарий 3 (LEASE_ALREADY_CLAIMED=1) должен создать ветку без повторного claim:\n$out3"
printf '%s\n' "$out3" | grep -qi "не повторяет" \
  || fail "сценарий 3: нет отметки, что повторный claim пропущен:\n$out3"
branch3="$(git -C "$WORK3" branch --show-current)"
[ "$branch3" = "agent/3-transport-claimed" ] \
  || fail "сценарий 3: ожидал ветку agent/3-transport-claimed, получил «$branch3»"
echo "OK: сценарий 3 (транспорт уже арендовал) — task-branch не арендует повторно"

# ── Сценарий 4: gh недоступен (офлайн) → предупреждение, ветка всё равно создаётся ──
WORK4="$TMP/work4"
git clone --quiet "$ORIGIN" "$WORK4"
# Офлайн-PATH: весь текущий PATH, но без каталога настоящего gh и без заглушки —
# task-branch должен пройти без gh вообще в PATH (claim_task.py тут не нужен).
gh_dir="$(dirname "$(command -v gh 2>/dev/null || echo /nonexistent)")"
OFFLINE_PATH="$(printf '%s\n' "$PATH" | tr ':' '\n' | grep -vF "$gh_dir" | grep -vF "$FAKEBIN" | paste -sd: -)"
out4="$(cd "$WORK4" && PATH="$OFFLINE_PATH" \
        env -u GITHUB_REPOSITORY -u CLAIM_ACTOR -u CLAIM_VIA -u LEASE_ALREADY_CLAIMED \
        "$REPO/scripts/git/task-branch" 4-offline-task 2>&1)" \
  || fail "сценарий 4 (офлайн, gh недоступен) должен создать ветку, не блокировать:\n$out4"
printf '%s\n' "$out4" | grep -qi "gh недоступен" \
  || fail "сценарий 4: нет предупреждения про недоступный gh:\n$out4"
branch4="$(git -C "$WORK4" branch --show-current)"
[ "$branch4" = "agent/4-offline-task" ] \
  || fail "сценарий 4: ожидал ветку agent/4-offline-task, получил «$branch4»"
echo "OK: сценарий 4 (офлайн) — предупреждение, ветка всё равно создана"

echo "task-branch-lease.test.sh: все сценарии зелёные"
