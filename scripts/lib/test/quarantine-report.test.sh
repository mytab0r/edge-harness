#!/usr/bin/env bash
# Поведенческая гвардия quarantine_report (issue #1379).
#
# Стенд поднимает НАСТОЯЩИЙ http.server и зовёт НАСТОЯЩИЙ curl через
# canary_http; «маршрут недоступен» воспроизводится закрытым портом, а не
# подменённой функцией. Это прямое требование AGENTS.md («Заглушка внешнего
# инструмента — это пересказ, и она ломается на исправном коде», #1373/PR
# #1380): подменённый curl понимает ту форму вызова, под которую написан, и
# краснеет на следующем изменении формы при исправном прод-коде.
#
# Проверяется РАЗЛИЧЕНИЕ, ради которого задача заведена: «карантин пуст» и
# «состав прочитать не смогли» — разные исходы. Тест требует, чтобы второй
# никогда не выглядел как первый.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
source "$ROOT/scripts/lib/canary_http.sh"
source "$ROOT/scripts/lib/quarantine_report.sh"

TMP=$(mktemp -d)
JAR="$TMP/cookies"
: > "$JAR"
SERVER_PID=""
failures=0

cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && wait "$SERVER_PID" 2>/dev/null
  rm -rf "$TMP"
}
trap cleanup EXIT

# Поднимает сервер, отдающий $1 на /api/quarantine. Печатает базовый URL.
start_server() {
  local body=$1 port
  printf '%s' "$body" > "$TMP/body.json"
  port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
  # stdout/stderr сервера уводятся в файл СОЗНАТЕЛЬНО: функция зовётся из
  # `base=$(start_server …)`, и фоновый процесс, держащий stdout подстановки,
  # не даёт ей вернуться — подстановка ждёт закрытия дескриптора, а сервер его
  # не закрывает никогда. Поймано исполнением: первая редакция теста висела.
  python3 - "$port" "$TMP/body.json" >"$TMP/server.log" 2>&1 <<'PY' &
import sys, http.server
port, path = int(sys.argv[1]), sys.argv[2]
payload = open(path, "rb").read()
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/quarantine":
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
PY
  SERVER_PID=$!
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "http://127.0.0.1:$port/api/quarantine" && break
    sleep 0.1
  done
  echo "http://127.0.0.1:$port"
}

stop_server() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null && wait "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
}

check() {
  local name=$1 expected=$2 actual=$3
  if [ "$expected" = "$actual" ]; then
    echo "  ok: $name"
  else
    echo "  ПРОВАЛ: $name — ожидалось «$expected», получено «$actual»" >&2
    failures=$((failures + 1))
  fi
}

# ── 1. Пустой карантин: честное «пусто», возврат 0 ───────────────────────────
base=$(start_server '{"ok":true,"quarantined":[]}')
out=$(quarantine_report "$base" "$JAR" 2>&1) && rc=0 || rc=$?
check "пустой карантин: возврат 0" "0" "$rc"
case "$out" in
  *"карантин пуст"*) echo "  ok: пустой карантин назван пустым" ;;
  *) echo "  ПРОВАЛ: пустой карантин не назван пустым: $out" >&2; failures=$((failures + 1)) ;;
esac
stop_server

# ── 2. Непустой: печатает id и причину КАЖДОЙ сессии, но НЕ красит шаг ───────
base=$(start_server '{"ok":true,"quarantined":[
  {"id":"sess-a","storedVersion":2,"reason":"no migration path to v3","observedAt":1},
  {"id":"sess-b","storedVersion":1,"reason":"corrupt header","observedAt":2}]}')
out=$(quarantine_report "$base" "$JAR" 2>&1) && rc=0 || rc=$?
check "непустой карантин: возврат 0 (карантин — не поломка)" "0" "$rc"
for token in "sess-a" "sess-b" "no migration path to v3" "corrupt header"; do
  case "$out" in
    *"$token"*) echo "  ok: в логе есть «$token»" ;;
    *) echo "  ПРОВАЛ: в логе нет «$token»: $out" >&2; failures=$((failures + 1)) ;;
  esac
done
case "$out" in
  *"карантин пуст"*) echo "  ПРОВАЛ: непустой карантин назван пустым" >&2; failures=$((failures + 1)) ;;
  *) echo "  ok: непустой карантин пустым не назван" ;;
esac
stop_server

# ── 3. Маршрут недоступен (ЗАКРЫТЫЙ ПОРТ): отказ, и это НЕ «пусто» ───────────
# Главная сцена задачи: молчащий маршрут не имеет права выглядеть как пустой
# карантин. Порт занимаем и сразу освобождаем — адрес заведомо никем не слушается.
dead_port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')
out=$(quarantine_report "http://127.0.0.1:$dead_port" "$JAR" 2>&1) && rc=0 || rc=$?
check "закрытый порт: возврат 1" "1" "$rc"
case "$out" in
  *"карантин пуст"*) echo "  ПРОВАЛ: недоступный маршрут назван пустым карантином" >&2; failures=$((failures + 1)) ;;
  *"НЕ прочитан"*) echo "  ok: недоступность названа недоступностью" ;;
  *) echo "  ПРОВАЛ: недоступность не названа: $out" >&2; failures=$((failures + 1)) ;;
esac

# ── 4. Ответ 200, но не наш JSON: тот же класс «не знаю» ─────────────────────
base=$(start_server '<html>502 Bad Gateway</html>')
out=$(quarantine_report "$base" "$JAR" 2>&1) && rc=0 || rc=$?
check "неразбираемое тело: возврат 1" "1" "$rc"
case "$out" in
  *"карантин пуст"*) echo "  ПРОВАЛ: HTML-ответ назван пустым карантином" >&2; failures=$((failures + 1)) ;;
  *"НЕ прочитан"*) echo "  ok: неразбираемое тело названо непрочитанным" ;;
  *) echo "  ПРОВАЛ: неразбираемое тело не названо: $out" >&2; failures=$((failures + 1)) ;;
esac
stop_server

# ── 5. JSON без поля quarantined: тоже «не знаю», а не пусто ─────────────────
base=$(start_server '{"ok":true}')
out=$(quarantine_report "$base" "$JAR" 2>&1) && rc=0 || rc=$?
check "JSON без поля quarantined: возврат 1" "1" "$rc"
case "$out" in
  *"карантин пуст"*) echo "  ПРОВАЛ: ответ без поля назван пустым карантином" >&2; failures=$((failures + 1)) ;;
  *"НЕ прочитан"*) echo "  ok: отсутствие поля названо непрочитанным" ;;
  *) echo "  ПРОВАЛ: отсутствие поля не названо: $out" >&2; failures=$((failures + 1)) ;;
esac
stop_server

if [ "$failures" -ne 0 ]; then
  echo "quarantine-report: провалов $failures" >&2
  exit 1
fi
echo "quarantine-report: все проверки прошли"
