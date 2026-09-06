#!/usr/bin/env bash
# Проверка на входе scripts/gh/issue-create (#566): заголовок новой issue
# сверяется с уже ОТКРЫТЫМИ задачами пула (scripts/lib/duplicate_guard.py) —
# похожая находится ДО вызова `gh issue create`, реальный `gh` для создания
# issue не вызывается вовсе, если совпадение не подтверждено осознанно.
#
# Фикстуры заголовков — ДОСЛОВНЫЕ заголовки живого инцидента 2026-09-06
# (#562/#564, реальный измеренный дубль; см. scripts/lib/test_duplicate_guard.py
# и модульный докстринг duplicate_guard.py).
#
# Мутация, которой доказана проверка: в scripts/gh/issue-create закомментируй
# блок `if [ -n "$matches" ]` целиком (до соответствующего `fi`, оставив
# только `exec gh issue create "${args[@]}"`) — случай 1 (похожий заголовок
# без --confirm-not-duplicate) перестаёт отклоняться и оставляет маркер
# вызова gh issue create, тест краснеет. Верни блок — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/gh/issue-create"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

TITLE_562='deploy-dsh-edge: автооткат (#549) оставляет рассинхрон версий — следующий wrangler secret put падает'
TITLE_564='deploy-dsh-edge: автооткат (#549) блокирует следующий деплой — wrangler secret put падает VERSION_NOT_DEPLOYED'
TITLE_UNRELATED='Атомарная аренда задачи (task lease): единый claim для всех пайплайнов'

mkdir -p "$WORK/bin"
MARKER="$WORK/gh-issue-create-called"
CREATED_BODY="$WORK/created-body"

# Фикстура открытого пула для scripts/lib/duplicate_guard.py — читается через
# тестовый шов DUPLICATE_GUARD_FIXTURE (см. duplicate_guard.py::fetch_open_task_candidates).
# Не через фейковый `gh issue list` в PATH: duplicate_guard.py зовётся ИЗ
# scripts/gh/issue-create отдельным процессом python3, и на Windows нативный
# python.exe резолвит голое имя `gh` через CreateProcess (автодобавление
# только .exe), а не через PATHEXT-полный поиск — фейковый bash-скрипт `gh`
# без расширения там не находится, находится реальный установленный gh.exe.
# Env-переменная — явный, документированный шов, а не скрытая магия.
FIXTURE="$WORK/open-tasks.json"
cat >"$FIXTURE" <<FIXEOF
[
  {"number": 562, "title": "$TITLE_562", "url": "https://github.com/o/r/issues/562"},
  {"number": 1, "title": "$TITLE_UNRELATED", "url": "https://github.com/o/r/issues/1"}
]
FIXEOF
export DUPLICATE_GUARD_FIXTURE="$FIXTURE"

cat >"$WORK/bin/gh" <<GHEOF
#!/usr/bin/env bash
if [ "\$1" = "repo" ] && [ "\$2" = "view" ]; then
  echo "o/r"
  exit 0
fi
if [ "\$1" = "issue" ] && [ "\$2" = "create" ]; then
  echo called >"$MARKER"
  # найти значение после --body или содержимое файла после --body-file
  args=("\$@")
  for idx in "\${!args[@]}"; do
    if [ "\${args[\$idx]}" = "--body" ]; then
      next=\$((idx + 1))
      printf '%s' "\${args[\$next]}" >"$CREATED_BODY"
    fi
    if [ "\${args[\$idx]}" = "--body-file" ]; then
      next=\$((idx + 1))
      cat "\${args[\$next]}" >"$CREATED_BODY"
    fi
  done
  echo "https://github.com/o/r/issues/999"
  exit 0
fi
echo "unexpected gh call: \$*" >&2
exit 1
GHEOF
# GHEOF (терминатор без кавычек) интерполирует $MARKER/$CREATED_BODY сразу
# при записи файла. \$1/\$2/\$@ экранированы намеренно — это позиционные
# параметры САМОГО fake-gh при его запуске, не этого генерирующего скрипта.
# Этот fake-gh резолвится только вызовами ИЗ bash (repo view, issue create) —
# git-bash понимает shebang-скрипт без расширения; python3-вызов gh issue
# list обходит его через DUPLICATE_GUARD_FIXTURE выше, вообще не трогая PATH.
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH"

fail=0
note() { echo "$@"; }

# ── случай 1: похожий заголовок, БЕЗ --confirm-not-duplicate — отказ ─────────
rm -f "$MARKER" "$CREATED_BODY"
if bash "$SCRIPT_SRC" --title "$TITLE_564" --body b --label task >"$WORK/out1" 2>"$WORK/err1"; then
  note "FAIL случай 1: похожий заголовок принят без --confirm-not-duplicate"; fail=1
elif [ -f "$MARKER" ]; then
  note "FAIL случай 1: gh issue create был вызван, хотя похожая задача есть"; fail=1
elif ! grep -q "562" "$WORK/err1"; then
  note "FAIL случай 1: кандидат #562 не назван в отказе"; cat "$WORK/err1"; fail=1
else
  note "OK случай 1: похожий заголовок (реальный инцидент #562/#564) отклонён, gh не вызван"
fi

# ── случай 2: похожий заголовок + --confirm-not-duplicate — принято, причина в теле ──
rm -f "$MARKER" "$CREATED_BODY"
if ! bash "$SCRIPT_SRC" --title "$TITLE_564" --body "исходное тело" --label task \
    --confirm-not-duplicate "разные причины отказа, не дубль" >"$WORK/out2" 2>"$WORK/err2"; then
  note "FAIL случай 2: --confirm-not-duplicate отклонён"; cat "$WORK/err2"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 2: gh issue create не вызван при явном --confirm-not-duplicate"; fail=1
elif ! grep -q "исходное тело" "$CREATED_BODY"; then
  note "FAIL случай 2: исходное тело потеряно"; cat "$CREATED_BODY"; fail=1
elif ! grep -q "разные причины отказа, не дубль" "$CREATED_BODY"; then
  note "FAIL случай 2: причина --confirm-not-duplicate не попала в тело issue"; cat "$CREATED_BODY"; fail=1
elif ! grep -q "562" "$CREATED_BODY"; then
  note "FAIL случай 2: список проверенных кандидатов не попал в тело issue"; cat "$CREATED_BODY"; fail=1
else
  note "OK случай 2: --confirm-not-duplicate принят, причина и кандидаты видны в теле issue"
fi

# ── случай 3: непохожий заголовок — принято без всякого флага ───────────────
rm -f "$MARKER" "$CREATED_BODY"
if ! bash "$SCRIPT_SRC" --title "Совсем другая задача про докер-канарейку" --body b --label task \
    >"$WORK/out3" 2>"$WORK/err3"; then
  note "FAIL случай 3: непохожий заголовок отклонён"; cat "$WORK/err3"; fail=1
elif [ ! -f "$MARKER" ]; then
  note "FAIL случай 3: gh issue create не вызван для непохожего заголовка"; fail=1
else
  note "OK случай 3: непохожий заголовок принят без --confirm-not-duplicate"
fi

exit "$fail"
