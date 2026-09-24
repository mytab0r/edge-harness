#!/usr/bin/env python3
"""Гвардия класса #1510: **холостой прогон воркера не имеет права платить
аккаунтной квотой Durable Objects.**

Отказ ДО работы агента (цепочка провайдеров не ответила) возвращает задачу в
пул немедленно — и до этой правки ВСЁ РАВНО сливал транскрипт в dsh-edge, то
есть записывал ход работы, которой не было. Приём транскрипта идёт в Durable
Object, а суточный лимит `rows_read` (5 млн) общий на аккаунт с мордой.

Живая цена (замер #1513, разбивка по `objectId`): 2026-09-23 холостые прогоны
выбрали 5 700 194 строки — 114 % суточного лимита, после чего морда стала
отдавать `1101` на каждый `/api/*`, и кнопка решения владельца в Telegram
умерла вместе с ней. Сама морда прочитала за те сутки 198 строк из 5 700 392.

ПОЧЕМУ СТЕНД С НАСТОЯЩИМ СЕРВЕРОМ, А НЕ ЗАГЛУШКОЙ. AGENTS.md, «Заглушка
внешнего инструмента — это пересказ, и она ломается на исправном коде»
(#1373/PR #1380): подменённый `curl` понимает ту форму вызова, под которую
написан, и краснеет на исправном коде, как только форма меняется. Здесь
поднимается НАСТОЯЩИЙ `http.server` и зовётся НАСТОЯЩИЙ клиент
`scripts/lib/dsh-edge-session.sh` настоящим bash. Проверяется ВИДИМЫЙ
РЕЗУЛЬТАТ — дошёл ли до сервера POST, — а не «функция не вызвана»: последнее
совпадает и когда вызов просто переименовали.

Блок вырезается из НАСТОЯЩЕГО `scripts/worker/task.sh` по якорю, и дрейф
якоря ловится отдельно: если вырезанный кусок не несёт `WORKER_SKIP_TRANSCRIPT`
и `dsh_edge_drain_spool hard`, тест падает, а не зеленеет на пустоте.

Запуск: python -m pytest scripts/lib/test_idle_run_transcript_guard.py -q
"""

from __future__ import annotations

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import http.server
import re
import subprocess
import tempfile
import threading

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TASK_SH = REPO_ROOT / "scripts" / "worker" / "task.sh"
DSH_CI = REPO_ROOT / "scripts" / "lib" / "dsh-ci.sh"
EDGE_SESSION = REPO_ROOT / "scripts" / "lib" / "dsh-edge-session.sh"

#: Якорь блока в прод-файле. Держи в синхроне с комментарием в task.sh —
#: расхождение ловится проверкой «блок не найден» ниже, громко.
ANCHOR = "# Отказ ДО работы агента — транскрипт НЕ льём"

#: Три класса отказа ДО работы агента — ровно те, что в блоке провайдерного
#: отказа возвращают задачу в пул НЕМЕДЛЕННО и дают зелёный прогон (#1286):
#: они и образуют цикл, который платит квотой.
IDLE_CLASSES = ["all_providers_exhausted", "quota_exhausted",
                "rate_limit_retry_budget_exceeded"]


def _extract_block() -> str:
    """Вырезает НАСТОЯЩИЙ блок из task.sh — тест исполняет прод-код, а не свою
    копию его логики (тот же приём, что provider-exhaustion-exit-code.smoke.sh,
    класс #476)."""
    source = TASK_SH.read_text(encoding="utf-8")
    start = source.find(ANCHOR)
    assert start != -1, (
        f"блок транскрипта не найден в {TASK_SH} по якорю {ANCHOR!r} — "
        "комментарий переименован? Тест обязан падать, а не зеленеть на пустоте")
    end = source.find("\nfi\n", start)
    assert end != -1, "у блока транскрипта не найден закрывающий fi на нулевом отступе"
    block = source[start:end + len("\nfi\n")]
    # Гвардия дрейфа якоря: без неё вырезанный кусок мог бы оказаться чужим.
    assert "WORKER_SKIP_TRANSCRIPT" in block, (
        "в извлечённом блоке нет WORKER_SKIP_TRANSCRIPT — фикс #1510 снят или переехал")
    assert "dsh_edge_drain_spool hard" in block, (
        "в извлечённом блоке нет жёсткого дрена — якорь снял не тот кусок")
    return block


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Настоящая морда-стенд: пишет по строке на каждый запрос."""

    hits: list[str] = []

    def _record(self) -> None:
        type(self).hits.append(f"{self.command} {self.path}")

    def do_POST(self) -> None:  # noqa: N802 — имя задаёт BaseHTTPRequestHandler
        self._record()
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true,"accepted":1}')

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def morda():
    """Живой HTTP-сервер вместо морды. Закрытый порт здесь не нужен вовсе: нас
    интересует обратное — что запрос НЕ пришёл на живой, готовый принять."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _run_block(morda_server, failure_reason: str) -> tuple[list[str], str]:
    """Исполняет настоящий блок настоящим bash поверх настоящего клиента морды.
    Возвращает (запросы, дошедшие до сервера; вывод блока)."""
    _Recorder.hits = []
    block = _extract_block()
    port = morda_server.server_address[1]
    with tempfile.TemporaryDirectory() as work:
        spool = Path(work) / "spool.ndjson"
        spool.write_text(
            "".join(f'{{"type":"text","text":"событие {i}"}}\n' for i in range(1, 4)),
            encoding="utf-8")
        snippet = Path(work) / "block.sh"
        snippet.write_text(block, encoding="utf-8")
        script = (
            "set -euo pipefail\n"
            # Обе библиотеки и в том же порядке, что подключает сам task.sh:
            # dsh-ci.sh несёт redact(), которым клиент чистит батч от секретов.
            f'source "{DSH_CI}"\n'
            f'source "{EDGE_SESSION}"\n'
            "DSH_EDGE_MORDA_AVAILABLE=1\n"
            "DSH_EDGE_SESSION_ID=guard-session\n"
            "rc=1\n"
            f'source "{snippet}"\n'
        )
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": work,
            "WORK": work,
            "SPOOL_FILE": str(spool),
            "DSH_EDGE_URL": f"http://127.0.0.1:{port}",
            # Стенд ключ не проверяет вовсе — переменная нужна клиенту только
            # чтобы собрать заголовок. Значение короткое и очевидно ненастоящее
            # нарочно: длинный «похожий на ключ» литерал справедливо ловит
            # гвардия секретов check_pr.py, и обходить её подбором длины значило
            # бы учить следующего агента обходу вместо честного placeholder'а.
            "DSH_EDGE_ACCESS_KEY": "smoke",
            "WORKER_TASK_FAILURE_REASON": failure_reason,
            "LC_ALL": "C.UTF-8",
        }
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, encoding="utf-8", env=env, timeout=120)
    return list(_Recorder.hits), result.stdout + result.stderr


