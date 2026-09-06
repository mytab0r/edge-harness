#!/usr/bin/env bash
# Гвардия класса #476 (живой прогон worker.yml 34027035455, PR #408): доводка
# уже открытого PR (шаг «5. Ветка» в scripts/worker/task.sh, CONTINUE_PR_NUMBER)
# обязана исполнять инструментарий (scripts/*, вызываемое по пути — здесь
# scripts/gh/infra_digest.sh) из main-дерева, а рабочее дерево задачи держать
# отдельно (linked git worktree), а не заменять main-дерево содержимым старой
# ветки PR через `git checkout -B` на месте.
#
# Извлекаем РЕАЛЬНЫЙ блок из scripts/worker/task.sh (между тем же якорем-
# комментарием «Ветка: новая от свежего origin/main» и первым закрывающим
# `fi`) и исполняем его как есть — не переписываем логику здесь заново,
# иначе тест держит копию, а не проверяет код. Сценарий: локальный bare
# origin, где ветка PR ответвлена ДО того, как в main добавили
# scripts/gh/infra_digest.sh — та же форма живого отказа.
#
# Мутация, которой доказана проверка: верни в task.sh старое поведение
# (`git checkout -B "$BRANCH" "origin/$BRANCH"` в том же дереве вместо
# `git worktree add`) — тест должен покраснеть (infra_digest.sh пропадает
# из main-дерева). Восстанови фикс — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TASK_SH="$REPO_ROOT/scripts/worker/task.sh"

WORK="$(mktemp -d)"
cleanup() {
  # Воркспейс мог поставить linked worktree — снять его явно, иначе
  # временная директория осиротевшей записью висит в .git/worktrees хоть
  # какого-то временного bare-репо (сам bare удаляется вместе с $WORK).
  rm -rf "$WORK"
}
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── извлечение блока «5. Ветка» из настоящего task.sh ────────────────────────
SNIPPET="$WORK/branch-block.sh"
awk '
  /Ветка: новая от свежего origin\/main/ { grab = 1 }
  grab { print }
  grab && /^fi$/ { exit }
' "$TASK_SH" >"$SNIPPET"
[ -s "$SNIPPET" ] || { echo "::error::блок «5. Ветка» не найден в $TASK_SH — якорь-комментарий переименован?" >&2; exit 1; }
grep -q 'git worktree add' "$SNIPPET" \
  || { echo "::error::в извлечённом блоке нет git worktree add — тест смотрит не туда" >&2; exit 1; }

# ── bare origin: main расходится с веткой PR после её ответвления ───────────
git init -q --bare -b main "$WORK/origin.git"
git init -q -b main "$WORK/seed"
(
  cd "$WORK/seed"
  git config user.email test@example.com
  git config user.name test
  mkdir -p scripts/worker scripts/gh
  echo base >file.txt
  # git не хранит пустые каталоги: без файла внутри scripts/worker путь
  # scripts/worker/../gh/... не резолвится после чекаута (каталог физически
  # отсутствует) — реальный task.sh живёт именно в scripts/worker, поэтому
  # каталог у него всегда есть; здесь имитируем тем же placeholder-файлом.
  echo placeholder >scripts/worker/.keep
  git add file.txt scripts/worker/.keep
  git commit -q -m base
  git remote add origin "$WORK/origin.git"
  git push -q origin HEAD:refs/heads/main
  # Ветка PR ответвляется ЗДЕСЬ — до появления infra_digest.sh в main.
  git push -q origin HEAD:refs/heads/agent/999-old-pr
  # main уходит вперёд: добавляем именно тот файл, чьё отсутствие роняло
  # доводку живьём (#476, #326).
  cat >scripts/gh/infra_digest.sh <<'DIGEST'
print_infra_digest() { echo "INFRA_DIGEST_MARKER_OK"; }
DIGEST
  git add scripts/gh/infra_digest.sh
  git commit -q -m "main: добавлен infra_digest.sh (имитация #323/#326)"
  git push -q origin HEAD:refs/heads/main
)

