#!/usr/bin/env python3
"""Гвардия «канарейка печатает тело ответа» (issue #1371).

Проверка ПОВЕДЕНЧЕСКАЯ, не текстовая: поднимается настоящий HTTP-сервер,
через `scripts/lib/canary_http.sh` идёт настоящий запрос настоящим curl, и
проверяется настоящий вывод. Текстовая проверка исходника на подстроку
(`--fail-with-body`, отсутствие `-f`) доказала бы только орфографию — ровно
тот класс ложно-зелёных гвардий, который AGENTS.md разбирает на примере
worktree-cleanup (#891/#893).

Класс, который держит гвардия: шаг CI, знающий и код, и тело ответа, не имеет
права печатать только код. Живой случай — прогон deploy-dsh-edge.yml
35387441612: «curl: (22) The requested URL returned error: 500» и больше
ничего, хотя морда ответила осмысленным JSON.

Запуск: python -m pytest scripts/lib/test_canary_http_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

LIB = Path(__file__).resolve().parent / "canary_http.sh"

# Прод-форма отказа морды: именно такой JSON отдаёт dsh-edge на ошибке RPC
# (см. edge-api.ts, ветка `err`), и именно его выбрасывал `curl -f`.
ERROR_BODY = {
    "type": "server-response",
    "rpcId": "canary",
    "result": {"ok": False, "error": {"message": "Workspace registry is unavailable."}},
}
OK_BODY = {"type": "server-response", "rpcId": "canary", "result": {"ok": True, "value": {"workspaceId": "w-1"}}}


class _Handler(BaseHTTPRequestHandler):
    status = 500
    payload = json.dumps(ERROR_BODY).encode("utf-8")

    def do_POST(self):  # noqa: N802 — имя требует BaseHTTPRequestHandler
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    do_GET = do_POST

    def log_message(self, *args):  # тишина в выводе теста
        pass


@pytest.fixture
def server():
    def start(status: int, payload: bytes):
        handler = type("Bound", (_Handler,), {"status": status, "payload": payload})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, f"http://127.0.0.1:{httpd.server_port}/api/workspace.create"
    started = []

    def factory(status, payload):
        httpd, url = start(status, payload)
        started.append(httpd)
        return url
    yield factory
    for httpd in started:
        httpd.shutdown()


def run_canary(url: str, label: str = "Канарейка ingest-шва (#119)", env: dict | None = None):
    script = (
        f'set -euo pipefail\n'
        f'source "{LIB}"\n'
        f'canary_http "{label}" -X POST "{url}" -d \'{{"probe":1}}\'\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          encoding="utf-8", env=env)


def run_probe(url: str, expected: str, label: str = "Канарейка прода"):
    """Вызов canary_probe ровно в той форме, в какой его зовёт workflow:
    code=$(canary_probe ...) — то есть stdout уходит в подстановку, и любой
    шум в нём сломал бы сравнение кода у вызывающего."""
    script = (
        f'set -euo pipefail\n'
        f'source "{LIB}"\n'
        f'code=$(canary_probe "{label}" {expected} "{url}")\n'
        f'printf "КОД=%s" "$code"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, encoding="utf-8")


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_non_2xx_prints_code_and_body(server):
    """Главное требование: и код, и ПРИЧИНА видны в логе job'а."""
    url = server(500, json.dumps(ERROR_BODY).encode("utf-8"))
    result = run_canary(url)

    assert result.returncode != 0, "не-2xx обязан завершать шаг ошибкой"
    assert "HTTP 500" in result.stderr, result.stderr
    # Ровно то, что выбрасывал `curl -f`: текст причины из тела ответа.
    assert "Workspace registry is unavailable." in result.stderr, result.stderr
    # Метка шага — чтобы в логе было видно, КАКАЯ канарейка упала.
    assert "Канарейка ingest-шва (#119)" in result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_2xx_body_goes_to_stdout_unchanged(server):
    """Успех обязан вести себя как прежний `curl -fsS`: тело в stdout,
    код возврата 0 — иначе подстановка в пайплайны с jq сломается."""
    url = server(200, json.dumps(OK_BODY).encode("utf-8"))
    result = run_canary(url)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["result"]["value"]["workspaceId"] == "w-1"
    assert result.stderr.strip() == "", f"на успехе шаг не шумит: {result.stderr}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_empty_error_body_is_named_as_empty_not_silently_skipped(server):
    """Пустое тело — отдельный честный факт, а не молчание: «сервер не сказал
    причину» и «шаг потерял причину» лечатся по-разному."""
    url = server(502, b"")
    result = run_canary(url)

    assert result.returncode != 0
    assert "HTTP 502" in result.stderr
    assert "тело ответа пустое" in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_huge_body_is_truncated_and_says_so(server):
    """HTML-простыня от прокси не имеет права утопить лог, но факт усечения
    называется числом — читатель должен знать, что увидел не всё."""
    url = server(503, b"X" * 50_000)
    result = run_canary(url)

    assert result.returncode != 0
    assert "показаны первые" in result.stderr, result.stderr
    assert "50000 байт" in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_network_failure_says_there_was_no_http_answer():
    """Сетевой отказ и HTTP-ошибка — разные причины: во втором случае сервер
    ответил, в первом до него не дошли. Шаг обязан их различать."""
    # Порт, на котором заведомо никто не слушает.
    result = run_canary("http://127.0.0.1:1/api/workspace.create")

    assert result.returncode != 0
    assert "запрос не состоялся" in result.stderr, result.stderr
    assert "HTTP-ответа нет" in result.stderr

