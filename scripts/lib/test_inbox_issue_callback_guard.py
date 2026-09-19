#!/usr/bin/env python3
"""Гвардия #1381: подтверждение issue-created не теряет ни причину отказа,
ни сам факт отказа.

Два дефекта одного шага `.github/workflows/inbox-issue.yml` («Подтвердить
создание issue в DO»), и оба — про потерю правды об ответе:

1. **Причина отказа выбрасывалась.** Вызов шёл `curl -fsS`, а `-f` придуман
   ровно для того, чтобы тело на HTTP-ошибке не отдавать. DO отвечает
   осмысленно — `{"error":{"code":"need_claimed_ts"}}` на 400, — и эта
   причина превращалась в «curl: (22) The requested URL returned error».
   Тот же класс, что #1371 (канарейки деплоя) и #1373 (клиенты рук).

2. **Отказ при 2xx не замечался вовсе.** Устаревший `claimed_ts` DO отвечает
   **200** с `{"accepted": false}`: ватчдог уже увёл сообщение другой
   проходке, и запись ЭТОГО job'а отброшена. `curl -f` такой ответ пропускает,
   шаг выходил нулём — issue создана, DO о ней не знает, дубль всплывает позже
   и никто не связывает его с этим прогоном. Это silent-wrong: «принято» и
   «отброшено» были неотличимы по коду возврата.

   Второй дефект найден ПРИ починке первого и здесь же закрыт — не потому,
   что задача #1381 его называла (она про тело ответа), а потому что оставить
   знакомый silent-wrong, увидев его, значит сознательно отгрузить дефект.
   Названо вслух в теле PR, а не подшито молча.

Проверка ПОВЕДЕНЧЕСКАЯ: исполняется РЕАЛЬНЫЙ bash-скрипт шага, извлечённый
из workflow (тот же приём, что scripts/lib/test_deploy_post_rollback_canary_
guard.py, #1170), против настоящего HTTP-сервера. Прод-формы ответов взяты
из тестов самого DO — cf-worker/test/harness.spec.ts, — а не выдуманы:
`{"accepted":true,"action":"issue_created"}`, `{"accepted":false}`,
`{"error":{"code":"need_claimed_ts"}}` c кодом 400.

Запуск: python -m pytest scripts/lib/test_inbox_issue_callback_guard.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "inbox-issue.yml"
STEP_MARKER = "/api/messages/issue-created"

# Прод-формы ответов DO — из cf-worker/test/harness.spec.ts, не пересказ.
ACCEPTED = {"accepted": True, "action": "issue_created"}
STALE = {"accepted": False}
NEED_CLAIMED_TS = {"error": {"code": "need_claimed_ts"}}


def _callback_step() -> dict:
    assert WORKFLOW.exists(), (
        f"{WORKFLOW} исчез или переименован — обнови путь в гвардии сознательной "
        "правкой, а не молчаливым обходом"
    )
    with WORKFLOW.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    matches = [
        step
        for job in (doc.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
        if STEP_MARKER in (step.get("run") or "")
    ]
    assert len(matches) == 1, (
        f"ожидался ровно один шаг, зовущий {STEP_MARKER}, найдено {len(matches)} — "
        "workflow уехал от этой гвардии, почини её сознательно"
    )
    return matches[0]


class _Handler(BaseHTTPRequestHandler):
    status = 200
    payload = json.dumps(ACCEPTED).encode("utf-8")

    def do_POST(self):  # noqa: N802 — имя требует BaseHTTPRequestHandler
        self.send_response(type(self).status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def harness_do():
    started = []

    def factory(status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        handler = type("Bound", (_Handler,), {"status": status, "payload": payload})
        httpd = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield factory
    for httpd in started:
        httpd.shutdown()


def _run_callback(url: str, tmp_path: Path, create_outcome: str = "success"):
    """Исполняет РЕАЛЬНЫЙ скрипт шага из workflow (не пересказ).

    `$GITHUB_WORKSPACE` указывает на корень репозитория — ровно как в job'е,
    где шаг подключает по нему общую библиотеку. Подменять её нельзя: тогда
    проверялся бы не тот код.
    """
    env = dict(os.environ)
    env.update({
        "GITHUB_WORKSPACE": str(REPO_ROOT),
        "HANDS_URL": url,
        "HANDS_TOKEN": "stub-token",
        "MESSAGE_ID": "77",
        "CLAIMED_TS": "1750000000000",
        "ISSUE_NUMBER": "4242",
        "ISSUE_URL": "https://github.com/mytab0r/edge-harness/issues/4242",
        "CREATE_OUTCOME": create_outcome,
    })
    return subprocess.run(
        ["bash", "-c", _callback_step()["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=60,
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_refusal_reason_reaches_the_log(harness_do, tmp_path):
    """Дефект 1: причина, которую DO назвал прямо, обязана доехать до лога."""
    url = harness_do(400, NEED_CLAIMED_TS)
    result = _run_callback(url, tmp_path)

    assert result.returncode != 0, "400 обязан ронять шаг"
    assert "HTTP 400" in result.stderr, result.stderr
    assert "need_claimed_ts" in result.stderr, (
        "код причины из тела ответа обязан быть в логе — ровно его выбрасывал "
        f"curl -f (#1371/#1381); вывод: {result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_stale_claim_is_not_silently_accepted(harness_do, tmp_path):
    """Дефект 2, тот самый silent-wrong: 200 + accepted:false обязан ронять шаг.

    Раньше `curl -f` пропускал такой ответ и job выходил нулём: issue создана,
    DO о ней не знает. Дубль всплывал позже, и связать его с этим прогоном было
    нечем."""
    url = harness_do(200, STALE)
    result = _run_callback(url, tmp_path)

    assert result.returncode != 0, (
        "200 с accepted:false — ОТКАЗ приёма, а не успех: ватчдог увёл сообщение "
        f"другой проходке, запись этого job'а отброшена; вывод: {result.stderr!r}"
    )
    assert "accepted" in result.stderr, result.stderr
    assert "77" in result.stderr, (
        "сообщение обязано назвать id, по которому искать дубль: "
        f"{result.stderr!r}"
    )


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_accepted_confirmation_stays_green_and_quiet(harness_do, tmp_path):
    """Нормальный путь не должен ни падать, ни шуметь — иначе лог утонет, а
    зелёное станет неотличимо от красного."""
    url = harness_do(200, ACCEPTED)
    result = _run_callback(url, tmp_path)

    assert result.returncode == 0, f"{result.stdout!r} {result.stderr!r}"
    assert "::error::" not in result.stderr, result.stderr


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl недоступен в этой среде")
def test_server_error_reaches_the_log_too(harness_do, tmp_path):
    """Не только 400: пятисотка DO тоже обязана приносить тело, иначе разбор
    «почему сообщение зависло» снова начинается вслепую."""
    url = harness_do(500, {"error": {"code": "internal", "detail": "storage unavailable"}})
    result = _run_callback(url, tmp_path)

    assert result.returncode != 0
    assert "HTTP 500" in result.stderr, result.stderr
    assert "storage unavailable" in result.stderr, result.stderr


def test_step_sources_library_before_first_use():
    """Порядок, а не только наличие: `source` обязан стоять ДО первого вызова.
    Собственная ошибка первой итерации PR #1372 — вызов строкой выше сёрса,
    который в прогоне даёт «canary_http: command not found». Проверка дешёвая,
    класс ошибки — нет."""
    script = _callback_step()["run"]
    source_at = script.find("scripts/lib/canary_http.sh")
    call_at = script.find("canary_http \"")
    assert source_at != -1, "шаг больше не подключает общую библиотеку"
    assert call_at != -1, "шаг больше не зовёт canary_http"
    assert source_at < call_at, (
        "source стоит ПОСЛЕ первого вызова — в прогоне шаг упадёт на "
        "«command not found», а не на том, что проверяет"
    )
