#!/usr/bin/env python3
"""Sweep осиротевших сессий раннера в морде dsh-edge (#940).

Класс, который `archive_runner_sessions` (scripts/orchestra/scheduler.py,
~строка 1509) не закрывает: при испорченной холодной загрузке
`dsh_edge_session_begin` (scripts/lib/dsh-edge-session.sh:155-157) заводит
НОВУЮ сессию `${session_id}-r${GITHUB_RUN_ID}` и прямо пишет «старая
остаётся сиротой». `archive_runner_sessions` архивирует ТОЛЬКО
`harness-{number}` (жёсткая подстановка session_id) — ни испорченную
`harness-N`, ни рабочую `harness-N-r<run>` эта функция никогда не находит,
если задача была решена именно в прогоне с суффиксом. Растёт в дефицитном
месте — DO SQLite, потолок 100k строк/сутки (AGENTS.md).

Не трогаем scheduler.py (задание #940 явно исключает файл — параллельная
работа) — это ОТДЕЛЬНЫЙ периодический sweep, устроенный иначе:
`archive_runner_sessions` получает номера задач из тела уже слитого PR (не
опрашивает морду вообще), этот sweep — наоборот, перечисляет ВСЕ сессии
морды через `session.list` и для каждой `harness-*` сам смотрит, закрыта ли
задача. Разные источники начала работы, общий примитив архивации
(`workspace.archiveSession`).

RPC/логин — минимальное намеренное дублирование `_morde_opener`/
`_morde_login`/`_morde_rpc` из scheduler.py (импортировать нельзя — правка
scheduler.py вне рамок этой задачи). Место будущего объединения — вынести
общий модуль `scripts/lib/dsh_edge_morde.py`, когда scheduler.py снова
открыт для правки (названо в отчёте задачи #940, не заведено отдельной
задачей — не блокирующий долг, чистая рефакторинг-заметка).

НЕ ПОДТВЕРЖДЕНО (docs/research/12-dsh-edge-session-api.md называет
проверенным ТОЛЬКО поле `items[].projections.values.title` у `session.list`
— имя поля идентификатора сессии в ответе живым вызовом не проверено).
`session_id_of` пробует `item["sessionId"]`, затем `item["id"]` (тот же
именной приём, что несёт каждый ДРУГОЙ session.*-метод этого API) — элемент
без ни одного из двух пропускается с явным предупреждением в отчёте, не
гадаем и не падаем всем прогоном. Там же — второе непроверенное допущение,
честно названное (находка ревью PR #944): `fetch_session_list` читает
ОДНУ страницу `session.list` — контракт пагинации этого метода (есть ли
курсор/hasMore в ответе, как у `session.history` `beforeSeq`) неизвестен,
листать «вслепую» не на чем; хвост списка длиннее страницы терялся бы
молча, поэтому ограничение названо и в докстринге, и в каждой строке
отчёта прогона — до проверки живым прогоном это пробел, а не факт.

Счётчик (видимость роста сирот, #940 п.2): каждый прогон печатает (сколько
сессий `harness-*` увидел `session.list`, сколько распознано как сироты,
сколько заархивировано, сколько ошибок, сколько элементов нечитаемой формы)
— в step summary orchestra.yml (cron */15 мин) рост виден без отдельной
инфраструктуры хранения счётчика.
"""

import http.cookiejar
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent.parent / "lib" / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

DSH_EDGE_URL = os.environ.get("DSH_EDGE_URL", "")
DSH_EDGE_ACCESS_KEY = os.environ.get("DSH_EDGE_ACCESS_KEY", "")
MORDE_USER_AGENT = "edge-harness-orchestra/1.0 (+https://github.com/mytab0r/edge-harness)"

