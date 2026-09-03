#!/usr/bin/env bash
# Клиент рук (dsh-in-job слайс 1 + runner-sessions #119 + канал конвейера #86):
# текст задачи из payload диспетча → DSH headless → сессия в морде dsh-edge.
# Два канала морды, оба через scripts/lib/dsh-edge-session.sh (ingest, патч 0004):
#   - сессия раннера harness-<id>: полный транскрипт хода работы (спул стримера);
#   - сессия конвейера harness-pipeline: жизненный цикл job (job_start/bootstrap/
#     agent_answer/agent_error/job_end) — заменяет журнал edge-harness (#86),
#     читается назад через /api/harness/events (патч 0005).
# Правила: fail loud, без silent-wrong; улика прогресса — транскрипт сессии в
# морде; ноль событий транскрипта при успешном прогоне — красный job (#119).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Пины версий/целостности, установка и redact — единственное место правды:
# scripts/lib/dsh-ci.sh (общее с автономным воркером).
# shellcheck source=scripts/lib/dsh-ci.sh
source "$SCRIPT_DIR/../lib/dsh-ci.sh"
# Шов сессии раннера в морду (#119): логин, begin, дрен спула в ingest, архив.
# shellcheck source=scripts/lib/dsh-edge-session.sh
source "$SCRIPT_DIR/../lib/dsh-edge-session.sh"
# Канал конвейера (#86): жизненный цикл job → сессию harness-pipeline.
# shellcheck source=scripts/lib/dsh-edge-pipeline.sh
source "$SCRIPT_DIR/../lib/dsh-edge-pipeline.sh"
# Аренда задачи (#121): claim/release/locks — единственный вход в работу.
# shellcheck source=scripts/lib/lease.sh
source "$SCRIPT_DIR/../lib/lease.sh"

REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

DSH_TIMEOUT_SECS="${DSH_TIMEOUT_SECS:-1500}"
DRAIN_INTERVAL_SECS="${DRAIN_INTERVAL_SECS:-1}"

: "${TASK_ID:?TASK_ID не задан (repository_dispatch payload или manual-<run_id>)}"
: "${TASK_TEXT:?TASK_TEXT не задан (client_payload.task_text диспетча или inputs.task_text ручного прогона)}"
# Одно место правды — vars.DEEPSEEK_BASE_URL/DEEPSEEK_MODEL репозитория (#153):
# зашитых фолбэков на конкретный эндпоинт/модель здесь больше нет. Проверяем
# в блоке обязательных переменных — ДО dsh_edge_login и создания сессий в морде:
# иначе конфиг-ошибка даёт пустые сессии в UI морды и задачу,
# помеченную провалом, вместо честного «не сконфигурировано».
dsh_require_provider_env || exit 1
JOB_ID="${JOB_ID:-hands-${GITHUB_RUN_ID:-local}-$$}"
WORK="${RUNNER_TEMP:-/tmp}/dsh-hands"
mkdir -p "$WORK"
ANSWER_FILE="$WORK/answer.txt"
ERR_FILE="$WORK/stderr.txt"
SPOOL_FILE="$WORK/session-stream.ndjson"      # NDJSON-спул плагина dsh-hands-streamer
: >"$ANSWER_FILE"; : >"$ERR_FILE"

# ── Жизненный цикл job → канал конвейера (#86) ────────────────────────────────────
# Журнал edge-harness списан: события жизненного цикла уходят в сессию
# harness-pipeline морды (статусы видны в чате). dsh_edge_ingest применяет redact
# на пути в морду; джобный канал, как и транскрипт, требует живой морды.
add_event() { # kind json_data
  # БЕЗ подshell: env-префикс виден внутри функции и не течёт после (проверено),
  # а PIPELINE_LOGGED_IN/PIPELINE_SESSION_READY библиотеки живут в этом шелле —
  # логин и создание сессии конвейера идут ОДИН раз на job, не на каждое событие.
  if ! PIPELINE_HARD=1 FINAL="${FINAL:-0}" TASK_ID="$TASK_ID" KIND="$1" \
      DATA_JSON="${2:-null}" SOURCE="job" pipeline_post_event; then
    echo "::error::Событие $1 не доехало до канала конвейера — улики прогона потеряны (#86)" >&2
    return 1
  fi
}

