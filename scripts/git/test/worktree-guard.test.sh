#!/usr/bin/env bash
# Гвардия «дерево == задача» (#332): доказывает поведение РЕАЛЬНОГО
# .githooks/pre-commit на живых git-репозиториях и worktree — не пересказ,
# а прогон настоящего файла гвардии (копируется как есть, не переписывается
# здесь заново). Три случая:
#   1) коммит в agent/<N>-* из дерева «<N>-slug»           — проходит;
#   2) коммит в agent/<N>-* из дерева «<M>-slug» (M != N)  — отклонён;
#   3) коммит в main (не agent/*) из дерева с любым именем — гвардия неприменима, проходит.
#
# Мутация, которой доказана гвардия (#332): временно закомментируй блок
# «Гвардия «дерево == задача»» в .githooks/pre-commit (от `if [[ "$branch"`
# до соответствующего `fi`) — случай (2) перестаёт отклоняться, тест красный.
# Верни блок — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK_SRC="$REPO_ROOT/.githooks/pre-commit"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── подготовка: bare origin + локальный клон с main, гвардия из прод-файла ──
# .githooks/pre-commit коммитится в main ДО создания worktree — иначе каждое
# новое дерево (свой checkout ветки) окажется без файла гвардии вовсе, и тест
# ничего не докажет (реальный репозиторий коммитит .githooks вместе с кодом).
git init -q --bare "$WORK/origin.git"

git init -q "$WORK/seed"
(
  cd "$WORK/seed"
  git config user.email test@example.com
  git config user.name test
  mkdir -p .githooks
  cp "$HOOK_SRC" .githooks/pre-commit
  chmod +x .githooks/pre-commit
  git add .githooks
  git commit -q -m init
  git remote add origin "$WORK/origin.git"
  git push -q origin HEAD:refs/heads/main
)

git clone -q "$WORK/origin.git" "$WORK/main-tree"
(
  cd "$WORK/main-tree"
  git config user.email test@example.com
  git config user.name test
  git config core.hooksPath .githooks
)

make_worktree() {
  local dirname="$1" branch="$2"
  git -C "$WORK/main-tree" worktree add -q -b "$branch" "$WORK/$dirname" origin/main
}

try_commit() {
  # echo "ok" >&2 через git commit — возвращает код возврата коммита
  local dir="$1"
  (
    cd "$WORK/$dir"
    git config user.email test@example.com
    git config user.name test
    echo "change in $dir at $(date +%s%N)" >>note.txt
    git add note.txt
    git commit -q -m "test commit" 2>"$WORK/$dir.stderr"
  )
}

# ── случай 1: дерево «42-good-slug», ветка agent/42-good-slug — должен пройти ──
make_worktree "42-good-slug" "agent/42-good-slug"
if try_commit "42-good-slug"; then
  note "случай 1 (дерево совпадает с задачей): коммит прошёл — ОК"
else
  note "случай 1 (дерево совпадает с задачей): коммит ОТКЛОНЁН — ОШИБКА, ожидался успех"
  cat "$WORK/42-good-slug.stderr" >&2
  fail=1
fi

# ── случай 2: дерево «99-other-task», ветка agent/42-mismatch — должен отклониться ──
make_worktree "99-other-task" "agent/42-mismatch"
if try_commit "99-other-task"; then
  note "случай 2 (дерево НЕ совпадает с задачей): коммит прошёл — ОШИБКА, ожидался отказ"
  fail=1
else
  msg="$(cat "$WORK/99-other-task.stderr")"
  note "случай 2 (дерево НЕ совпадает с задачей): коммит отклонён — ОК"
  note "  сообщение гвардии: $(printf '%s' "$msg" | head -1)"
  case "$msg" in
    *"рабочее дерево этой задачи не здесь"*) ;;
    *)
      note "  ОШИБКА: сообщение гвардии не объясняет причину отказа"
      fail=1
      ;;
  esac
fi

# ── случай 3: не agent/*-ветка (main) — гвардия неприменима вне зависимости от имени дерева ──
make_worktree "unrelated-dir-name" "local/no-task-branch"
if try_commit "unrelated-dir-name"; then
  note "случай 3 (ветка не agent/*): коммит прошёл — ОК (правило вне области)"
else
  note "случай 3 (ветка не agent/*): коммит ОТКЛОНЁН — ОШИБКА, правило не должно применяться"
  cat "$WORK/unrelated-dir-name.stderr" >&2
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "worktree-guard: все три случая прошли как ожидалось"
fi
exit "$fail"
