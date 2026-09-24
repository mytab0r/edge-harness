#!/usr/bin/env bash
# Гвардия класса #1510: **холостой прогон воркера не имеет права платить
# аккаунтной квотой Durable Objects.**
#
# Отказ ДО работы агента (цепочка провайдеров не ответила) возвращает задачу в
# пул немедленно — и до этой правки ВСЁ РАВНО сливал транскрипт в dsh-edge, то
# есть записывал ход работы, которой не было. Приём транскрипта идёт в Durable
# Object, а лимит rows_read (5 млн/сутки) общий на аккаунт с мордой.
#
# Замер цены (2026-09-23/24, цепочка мертва — #1502): 18 прогонов worker.yml за
# сутки, каждый падает до агента за секунду и сливает транскрипт; namespace
# dsh-edge вычитал 5 700 194 строки (114% суточного лимита), после чего морда
# стала отдавать 1101 на каждый /api/*, и кнопка решения владельца в Telegram
# умерла вместе с ней — при том что сам харнес прочитал за те сутки 198 строк.
#
# ПОЧЕМУ СТЕНД С НАСТОЯЩИМ СЕРВЕРОМ, А НЕ ЗАГЛУШКОЙ curl. AGENTS.md, «Заглушка
# внешнего инструмента — это пересказ, и она ломается на исправном коде»
# (#1373/PR #1380): подменённый curl понимает ту форму вызова, под которую
# написан, и краснеет на исправном коде, как только форма меняется. Здесь
# поднимается НАСТОЯЩИЙ http.server и зовётся НАСТОЯЩИЙ dsh_edge_ingest из
# scripts/lib/dsh-edge-session.sh. Проверяется ВИДИМЫЙ РЕЗУЛЬТАТ — дошёл ли до
# сервера хоть один POST, — а не то, что «функция не вызвана»: последнее
# совпадает и когда вызов просто переименовали.
#
# Мутация (исполнена, дословный вывод в теле PR): если снять ветку
# WORKER_SKIP_TRANSCRIPT из scripts/worker/task.sh, сценарий 1 краснеет —
# сервер получает POST там, где его быть не должно.
#
# Запуск: bash scripts/worker/test/idle-run-skips-transcript.smoke.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TASK_SH="$REPO_ROOT/scripts/worker/task.sh"

WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "::error::SMOKE(idle-run-skips-transcript): $*" >&2; exit 1; }
note() { echo "SMOKE(idle-run-skips-transcript): $*"; }

# ── извлечение НАСТОЯЩЕГО блока транскрипта из task.sh ──────────────────────
# Тест исполняет прод-код, а не свою копию его логики (тот же приём, что
# provider-exhaustion-exit-code.smoke.sh, класс #476).
SNIPPET="$WORK/transcript-block.sh"
awk '
  /^# Отказ ДО работы агента — транскрипт НЕ льём/ { grab = 1 }
  grab { print }
  grab && /^fi$/ { exit }
' "$TASK_SH" >"$SNIPPET"
[ -s "$SNIPPET" ] || fail "блок транскрипта не найден в $TASK_SH — якорь-комментарий переименован?"
# Гвардия дрейфа якоря: без неё вырезанный кусок мог бы оказаться чужим, и
# тест красил бы CI зелёным, ничего не проверяя (fail loud, не silent-wrong).
grep -q 'WORKER_SKIP_TRANSCRIPT' "$SNIPPET" \
  || fail "в извлечённом блоке нет WORKER_SKIP_TRANSCRIPT — фикс #1510 снят или переехал"
grep -q 'dsh_edge_drain_spool hard' "$SNIPPET" \
  || fail "в извлечённом блоке нет жёсткого дрена — якорь снял не тот кусок"

# ── НАСТОЯЩИЙ сервер вместо морды ───────────────────────────────────────────
# Пишет по строке на каждый POST. «Недоступность» здесь не нужна вовсе: нас
# интересует ровно обратное — что запрос НЕ пришёл на живой, готовый принять.
HITS="$WORK/hits.log"
: >"$HITS"
cat >"$WORK/server.py" <<'PYEOF'
import http.server, os, sys

HITS = os.environ["HITS"]


class Handler(http.server.BaseHTTPRequestHandler):
    def _record(self):
        with open(HITS, "a", encoding="utf-8") as handle:
            handle.write(f"{self.command} {self.path}\n")

    def do_POST(self):
        self._record()
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"accepted":1}')

    def do_GET(self):
        self._record()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
