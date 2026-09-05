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
shim="$WORK/shim-no-gh"
mkdir -p "$shim"
for tool in git grep sed cut mktemp cat rm; do
  tool_path="$(command -v "$tool" 2>/dev/null || true)"
  [ -n "$tool_path" ] || { note "случай 7: инструмент $tool не найден в окружении теста — ОШИБКА"; fail=1; continue; }
  ln -sf "$tool_path" "$shim/$tool"
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

if [ "$fail" = 0 ]; then
  echo "task-branch: все случаи входной проверки прошли как ожидалось"
fi
exit "$fail"
