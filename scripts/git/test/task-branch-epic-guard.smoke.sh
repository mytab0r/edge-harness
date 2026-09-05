#!/usr/bin/env bash
# Гвардия «PR не заводится на эпик» (#376): проверка ПРОВОДКИ task-branch, не
# логики epic_guard.py (та кормится юнит-тестами
# scripts/lib/test_epic_guard.py на моке scheduler.gh/subprocess — сеть и
# gh не нужны, включая мутацию на is_epic_issue).
#
# Здесь исполняется НАСТОЯЩИЙ `scripts/git/task-branch` целиком в
# изолированном bare-репозитории; застаблен только `python3` в PATH (bash
# resolvит extensionless-скрипт через собственный exec, в отличие от
# Windows CreateProcess у subprocess.run — там нужен реальный .exe, поэтому
# логика epic_guard.py тестируется юнитами, а не живым gh здесь). Стаб
# отвечает на ТОТ ЖЕ контракт CLI, что и настоящий epic_guard.py: argv —
# путь к epic_guard.py и номер задачи; SMOKE_EPIC_MODE=epic переключает
# ответ между «эпик» (exit 1) и «обычная задача» (exit 0).
#
# Два случая:
#   А. номер задачи — эпик → task-branch падает ДО git switch -c: ветка
#      agent/77-... не создаётся, рабочее дерево остаётся на исходной ветке.
#   Б. номер задачи — обычная задача → task-branch заводит ветку как обычно.
#
# Запуск: bash scripts/git/test/task-branch-epic-guard.smoke.sh
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# ── Bare "origin" с историей из одного коммита на main ───────────────────────
ORIGIN="$TMP/origin.git"
git init --bare -q "$ORIGIN"
SEED="$TMP/seed"
git init -q "$SEED"
git -C "$SEED" config user.email test@example.com
git -C "$SEED" config user.name test
printf 'seed\n' > "$SEED/README.md"
git -C "$SEED" add README.md
git -C "$SEED" commit -qm seed
git -C "$SEED" branch -M main
git -C "$SEED" remote add origin "$ORIGIN"
git -C "$SEED" push -q origin main
# init --bare может завести HEAD на "master" (локальный default), которого
# после push нет — clone ниже упал бы на checkout несуществующего ref.
git -C "$ORIGIN" symbolic-ref HEAD refs/heads/main

# ── Рабочий клон, откуда запускается task-branch ─────────────────────────────
WORK="$TMP/work"
git clone -q "$ORIGIN" "$WORK"
git -C "$WORK" config user.email test@example.com
git -C "$WORK" config user.name test

# ── Заглушка python3: тот же CLI-контракт, что epic_guard.py (путь к скрипту
#    + номер задачи), без сети/gh. Реальный epic_guard.py — под своими
#    юнит-тестами (scripts/lib/test_epic_guard.py).
BIN="$TMP/bin"
mkdir -p "$BIN"
cat > "$BIN/python3" <<'PY_STUB'
#!/usr/bin/env bash
set -euo pipefail
script="${1:-}"
num="${2:-}"
case "$script" in
  */epic_guard.py)
    if [ "${SMOKE_EPIC_MODE:-}" = "epic" ]; then
      echo "ОШИБКА: #$num — эпик, ветку заводить нельзя. Что делать: заведи узкую задачу на конкретную стройку эпика #$num." >&2
      exit 1
    fi
    exit 0
    ;;
esac
echo "python3-stub: неожиданный вызов: $*" >&2
exit 2
PY_STUB
chmod +x "$BIN/python3"

export PATH="$BIN:$PATH"

fail=0

# ── А: номер — эпик → отказ, ветка не создаётся ──────────────────────────────
before_branch=$(git -C "$WORK" rev-parse --abbrev-ref HEAD)
set +e
( cd "$WORK" && SMOKE_EPIC_MODE=epic bash "$REPO/scripts/git/task-branch" 77-epic-slug \
    > "$TMP/epic.out" 2> "$TMP/epic.err" )
epic_rc=$?
set -e
after_branch=$(git -C "$WORK" rev-parse --abbrev-ref HEAD)

if [ "$epic_rc" -eq 0 ]; then
  echo "::error::task-branch на номере эпика (77) вернул 0 — гвардия не сработала"
  cat "$TMP/epic.out" "$TMP/epic.err" >&2
  fail=1
fi
if [ "$after_branch" != "$before_branch" ]; then
  echo "::error::task-branch создал ветку на номере эпика (77): HEAD «$before_branch» → «$after_branch»"
  fail=1
fi
if ! grep -q "эпик" "$TMP/epic.err"; then
  echo "::error::отказ на эпике не назвал газ («заведи узкую задачу…») — сообщение:"
  cat "$TMP/epic.err" >&2
  fail=1
fi

# ── Б: обычная задача → ветка создаётся как обычно ───────────────────────────
set +e
( cd "$WORK" && SMOKE_EPIC_MODE=task bash "$REPO/scripts/git/task-branch" 999-ordinary-slug \
    > "$TMP/ordinary.out" 2> "$TMP/ordinary.err" )
ordinary_rc=$?
set -e
ordinary_branch=$(git -C "$WORK" rev-parse --abbrev-ref HEAD)

if [ "$ordinary_rc" -ne 0 ]; then
  echo "::error::task-branch на обычной задаче (999) упал (rc=$ordinary_rc) — гвардия ложно сработала"
  cat "$TMP/ordinary.out" "$TMP/ordinary.err" >&2
  fail=1
fi
if [ "$ordinary_branch" != "agent/999-ordinary-slug" ]; then
  echo "::error::task-branch на обычной задаче не завёл ожидаемую ветку (сейчас: $ordinary_branch)"
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "task-branch: номер эпика отклонён с газом, обычная задача заведена штатно"
fi
exit "$fail"
