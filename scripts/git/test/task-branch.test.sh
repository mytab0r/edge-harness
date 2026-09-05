#!/usr/bin/env bash
# Проверка на входе (не гвардия постфактум) в scripts/git/task-branch: задача
# обязана существовать, быть открытой, нести метку task и не нести blocked —
# иначе ветку не заводим. Прогон настоящего файла скрипта (копируется как
# есть, не переписывается здесь заново) на реальном git с фейковым `gh` в
# PATH — сеть не нужна: `git remote` переписан на локальный bare-репозиторий
# через `url.<path>.insteadOf`, `git remote get-url origin` при этом
# по-прежнему отдаёт исходный github-вид URL (нужен скрипту для owner/repo).
#
# Мутация, которой доказана проверка: закомментируй блок «Проверка на входе»
# в scripts/git/task-branch (от `task_number=` до строки перед `git fetch
# origin --prune`) — случай 2 (закрытая задача) перестаёт отклоняться, тест
# краснеет. Верни блок — тест снова зелёный.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/git/task-branch"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── фейковый gh: маршруты по номеру issue в URL ──────────────────────────────
mkdir -p "$WORK/bin"
cat >"$WORK/bin/gh" <<'GHEOF'
#!/usr/bin/env bash
if [ "$1" = "api" ]; then
  url="$2"
  case "$url" in
    */issues/61) printf 'open\ttask\n' ;;
    */issues/62) printf 'closed\ttask\n' ;;
    */issues/63) printf 'open\t\n' ;;
    */issues/64) printf 'open\ttask,blocked\n' ;;
    */issues/65) echo "gh: HTTP 404: Not Found" >&2; exit 1 ;;
    */issues/66) echo "dial tcp: lookup api.github.com: no such host" >&2; exit 1 ;;
    *) echo "фейковый gh: неизвестный маршрут $url" >&2; exit 1 ;;
  esac
  exit 0
fi
echo "фейковый gh: неизвестная команда $*" >&2
exit 1
GHEOF
chmod +x "$WORK/bin/gh"

# ── подготовка: bare origin с github-видом URL через insteadOf ──────────────
git init -q --bare -b main "$WORK/origin.git"
git init -q -b main "$WORK/seed"
(
  cd "$WORK/seed"
  git config user.email test@example.com
  git config user.name test
  echo seed >file.txt
  git add file.txt
  git commit -q -m init
  git remote add origin "$WORK/origin.git"
  git push -q origin HEAD:refs/heads/main
)

make_tree() {
  local dirname="$1"
  git clone -q "$WORK/origin.git" "$WORK/$dirname"
  (
    cd "$WORK/$dirname"
    git config user.email test@example.com
    git config user.name test
    # Github-вид URL для owner/repo, реально резолвится в локальный bare —
    # ровно то, что видит task-branch в проде (origin — github.com, а
    # ls-remote/fetch ходят по сети): здесь insteadOf прячет сеть за файл.
    git remote set-url origin https://github.com/o/r.git
    git config "url.$WORK/origin.git.insteadOf" https://github.com/o/r.git
  )
}

run_task_branch() {
  local dirname="$1" task="$2"
  (
    cd "$WORK/$dirname"
    PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" "$task"
  )
}

# ── случай 1: открытая задача с меткой task — ветка заводится ────────────────
make_tree "case1"
if out=$(run_task_branch "case1" "61-good" 2>"$WORK/case1.stderr"); then
  branch=$(git -C "$WORK/case1" branch --show-current)
  if [ "$branch" = "agent/61-good" ]; then
    note "случай 1 (открыта, метка task): ветка заведена — ОК"
  else
    note "случай 1: ветка не та ($branch) — ОШИБКА"
    fail=1
  fi
else
  note "случай 1: скрипт отказал — ОШИБКА, ожидался успех"
  cat "$WORK/case1.stderr" >&2
  fail=1
fi

# ── случай 2: закрытая задача — отказ, ветки нет ─────────────────────────────
make_tree "case2"
if run_task_branch "case2" "62-closed" 2>"$WORK/case2.stderr"; then
  note "случай 2 (закрытая задача): скрипт создал ветку — ОШИБКА, ожидался отказ"
  fail=1
else
  msg="$(cat "$WORK/case2.stderr")"
  case "$msg" in
    *"закрыта"*) note "случай 2 (закрытая задача): отказ с внятным сообщением — ОК" ;;
    *) note "случай 2: отказ без внятной причины — ОШИБКА ($msg)"; fail=1 ;;
  esac
  branch=$(git -C "$WORK/case2" branch --show-current)
  [ "$branch" = "main" ] || { note "случай 2: ветка всё же переключена на «$branch» — ОШИБКА"; fail=1; }
fi

