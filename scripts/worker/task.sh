#!/usr/bin/env bash
# Автономный воркер (задача #89): воплощение docs/agents/WORKER-PLAYBOOK.md.
# Здесь ТОЛЬКО транспорт и отчётность: выбрать свободную задачу из пула, взять
# её в атомарную аренду (claim_task, #121), создать ветку agent/N-slug (либо,
# если на задачу уже открыт PR без исполнителя — #245, довести его: чекаут
# существующей ветки, без второй ветки/PR), накормить DSH headless промптом
# (тело задачи + playbook + критерий), проверить результат (PR по ветке
# СУЩЕСТВУЕТ — открыт или уже слит, #413) и отчитаться (комментарий в задачу
# + Telegram). Работу над задачей делает DSH — этот скрипт за него ничего не
# решает и не пишет.
#
# Ретрай временного RATE_LIMIT провайдера (#422, механизм #419/#421 —
# dsh_run_with_retry в lib/dsh-ci.sh): живой факт — прогоны worker.yml
# 34007508064/34006554580 упали с «dsh: RATE_LIMIT: Rate limit reached for
# requests», раньше, чем ретрай вообще появился (тогда — только у ai-review).
# Бюджет ожидания WORKER_RATE_LIMIT_MAX_WAIT_SECS (по умолчанию 1800с/30 мин —
# та же длительность короткого окна провайдера, что и у ai-review: она не
# зависит от вызывающего канала) — ОБЩИЙ на ВЕСЬ прогон (пул + цепочка, все
# до 10 попыток вместе, #877/#880): dsh_run_with_provider_chain в lib/dsh-ci.sh
# расходует его по факту через DSH_RUN_WAITED_SECS, не выдаёт заново каждому
# провайдеру, а dsh_run_with_pool_then_chain передаёт цепочке то, что уже
# потратил anthropic-oauth-pool (initial_rl_used) — пул это ПЕРВАЯ из 10
# попыток, не отдельная ось бюджета. Без учёта пула N провайдеров могли бы
# выжечь ещё N × 30 мин суммарно сверх бюджета пула (находка ai-review PR
# #880 на первой версии этого фикса — стык пул→цепочка не передавал остаток).
#
# DSH_TIMEOUT_SECS за попытку ОДНОГО провайдера (#877, инцидент 2026-09-10,
# прогон worker.yml 34498185823, задача #140): исходные 150 мин были подобраны
# ДО того, как #727 ввёл цепочку из нескольких провайдеров — тогда за прогон
# была ровно одна попытка, и 150 минут укладывались в 280-минутный бюджет
# job'а с запасом. Сегодня config/provider-usage.json::default-chain несёт 9
# провайдеров, плюс anthropic-oauth-pool пробуется первым
# (dsh_run_with_pool_then_chain) — до 10 попыток за прогон. При старом
# значении даже ДВА зависших провайдера (2 × 150 мин = 300 мин) убили бы job
# таймаутом GitHub'а ДО того, как цепочка дойдёт до конца и напишет отчёт —
# живой случай: GLM отработал ровно 9000с и был убит по нашему таймауту,
# оставив на оставшиеся ZAI/OpenRouter-2/Ollama-2 меньше трети job-бюджета.
# Новое значение — 1200с (20 мин). Худший реалистичный сценарий по факту
# конфигурации (не гипотеза о типичной длительности успешного вызова — данных
# о ней нет, «не подтверждено»): ВСЕ 10 попыток зависают на полный
# DSH_TIMEOUT_SECS (эта ось не ограничена общим бюджетом RATE_LIMIT — зависание
# и короткий RATE_LIMIT решают РАЗНЫЕ отказы одной попытки, dsh_run_with_retry
# при зависании даже не пытается ретраить, см. её комментарий) — 10 × 20 мин =
# 200 мин, ПЛЮС общий бюджет RATE_LIMIT (30 мин, расходуется РОВНО один раз на
# весь прогон, не умножается на число провайдеров) = 230 мин, оставляет ~50 мин
# из 280-минутного бюджета job'а на установку/git/пост-обработку. Патологический
# случай «зависло почти у самого края ОБОИХ таймаутов у КАЖДОГО из 10
# провайдеров одновременно» арифметически исключён общим бюджетом RATE_LIMIT
# (он физически не может выдаться дважды); упор в 280 минут остаётся
# теоретически возможным только если install/git/отчёт сами по себе съедят
# оставшиеся ~50 мин — тот же непокрытый класс уже принят в #421 для
# ai-review, отдельно не решается здесь.
#
# Не подтверждено (находка ai-review PR #880): арифметика выше считает
# ретраи ВНУТРИ одного провайдера (dsh_run_with_retry, короткий RATE_LIMIT)
# почти мгновенными — если сервер отвечает не сразу, а держит соединение
# ощутимое время (не полный DSH_TIMEOUT_SECS, что дало бы «зависание» и
# остановило ретрай, но и не мгновенно), до ~9 таких попыток одного
# провайдера (задержки 30→300с против общего бюджета 1800с) способны
# накопить заметно больше времени, чем учтено выше. Данных о реальной
# длительности такого ответа нет — арифметика 230 мин честна только при
# допущении «ретраи быстрые», не при произвольной задержке сервера.
# rc=124 (наш таймаут) теперь ОТДЕЛЁН
# от «провайдер молчит» в самом сообщении (dsh_chain_should_advance, lib/
# dsh-ci.sh, #877) — решение «пробовать следующего» то же самое, текст честнее.
#
# quota_exhausted (недельная/месячная квота), rate_limit_retry_budget_exceeded
# (бюджет короткого окна кончился) и all_providers_exhausted (цепочка ниже
# исчерпана целиком) — все три причины возвращают задачу в пул СРАЗУ
# (lease_cli release-full, #422), не дожидаясь 24-часового TTL-сборщика: вина
# не в задаче, держать assignee до таймера — зря прятать её от других каналов.
#
# Цепочка провайдеров (#727, довод #797): dsh_require_provider_env/
# dsh_run_with_retry заменены на dsh_require_provider_chain/
# dsh_run_with_provider_chain (lib/dsh-ci.sh, тот же механизм, что уже несёт
# ai-review.yml, scripts/review/ai_dsh.sh) — quota_exhausted и повторяемый
# транспортный отказ (HTTP_404/EMPTY_RESPONSE) переключают на следующего
# провайдера ВНУТРИ одного прогона, без ручной смены vars/секрета (иначе
# недельная/месячная квота GLM держала бы воркер мёртвым до сброса, хотя
# NVIDIA в цепочке рабочий). Порядок относительно монтажа плагина стрима
# (dsh-hands-streamer, шаг 6b ниже) — см. комментарий у шага 6: профиль
# затравлен ПЕРВЫМ провайдером цепочки ДО первого `dsh` (dsh plugin add),
# цепочка перепатчивает профиль заново на каждую попытку внутри шага 7 — тот
# же dsh_patch_profile, не второй механизм.
# Использование:
#   task.sh               — выбрать свободную задачу из пула и выполнить
#   task.sh --task 89     — выполнить конкретную задачу (если она открыта и свободна)
#   task.sh --dry-run     — самотест: напечатать выбранную задачу и промпт,
#                           ничего не назначая, не запуская и не отправляя
#
# Итог запуска: PR открыт ИЛИ слит → job зелёный (#413: слитый PR — успех
# более полный, чем открытый, не отсутствие результата); эскалация (метка
# blocked) → зелёный; провайдер в лимите/квоте надолго → job красный, но
# задача честно возвращена в пул (не «воркер не справился» — вина не его);
# иначе (PR нет, закрыт без слияния или пуст без диффа, реальный сбой) →
# job красный. Нет свободных задач → зелёный без действий.
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
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-1200}"   # 20 минут за попытку ОДНОГО провайдера (#877, см. обоснование в шапке файла)
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

