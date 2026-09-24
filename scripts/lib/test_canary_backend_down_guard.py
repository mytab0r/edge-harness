#!/usr/bin/env python3
"""Гвардия класса #1426: **канарейка деплоя не откатывает прод, когда бэкенд
лежит по причине, к деплою не относящейся.**

Кольцо, ради разрыва которого это написано: исчерпана суточная квота
`rows_read` Durable Objects → любой вызов морды отвечает 500 → канарейка
деплоя краснеет → автооткат возвращает прод на прежнюю версию → а нёс этот
деплой как раз фикс расхода квоты. **Фикс исчерпанной квоты нельзя выкатить,
потому что квота исчерпана.**

Живой случай, дословно из прогона `deploy-dsh-edge` **35989178639**
(2026-09-24 10:50:29Z):

```
##[error]RPC workspace.create канарейки #119: HTTP 500
##[error]RPC workspace.create канарейки #119: тело ответа:
{"ok":false,"error":"Internal runtime error.","code":"internal",
 "detail":"Error: Exceeded allowed rows read in Durable Objects free tier."}
```

и шаг «Автооткат прода при красной канарейке/смоуке: success» — прод откачен.

`deploy-worker.yml` это уже различает (вердикт `backend-down`, #1426), а
`deploy-dsh-edge.yml` — не различал. Одно правило, две реализации по языку
шага (там Node, здесь bash); эта гвардия и держит их в одном смысле.

СТЕНД НАСТОЯЩИЙ. Поднимается `http.server`, отвечающий РОВНО тем, чем ответил
прод (тело выше — дословно), и зовётся НАСТОЯЩИЙ `scripts/lib/canary_http.sh`
настоящим bash. Заглушка `curl` здесь была бы пересказом, который ломается на
исправном коде (AGENTS.md, #1373): вердикт считается по HTTP-коду, а код —
ровно то, что заглушка подделывает.

Запуск: python -m pytest scripts/lib/test_canary_backend_down_guard.py -q
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
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CANARY_LIB = REPO_ROOT / "scripts" / "lib" / "canary_http.sh"
DSH_EDGE_DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-dsh-edge.yml"
WORKER_DEPLOY = REPO_ROOT / ".github" / "workflows" / "deploy-worker.yml"

#: Прод-форма тела: дословно из прогона 35989178639. Не пересказ своими
#: словами (AGENTS.md, «Тест кормит прод-форму данных»).
PROD_QUOTA_BODY = (
    b'{"ok":false,"error":"Internal runtime error.","code":"internal",'
    b'"detail":"Error: Exceeded allowed rows read in Durable Objects free tier."}'
)


class _Responder(http.server.BaseHTTPRequestHandler):
    status = 500
    body = PROD_QUOTA_BODY

    def _reply(self) -> None:
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def do_POST(self) -> None:  # noqa: N802 — имя задаёт BaseHTTPRequestHandler
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._reply()

    def do_GET(self) -> None:  # noqa: N802
        self._reply()

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Responder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _verdict_for(server, status: int, body: bytes = PROD_QUOTA_BODY) -> tuple[str, int]:
    """Настоящий bash, настоящий canary_http.sh, настоящий HTTP-ответ.
    Возвращает (вердикт, код возврата канарейки)."""
    _Responder.status = status
    _Responder.body = body
    port = server.server_address[1]
    with tempfile.TemporaryDirectory() as work:
        script = (
            "set -euo pipefail\n"
            f'source "{CANARY_LIB}"\n'
            f'export CANARY_LAST_CODE_FILE="{work}/last-code"\n'
            ': >"$CANARY_LAST_CODE_FILE"\n'
            "rc=0\n"
            f'canary_http "проба" -X POST "http://127.0.0.1:{port}/api/x" -d \'{{}}\' '
            ">/dev/null 2>&1 || rc=$?\n"
            'printf "%s %s\\n" "$(canary_rollback_verdict)" "$rc"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, f"стенд не поднялся: {result.stderr[:400]}"
    verdict, rc = result.stdout.split()
    return verdict, int(rc)


def test_quota_exhausted_does_not_roll_back(server):
    """Главное: прод-ответ исчерпанной квоты даёт `backend-down`.

    Иначе автооткат снимает деплой, который нёс фикс расхода квоты, — и
    кольцо «фикс нельзя выкатить, потому что квота исчерпана» замыкается
    снова (#1426)."""
    verdict, rc = _verdict_for(server, 500)

    assert rc != 0, "канарейка обязана остаться КРАСНОЙ: деплой не считается удачным"
    assert verdict == "backend-down", (
        "500 от морды с телом исчерпанной квоты обязан давать вердикт "
        "backend-down — иначе откат вернёт прод на версию БЕЗ фикса расхода")


def test_any_5xx_counts_as_backend_down(server):
    """Правило — по КОДУ, а не по тексту тела. Текст завтра изменится, класс
    отказа останется: 502/503 от бэкенда при живой раздаче — та же посторонняя
    поломка, и привязка к словам сделала бы гвардию ложно-зелёной."""
    for status in (500, 502, 503):
        verdict, _rc = _verdict_for(server, status, b'{"error":"whatever"}')
        assert verdict == "backend-down", f"HTTP {status} обязан быть backend-down"


def test_4xx_still_rolls_back(server):
    """Обратная сторона, без которой фикс выродился бы в «не откатывать
    никогда»: 4xx — это ответ приложения, а не лежащий бэкенд. Плохой деплой,
    отдающий 404 на своём же маршруте, обязан откатываться."""
    verdict, rc = _verdict_for(server, 404, b'{"error":"not found"}')

    assert rc != 0
    assert verdict == "deploy", (
        "4xx обязан оставлять откат включённым: иначе сломанный деплой "
        "останется в проде под видом «посторонней поломки»")


def test_success_leaves_verdict_deploy(server):
    """Зелёная канарейка не пишет backend-down: вердикт читает `if:` условие
    шага отката, и ложный backend-down на успехе выключил бы откат для
    СЛЕДУЮЩЕГО красного шага того же прогона (смоук после канарейки)."""
    verdict, rc = _verdict_for(server, 200, b'{"ok":true}')

    assert rc == 0
    assert verdict == "deploy"


def test_no_code_at_all_falls_back_to_rolling_back():
    """«Кода нет вовсе» (сетевой отказ, DNS, TLS) — это НЕ знание о здоровье
    бэкенда. Умолчание обязано остаться откатом: «не знаю» лечится откатом,
    «знаю, что посторонняя» — не лечится."""
    with tempfile.TemporaryDirectory() as work:
        script = (
            "set -euo pipefail\n"
            f'source "{CANARY_LIB}"\n'
            f'export CANARY_LAST_CODE_FILE="{work}/last-code"\n'
            ': >"$CANARY_LAST_CODE_FILE"\n'
            "canary_rollback_verdict\n"
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr[:400]
    assert result.stdout.strip() == "deploy", (
        "пустой вердикт обязан вести к откату — иначе канарейка, упавшая до "
        "первого HTTP-ответа, молча выключала бы газ отката")


def _rollback_condition(workflow: Path, step_name_part: str) -> str:
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps") or []:
            if step_name_part in (step.get("name") or ""):
                return step.get("if") or ""
    raise AssertionError(f"шаг с '{step_name_part}' не найден в {workflow}")


def test_both_deploys_gate_rollback_on_the_verdict():
    """Одно правило — два деплоя. До #1426 `deploy-worker.yml` вердикт знал, а
    `deploy-dsh-edge.yml` нет, и фикс расхода квоты откатывался именно там, где
    расход и живёт. Структурная проверка рядом с поведенческой: поведение выше
    доказывает, что вердикт СЧИТАЕТСЯ верно, а это — что его кто-то ЧИТАЕТ."""
    for workflow in (WORKER_DEPLOY, DSH_EDGE_DEPLOY):
        condition = _rollback_condition(workflow, "Автооткат прода")
        assert "backend-down" in condition, (
            f"{workflow.name}: шаг автооткота не смотрит на вердикт канарейки — "
            "посторонняя поломка бэкенда снова будет откатывать исправный деплой "
            "(#1426), в том числе деплой с фиксом самой этой поломки")


def test_dsh_edge_canary_records_the_verdict():
    """У читателя должен быть писатель: шаг канарейки обязан объявлять `id` и
    писать вердикт в `$GITHUB_OUTPUT`. Без этого условие выше сравнивало бы
    пустую строку — откат работал бы всегда, и гвардия зеленела бы, ничего не
    защищая."""
    text = DSH_EDGE_DEPLOY.read_text(encoding="utf-8")
    assert re.search(r"id:\s*canary_ingest", text), (
        "шаг канарейки ingest-шва потерял id — вердикт некому прочитать")
    assert "CANARY_LAST_CODE_FILE" in text, (
        "канарейка не включает запись кода — canary_rollback_verdict всегда "
        "вернёт 'deploy', то есть фикс #1426 выключится молча")
    assert 'echo "verdict=$verdict" >>"$GITHUB_OUTPUT"' in text, (
        "вердикт не уходит в output шага — читать нечего")
