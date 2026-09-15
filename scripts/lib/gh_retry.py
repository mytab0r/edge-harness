#!/usr/bin/env python3
"""Транзиентный обрыв `gh api` — общая классификация класса отказа и повтор
с выдержкой (issue #770).

Живой класс: `gh api`, оборвавшийся посреди прогона (`unexpected end of
JSON input`, обрыв соединения, 5xx), обрабатывался как содержательный
отказ — так же, как 404/403/нарушение контракта. Оба случая раньше давали
одно и то же: `RuntimeError` без повтора. Разница по цене: содержательный
отказ повторять бессмысленно и вредно (вернётся тот же ответ), транзиентный —
типично одноразовый сетевой чих, снимается повтором.

Классификация — по СТРУКТУРНОМУ факту, не по совпадению текста (issue #770,
критерий 1): двух структурных фактов достаточно —

  1. Код возврата `gh` (`gh help exit-codes`, версия 2.85.0, проверено
     2026-09-15): 0 — успех; 1 — общий провал (запросу может сопутствовать
     разобранный HTTP-статус, а может и не сопутствовать — обрыв
     транспорта); 2 — команда отменена; 4 — «команда требует
     аутентификации» (задокументировано буквально).
  2. HTTP-статус, если `gh` успел его разобрать и вписать в stderr. Обе
     живые формы несут общую примету — подстроку "HTTP <3 цифры>":
     `gh: Not Found (HTTP 404)` (живой вызов 2026-09-15) и
     `gh: HTTP 502: upstream connect error` (уже лежащая в репозитории
     заглушка, scripts/git/test/task-branch-lease.test.sh:133). Разные
     обрамления вокруг одной и той же подстроки — regex ищет её, а не
     конкретную форму целиком.

Три состояния, не два (AGENTS.md, «алерт не гадает»; issue #770, «третье
состояние»):

  TRANSIENT — сетевой/серверный блип: 5xx, 429, 408, либо rc=1 БЕЗ
              разобранного статуса вовсе (обрыв транспорта — статус не
              успел долететь или разобраться, живой пример "unexpected end
              of JSON input"). Повторить есть смысл.
  FATAL     — rc=4 (аутентификация — токен сам между попытками не
              появится) либо разобранный статус 4xx, кроме 408/429
              (401/403/404/422 — клиент неправ, повтор вернёт тот же
              ответ).
  UNKNOWN   — rc=2 (cancelled: не подтверждено, было ли это отменой
              процесса или обрывом контекста по таймауту — gh сама не
              говорит) либо разобранный статус вне диапазона 4xx/5xx
              (не наблюдалось ни разу, но regex не исключает такую форму).
              НЕ ретраится молча: «не определили класс» — это не то же
              самое, что «класс транзиентный», трактовка по умолчанию —
              как FATAL (не жечь бюджет попыток на то, чего не понимаем;
              совпадает по эффекту, но сообщение честно называет "unknown",
              а не подделывает уверенность в "fatal").

Форма повтора — заимствована, не изобретена заново (issue #770, «носитель
уже есть»): экспоненциальная выдержка с потолком — та же форма, что
`pulse_guard.probe_backoff_minutes` (другой масштаб времени: там минуты
для полуоткрытого состояния предохранителя конвейера, здесь секунды для
одиночного сетевого вызова); объявленный бюджет попыток и громкий отказ с
именем причины — та же форма, что `scripts/lib/dsh-ci.sh::dsh_run_with_retry`.
Пороги объявлены ОДНИМ местом правды ниже — третий порог не заводится.
"""
import re
import time

# ── Пороги — одно место правды (issue #770, критерий 2) ──────────────────────
GH_RETRY_MAX_ATTEMPTS = 4
GH_RETRY_BASE_DELAY_SECS = 2.0
GH_RETRY_MAX_DELAY_SECS = 20.0

TRANSIENT = "transient"
FATAL = "fatal"
UNKNOWN = "unknown"

# rc==4 — «If a command requires authentication» (gh help exit-codes, gh 2.85.0).
_GH_EXIT_AUTH_REQUIRED = 4
# rc==2 — «If a command is running but gets cancelled».
_GH_EXIT_CANCELLED = 2

_HTTP_STATUS_RE = re.compile(r"HTTP[:\s]+(\d{3})")


def parse_http_status(stderr: str) -> int | None:
    """Разобранный HTTP-статус из stderr `gh`, если он там есть — общая
    примета обеих живых форм (см. докстринг модуля). None — статус не
    разобран (не значит «нет отказа», rc проверяется отдельно)."""
    match = _HTTP_STATUS_RE.search(stderr or "")
    return int(match.group(1)) if match else None


