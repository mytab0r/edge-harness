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
#
# Случаи 8-10 — рабочее дерево (#332): task-branch заводит/переиспользует
# рабочее дерево, не только ветку:
#   8) вне CI (без GITHUB_ACTIONS) — заводит .claude/worktrees/<task>,
#      печатает путь, текущий каталог остаётся на своей ветке;
#   9) повторный вызов на ту же задачу — переиспользует то же дерево, не
#      падает и не плодит второе;
#   10) в CI (GITHUB_ACTIONS=true) — старое поведение: переключает ветку
#       прямо в текущем каталоге, worktree не заводит (обратная совместимость
#       с scripts/worker/task.sh, который сам работает в одноразовом чекауте);
#   11) стык с гвардией (находка ревью #333): после переключения ветки в
#       CI-режиме коммит настоящим .githooks/pre-commit обязан проходить —
#       без этого случая случай 10 (CI-режим task-branch) и
#       worktree-guard.test.sh (гвардия вне CI) проверяют половины отдельно,
#       а стык — коммит после CI-переключения под настоящим хуком — не
#       покрывает никто;
#  12-14) сверка локальной ветки с origin/<branch> (находка ревью #333):
#      12) ветку подвинул другой канал — переиспользование отказывает
#          с командой синхронизации;
#      13) то же на пути усыновления (дерево пропало с диска);
#      14) свои незапушенные коммиты расхождением не считаются —
#          переиспользование работает;
#     15) ветка существует только на origin (чужой PR, дерево потеряно) —
#          усыновляется СЕРВЕРНАЯ голова, а не тихо заводится одноимённая
#          локальная от origin/main (класс #332, репро ревью #333).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPT_SRC="$REPO_ROOT/scripts/git/task-branch"
HOOK_SRC="$REPO_ROOT/.githooks/pre-commit"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

fail=0
note() { echo "$@"; }

# ── фейковый gh: маршруты по номеру issue в URL ──────────────────────────────
# Два потребителя одного бинаря в одном прогоне: «Проверка на входе» (#357/
# #358, `gh api .../issues/N --jq '[.state, labels] | @tsv'`) и гвардия «PR не
# заводится на эпик» (#376, `scripts/lib/epic_guard.py` → `gh repo view` +
# `gh api .../issues/N` БЕЗ --jq, ждёт настоящий JSON). Разветвление по
# наличию --jq: тот же URL, разный формат ответа — ни один из тестовых issue
# здесь не эпик (без префикса ЭПИК, sub_issues_summary нулевой), поэтому
# гвардия эпика везде молча пропускает и не мешает случаям 1-7.
mkdir -p "$WORK/bin"
cat >"$WORK/bin/gh" <<'GHEOF'
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
  case "$url" in
    graphql) echo '{"data":{"repository":{"issue":{"subIssues":{"nodes":[]}}}}}'; exit 0 ;;
    */issues/61)
      if [ "$has_jq" = 1 ]; then printf 'open\ttask\n'
      else echo '{"state":"open","title":"т","labels":[{"name":"task"}],"sub_issues_summary":{"total":0,"completed":0}}'
      fi ;;
    */issues/62)
      if [ "$has_jq" = 1 ]; then printf 'closed\ttask\n'
      else echo '{"state":"closed","title":"т","labels":[{"name":"task"}],"sub_issues_summary":{"total":0,"completed":0}}'
      fi ;;
    */issues/63)
      if [ "$has_jq" = 1 ]; then printf 'open\t\n'
      else echo '{"state":"open","title":"т","labels":[],"sub_issues_summary":{"total":0,"completed":0}}'
      fi ;;
    */issues/64)
      if [ "$has_jq" = 1 ]; then printf 'open\ttask,blocked\n'
      else echo '{"state":"open","title":"т","labels":[{"name":"task"},{"name":"blocked"}],"sub_issues_summary":{"total":0,"completed":0}}'
      fi ;;
    */issues/65) echo "gh: HTTP 404: Not Found" >&2; exit 1 ;;
    */issues/66) echo "dial tcp: lookup api.github.com: no such host" >&2; exit 1 ;;
    */issues/67)
      if [ "$has_jq" = 1 ]; then printf 'open\ttask\n'
      else echo '{"state":"open","title":"т","labels":[{"name":"task"}],"sub_issues_summary":{"total":0,"completed":0}}'
      fi ;;
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
    # -u GITHUB_ACTIONS: случаи 1-7 симулируют локальный не-CI запуск
    # (worktree, не переключение ветки на месте, #332) — если сам этот тест
    # выполняется внутри настоящего GitHub Actions job, переменная
    # GITHUB_ACTIONS=true уже стоит в окружении и наследовалась бы сюда,
    # заставляя task-branch взять CI-ветку поведения по ошибочной причине.
    env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" "$task"
  )
}

