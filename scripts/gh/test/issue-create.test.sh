#!/usr/bin/env bash
# Проверка на входе scripts/gh/issue-create (#526): issue без метки `task`
# отклоняется ДО вызова `gh issue create` — реальный `gh` не вызывается вовсе
# (фейковый gh ниже оставляет маркер только при настоящем вызове
# `issue create`, тест проверяет это явно, а не по коду возврата).
#
# Живой случай, который эта обёртка закрывает: issue #523 (`gh issue create
# --label white-spot`, без task — пул воркера её не увидел) и #425 (без
# единой метки).
#
# Мутация, которой доказана проверка: закомментируй блок `if [ "$has_task"
# -ne 1 ]` в scripts/gh/issue-create (до `fi` перед `exec gh issue create`) —
# случай 1 (--label white-spot без task) перестаёт отклоняться и оставляет
# маркер, тест краснеет. Верни блок — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/gh/issue-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-issue-create-called"
cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  echo "o/r"
  exit 0
fi
if [ "\$1" = "issue" ] && [ "\$2" = "create" ]; then
  echo called >"$MARKER"
  echo "https://github.com/o/r/issues/1"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

# Приоритетная печать (#695) — сеть подменена фикстурой (пустой пул), этот
# файл тестирует ТОЛЬКО метку task, не приоритет (тот — issue-create-priority-gate.test.sh).
export PRIORITY_TOP_FIXTURE="$WORK/priority-fixture.json"
echo '[]' >"$PRIORITY_TOP_FIXTURE"

fail=0
note() { echo "$@"; }

# ── случай 1: только white-spot, без task — отказ, gh не вызван ──────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b --label white-spot >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: issue-create принял --label white-spot без task"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh issue create был вызван, хотя task отсутствует"; fail=1
elif ! grep -q "без метки task" "$WORK/err1"; then
  note "FAIL случай 1: нет объяснения в stderr"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: --label white-spot без task отклонён, gh не вызван"
fi

# ── случай 2: вовсе без --label (класс #425) — отказ ──────────────────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: issue-create принял issue вовсе без меток"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 2: gh вызван без единой метки"; fail=1
else
  note "OK случай 2: issue без меток отклонена (класс #425)"
fi

# ── случай 3: --label task — принимает и вызывает gh (приоритет явно снят #695) ─
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title t --body b --label task \
    --not-process-ack "не про приоритет, тест метки task (#526)" >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: --label task отклонён"; cat "$WORK/err3"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 3: gh issue create не вызван при наличии task"; fail=1
else
  note "OK случай 3: --label task принят, gh вызван"
fi

# ── случай 4: --label task,white-spot одним значением — принимает ────────────
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title t --body b --label "task,white-spot" \
    --not-process-ack "не про приоритет, тест метки task (#526)" >"$WORK/out4" 2>"$WORK/err4"; then
  note "FAIL случай 4: комбинированный --label с task отклонён"; cat "$WORK/err4"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 4: gh не вызван для комбинированного --label с task"; fail=1
else
  note "OK случай 4: 'task,white-spot' в одном --label распознан как содержащий task"
fi

# ── случай 5: white-spot,area:process без task одним значением — отказ ───────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body b --label "white-spot,area:process" >"$WORK/out5" 2>"$WORK/err5"; then
  note "FAIL случай 5: комбинированный --label без task принят"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 5: gh вызван для комбинированного --label без task"; fail=1
else
  note "OK случай 5: 'white-spot,area:process' без task отклонён"
fi

# ── случай 6: --no-task-ack "причина" — осознанный пропуск, gh вызван ────────
rm -f "$MARKER"
if ! bash "$SCRIPT_SRC" --title t --body b --label white-spot --no-task-ack "не задача пула" >"$WORK/out6" 2>"$WORK/err6"; then
  note "FAIL случай 6: --no-task-ack отклонён"; cat "$WORK/err6"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 6: gh не вызван при явном --no-task-ack"; fail=1
elif ! grep -q "не задача пула" "$WORK/err6"; then
  note "FAIL случай 6: причина --no-task-ack не попала в лог"; cat "$WORK/err6"; fail=1
else
  note "OK случай 6: --no-task-ack осознанно пропускает проверку, причина видна в логе"
fi

exit "$fail"
