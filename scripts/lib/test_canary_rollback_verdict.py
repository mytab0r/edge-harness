#!/usr/bin/env python3
"""Гвардия: красная канарейка различает «деплой плохой» и «бэкенд лежит» (#1426).

Чем оплачено. Прогон 35639422589 (`deploy-worker.yml`, job `deploy` на
`61b185aa`, слияние PR #1420): деплой состоялся, канарейка упала по таймауту
`waiting for locator('#gate-token')`, и `wrangler rollback` откатил КОРРЕКТНЫЙ
код. Поле не появилось не из-за деплоя: была исчерпана суточная квота
rows_read Durable Objects (#1411) — живой зонд того же периода: `GET
/api/status → 500, error code: 1101` при `GET / → 200`. Статика отдавалась
нормально. Кольцо: фикс квоты нельзя задеплоить, потому что квота исчерпана.

Стенд настоящий: поднимается РЕАЛЬНЫЙ `http.server` и зовётся РЕАЛЬНЫЙ
`node cf-worker/scripts/canary-ui.mjs` — прямое требование AGENTS.md
(«Заглушка внешнего инструмента — это пересказ, и она ломается на исправном
коде»). Подменять сам node-скрипт заглушкой значило бы проверять пересказ.
Тела ответов — прод-формы: JSON `storageErrorResponse` воркера и служебная
страница Cloudflare из живого случая.

Запуск: python -m pytest scripts/lib/test_canary_rollback_verdict.py -q
"""

import http.server
import json
import socket
import os
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CANARY = REPO_ROOT / "cf-worker" / "scripts" / "canary-ui.mjs"

# Прод-форма ответа воркера при исчерпанной квоте хранилища: собирается
# `storageErrorResponse` (cf-worker/src/harness.ts) — код + сообщение внутри
# `error`. Не пересказ: та же форма проверяется юнит-тестами воркера.
QUOTA_BODY = json.dumps({"error": {
    "code": "storage_quota_exceeded",
    "message": "Хранилище DO вернуло похожую на квоту ошибку: "
               "Error: Exceeded allowed rows read in Durable Objects free tier.",
}}).encode("utf-8")

# РЕАЛЬНОЕ тело живого случая #1426: воркер упал до собственного обработчика,
# Cloudflare отдал служебную страницу. Не наш JSON — и всё равно «бэкенд
# лежит», а не «деплой плохой»: статика при этом отдаётся.
CLOUDFLARE_1101 = b"error code: 1101"

# Тот же маршрут, тот же код HTTP, но тело причины не называет.
GENERIC_BODY = json.dumps({"error": {"code": "internal", "message": "boom"}}).encode("utf-8")

# Исправная статика: страница морды с элементами гейта.
PAGE = b"<html><body><input id=gate-token><button id=gate-enter></button></body></html>"


class _Handler(http.server.BaseHTTPRequestHandler):
    static_status = 200
    api_status = 500
    api_body = QUOTA_BODY

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        if self.path.startswith("/api/"):
            # Канарейка зондит /api/ready, но 500 на ВСЕМ /api/* — прод-форма
            # живого случая: квота отказывает любому SELECT, включая /api/status.
            self.send_response(self.api_status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(self.api_body)))
            self.end_headers()
            self.wfile.write(self.api_body)
            return
        page = PAGE if self.static_status == 200 else CLOUDFLARE_1101
        self.send_response(self.static_status)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


def _serve(api_body: bytes = QUOTA_BODY, api_status: int = 500, static_status: int = 200):
    handler = type("H", (_Handler,), {
        "api_body": api_body, "api_status": api_status, "static_status": static_status,
    })
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run_canary(port: int) -> subprocess.CompletedProcess:
    env = dict(os.environ, HANDS_TOKEN="canary-test-token")
    return subprocess.run(
        ["node", str(CANARY), "--url", f"http://127.0.0.1:{port}"],
        cwd=REPO_ROOT / "cf-worker", capture_output=True, text=True,
        encoding="utf-8", env=env, timeout=180,
    )


def _node_available() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=30, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _node_available(), reason="node недоступен")


# ── Исход «бэкенд лежит»: красный громко, откат НЕ вызывается ────────────────

