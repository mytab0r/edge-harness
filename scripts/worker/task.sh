#!/usr/bin/env bash
# Автономный воркер (задача #89): воплощение docs/agents/WORKER-PLAYBOOK.md.
# Здесь ТОЛЬКО транспорт и отчётность: выбрать свободную задачу из пула, взять
# её в атомарную аренду (claim_task, #121), создать ветку agent/N-slug (либо,
# если на задачу уже открыт PR без исполнителя — #245, довести его: чекаут
# существующей ветки, без второй ветки/PR), накормить DSH headless промптом
# (тело задачи + playbook + критерий), проверить результат (открытый PR) и
# отчитаться (комментарий в задачу + Telegram). Работу над задачей делает
# DSH — этот скрипт за него ничего не решает и не пишет.
#
# Ретрай временного RATE_LIMIT провайдера (#422, механизм #419/#421 —
# dsh_run_with_retry в lib/dsh-ci.sh): живой факт — прогоны worker.yml
# 34007508064/34006554580 упали с «dsh: RATE_LIMIT: Rate limit reached for
# requests», раньше, чем ретрай вообще появился (тогда — только у ai-review).
# Бюджет ожидания WORKER_RATE_LIMIT_MAX_WAIT_SECS (по умолчанию 1800с/30 мин —
# та же длительность короткого окна провайдера, что и у ai-review: она не
# зависит от вызывающего канала) подобран под ОДИН прогон DSH_TIMEOUT_SECS
# (150 мин): реалистичный случай — первая попытка падает СРАЗУ (провайдер
# отклоняет самый первый вызов модели), не после долгой работы, поэтому
# бюджета хватает без риска упереться в 6-часовой потолок job'а (см.
# worker.yml timeout-minutes). Патологический случай «упало после 140 минут
# работы» теоретически возможен и не решён здесь (тот же непокрытый класс уже
# принят в #421 для ai-review) — задокументирован, не тихо проигнорирован.
# quota_exhausted (недельная/месячная квота) и rate_limit_retry_budget_exceeded
# (бюджет короткого окна кончился) — обе причины возвращают задачу в пул СРАЗУ
# (lease_cli release-full, #422), не дожидаясь 24-часового TTL-сборщика: вина
# не в задаче, держать assignee до таймера — зря прятать её от других каналов.
#
# Использование:
#   task.sh               — выбрать свободную задачу из пула и выполнить
#   task.sh --task 89     — выполнить конкретную задачу (если она открыта и свободна)
#   task.sh --dry-run     — самотест: напечатать выбранную задачу и промпт,
#                           ничего не назначая, не запуская и не отправляя
#
# Итог запуска: PR открыт → job зелёный; эскалация (метка blocked) → зелёный;
# провайдер в лимите/квоте надолго → job красный, но задача честно возвращена
# в пул (не «воркер не справился» — вина не его); иначе (нет PR, реальный
# сбой) → job красный. Нет свободных задач → зелёный без действий.
set -euo pipefail

die() { echo "::error::$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины DSH, integrity, GLM-патч профиля, redact — единственное место правды в lib.
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"
# Шов сессии раннера в морду (#119): логин, begin, дрен спула в ingest.
# shellcheck source=scripts/lib/dsh-edge-session.sh
source "$SCRIPT_DIR/../lib/dsh-edge-session.sh"
# Аренда задачи (#121): claim/release/locks — единственный вход в работу.
# shellcheck source=scripts/lib/lease.sh
source "$SCRIPT_DIR/../lib/lease.sh"

WORKER_LOGIN="${WORKER_LOGIN:?WORKER_LOGIN не задан (логин, под которым воркер берёт задачи)}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-9000}"   # 150 минут на прогон DSH
# Бюджет ретрая временного RATE_LIMIT (#422) — суммарно, не на попытку;
# обоснование значений — комментарий в шапке файла.
WORKER_RATE_LIMIT_MAX_WAIT_SECS="${WORKER_RATE_LIMIT_MAX_WAIT_SECS:-1800}"
WORKER_RATE_LIMIT_INITIAL_DELAY_SECS="${WORKER_RATE_LIMIT_INITIAL_DELAY_SECS:-30}"
WORKER_RATE_LIMIT_MAX_DELAY_SECS="${WORKER_RATE_LIMIT_MAX_DELAY_SECS:-300}"
# Профиль headless — pnpm-workspace: инициализация делает pnpm add в корень
# профиля. Без этого флага — ERR_PNPM_ADDING_TO_ROOT (#93/#94); фикс обязан
# быть в ОБЕИХ транспортных обёртках (worker task.sh и hands dsh_task.sh):
# выпадение из одной возвращает класс — прогон 2026-08-31 15:57 на #131.
export npm_config_ignore_workspace_root_check=true
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY не задан}"
export GH_REPO="${GH_REPO:-$GITHUB_REPOSITORY}"

WORK="${RUNNER_TEMP:-/tmp}/dsh-worker"
mkdir -p "$WORK"
ANSWER_FILE="$WORK/answer.txt"
ERR_FILE="$WORK/stderr.txt"
PROMPT_FILE="$WORK/prompt.md"
: >"$ANSWER_FILE"; : >"$ERR_FILE"