# Цепочка приходит из манифеста использования (openspec/changes/
# llm-provider-usage-manifest, config/provider-usage.json, потребитель
# "worker") — dsh_require_provider_chain резолвит её по id ПЕРЕД обычной
# валидацией; манифеста нет вовсе — фоллбэк на vars.DSH_PROVIDER_CHAIN
# (#727/#797) как раньше. Проверяем раньше дупгарда/назначения/ветки/сессии
# морды — падать сразу, а не после дорогой подготовительной работы.
# Пропускаем при --dry-run: самотест печатает выбор и промпт без реального
# вызова модели, требовать цепочку здесь незачем.
if [ "$DRY_RUN" != "1" ]; then
  dsh_require_provider_chain "worker" || die "провайдер не сконфигурирован (см. ::error:: выше)"
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

# ── 2. Промпт: тело задачи + критерий + AGENTS.md + PROTOCOL.md + playbook ────────
# Правила репозитория раньше до исполнителя не доходили вообще (только ссылка
# на них внутри playbook) — здесь они впечатываются дословно, не пересказом:
# бюджет промпта позволяет с большим запасом (модель канала — переменные
# репозитория DEEPSEEK_MODEL/DSH_EDGE_MODEL_CATALOG, контекстное окно на
# порядки больше суммы этих файлов).
PLAYBOOK_FILE="$SCRIPT_DIR/../../docs/agents/WORKER-PLAYBOOK.md"
[ -f "$PLAYBOOK_FILE" ] || die "Нет docs/agents/WORKER-PLAYBOOK.md — воркер без playbook не работает"
AGENTS_FILE="$SCRIPT_DIR/../../AGENTS.md"
[ -f "$AGENTS_FILE" ] || die "Нет AGENTS.md — воркер без правил репозитория не работает"
PROTOCOL_FILE="$SCRIPT_DIR/../../docs/agents/PROTOCOL.md"
[ -f "$PROTOCOL_FILE" ] || die "Нет docs/agents/PROTOCOL.md — воркер без протокола совместной работы не работает"
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