# Вне CI (#332) успешный запуск заводит ОТДЕЛЬНЫЙ worktree на agent/<task>,
# не переключает ветку в текущем каталоге. `git worktree list` перечисляет и
# сам исходный чекаут (первой строкой) — если бы скрипт вместо worktree
# сделал `git switch -c` на месте, исходный каталог сам оказался бы
# «совпадением» и маскировал регресс; поэтому путь ещё сверяется с исходным
# каталогом — совпадение с ним не считается заведённым worktree.
worktree_for_branch() {
  local dirname="$1" branch="$2"
  # Оба пути — из самого git (не из shell pwd): на Windows/Cygwin `git worktree
  # list` печатает диск-стиль (C:/Users/...), а builtin pwd — POSIX-стиль
  # (/c/Users/...) для ОДНОГО и того же каталога — прямое сравнение строк
  # дало бы ложное «это отдельный worktree» даже когда скрипт остался на
  # месте (`git switch -c`), маскируя ровно тот регресс, который тест обязан
  # ловить.
  local origin_dir path
  origin_dir=$(git -C "$WORK/$dirname" worktree list --porcelain | awk 'NR==1{print $2}')
  path=$(git -C "$WORK/$dirname" worktree list --porcelain \
    | awk -v want="refs/heads/$branch" '/^worktree /{p=$2} $0=="branch "want{print p}')
  [ -n "$path" ] || return 0
  [ "$path" = "$origin_dir" ] && return 0
  printf '%s\n' "$path"
}

# ── случай 1: открытая задача с меткой task — ветка заводится ────────────────
make_tree "case1"
if out=$(run_task_branch "case1" "61-good" 2>"$WORK/case1.stderr"); then
  wt=$(worktree_for_branch "case1" "agent/61-good")
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    note "случай 1 (открыта, метка task): worktree на agent/61-good заведён — ОК"
  else
    note "случай 1: worktree на agent/61-good НЕ найден — ОШИБКА"
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

# ── случай 6: gh отвечает сетевой ошибкой (не HTTP-код) ──────────────────────
# Проверка «открыта/метки» одна прощает сетевую ошибку (предупреждение,
# ветка заводится непроверенной), но та же сетевая ошибка бьёт и по gh-вызову
# гвардии эпика (#376) на том же номере — а для неё offline не прощается
# (proposal.md: «сбой сети/gh при проверке эпика тоже громкий»). Итог всего
# скрипта — отказ с обоими сообщениями, не успех.
make_tree "case6"
if out=$(run_task_branch "case6" "66-offline" 2>"$WORK/case6.stderr"); then
  note "случай 6 (сетевая ошибка gh): скрипт создал ветку — ОШИБКА, ожидался отказ гвардии эпика (офлайн-режима нет)"
  fail=1
else
  branch=$(git -C "$WORK/case6" branch --show-current)
  [ "$branch" = "main" ] || { note "случай 6: ветка всё же переключена на «$branch» — ОШИБКА"; fail=1; }
  grep -q "ПРЕДУПРЕЖДЕНИЕ" "$WORK/case6.stderr" || { note "случай 6: нет предупреждения проверки «открыта/метки» — ОШИБКА"; fail=1; }
  grep -q "epic_guard" "$WORK/case6.stderr" || { note "случай 6: нет громкого отказа гвардии эпика — ОШИБКА"; fail=1; }
  if [ "$fail" = 0 ]; then
    note "случай 6 (сетевая ошибка gh): предупреждение по задаче + громкий отказ гвардии эпика — ОК"
  fi
fi