DRY_RUN=0
TASK_INPUT="${WORKER_TASK:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --task)
      shift
      [ -n "${1:-}" ] || die "--task требует номер задачи"
      TASK_INPUT=$1
      ;;
    *) die "неизвестный аргумент: $1 (допустимы --task N и --dry-run)" ;;
  esac
  shift
done
if [ -n "$TASK_INPUT" ]; then
  case "$TASK_INPUT" in *[!0-9]*) die "номер задачи должен быть числом: '$TASK_INPUT'" ;; esac
fi

# Одно место правды — vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория (#153):
# зашитых фолбэков на конкретный эндпоинт/модель здесь больше нет. Проверяем
# раньше дупгарда/назначения/ветки/сессии морды — падать сразу, а не после
# дорогой подготовительной работы. Пропускаем при --dry-run: самотест печатает
# выбор и промпт без реального вызова модели, требовать ключ здесь незачем.
if [ "$DRY_RUN" != "1" ]; then
  dsh_require_provider_env || die "провайдер не сконфигурирован (см. ::error:: выше)"
fi

# Гвардия дублей прогонов: если живёт ДРУГОЙ прогон воркера — активный или
# ожидающий в очереди concurrency-группы — выходим зелёным no-op. Очередь
# запускает прогоны последовательно, и второму прогону делать нечего; раньше
# он сжигал установку и мог столкнуться с первым на выборе задачи. Дубль
# РАБОТЫ над одной задачей (в отличие от дубля прогонов) закрывает атомарная
# аренда #121 — claim ниже; эта гвардия — про бессмысленный второй прогон.
if [ "$DRY_RUN" != "1" ] && [ -z "${WORKER_SKIP_DUPGUARD:-}" ]; then
  others=""
  for state in "in_progress" "queued"; do
    others="$others$(gh run list --workflow=worker.yml --status "$state" \
      --json databaseId -q '[.[].databaseId] | join(" ")' 2>/dev/null || true)"
  done
  mine="${GITHUB_RUN_ID:-0}"
  dup=""
  for id in $others; do
    [ "$id" = "$mine" ] && continue
    dup="$dup$id "
  done
  if [ -n "$dup" ]; then
    echo "Живёт другой прогон воркера (id: $dup) — выхожу no-op, чтобы не делать ту же работу дважды (#121)."
    exit 0
  fi
fi

# Отчёт в Telegram — best-effort: место правды всегда комментарий в задаче,
# но промах кричит warning'ом в лог job'а, не молчит.
# parse_mode=HTML — всегда (#170): без него Telegram рендерит plain text и
# кликабельных ссылок не бывает. Второй отправитель репозитория —
# pulse_guard.send_telegram; новых отправителей заводить нельзя, формат один.
telegram_report() { # $1 — текст (динамические части — уже через tg_html)
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "::warning::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — Telegram-отчёт не отправлен"
    return 1
  fi
  if ! curl -fsS --max-time 30 -X POST \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "parse_mode=HTML" \
      --data-urlencode "text=$1" >/dev/null; then
    echo "::warning::Telegram не принял отчёт — комментарий в задаче остаётся местом правды"
    return 1
  fi
}

