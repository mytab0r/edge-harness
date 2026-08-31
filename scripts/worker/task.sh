#!/usr/bin/env bash
# Автономный воркер (задача #89): воплощение docs/agents/WORKER-PLAYBOOK.md.
# Здесь ТОЛЬКО транспорт и отчётность: выбрать свободную задачу из пула, назначить,
# создать ветку agent/N-slug, накормить DSH headless промптом (тело задачи +
# playbook + критерий), проверить результат (открытый PR) и отчитаться
# (комментарий в задачу + Telegram). Работу над задачей делает DSH — этот скрипт
# за него ничего не решает и не пишет.
#
# Использование:
#   task.sh               — выбрать свободную задачу из пула и выполнить
#   task.sh --task 89     — выполнить конкретную задачу (если она открыта и свободна)
#   task.sh --dry-run     — самотест: напечатать выбранную задачу и промпт,
#                           ничего не назначая, не запуская и не отправляя
#
# Итог запуска: PR открыт → job зелёный; эскалация (метка blocked) → зелёный;
# иначе (нет PR) → job красный. Нет свободных задач → зелёный без действий.
set -euo pipefail

die() { echo "::error::$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины DSH, integrity, GLM-патч профиля, redact — единственное место правды в lib.
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"
# Шов сессии раннера в морду (#119): логин, begin, дрен спула в ingest.
# shellcheck source=scripts/lib/dsh-edge-session.sh
source "$SCRIPT_DIR/../lib/dsh-edge-session.sh"

WORKER_LOGIN="${WORKER_LOGIN:?WORKER_LOGIN не задан (логин, под которым воркер берёт задачи)}"
DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-9000}"   # 150 минут на прогон DSH
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

# Гвардия дублей (стопгэп к аренде задач #121): если живёт ДРУГОЙ прогон
# воркера — активный или ожидающий в очереди concurrency-группы — выходим
# зелёным no-op. Очередь запускает прогоны последовательно, и второму
# прогону на той же задаче делать нечего; раньше он сжигал установку и мог
# столкнуться с первым на выборе задачи. Кросс-канальную атомарную аренду
# (worker/manual/hands) закрывает #121 — здесь защита от дубля самих прогонов.
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
telegram_report() { # $1 — текст
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "::warning::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID не заданы — Telegram-отчёт не отправлен"
    return 1
  fi
  if ! curl -fsS --max-time 30 -X POST \
      "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=$1" >/dev/null; then
    echo "::warning::Telegram не принял отчёт — комментарий в задаче остаётся местом правды"
    return 1
  fi
}

# Свободная задача: открыта, метка task, без assignee И без открытого PR,
# ссылающегося на неё (playbook «Конвейер задачи» п.1: PR появляется раньше,
# чем контракт успеет авто-назначить автора). Печатает «номер<TAB>заголовок».
# Коды: 0 — нашла; 1 — пул пуст; 2 — сломался инструмент (gh/jq/сеть):
# «пусто» и «сломано» — разные состояния, смешивать запрещено (fail loud).
free_task() {
  local taken candidates number title
  taken=$(gh pr list --state open --limit 100 --json body \
    | jq -r '[.[].body // "" | scan("#[0-9]+") | ltrimstr("#")] | unique | join(" ")') || return 2
  candidates=$(gh issue list --label task --state open --limit 100 --json number,assignees,title \
    | jq -r '.[] | select((.assignees | length) == 0) | [.number, .title] | @tsv') || return 2
  while IFS=$'\t' read -r number title; do
    [ -n "$number" ] || continue
    case " $taken " in *" $number "*) continue ;; esac
    printf '%s\t%s\n' "$number" "$title"
    return 0
  done <<<"$candidates"
  return 1
}

# ── 1. Выбор задачи ────────────────────────────────────────────────────────────────
if [ -n "$TASK_INPUT" ]; then
  ISSUE_JSON=$(gh issue view "$TASK_INPUT" --json number,title,body,state,assignees,labels) \
    || die "Задача #$TASK_INPUT не читается"
  number=$(jq -r '.number' <<<"$ISSUE_JSON")
  [ "$(jq -r '.state' <<<"$ISSUE_JSON")" = "OPEN" ] || die "Задача #$number закрыта"
  jq -e '.labels[]? | select(.name == "task")' <<<"$ISSUE_JSON" >/dev/null \
    || die "На задаче #$number нет метки task — это не задача пула"
  assignees=$(jq -r '[.assignees[].login] | join(" ")' <<<"$ISSUE_JSON")
  if [ -n "$assignees" ] && [ "$assignees" != "$WORKER_LOGIN" ]; then
    die "Задача #$number занята не воркером (назначено: $assignees)"
  fi
  # Открытый PR на задачу — она уже делается: второй PR контракт не пропустит,
  # а одноимённая ветка на remote сделает пуш невыполнимым.
  taken=$(gh pr list --state open --limit 100 --json body \
    | jq -r '[.[].body // "" | scan("#[0-9]+") | ltrimstr("#")] | unique | join(" ")') \
    || die "не смог прочитать открытые PR (gh/jq/сеть)"
  case " $taken " in *" $number "*)
    die "На задачу #$number уже есть открытый PR — доводить его, а не открывать второй" ;;
  esac
