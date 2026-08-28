#!/usr/bin/env python3
"""Клиент «рук» для job'а edge-harness.

Шлёт в Durable Object heartbeat и события батчами. Идемпотентность журнала держится
на порядковых номерах: seq уникальны в рамках TASK_ID и переживают отдельные шаги
job'а через файл состояния. Только стандартная библиотека.

Команды:
    hands_client.py start              — первый heartbeat (якорь замера задержки) + job_start
    hands_client.py step KIND -- CMD   — исполняет CMD, шлёт событие KIND с кодом возврата
    hands_client.py finish ok|fail     — job_end, финальная досылка, стоп heartbeat-потока

Переменные окружения: HARNESS_URL, HANDS_TOKEN, TASK_ID, опционально GITHUB_RUN_ID.
Секрет HANDS_TOKEN никогда не печатается — ни сам, ни в составе заголовков.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HEARTBEAT_PERIOD_S = 10
FLUSH_RETRIES = 5
FLUSH_BACKOFF_S = 2
HTTP_TIMEOUT_S = 30
TAIL_CHARS = 4000
REPLAY_PAGE = 500

BASE = os.environ["HARNESS_URL"].rstrip("/")
TOKEN = os.environ["HANDS_TOKEN"]
# Cloudflare на workers.dev блокирует UA "Python-urllib/*" (403 от edge, до кода не доходит).
UA = "edge-harness/0.1 (GitHub Actions; +https://github.com/mytab0r/edge-harness)"
TASK_ID = os.environ["TASK_ID"]
JOB_ID = os.environ.get("GITHUB_RUN_ID", "local")
STATE_FILE = os.path.join(tempfile.gettempdir(), f"edge-harness-hands-{TASK_ID}.json")

_stop_heartbeat = threading.Event()
_seq_lock = threading.Lock()


def _request(path: str, body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {TOKEN}", "User-Agent": UA}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as response:
        parsed = json.load(response)
        if response.status != 200:
            raise RuntimeError(f"{path} ответил {response.status}")
        return parsed


def _post_with_retries(path: str, body: dict) -> None:
    last_error = None
    for attempt in range(FLUSH_RETRIES):
        try:
            _request(path, body)
            return
        except Exception as error:  # noqa: BLE001 — сетевые ошибки любого рода ретраим
            last_error = error
            if attempt < FLUSH_RETRIES - 1:
                time.sleep(FLUSH_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"{path} не принял батч за {FLUSH_RETRIES} попыток: {last_error}")


def _seed_seq() -> None:
    """Засевает счётчик seq с сервера: максимум seq этой задачи.

    Без засева повторный запуск job'а (новый runner, пустой /tmp) начал бы seq с 1,
    и новые события молча выпали бы по конфликту UNIQUE(task_id, seq) — тихая потеря.
    Ретраи той же доставки при этом по-прежнему дедуплицируются на сервере.
    """
    with _seq_lock:
        state = _load_state()
        if state.get("seq"):
            return  # внутри текущего запуска счётчик уже живёт
        max_seq = 0
        after = 0
        for _ in range(1000):  # страховка от вечного цикла; страницы по REPLAY_PAGE
            page = _request(f"/api/events?task_id={TASK_ID}&after={after}&limit={REPLAY_PAGE}")
            for event in page["events"]:
                if event["source"] == "job":
                    max_seq = max(max_seq, event["seq"])
            if not page["has_more"]:
                break
            after = page["next_after"]
        state["seq"] = max(state["seq"], max_seq)
        _save_state(state)


def _load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"seq": 0}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file)


def emit(kind: str, data, flush: bool = True) -> None:
    """Добавляет событие с очередным seq; дубли при ретрае не двоят журнал —
    идемпотентность на стороне Durable Object по (task_id, seq)."""
    with _seq_lock:
        state = _load_state()
        state["seq"] += 1
        seq = state["seq"]
        _save_state(state)
    batch = {"task_id": TASK_ID, "events": [{"seq": seq, "kind": kind, "data": data}]}
    if flush:
        _post_with_retries("/api/events", batch)


def heartbeat_loop() -> None:
    while not _stop_heartbeat.wait(HEARTBEAT_PERIOD_S):
        try:
            _request("/api/heartbeat", {"job_id": JOB_ID, "task_id": TASK_ID})
        except Exception as error:  # noqa: BLE001 — heartbeat не должен ронять job
            print(f"hands: heartbeat не прошёл ({error}), продолжаем", file=sys.stderr)


def start_heartbeat() -> None:
    # Первый heartbeat отправляется сразу: это якорь замера задержки
    # «repository_dispatch → первый heartbeat» — DO фиксирует latency_ms у первой отметки.
    _request("/api/heartbeat", {"job_id": JOB_ID, "task_id": TASK_ID})
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()


def cmd_start() -> int:
    _seed_seq()
    start_heartbeat()
    emit(
        "job_start",
        {
            "job_id": JOB_ID,
            "repo": os.environ.get("GITHUB_REPOSITORY"),
            "ref": os.environ.get("GITHUB_REF"),
        },
    )
    print(f"hands: задача {TASK_ID} начата, job {JOB_ID}")
    return 0


def cmd_step(kind: str, command: list[str]) -> int:
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True)
    elapsed_s = round(time.monotonic() - started, 1)
    emit(
        kind,
        {
            "command": command,
            "exit_code": result.returncode,
            "elapsed_s": elapsed_s,
            "stdout_tail": result.stdout[-TAIL_CHARS:],
            "stderr_tail": result.stderr[-TAIL_CHARS:],
        },
    )
    print(f"hands: шаг {kind} завершён за {elapsed_s} с, код {result.returncode}")
    if result.returncode != 0:
        print(result.stderr[-TAIL_CHARS:], file=sys.stderr)
    return result.returncode


def cmd_finish(result: str) -> int:
    emit("job_end", {"result": result})
    _stop_heartbeat.set()
    print(f"hands: задача {TASK_ID} завершена, результат {result}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    try:
        if command == "start":
            return cmd_start()
        if command == "step":
            # argv[2] — kind, argv[3] — разделитель --, дальше исполняемая команда.
            if len(argv) < 5 or argv[3] != "--":
                print("использование: hands_client.py step KIND -- CMD [ARGS...]", file=sys.stderr)
                return 2
            return cmd_step(argv[2], argv[4:])
        if command == "finish":
            return cmd_finish(argv[2] if len(argv) > 2 else "ok")
    except Exception as error:  # noqa: BLE001 — наверх громко, а не тихо
        print(f"hands: ФАТАЛЬНО: {error}", file=sys.stderr)
        return 3
    print(f"неизвестная команда: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