def classify_gh_error(returncode: int, stderr: str) -> str:
    """TRANSIENT | FATAL | UNKNOWN — см. докстринг модуля."""
    if returncode == _GH_EXIT_AUTH_REQUIRED:
        return FATAL
    status = parse_http_status(stderr)
    if status is not None:
        if status == 429 or status == 408 or status >= 500:
            return TRANSIENT
        if 400 <= status < 500:
            return FATAL
        return UNKNOWN  # диапазон не наблюдался ни разу — не гадаем, честно "unknown"
    if returncode == _GH_EXIT_CANCELLED:
        return UNKNOWN
    # rc==1 без разобранного статуса — обрыв транспорта (issue #770,
    # критерий 1: «отсутствие разобранного статуса = обрыв транспорта»).
    return TRANSIENT


def retry_delay_secs(attempt: int, base: float = GH_RETRY_BASE_DELAY_SECS,
                      cap: float = GH_RETRY_MAX_DELAY_SECS) -> float:
    """Выдержка ПЕРЕД попыткой номер `attempt` (считается от 1 — перед первым
    повтором, то есть уже после одного провала). Форма — probe_backoff_minutes
    (pulse_guard.py): экспонента с потолком, другой масштаб единиц."""
    if attempt < 1:
        attempt = 1
    return min(base * (2 ** (attempt - 1)), cap)


def describe_call(argv: list[str]) -> str:
    """Текст вызова для сообщений об ошибке — "gh api -X" / "gh api <path>":
    ПЕРВЫЕ ДВА токена после "gh" (обратная совместимость с форматом, который
    уже несли ai_review.py::gh()/run_gh() и pulse_guard.gh() ДО этой правки —
    живой инцидент цитирует ровно эту форму: "gh api -X: unexpected end of
    JSON input")."""
    return "gh " + " ".join(argv[1:3])


class GhCallExhausted(RuntimeError):
    """Класс TRANSIENT, но бюджет попыток исчерпан. Сообщение обязано нести
    все четыре части (issue #770, критерий 3): сам вызов, класс, сколько
    попыток за сколько времени, что делать дальше. Атрибуты — структурные
    (не только текст), чтобы вызывающий код мог обогатить сообщение
    (например, вычисленным вердиктом — ai_review.py::cmd_verdict)."""

    def __init__(self, call: str, attempts: int, elapsed_secs: float, last_stderr: str):
        self.call = call
        self.attempts = attempts
        self.elapsed_secs = elapsed_secs
        self.last_stderr = last_stderr
        super().__init__(
            f"{call}: класс отказа — транзиентный (сеть/сервер), не снялся "
            f"за {attempts} попыт{_attempt_suffix(attempts)} ({elapsed_secs:.1f}с). "
            f"Последняя ошибка: {last_stderr}. Дальше: повторить прогон "
            "позже (workflow_dispatch force: true при необходимости — "
            "обычный сетевой блип снимается сам за минуты; если повторяется "
            "подряд — проверить status.github.com)."
        )


def _attempt_suffix(n: int) -> str:
    if n == 1:
        return "ку"
    if 2 <= n <= 4:
        return "ки"
    return "ок"


def call_gh_with_retry(argv: list[str], *, run, sleep=time.sleep,
                        max_attempts: int = GH_RETRY_MAX_ATTEMPTS,
                        base_delay: float = GH_RETRY_BASE_DELAY_SECS,
                        cap_delay: float = GH_RETRY_MAX_DELAY_SECS):
    """Выполняет `run(argv)` (совместим с `subprocess.run` — принимает уже
    готовый argv, включая "gh" на месте [0], возвращает объект с
    `.returncode`/`.stdout`/`.stderr`) с повтором ТОЛЬКО класса TRANSIENT.

    FATAL/UNKNOWN — падение с ПЕРВОЙ попытки (issue #770, критерий 1:
    повторять содержательный отказ бессмысленно и вредно; UNKNOWN трактуется
    так же строго — не значит «пробуем ещё», см. докстринг модуля), с
    сообщением в прежнем формате "<call>: <stderr>" — обратная совместимость
    с текстом, который несли gh()/run_gh() до этой правки.

    Возвращает `run(argv)` целиком при rc==0. Бросает `GhCallExhausted` на
    исчерпании бюджета TRANSIENT-попыток."""
    call = describe_call(argv)
    started = time.monotonic()
    attempt = 0
    last_stderr = ""
    while True:
        attempt += 1
        result = run(argv)
        if result.returncode == 0:
            return result
        last_stderr = (result.stderr or "").strip()
        cls = classify_gh_error(result.returncode, last_stderr)
        if cls != TRANSIENT:
            raise RuntimeError(f"{call}: {last_stderr}")
        if attempt >= max_attempts:
            raise GhCallExhausted(call, attempt, time.monotonic() - started, last_stderr)
        sleep(retry_delay_secs(attempt, base_delay, cap_delay))