PR на задачу (открытый или уже слитый) — обязательный результат: без него запуск считается провалом воркера.

# Правила репозитория (AGENTS.md, дословно; обязательны, не пересказ)

PROMPT
  cat "$AGENTS_FILE"
  cat <<PROMPT

# Протокол совместной работы агентов (docs/agents/PROTOCOL.md, дословно; обязателен)

PROMPT
  cat "$PROTOCOL_FILE"
  cat <<PROMPT

# Правила работы воркера (playbook — то, чего нет в AGENTS.md)

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
# флаг несёт НОМЕР арендованной задачи (не булев признак): task-branch сверяет
# его с номером СВОЕЙ ветки и пропускает claim только при совпадении — иначе
# повторный claim того же номера тем же держателем отклонился бы как «занято»
# (claim неидемпотентен по актору). Булев признак здесь был бы дырой: если за
# один прогон агент создаёт вторую ветку на ДРУГУЮ задачу (playbook это
# допускает), «занято»-флаг молча пропустил бы аренду второй задачи.
export LEASE_ALREADY_CLAIMED="$number"

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
# `|| die` — тот же класс, что у git ls-remote ниже (находка ai-review PR #937,
# второй проход): голая сетевая подстановка под `set -euo pipefail` без
# явной обработки отказа роняет job тихой bash-ошибкой строки.
gh_user_id=$(gh api "users/$WORKER_LOGIN" --jq .id) \
  || die "не смог прочитать id $WORKER_LOGIN (gh/сеть)"
git config user.name "$WORKER_LOGIN"
git config user.email "${gh_user_id}+${WORKER_LOGIN}@users.noreply.github.com"