def test_incident_5xx_with_healthy_static_forbids_rollback():
    """Критерий готовности 1 в лоб: канарейке подсунут 5xx от /api/* при
    исправной статике — прогон красный, но `wrangler rollback` НЕ вызывается.
    Проверка по факту: шаг автооката запускает откат при любом коде, КРОМЕ 3
    (условие `steps.canary_ui.outputs.verdict != 'backend-down'` в
    deploy-worker.yml, прибито гвардией canary_rollback_guard.py), поэтому
    код возврата 3 здесь и есть «отката не было». Тело — РЕАЛЬНОЕ живого
    случая: страница Cloudflare error code: 1101, не наш JSON."""
    server = _serve(api_body=CLOUDFLARE_1101)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    # Исход назван (критерий готовности 3), и решение названо. «1101» обязана
    # быть в НАЗВАННОЙ причине («воркер упал до собственного обработчика»), а
    # не только в цитате тела: иначе точность распознавания не прибита.
    assert "BACKEND-DOWN" in result.stderr, result.stderr
    assert "упал до собственного обработчика" in result.stderr, result.stderr
    assert "1101" in result.stderr, result.stderr
    assert "НЕ откатываю" in result.stderr, result.stderr
    assert "прежняя версия упрётся" in result.stderr, result.stderr


def test_worker_named_quota_names_the_fact():
    """Второй облик того же отказа: воркер не упал до обработчика и назвал
    квоту сам (storageErrorResponse → storage_quota_exceeded). Исход тот же
    (код 3, отката нет), а текст отказа называет причину ФАКТОМ — включая
    лечение (сброс 00:00 UTC), а не списком гипотез."""
    server = _serve(api_body=QUOTA_BODY)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "BACKEND-DOWN" in result.stderr, result.stderr
    assert "квота" in result.stderr, result.stderr
    assert "00:00 UTC" in result.stderr, result.stderr
    assert "НЕ откатываю" in result.stderr, result.stderr


def test_unrecognized_5xx_is_still_backend_down():
    """Тело причины не назвало — исход НЕ меняется (правило задачи: ЛЮБОЙ
    5xx при отдающейся статике не откатывается), а текст называет сам факт:
    статус и тело ответа, без списка гипотез («Алерт не гадает»)."""
    server = _serve(api_body=GENERIC_BODY)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "BACKEND-DOWN" in result.stderr, result.stderr
    assert "отдаёт 500" in result.stderr, result.stderr
    assert "boom" in result.stderr, result.stderr


# ── Обратная ветка (критерий готовности 2): деплой плохой — откат остаётся ───

def test_static_down_is_deploy_bad_and_rolls_back():
    """Битая статика по-прежнему ведёт к откату: канарейка выходит кодом 1
    («деплой плохой»), по которому шаг автооката откатывает. 5xx от API тут
    не спасает: статика не отдаётся — постановка задачи называет это виной
    деплоя."""
    server = _serve(static_status=500, api_body=GENERIC_BODY)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert "DEPLOY-BAD" in result.stderr, result.stderr
    assert "откат оправдан" in result.stderr, result.stderr
    assert "BACKEND-DOWN" not in result.stderr, result.stderr


def test_healthy_probes_go_to_browser_not_to_verdicts():
    """Зонды не подменяют собой канарейку: при исправной статике и API
    исполнение обязано идти дальше, в браузерную часть. Признак — канарейка
    НЕ завершилась ни кодом 3 (бэкенд лежит), ни кодом 1 на уровне зондов:
    дальше она упадёт на отсутствии настоящей морды, и это правильный
    «деплой плохой»."""
    server = _serve(api_body=json.dumps({"ok": True}).encode("utf-8"), api_status=200)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode != 3, (result.returncode, result.stderr)
    assert "BACKEND-DOWN" not in result.stderr, result.stderr
    assert "DEPLOY-BAD" not in result.stderr, result.stderr
    assert "бэкенд отвечает" in result.stdout, (result.stdout, result.stderr)


def test_unanswered_probes_take_no_verdict_and_go_to_browser():
    """Консервативная ветка зонда (находка AI-ревью PR #1441): сеть не
    ответила вовсе (оба зонда упали) — решение НЕ принимается, канарейка
    идёт в браузер. Замыкание сети здесь честное: закрытый порт, не
    подставной код возврата (AGENTS.md: «недоступность» воспроизводится
    закрытым портом, не подставным кодом)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()  # порт свободен и никто его не слушает
    result = _run_canary(dead_port)
    assert result.returncode != 3, (result.returncode, result.stderr)
    assert "BACKEND-DOWN" not in result.stderr, result.stderr
    assert "DEPLOY-BAD" not in result.stderr, result.stderr
    # Решение не принято — канал ушёл в браузерную часть, а не в вердикт.
    assert "дальше решает браузер" in result.stdout, (result.stdout, result.stderr)