# harness-<N> (основная сессия) или harness-<N>-r<run_id> (фоллбэк после
# порчи холодной загрузки, #809/#871) — оба узнаём по одному номеру задачи.
SESSION_ID_RE = re.compile(r"^harness-(\d+)(?:-r\d+)?$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Тот же контракт, что scheduler.py::_NoRedirect (docs/research/12:13-14):
    POST /api/auth/login отвечает 303 + Set-Cookie — это успех, не редирект."""

    def redirect_request(self, *args, **kwargs):
        return None


def _morde_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [("User-Agent", MORDE_USER_AGENT)]
    return opener


def _morde_login(opener: urllib.request.OpenerDirector) -> None:
    data = urllib.parse.urlencode({"accessKey": DSH_EDGE_ACCESS_KEY}).encode()
    req = urllib.request.Request(
        DSH_EDGE_URL.rstrip("/") + "/api/auth/login", data=data, method="POST")
    try:
        with opener.open(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as error:
        status = error.code
        if status != 303:
            raise RuntimeError(f"логин в морду не удался: HTTP {status}") from error
        return
    if status != 303:
        raise RuntimeError(f"логин в морду не удался: ожидали HTTP 303, получили {status}")


def _morde_rpc(opener: urllib.request.OpenerDirector, method: str, payload: dict) -> dict:
    body = json.dumps({
        "type": "client-request",
        "rpcId": "orchestra-session-sweep",
        "method": method,
        "payload": payload,
    }).encode()
    req = urllib.request.Request(
        DSH_EDGE_URL.rstrip("/") + "/api/" + method,
        data=body, method="POST",
        headers={"content-type": "application/json"})
    with opener.open(req, timeout=30) as resp:
        result = json.load(resp)
    inner = result.get("result", {})
    if not inner.get("ok"):
        error = inner.get("error", {})
        raise RuntimeError(f'{error.get("code", "unknown")}: {error.get("message", "")}')
    return inner.get("value", {})


def session_id_of(item: dict) -> str | None:
    """См. блок «НЕ ПОДТВЕРЖДЕНО» в докстринге модуля."""
    sid = item.get("sessionId") or item.get("id")
    return sid if isinstance(sid, str) else None


def classify_sessions(session_items: list[dict], issue_state) -> dict:
    """Чистая функция: (сырые элементы session.list, callable(number)->str|None)
    -> статистика с явными списками. `issue_state(number)` — состояние задачи
    ВЕРХНИМ регистром (`"CLOSED"`/`"OPEN"`) или `None`, если определить не
    удалось (сеть/квота) — трактуется как «не трогаем», не как «закрыта».

    Возвращает dict с ключами:
      total_items — сколько элементов пришло от session.list вообще;
      unparseable — элементы без sessionId/id вовсе (см. session_id_of);
      not_harness — сколько НЕ подошло под SESSION_ID_RE (не наш предмет);
      orphans — [(session_id, task_number)] задача закрыта — кандидат в архив;
      kept — [(session_id, task_number, reason)] задача открыта или статус
             не определён — не трогаем.
    """
    stats = {
        "total_items": len(session_items),
        "unparseable": 0,
        "not_harness": 0,
        "orphans": [],
        "kept": [],
    }
    for item in session_items:
        sid = session_id_of(item)
        if sid is None:
            stats["unparseable"] += 1
            continue
        match = SESSION_ID_RE.match(sid)
        if not match:
            stats["not_harness"] += 1
            continue
        number = int(match.group(1))
        state = issue_state(number)
        if state is None:
            stats["kept"].append((sid, number, "статус задачи не определён — не трогаем"))
            continue
        if state == "CLOSED":
            stats["orphans"].append((sid, number))
        else:
            stats["kept"].append((sid, number, f"задача открыта (state={state})"))
    return stats


def fetch_session_list(opener: urllib.request.OpenerDirector) -> list[dict]:
    """Первая (и, насколько известно, единственная) страница `session.list`.
    Контракт пагинации метода НЕ ПОДТВЕРЖДЁН (см. докстринг модуля и
    docs/research/12) — хвост длиннее страницы был бы потерян молча, поэтому
    ограничение напечатано в отчёте каждого прогона, а не спрятано здесь."""
    value = _morde_rpc(opener, "session.list", {})
    return value.get("items", []) if isinstance(value, dict) else []


def archive_session(opener: urllib.request.OpenerDirector, session_id: str) -> tuple[bool, str]:
    """(успех, сообщение). Мягкий успех — ТОЛЬКО наблюдённый и
    задокументированный код `session-not-found` (docs/research/12:64,
    живой вызов 2026-08-31; тот же единственный код трактует
    `archive_runner_sessions` в scheduler.py): сессия уже не активна —
    ровно то, чего мы добивались. Любой другой отказ — отказ (находка
    ревью PR #944: обобщённая подстрока "already" выдавала любую поломку
    со словом «already» в тексте за успех и обнуляла счётчик ошибок)."""
    try:
        _morde_rpc(opener, "workspace.archiveSession", {"sessionId": session_id})
        return True, "заархивирована"
    except RuntimeError as error:
        if "session-not-found" in str(error):
            return True, f"уже не активна ({error})"
        return False, str(error)


def run_sweep(dry_run: bool = False) -> int:
    if not DSH_EDGE_URL or not DSH_EDGE_ACCESS_KEY:
        print("⚠️ DSH_EDGE_URL/DSH_EDGE_ACCESS_KEY не заданы — sweep осиротевших сессий пропущен")
        return 0

    import subprocess

    def issue_state(number: int) -> str | None:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "state", "-q", ".state"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            return None
        state = result.stdout.strip()
        return state or None

    try:
        opener = _morde_opener()
        _morde_login(opener)
        items = fetch_session_list(opener)
    except (RuntimeError, OSError, urllib.error.URLError, ValueError) as error:
        print(f"🚨 морда dsh-edge недоступна для sweep осиротевших сессий (возможность сломана, не отсутствует): {error}",
              file=sys.stderr)
        return 1

    stats = classify_sessions(items, issue_state)
    print(f"session.list (первая страница — пагинация метода НЕ ПОДТВЕРЖДЕНА, "
          f"см. докстринг): {stats['total_items']} элементов, "
          f"{stats['total_items'] - stats['unparseable'] - stats['not_harness']} узнано как harness-*, "
          f"{stats['unparseable']} нечитаемой формы (см. «НЕ ПОДТВЕРЖДЕНО» в докстринге), "
          f"{len(stats['orphans'])} сирот (задача закрыта)")

    errors = 0
    for sid, number in stats["orphans"]:
        if dry_run:
            print(f"DRY-RUN: заархивировал бы {sid} (#{number} закрыта)")
            continue
        ok, message = archive_session(opener, sid)
        if ok:
            print(f"🗄️ {sid} (#{number}): {message}")
        else:
            print(f"🚨 {sid} (#{number}): не заархивирована (возможность сломана): {message}", file=sys.stderr)
            errors += 1

    # stats["kept"] сознательно не льём построчно в лог (шум на десятки живых
    # задач) — причина по каждой сессии уже накоплена в stats для отчёта/теста.

    return 1 if errors else 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    return run_sweep(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