# ── случай 3: открыта, но без метки task — отказ ─────────────────────────────
make_tree "case3"
if run_task_branch "case3" "63-no-label" 2>"$WORK/case3.stderr"; then
  note "случай 3 (нет метки task): скрипт создал ветку — ОШИБКА"
  fail=1
else
  case "$(cat "$WORK/case3.stderr")" in
    *"нет метки task"*) note "случай 3 (нет метки task): отказ с внятным сообщением — ОК" ;;
    *) note "случай 3: отказ без внятной причины — ОШИБКА"; fail=1 ;;
  esac
fi

# ── случай 4: метка blocked — отказ ──────────────────────────────────────────
make_tree "case4"
if run_task_branch "case4" "64-blocked" 2>"$WORK/case4.stderr"; then
  note "случай 4 (blocked): скрипт создал ветку — ОШИБКА"
  fail=1
else
  case "$(cat "$WORK/case4.stderr")" in
    *"blocked"*) note "случай 4 (blocked): отказ с внятным сообщением — ОК" ;;
    *) note "случай 4: отказ без внятной причины — ОШИБКА"; fail=1 ;;
  esac
fi

# ── случай 5: задача не найдена (404) — отказ ────────────────────────────────
make_tree "case5"
if run_task_branch "case5" "65-missing" 2>"$WORK/case5.stderr"; then
  note "случай 5 (задача не найдена): скрипт создал ветку — ОШИБКА"
  fail=1
else
  case "$(cat "$WORK/case5.stderr")" in
    *"не найдена"*) note "случай 5 (задача не найдена): отказ с внятным сообщением — ОК" ;;
    *) note "случай 5: отказ без внятной причины — ОШИБКА"; fail=1 ;;
  esac
fi

# ── случай 6: gh отвечает сетевой ошибкой (не HTTP-код) — предупреждение, НЕ отказ ──
make_tree "case6"
if out=$(run_task_branch "case6" "66-offline" 2>"$WORK/case6.stderr"); then
  branch=$(git -C "$WORK/case6" branch --show-current)
  if [ "$branch" = "agent/66-offline" ]; then
    note "случай 6 (сетевая ошибка gh): ветка заведена непроверенной — ОК"
  else
    note "случай 6: ветка не та ($branch) — ОШИБКА"
    fail=1
  fi
  grep -q "ПРЕДУПРЕЖДЕНИЕ" "$WORK/case6.stderr" || { note "случай 6: нет предупреждения — ОШИБКА"; fail=1; }
else
  note "случай 6 (сетевая ошибка gh): скрипт отказал — ОШИБКА, ожидалось предупреждение и успех"
  cat "$WORK/case6.stderr" >&2
  fail=1
fi

# ── случай 7: gh не установлен — предупреждение, ветка заводится ────────────
# PATH без каталогов, где лежит хоть какой-то `gh` (фейковый из $WORK/bin
# или настоящий из системного PATH) — портируемо между Windows/Linux, без
# завязки на имя каталога. На GitHub-раннерах gh и bash оба лежат в /usr/bin —
# та же чистка вынесла бы и bash: PATH="$safe_path" bash… не находит команду
# `bash` (assignment-префикс простой команды режет PATH и для её собственного
# поиска, не только для дочернего процесса — проверено). Резолвим bash
# абсолютным путём ДО чистки, зовём по нему — сам bash от PATH не зависит.
bash_bin="$(command -v bash)"
safe_path=""
IFS=':' read -ra _dirs <<<"$PATH"
for _d in "${_dirs[@]}"; do
  [ -n "$_d" ] || continue
  if [ ! -e "$_d/gh" ] && [ ! -e "$_d/gh.exe" ]; then
    safe_path="$safe_path:$_d"
  fi
done
safe_path="${safe_path#:}"

make_tree "case7"
if (
  cd "$WORK/case7"
  PATH="$safe_path" "$bash_bin" "$SCRIPT_SRC" "67-no-gh"
) 2>"$WORK/case7.stderr"; then
  branch=$(git -C "$WORK/case7" branch --show-current)
  if [ "$branch" = "agent/67-no-gh" ]; then
    note "случай 7 (gh не найден): ветка заведена непроверенной — ОК"
  else
    note "случай 7: ветка не та ($branch) — ОШИБКА"
    fail=1
  fi
  grep -q "ПРЕДУПРЕЖДЕНИЕ" "$WORK/case7.stderr" || { note "случай 7: нет предупреждения — ОШИБКА"; fail=1; }
else
  note "случай 7 (gh не найден): скрипт отказал — ОШИБКА, ожидался успех с предупреждением"
  cat "$WORK/case7.stderr" >&2 || true
  fail=1
fi

if [ "$fail" = 0 ]; then
  echo "task-branch: все случаи входной проверки прошли как ожидалось"
fi
exit "$fail"
