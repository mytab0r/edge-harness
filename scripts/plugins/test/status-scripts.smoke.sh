#!/usr/bin/env bash
# Гвардия класса «удалил определение при живом вызове» (ревью #128, Б1) для
# обёрток статусов журнала: bash -n тело не исполняет и молчит, когда функция
# исчезла из общей библиотеки, а вызов остался. Здесь каждая обёртка
# (plugin_status.sh, integration_status.sh) исполняется ЦЕЛИКОМ дочерним bash
# против НАСТОЯЩЕГО HTTP-сервера: журнал отдаёт прод-форму
# {events:[{seq}], has_more, next_after} и принимает POST /api/events.
#
# Почему настоящий сервер, а не заглушка curl (#1373). Заглушка перекрывала
# бинарь bash-функцией и ПЕРЕСКАЗЫВАЛА флаги: понимала форму `curl -fsS …`
# (тело в stdout) и не понимала `curl -o ФАЙЛ -w '%{http_code}'`. Когда клиент
# журнала перешёл на общую библиотеку scripts/lib/canary_http.sh (отказ обязан
# нести тело ответа), заглушка стала отдавать ТЕЛО там, где код ждал КОД: смок
# покраснел сообщением «Журнал GET: HTTP {"events":…}» + «тело ответа пустое»,
# то есть прод-код был исправен, а сломан был пересказ. Настоящий сервер
# пересказывать нечего: он исполняет ровно тот HTTP, который увидит прод.
# Тот же приём, что scripts/lib/test_hands_http_body_guard.py.
#
# Ассерты: код 0, POST-тело по контракту журнала (task_id, kind, data, seq =
# max+1 — посев с сервера, не с потолка), финальный статус при лежащем журнале
# красит вызов (exit 1), промежуточный — нет (exit 0). Сломанная проводка
# библиотеки = красный smoke, а не тихий пропуск статуса на живом деплое.
#
# Запуск: bash scripts/plugins/test/status-scripts.smoke.sh  (jq, curl, python3)
#
# Доказано мутацией — ИСПОЛНЕНО, не пересказано: вернуть в обёртку api
# (scripts/lib/journal_status.sh) прежний `curl -fsS` вместо canary_http —
# смок краснеет на «лежащий журнал обязан назвать ПРИЧИНУ (сетевой отказ без
# HTTP-ответа)»: в логе остаются только строки самого curl «(7) Failed to
# connect», без метки вызова. База до мутации и после отката — зелёная.
set -euo pipefail

SMOKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SMOKE_DIR/../../.." && pwd)"
TMP="$(mktemp -d)"
CALLLOG="$TMP/calls.log"
export CALLLOG
: >"$CALLLOG"

for tool in jq curl python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "::error::$tool недоступен — смок обёрток статусов проверяет НАСТОЯЩИЙ HTTP и без него не проверяет ничего" >&2
    exit 1
  }
done

JOURNAL_PID=""
stop_journal() {
  [ -n "$JOURNAL_PID" ] && kill "$JOURNAL_PID" 2>/dev/null || true
  JOURNAL_PID=""
}
cleanup() { stop_journal; rm -rf "$TMP"; }
trap cleanup EXIT

# Стаб-журнал: прод-форма ответов /api/events, сценарий выбирается переменной.
# Пишет каждый вызов в $CALLLOG строкой «<МЕТОД> <путь>» и, для POST, телом
# запроса как есть — по нему тест сверяет контракт события.
cat >"$TMP/journal.py" <<'PY_EOF'
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SCENARIO = os.environ["SMOKE_SCENARIO"]
CALLLOG = os.environ["CALLLOG"]

PAGE_TAIL = {"events": [{"id": 1, "seq": 3, "kind": "plugin_status", "data": {}}],
             "has_more": False, "next_after": 1}
PAGE_HEAD = {"events": [{"id": 1, "seq": 5, "kind": "plugin_status", "data": {}}],
             "has_more": True, "next_after": 1}
PAGE_SECOND = {"events": [{"id": 2, "seq": 9, "kind": "plugin_status", "data": {}}],
               "has_more": False, "next_after": 2}


class Handler(BaseHTTPRequestHandler):
    def _record(self, method, body):
        with open(CALLLOG, "a", encoding="utf-8") as log:
            log.write("%s %s\n" % (method, self.path))
            if body:
                log.write(body + "\n")

    def _send(self, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 — имя требует BaseHTTPRequestHandler
        self._record("GET", "")
        if SCENARIO == "paged":
            self._send(PAGE_HEAD if "after=0" in self.path else PAGE_SECOND)
        elif SCENARIO == "endless":
            self._send(PAGE_HEAD)
        else:
            self._send(PAGE_TAIL)

    def do_POST(self):  # noqa: N802 — имя требует BaseHTTPRequestHandler
        length = int(self.headers.get("content-length") or 0)
        self._record("POST", self.rfile.read(length).decode("utf-8"))
        self._send({})

    def log_message(self, *args):
        pass


httpd = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[1], "w", encoding="utf-8") as port_file:
    port_file.write(str(httpd.server_port))