# ── случай 7: gh не установлен ───────────────────────────────────────────────
# Проверка «открыта/метки» (#357/#358) без gh — мягкое предупреждение, но
# гвардия эпика (#376, тот же task-branch, тот же PATH) объявляет прямо в
# proposal.md: «офлайн-режима у входа в задачу нет… сбой сети/gh при проверке
# эпика тоже громкий, не тихий пропуск проверки». Цена молчаливого пропуска
# именно этой проверки — не абстрактная (PR #162, 44 раунда ревью на ветку,
# привязанную к эпику #77) — выше цены отказа завести ветку офлайн. Поэтому
# итог всего скрипта на «gh не найден» — отказ (с обоими сообщениями:
# предупреждение первой проверки + громкая ошибка эпик-гвардии), а не успех.
# Вычитание каталогов из PATH (первая попытка) ломалось на GitHub-раннере:
# там git/grep/bash/gh все живут в ОДНОМ /usr/bin, и вычёркивание каталога
# с gh вычёркивает вместе с ним и git, и grep, которыми пользуется сам
# task-branch (не только bash — тот уже резолвится абсолютным путём). Голый
# каталог символических ссылок вместо PATH тоже не универсален: на Windows
# динамический линкер ищет DLL рядом с запускаемым файлом по каталогу
# СИМЛИНКА, а не по каталогу цели — grep/git оттуда падают на "cannot open
# shared object file" (в Linux этой проблемы нет — там разрешение библиотек
# не завязано на PATH). Комбинация: safe_path (каталоги без gh) — ПЕРВЫМ,
# им покрыт случай «gh лежит отдельно от остальных инструментов» (Windows
# здесь и локальная разработка) без риска для DLL; shim (символические
# ссылки на нужные внешние команды без gh) — ВТОРЫМ, страхует случай «gh
# живёт в том же каталоге, что и остальные инструменты» (GitHub-раннер),
# где safe_path вычёркивает и их, но PATH там не нужен для разрешения
# библиотек.
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
# Не фиксированный список имён инструментов — та копия зависимостей
# task-branch неизбежно расходится с самим скриптом (реальный случай:
# #386 добавил вызов dirname через source ../gh/infra_digest.sh, список
# его не знал, случай 7 упал). Вместо списка — зеркалим КАЖДЫЙ каталог,
# в котором нашёлся gh/gh.exe, целиком (кроме самого gh): любой инструмент,
# который живёт рядом с gh на GitHub-раннере (общий /usr/bin), окажется
# в шиме автоматически, а инструменты из каталогов без gh уже целы в
# safe_path и шима не требуют.
shim="$WORK/shim-no-gh"
mkdir -p "$shim"
for _d in "${_dirs[@]}"; do
  [ -n "$_d" ] || continue
  if [ -e "$_d/gh" ] || [ -e "$_d/gh.exe" ]; then
    for entry in "$_d"/*; do
      [ -e "$entry" ] || [ -L "$entry" ] || continue
      name="$(basename "$entry")"
      case "$name" in
        gh|gh.exe) continue ;;
      esac
      [ -e "$shim/$name" ] || ln -sf "$entry" "$shim/$name" 2>/dev/null || true
    done
  fi
done

make_tree "case7"
if (
  cd "$WORK/case7"
  unset GITHUB_ACTIONS
  PATH="$safe_path:$shim" "$bash_bin" "$SCRIPT_SRC" "67-no-gh"
) 2>"$WORK/case7.stderr"; then
  note "случай 7 (gh не найден): скрипт создал ветку — ОШИБКА, ожидался отказ гвардии эпика (офлайн-режима нет)"
  fail=1
else
  branch=$(git -C "$WORK/case7" branch --show-current)
  [ "$branch" = "main" ] || { note "случай 7: ветка всё же переключена на «$branch» — ОШИБКА"; fail=1; }
  grep -q "ПРЕДУПРЕЖДЕНИЕ" "$WORK/case7.stderr" || { note "случай 7: нет предупреждения проверки «открыта/метки» — ОШИБКА"; fail=1; }
  grep -q "epic_guard" "$WORK/case7.stderr" || { note "случай 7: нет громкого отказа гвардии эпика — ОШИБКА"; fail=1; }
  if [ "$fail" = 0 ]; then
    note "случай 7 (gh не найден): предупреждение по задаче + громкий отказ гвардии эпика — ОК"
  fi
fi

# ── рабочее дерево (#332): отдельные синтетические origin ───────────────────
# origin — локальный путь, не github-вид URL, поэтому проверка задачи в
# task-branch не резолвит настоящий owner/repo и всегда уходит в ветку
# «предупреждение, не отказ» — НО только если сам gh не сможет ответить.
# Фейковый gh из $WORK/bin (сделан для случаев 1-7) переиспользуется здесь:
# для номеров 99/55 он не знает маршрута и вернёт «неизвестный маршрут»
# (не HTTP 404) — предохраняет тест от НАСТОЯЩЕГО gh на PATH хоста, который
# иначе бы дошёл до api.github.com и мог вернуть подлинный 404 на несуществующий
# путь, ложно провалив случай (плавающий тест, воспроизведено локально).
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

# ── случай 8+9: вне CI — заводит и переиспользует worktree ──────────────────
# Скрипт зовётся по $SCRIPT_SRC (не копией): после #386 он тянет соседей
# (../lib/lease.sh, ../lib/epic_guard.py, ../gh/infra_digest.sh) относительно
# СЕБЯ — копия в синтетическом клоне осталась бы без них. Git-операции при
# этом идут в синтетический клон (cwd) и через insteadOf — в локальный bare;
# номера задач (67) взяты из маршрутов фейкового gh: epic guard (#376) на
# неизвестном маршруте отказывает громко, и случай 8 падал бы не по делу.
new_origin "a"
git clone -q --branch main "$WORK/a-origin.git" "$WORK/a-main" 2>/dev/null

(cd "$WORK/a-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-demo-slug) \
  >"$WORK/a-run1.out" 2>&1 || { fail=1; note "run1 упал:"; cat "$WORK/a-run1.out"; }

# Путь берём из самого git (worktree list), а не собираем строкой сами: на
# Windows/Cygwin git печатает диск-стиль (C:/Users/...), а $WORK — POSIX-стиль
# (/tmp/...) — один и тот же каталог, разные строки одного пути.
expected_wt=$(
  git -C "$WORK/a-main" worktree list --porcelain \
    | awk '/^worktree /{p=$2} /^branch refs\/heads\/agent\/67-demo-slug$/{print p}'
)
if [ -n "$expected_wt" ] && [ -d "$expected_wt" ]; then
  note "случай 8 (вне CI, новая задача): worktree заведён на agent/67-demo-slug — ОК"
else
  note "случай 8 (вне CI, новая задача): worktree НЕ заведён — ОШИБКА"
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
(cd "$WORK/a-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-demo-slug) \
  >"$WORK/a-run2.out" 2>&1 || { fail=1; note "run2 (повтор) упал:"; cat "$WORK/a-run2.out"; }
after_count=$(git -C "$WORK/a-main" worktree list --porcelain | grep -c '^worktree ')
if [ "$before_count" = "$after_count" ] && grep -qF "$expected_wt" "$WORK/a-run2.out"; then
  note "случай 9 (повтор на ту же задачу): дерево переиспользовано, дубля нет — ОК"
else
  note "случай 9 (повтор на ту же задачу): дубль дерева или путь не сообщён — ОШИБКА"
  fail=1
fi

# ── случай 10: CI — старое поведение (переключение в текущем каталоге) ──────
# LEASE_ALREADY_CLAIMED=61 — транспорт арендует задачу сам и пробрасывает номер
# (#356): здесь этим же путём проверяется, что claim под флагом не повторяется.
new_origin "b"
git clone -q --branch main "$WORK/b-origin.git" "$WORK/b-main" 2>/dev/null
(cd "$WORK/b-main" && GITHUB_ACTIONS=true LEASE_ALREADY_CLAIMED=61 PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 61-ci-demo) \
  >"$WORK/b-run.out" 2>&1 || { fail=1; note "CI-прогон упал:"; cat "$WORK/b-run.out"; }
ci_branch=$(git -C "$WORK/b-main" rev-parse --abbrev-ref HEAD)
if [ "$ci_branch" = "agent/61-ci-demo" ] && [ ! -d "$WORK/b-main/.claude/worktrees" ]; then
  note "случай 10 (CI): ветка переключена в текущем каталоге, worktree не заведён — ОК"
else
  note "случай 10 (CI): ожидалось старое поведение (ветка $ci_branch, worktree отсутствует) — ОШИБКА"
  fail=1
fi

# ── случай 11: после CI-переключения коммит настоящим хуком обязан пройти ───
# Скрипт зовётся по $SCRIPT_SRC (не копией): эпик-гвардия (#376) и lease.sh
# тянут соседей относительно СЕБЯ; сам ХУК при этом копируется в c-main —
# смысл случая в том, что коммит идёт под НАСТОЯЩИМ файлом гвардии. Номер 61 —
# из маршрутов фейкового gh: на неизвестном маршруте эпик-гвардия отказала бы
# громко ещё до переключения ветки.
new_origin "c"
git clone -q --branch main "$WORK/c-origin.git" "$WORK/c-main" 2>/dev/null
mkdir -p "$WORK/c-main/.githooks"
cp "$HOOK_SRC" "$WORK/c-main/.githooks/pre-commit"
chmod +x "$WORK/c-main/.githooks/pre-commit"
(cd "$WORK/c-main" && GITHUB_ACTIONS=true PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 61-ci-commit) \
  >"$WORK/c-run.out" 2>&1 || { fail=1; note "случай 11: CI-переключение упало:"; cat "$WORK/c-run.out"; }
(
  cd "$WORK/c-main"
  git config user.email test@example.com
  git config user.name test
  echo "change" >>note.txt
  git add note.txt
  GITHUB_ACTIONS=true git commit -q -m "ci commit after task-branch"
) >"$WORK/c-commit.out" 2>"$WORK/c-commit.stderr"
if [ -f "$WORK/c-commit.out" ] && grep -qF "ci commit after task-branch" <(git -C "$WORK/c-main" log -1 --format=%s 2>/dev/null); then
  note "случай 11 (коммит после CI-переключения настоящим хуком): прошёл — ОК"
else
  note "случай 11 (коммит после CI-переключения настоящим хуком): ОТКЛОНЁН — ОШИБКА"
  cat "$WORK/c-commit.stderr" >&2
  fail=1
fi

# ── сверка с origin/<branch> (находка ревью #333): ветку мог подвинуть другой ──
# канал — оркестратор снимает исполнителя с конфликтного PR и диспатчит нового
# воркера, локальная голова отстаёт от серверной. Гвардия свежести сверяет
# только origin/main и этот стык не видит, коммит поверх чужой головы валился
# бы уже на пуше — отказ обязан прозвучать ДО работы в дереве.
# Мутация: сними сверку branch_diverged_from_origin в scripts/git/task-branch
# (пути переиспользования и усыновления) — случаи 12/13 перестают отклоняться,
# тест красный. Верни — снова зелёный. Случай 14 страхует обратное: свои
# незапушенные коммиты (origin — предок локальной головы) отсекаться не должны.
foreign_move() {
  local clone="$1" branch="$2"
  git clone -q "$WORK/$3" "$clone" 2>/dev/null
  (
    cd "$clone"
    git config user.email rival@example.com
    git config user.name rival
    git checkout -q -b "$branch" "origin/$branch"
    echo "rival move" >rival.txt
    git add rival.txt
    git commit -q -m "rival move"
    git push -q origin "$branch"
  )
}

# ── случай 12: подвинутая ветка — переиспользование отказывает ──────────────
new_origin "d"
git clone -q --branch main "$WORK/d-origin.git" "$WORK/d-main" 2>/dev/null
(cd "$WORK/d-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-sync-reuse) \
  >"$WORK/d-run1.out" 2>&1 || { fail=1; note "случай 12: run1 упал:"; cat "$WORK/d-run1.out"; }
git -C "$WORK/d-main" push -q origin agent/67-sync-reuse
foreign_move "$WORK/d-rival" agent/67-sync-reuse "d-origin.git"
git -C "$WORK/d-main" fetch -q origin
if (cd "$WORK/d-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-sync-reuse) \
  >"$WORK/d-run2.out" 2>"$WORK/d-run2.stderr"; then
  note "случай 12 (ветка подвинута другим каналом): переиспользование прошло — ОШИБКА, ожидался отказ"
  fail=1
else
  case "$(cat "$WORK/d-run2.stderr")" in
    *"разошлась"*) note "случай 12 (ветка подвинута другим каналом): отказ с командой синхронизации — ОК" ;;
    *) note "случай 12: отказ без внятной причины — ОШИБКА ($(cat "$WORK/d-run2.stderr"))"; fail=1 ;;
  esac
fi

# ── случай 13: та же сверка на пути усыновления (дерево пропало с диска) ────
new_origin "e"
git clone -q --branch main "$WORK/e-origin.git" "$WORK/e-main" 2>/dev/null
(cd "$WORK/e-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-sync-adopt) \
  >"$WORK/e-run1.out" 2>&1 || { fail=1; note "случай 13: run1 упал:"; cat "$WORK/e-run1.out"; }
git -C "$WORK/e-main" push -q origin agent/67-sync-adopt
foreign_move "$WORK/e-rival" agent/67-sync-adopt "e-origin.git"
# Дерево стёрто мимо `git worktree remove`: админ-запись чистится prune'ом
# внутри скрипта, ветка остаётся — дальше путь усыновления.
rm -rf "$WORK/e-main/.claude/worktrees/67-sync-adopt"
git -C "$WORK/e-main" worktree prune
git -C "$WORK/e-main" fetch -q origin
if (cd "$WORK/e-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-sync-adopt) \
  >"$WORK/e-run2.out" 2>"$WORK/e-run2.stderr"; then
  note "случай 13 (усыновление подвинутой ветки): прошло — ОШИБКА, ожидался отказ"
  fail=1
else
  case "$(cat "$WORK/e-run2.stderr")" in
    *"разошлась"*) note "случай 13 (усыновление подвинутой ветки): отказ с командой синхронизации — ОК" ;;
    *) note "случай 13: отказ без внятной причины — ОШИБКА ($(cat "$WORK/e-run2.stderr"))"; fail=1 ;;
  esac
fi

# ── случай 14: свои незапушенные коммиты — переиспользование работает ───────
new_origin "f"
git clone -q --branch main "$WORK/f-origin.git" "$WORK/f-main" 2>/dev/null
(cd "$WORK/f-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-ahead) \
  >"$WORK/f-run1.out" 2>&1 || { fail=1; note "случай 14: run1 упал:"; cat "$WORK/f-run1.out"; }
ahead_wt=$(git -C "$WORK/f-main" worktree list --porcelain \
  | awk '/^worktree /{p=$2} /^branch refs\/heads\/agent\/67-ahead$/{print p}')
(
  cd "$ahead_wt"
  git config user.email test@example.com
  git config user.name test
  echo "own work" >own.txt
  git add own.txt
  env -u GITHUB_ACTIONS git commit -q -m "own unpushed work"
)
if (cd "$WORK/f-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-ahead) \
  >"$WORK/f-run2.out" 2>&1 && grep -qF "$ahead_wt" "$WORK/f-run2.out"; then
  note "случай 14 (свои незапушенные коммиты): дерево переиспользовано, путь напечатан — ОК"
else
  note "случай 14 (свои незапушенные коммиты): переиспользование отказало или путь потерян — ОШИБКА"
  cat "$WORK/f-run2.out" 2>/dev/null
  fail=1
fi

# ── случай 15: ветка только на origin — усыновление серверной головы ────────
# Чужой PR уже запушен, локальной ветки и дерева нет: молча завести
# одноимённую локальную ветку от origin/main значило бы тихо подменить чужую
# работу (находка ревью #333, живое репро). task-branch обязан усыновить
# серверную голову и напомнить про ручную арену доводки.
# Мутация: сними проверку `refs/remotes/origin/$branch` в scripts/git/task-branch
# (не-CI fall-through) — случай 15 перестаёт падать на сверке голов, тест красный.
new_origin "g"
git clone -q --branch main "$WORK/g-origin.git" "$WORK/g-main" 2>/dev/null
git clone -q "$WORK/g-origin.git" "$WORK/g-first" 2>/dev/null
(
  cd "$WORK/g-first"
  git config user.email rival@example.com
  git config user.name rival
  git checkout -q -b agent/67-origin-adopt
  echo "pushed work" >pushed.txt
  git add pushed.txt
  git commit -q -m "pushed work"
  git push -q origin agent/67-origin-adopt
)
remote_head=$(git -C "$WORK/g-main" ls-remote origin refs/heads/agent/67-origin-adopt | cut -f1)
if (cd "$WORK/g-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-origin-adopt) \
  >"$WORK/g-run.out" 2>"$WORK/g-run.stderr"; then
  adopt_wt=$(git -C "$WORK/g-main" worktree list --porcelain \
    | awk '/^worktree /{p=$2} /^branch refs\/heads\/agent\/67-origin-adopt$/{print p}')
  adopt_head=$(git -C "$WORK/g-main" rev-parse refs/heads/agent/67-origin-adopt 2>/dev/null || true)
  if [ -n "$adopt_wt" ] && [ "$adopt_head" = "$remote_head" ] \
     && git -C "$adopt_wt" ls-files --error-unmatch pushed.txt >/dev/null 2>&1 \
     && grep -qF "$adopt_wt" "$WORK/g-run.out"; then
    note "случай 15 (ветка только на origin): серверная голова усыновлена, путь напечатан — ОК"
  else
    note "случай 15: дерево не от серверной головы, чужой файл потерян или путь не напечатан — ОШИБКА"
    fail=1
  fi
  grep -q "аренду здесь task-branch не берёт" "$WORK/g-run.stderr" \
    || { note "случай 15: нет напоминания про ручную аренду доводки — ОШИБКА"; fail=1; }
else
  note "случай 15 (ветка только на origin): task-branch отказал — ОШИБКА, ожидалось усыновление"
  cat "$WORK/g-run.stderr" >&2
  fail=1
fi

# ── случай 16: легаси-имя дерева — отказ с переносом, не вечный цикл ────────
# Дерево «246-legacy» закреплено за веткой agent/61-x: путь переиспользования,
# не сверяющий имя с правилом <N>-, замыкает цикл «гвардия шлёт в task-branch,
# task-branch печатает то же дерево, гвардия снова отклоняет» (находка ревью
# #333, раунд 4, репро на состоянии из задачи: 246-lease-claim / agent/121).
# Мутация: сними сверку имени в пути переиспользования — случай 16 перестаёт
# отклоняться, тест красный.
new_origin "h"
git clone -q --branch main "$WORK/h-origin.git" "$WORK/h-main" 2>/dev/null
mkdir -p "$WORK/h-main/.claude/worktrees"
git -C "$WORK/h-main" worktree add -q -b agent/61-x "$WORK/h-main/.claude/worktrees/246-legacy" origin/main
if (cd "$WORK/h-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 61-x) \
  >"$WORK/h-run.out" 2>"$WORK/h-run.stderr"; then
  note "случай 16 (легаси-имя дерева): путь напечатан — ОШИБКА, ожидался отказ с командой переноса"
  fail=1
else
  case "$(cat "$WORK/h-run.stderr")" in
    *"не начинается с «61-»"*"worktree move"*) note "случай 16 (легаси-имя дерева): отказ с рабочей командой git worktree move — ОК" ;;
    *) note "случай 16: отказ без внятной причины или без команды переноса — ОШИБКА ($(cat "$WORK/h-run.stderr"))"; fail=1 ;;
  esac
fi

# ── случай 17: путь дерева занят — отказ ДО claim'а, без осиротевшей ветки ──
# Канонический путь .claude/worktrees/61-occupied занят деревом на чужой
# ветке (легаси-имя с числовым префиксом делает такую коллизию вероятной),
# ветки agent/61-occupied нет нигде: падение сквозь claim на сыром
# `git worktree add` захватывало аренду и оставляло висеть свежесозданную
# ветку (класс #360; находка ревью #333, раунд 4, живое репро). Мутация:
# сними проверку [ -e "$wt_dir" ] — ветка agent/61-occupied появляется,
# тест красный.
new_origin "i"
git clone -q --branch main "$WORK/i-origin.git" "$WORK/i-main" 2>/dev/null
mkdir -p "$WORK/i-main/.claude/worktrees"
git -C "$WORK/i-main" worktree add -q -b agent/121-other "$WORK/i-main/.claude/worktrees/61-occupied" origin/main
(cd "$WORK/i-main" && env -u GITHUB_ACTIONS -u LEASE_ALREADY_CLAIMED PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 61-occupied) \
  >"$WORK/i-run.out" 2>"$WORK/i-run.stderr" \
  && { note "случай 17 (путь занят): скрипт прошёл — ОШИБКА, ожидался отказ"; fail=1; }
case "$(cat "$WORK/i-run.stderr")" in
  *"занят (не пуст)"*) note "случай 17 (путь занят): отказ с объяснением и лечением — ОК" ;;
  *) note "случай 17: отказ без внятной причины — ОШИБКА ($(cat "$WORK/i-run.stderr"))"; fail=1 ;;
esac
if git -C "$WORK/i-main" show-ref --verify -q refs/heads/agent/61-occupied; then
  note "случай 17: ветка всё же создана (осиротевшая, класс #360) — ОШИБКА"
  fail=1
else
  note "случай 17: ветка не создавалась — осиротевших не осталось — ОК"
fi

if [ "$fail" = 0 ]; then
  echo "task-branch: все случаи входной проверки, работы с worktree, стыка с гвардией и сверки с origin прошли как ожидалось"
fi
exit "$fail"
