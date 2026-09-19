#!/usr/bin/env bash
# HTTP для канареек деплоя: отказ ОБЯЗАН нести тело ответа (issue #1371).
#
# Класс, оплаченный живым разбором. Прогон deploy-dsh-edge.yml 35387441612
# (2026-09-18) упал на канарейке ingest-шва, и всё, что осталось в логе:
#
#   curl: (22) The requested URL returned error: 500
#   jq: parse error: Expected string key before ':' at line 1, column 1
#   ##[error]Process completed with exit code 5.
#
# Код есть, причины нет. Морда при этом отвечает на ошибках осмысленным JSON
# (`{"type":"server-response","result":{"ok":false,...}}`) — то есть причина
# существовала, дошла по сети и была ВЫБРОШЕНА шагом. Виноват флаг `-f`
# (`--fail`): он и придуман для того, чтобы не отдавать тело на HTTP-ошибке.
# Цена: разбор свёлся к перебору гипотез, три из которых пришлось отвергать
# исполнением (см. #1371).
#
# Правило AGENTS.md «алерт не гадает» — про сообщение человеку; здесь тот же
# класс для шага CI: шаг, который знает код И тело, не имеет права печатать
# только код.
#
# Подключение:
#   source "$GITHUB_WORKSPACE/scripts/lib/canary_http.sh"
# Рассчитано на bash с `set -euo pipefail` (источник задаёт).

# Сколько байт тела показывать. Тело ответа морды на ошибке — короткий JSON;
# потолок нужен против случая, когда вместо него прилетела HTML-страница
# прокси/Cloudflare на сотни килобайт и утопила лог.
CANARY_BODY_LIMIT_BYTES="${CANARY_BODY_LIMIT_BYTES:-2000}"

# Уровень аннотации отказа: `error` по умолчанию (канарейка обязана быть
# громкой). `warning` нужен ровно там, где отказ запроса объявлен НЕ фатальным
# и у него назван газ — шаг «Снимок версии прода до деплоя»: его неудача не
# блокирует деплой, автооткат дальше откажется явно. Печатать там `::error::`
# значило бы гадать читателю, сломалось ли что-то (AGENTS.md «алерт не
# гадает»), а печатать код БЕЗ тела — ровно тот класс, ради которого написан
# этот файл. Читается при КАЖДОМ вызове, чтобы значение можно было выставить
# после `source`.
_canary_level() { printf '%s' "${CANARY_ERROR_LEVEL:-error}"; }

# canary_http <метка> <curl-аргументы...>
#
# Выполняет запрос БЕЗ `-f` и сам решает по коду. Успех (2xx) — тело уходит в
# stdout, как отдавал бы `curl -fsS`, поэтому вызов подставляется в пайплайны
# с jq без изменений. Не-2xx или сетевой отказ — громкий `::error::` с меткой,
# кодом И телом, возврат 1.
canary_http() {
  local label=$1
  shift
  local body_file code curl_rc
  body_file=$(mktemp)
  # `-sS`: без прогресс-бара, но с текстом сетевой ошибки. Никакого `-f`:
  # тело на не-2xx — это и есть то, ради чего функция существует.
  code=$(curl -sS -o "$body_file" -w '%{http_code}' "$@") && curl_rc=0 || curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    # Сетевой отказ (DNS, TLS, таймаут): HTTP-кода нет вовсе — говорим это
    # прямо, а не печатаем пустую строку вместо кода.
    echo "::$(_canary_level)::$label: запрос не состоялся (curl rc=$curl_rc, HTTP-ответа нет)" >&2
    _canary_print_body "$label" "$body_file"
    rm -f "$body_file"
    return 1
  fi
  case "$code" in
    2??)
      cat "$body_file"
      rm -f "$body_file"
      return 0
      ;;
    *)
      echo "::$(_canary_level)::$label: HTTP $code" >&2
      _canary_print_body "$label" "$body_file"
      rm -f "$body_file"
      return 1
      ;;
  esac
}

# canary_probe <метка> <ожидаемый-код> <curl-аргументы...>
#
# Вариант для проб «ответил ли эндпоинт тем, чем должен»: печатает в stdout
# ТОЛЬКО код (чтобы `code=$(canary_probe ...)` работал как прежний
# `code=$(curl -o /dev/null -w '%{http_code}' ...)`), а тело — исключительно
# когда код НЕ СОВПАЛ с ожидаемым, и исключительно в stderr. Вердикт остаётся
# за вызывающим: он сравнивает код сам и печатает свой текст.
#
# Почему ожидаемый код — аргумент, а не «любой 2xx» (находка ai-review
# PR #1372, вторая итерация): пробы корня ждут РОВНО 200, а логины канареек —
# РОВНО 303 (редирект после успешной авторизации). «2xx» сделало бы успешный
# логин «неудачей» и залило бы лог телом редиректа на каждом деплое, а
# оставить логины в стороне значило бы сузить класс: отказ логина с 500 точно
# так же выбрасывал бы причину.
#
# Почему отдельная функция, а не canary_http: у той контракт «успех → тело в
# stdout», и на пробе корня он залил бы HTML страницы морды в лог КАЖДОГО
# успешного деплоя. Тихо на ожидаемом коде, громко на любом другом — разные
# контракты, две функции, одно место правды на печать тела.
canary_probe() {
  local label=$1 expected=$2
  shift 2
  local body_file code curl_rc
  body_file=$(mktemp)
  code=$(curl -sS -o "$body_file" -w '%{http_code}' "$@") && curl_rc=0 || curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    echo "::$(_canary_level)::$label: запрос не состоялся (curl rc=$curl_rc, HTTP-ответа нет)" >&2
    _canary_print_body "$label" "$body_file"
  elif [ "$code" != "$expected" ]; then
    echo "::$(_canary_level)::$label: HTTP $code, ожидался $expected" >&2
    _canary_print_body "$label" "$body_file"
  fi
  rm -f "$body_file"
  # curl при сетевом отказе сам отдаёт 000 — вызывающий сравнит с ожидаемым и упадёт.
  printf '%s' "${code:-000}"
}

# Печать тела одной строкой: перевод строки в пробел, потому что в логе job'а
# многострочный ответ разъезжается по группам и теряется при сворачивании.
_canary_print_body() {
  local label=$1 body_file=$2
  local size body
  size=$(wc -c < "$body_file" | tr -d ' ')
  if [ "$size" = "0" ]; then
    echo "::$(_canary_level)::$label: тело ответа пустое (сервер не сказал причину)" >&2
    return 0
  fi
  body=$(head -c "$CANARY_BODY_LIMIT_BYTES" "$body_file" | tr '\n\r' '  ')
  if [ "$size" -gt "$CANARY_BODY_LIMIT_BYTES" ]; then
    echo "::$(_canary_level)::$label: тело ответа ($size байт, показаны первые $CANARY_BODY_LIMIT_BYTES): $body" >&2
  else
    echo "::$(_canary_level)::$label: тело ответа: $body" >&2
  fi
}
