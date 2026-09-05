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
#       покрывает никто.
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
    env -u GITHUB_ACTIONS PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" "$task"
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

(cd "$WORK/a-main" && env -u GITHUB_ACTIONS PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-demo-slug) \
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
(cd "$WORK/a-main" && env -u GITHUB_ACTIONS PATH="$WORK/bin:$PATH" bash "$SCRIPT_SRC" 67-demo-slug) \
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

if [ "$fail" = 0 ]; then
  echo "task-branch: все случаи входной проверки, работы с worktree и стыка с гвардией прошли как ожидалось"
fi
exit "$fail"