# Отпечаток HEAD ветки ДО прогона DSH (issue #876, живой случай — прогон
# 34498185823 отрапортовал успех по PR #395, существовавшему с прошлого
# прогона, без единого нового коммита в этом): признак «в РАБОЧЕМ ДЕРЕВЕ
# воркера появилась работа этого прогона». dsh_worker_run_is_success
# (dsh-ci.sh) сверяет его с WORKER_BRANCH_END_SHA на шаге 8.
WORKER_BRANCH_START_SHA=$(git rev-parse HEAD)
# Отпечаток ГОЛОВЫ ВЕТКИ НА ORIGIN ДО прогона (регрессия #878/#935, живой
# случай — восемь прогонов подряд 2026-09-11): локальный чекаут воркера —
# это линкованный git worktree (см. комментарий у $PR_WORKTREE выше), чей
# admin-каталог лежит СНАРУЖИ рабочего дерева в общем commondir. Файловая
# песочница dsh блокирует запись именно туда (текст самого DSH в логах
# прогонов 34557341675/34559273918/34578536716: «разделяемый .git главного
# чекаута оказался закрыт на запись файловой песочницей») — агент обходит
# это отдельным клоном и пушит НАПРЯМУЮ в origin, минуя локальный чекаут
# целиком. Локальный HEAD в таком прогоне не двигается вообще, хотя работа
# доезжает до origin (живой пример — PR #412 слит в рамках прогона
# 34550467839, локальный WORKER_BRANCH_START_SHA/END_SHA совпали). Origin —
# это то, что видно СНАРУЖИ независимо от того, как именно агент обошёл
# песочницу, поэтому именно он, а не только локальный чекаут, доказывает
# работу этого прогона (см. dsh_worker_run_is_success). Пустая строка —
# ветки на origin ещё нет (обычный случай для новой задачи до первого пуша).
WORKER_BRANCH_ORIGIN_START_SHA=$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1) \
  || die "не смог снять снимок головы ветки $BRANCH на origin (git/сеть)"

# ── 5b. Сессия раннера в морде (#119): создать/переиспользовать и назвать ────────
# Имя сессии = «#N: название задачи», воркспейс edge-harness. Отказ громкий:
# без сессии ход работы владельцу не виден — job красный (критерий #119).
HARNESS_SID="harness-$number"
HARNESS_TITLE="#$number: $title"
dsh_edge_login || { echo "::error::Нет доступа к морде dsh-edge — job красный (#119)" >&2; exit 1; }
# Реально использованный id может отличаться от HARNESS_SID (#809: фоллбэк
# на испорченной холодной загрузке) — читаем возврат функции, не подставляем
# исходный HARNESS_SID вручную.
HARNESS_SID_ACTUAL=$(dsh_edge_session_begin "$HARNESS_SID" "$HARNESS_TITLE") \
  || { echo "::error::Сессия $HARNESS_SID не создана в морде — ход работы останется невидимым (#119)" >&2; exit 1; }
export DSH_EDGE_SESSION_ID="$HARNESS_SID_ACTUAL"
echo "Сессия морды: $DSH_EDGE_SESSION_ID — «$HARNESS_TITLE»"