# ── Транскрипт сессии: дрен спула в морду (#119) ──────────────────────────────────
# Спул плагина — единственный источник; дрен живёт в scripts/lib/dsh-edge-session.sh
# и постит батчи строк спула в DSH-сессию (POST /api/sessions/:id/ingest).

JOB_ENDED=0
# Единственная точка финального статуса. job_end уходит ТОЛЬКО после того, как
# канал принял его: зелёный job с непринятым job_end — тот же silent-wrong.
# FINAL=1 (градация fail loud библиотеки канала) временно, на время вызова:
# `VAR=x fn` виден и вложенному pipeline_post_event, но не течёт дальше.
post_job_end() { # result
  FINAL=1 add_event "job_end" "{\"result\":\"$1\", \"job_id\":\"$JOB_ID\"}"
  JOB_ENDED=1
}

cleanup() {
  dsh_edge_stop_drain
  if [ "$JOB_ENDED" -eq 0 ]; then
    dsh_edge_drain_spool hard || true   # улики — до job_end; упавший дрен не отменяет финальный статус
    # В трапе отказ канала не должен маскировать код выхода: каждый пост — с || true,
    # честность обеспечивает post_job_end (FINAL=1) на штатном пути, а не здесь.
    add_event "agent_error" '{"stderr":"job завершён до финала (отмена/ошибка среды)"}' || true
    post_job_end "fail" || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT   # graceful-семантика dsh: SIGINT → 130
trap 'exit 143' TERM

echo "Задача $TASK_ID: текст из payload диспетча ($(wc -c <<<"$TASK_TEXT") байт)"

# ── 1a. Аренда задачи (#121): заказ «поработай над issue-N» берётся только ────────
# через атомарный claim. Без замка морда-агент мог заказать работу по задаче,
# которую уже делает воркер, — два исполнителя на одной работе. Отказ —
# зелёный no-op job'а (контракт #121), но канал конвейера получает честный финал
# task_busy + job_end: задача не должна висеть вечно, а «done»
# означал бы ложь «работа сделана». Задачи без issue (manual-*) — вне пула,
# аренды не имеют. Поломка утилиты — громкий красный job: «инструмент сломан»
# и «задача занята» — разные состояния.
if [[ "$TASK_ID" =~ ^issue-([0-9]+)$ ]]; then
  # CLAIM_ACTOR обязан быть валидным логином (назначение идёт им): не
  # переопределяем — current_actor() возьмёт GITHUB_ACTOR (аккаунт,
  # инициировавший dispatch). Канал для следа в задаче — CLAIM_VIA.
  export CLAIM_VIA="hands $TASK_ID (run ${GITHUB_RUN_ID:-local})"
  claim_out="$(lease_cli claim "${BASH_REMATCH[1]}" 2>&1)" && claim_rc=0 || claim_rc=$?
  if [ "$claim_rc" -eq 1 ]; then
    echo "Задача #${BASH_REMATCH[1]} занята другим исполнителем — зелёный no-op: $claim_out"
    add_event "agent_error" \
      "$(jq -n --arg t "$claim_out" '{error: "task_busy", detail: $t}')"
    post_job_end "fail"
    exit 0
  fi
  if [ "$claim_rc" -ne 0 ]; then
    echo "::error::claim_task сломался (rc=$claim_rc): $claim_out" >&2
    exit 1
  fi
  echo "Аренда взята: $claim_out"
fi

# Токен аренды больше не нужен никому ниже, включая DSH: снимается сразу после
# блока аренды и до любого выхода из скрипта. Раньше блока снимать нельзя —
# claim в проде авторизуется именно GH_RUN_TOKEN.
unset GH_RUN_TOKEN

# ── 1b. Канал конвейера и сессия раннера в морде ──────────────────────────────────
# Первый вызов pipeline_post_event (job_start ниже) логинится в морду и создаёт
# сессию конвейера harness-pipeline. Затем — сессия раннера (#119): создать/
# переиспользовать и назвать. Отказ громкий: без сессии ход работы владельцу
# не виден, job красный. «Не настроено» и «сломано» — разные сообщения.
if [[ "$TASK_ID" =~ ^issue-([0-9]+)$ ]]; then
  HARNESS_SID="harness-${BASH_REMATCH[1]}"
  HARNESS_TITLE="#${BASH_REMATCH[1]}: $(head -n1 <<<"$TASK_TEXT" | cut -c1-160)"
else
  slug=$(printf '%s' "$TASK_ID" | tr '[:upper:]' '[:lower:]' | tr -cs 'A-Za-z0-9' '-' \
  | sed -e 's/-\{2,\}/-/g' -e 's/^-*//' -e 's/-*$//' | cut -c1-48)
  HARNESS_SID="harness-${slug:-manual}"
  HARNESS_TITLE="$(head -n1 <<<"$TASK_TEXT" | cut -c1-160)"
fi
add_event "job_start" "{\"job_id\":\"$JOB_ID\"}" \
  || { echo "::error::Нет доступа к каналу конвейера морды dsh-edge — job красный (#86)" >&2; exit 1; }
dsh_edge_session_begin "$HARNESS_SID" "$HARNESS_TITLE" >/dev/null \
  || { echo "::error::Сессия $HARNESS_SID не создана в морде — ход работы останется невидимым (#119)" >&2; exit 1; }
export DSH_EDGE_SESSION_ID="$HARNESS_SID"
echo "Сессия морды: $HARNESS_SID — «$HARNESS_TITLE»"

# ── 2. Провайдер: проверка выше (блок обязательных переменных) — здесь только export ──
# Профиль headless — pnpm-workspace: `pnpm add` внутри требует явного
# подтверждения root (иначе ERR_PNPM_ADDING_TO_ROOT, живой прогон 2026-08-30).
export npm_config_ignore_workspace_root_check=true
export DEEPSEEK_API_KEY DEEPSEEK_BASE_URL DEEPSEEK_MODEL

# ── 3. Установка DSH: tarball + сверка целостности (supply-chain пин) ─────────────
PKGS="$WORK/pkgs"
dsh_install "$PKGS"
dsh --version || true

# ── 3a. Модель и лимит ответа — settings-слой профиля, ДО монтажа плагина ─────────
# Порядок важен: --dump-config в 3b обязан доказывать монтаж плагина поверх
# ИТОГОВОГО патча профиля — ровно той конфигурации, с которой стартует dsh,
# а не промежуточной.
# Адаптер dsh-llm-deepseek читает из env только DEEPSEEK_BASE_URL/DEEPSEEK_API_KEY,
# модель живёт в settings namespace agent-default-model (проверено живым прогоном:
# без патча уходит deepseek-v4-flash, GLM отвечает modelCode does not exist;
# maxTokens-дефолт адаптера 256000 выше потолка GLM 131072 → INVALID_REQUEST).
dsh_patch_profile headless

# ── 3b. Плагин стрима: bundle-механизм профиля, факт монтажа доказывается здесь ───
# (dsh-streaming, проверка допущений 0: `dsh plugin add` + `--dump-config`
# подтверждены живьём). tarball собирается из этого же чекаута — отдельный пин
# не нужен, версия приезжает вместе с клиентом. Плагин БЕЗ сети: транспорт и
# ретраи — только здесь; pnpm-форвардер — штатная механика `dsh plugin`.
# Проверка идёт ПОСЛЕ записи модельного патча (3a): dump-config доказывает
# совместный слой «модель + плагин». initProfile пишет файлы профиля только
# при отсутствии — наш патч при `dsh plugin add` не перезаписывается
# (docs/research/10-dsh-architecture.md, замер 2026-08-30).
command -v pnpm >/dev/null || { echo "::error::pnpm не найден — dsh plugin add без него не работает" >&2; exit 1; }
PLUGIN_TGZ="$WORK/dsh-hands-streamer.tgz"
npm pack "$REPO_DIR/scripts/dsh-hands-streamer" --pack-destination "$WORK" >/dev/null
mv "$WORK"/dsh-hands-streamer-*.tgz "$PLUGIN_TGZ"
dsh plugin --profile headless add "$PLUGIN_TGZ"
dsh --profile headless --dump-config >"$WORK/dump-config.txt" 2>&1 \
  || { echo "::error::dsh --dump-config упал — профиль headless не собирается" >&2; exit 1; }
grep -q '^- id: hands-streamer$' "$WORK/dump-config.txt" \
  || { echo "::error::плагин hands-streamer не смонтировался: --dump-config без его строки; стрим событий невозможен" >&2; exit 1; }
echo "Плагин hands-streamer смонтирован в профиль headless (вместе с модельным патчем)"
add_event "bootstrap" "$(jq -n \
  --arg dsh "$DSH_VERSION" --arg hl "$DSH_HEADLESS_VERSION" \
  --arg node "$(node --version)" --arg model "$DSH_MODEL" \
  --argjson mt "$DSH_MAX_TOKENS" \
  '{dsh: $dsh, dsh_headless: $hl, node: $node, integrity: "verified", model: $model, max_tokens: $mt, stream_plugin: "hands-streamer"}')"

# ── 4. Прогон: one-shot dsh-headless над этим репозиторием ────────────────────────
# cwd ДО старта становится корнем воркспейса и после не меняется (контракт dsh).
cd "${GITHUB_WORKSPACE:-$WORK}"

# Спул стрима: путь задаётся плагину через env до старта dsh; чистый прогон не
# должен дочитывать старьё от предыдущей попытки. Курсор дрена — единственный
# владелец границы «принято мордой» (ретрай батча идёт от позиции, не от содержимого).
rm -f "$SPOOL_FILE" "$SPOOL_FILE.stats.json"
export HANDS_SPOOL="$SPOOL_FILE"
dsh_edge_start_drain

DSH_START_TS=$(date -u +%s)
set +e
timeout "$DSH_TIMEOUT_SECS" dsh --profile headless "$TASK_TEXT" >"$ANSWER_FILE" 2>"$ERR_FILE"
rc=$?
set -e
DSH_SECS=$(( $(date -u +%s) - DSH_START_TS ))

# Финальный drain — жёсткий и ДО финального статуса: транскрипт сессии в морде
# обязан обгонять job_end в канале конвейера.
dsh_edge_stop_drain
dsh_edge_drain_spool hard || { echo "::error::Хвост транскрипта не принят мордой" >&2; exit 1; }
drained_lines=$(cat "$DSH_EDGE_DRAIN_CURSOR" 2>/dev/null || echo 0)

# ── 5. Канал конвейера: ответ и улики ─────────────────────────────────────────────
ANSWER=$(tail -c 60000 "$ANSWER_FILE" | redact)
add_event "agent_answer" \
  "$(jq -n --arg t "$ANSWER" --argjson secs "$DSH_SECS" '{text: $t, elapsed_s: $secs}')"

# Громкий отказ «стрим не доставил»: успешный прогон с пустым транскриптом
# морды — молчаливая деградация слоя доказательств, job обязан краснеть.
# «Спул не создан вовсе» — другой отказ: плагин не смонтировался ≠ событий не было.
if [ "$rc" -eq 0 ]; then
  if [ ! -f "$SPOOL_FILE" ]; then
    add_event "agent_error" '{"error":"stream_plugin_not_mounted","stderr":"прогон успешен, а спул стрима не создан — плагин hands-streamer не писал, хотя dump-config его смонтировал"}'
    post_job_end "fail"
    echo "::error::Спул стрима не создан при успешном прогоне — плагин не работал" >&2
    exit 1
  fi
  if [ "$drained_lines" -eq 0 ]; then
    add_event "agent_error" '{"error":"stream_no_events","stderr":"прогон успешен, а в сессию морды не доставлено ни одного события — транскрипт пуст (#119)"}'
    post_job_end "fail"
    echo "::error::Ноль событий в сессии морды при успешном прогоне — стрим не доставил событий" >&2
    exit 1
  fi
fi

# Статистика плагина (счётчики отброшенного, capped) — одна строка stream_note
# на прогон вместо строк на каждое событие. capped — warn: деградация объявлена.
if [ -f "$SPOOL_FILE.stats.json" ]; then
  jq -e . "$SPOOL_FILE.stats.json" >/dev/null 2>&1 || { echo '{"accepted":null,"capped":false,"note":"stats повреждён"}' >"$SPOOL_FILE.stats.json"; }
  add_event "stream_note" "$(jq -n \
    --slurpfile s "$SPOOL_FILE.stats.json" \
    --argjson drained "${drained_lines:-0}" \
    '{level: (if ($s[0].capped // false) then "warn" else "debug" end),
      note: "статистика стрима сессии",
      spool: {accepted: $s[0].accepted, drained: $drained, dropped: ($s[0].dropped // {}), capped: ($s[0].capped // false)}}')"
fi

if [ "$rc" -eq 0 ]; then
  post_job_end "ok"
else
  ERRTEXT=$(tail -c 8000 "$ERR_FILE" | redact)
  add_event "agent_error" \
    "$(jq -n --arg t "$ERRTEXT" --argjson code "$rc" '{stderr: $t, exit_code: $code}')"
  post_job_end "fail"
  echo "::error::dsh завершился с кодом $rc" >&2
  exit 1
fi