@pytest.mark.parametrize("failure_reason", IDLE_CLASSES)
def test_idle_run_does_not_touch_the_morda(morda, failure_reason: str):
    """Главное: отказ ДО работы агента не шлёт морде НИ ОДНОГО запроса.

    Проверяется видимый результат (сервер ничего не получил), а не факт
    невызова функции: последнее совпадает и при переименовании вызова."""
    hits, _out = _run_block(morda, failure_reason)

    assert hits == [], (
        f"отказ до работы агента ({failure_reason}) обязан НЕ трогать морду, "
        f"а стенд получил {len(hits)} запрос(ов): {hits[:3]} — холостой прогон "
        "снова платит аккаунтной квотой rows_read (#1510)")


def test_the_skip_is_announced_out_loud(morda):
    """Fail loud, не silent-wrong: читатель лога обязан отличить «не лили» от
    «морда отказала» — лечатся эти два случая по-разному."""
    _hits, out = _run_block(morda, "all_providers_exhausted")

    assert "Транскрипт не отправлен намеренно" in out, (
        f"пропуск транскрипта не объявлен в логе: {out[-400:]}")


def test_successful_run_still_ships_the_transcript(morda):
    """Обратная сторона, без которой фикс выродился бы в «транскрипт не льётся
    никогда», и витрина хода работы умерла бы молча."""
    hits, out = _run_block(morda, "")

    assert hits, ("при успешном прогоне (пустой класс отказа) транскрипт обязан "
                  f"уехать в морду, а стенд не получил ни одного запроса: {out[-400:]}")


def test_our_own_failure_keeps_the_transcript(morda):
    """`prompt_too_long` — НАШ отказ: прогон красный, его серию останавливает
    предохранитель диспатча (#1315), цикла нет, транскрипт остаётся
    диагностикой. Список классов сужен намеренно, и это проверяется."""
    hits, out = _run_block(morda, "prompt_too_long")

    assert hits, ("prompt_too_long обязан оставлять транскрипт — он диагностика, "
                  f"а не холостой цикл; стенд не получил ни одного запроса: {out[-400:]}")


def test_class_list_matches_the_green_branch_of_task_sh():
    """Одно место правды на список классов: блок пропуска и блок провайдерного
    отказа (#1286) обязаны называть ОДНИ И ТЕ ЖЕ три класса.

    Разойдутся — и появится класс, который возвращает задачу в пул немедленно
    (то есть крутит цикл), но транскрипт при этом льёт. Такое расхождение
    глазами не видно: блоки лежат в трёхстах строках друг от друга."""
    source = TASK_SH.read_text(encoding="utf-8")
    skip_block = _extract_block()
    green = re.search(r"case \"\$WORKER_TASK_FAILURE_REASON\" in\n"
                      r"\s*(quota_exhausted[^\n]*)\n\s*job_exit=\"green\"", source)
    assert green, ("зелёная ветка блока провайдерного отказа не найдена — "
                   "переименована? Сверка списков невозможна, и это отказ, "
                   "а не молчаливое «совпало»")
    green_classes = {c.strip() for c in green.group(1).rstrip(")").split("|")}
    skip_classes = set()
    for line in skip_block.splitlines():
        if "|" in line and line.strip().endswith(")"):
            skip_classes = {c.strip() for c in line.strip().rstrip(")").split("|")}
            break
    assert skip_classes == green_classes, (
        f"списки разошлись: пропуск транскрипта {sorted(skip_classes)}, "
        f"зелёная ветка {sorted(green_classes)} — класс, возвращающий задачу в "
        "пул немедленно, но льющий транскрипт, крутил бы цикл за счёт квоты")