httpd.serve_forever()
PY_EOF

start_journal() { # $1 — сценарий (tail|paged|endless)
  stop_journal
  rm -f "$TMP/port"
  SMOKE_SCENARIO="$1" python3 "$TMP/journal.py" "$TMP/port" &
  JOURNAL_PID=$!
  local waited=0
  while [ ! -s "$TMP/port" ]; do
    waited=$((waited + 1))
    if [ "$waited" -gt 200 ]; then
      echo "::error::стаб-журнал не поднялся за 10 с — смок не проверил бы ничего" >&2
      exit 1
    fi
    sleep 0.05
  done
  JOURNAL_URL="http://127.0.0.1:$(cat "$TMP/port")"
}

run_wrapper() { # $1 — скрипт-обёртка, остальные — env-присваивания
  local script="$1"
  shift
  env "$@" HARNESS_URL="$JOURNAL_URL" HANDS_TOKEN="hands-token-smoke" \
    bash "$REPO/scripts/plugins/$script"
}

post_bodies() { # POST-записи из журнала вызовов (тело jq печатает многострочно)
  sed -n '/^POST/,$p' "$CALLLOG" || true
}

# ── plugin_status.sh: контракт события ────────────────────────────────────────
start_journal tail
run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=ready FINAL=1 DETAIL="0.1.1" >/dev/null
body=$(post_bodies)
for marker in '"task_id": "plugin:hello"' '"kind": "plugin_status"' '"plugin": "hello"' '"state": "ready"' '"detail": "0.1.1"' '"seq": 4'; do
  if ! grep -q -- "$marker" <<<"$body"; then
    echo "::error::plugin_status: в POST-теле нет $marker — контракт журнала нарушен" >&2
    exit 1
  fi
done

# ── integration_status.sh: контракт события ───────────────────────────────────
: >"$CALLLOG"
run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=not_configured FINAL=1 DETAIL="нет секретов: JIRA_API_TOKEN" >/dev/null
body=$(post_bodies)
for marker in '"task_id": "integration:jira"' '"kind": "integration_status"' '"integration": "jira"' '"state": "not_configured"' 'нет секретов: JIRA_API_TOKEN' '"seq": 4'; do
  if ! grep -q -- "$marker" <<<"$body"; then
    echo "::error::integration_status: в POST-теле нет $marker — контракт журнала нарушен" >&2
    exit 1
  fi
done

# ── Многостраничный посев seq: первая страница отдаёт has_more=true, ──────────
# вторая (после after) — хвост. seq события берётся как max по ОБЕИМ страницам.
: >"$CALLLOG"
start_journal paged
run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=deploying FINAL=0 >/dev/null
if ! grep -q -- '"seq": 10' "$CALLLOG"; then
  echo "::error::многостраничный посев: seq не взял максимум с обеих страниц (ожидается 10 = max(5,9)+1)" >&2
  exit 1
fi

# ── История длиннее потолка посева — честная причина, а не «журнал недоступен» ─
# Журнал отвечает ВСЕГДА has_more=true: потолок 16 страниц, ретраи не расходуются.
: >"$CALLLOG"
start_journal endless
if run_wrapper plugin_status.sh PLUGIN_ID=hello STATE=ready FINAL=1 >/dev/null 2>"$TMP/seed-msg.txt"; then
  echo "::error::история длиннее потолка посева: финальный статус обязан красить (fail loud)" >&2
  exit 1
fi
if ! grep -q "длиннее потолка посева" "$TMP/seed-msg.txt"; then
  echo "::error::исчерпание страниц должно называться «длиннее потолка посева», а не недоступностью" >&2
  exit 1
fi
if grep -q "Журнал недоступен" "$TMP/seed-msg.txt"; then
  echo "::error::исчерпание страниц неверно классифицировано как «журнал недоступен»" >&2
  exit 1
fi

# ── Финальный статус при лежащем журнале красит, промежуточный — нет ──────────
# «Лежит» воспроизводится честно: адрес того же стаба ПОСЛЕ его остановки —
# curl упирается в закрытый порт (ECONNREFUSED), ровно как на мёртвой морде.
stop_journal
sleep() { :; }       # ретраи не тянут прогон: механика ретраев проверена выше
export -f sleep
if run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=ready FINAL=1 >/dev/null 2>"$TMP/down-final.txt"; then
  echo "::error::final: лежащий журнал обязан красить вызов (fail loud), а не возвращать 0" >&2
  exit 1
fi
if ! grep -q "запрос не состоялся" "$TMP/down-final.txt"; then
  echo "::error::лежащий журнал обязан назвать ПРИЧИНУ (сетевой отказ без HTTP-ответа), а не только итог (#1371/#1373)" >&2
  cat "$TMP/down-final.txt" >&2
  exit 1
fi
if ! run_wrapper integration_status.sh INTEGRATION_ID=jira STATE=deploying FINAL=0 >/dev/null 2>&1; then
  echo "::error::промежуточный статус при лежащем журнале не должен красить вызов" >&2
  exit 1
fi

echo "status-scripts smoke: ок — обе обёртки держат контракт журнала и градацию fail loud"