# ── воркспейс job'а: чекаут main (как worker.yml, ref=main всегда) ──────────
WORKSPACE="$WORK/workspace"
git clone -q "$WORK/origin.git" "$WORKSPACE"
(
  cd "$WORKSPACE"
  git config user.email test@example.com
  git config user.name test
)
[ -f "$WORKSPACE/scripts/gh/infra_digest.sh" ] \
  || { echo "::error::подготовка теста сломана: infra_digest.sh нет даже в main-воркспейсе" >&2; exit 1; }

before_branch=$(git -C "$WORKSPACE" branch --show-current)

# ── исполняем настоящий блок доводки PR ──────────────────────────────────────
BRANCH="agent/999-old-pr"
CONTINUE_PR_NUMBER="999"
number="999"
slug="old-pr"
SCRIPT_DIR="$WORKSPACE/scripts/worker"
# Сиблинг воркспейса, НЕ вложенный в него каталог — та же топология, что на
# раннере (RUNNER_TEMP не лежит внутри GITHUB_WORKSPACE): снипет читает $WORK
# как рабочий каталог задачи ($WORK/pr-worktree).
WORK_VAR_FOR_SNIPPET="$WORK/task-work"
mkdir -p "$WORK_VAR_FOR_SNIPPET"

out="$WORK/run.out"
(
  cd "$WORKSPACE"
  export BRANCH CONTINUE_PR_NUMBER number slug SCRIPT_DIR
  export WORK="$WORK_VAR_FOR_SNIPPET"
  bash -c "set -euo pipefail; source '$SNIPPET'; echo PWD_AFTER=\$PWD"
) >"$out" 2>&1 || { echo "::error::блок «5. Ветка» упал:" >&2; cat "$out" >&2; exit 1; }

cat "$out"

pwd_after=$(grep '^PWD_AFTER=' "$out" | cut -d= -f2-)
pr_worktree_expected="$WORK_VAR_FOR_SNIPPET/pr-worktree"

# ── проверки ──────────────────────────────────────────────────────────────
if [ "$pwd_after" != "$pr_worktree_expected" ]; then
  note "ОШИБКА: cwd после блока «$pwd_after», ожидалось «$pr_worktree_expected» (рабочее дерево задачи)"
  fail=1
else
  note "OK: cwd после блока — отдельное рабочее дерево ($pwd_after)"
fi

if [ -f "$WORKSPACE/scripts/gh/infra_digest.sh" ]; then
  note "OK: main-воркспейс не тронут — scripts/gh/infra_digest.sh на месте"
else
  note "ОШИБКА: main-воркспейс потерял scripts/gh/infra_digest.sh — регресс класса #476"
  fail=1
fi

after_branch=$(git -C "$WORKSPACE" branch --show-current)
if [ "$after_branch" != "$before_branch" ]; then
  note "ОШИБКА: main-воркспейс переключил ветку ($before_branch → $after_branch) — рабочее дерево задачи обязано быть отдельным каталогом"
  fail=1
else
  note "OK: main-воркспейс остался на своей ветке ($after_branch)"
fi

if [ -f "$pr_worktree_expected/scripts/gh/infra_digest.sh" ]; then
  note "ОШИБКА ТЕСТА: рабочее дерево задачи содержит infra_digest.sh — сценарий расхождения веток не воспроизведён"
  fail=1
else
  note "OK: рабочее дерево задачи (ветка PR) не содержит infra_digest.sh — воспроизведён живой класс расхождения"
fi

pr_worktree_branch=$(git -C "$pr_worktree_expected" branch --show-current 2>/dev/null || echo "?")
if [ "$pr_worktree_branch" != "$BRANCH" ]; then
  note "ОШИБКА: рабочее дерево задачи не на ветке $BRANCH (сейчас: $pr_worktree_branch)"
  fail=1
else
  note "OK: рабочее дерево задачи на ветке $BRANCH"
fi

if grep -q 'INFRA_DIGEST_MARKER_OK' "$out"; then
  note "OK: print_infra_digest выполнилась (источник — main-дерево, не рабочее дерево задачи)"
else
  note "ОШИБКА: print_infra_digest не выполнилась — ровно тот отказ, что был в живом прогоне 34027035455"
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "continue-pr-worktree: доводка PR держит инструментарий на main и рабочее дерево отдельно — ОК"
fi
exit "$fail"
