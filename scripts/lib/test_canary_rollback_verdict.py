#!/usr/bin/env python3
"""Гвардия: красная канарейка различает «деплой плохой» и «бэкенд лежит» (#1426).

Чем оплачено. Прогон 35639422589 (`deploy-worker.yml`, job `deploy` на
`61b185aa`, слияние PR #1420): деплой состоялся, канарейка упала по таймауту
`waiting for locator('#gate-token')`, и `wrangler rollback` откатил КОРРЕКТНЫЙ
код. Поле не появилось не из-за деплоя: была исчерпана суточная квота
rows_read Durable Objects (#1411) — `/api/status` отдавал 500, страница не
инициализировалась, статика при этом отдавалась нормально. Кольцо: фикс
квоты нельзя задеплоить, потому что квота исчерпана.

Стенд настоящий: поднимается РЕАЛЬНЫЙ `http.server` и зовётся РЕАЛЬНЫЙ
`node cf-worker/scripts/canary-ui.mjs` — прямое требование AGENTS.md
(«Заглушка внешнего инструмента — это пересказ, и она ломается на исправном
коде»). Подменять сам node-скрипт заглушкой значило бы проверять пересказ.

Запуск: python -m pytest scripts/lib/test_canary_rollback_verdict.py -q
"""

import http.server
import json
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

# Тот же маршрут, тот же код HTTP, но отказ НЕ распознан как инфраструктурный.
GENERIC_BODY = json.dumps({"error": {"code": "internal", "message": "boom"}}).encode("utf-8")

# Страница Cloudflare при исключении в воркере — вообще не наш JSON.
CLOUDFLARE_1101 = b"error code: 1101"


class _Handler(http.server.BaseHTTPRequestHandler):
    body = QUOTA_BODY
    status = 500

    def do_GET(self):  # noqa: N802 — имя задано базовым классом
        if self.path.startswith("/api/ready"):
            self.send_response(self.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)
            return
        page = b"<html><body><input id=gate-token></body></html>"
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


def _serve(body: bytes, status: int = 500):
    handler = type("H", (_Handler,), {"body": body, "status": status})
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


def test_quota_exhaustion_is_backend_down_and_forbids_rollback():
    """Живой случай #1426 целиком: воркер сам назвал отказ квотой — канарейка
    обязана выйти кодом 3 («бэкенд лежит»), а не 1 («деплой плохой»). По этому
    коду шаг автооката и не вызывает `wrangler rollback`."""
    server = _serve(QUOTA_BODY)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "BACKEND-DOWN" in result.stderr, result.stderr
    # Алерт называет факт, а не гипотезы (AGENTS.md).
    assert "квота" in result.stderr
    assert "откат" in result.stderr.lower()


def test_unrecognized_failure_still_counts_as_bad_deploy():
    """Обратная ветка, и она важнее первой: умолчание консервативное. Отказ,
    который воркер НЕ назвал квотой, остаётся «виноват деплой» — иначе
    сломанный деплой, положивший API, перестал бы откатываться, и канарейка
    лишилась бы своей главной работы."""
    server = _serve(GENERIC_BODY)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode != 3, (result.returncode, result.stderr)
    assert "BACKEND-DOWN" not in result.stderr
    # Положительный признак, а не одно «не 3»: без него тест зеленел бы и
    # тогда, когда канарейка вообще не дошла до зонда (например не нашла
    # playwright) — ровно тот ложно-зелёный, о котором AGENTS.md.
    assert "НЕ распознан как инфраструктурный" in result.stderr, result.stderr


def test_cloudflare_error_page_is_not_recognized_as_infrastructure():
    """Честный потолок, закреплённый тестом. Если воркер падает ДО своего
    обработчика, Cloudflare отдаёт собственную страницу `error code: 1101` —
    в ней нет ни кода, ни признака причины. Такой отказ неотличим от
    сломанного деплоя, и канарейка обязана вести себя как раньше (откат), а
    не угадывать. Тест стоит здесь, чтобы следующий читатель не принял
    «распознаём квоту» за «распознаём любую инфраструктуру»."""
    server = _serve(CLOUDFLARE_1101)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode != 3, (result.returncode, result.stderr)
    assert "НЕ распознан как инфраструктурный" in result.stderr, result.stderr


def test_healthy_ready_does_not_short_circuit():
    """Зонд не подменяет собой канарейку: при здоровом `/api/ready`
    исполнение обязано идти дальше, в браузерную часть. Признак — канарейка
    НЕ завершилась кодом 3 и не напечатала вердикт бэкенда (дальше она
    упадёт на отсутствии настоящей морды, и это правильный «деплой плохой»)."""
    server = _serve(json.dumps({"ok": True}).encode("utf-8"), status=200)
    try:
        result = _run_canary(server.server_address[1])
    finally:
        server.shutdown()
    assert result.returncode != 3, (result.returncode, result.stderr)
    assert "BACKEND-DOWN" not in result.stderr
    assert "бэкенд отвечает" in result.stdout, (result.stdout, result.stderr)
