#!/usr/bin/env bash
# Проверка на входе scripts/git/pr-create (issue #496): тело с Closes/Fixes/
# Resolves отклоняется ДО вызова `gh pr create` — реальный `gh` не вызывается
# вовсе (фейковый gh ниже оставляет маркер только при настоящем вызове
# `pr create`, и тест это проверяет явно, а не по коду возврата).
#
# Мутация, которой доказана проверка: закомментируй блок `if [ -n "$body" ]`
# в scripts/git/pr-create (до `fi` перед `exec gh pr create`) — случай 1
# (директива в --body-file) перестаёт отклоняться и оставляет маркер, тест
# краснеет. Верни блок — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/git/pr-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-pr-create-called"
cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "pr" ] && [ "\$2" = "create" ]; then
  echo called >"$MARKER"
  echo "https://github.com/o/r/pull/1"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

fail=0
note() { echo "$@"; }

# ── случай 1: директива в --body-file (реальный файл) ────────────────────────
rm -f "$MARKER"
body_file1="$WORK/body1.md"
printf 'Closes #490.\n\n## Что сделано\n' >"$body_file1"
if bash "$SCRIPT_SRC" --title t --body-file "$body_file1" >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: pr-create принял тело с 'Closes #490.'"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh pr create был вызван, хотя тело нарушает правило"; fail=1
elif ! grep -q "Не пиши Closes/Fixes/Resolves" "$WORK/err1"; then
  note "FAIL случай 1: нет объяснения в stderr"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: директива в --body-file отклонена, gh не вызван"
fi

# ── случай 2: чистое тело — принимает и вызывает gh ───────────────────────────
rm -f "$MARKER"
body_file2="$WORK/body2.md"
printf '#490\n\n## Что сделано\n- всё по делу\n' >"$body_file2"
if ! bash "$SCRIPT_SRC" --title t --body-file "$body_file2" >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: чистое тело отклонено"; cat "$WORK/err2"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 2: gh pr create не был вызван для чистого тела"; fail=1
else
  note "OK случай 2: чистое тело принято, gh вызван"
fi

# ── случай 3: --body-file - (stdin) с директивой ──────────────────────────────
rm -f "$MARKER"
if printf 'Fixes #1\n' | bash "$SCRIPT_SRC" --title t --body-file - >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: директива через stdin принята"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 3: gh вызван при директиве через stdin"; fail=1
else
  note "OK случай 3: директива через --body-file - отклонена"
fi

# ── случай 4: --body напрямую с директивой (регистронезависимо) ──────────────
rm -f "$MARKER"
if bash "$SCRIPT_SRC" --title t --body "resolves #7" >"$WORK/out4" 2>"$WORK/err4"; then
  note "FAIL случай 4: директива через --body принята"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 4: gh вызван при директиве через --body"; fail=1
else
  note "OK случай 4: директива через --body отклонена"
fi

# ── случай 5: слово без #N рядом — не директива, пропускает ─────────────────
rm -f "$MARKER"
body_file5="$WORK/body5.md"
printf '#490\n\nЭта правка closes семантический разрыв в парсере, не задачу.\n' >"$body_file5"
if ! bash "$SCRIPT_SRC" --title t --body-file "$body_file5" >"$WORK/out5" 2>"$WORK/err5"; then
  note "FAIL случай 5: безобидное упоминание слова отклонено"; cat "$WORK/err5"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 5: gh не вызван для безобидного тела"; fail=1
else
  note "OK случай 5: 'closes' без #N рядом не триггерит отказ"
fi

# ── случай 6: директива внутри inline-кода — GitHub её не видит, детектор
# task_ref.closing_keyword_refs (#423/#429) тоже не должен, тело принимается ─
rm -f "$MARKER"
body_file6="$WORK/body6.md"
printf '#490\n\nПравило: `` `Closes/Fixes/Resolves #N` `` запрещены контрактом.\n' >"$body_file6"
if ! bash "$SCRIPT_SRC" --title t --body-file "$body_file6" >"$WORK/out6" 2>"$WORK/err6"; then
  note "FAIL случай 6: упоминание в inline-коде отклонено"; cat "$WORK/err6"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 6: gh не вызван для тела с упоминанием в inline-коде"; fail=1
else
  note "OK случай 6: директива внутри inline-кода не триггерит отказ (семантика #423/#429)"
fi

exit "$fail"