PYEOF
HITS="$HITS" python3 "$WORK/server.py" >"$WORK/port.txt" 2>"$WORK/server.err" &
SERVER_PID=$!
for _ in $(seq 1 50); do
  PORT="$(cat "$WORK/port.txt" 2>/dev/null || true)"
  [ -n "$PORT" ] && break
  sleep 0.1
done
[ -n "${PORT:-}" ] || fail "стенд не поднялся: $(head -3 "$WORK/server.err" 2>/dev/null)"
note "стенд морды слушает 127.0.0.1:$PORT (настоящий http.server, не заглушка curl)"

# ── окружение блока: настоящая библиотека клиента морды ─────────────────────
export DSH_EDGE_URL="http://127.0.0.1:$PORT"
# Стенд ключ не проверяет вовсе — переменная нужна клиенту только чтобы
# собрать заголовок. Значение короткое и очевидно ненастоящее нарочно:
# длинный «похожий на ключ» литерал справедливо ловит гвардия секретов
# check_pr.py (SECRET_PATTERNS), и обходить её подбором длины — значит
# учить следующего агента обходу вместо честного placeholder'а.
export DSH_EDGE_ACCESS_KEY="smoke"
export WORK
export SPOOL_FILE="$WORK/spool.ndjson"
printf '{"type":"text","text":"событие %s"}\n' 1 2 3 >"$SPOOL_FILE"

# Обе библиотеки — те же и в том же порядке, что подключает сам task.sh
# (строки 133 и 136): dsh-ci.sh несёт redact(), которым клиент морды чистит
# батч от секретов. Подменять её здесь нельзя — это был бы ровно тот пересказ
# внешнего инструмента, от которого предостерегает AGENTS.md.
run_block() { # $1 = WORKER_TASK_FAILURE_REASON
  WORKER_TASK_FAILURE_REASON="$1" bash -c '
    set -euo pipefail
    source "$1"                      # dsh-ci.sh: redact() и общие помощники
    source "$2"                      # настоящий клиент морды
    DSH_EDGE_MORDA_AVAILABLE=1
    DSH_EDGE_SESSION_ID=smoke-session
    rc=1
    source "$3"                      # настоящий блок из task.sh
  ' bash "$REPO_ROOT/scripts/lib/dsh-ci.sh" \
    "$REPO_ROOT/scripts/lib/dsh-edge-session.sh" "$SNIPPET" >>"$WORK/out.log" 2>&1
}

# ── 1) холостой прогон: НИ ОДНОГО запроса к морде ───────────────────────────
: >"$HITS"
rc_run=0
run_block all_providers_exhausted || rc_run=$?
hits=$(wc -l <"$HITS" | tr -d ' ')
[ "$hits" = "0" ] || fail "1) отказ до работы агента (all_providers_exhausted) обязан НЕ трогать морду, а стенд получил $hits запрос(ов): $(head -3 "$HITS" | tr '\n' ' ') — холостой прогон снова платит аккаунтной квотой rows_read (#1510)"
grep -q "Транскрипт не отправлен намеренно" "$WORK/out.log" \
  || fail "1) пропуск транскрипта не объявлен в логе — читатель не отличит «не лили» от «морда отказала» (fail loud, не silent-wrong)"
note "1) холостой прогон: запросов к морде 0, пропуск объявлен вслух ✓"

# ── 2) тот же блок при УСПЕХЕ льёт транскрипт ───────────────────────────────
# Без этой половины фикс выродился бы в «транскрипт не льётся никогда», и
# витрина хода работы умерла бы молча. Обе проверки нужны.
: >"$HITS"
: >"$WORK/out.log"
rc_run=0
run_block "" || rc_run=$?
hits=$(wc -l <"$HITS" | tr -d ' ')
[ "$hits" -gt 0 ] || fail "2) при успешном прогоне (пустой класс отказа) транскрипт обязан уехать в морду, а стенд не получил ни одного запроса — фикс #1510 выключил витрину целиком"
note "2) успешный прогон: запросов к морде $hits ✓"

# ── 3) прочие классы отказа не задеты ───────────────────────────────────────
# prompt_too_long — НАШ отказ: он красный, его серию останавливает
# предохранитель диспатча (#1315), цикла нет, транскрипт остаётся диагностикой.
: >"$HITS"
: >"$WORK/out.log"
rc_run=0
run_block prompt_too_long || rc_run=$?
hits=$(wc -l <"$HITS" | tr -d ' ')
[ "$hits" -gt 0 ] || fail "3) prompt_too_long (НАШ отказ, красный прогон) обязан оставлять транскрипт — он диагностика, а не холостой цикл; стенд не получил ни одного запроса"
note "3) prompt_too_long: транскрипт на месте, запросов $hits ✓"

note "все сценарии целы"