else
  free_rc=0
  ISSUE_LINE=$(free_task) || free_rc=$?
  if [ "$free_rc" -eq 1 ]; then
    echo "Свободных задач нет — воркеру нечего делать, job зелёный"
    exit 0
  fi
  [ "$free_rc" -eq 0 ] || die "выбор свободной задачи сломался (gh/jq/сеть), rc=$free_rc"
  number=${ISSUE_LINE%%$'\t'*}
  ISSUE_JSON=$(gh issue view "$number" --json number,title,body,state,assignees,labels) \
    || die "Задача #$number исчезла между выбором и чтением"
fi
title=$(jq -r '.title' <<<"$ISSUE_JSON")
body=$(jq -r '.body // ""' <<<"$ISSUE_JSON")
echo "Задача #$number: $title"

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
BRANCH="agent/$number-$slug"

{
  cat <<PROMPT
Ты — автономный воркер-агент репозитория edge-harness на одноразовом раннере GitHub Actions (Ubuntu, неинтерактивная среда). Владелец делегировал все решения: вопросов задавать некому — решай сам по правилам ниже.

Текущий каталог — корень клона репозитория. Ты уже на ветке $BRANCH, созданной от свежего origin/main; НЕ переключай и не пересоздавай ветку. git и gh авторизованы под учёткой владельца ($WORKER_LOGIN): git push, комментарии в задачах и создание PR работают. Прямой пуш в main отклоняется молча — пушь ветку и проверяй результат push без -q.

# Задача #$number: $title

$body

# Критерий готовности

$criterion

# Твой маршрут (транспорт уже подготовлен скриптом)
1. Прочитай docs/INDEX.md и относящиеся к задаче спеки/research. Архитектурное предложение — только после docs/research/30-rejected-alternatives.md.
2. Сделай задачу: минимальный правильный дифф, тесты, саморевью (секреты, мёртвый код, расхождение доков с кодом, вызовы переименованных функций по всему репо).
3. Закоммить осмысленными коммитами (git add -A; git commit) и запушь: git push -u origin $BRANCH — проверь вывод пуша. При «main уехал» — git fetch и git rebase origin/main.
4. Открой PR в main: gh pr create --base main --head $BRANCH --title "…" --body-file <файл>. Первая строка тела PR — ровно "#$number". Разделы: Что сделано / Чем доказано (видимый результат, а не «шаг success») / Пост-мерж проверка / Чек-лист. Closes/Fixes/Resolves ЗАПРЕЩЕНЫ — контракт отклонит такой PR.
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

# ── 4. Захват задачи: назначение с проверкой результата ──────────────────────────
gh issue edit "$number" --add-assignee "$WORKER_LOGIN" >/dev/null
now_assigned=$(gh issue view "$number" --json assignees --jq '[.assignees[].login] | join(" ")')
[ "$now_assigned" = "$WORKER_LOGIN" ] \
  || die "Назначение не подтвердилось: сейчас назначено '$now_assigned'"

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

# ── 5. Ветка от свежего origin/main: канонический вход scripts/git/task-branch ───
"$SCRIPT_DIR/../git/task-branch" "$number-$slug"

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

# ── 6. DSH: провайдер, установка (lib), GLM-патч профиля ─────────────────────────
[ -n "${DEEPSEEK_API_KEY:-}" ] || die "DEEPSEEK_API_KEY не задан — DSH не сможет вызвать модель"
export DEEPSEEK_API_KEY
export DEEPSEEK_BASE_URL="${DEEPSEEK_BASE_URL:-https://api.z.ai/api/coding/paas/v4}"

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
dsh_edge_start_drain
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$(cat "$PROMPT_FILE")" \
  >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
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
  telegram_report "worker: задача #$number выполнена, PR открыт: $pr_url" || true
  echo "PR открыт: $pr_url — job зелёный"
  exit 0
fi

# Эскалация playbook (п.2 главных правил) — законный исход: задача ждёт владельца,
# конвейер не сломан, job зелёный.
if jq -e '.labels[]? | select(.name == "blocked")' \
    <(gh issue view "$number" --json labels) >/dev/null; then
  comment=$(cat <<COMMENT
🤖 Автономный воркер эскалировал: то, что нужно для задачи, есть только у владельца.
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

reason="dsh завершился с кодом $rc без открытого PR"
[ "$rc" = "124" ] && reason="DSH уложился в таймаут ${DSH_TIMEOUT_SECS}с, PR не открыт"
comment=$(cat <<COMMENT
🤖 Автономный воркер не справился: $reason.
Задача остаётся назначенной: оркестратор вернёт её в пул через 24 ч без PR,
либо сними назначение вручную. Хвосты логов ниже (секреты замаскированы).

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
