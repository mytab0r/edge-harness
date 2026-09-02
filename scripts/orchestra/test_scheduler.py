#!/usr/bin/env python3
"""Тесты фикса #174: логин в морду dsh-edge не должен идти по редиректу
303 → GET (urllib.HTTPRedirectHandler по умолчанию), и сбой архивации
сессии раннера после мержа обязан быть громким (fail loud), не тонуть в ⚠️.

Логин проверяется на настоящем HTTP-сервере в отдельном потоке — это и есть
прод-форма контракта (303 + Set-Cookie, docs/research/12-dsh-edge-session-api.md),
а не наш пересказ. Остальная проводка (archive_runner_sessions/after_merge/main)
— на моках gh/urllib, сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_scheduler.py -q
"""

import http.server
import importlib.util
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))  # scheduler.py делает `from pulse_guard import …`

SCRIPT = _DIR / "scheduler.py"
spec = importlib.util.spec_from_file_location("scheduler", SCRIPT)
sch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sch)  # type: ignore[union-attr]


# ── Живой HTTP-сервер: контракт логина морды (303 + Set-Cookie) ──────────────────


class _LoginHandler(http.server.BaseHTTPRequestHandler):
    status = 303

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/api/auth/login":
            self.send_response(self.status)
            if self.status == 303:
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", "__Host-dsh_edge_owner=abc123; Path=/; HttpOnly")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        # Второй GET по Location — то, что раньше делал автослежение urllib и
        # ловил 403 (Origin/кука не те у GET без тела). Если фикс работает,
        # этот путь не должен вызываться вовсе.
        self.send_response(403)
        self.end_headers()

    def log_message(self, *a):  # тише pytest-вывод
        pass


@pytest.fixture()
def login_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _LoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def test_login_303_succeeds_without_following_redirect(login_server, monkeypatch):
    _LoginHandler.status = 303
    port = login_server.server_address[1]
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    opener = sch._morde_opener()
    sch._morde_login(opener)  # не должен бросить: 303 — это успех, не 403


def test_login_non_303_is_loud_runtime_error(login_server, monkeypatch):
    _LoginHandler.status = 500
    port = login_server.server_address[1]
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    opener = sch._morde_opener()
    with pytest.raises(RuntimeError):
        sch._morde_login(opener)


def test_no_redirect_handler_does_not_follow_303():
    # Гвардия класса: если кто-то соберёт opener без _NoRedirect, urllib молча
    # уйдёт вторым GET по Location — здесь фиксируем контракт хендлера отдельно
    # от сети (redirect_request обязан вернуть None на 303).
    handler = sch._NoRedirect()
    import email.message
    headers = email.message.Message()
    headers["Location"] = "/"
    assert handler.redirect_request(None, None, 303, "See Other", headers, "/") is None


# ── archive_runner_sessions: жёсткий сбой отличим от нормы ───────────────────────


def test_archive_no_config_is_not_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "")
    lines, hard = sch.archive_runner_sessions([5])
    assert hard is False
    assert any("не заданы" in line for line in lines)


def test_archive_login_failure_is_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")

    def broken_login(opener):
        raise RuntimeError("логин в морду не удался: HTTP 403")

    monkeypatch.setattr(sch, "_morde_login", broken_login)
    lines, hard = sch.archive_runner_sessions([5])
    assert hard is True
    assert any("сломана" in line for line in lines)


def test_archive_session_not_found_is_not_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sch, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sch, "_morde_rpc",
        lambda opener, method, payload: (_ for _ in ()).throw(RuntimeError("session-not-found: нет такой сессии")),
    )
    lines, hard = sch.archive_runner_sessions([5])
    assert hard is False
    assert any("архивировать нечего" in line for line in lines)


def test_archive_rpc_failure_is_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sch, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sch, "_morde_rpc",
        lambda opener, method, payload: (_ for _ in ()).throw(RuntimeError("internal: что-то сломалось")),
    )
    lines, hard = sch.archive_runner_sessions([5])
    assert hard is True
    assert any("сломана" in line for line in lines)


# ── main(): жёсткий сбой красит прогон ПОСЛЕ мержа, эскалирует одним каналом ─────


def test_main_exits_nonzero_and_escalates_on_archive_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: (["✅ PR #1 слит"], True))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool: [])
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append((repo, issue, text)) or "ок")
    code = sch.main()
    assert code == 1  # прогон окрашен красным — мерж уже состоялся, но поломка не молчит
    assert escalated and escalated[0][0] == "o/r" and escalated[0][1] == sch.WATCHDOG_ISSUE


def test_main_stays_green_when_archive_ok(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: (["✅ PR #1 слит"], False))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool: [])
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("не должен эскалировать — сбоя не было"))
    assert sch.main() == 0