# ── canary_probe: тихая на ожидаемом коде проба (находка ai-review PR #1372) ──


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_probe_is_silent_when_code_is_the_expected_one(server):
    """Проба корня выполняется на КАЖДОМ успешном деплое. Печатать тело на
    успехе значило бы заливать HTML страницы морды в лог каждый раз — ровно
    поэтому у пробы контракт другой, чем у canary_http."""
    url = server(200, b"<!doctype html><html><body>login page</body></html>")
    result = run_probe(url, "200")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "КОД=200", result.stdout
    assert result.stderr.strip() == "", f"на ожидаемом коде проба молчит: {result.stderr}"


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_probe_prints_body_when_code_differs_from_expected(server):
    """Не тот код — причина обязана быть видна, и код обязан дойти до
    вызывающего: вердикт принимает он, а не проба."""
    url = server(502, b"error code: 1042")
    result = run_probe(url, "200")

    assert result.stdout == "КОД=502", result.stdout
    assert "HTTP 502, ожидался 200" in result.stderr, result.stderr
    assert "error code: 1042" in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_probe_treats_303_login_as_success_not_as_failure(server):
    """Логин канареек успешен РОВНО на 303. Будь у пробы контракт «любой 2xx»,
    успешный логин считался бы отказом, а его тело лилось бы в лог каждого
    деплоя — поэтому ожидаемый код передаётся аргументом."""
    url = server(303, b"")
    result = run_probe(url, "303", label="Логин канарейки #119")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "КОД=303", result.stdout
    assert result.stderr.strip() == "", result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_probe_returns_000_and_says_so_when_there_was_no_http_answer():
    """Сетевой отказ: вызывающий сравнит 000 с ожидаемым и упадёт, а в логе
    будет сказано, что HTTP-ответа не было вовсе — это другой диагноз, чем
    «ответил не тем кодом»."""
    result = run_probe("http://127.0.0.1:1/", "200")

    assert result.stdout == "КОД=000", result.stdout
    assert "запрос не состоялся" in result.stderr, result.stderr
    assert "HTTP-ответа нет" in result.stderr



if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_default_annotation_level_is_error(server):
    """Умолчание — `::error::`: канарейка обязана быть громкой без всяких
    настроек. Проверка нужна ровно потому, что уровень стал переменной:
    опечатка в умолчании (`::warning::` вместо `::error::`) превратила бы
    КАЖДУЮ канарейку в тихую, а красный job — в жёлтую строку лога."""
    url = server(500, json.dumps(ERROR_BODY).encode("utf-8"))
    result = run_canary(url)

    assert "::error::" in result.stderr, result.stderr
    assert "::warning::" not in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_lowered_annotation_level_still_prints_the_body(server):
    """Шаг «Снимок версии прода до деплоя» объявляет свой отказ НЕ фатальным
    (газ назван: автооткат дальше откажется явно), поэтому печатает
    `::warning::`. Понижение уровня не имеет права утаскивать за собой тело
    ответа — иначе класс #1371 возвращается через чёрный ход: причина снова
    известна шагу и снова не видна человеку."""
    url = server(403, json.dumps(
        {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    ).encode("utf-8"))
    script = (
        f'set -euo pipefail\n'
        f'source "{LIB}"\n'
        f'CANARY_ERROR_LEVEL=warning\n'
        f'canary_http "Снимок версии прода до деплоя" "{url}" || true\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            encoding="utf-8")

    assert "::warning::" in result.stderr, result.stderr
    assert "::error::" not in result.stderr, (
        "уровень объявлен warning — ни одной error-аннотации быть не должно: "
        f"{result.stderr}"
    )
    assert "HTTP 403" in result.stderr, result.stderr
    assert "Authentication error" in result.stderr, (
        f"тело обязано печататься и на пониженном уровне: {result.stderr}"
    )