# ── 6. DSH: цепочка провайдеров (проверена в начале скрипта), установка (lib) ─────
dsh_install "$WORK/pkgs"
dsh --version || true
dsh_install_plugins_suite "$WORK/plugins" || die "suite ротации учёток не установился (см. ::error:: выше, #215)"
# Быстрый провайдер Claude (#838) — независимо от suite выше, гейт: секреты
# ANTHROPIC_OAUTH_1/2, не vars.PLUGINS_SUITE_URL. Импорт — до первого dsh.
dsh_install_anthropic_pool "$WORK/anthropic-pool" || die "быстрый провайдер Claude не установился (см. ::error:: выше, #838)"
dsh_import_anthropic_accounts || die "импорт аккаунтов Claude не удался (см. ::error:: выше, #838)"
# Затравка профиля первым провайдером цепочки (chain[0]) — ОБЯЗАНА случиться
# ДО первого `dsh` этого прогона (dsh plugin add, шаг 6b ниже): «initProfile
# пишет package.json/cordis.patch.yml/pnpm-workspace.yaml только при
# отсутствии, ничего не перезаписывает» (research/10-dsh-architecture.md,
# живой прогон 2026-08-30) — если cordis.patch.yml ещё не существует к
# моменту plugin add, initProfile создаст файл сам, с содержимым, которое
# отсюда не контролируется. Дальше, на шаге 7, dsh_run_with_provider_chain
# перепатчивает профиль ЗАНОВО на каждую попытку (тот же dsh_patch_profile,
# полная перезапись файла) — здесь важен только факт, что файл СУЩЕСТВУЕТ к
# моменту первого `dsh`, не его точное содержимое; профиль на тот момент уже
# инициализирован (package.json/pnpm-workspace.yaml созданы), поэтому
# повторный патч не задевает монтаж плагина (bundles профиля — отдельный
# слой, `dsh --dump-config`, «Порядок слоёв», research/10-dsh-architecture.md).
_chain_head=$(jq -c '.[0]' <<<"$DSH_PROVIDER_CHAIN")
_chain_head_secret=$(jq -r '.secret_env' <<<"$_chain_head")
DEEPSEEK_BASE_URL=$(jq -r '.base_url' <<<"$_chain_head")
DEEPSEEK_MODEL=$(jq -r '.model' <<<"$_chain_head")
DEEPSEEK_API_KEY="${!_chain_head_secret:-}"
export DEEPSEEK_BASE_URL DEEPSEEK_MODEL DEEPSEEK_API_KEY
DSH_MAX_TOKENS=$(jq -r '.max_output_tokens // 131072' <<<"$_chain_head") dsh_patch_profile headless
dsh_mount_plugins_suite headless || die "suite ротации учёток не смонтировался (см. ::error:: выше, #215)"
dsh_mount_anthropic_pool headless || die "быстрый провайдер Claude не смонтировался (см. ::error:: выше, #838)"

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
  dsh_run_with_pool_then_chain "$ANSWER_FILE" "$ERR_FILE" "$(cat "$PROMPT_FILE")"
rc=$DSH_RUN_RC
WORKER_TASK_FAILURE_REASON="$DSH_RUN_FAILURE_REASON"
WORKER_CHAIN_PROVIDER="$DSH_CHAIN_PROVIDER"
WORKER_CHAIN_TRIED="$DSH_CHAIN_TRIED"
WORKER_CHAIN_RESET_HINT="$DSH_CHAIN_RESET_HINT"
echo "dsh завершился с кодом $rc (провайдер: ${WORKER_CHAIN_PROVIDER:-нет успеха}, опробованы: ${WORKER_CHAIN_TRIED:-?})"
# Отпечаток HEAD ветки ПОСЛЕ прогона — сравнивается с WORKER_BRANCH_START_SHA
# на шаге 8 (dsh_worker_run_is_success, issue #876).
WORKER_BRANCH_END_SHA=$(git rev-parse HEAD)

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

# Отпечаток головы ветки на origin ПОСЛЕ прогона — сравнивается с
# WORKER_BRANCH_ORIGIN_START_SHA на шаге 8 (регрессия #878/#935, см.
# комментарий у WORKER_BRANCH_ORIGIN_START_SHA выше). Снят ЗДЕСЬ, а не сразу
# после dsh (находка ai-review PR #937): это тоже сетевое чтение под
# `set -euo pipefail`, и при отказе сети `die` роняет job — если снимать его
# ДО слива транскрипта в морду, отказ сети сжирает вообще весь отчёт о
# прогоне (транскрипт так и не долетает до морды). Здесь транскрипт уже
# доставлен и проверен — при отказе сети теряется только гейт успеха/шаг 8
# с комментарием в задачу, ход работы в морде уже виден. Момент измерения
# (голова ветки на origin) от переноса не меняется: между концом dsh и этой
# строкой в ветку никто не пишет.
WORKER_BRANCH_ORIGIN_END_SHA=$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1) \
  || die "не смог снять снимок головы ветки $BRANCH на origin (git/сеть)"

