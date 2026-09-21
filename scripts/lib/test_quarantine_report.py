#!/usr/bin/env python3
"""Гвардия «состав карантина доезжает до лога деплоя» (issue #1379).

Класс, ради которого задача заведена: «карантин пуст» и «состав прочитать не
смогли» — РАЗНЫЕ исходы, и второй не имеет права выглядеть как первый. Патч
0007 (#1376) изолирует сессию, которую билд не может перевести в текущий
формат; состав изоляции до #1379 был недоступен прогону деплоя
(`console.error` уходит в Workers-логи, таблица живёт внутри Durable Object),
и владелец узнавал его только руками.

Проверка ПОВЕДЕНЧЕСКАЯ: поднимается настоящий HTTP-сервер, через
`scripts/lib/canary_http.sh` идёт настоящий curl, разбирает настоящий jq.
«Маршрут недоступен» воспроизводится ЗАКРЫТЫМ ПОРТОМ, а не подменённой
функцией — прямое требование AGENTS.md («Заглушка внешнего инструмента — это
пересказ», #1373/PR #1380): подменённый curl понимает ровно ту форму вызова,
под которую написан, и краснеет на следующем изменении формы при исправном
прод-коде. Текстовая проверка исходника доказала бы орфографию, а не
поведение (класс #891/#893).

Что гвардия поймала на живом коде, до всякого ревью — две штуки, обе мои:
  1) `jq '.quarantined | length'` на теле БЕЗ поля даёт 0 (`null | length`),
     то есть ответ `{"ok":true}` без массива выглядел как ПУСТОЙ карантин —
     ровно silent-wrong, против которого задача. Лечение: спрашивать тип, а
     не длину;
  2) текст отказа содержал фразу «карантин пуст» внутри отрицания — grep и
     беглый взгляд читают её как утверждение. Лечение: фраза убрана из
     отказов вовсе.

Запуск: python -m pytest scripts/lib/test_quarantine_report.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import socket
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

LIB = Path(__file__).resolve().parent
CANARY = LIB / "canary_http.sh"
REPORT = LIB / "quarantine_report.sh"

needs_tools = pytest.mark.skipif(
    shutil.which("curl") is None or shutil.which("jq") is None,
    reason="нужны настоящие curl и jq: подменять их запрещено (AGENTS.md)")


class _Handler(BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):  # noqa: N802 — имя требует BaseHTTPRequestHandler
        if self.path != "/api/quarantine":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args):  # тишина в выводе теста
        pass


@pytest.fixture
def server():
    """Поднимает настоящий сервер, отдающий заданное тело на /api/quarantine.
    Возвращает базовый URL — ровно в той форме, в какой его передаёт деплой."""
    started = []

    def factory(body: str) -> str:
        handler = type("Bound", (_Handler,), {"payload": body.encode("utf-8")})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield factory
    for httpd in started:
        httpd.shutdown()


def dead_base() -> str:
    """Адрес, на котором заведомо никто не слушает: порт занят и сразу
    освобождён. Именно закрытый порт, а не подменённый curl."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def run_report(base: str, tmp_path) -> subprocess.CompletedProcess:
    """Зовёт quarantine_report ровно так, как зовёт шаг деплоя: после
    `source` обоих файлов, с cookie-jar'ом, под `set -euo pipefail`."""
    jar = tmp_path / "cookies"
    jar.write_text("", encoding="utf-8")
    script = (
        f'set -euo pipefail\n'
        f'source "{CANARY}"\n'
        f'source "{REPORT}"\n'
        f'quarantine_report "{base}" "{jar}"\n'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          encoding="utf-8")


@needs_tools
def test_empty_quarantine_is_named_empty_and_does_not_fail_the_step(server, tmp_path):
    """Штатное состояние: ни одна сессия не осталась непереведённой. Факт
    ПРОЧИТАН с прода, а не предположен по молчанию."""
    result = run_report(server('{"ok":true,"quarantined":[]}'), tmp_path)

    assert result.returncode == 0, result.stderr
    assert "карантин пуст" in result.stdout, result.stdout


@needs_tools
def test_non_empty_prints_every_session_and_still_does_not_fail_the_step(server, tmp_path):
    """Состав виден целиком — id, формат, причина каждой сессии. И шаг при
    этом НЕ краснеет: иначе одна старая запись валила бы каждый деплой, ровно
    тот исход, против которого делался #1376."""
    result = run_report(server(
        '{"ok":true,"quarantined":['
        '{"id":"sess-a","storedVersion":2,"reason":"no migration path to v3","observedAt":1},'
        '{"id":"sess-b","storedVersion":1,"reason":"corrupt header","observedAt":2}]}'), tmp_path)
    out = result.stdout + result.stderr

    assert result.returncode == 0, result.stderr
    for token in ("sess-a", "sess-b", "no migration path to v3", "corrupt header"):
        assert token in out, f"в логе нет «{token}»: {out}"
    assert "карантин пуст" not in out, out


@needs_tools
def test_unreachable_route_is_a_read_failure_not_an_empty_quarantine(tmp_path):
    """Главная сцена задачи: молчащий маршрут не имеет права выглядеть как
    пустой карантин. Закрытый порт — настоящая недоступность."""
    result = run_report(dead_base(), tmp_path)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "карантин пуст" not in out, out
    assert "НЕ прочитан" in out, out


@needs_tools
def test_unparsable_body_is_the_same_class_of_dont_know(server, tmp_path):
    """200 с HTML-страницей прокси — тот же класс «не знаю», что и молчащий
    маршрут, и лечится он не как пустой карантин."""
    result = run_report(server("<html>502 Bad Gateway</html>"), tmp_path)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "карантин пуст" not in out, out
    assert "НЕ прочитан" in out, out


@needs_tools
def test_json_without_the_field_is_a_read_failure_not_an_empty_quarantine(server, tmp_path):
    """Живой дефект, пойманный этой гвардией до ревью: `.quarantined | length`
    на теле БЕЗ поля даёт 0 — ответ `{"ok":true}` выглядел как пустой
    карантин. Поэтому сначала спрашивается тип, и только потом длина."""
    result = run_report(server('{"ok":true}'), tmp_path)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "карантин пуст" not in out, out
    assert "НЕ прочитан" in out, out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