# plain-текст → безопасный внутри Telegram-HTML (#170): при parse_mode=HTML
# символы <, >, & управляющие — заголовок задачи с ними развалил бы доставку
# всего сообщения (400 от Telegram). & первым, иначе задвоение.
tg_html() { # $1 — plain-текст
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

# «Заголовок задачи в двух словах» (#170): первые 6 слов, остальное молча
# отбрасывается — сообщение обязано остаться коротким. Строго по пробелам,
# не по позициям: байтовая обрезка режет кириллицу посреди символа в C-локали.
short_title() { # $1 — заголовок
  printf '%s' "$1" | tr -s '[:space:]' ' ' | cut -d' ' -f1-6
}

# Свободная задача: открыта, метка task, без assignee, без живой аренды
# (#121) и без метки waiting:owner (#470/#471 — ждёт решения владельца, не
# должна стопорить диспатч как «старейшая свободная»). Открытый PR сам по
# себе больше НЕ исключает задачу (класс #245,
# ранее совпадал с классом #187/#195 — скан любого упоминания номера в прозе
# PR вместо декларации): единственное место правды на критерий свободы —
# python scripts/lib/free_task.py, чтобы bash не заводил третью расходящуюся
# копию regex рядом с task_ref.py и contract_check.py. Печатает
# «номер<TAB>заголовок» ПРИОРИТЕТНОЙ свободной задачи (находка ревью PR #367:
# с #361 это не обязательно старейшая по номеру — приоритет мета → блокирует
# открытых → номер, `free_task.py::issue_priority_key`; свежая по номеру
# задача может обойти старую, если у неё выше приоритет).
# Коды: 0 — нашла; 1 — пул пуст; 2 — сломался инструмент (gh/python/сеть):
# «пусто» и «сломано» — разные состояния, смешивать запрещено (fail loud).
free_task() {
  local issues_file locked excluded line rc
  issues_file="$WORK/pool-issues.json"
  # Пул — через task_deps.py (GraphQL), не `gh issue list` (REST): приоритет
  # #361 читает граф блокировок (blockedBy/blocking), которого REST не отдаёт
  # ни в каком виде (проверено живым запросом, design.md этого change) —
  # одно место получения пула, не два расходящихся запроса. Отдаёт и labels
  # (находка AI-ревью PR #471/#470 — waiting:owner), и blocking_open.
  python3 "$SCRIPT_DIR/../lib/task_deps.py" pool "$GITHUB_REPOSITORY" \
    >"$issues_file" || return 2
  # Замок (в том числе ещё не собранный протухший) = задачу уже взял другой
  # канал (#121). Фильтр здесь — экономия прогона, НЕ защита: гарантией
  # остаётся атомарный claim ниже. Сломался список замков — сломан инструмент
  # (2), а не «пул пуст» (1).
  locked=$(lease_cli locks) || return 2
  # Задача, чей объявленный PR в конфликте, исключена из ОБЩЕГО выбора
  # (находка ревью PR #478): её доводка — только адресно, через
  # scheduler.py::dispatch_conflict_rework (бюджет РОВНО одна попытка +
  # эскалация). Без исключения generic-пульс мог бы взять её мимо этого
  # бюджета, если адресный прогон освободил задачу (упал по квоте/крашу) до
  # того, как снова её занял. $WORK/pool-prs.json читается один раз выше
  # (шаг 0), не второй HTTP-обход.
  excluded=$(python3 "$SCRIPT_DIR/../lib/free_task.py" conflict-tasks "$WORK/pool-prs.json") || return 2
  # oldest-free сам считает приоритет (#361: мета-метка → число блокируемых
  # открытых задач → номер issue как тайбрейк) и фильтрует замки из locked и
  # конфликтные задачи из excluded.
  set +e
  line=$(python3 "$SCRIPT_DIR/../lib/free_task.py" oldest-free "$issues_file" "$locked" "$excluded")
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then printf '%s\n' "$line"; return 0; fi
  if [ "$rc" -eq 1 ]; then return 1; fi
  return 2
}

# ── 0. Открытые PR — читаются ОДИН раз, используются и здесь (free_task —
# исключение конфликтных задач из общего выбора, #478), и на шаге 1b ниже
# (декларация PR текущей задачи) — второй HTTP-обход не заводится. `labels`
# нужен именно для конфликтного фильтра — до #478 запрос не нёс его вовсе.
gh pr list --state open --limit 100 --json number,body,headRefName,labels \
  >"$WORK/pool-prs.json" || die "не смог прочитать открытые PR (gh/jq/сеть)"

# ── 1. Выбор задачи ────────────────────────────────────────────────────────────────
if [ -n "$TASK_INPUT" ]; then
  ISSUE_JSON=$(gh issue view "$TASK_INPUT" --json number,title,body,state,assignees,labels) \
    || die "Задача #$TASK_INPUT не читается"
  number=$(jq -r '.number' <<<"$ISSUE_JSON")
  [ "$(jq -r '.state' <<<"$ISSUE_JSON")" = "OPEN" ] || die "Задача #$number закрыта"
  jq -e '.labels[]? | select(.name == "task")' <<<"$ISSUE_JSON" >/dev/null \
    || die "На задаче #$number нет метки task — это не задача пула"
  # Проверка «занята не воркером по assignee» удалена (#121): все агенты — один
  # логин, она не могла сработать никогда. Занятость видит атомарный claim.
  # Открытый PR на задачу больше не отклоняется здесь (#245) — символично с
  # авто-выбором, см. шаг 1b: декларирующий PR без исполнителя ведёт к доводке.
else
  free_rc=0
  ISSUE_LINE=$(free_task) || free_rc=$?
  if [ "$free_rc" -eq 1 ]; then
    echo "Свободных задач нет — воркеру нечего делать, job зелёный"
    exit 0
  fi
  [ "$free_rc" -eq 0 ] || die "выбор свободной задачи сломался (gh/python/сеть), rc=$free_rc"
  number=${ISSUE_LINE%%$'\t'*}
  ISSUE_JSON=$(gh issue view "$number" --json number,title,body,state,assignees,labels) \
    || die "Задача #$number исчезла между выбором и чтением"
fi
title=$(jq -r '.title' <<<"$ISSUE_JSON")
body=$(jq -r '.body // ""' <<<"$ISSUE_JSON")
echo "Задача #$number: $title"

# ── 1b. Режим: новая ветка или доводка существующего PR (#245) ────────────────────
# Открытый PR больше не блокирует выбор задачи и не отказывает молча: задача
# без исполнителя, но с уже открытым PR — сигнал «довести» (сценарий
# scheduler.py::unhealthy_pulls, снявшего исполнителя с нездорового PR), не
# «пропустить». Одно место правды на объявление PR задачи —
# scripts/lib/task_ref.py::task_from_branch (имя agent-ветки, единственный
# источник, решение владельца 2026-09-06) через scripts/lib/free_task.py,
# то же самое, что использует contract_check.py — симметрично для явного
# входа --task и для авто-выбора free_task(). Атомарная защита от гонки
# каналов на этот же PR — claim ниже (шаг 4), не эта проверка.
# $WORK/pool-prs.json уже прочитан на шаге 0 — второй HTTP-обход не нужен.
set +e
pr_line=$(python3 "$SCRIPT_DIR/../lib/free_task.py" declared-pr "$number" "$WORK/pool-prs.json")
pr_rc=$?
set -e
if [ "$pr_rc" -ne 0 ] && [ "$pr_rc" -ne 1 ]; then
  die "проверка открытого PR задачи #$number сломалась (gh/python/сеть)"
fi
CONTINUE_PR_NUMBER=""
CONTINUE_HEAD_REF=""
if [ "$pr_rc" -eq 0 ]; then
  CONTINUE_PR_NUMBER=${pr_line%%$'\t'*}
  CONTINUE_HEAD_REF=${pr_line#*$'\t'}
  [ -n "$CONTINUE_HEAD_REF" ] \
    || die "PR #$CONTINUE_PR_NUMBER объявляет задачу #$number, но headRefName пуст — доводить нечего"
  echo "На задачу #$number уже открыт PR #$CONTINUE_PR_NUMBER (ветка $CONTINUE_HEAD_REF) — довожу его, новый не открываю"
fi

# ── 2. Промпт: тело задачи + критерий + playbook + маршрут протокола ──────────────
PLAYBOOK_FILE="$SCRIPT_DIR/../../docs/agents/WORKER-PLAYBOOK.md"
[ -f "$PLAYBOOK_FILE" ] || die "Нет docs/agents/WORKER-PLAYBOOK.md — воркер без playbook не работает"
criterion=$(awk '
  /^#{1,6}[[:space:]]*Критерий готовности/ {flag = 1; next}
  /^#{1,6}[[:space:]]/ {flag = 0}
  flag {print}
' <<<"$body")
[ -n "$criterion" ] || criterion="(в теле задачи нет отдельного раздела «Критерий готовности» — критерий ищи в тексте задачи выше)"

slug=$(printf '%s' "$title" | tr '[:upper:]' '[:lower:]' | tr -cs 'A-Za-z0-9' '-' \
  | sed -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' | cut -c1-30)
[ -n "$slug" ] || slug=worker

# Ветка и текст маршрута зависят от режима (1b): доводка существующего PR
# получает свою ветку и запрет на второй PR, новая задача — прежний текст.
if [ -n "$CONTINUE_PR_NUMBER" ]; then
  BRANCH="$CONTINUE_HEAD_REF"
  route_intro="Текущий каталог — корень клона репозитория. На задачу #$number уже открыт PR #$CONTINUE_PR_NUMBER на ветке $BRANCH — шаг ниже уже сделал checkout именно на неё, новую ветку НЕ создавай и не переключайся с неё. git и gh авторизованы под учёткой владельца ($WORKER_LOGIN): git push, комментарии в задачах и работа с PR работают. Прямой пуш в main отклоняется молча — пушь именно $BRANCH и проверяй результат push без -q."
  route_pr_step="PR #$CONTINUE_PR_NUMBER уже открыт на эту задачу — НЕ открывай второй, контракт его отклонит. Прочитай gh pr view $CONTINUE_PR_NUMBER --comments и последний вердикт (метка ai:changes-requested / красный обязательный чек), исправь по нему и запушь в ту же ветку $BRANCH (git push — ветка уже отслеживает origin, -u не нужен)."
else
  BRANCH="agent/$number-$slug"
  route_intro="Текущий каталог — корень клона репозитория. Ты уже на ветке $BRANCH, созданной от свежего origin/main; НЕ переключай и не пересоздавай ветку. git и gh авторизованы под учёткой владельца ($WORKER_LOGIN): git push, комментарии в задачах и создание PR работают. Прямой пуш в main отклоняется молча — пушь ветку и проверяй результат push без -q."
  route_pr_step="Открой PR в main: scripts/git/pr-create --base main --head $BRANCH --title \"…\" --body-file <файл> (обёртка над gh pr create — тот же интерфейс, issue #496; она откажет ДО отправки, если тело нарушает правило ниже, вместо гадания по красному гейту постфактум). Первая строка тела PR — ровно \"#$number\". Разделы: Что сделано / Чем доказано (видимый результат, а не «шаг success») / Пост-мерж проверка / Чек-лист. Closes/Fixes/Resolves ЗАПРЕЩЕНЫ — контракт отклонит такой PR."
fi

{
  cat <<PROMPT
Ты — автономный воркер-агент репозитория edge-harness на одноразовом раннере GitHub Actions (Ubuntu, неинтерактивная среда). Владелец делегировал все решения: вопросов задавать некому — решай сам по правилам ниже.

$route_intro

# Задача #$number: $title

$body

# Критерий готовности

$criterion

# Твой маршрут (транспорт уже подготовлен скриптом)
1. Прочитай docs/INDEX.md и относящиеся к задаче спеки/research. Архитектурное предложение — только после docs/research/30-rejected-alternatives.md.
2. Сделай задачу: минимальный правильный дифф, тесты, саморевью (секреты, мёртвый код, расхождение доков с кодом, вызовы переименованных функций по всему репо).
3. Закоммить осмысленными коммитами (git add -A; git commit) и запушь: git push -u origin $BRANCH — проверь вывод пуша. При «main уехал» (в т.ч. метка conflict на этом PR — почти всегда механический дрейф, не содержательный конфликт) — git fetch origin main, git rebase origin/main, вручную сведи конфликтующие файлы (обе стороны), git rebase --continue. Ребейз переписывает историю ветки — обычный push после него отклоняется (non-fast-forward); пушь git push --force-with-lease (НЕ голый --force: lease проверяет, что на origin именно ожидаемый коммит, а не чья-то ещё работа поверх той же ветки).
4. $route_pr_step
5. Упёрся в то, что есть только у владельца (секрет вне хранилища, доступ, деньги, необратимое внешнее действие), — единственная эскалация: комментарий в задачу #$number (что нужно и почему не сам) + метка blocked (gh issue edit $number --add-label blocked), PR не открывай, работу останови. Это законный исход запуска.
6. Финальный ответ в stdout — краткий отчёт: ссылка на PR или причина отказа/эскалации.

Открытый PR на задачу — обязательный результат: без него запуск считается провалом воркера.

# Правила работы (обязательны; дистилляция живой практики)

PROMPT
  cat "$PLAYBOOK_FILE"
} >"$PROMPT_FILE"
echo "Промпт собран: $PROMPT_FILE ($(wc -c <"$PROMPT_FILE") байт), ветка $BRANCH"

# ── 3. Сухой прогон: печатаем выбор и промпт, ничего не трогаем ───────────────────
if [ "$DRY_RUN" -eq 1 ]; then
  echo "=== DRY-RUN: назначено ничего, запущено ничего, отправлено ничего ==="
  cat "$PROMPT_FILE"
  exit 0
fi

# ── 4. Захват задачи: атомарная аренда через claim_task (#121, ADR 0006) ─────────
# Единственный вход в работу: замок refs/locks/task-N создаётся серверно
# атомарно, проигравший гонку каналов (worker auto/manual, hands) получает
# отказ. Назначение и след в задаче делает сам claim — это видимость, НЕ
# защита: логин у всех агентов один. Отказ (rc=1) — зелёный no-op «занята»,
# поломка утилиты (rc=2) — громкая.
# CLAIM_ACTOR — назначаемый аккаунт, ОБЯЗАН быть валидным логином (иначе
# назначение отклоняется, а контракт PR требует назначенного исполнителя);
# различие каналов для следа в задаче — отдельная CLAIM_VIA.
CLAIM_ACTOR="${CLAIM_ACTOR:-$WORKER_LOGIN}"
CLAIM_VIA="worker run ${GITHUB_RUN_ID:-local}${TASK_INPUT:+, task=$TASK_INPUT}"
export CLAIM_ACTOR CLAIM_VIA
claim_out="$(lease_cli claim "$number" 2>&1)" && claim_rc=0 || claim_rc=$?
if [ "$claim_rc" -eq 1 ]; then
  echo "Задача #$number занята другим исполнителем — зелёный no-op: $claim_out"
  exit 0
fi
[ "$claim_rc" -eq 0 ] || die "claim_task сломался (rc=$claim_rc): $claim_out"
echo "Аренда взята: $claim_out"
# task-branch (шаг 5) теперь тоже умеет арендовать (#356, внешний путь) — этот
# флаг говорит ей, что claim уже наш, повторный claim того же номера тем же
# держателем иначе отклонился бы как «занято» (claim неидемпотентен по актору).
export LEASE_ALREADY_CLAIMED=1

# ── 4b. Пульс живости: пока идёт работа, журнал знает, что воркер жив ────────────
# Стопгэп наблюдаемости (#112): свежий /api/heartbeat — доказательство «агент
# работает, не висит» (тот же контракт, что у hands); после #105 это видно и в
# морде. Best-effort: промах не роняет job, но кричит warning'ом — молча-мертвый
# пульс хуже шума. Полное решение (транскрипт сессии в морде) — #119.
HB_PID=""
stop_worker_heartbeat() {
  if [ -n "$HB_PID" ]; then kill "$HB_PID" 2>/dev/null || true; fi
}
trap stop_worker_heartbeat EXIT
if [ -n "${HANDS_TOKEN:-}" ] && [ -n "${HARNESS_URL:-}" ]; then
  (
    while :; do
      sleep "${HEARTBEAT_SECS:-60}"
      curl -fsS --max-time 20 -X POST "$HARNESS_URL/api/heartbeat" \
        -H "Authorization: Bearer $HANDS_TOKEN" \
        -H "content-type: application/json" \
        -d "{\"job_id\":\"worker-${GITHUB_RUN_ID:-local}\",\"task_id\":\"issue-$number\"}" \
        >/dev/null 2>&1 || echo "::warning::heartbeat не принят журналом"
    done
  ) &
  HB_PID=$!
  echo "Пульс живости: $HARNESS_URL/api/heartbeat каждые ${HEARTBEAT_SECS:-60} с (worker-${GITHUB_RUN_ID:-local} / issue-$number)"
else
  echo "::warning::HANDS_TOKEN/HARNESS_URL не заданы — пульс живости выключен, зависание видно только по таймауту"
fi

# ── 5. Ветка: новая от свежего origin/main, либо отдельный worktree на ветке PR (#245) ──
# Trust-зона (#476, тот же принцип, что в ai-review.yml): доверенный
# инструментарий этого job'а — scripts/* в $GITHUB_WORKSPACE, куда worker.yml
# всегда чекаутит main (scheduler.py дисптатчит ровно с --ref main). Раньше
# доводка PR делала `git checkout -B` ПРЯМО в $GITHUB_WORKSPACE — это подменяло
# ВСЮ рабочую директорию, включая scripts/, деревом старой ветки PR. Любой
# файл, добавленный в scripts/ после её ответвления (например
# scripts/gh/infra_digest.sh), в этом дереве отсутствовал — живой отказ
# «No such file or directory» (прогон worker.yml 34027035455, PR #408, #476).
# Фикс — линкованный git worktree в отдельном каталоге: $SCRIPT_DIR (и всё,
# что читается по пути из main-дерева — infra_digest.sh, dsh-hands-streamer)
# остаётся main НЕЗАВИСИМО от того, какую ветку доводит DSH; ветка PR — только
# рабочее дерево, куда DSH коммитит и пушит (cd в самом конце этого блока).
if [ -n "$CONTINUE_PR_NUMBER" ]; then
  git fetch origin "$BRANCH"
  PR_WORKTREE="$WORK/pr-worktree"
  rm -rf "$PR_WORKTREE"
  git worktree add -B "$BRANCH" "$PR_WORKTREE" "origin/$BRANCH"
  git -C "$PR_WORKTREE" branch --set-upstream-to="origin/$BRANCH" "$BRANCH"
  # core.hooksPath — репозиторий-уровневый конфиг (общий .git на все worktree),
  # не per-worktree: гвардия свежести действует в $PR_WORKTREE без отдельной
  # установки. Та же гвардия, что ставит task-branch: следующий коммит на этой
  # ветке обязан быть впереди актуального origin/main, иначе pre-commit велит
  # git rebase origin/main — свежесть базы не предполагается, а доказывается.
  git config core.hooksPath .githooks
  echo "Ветка $BRANCH (PR #$CONTINUE_PR_NUMBER) выделена отдельным worktree для доводки: $(git -C "$PR_WORKTREE" rev-parse --short HEAD) ($PR_WORKTREE)"
  # Второй вход в agent-ветку (первый — task-branch ниже): доводка уже
  # открытого PR не проходит через task-branch, поэтому дайджест граблей
  # инфраструктуры печатается здесь явно — тем же общим модулем, не второй
  # копией текста (#326 находка 1: свежий агент на доводке стартовал без
  # дайджеста, хотя scripts/gh/* нужны там раньше всего). Печатается ИЗ
  # main-дерева ($SCRIPT_DIR), а не из $PR_WORKTREE — см. обоснование выше.
  source "$SCRIPT_DIR/../gh/infra_digest.sh"
  print_infra_digest
  # С этой строки и до конца скрипта cwd — рабочее дерево задачи ($PR_WORKTREE):
  # DSH коммитит и пушит именно туда. $SCRIPT_DIR — абсолютный путь, вычисленный
  # из BASH_SOURCE в начале скрипта, за cd не следует и продолжает указывать на
  # main-дерево ($GITHUB_WORKSPACE) для всех последующих обращений по пути
  # (npm pack scripts/dsh-hands-streamer и т.п., шаг 6b).
  cd "$PR_WORKTREE"
else
  "$SCRIPT_DIR/../git/task-branch" "$number-$slug"
fi

# Коммиты агента атрибутируются владельцу: noreply-адрес привязан к аккаунту.
gh_user_id=$(gh api "users/$WORKER_LOGIN" --jq .id)
git config user.name "$WORKER_LOGIN"
git config user.email "${gh_user_id}+${WORKER_LOGIN}@users.noreply.github.com"

# ── 5b. Сессия раннера в морде (#119): создать/переиспользовать и назвать ────────
# Имя сессии = «#N: название задачи», воркспейс edge-harness. Отказ громкий:
# без сессии ход работы владельцу не виден — job красный (критерий #119).
HARNESS_SID="harness-$number"
HARNESS_TITLE="#$number: $title"
dsh_edge_login || { echo "::error::Нет доступа к морде dsh-edge — job красный (#119)" >&2; exit 1; }
dsh_edge_session_begin "$HARNESS_SID" "$HARNESS_TITLE" >/dev/null \
  || { echo "::error::Сессия $HARNESS_SID не создана в морде — ход работы останется невидимым (#119)" >&2; exit 1; }
export DSH_EDGE_SESSION_ID="$HARNESS_SID"
echo "Сессия морды: $HARNESS_SID — «$HARNESS_TITLE»"

# ── 6. DSH: провайдер (проверен в начале скрипта), установка (lib), GLM-патч профиля
export DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL

dsh_install "$WORK/pkgs"
dsh --version || true
dsh_patch_profile headless

# ── 6b. Плагин стрима: спул событий сессии для морды (#119) ──────────────────────
# Тот же dsh-hands-streamer, что у рук: NDJSON-спул канонических событий,
# дрен в DSH-сессию морды ведёт scripts/lib/dsh-edge-session.sh. Сеть в плагине
# отсутствует по построению; факт монтажа доказывает --dump-config (гвардия рук).
if ! command -v pnpm >/dev/null; then
  echo "::error::pnpm не найден — dsh plugin add без него не работает, транскрипт морды невозможен (#119)" >&2
  exit 1
fi
PLUGIN_TGZ="$WORK/dsh-hands-streamer.tgz"
npm pack "$SCRIPT_DIR/../../scripts/dsh-hands-streamer" --pack-destination "$WORK" >/dev/null
mv "$WORK"/dsh-hands-streamer-*.tgz "$PLUGIN_TGZ"
dsh plugin --profile headless add "$PLUGIN_TGZ"
dsh --profile headless --dump-config >"$WORK/dump-config.txt" 2>&1 \
  || { echo "::error::dsh --dump-config упал — профиль headless не собирается" >&2; exit 1; }
grep -q '^- id: hands-streamer$' "$WORK/dump-config.txt" \
  || { echo "::error::плагин hands-streamer не смонтировался — транскрипт морды невозможен (#119)" >&2; exit 1; }

# ── 7. Прогон: cwd до старта = корень воркспейса и после не меняется (контракт dsh)
SPOOL_FILE="$WORK/session-stream.ndjson"   # NDJSON-спул плагина (дрен — lib dsh-edge-session)
rm -f "$SPOOL_FILE" "$SPOOL_FILE.stats.json"
export HANDS_SPOOL="$SPOOL_FILE"

# Отметка «дошли до git-шага» (#588, scheduler.py::conflict_rework_attempts,
# маркер WORKER_GIT_STEP_MARKER в pulse_guard.py — держи текст в синхроне):
# всё ДО этой строки — инфраструктура (морда/деплой/сеть/плагин), не работа
# агента; только с этой точки промпт (шаг 3 маршрута — git rebase origin/main
# при дрейфе) вообще доходит до DSH. Комментарий — в ЗАДАЧУ (не в PR): тот же
# носитель следа аренды, что "worker run N" в claim_task.claim (CLAIM_VIA),
# та же граница по цифре нужна читателю на стороне scheduler.py, поэтому та
# же подстрока "worker run N" — часть текста ниже. Best-effort: провал
# постановки комментария не должен ронять сам прогон задачи.
gh issue comment "$number" \
  --body "🤖 [worker: git-шаг] worker run ${GITHUB_RUN_ID:-local}" >/dev/null \
  || echo "::warning::маркер git-шага не отправлен в задачу #$number — оркестратор увидит эту попытку как инфраструктурный сбой"

dsh_edge_start_drain
WORKER_TASK_FAILURE_REASON=""
DSH_RATE_LIMIT_MAX_WAIT_SECS="$WORKER_RATE_LIMIT_MAX_WAIT_SECS" \
DSH_RATE_LIMIT_INITIAL_DELAY_SECS="$WORKER_RATE_LIMIT_INITIAL_DELAY_SECS" \
DSH_RATE_LIMIT_MAX_DELAY_SECS="$WORKER_RATE_LIMIT_MAX_DELAY_SECS" \
  dsh_run_with_retry "$ANSWER_FILE" "$ERR_FILE" "$(cat "$PROMPT_FILE")"
rc=$DSH_RUN_RC
WORKER_TASK_FAILURE_REASON="$DSH_RUN_FAILURE_REASON"
echo "dsh завершился с кодом $rc"

# Транскрипт — до пост-обработки: ход работы в морде обгоняет отчёт в задаче.
dsh_edge_stop_drain
dsh_edge_drain_spool hard \
  || { echo "::error::Транскрипт не принят мордой — ход работы останется невидимым (#119)" >&2; exit 1; }
drained_lines=$(cat "$DSH_EDGE_DRAIN_CURSOR" 2>/dev/null || echo 0)
echo "Событий транскрипта в морде: $drained_lines"
if [ "$rc" -eq 0 ]; then
  [ -f "$SPOOL_FILE" ] || { echo "::error::Спул стрима не создан при успешном прогоне — плагин не работал" >&2; exit 1; }
  [ "$drained_lines" -gt 0 ] || { echo "::error::Ноль событий в сессии морды при успешном прогоне (#119)" >&2; exit 1; }
fi

# Рендер транскрипта (#131): доставка в морду не значит, что владелец увидит
# актуальные provider/model и раскрытые детали тула — это отдельный
# структурный инвариант формы батча (research/12), проверяется best-effort
# (не роняет успешный прогон, warning уже пишет сама функция).
if [ "$drained_lines" -gt 0 ]; then
  dsh_edge_verify_transcript "$DSH_EDGE_SESSION_ID" \
    || echo "::warning::Транскрипт-проверка (#131) нашла расхождения — см. warning'и выше" >&2
fi

ANSWER_TAIL=$(tail -c 4000 "$ANSWER_FILE" | redact)
ERR_TAIL=$(tail -c 4000 "$ERR_FILE" | redact)
echo "--- хвост ответа DSH ---"; [ -n "$ANSWER_TAIL" ] && printf '%s\n' "$ANSWER_TAIL"
echo "--- хвост stderr DSH ---"; [ -n "$ERR_TAIL" ] && printf '%s\n' "$ERR_TAIL"

# ── 8. Пост-обработка: видимый результат — открытый PR, а не код возврата ────────
# exit 0 у headless = «turn/end completed», но промпт мог быть исполнен мимо PR —
# поэтому проверяем артефакт, а не шаг.
pr_url=$(gh pr list --head "$BRANCH" --state open --limit 1 --json url --jq '.[0].url // ""')

if [ -n "$pr_url" ]; then
  comment=$(cat <<COMMENT
🤖 Автономный воркер справился. PR: $pr_url

Финальный ответ DSH (хвост, секреты замаскированы):

~~~~
$ANSWER_TAIL
~~~~
COMMENT
  )
  gh issue comment "$number" --body "$comment" >/dev/null
  # «Выполнена» здесь НЕ звучит (#170): открытый PR — промежуточный шаг, а не
  # сделанная задача; это слово в Telegram теперь значит только «слито в main»
  # (scheduler.py::after_merge). Коротко, номера задачи и PR — кликабельные
  # ссылки (parse_mode=HTML в telegram_report), заголовок — первые 6 слов,
  # экранированные tg_html.
  pr_number=${pr_url##*/}
  telegram_report "🤖 worker: PR открыт — <a href=\"${pr_url}\">#${pr_number}</a> по задаче <a href=\"https://github.com/${GITHUB_REPOSITORY}/issues/${number}\">#${number}</a> «$(tg_html "$(short_title "$title")")»" || true
  echo "PR открыт: $pr_url — job зелёный"
  exit 0
fi

# Эскалация playbook (п.2 главных правил) — законный исход: задача ждёт владельца,
# конвейер не сломан, job зелёный.
if jq -e '.labels[]? | select(.name == "blocked")' \
    <(gh issue view "$number" --json labels) >/dev/null; then
  # Явный drop аренды при blocked-эскалации (#121): работу никто не ведёт,
  # держать замок — зря блокировать задачу остальным на TTL. Сбой снятия не
  # роняет отчёт: газ — TTL-сборщик оркестратора (24 ч).
  drop_out="$(lease_cli release "$number" 2>&1)" && drop_rc=0 || drop_rc=$?
  if [ "$drop_rc" -eq 0 ]; then
    echo "Аренда снята (blocked): $drop_out"
  else
    echo "::warning::замок задачи #$number не снят при эскалации (rc=$drop_rc): $drop_out — снимет TTL-сборщик"
  fi
  comment=$(cat <<COMMENT
🤖 Автономный воркер эскалировал: то, что нужно для задачи, есть только у владельца.
Аренда задачи снята (замок убран, назначение осталось — задача ждёт владельца).
Детали — в комментариях выше и в хвосте ответа DSH ниже (секреты замаскированы).

~~~~
$ANSWER_TAIL
~~~~
COMMENT
  )
  gh issue comment "$number" --body "$comment" >/dev/null
  telegram_report "worker: задача #$number — эскалация владельцу (метка blocked)" || true
  echo "Эскалация оформлена (blocked) — job зелёный, ждём владельца"
  exit 0
fi

# Провайдер в лимите (#422) — не сбой агента: сообщение и Telegram обязаны
# звучать иначе, чем «воркер не справился» (правило AGENTS.md — «возможности
# нет» и «возможность есть, но сломана» лечатся по-разному), а задача обязана
# вернуться в пул СРАЗУ (снять и замок, и назначение), не ждать 24-часовой
# TTL-сборщик — вина не в задаче, держать её занятой зря.
if [ "$WORKER_TASK_FAILURE_REASON" = "quota_exhausted" ] || \
   [ "$WORKER_TASK_FAILURE_REASON" = "rate_limit_retry_budget_exceeded" ]; then
  if [ "$WORKER_TASK_FAILURE_REASON" = "quota_exhausted" ]; then
    reason="квота провайдера исчерпана надолго (RATE_LIMIT: Weekly/Monthly Limit Exhausted, код возврата $rc) — повтор внутри этого прогона не поможет, нужно ждать вне CI или сменить провайдера (docs/runbooks/switch-llm-provider.md)"
  else
    reason="временный RATE_LIMIT провайдера не снялся за отведённый бюджет ожидания ${WORKER_RATE_LIMIT_MAX_WAIT_SECS}с (код возврата $rc)"
  fi
  release_out="$(lease_cli release-full "$number" 2>&1)" && release_rc=0 || release_rc=$?
  if [ "$release_rc" -eq 0 ]; then
    echo "Провайдер в лимите — задача #$number возвращена в пул немедленно: $release_out"
    release_note="Задача возвращена в пул немедленно — снят и замок, и назначение ($release_out)."
  else
    echo "::warning::задача #$number не возвращена в пул (rc=$release_rc): $release_out — снимет TTL-сборщик через 24 ч"
    release_note="Возврат в пул не подтверждён (см. лог job'а) — снимет TTL-сборщик через 24 ч."
  fi
  comment=$(cat <<COMMENT
🤖 Автономный воркер остановлен провайдером, не своей ошибкой: $reason.
$release_note Хвосты логов ниже (секреты замаскированы).

Хвост stderr DSH:

~~~~
$ERR_TAIL
~~~~

Хвост ответа DSH:

~~~~
$ANSWER_TAIL
~~~~
COMMENT
  )
  gh issue comment "$number" --body "$comment" >/dev/null
  telegram_report "worker: задача #$number — провайдер в лимите, не сбой агента ($reason). Задача возвращена в пул" || true
  die "Провайдер в лимите: $reason"
fi

reason="dsh завершился с кодом $rc без открытого PR"
[ "$rc" = "124" ] && reason="DSH уложился в таймаут ${DSH_TIMEOUT_SECS}с, PR не открыт"
comment=$(cat <<COMMENT
🤖 Автономный воркер не справился: $reason.
Задача остаётся под арендой: оркестратор вернёт её в пул через 24 ч без PR
(снимет и протухший замок, и назначение), либо сними их вручную
(python3 scripts/lib/claim_task.py release $number). Хвосты логов ниже
(секреты замаскированы).

Хвост stderr DSH:

~~~~
$ERR_TAIL
~~~~

Хвост ответа DSH:

~~~~
$ANSWER_TAIL
~~~~
COMMENT
  )
gh issue comment "$number" --body "$comment" >/dev/null
telegram_report "worker: задача #$number — ПРОВАЛ ($reason). Детали в задаче" || true
die "Воркер не справился: $reason"