ANSWER_TAIL=$(tail -c 4000 "$ANSWER_FILE" | redact)
ERR_TAIL=$(tail -c 4000 "$ERR_FILE" | redact)
echo "--- хвост ответа DSH ---"; [ -n "$ANSWER_TAIL" ] && printf '%s\n' "$ANSWER_TAIL"
echo "--- хвост stderr DSH ---"; [ -n "$ERR_TAIL" ] && printf '%s\n' "$ERR_TAIL"

# ── 8. Пост-обработка: видимый результат — PR по ветке СУЩЕСТВУЕТ (открыт ИЛИ
# слит) И работа доказана В ЭТОМ ПРОГОНЕ (#876) — ни то, ни другое поодиночке
# не гейт успеха. «PR существует» — pr_outcome.py: «PR нет вообще», «закрыт
# без слияния» и «открыт без диффа» остаются провалом (fail loud, тот же
# промпт мог быть исполнен мимо PR), «открыт с диффом» и «слит» проходят
# дальше. «Работа этого прогона» — dsh_worker_run_is_success (dsh-ci.sh, #876,
# живой случай — прогон 34498185823 отрапортовал успех по PR #395,
# существовавшему с прошлого прогона, при rc=1 и пустом WORKER_CHAIN_PROVIDER
# в ЭТОМ прогоне): rc dsh этого прогона, непустой провайдер, новый коммит В
# РАБОЧЕМ ДЕРЕВЕ ИЛИ на origin ветки (регрессия #878/#935: локальный чекаут
# воркера не двигается, если агент обошёл файловую песочницу отдельным
# клоном и пушит напрямую в origin — origin в этом случае и есть внешнее
# доказательство работы, локальный HEAD сам по себе больше не единственный
# признак). Слитый PR — успех более полный, чем открытый: DSH мог за один
# вызов довести цикл до мержа (кейс #170/PR #402 — старая проверка искала
# только открытый PR и считала уже слитую работу провалом, потому что нашла
# её доведённой лучше ожидаемого).
gh pr list --head "$BRANCH" --state all --limit 10 \
    --json number,state,additions,deletions,changedFiles,url >"$WORK/branch-prs.json" \
  || die "не смог прочитать PR ветки $BRANCH (gh/сеть)"
set +e
pr_line=$(python3 "$SCRIPT_DIR/../lib/pr_outcome.py" "$WORK/branch-prs.json")
pr_outcome_rc=$?
set -e
# Находка ревью PR #415: только 0/1 — легитимные исходы (успех/провал).
# Любой другой код (2 — контракт CLI сломан, 127 — python3 не найден,
# необработанный трейсбек и т.п.) — поломка самой проверки, не «PR открыт,
# но пуст»: смешивать их значило бы съедать сломанный tooling под видом
# честного провала задачи (тот же принцип «пусто ≠ сломано», что уже
# проводит pr_outcome.py и что закрывал #245 для free_task.py).
case "$pr_outcome_rc" in
  0|1) ;;
  *) die "проверка PR ветки $BRANCH сломалась (rc=$pr_outcome_rc)" ;;
