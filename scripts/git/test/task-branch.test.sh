#!/usr/bin/env bash
# task-branch заводит/переиспользует рабочее дерево (#332), не только ветку.
# Прогоняет РЕАЛЬНЫЙ scripts/git/task-branch (копия файла как есть) на
# синтетическом origin — не пересказ поведения, а факт его исполнения.
#
#   1) вне CI (без GITHUB_ACTIONS) — заводит .claude/worktrees/<task>,
#      печатает путь, текущий каталог остаётся на своей ветке;
#   2) повторный вызов на ту же задачу — переиспользует то же дерево, не
#      падает и не плодит второе;
#   3) в CI (GITHUB_ACTIONS=true) — старое поведение: переключает ветку
#      прямо в текущем каталоге, worktree не заводит (обратная совместимость
#      с scripts/worker/task.sh, который сам работает в одноразовом чекауте).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/git/task-branch"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

new_origin() {
  local name="$1"
  git init -q --bare "$WORK/$name-origin.git"
  git init -q "$WORK/$name-seed"
  (
    cd "$WORK/$name-seed"
    git config user.email test@example.com
    git config user.name test
    git commit -q --allow-empty -m init
    git remote add origin "$WORK/$name-origin.git"
    git push -q origin HEAD:refs/heads/main
  )
}

# ── случай 1+2: вне CI — заводит и переиспользует worktree ──────────────────
new_origin "a"
git clone -q --branch main "$WORK/a-origin.git" "$WORK/a-main" 2>/dev/null
cp "$SCRIPT_SRC" "$WORK/a-main/task-branch"
chmod +x "$WORK/a-main/task-branch"

(cd "$WORK/a-main" && env -u GITHUB_ACTIONS bash ./task-branch 99-demo-slug) \
  >"$WORK/a-run1.out" 2>&1 || { fail=1; note "run1 упал:"; cat "$WORK/a-run1.out"; }

# Путь берём из самого git (worktree list), а не собираем строкой сами: на
# Windows/Cygwin git печатает диск-стиль (C:/Users/...), а $WORK — POSIX-стиль
# (/tmp/...) — один и тот же каталог, разные строки одного пути.
expected_wt=$(
  git -C "$WORK/a-main" worktree list --porcelain \
    | awk '/^worktree /{p=$2} /^branch refs\/heads\/agent\/99-demo-slug$/{print p}'
)
if [ -n "$expected_wt" ] && [ -d "$expected_wt" ]; then
  note "случай 1 (вне CI, новая задача): worktree заведён на agent/99-demo-slug — ОК"
else
  note "случай 1 (вне CI, новая задача): worktree НЕ заведён — ОШИБКА"
  fail=1
fi
if [ -n "$expected_wt" ] && grep -qF "$expected_wt" "$WORK/a-run1.out"; then
  note "  путь напечатан агенту — ОК"
else
  note "  путь НЕ напечатан — ОШИБКА (агент не узнает, куда идти)"
  fail=1
fi
main_branch_after=$(git -C "$WORK/a-main" rev-parse --abbrev-ref HEAD)
if [ "$main_branch_after" = "master" ] || [ "$main_branch_after" = "main" ]; then
  note "  исходный каталог остался на своей ветке ($main_branch_after) — ОК"
else
  note "  исходный каталог переключился на $main_branch_after — ОШИБКА (не должен трогаться)"
  fail=1
fi

before_count=$(git -C "$WORK/a-main" worktree list --porcelain | grep -c '^worktree ')
(cd "$WORK/a-main" && env -u GITHUB_ACTIONS bash ./task-branch 99-demo-slug) \
  >"$WORK/a-run2.out" 2>&1 || { fail=1; note "run2 (повтор) упал:"; cat "$WORK/a-run2.out"; }
after_count=$(git -C "$WORK/a-main" worktree list --porcelain | grep -c '^worktree ')
if [ "$before_count" = "$after_count" ] && grep -qF "$expected_wt" "$WORK/a-run2.out"; then
  note "случай 2 (повтор на ту же задачу): дерево переиспользовано, дубля нет — ОК"
else
  note "случай 2 (повтор на ту же задачу): дубль дерева или путь не сообщён — ОШИБКА"
  fail=1
fi

# ── случай 3: CI — старое поведение (переключение в текущем каталоге) ───────
new_origin "b"
git clone -q --branch main "$WORK/b-origin.git" "$WORK/b-main" 2>/dev/null
cp "$SCRIPT_SRC" "$WORK/b-main/task-branch"
chmod +x "$WORK/b-main/task-branch"
(cd "$WORK/b-main" && GITHUB_ACTIONS=true bash ./task-branch 55-ci-demo) \
  >"$WORK/b-run.out" 2>&1 || { fail=1; note "CI-прогон упал:"; cat "$WORK/b-run.out"; }
ci_branch=$(git -C "$WORK/b-main" rev-parse --abbrev-ref HEAD)
if [ "$ci_branch" = "agent/55-ci-demo" ] && [ ! -d "$WORK/b-main/.claude/worktrees" ]; then
  note "случай 3 (CI): ветка переключена в текущем каталоге, worktree не заведён — ОК"
else
  note "случай 3 (CI): ожидалось старое поведение (ветка $ci_branch, worktree отсутствует) — ОШИБКА"
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "task-branch: worktree заводится/переиспользуется вне CI, в CI поведение не изменилось"
fi
exit "$fail"