esac
pr_status=${pr_line%%$'\t'*}
pr_url=${pr_line#*$'\t'}

run_is_success=0
if [ "$pr_outcome_rc" -eq 0 ] && dsh_worker_run_is_success \
    "$rc" "$WORKER_CHAIN_PROVIDER" "$WORKER_BRANCH_START_SHA" "$WORKER_BRANCH_END_SHA" \
    "$WORKER_BRANCH_ORIGIN_START_SHA" "$WORKER_BRANCH_ORIGIN_END_SHA"; then
  run_is_success=1
fi

if [ "$run_is_success" -eq 1 ]; then
  # Гейт уже потребовал непустой WORKER_CHAIN_PROVIDER (dsh_worker_run_is_success) —
  # фолбэк "?" здесь был бы ровно тем литералом, что печатает противоречие
  # инцидента #876, если гейт когда-нибудь разъедется с этим местом. Падаем
  # громко, а не молча подставляем «?» (находка ai-review PR #880).
  [ -n "$WORKER_CHAIN_PROVIDER" ] \
    || die "гейт успеха пройден, но WORKER_CHAIN_PROVIDER пуст — рассинхрон с dsh_worker_run_is_success (#876/#880), это баг гейта"
  verb="открыт"; [ "$pr_status" = "merged" ] && verb="слит"
  comment=$(cat <<COMMENT
🤖 Автономный воркер справился (провайдер: ${WORKER_CHAIN_PROVIDER}). PR $verb: $pr_url

Хвост stdout DSH (секреты замаскированы; это не обязательно «финальный
ответ» — содержимое не проверяется структурно, #876):

~~~~
$ANSWER_TAIL
~~~~
COMMENT
  )
  gh issue comment "$number" --body "$comment" >/dev/null
  # «Выполнена» здесь НЕ звучит (#170): даже слитый ветвью PR — это то, что
  # воркер увидел ПОСТФАКТУМ в собственном прогоне, а не факт слияния,
  # подтверждённый оркестратором; это слово в Telegram значит только «слито в
  # main» от scheduler.py::after_merge — второй отправитель того же события
  # не заводим. Коротко, номера задачи и PR — кликабельные ссылки
  # (parse_mode=HTML в telegram_report), заголовок — первые 6 слов,
  # экранированные tg_html.
  pr_number=${pr_url##*/}
  telegram_report "🤖 worker: PR ${verb} — <a href=\"${pr_url}\">#${pr_number}</a> по задаче <a href=\"https://github.com/${GITHUB_REPOSITORY}/issues/${number}\">#${number}</a> «$(tg_html "$(short_title "$title")")»" || true
  echo "PR $verb: $pr_url — job зелёный"
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

# Провайдер в лимите (#422) или вся цепочка исчерпана (#727/#797) — не сбой
# агента: сообщение и Telegram обязаны звучать иначе, чем «воркер не
# справился» (правило AGENTS.md — «возможности нет» и «возможность есть, но
# сломана» лечатся по-разному), а задача обязана вернуться в пул СРАЗУ (снять
# и замок, и назначение), не ждать 24-часовой TTL-сборщик — вина не в задаче,
# держать её занятой зря.
if [ "$WORKER_TASK_FAILURE_REASON" = "quota_exhausted" ] || \
   [ "$WORKER_TASK_FAILURE_REASON" = "rate_limit_retry_budget_exceeded" ] || \
   [ "$WORKER_TASK_FAILURE_REASON" = "all_providers_exhausted" ]; then
  case "$WORKER_TASK_FAILURE_REASON" in
    quota_exhausted)
      reason="квота провайдера исчерпана надолго (RATE_LIMIT: Weekly/Monthly Limit Exhausted, код возврата $rc) — повтор внутри этого прогона не поможет, нужно ждать вне CI или сменить провайдера (docs/runbooks/switch-llm-provider.md)" ;;
    rate_limit_retry_budget_exceeded)
      # #880 (некритичная находка ai-review): WORKER_RATE_LIMIT_MAX_WAIT_SECS
      # — это ОБЩИЙ бюджет на весь прогон (#877), не то, что достался
      # именно этому провайдеру — предыдущие попытки цепочки могли уже
      # потратить часть или весь бюджет, оставив этому нуль. Формулировка
      # называет число честно как «суммарный лимит», не как «выданный этому
      # провайдеру остаток» (которого сообщение здесь не знает).
      reason="временный RATE_LIMIT провайдера не снялся до исчерпания общего бюджета ожидания на весь прогон (${WORKER_RATE_LIMIT_MAX_WAIT_SECS}с суммарно, #877; код возврата $rc)" ;;
    all_providers_exhausted)
      reason="цепочка провайдеров исчерпана целиком (опробованы: ${WORKER_CHAIN_TRIED:-?})${WORKER_CHAIN_RESET_HINT:+, ближайший названный сброс: $WORKER_CHAIN_RESET_HINT} — повтор внутри этого прогона не поможет (docs/runbooks/switch-llm-provider.md, #727)" ;;
  esac
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

# pr_status здесь бывает двух родов (#876): "empty"/"absent" (pr_outcome_rc=1)
# — настоящий провал по факту PR, пустой PR — DSH создал ветку/PR, но не
# поработал; отсутствие PR — работы не видно вовсе. "open"/"merged"
# (pr_outcome_rc=0) — PR СУЩЕСТВУЕТ, но dsh_worker_run_is_success выше
# отказал — живой случай #876 (PR #395 существовал с прошлого прогона), но
# НЕ единственный: rc!=0 с уже сделанными коммитами (попытка закоммитила и
# упала позже) или rc=0 без единого нового коммита — тоже сюда. Сообщение
# обязано называть КОНКРЕТНО несработавший конъюнкт (находка ai-review PR
# #880 «алерт не гадает» AGENTS.md), не один и тот же текст для всех причин.
reason="dsh завершился с кодом $rc без PR по ветке $BRANCH"
if [ "$pr_outcome_rc" -eq 0 ]; then
  # DSH_WORKER_RUN_GATE_GAPS — уже посчитан вызовом dsh_worker_run_is_success
  # выше (шаг 8, та же ветка `[ "$pr_outcome_rc" -eq 0 ] && dsh_worker_run_is_success`) —
  # одно место правды на текст несработавшего конъюнкта, не вторая копия тех
  # же трёх условий (находка ai-review PR #880: было — task.sh пересчитывал
  # их сам, и при эволюции гейта список мог молча разойтись).
  reason="PR $pr_url по ветке $BRANCH ($pr_status) существует, но не доказывает работу этого прогона: ${DSH_WORKER_RUN_GATE_GAPS:-неизвестная причина (DSH_WORKER_RUN_GATE_GAPS пуст — баг проводки гейта)}"
elif [ "$pr_status" = "empty" ]; then
  reason="dsh завершился с кодом $rc — PR $pr_url по ветке $BRANCH открыт, но пуст (без диффа)"
fi
# #877/#880 (второй экземпляр того же класса, найденный ai-review): rc=124
# сюда попадает и когда цепочка остановилась стоп-классом
# (dsh_chain_should_advance вернула «не переключаемо») — с тех пор, как #877
# завёл замер elapsed/timeout, «зависание» (наш нож) само уходит в
# переключаемую ветку и до этой строки не доходит по умолчанию; rc=124 здесь
# может означать, что ребёнок САМ вышел с этим кодом (stderr непустой,
# elapsed < timeout) — «наш таймаут» тогда было бы недоказанным утверждением
# (сценарий 15 смоука цепочки это запрещает). Проверяем то же условие
# elapsed>=timeout для ОБЕИХ ветвей (PR существует/не существует).
if [ "$rc" = "124" ] && [ "${DSH_RUN_LAST_ATTEMPT_ELAPSED_SECS:-0}" -ge "${DSH_RUN_LAST_ATTEMPT_TIMEOUT_SECS:-999999999}" ]; then
  if [ "$pr_outcome_rc" -eq 0 ]; then
    reason="DSH уложился в таймаут ${DSH_TIMEOUT_SECS}с; PR $pr_url по ветке $BRANCH ($pr_status) существует, ${DSH_WORKER_RUN_GATE_GAPS:-неизвестная причина (DSH_WORKER_RUN_GATE_GAPS пуст — баг проводки гейта)}"
  else
    reason="DSH уложился в таймаут ${DSH_TIMEOUT_SECS}с, PR по ветке $BRANCH не найден"
  fi
fi
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
