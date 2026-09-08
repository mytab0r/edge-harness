#!/usr/bin/env python3
"""Тесты фикса #174: логин в морду dsh-edge не должен идти по редиректу
303 → GET (urllib.HTTPRedirectHandler по умолчанию), и сбой архивации
сессии раннера после мержа обязан быть громким (fail loud), не тонуть в ⚠️.

Логин проверяется на настоящем HTTP-сервере в отдельном потоке — это и есть
прод-форма контракта (303 + Set-Cookie, docs/research/12-dsh-edge-session-api.md),
а не наш пересказ. Остальная проводка (archive_runner_sessions/after_merge/main)
— на моках gh/urllib, сеть не нужна.

Плюс тесты петли состояния открытого PR (#196). Три поведения: (1) PR с
review:ok без вердикта AI (или ai:failed) дольше порога — оркестратор сам
запускает ai-review.yml, с ограничением попыток; (2) нездоровый PR (красный
обязательный чек или ai:changes-requested) дольше порога — задача
возвращается в пул, PR не закрывается; (3) после слияния — gh pr
update-branch для остальных открытых PR.

Кормятся прод-формой: payload'ы ниже — реальные формы ответов GitHub API
(timeline labeled-события, labels-массив с id/description/color, check-runs,
issues/comments), снятые живым запросом `gh api` по этому репозиторию
2026-09-02 (review:ok/ai:failed/ai:changes-requested — метки, которые сейчас
реально стоят на открытых PR). Проводка — на моке gh, сеть не нужна.

Запуск: python -m pytest scripts/orchestra/test_scheduler.py -q
"""

import http.server
import importlib.util
import io
import json
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
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
        # Гвардия класса #225: воспроизводим фильтр Cloudflare по подписи
        # клиента, а не наш пересказ — библиотечный User-Agent (то, что
        # молча подставляет urllib.request без явного addheaders) режется
        # 403' им ДО того, как запрос доходит до логики приложения ниже.
        user_agent = self.headers.get("User-Agent", "")
        if user_agent.startswith("Python-urllib"):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"error code: 1010")
            return
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


def test_morde_opener_sets_explicit_user_agent_past_cf_filter(login_server, monkeypatch):
    # Гвардия класса #225: без явного addheaders в _morde_opener urllib.request
    # шлёт дефолтный `Python-urllib/3.x`, который _LoginHandler режет 403'м
    # (тем же кодом, что живая Cloudflare перед мордой) — этот тест красный,
    # если кто-то уберёт addheaders или сотрёт MORDE_USER_AGENT.
    _LoginHandler.status = 303
    port = login_server.server_address[1]
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    opener = sch._morde_opener()
    assert ("User-Agent", sch.MORDE_USER_AGENT) in opener.addheaders
    assert not sch.MORDE_USER_AGENT.startswith("Python-urllib")
    sch._morde_login(opener)  # не должен бросить: наш UA проходит CF-фильтр


def test_morde_opener_without_explicit_user_agent_is_blocked_by_cf_filter(login_server):
    # Контрольный эксперимент наоборот: голый opener БЕЗ addheaders (то есть
    # без фикса #225) получает дефолтный Python-urllib UA и режется тем же
    # хендлером — доказывает, что фикс не косметика, а необходимое условие.
    _LoginHandler.status = 303
    port = login_server.server_address[1]
    bare_opener = urllib.request.build_opener(
        sch._NoRedirect, urllib.request.HTTPCookieProcessor())
    data = urllib.parse.urlencode({"accessKey": "key"}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auth/login", data=data, method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        bare_opener.open(req, timeout=5)
    assert excinfo.value.code == 403


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


# ── append_session_notes (#480): заметки-итоги в сессии harness-<N> ──────────────


def test_append_session_notes_empty_list_makes_zero_network_calls(monkeypatch):
    # Гвардия холостого хода: без заметок функция не логинится в морду вовсе —
    # ДАЖЕ когда конфиг ЕСТЬ (иначе тест не отличим от соседнего "нет конфига").
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")

    def boom(*a, **k):
        raise AssertionError("не должно вызываться на пустом списке заметок")

    monkeypatch.setattr(sch, "_morde_opener", boom)
    lines, hard = sch.append_session_notes([])
    assert lines == []
    assert hard is False


def test_append_session_notes_no_config_is_not_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "")
    lines, hard = sch.append_session_notes([(5, "заметка")])
    assert lines == []
    assert hard is False


def test_append_session_notes_login_failure_is_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")

    def broken_login(opener):
        raise RuntimeError("логин в морду не удался: HTTP 403")

    monkeypatch.setattr(sch, "_morde_login", broken_login)
    lines, hard = sch.append_session_notes([(5, "заметка")])
    assert hard is True
    assert any("сломана" in line for line in lines)


def test_append_session_notes_session_not_found_is_not_hard_failure(monkeypatch):
    # Задача без раннера (ручной PR) — норма, не ошибка: строка не появляется,
    # прогон не красится.
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sch, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sch, "_morde_ingest",
        lambda opener, session_id, events: (_ for _ in ()).throw(RuntimeError("HTTP 404: Session not found.")),
    )
    lines, hard = sch.append_session_notes([(5, "заметка")])
    assert lines == []
    assert hard is False


def test_append_session_notes_ingest_failure_is_hard_failure(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sch, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sch, "_morde_ingest",
        lambda opener, session_id, events: (_ for _ in ()).throw(RuntimeError("HTTP 400: unknown event type")),
    )
    lines, hard = sch.append_session_notes([(5, "заметка")])
    assert hard is True
    assert any("сломана" in line for line in lines)


def test_append_session_notes_one_login_for_several_notes(monkeypatch):
    # Мутация-гвардия: если кто-то перенесёт логин внутрь цикла по заметкам,
    # этот тест покраснеет — login_calls вырастет с 1 до len(notes).
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    login_calls = []
    ingested = []
    monkeypatch.setattr(sch, "_morde_login", lambda opener: login_calls.append(1))
    monkeypatch.setattr(
        sch, "_morde_ingest",
        lambda opener, session_id, events: ingested.append((session_id, events)) or {"appended": 1, "lastSeq": 1},
    )
    lines, hard = sch.append_session_notes([(5, "первая"), (7, "вторая")])
    assert hard is False
    assert lines == []
    assert len(login_calls) == 1
    assert [sid for sid, _ in ingested] == ["harness-5", "harness-7"]


def test_append_session_notes_event_shape_is_allowlisted_assistant_message(monkeypatch):
    # Мутация-гвардия формы события: ingest (патч 0004) принимает только
    # allowlist-типы с валидными turn/step/message.content — форма ниже снята
    # с dsh-edge/ingest-integration/check.mjs (реальный прод-контракт, не
    # наш пересказ).
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sch, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sch, "_morde_login", lambda opener: None)
    captured = {}

    def fake_ingest(opener, session_id, events):
        captured["session_id"] = session_id
        captured["events"] = events
        return {"appended": len(events), "lastSeq": len(events)}

    monkeypatch.setattr(sch, "_morde_ingest", fake_ingest)
    sch.append_session_notes([(480, "🔀 PR #1 слит в main.")])
    assert captured["session_id"] == "harness-480"
    assert len(captured["events"]) == 1
    event = captured["events"][0]
    assert event["type"] == "assistant/message"
    assert isinstance(event["data"]["turn"], int) and event["data"]["turn"] >= 0
    assert isinstance(event["data"]["step"], int) and event["data"]["step"] >= 0
    message = event["data"]["message"]
    assert message["role"] == "assistant"
    assert isinstance(message["content"], list) and message["content"]
    assert message["content"][0]["type"] == "text"
    assert "PR #1 слит в main" in message["content"][0]["text"]


def test_morde_ingest_posts_raw_events_body_and_surfaces_http_error(monkeypatch):
    # _morde_ingest — не RPC-конверт (_morde_rpc): тело — сырой {"events":[...]},
    # без client-request-обёртки; HTTPError долетает как RuntimeError с кодом.
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"appended": 1, "lastSeq": 1}'

    class _FakeOpener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return _FakeResp()

    result = sch._morde_ingest(_FakeOpener(), "harness-5", [{"type": "assistant/message", "data": {}}])
    assert captured["url"] == "http://morde.invalid/api/sessions/harness-5/ingest"
    assert captured["body"] == {"events": [{"type": "assistant/message", "data": {}}]}
    assert "type" not in captured["body"]  # никакого client-request-конверта
    assert result == {"appended": 1, "lastSeq": 1}


def test_morde_ingest_http_error_is_loud_runtime_error(monkeypatch):
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "http://morde.invalid")

    class _FakeOpener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", None,
                io.BytesIO(b'{"error":"unknown event type"}'))

    with pytest.raises(RuntimeError, match="HTTP 400"):
        sch._morde_ingest(_FakeOpener(), "harness-5", [{"type": "hacker/event", "data": {}}])


# ── after_merge/accept_merged_tasks/unhealthy_pulls: проводка заметок (#480) ─────


def test_after_merge_appends_session_note_for_each_declared_task(monkeypatch):
    merged = pull(9, ref="agent/91-x", pr_body="#91")
    captured = {}

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/9/files?per_page=100&page=1",
                      "repos/o/r/pulls/9/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/91":
            return {**issue(91, assignees=("mytab0r",), title="т"), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (captured.setdefault("notes", notes) and []) or ([], False),
    )
    sch.after_merge("o/r", merged, [])
    assert captured["notes"] == [(91, "🔀 PR #9 слит в main.")]


def test_after_merge_appends_session_note_before_archiving(monkeypatch):
    # Находка ревью PR #489: заметка обязана дописываться ДО архивации сессии
    # (design.md, "after_merge дописывает заметку ДО архивации в рамках одного
    # вызова") — обратный порядок льёт заметку в уже заархивированную сессию,
    # комбинацию, которую design.md сам называет непроверенной живьём.
    merged = pull(9, ref="agent/91-x", pr_body="#91")
    order = []

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/9/files?per_page=100&page=1",
                      "repos/o/r/pulls/9/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/91":
            return {**issue(91, assignees=("mytab0r",), title="т"), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (order.append("note") and []) or ([], False),
    )
    monkeypatch.setattr(
        sch, "archive_runner_sessions",
        lambda numbers: (order.append("archive") and []) or ([], False),
    )
    sch.after_merge("o/r", merged, [])
    assert order == ["note", "archive"]


def test_after_merge_skips_session_note_when_no_task_numbers(monkeypatch):
    # Гвардия холостого хода: PR без задач пула (упоминание чужой/несуществующей
    # задачи в прозе) не должен даже пытаться дописать заметку — append_session_notes
    # сама тоже не сходит в сеть на пустом списке (см. её отдельный тест), но
    # проводка after_merge обязана не звать её вовсе, если task_numbers пуст.
    merged = pull(9, pr_body="упоминание #999 в прозе, не декларация")

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/9/files?per_page=100&page=1",
                      "repos/o/r/pulls/9/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/999":
            return {"pull_request": {}, "state": "open", "labels": []}  # это PR, не задача пула
        raise AssertionError(f"нет маршрута для: {joined}")

    def boom(notes):
        raise AssertionError("append_session_notes не должен вызываться без task_numbers")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", boom)  # тоже не должен звать — task_numbers пуст
    monkeypatch.setattr(sch, "append_session_notes", boom)
    sch.after_merge("o/r", merged, [])  # не должен упасть


def test_accept_merged_tasks_ok_close_appends_session_note(monkeypatch):
    task = issue(320, title="т")
    merged_pr = pull(9, pr_body="#320")
    merged_pr["merged_at"] = "2026-09-01T00:00:00Z"
    merged_pr["head"] = {"sha": "deadbeef"}
    captured = {}
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (captured.setdefault("notes", notes) and []) or ([], False),
    )
    monkeypatch.setattr(sch, "classify_acceptance", lambda filenames: sch.ACCEPT_SCRIPT)
    monkeypatch.setattr(sch, "script_evidence", lambda repo, sha: ("ok", "тесты зелёные"))
    patch_gh(monkeypatch, FakeGh({
        "repos/o/r/pulls/9/files": [],
        "issues/320/comments": [],
        "-X PATCH repos/o/r/issues/320": {},
        "task-320": "🔓 замок task-320 снят",
    }))
    observations, actions, hard = sch.accept_merged_tasks(
        "o/r", [task], {320: merged_pr}, utc(2026, 9, 2, 0, 0), open_pulls_list=[])
    assert hard is False
    assert captured["notes"] == [(320, "✅ Задача #320 закрыта приёмкой (script): тесты зелёные.")]


def test_accept_merged_tasks_fail_appends_session_note(monkeypatch):
    task = issue(321, title="т")
    merged_pr = pull(9, pr_body="#321")
    merged_pr["merged_at"] = "2026-09-01T00:00:00Z"
    merged_pr["head"] = {"sha": "deadbeef"}
    captured = {}
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (captured.setdefault("notes", notes) and []) or ([], False),
    )
    monkeypatch.setattr(sch, "classify_acceptance", lambda filenames: sch.ACCEPT_SCRIPT)
    monkeypatch.setattr(sch, "script_evidence", lambda repo, sha: ("fail", "тест провален"))
    patch_gh(monkeypatch, FakeGh({
        "repos/o/r/pulls/9/files": [],
        "issues/321/comments": [],
        "-X DELETE repos/o/r/issues/321/assignees": {},
        "task-321": "🔓 замок task-321 снят",
    }))
    observations, actions, hard = sch.accept_merged_tasks(
        "o/r", [task], {321: merged_pr}, utc(2026, 9, 2, 0, 0), open_pulls_list=[])
    assert hard is False
    assert captured["notes"] == [(321, "♻️ Задача #321 не закрыта приёмкой (script) — тест провален.")]


def test_unhealthy_pulls_appends_session_note(monkeypatch):
    task = issue(50)
    p = pull(9, labels=(sch.review_labels.AI_CHANGES,), updated_at="2026-09-02T00:00:00Z", pr_body="#50")
    captured = {}
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (captured.setdefault("notes", notes) and []) or ([], False),
    )
    patch_gh(monkeypatch, FakeGh({
        "-X DELETE repos/o/r/issues/50/assignees": {},
        "-X POST repos/o/r/issues/50/comments": {},
        "task-50": "🔓 замок task-50 снят",
    }))
    lines = sch.unhealthy_pulls("o/r", utc(2026, 9, 2, 12, 0), [p], pool=[task])
    assert any("возвращена в пул" in line for line in lines)
    assert captured["notes"] == [(50, f"♻️ Задача #50 возвращена в пул: PR #9 нездоров (метка {sch.review_labels.AI_CHANGES}).")]


def test_unhealthy_pulls_calls_append_session_notes_with_empty_list_when_queue_empty(monkeypatch):
    # Пустая очередь — session_notes тоже пуст; append_session_notes сама не
    # ходит в сеть на пустом списке (см. её отдельный тест), проверяем здесь
    # только то, что unhealthy_pulls не собрала ложных заметок.
    captured = {}
    monkeypatch.setattr(
        sch, "append_session_notes",
        lambda notes: (captured.setdefault("notes", notes) and []) or ([], False),
    )
    lines = sch.unhealthy_pulls("o/r", utc(2026, 9, 2, 12, 0), [], pool=[])
    assert lines == []
    assert captured["notes"] == []


# ── main(): жёсткий сбой красит прогон ПОСЛЕ мержа, эскалирует одним каналом ─────


def test_main_exits_nonzero_and_escalates_on_archive_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    # дрейф пина (#134) здесь не предмет теста — гасим, как остальные механизмы
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "unhealthy_pulls", lambda repo, now, pulls, *, pool=None: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], ["✅ PR #1 слит"], True, pulls))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "wip_gate", lambda repo, now, pulls, pool, dispatch_allowed: ([], [], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool, *, wip_allowed, pulls: ([], []))
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    # Детектор простоя (#201) — отдельная забота, не эта гвардия; здесь важен
    # только путь «жёсткий сбой архивации красит прогон», не его проводка.
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append((repo, issue, text)) or "ок")
    code = sch.main()
    assert code == 1  # прогон окрашен красным — мерж уже состоялся, но поломка не молчит
    assert escalated and escalated[0][0] == "o/r" and escalated[0][1] == sch.WATCHDOG_ISSUE


def test_main_exits_nonzero_and_escalates_on_stall_hard_failure(monkeypatch):
    """Находка ревью PR #248: до фикса RuntimeError из detect_and_act/
    escalate_stale_auto_tasks (сеть, gh без авторизации) выходил из main()
    НЕПОЙМАННЫМ — отчёт пульса (уже посчитанные строки: мержи, heartbeat)
    терялся целиком, summary(lines) не вызывался вовсе. Гвардия: сбой
    детектора красит прогон, но summary всё равно получает строки отчёта,
    включая уже сделанное слияние — тот же приём, что у archive_hard_failure."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "unhealthy_pulls", lambda repo, now, pulls, *, pool=None: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], ["✅ PR #1 слит"], False, pulls))
    monkeypatch.setattr(sch, "trigger_ai_review", lambda repo, now, pulls: ([], []))
    monkeypatch.setattr(sch, "stale_ready_pulls", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch, "reject_reopened_tasks", lambda repo, pool: [])
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(
        sch, "accept_merged_tasks",
        lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "mark_stale_unclaimed", lambda repo, now, pool: [])
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "wip_gate", lambda repo, now, pulls, pool, dispatch_allowed: ([], [], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool, *, wip_allowed, pulls: ([], []))

    def boom(repo, now, lines, run_url=None):
        raise RuntimeError("gh api issues?labels=auto-detected: authentication required")

    monkeypatch.setattr(sch, "detect_and_act", boom)
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: pytest.fail(
        "не должен вызываться — detect_and_act уже упал"))
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append((repo, issue, text)) or "ок")
    saved = []
    monkeypatch.setattr(sch, "summary", lambda lines: saved.append(list(lines)))

    code = sch.main()

    assert code == 1  # детектор сломан — прогон окрашен красным
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert saved, "summary(lines) обязан быть вызван — отчёт не теряется на сбое детектора"
    assert any("✅ PR #1 слит" in line for line in saved[0]), (
        "отчёт о слиянии, посчитанном ДО сбоя детектора, потерян"
    )


def test_main_stays_green_when_archive_ok(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    # дрейф пина (#134) здесь не предмет теста — гасим, как остальные механизмы
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], ["✅ PR #1 слит"], False, pulls))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "wip_gate", lambda repo, now, pulls, pool, dispatch_allowed: ([], [], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool, *, wip_allowed, pulls: ([], []))
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    # Детектор простоя (#201) — отдельная забота, не эта гвардия (см. соседний тест).
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("не должен эскалировать — сбоя не было"))
    assert sch.main() == 0


def test_main_exits_nonzero_when_acceptance_hard_failure(monkeypatch):
    """#227: жёсткий сбой приёмки (не архива сессий) тоже красит прогон — своя
    ветка, независимая от archive_hard_failure."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], [], False, pulls))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(
        sch, "accept_merged_tasks",
        lambda repo, pool, merged, now=None, open_pulls_list=None: ([], ["🚨 #227: улика не проверена"], True))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "wip_gate", lambda repo, now, pulls, pool, dispatch_allowed: ([], [], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool, *, wip_allowed, pulls: ([], []))
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    # Детектор простоя (#201) — отдельная забота, не эта гвардия (см. соседний тест).
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    monkeypatch.setattr(sch, "escalate", lambda *a: "ок")
    assert sch.main() == 1


# pulse_guard/stall_detector живут как отдельные модули (sys.modules[…], те
# же файлы, что импортирует scheduler.py через `from … import …`). Функции,
# определённые В НИХ (issue_marker_times/post_issue_comment/conveyor_gate/
# heartbeat_check/detect_and_act), резолвят `gh` через __globals__ СВОЕГО
# модуля — патчить нужно все три модуля разом (см. patch_gh ниже), иначе
# часть вызовов уходит в настоящий `gh api` подпроцесс (класс #201: то же
# самое разбиралось для scripts/orchestra/test_stall_detector.py).
pg = sys.modules["pulse_guard"]
sd = sys.modules["stall_detector"]


def patch_gh(monkeypatch, fake):
    """Единая точка патча: scheduler.gh (прямые вызовы scheduler.py),
    pulse_guard.gh (issue_marker_times/post_issue_comment/conveyor_gate/
    heartbeat_check, которые scheduler лишь реэкспортирует по имени) и
    stall_detector.gh (detect_and_act/escalate_stale_auto_tasks, #201)."""
    monkeypatch.setattr(sch, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)
    monkeypatch.setattr(sd, "gh", fake)


def patch_post_issue_comment(monkeypatch, fn):
    monkeypatch.setattr(sch, "post_issue_comment", fn)
    monkeypatch.setattr(pg, "post_issue_comment", fn)
    monkeypatch.setattr(sd, "post_issue_comment", fn)


@pytest.fixture(autouse=True)
def _no_telegram_env(monkeypatch):
    """#170: after_merge шлёт Telegram «слито в main». Юнит-тесты не обязаны
    зависеть от окружения раннера: без токена send_telegram честно молчит
    (warning + False), а с заданным в среде токеном отправлял бы НАСТОЯЩИЕ
    сообщения из тестов. Тесты самого сообщения патчат sch.send_telegram явно
    (см. test_after_merge_notifies_telegram_about_merge_once)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def label(name):
    """Прод-форма элемента labels[] (GitHub API, снято с открытых PR репозитория)."""
    return {"id": "LA_kwDOUHBaqc8AAAACypPLSQ", "name": name, "description": "", "color": "0E8A16"}


def pull(number, *, labels=(), draft=False, updated_at="2026-09-02T12:00:00Z", pr_body="",
         ref=None, base_sha=None, author_login="mytab0r", created_at="2026-09-01T00:00:00Z"):
    head = {"sha": f"sha{number}"}
    if ref is not None:
        head["ref"] = ref
    result = {
        "number": number,
        "draft": draft,
        "labels": [label(n) for n in labels],
        "updated_at": updated_at,
        # created_at — прод-форма (Pulls API отдаёт его всегда); используется
        # dispatch_conflict_rework как честный фолбэк возраста конфликта,
        # когда conflict_labeled_at не нашёл момент простановки метки (#588).
        "created_at": created_at,
        "body": pr_body,
        "head": head,
        "user": {"login": author_login},
    }
    if base_sha is not None:
        result["base"] = {"sha": base_sha}
    return result


def issue(number, *, assignees=("someone",), labels=("task",), title="", sub_issues_summary=None,
          state_reason=None, created_at="2026-09-01T00:00:00Z", state="open"):
    return {
        "number": number,
        "assignees": [{"login": a} for a in assignees],
        "labels": [{"name": n} for n in labels],
        "title": title,
        "sub_issues_summary": sub_issues_summary or {"total": 0, "completed": 0},
        # Прод-форма (#369): Issues API отдаёт state_reason уже в list-ответе
        # (open_task_issues); "reopened" — реальное значение для issue, чьё
        # текущее открытое состояние достигнуто событием reopened (замер
        # живых #111/#114/#115/#158, 2026-09-06). None — обычная (не
        # переоткрытая) задача, дефолт большинства существующих тестов.
        "state_reason": state_reason,
        # created_at — прод-форма (Issues API отдаёт его всегда); используется
        # mark_stale_unclaimed (#427) как проксирующий возраст задачи без
        # исполнителя.
        "created_at": created_at,
        # state — прод-форма (Issues API отдаёт его всегда); дефолт "open"
        # годится большинству существующих тестов (open_task_issues и так
        # фильтрует state=open на запросе), "closed" нужен там, где функция
        # сама проверяет состояние конкретной issue (#543, replace_closed_task_prs).
        "state": state,
    }


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов пишется —
    для гвардии холостого хода это и есть доказательство "ни одного вызова"."""

    # Тормоз гонки update_branch/ai-review (#488, review_labels.
    # other_active_ai_review_runs) читает этот эндпоинт ПЕРЕД КАЖДЫМ
    # update_branch, включая все существовавшие ДО этой правки тесты
    # update-branch/merge_queue/update_remaining_pulls, которым сама гонка не
    # предмет. Без запасного маршрута каждый такой тест был бы обязан
    # завести собственную строку "нет активных прогонов" — тот же шум, что
    # уже отвергнут для остальной инфраструктуры моков (routes уже позволяет
    # тесту переопределить конкретный fragment явно, если гонка — его предмет,
    # см. test_update_branch_skips_when_ai_review_running_for_pr ниже).
    _DEFAULT_ROUTES = {"actions/workflows/ai-review.yml/runs": {"workflow_runs": []}}

    def __init__(self, routes: dict):
        self.routes = {**self._DEFAULT_ROUTES, **routes}
        self.calls: list[str] = []

    def __call__(self, *args):
        joined = " ".join(args)
        self.calls.append(joined)
        for fragment, result in self.routes.items():
            if fragment in joined:
                if isinstance(result, Exception):
                    raise result
                return result
        raise AssertionError(f"нет маршрута для: {joined}")

    def mutating_calls(self):
        """Вызовы, меняющие состояние (POST/PUT/DELETE/PATCH) — не GET. PATCH
        добавлен приёмкой (#227): закрытие issue идёт через
        `-X PATCH .../issues/N -f state=closed`."""
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE", "-X PATCH"))]


REPO = "mytab0r/edge-harness"


@pytest.fixture(autouse=True)
def _reset_update_branch_budget():
    """Слот update_branch (#252, третий заход) — module-level и общий на
    прогон планировщика; без явного сброса перед каждым тестом состояние
    "слот уже занят" утекало бы из одного теста в следующий в том же
    процессе pytest."""
    sch.reset_update_branch_budget()
    yield
    sch.reset_update_branch_budget()


# ── reap_stale (#61): просрочённое назначение без PR возвращается в пул ──────────


def test_reap_stale_skips_blocked_labeled_issue(monkeypatch):
    # Тот же класс, что unhealthy_pulls (находка AI-ревью PR #247, 2026-09-03):
    # эскалация playbook (метка blocked) держит назначение намеренно — reap_stale
    # не имеет права снять его по истечении STALE_HOURS, иначе задача вернётся
    # в пул и снова достанется воркеру без того, что есть только у владельца.
    task = issue(260, labels=["task", "blocked"])
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.reap_stale(REPO, utc(2026, 9, 2, 12, 0), [], pool=[task])
    assert lines == []
    assert fake.calls == []  # blocked — пропуск ДО любого HTTP, снимок дан параметром (#443)


def test_pr_references_issue_true_on_branch_without_body_number():
    # Находка AI-ревью PR #398: после #394 тело PR не обязано называть номер
    # вовсе (шаблон говорит, что «#N» — для человека, не источник истины) —
    # чистого references_task(body) стало недостаточно, PR с пустым/чужим
    # телом, но веткой agent/256-… должен быть виден как ссылающийся на #256.
    p = pull(500, ref="agent/256-fix-thing", pr_body="Просто описание, без номера.")
    assert sch.pr_references_issue(p, 256) is True


def test_pr_references_issue_false_when_neither_branch_nor_body_match():
    p = pull(501, ref="agent/999-unrelated", pr_body="Тоже без номера.")
    assert sch.pr_references_issue(p, 256) is False


def test_reap_stale_skips_task_covered_by_pr_branch_without_body_number(monkeypatch):
    # Тот же класс: reap_stale раньше сканировал только тело — контракт-
    # проходящий PR с телом без номера (ветка agent/256-…) был бы невидим,
    # и задача-«просрочена» снялась бы при живом PR.
    old_assigned = [{"event": "assigned", "created_at": "2026-08-01T00:00:00Z"}]
    task = issue(256, assignees=("mytab0r",))
    fake = FakeGh({
        "issues/256/timeline?per_page=100": old_assigned,
    })
    patch_gh(monkeypatch, fake)
    p = pull(500, ref="agent/256-fix-thing", pr_body="Просто описание, без номера.")
    now = datetime.now(timezone.utc)

    lines = sch.reap_stale(REPO, now, [p], pool=[task])

    assert lines == []
    assert fake.mutating_calls() == []


# ── mark_stale_unclaimed (#427): видимость непринятых задач ─────────────────────


def test_mark_stale_unclaimed_labels_old_unassigned_issue(monkeypatch):
    task = issue(300, assignees=(), labels=["task"], created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({"issues/300/labels": None})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 2, 1, 0)  # 25ч > STALE_HOURS (24)
    lines = sch.mark_stale_unclaimed(REPO, now, [task])
    assert any("stale-unclaimed" in line and "300" in line for line in lines)
    posts = [c for c in fake.calls if c.startswith("-X POST") and "300/labels" in c]
    assert len(posts) == 1
    assert f"labels[]={sch.STALE_UNCLAIMED_LABEL}" in posts[0]


def test_mark_stale_unclaimed_skips_recent_unassigned_issue(monkeypatch):
    task = issue(301, assignees=(), labels=["task"], created_at="2026-09-02T00:30:00Z")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 2, 1, 0)  # 30 мин < STALE_HOURS
    lines = sch.mark_stale_unclaimed(REPO, now, [task])
    assert lines == []
    assert fake.calls == []  # холостой ход не делает НИ ОДНОГО вызова gh


def test_mark_stale_unclaimed_skips_blocked_issue(monkeypatch):
    # blocked уже сигнализирует владельцу отдельно (docs/agents/LABELS.md) —
    # дублировать сигнал второй меткой не нужно.
    task = issue(302, assignees=(), labels=["task", "blocked"], created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 2, 1, 0)
    lines = sch.mark_stale_unclaimed(REPO, now, [task])
    assert lines == []
    assert fake.calls == []


def test_mark_stale_unclaimed_idempotent_when_already_labeled(monkeypatch):
    task = issue(303, assignees=(), labels=["task", "stale-unclaimed"], created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 2, 1, 0)
    lines = sch.mark_stale_unclaimed(REPO, now, [task])
    assert lines == []
    assert fake.calls == []  # уже помечена — второй POST не идёт


def test_mark_stale_unclaimed_removes_label_when_claimed(monkeypatch):
    # Газ: задачу взяли (появился исполнитель) — метка обязана сняться сама,
    # без ручного вмешательства (это НЕ тормоз).
    task = issue(304, assignees=("someone",), labels=["task", "stale-unclaimed"],
                 created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({"issues/304/labels/stale-unclaimed": None})
    patch_gh(monkeypatch, fake)
    now = utc(2026, 9, 2, 1, 0)
    lines = sch.mark_stale_unclaimed(REPO, now, [task])
    assert any("304" in line and "снята" in line for line in lines)
    deletes = [c for c in fake.calls if c.startswith("-X DELETE") and "304/labels/stale-unclaimed" in c]
    assert len(deletes) == 1


def test_mark_stale_unclaimed_no_second_traversal_uses_passed_pool(monkeypatch):
    # Цена лишнего обхода (задача #427): mark_stale_unclaimed обязана работать
    # ТОЛЬКО с переданным pool — гвардия против регрессии «завёл свой
    # open_task_issues внутри функции».
    task = issue(305, assignees=(), labels=["task"], created_at="2026-09-01T00:00:00Z")
    fake = FakeGh({"issues/305/labels": None})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(
        sch, "open_task_issues",
        lambda repo: (_ for _ in ()).throw(AssertionError("не должен вызываться — pool уже передан")))
    now = utc(2026, 9, 2, 1, 0)
    sch.mark_stale_unclaimed(REPO, now, [task])
    assert not any("issues?state=open" in c for c in fake.calls)


# ── Поведение 1: готовый PR без вердикта — дёрнуть гейт самому ───────────────────


def gate1_status(when: str):
    """Прод-форма commit status `harness/review` на текущем head PR (#345) —
    якорь таймера #196 после находки ревью #424 (замена таймлайн-события
    'labeled', замороженного идемпотентностью #203: пуш с тем же вердиктом
    не выбрасывает 'labeled', но статус публикуется безусловно)."""
    return [{"context": sch.review_labels.STATUS_REVIEW, "created_at": when}]


def test_trigger_ai_review_dispatches_after_threshold_no_verdict(monkeypatch):
    p = pull(163, labels=["review:ok"])
    fake = FakeGh({
        "commits/sha163/statuses": gate1_status("2026-09-02T11:00:00Z"),
        "issues/163/comments": [],  # прод-форма: голый массив без маркеров попыток
        "ai-review.yml/dispatches": None,  # 204 без тела — прод-форма ответа dispatch
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    now = utc(2026, 9, 2, 11, 45)  # 45 мин > порог 30
    observations, actions = sch.trigger_ai_review(REPO, now, [p])

    dispatch_calls = [c for c in fake.calls if "ai-review.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[pr]=163" in dispatch_calls[0]
    assert any("163" in line and "ai-review.yml запущен" in line for line in (observations + actions))
    assert posted and posted[0][0] == 163
    assert sch.AI_REVIEW_RETRY_MARKER in posted[0][1]


def test_trigger_ai_review_dispatches_on_ai_failed(monkeypatch):
    p = pull(178, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha178/statuses": gate1_status("2026-09-01T22:37:09Z"),
        "issues/178/comments": [],
        "ai-review.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    now = utc(2026, 9, 1, 23, 30)  # больше порога с момента labeled review:ok
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert any("ai-review.yml/dispatches" in c for c in fake.calls)
    assert any("178" in line for line in (observations + actions))


def ai_failed_comment(reset_hint: str, verdict: str = "error"):
    """Прод-форма комментария-вердикта AI-ревью (ai_review.build_comment,
    #727) от доверенной учётки — шапка несёт `reviewer:`/`reset-at:`,
    остальное — proза, latest_ai_comment её не читает."""
    return {
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "body": f"pr: 1\nhead: sha1\nreviewer: {verdict}\nreset-at: {reset_hint}\n\nтело вердикта",
    }


def test_trigger_ai_review_holds_back_when_chain_exhausted_reset_in_future(monkeypatch):
    # #727: ai:failed из-за all_providers_exhausted, дата сброса ещё не
    # наступила — авто-повтор (#196) НЕ дёргает ai-review.yml вслепую в ту же
    # квоту, эскалирует владельцу вместо этого (маркер идемпотентен на эпизод).
    p = pull(700, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha700/statuses": gate1_status("2026-09-08T00:00:00Z"),
        "issues/700/comments": [ai_failed_comment("GLM: 2026-09-10 08:51:55")],
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    now = utc(2026, 9, 8, 2, 0)  # порог (30 мин) прошёл, но сброс — 2026-09-10
    observations, actions = sch.trigger_ai_review(REPO, now, [p])

    assert not any("dispatches" in c for c in fake.calls)  # квота не жжётся дальше
    # escalate() пишет в #120 (watchdog) ПЕРВЫМ, наш же след в самом PR —
    # второй пост тем же вызовом; оба используют один patched post_issue_comment.
    pr_posts = [text for n, text in posted if n == 700]
    assert pr_posts and "2026-09-10" in pr_posts[0]
    assert any(n == sch.WATCHDOG_ISSUE for n, _ in posted)
    assert any("700" in line and "эскалация" in line for line in (observations + actions))


def test_trigger_ai_review_holds_back_idempotent_second_pulse(monkeypatch):
    # Второй пульс в том же окне ожидания — маркер уже стоит, повторного
    # Telegram/комментария нет (тот же принцип, что у остальных эскалаций —
    # идемпотентно на эпизод, не на каждый 15-минутный пульс).
    p = pull(701, labels=["review:ok", "ai:failed"])
    marker = f"{sch.AI_REVIEW_CHAIN_COOLDOWN_MARKER} #701"
    fake = FakeGh({
        "commits/sha701/statuses": gate1_status("2026-09-08T00:00:00Z"),
        "issues/701/comments": [
            ai_failed_comment("GLM: 2026-09-10 08:51:55"),
            {"created_at": "2026-09-08T01:00:00Z", "body": f"🤖 {marker}\nуже эскалировано"},
        ],
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("повторная эскалация в том же окне — не должно быть"))

    now = utc(2026, 9, 8, 2, 0)
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert not any("dispatches" in c for c in fake.calls)
    assert any("701" in line and "придержан" in line for line in observations)


def test_trigger_ai_review_dispatches_once_reset_date_passed(monkeypatch):
    # Мутация обратного случая: дата сброса УЖЕ в прошлом — цепочка снова
    # жива, авто-повтор идёт как обычно (доказывает, что гейт не держит
    # ретрай вечно, только до даты).
    p = pull(702, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "commits/sha702/statuses": gate1_status("2026-09-08T00:00:00Z"),
        "issues/702/comments": [ai_failed_comment("GLM: 2026-09-01 00:00:00")],
        "ai-review.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    now = utc(2026, 9, 8, 2, 0)
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert any("ai-review.yml/dispatches" in c for c in fake.calls)


def test_parse_reset_hint_dates_prod_form():
    # Прод-форма #727: реальный текст отказа из run 34176910458 — «Your
    # limit will reset at 2026-09-10 08:51:55» (пространственная форма, без
    # зоны) и ISO-форма смоук-фикстуры dsh-clients.smoke.sh — обе разбираются
    # одной функцией.
    dates = sch.parse_reset_hint_dates("GLM: 2026-09-10 08:51:55; NVIDIA: 2026-09-11T00:00:00Z")
    assert len(dates) == 2
    assert min(dates) == utc(2026, 9, 10, 8, 51, 55)


def test_parse_reset_hint_dates_empty_or_garbage_is_no_dates():
    assert sch.parse_reset_hint_dates("") == []
    assert sch.parse_reset_hint_dates("не дата вовсе") == []


def test_trigger_ai_review_silent_before_threshold(monkeypatch):
    p = pull(163, labels=["review:ok"])
    fake = FakeGh({"commits/sha163/statuses": gate1_status("2026-09-02T11:40:00Z")})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано — не должен писать"))

    now = utc(2026, 9, 2, 11, 50)  # 10 мин < порог 30
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert (observations + actions) == []
    assert not any("dispatches" in c for c in fake.calls)


def test_trigger_ai_review_silent_when_verdict_already_ok(monkeypatch):
    p = pull(181, labels=["review:ok", "ai:ok"])
    fake = FakeGh({})  # ни один маршрут не должен понадобиться
    patch_gh(monkeypatch, fake)
    observations, actions = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert (observations + actions) == []
    assert fake.calls == []  # даже таймлайн не читаем — решение принято по меткам


def test_trigger_ai_review_silent_when_changes_requested(monkeypatch):
    # ai:changes-requested — вердикт ЕСТЬ, это не "нет вердикта": ждём человека/
    # доработку, не повторяем ai-review сами (это забота unhealthy_pulls).
    p = pull(999, labels=["review:ok", "ai:changes-requested"])
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert (observations + actions) == []
    assert fake.calls == []


def test_trigger_ai_review_stops_after_max_attempts(monkeypatch):
    p = pull(163, labels=["review:ok"])
    # AI_REVIEW_MAX_ATTEMPTS маркеров уже стоит в комментариях — прод-форма
    # ответа issues/{n}/comments (голый массив объектов с created_at/body).
    comments = [
        {"created_at": f"2026-09-02T1{i}:00:00Z", "body": f"🤖 {sch.AI_REVIEW_RETRY_MARKER} попытка {i}"}
        for i in range(sch.AI_REVIEW_MAX_ATTEMPTS)
    ]
    fake = FakeGh({
        "commits/sha163/statuses": gate1_status("2026-09-02T09:00:00Z"),
        "issues/163/comments": comments,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("лимит попыток исчерпан — не пишем"))

    now = utc(2026, 9, 2, 12, 0)
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert not any("dispatches" in c for c in fake.calls)  # квота не жжётся дальше
    # #456: «не дёргаю снова» ничего не меняет — наблюдение, не действие.
    assert any("нужен человек" in line for line in observations)
    assert actions == []


def test_trigger_ai_review_dispatches_for_review_large_ai_failed(monkeypatch):
    # Живой случай PR #412 (задача #432): review:large + ai:failed, помечен
    # 2026-09-06T02:34, автоповторов ноль спустя полтора часа при пороге
    # 30 минут. До фикса #432 last_gate1_labeled_at (тогда ещё
    # last_review_ok_labeled_at) искала ТОЛЬКО событие "labeled: review:ok",
    # которого для review:large PR не бывает — anchor оставался None навсегда,
    # и даже пропустив входной гейт, retry не смог бы посчитать возраст. С
    # #424 якорь — commit status `harness/review` (единый для review:ok/
    # review:large/review:changes-requested), различение по метке этому
    # якорю больше не нужно вовсе.
    p = pull(412, labels=["review:large", "ai:failed"])
    fake = FakeGh({
        "commits/sha412/statuses": gate1_status("2026-09-06T02:34:00Z"),
        "issues/412/comments": [],
        "ai-review.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    now = utc(2026, 9, 6, 4, 4)  # 90 мин > порог 30 (тот же разрыв, что в задаче)
    observations, actions = sch.trigger_ai_review(REPO, now, [p])
    assert any("ai-review.yml/dispatches" in c for c in fake.calls)
    assert any("412" in line for line in (observations + actions))


def test_trigger_ai_review_mutation_gate1_narrowed_to_review_ok_misses_review_large(monkeypatch):
    # Мутация (#432): сузить предикат "гейт 1 отработал" обратно до одного
    # review:ok (поведение ДО этого фикса) — PR #412-подобный (review:large +
    # ai:failed) снова становится невидим автоповтору. Доказывает, что именно
    # gate1_decided (не входной гейт по старинке) держит фикс живым: сними
    # его — и этот тест покраснеет первым.
    p = pull(412, labels=["review:large", "ai:failed"])
    fake = FakeGh({})  # решение обязано быть принято ДО первого сетевого вызова
    monkeypatch.setattr(
        sch.review_labels, "gate1_decided",
        lambda labels: sch.review_labels.REVIEW_OK in labels,
    )
    patch_gh(monkeypatch, fake)
    observations, actions = sch.trigger_ai_review(REPO, utc(2026, 9, 6, 4, 4), [p])
    assert (observations + actions) == []
    assert fake.calls == []


# ── Мутация гвардии поведения 1: без гейта на review_labels.gate1_decided ────
# ── — дёргает всё подряд ──────────────────────────────────────────────────


def test_trigger_ai_review_mutation_without_gate1_decided_gate_would_fire_on_anything(monkeypatch):
    # Доказательство того, что гейт "review_labels.gate1_decided(labels)" в
    # проде необходим: PR, который гейт 1 вообще ещё не тронул (ни review:ok,
    # ни review:large), не должен рассматриваться (иначе дёрнули бы ai-review
    # на любом свежем PR, включая черновики и PR без ревью). #432 расширил
    # входной гейт до review:ok ИЛИ review:large — это не то же самое, что
    # снять гейт вовсе: PR совсем без метки гейта 1 обязан остаться снаружи.
    p = pull(555, labels=[])  # ни review:ok, ни review:large — гейт 1 молчит
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert (observations + actions) == []
    assert fake.calls == []


# ── Поведение 2: нездоровый PR — вернуть задачу в пул ─────────────────────────────


CHECK_RUNS_RED = {"check_runs": [
    {"name": "test", "conclusion": "failure"},
    {"name": "lint", "conclusion": "success"},
]}
CHECK_RUNS_GREEN = {"check_runs": [{"name": "test", "conclusion": "success"}]}
CHECK_RUNS_EMPTY = {"check_runs": []}


def test_unhealthy_pulls_returns_task_on_red_required_check(monkeypatch):
    task = issue(200)
    p = pull(201, labels=["review:ok"], updated_at="2026-09-02T09:00:00Z", pr_body="#200")
    fake = FakeGh({
        "commits/sha201/check-runs": CHECK_RUNS_RED,
        "issues/200/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    now = utc(2026, 9, 2, 12, 0)  # 180 мин > порог 120
    lines = sch.unhealthy_pulls(REPO, now, [p], pool=[task])

    assert any("assignees" in c and "-X DELETE" in c for c in fake.calls)
    assert any("возвращена в пул" in line and "201" in line for line in lines)
    assert posted and posted[0][0] == 200
    assert "#201" in posted[0][1]
    assert "не переделывай" in posted[0][1]
    assert task["assignees"] == []  # (#443) мутация pool сразу вслед за DELETE


def test_unhealthy_pulls_returns_task_on_ai_changes_requested(monkeypatch):
    task = issue(210)
    p = pull(211, labels=["review:ok", "ai:changes-requested"],
              updated_at="2026-09-02T09:00:00Z", pr_body="#210")
    fake = FakeGh({
        "commits/sha211/check-runs": CHECK_RUNS_GREEN,  # чек зелёный — причина не в нём
        "issues/210/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: "ok")

    now = utc(2026, 9, 2, 12, 0)
    lines = sch.unhealthy_pulls(REPO, now, [p], pool=[task])
    assert any("ai:changes-requested" in line for line in lines)


def test_unhealthy_pulls_detects_task_via_branch_without_body_number(monkeypatch):
    # Тот же класс, что reap_stale выше (находка AI-ревью PR #398): тело PR
    # без номера, но ветка agent/220-… — unhealthy_pulls обязан найти задачу
    # через ветку, а не промолчать «нет ссылающегося PR».
    task = issue(220)
    p = pull(221, labels=["review:ok"], updated_at="2026-09-02T09:00:00Z",
              pr_body="Описание без номера задачи.", ref="agent/220-fix-thing")
    fake = FakeGh({
        "commits/sha221/check-runs": CHECK_RUNS_RED,
        "issues/220/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    now = utc(2026, 9, 2, 12, 0)  # 180 мин > порог 120
    lines = sch.unhealthy_pulls(REPO, now, [p], pool=[task])

    assert any("возвращена в пул" in line and "221" in line for line in lines)


def test_unhealthy_pulls_silent_before_threshold(monkeypatch):
    task = issue(220)
    p = pull(221, labels=["review:ok"], updated_at="2026-09-02T11:30:00Z", pr_body="#220")
    fake = FakeGh({
        "commits/sha221/check-runs": CHECK_RUNS_RED,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано — не пишем"))

    now = utc(2026, 9, 2, 12, 0)  # 30 мин < порог 120
    lines = sch.unhealthy_pulls(REPO, now, [p], pool=[task])
    assert lines == []
    assert not any("-X DELETE" in c for c in fake.calls)


def test_unhealthy_pulls_silent_when_pr_green(monkeypatch):
    task = issue(230)
    p = pull(231, labels=["review:ok"], updated_at="2026-09-02T08:00:00Z", pr_body="#230")
    fake = FakeGh({
        "commits/sha231/check-runs": CHECK_RUNS_GREEN,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("PR здоров — не пишем"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p], pool=[task])
    assert lines == []


def test_unhealthy_pulls_idempotent_after_release_no_assignee(monkeypatch):
    # После освобождения задачи assignees пуст — тот же приём, что у reap_stale:
    # follow-up вызов не действует повторно (не дублирует комментарий/снятие).
    task = issue(240, assignees=())
    p = pull(241, labels=["review:ok"], updated_at="2026-09-02T08:00:00Z", pr_body="#240")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не назначена — не трогаем"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p], pool=[task])
    assert lines == []
    # issue без assignees отфильтрован ДО чтения check-runs/мутирующих вызовов —
    # снимок пула дан параметром (#443), ни одного HTTP-вызова не требуется.
    assert fake.calls == []


def test_unhealthy_pulls_skips_conflict_labeled_pr(monkeypatch):
    # conflict — отдельный класс (mark_conflicts), unhealthy_pulls не дублирует.
    task = issue(250)
    p = pull(251, labels=["review:ok", "conflict"], updated_at="2026-09-02T08:00:00Z", pr_body="#250")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("conflict — не наш класс"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p], pool=[task])
    assert lines == []
    assert not any("check-runs" in c for c in fake.calls)


def test_unhealthy_pulls_skips_blocked_labeled_issue(monkeypatch):
    # Находка AI-ревью PR #247 (2026-09-03): воркер эскалировал (метка blocked,
    # playbook), назначение осталось. Без этого пропуска unhealthy_pulls снял бы
    # исполнителя с нездорового PR по таймеру, oldest_free выбрал бы ту же задачу
    # как старейшую свободную — вечный цикл без газа, PR не в силах владельца
    # починить чинит агент раз за разом.
    task = issue(270, labels=["task", "blocked"])
    p = pull(271, labels=["review:ok", "ai:changes-requested"],
              updated_at="2026-09-02T08:00:00Z", pr_body="#270")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("blocked — газ только у владельца"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p], pool=[task])
    assert lines == []
    assert not any("check-runs" in c for c in fake.calls)


# ── Мутация гвардии поведения 2: без возврата в пул задача осталась бы висеть ────


def test_pr_is_unhealthy_mutation_detects_reason_precisely():
    # Прямая проверка чистой функции: красный чек → причина есть; здоровый PR
    # (или draft/conflict) → None. Снявший любую из трёх ветвей развалит эти
    # ассерты по отдельности — так гвардия ловит мутацию по каждой причине.
    healthy = pull(1, labels=["review:ok"])
    unhealthy_changes = pull(2, labels=["review:ok", "ai:changes-requested"])
    draft = pull(3, labels=["review:ok"], draft=True)
    conflict = pull(4, labels=["review:ok", "conflict"])

    import scheduler as _unused  # noqa: F401  (модуль уже импортирован как sch)

    fake = FakeGh({
        "commits/sha1/check-runs": CHECK_RUNS_GREEN,
        "commits/sha2/check-runs": CHECK_RUNS_GREEN,
    })

    def with_gh(fn):
        return fn

    orig_gh = sch.gh
    sch.gh = fake
    try:
        assert sch.pr_is_unhealthy(REPO, healthy) is None
        assert sch.pr_is_unhealthy(REPO, unhealthy_changes) is not None
        assert sch.pr_is_unhealthy(REPO, draft) is None
        assert sch.pr_is_unhealthy(REPO, conflict) is None
    finally:
        sch.gh = orig_gh


# ── Инвариант #269: готовый PR не должен ждать слияния ───────────────────────────
# Противоположный класс unhealthy_pulls: PR ЗДОРОВ (обе метки-гейта, зелёные
# проверки), но слияния не было дольше UNHEALTHY_PR_AFTER_MINUTES с момента
# готовности (позже из двух commit status'ов harness/review и harness/ai-review
# на текущем head, #345 — якорь после находки ревью #424, замена таймлайн-
# события 'labeled', замороженного идемпотентностью #203).


def statuses_ready(review_at: str, ai_at: str):
    return [
        {"context": sch.review_labels.STATUS_REVIEW, "created_at": review_at},
        {"context": sch.review_labels.STATUS_AI_REVIEW, "created_at": ai_at},
    ]


def test_stale_ready_pulls_escalates_when_ready_longer_than_threshold(monkeypatch):
    p = pull(301, labels=["review:ok", "ai:ok"])
    fake = FakeGh({
        "pulls/301": {"mergeable_state": "clean"},
        "commits/sha301/check-runs": CHECK_RUNS_GREEN,
        "commits/sha301/statuses": statuses_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
        "issues/120/comments?per_page=100": [],
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    now = utc(2026, 9, 2, 10, 30)  # 145 мин с готовности (08:05) > порог 120
    lines = sch.stale_ready_pulls(REPO, now, [p])

    assert any("301" in line and "готов" in line for line in lines)
    assert posted and posted[0][0] == sch.WATCHDOG_ISSUE
    assert sch.READY_STALL_MARKER in posted[0][1]
    assert "301" in posted[0][1]


def test_stale_ready_pulls_silent_before_threshold(monkeypatch):
    p = pull(302, labels=["review:ok", "ai:ok"])
    fake = FakeGh({
        "pulls/302": {"mergeable_state": "clean"},
        "commits/sha302/check-runs": CHECK_RUNS_GREEN,
        "commits/sha302/statuses": statuses_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано — не пишем"))

    now = utc(2026, 9, 2, 9, 0)  # 55 мин < порог 120
    lines = sch.stale_ready_pulls(REPO, now, [p])
    assert lines == []
    assert not any("issues/120/comments" in c for c in fake.calls)  # маркеры лениво


def test_stale_ready_pulls_silent_when_ai_gate_missing(monkeypatch):
    # Только review:ok — вторая метка-гейт не стоит, PR не готов вовсе.
    p = pull(303, labels=["review:ok"])
    fake = FakeGh({"pulls/303": {"mergeable_state": "clean"}, "commits/sha303/check-runs": CHECK_RUNS_GREEN})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("гейта нет — не готов"))
    lines = sch.stale_ready_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []
    assert not any("statuses" in c for c in fake.calls)  # готовность даже не проверяем


def test_stale_ready_pulls_silent_when_checks_red(monkeypatch):
    p = pull(304, labels=["review:ok", "ai:ok"])
    fake = FakeGh({"pulls/304": {"mergeable_state": "clean"}, "commits/sha304/check-runs": CHECK_RUNS_RED})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("красный чек — не готов"))
    lines = sch.stale_ready_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []


def test_stale_ready_pulls_idempotent_after_already_signalled(monkeypatch):
    # Маркер новее момента готовности — уже оповещено, второй раз не пишем.
    p = pull(305, labels=["review:ok", "ai:ok"])
    fake = FakeGh({
        "pulls/305": {"mergeable_state": "clean"},
        "commits/sha305/check-runs": CHECK_RUNS_GREEN,
        "commits/sha305/statuses": statuses_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
        "issues/120/comments?per_page=100": [
            {"created_at": "2026-09-02T08:10:00Z", "body": f"🚨 edge-harness: {sch.READY_STALL_MARKER} #305\nPR #305 …"},
        ],
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("уже оповещено — не дублируем"))
    now = utc(2026, 9, 2, 10, 30)  # тот же возраст, что в escalates-тесте
    lines = sch.stale_ready_pulls(REPO, now, [p])
    assert lines == []


def test_stale_ready_pulls_signals_each_pr_independently(monkeypatch):
    # #303, находка ревью: маркер по PR #301 не имеет права подавить #302 —
    # у каждого просроченного PR маркер свой (номер — часть текста маркера).
    # Мутация-гвардия: если вернуть общий READY_STALL_MARKER без номера PR,
    # маркер по #301 (10:01, новее готовности #302 в 08:30) подавит #302 и
    # второй assert покраснеет.
    p301 = pull(301, labels=["review:ok", "ai:ok"])
    p302 = pull(302, labels=["review:ok", "ai:ok"])
    fake = FakeGh({
        "pulls/301": {"mergeable_state": "clean"},
        "pulls/302": {"mergeable_state": "clean"},
        "commits/sha301/check-runs": CHECK_RUNS_GREEN,
        "commits/sha302/check-runs": CHECK_RUNS_GREEN,
        "commits/sha301/statuses": statuses_ready("2026-09-02T06:00:00Z", "2026-09-02T06:00:00Z"),  # готов 08:00
        "commits/sha302/statuses": statuses_ready("2026-09-02T06:30:00Z", "2026-09-02T06:30:00Z"),  # готов 08:30
        # #301 уже прокричал в 10:01 — маркер несёт свой номер.
        "issues/120/comments?per_page=100": [
            {"created_at": "2026-09-02T10:01:00Z", "body": f"🚨 edge-harness: {sch.READY_STALL_MARKER} #301\nPR #301 …"},
        ],
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    now = utc(2026, 9, 2, 10, 31)  # #301: 151 мин с 08:00; #302: 121 мин с 08:30 — оба > 120
    lines = sch.stale_ready_pulls(REPO, now, [p301, p302])

    assert not any("301" in line for line in lines)  # #301 уже оповещён — молчим
    assert any("302" in line and "готов" in line for line in lines)  # #302 обязан прокричать
    assert len(posted) == 1 and "302" in posted[0][1]


def test_stale_ready_pulls_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({})  # ни один маршрут не должен понадобиться
    patch_gh(monkeypatch, fake)
    lines = sch.stale_ready_pulls(REPO, utc(2026, 9, 2, 12, 0), [])
    assert lines == []
    assert fake.calls == []


# ── Мутация гвардии инварианта #269: без ОБЕИХ меток-гейта готовность ложная ─────


def test_pr_is_merge_ready_mutation_requires_both_gate_labels_and_green_checks():
    only_review = pull(1, labels=["review:ok"])
    both_gates = pull(2, labels=["review:ok", "ai:ok"])
    red_checks = pull(3, labels=["review:ok", "ai:ok"])
    draft = pull(4, labels=["review:ok", "ai:ok"], draft=True)
    for p in (only_review, both_gates, red_checks, draft):
        p["mergeable_state"] = "clean"

    fake = FakeGh({
        "commits/sha1/check-runs": CHECK_RUNS_GREEN,
        "commits/sha2/check-runs": CHECK_RUNS_GREEN,
        "commits/sha3/check-runs": CHECK_RUNS_RED,
        "commits/sha4/check-runs": CHECK_RUNS_GREEN,
    })
    orig_gh = sch.gh
    sch.gh = fake
    try:
        assert sch.pr_is_merge_ready(REPO, only_review) is False
        assert sch.pr_is_merge_ready(REPO, both_gates) is True
        assert sch.pr_is_merge_ready(REPO, red_checks) is False
        assert sch.pr_is_merge_ready(REPO, draft) is False
    finally:
        sch.gh = orig_gh


# НАХОДКА РЕВЬЮ (#303): докстринг pr_is_merge_ready заявляет «тот же критерий
# готовности, что merge_queue», но merge_queue на пустом списке check-run'ов
# явно пропускает PR («проверки ещё не заведены», scheduler.py:434), а
# pr_bad_checks на пустом списке отдаёт [] — «красных нет» — что без отдельной
# проверки сделало бы пустые check-run'ы неотличимыми от зелёных именно здесь.
# Докажи мутацией: убери `if not runs: return False` из pr_is_merge_ready —
# тест ниже покраснеет (готовность станет True на пустом списке).
def test_pr_is_merge_ready_false_on_empty_check_runs_same_as_merge_queue():
    empty_checks = pull(5, labels=["review:ok", "ai:ok"])
    empty_checks["mergeable_state"] = "clean"
    fake = FakeGh({"commits/sha5/check-runs": CHECK_RUNS_EMPTY})
    orig_gh = sch.gh
    sch.gh = fake
    try:
        assert sch.pr_is_merge_ready(REPO, empty_checks) is False
    finally:
        sch.gh = orig_gh


# ── Поведение 3: после слияния — подтянуть остальных, но выборочно (#252) ────────
# Раньше update_remaining_pulls дёргал update-branch для ВСЕХ открытых недрафт
# PR — каждый такой push синхронизирует pr-review.yml и снимает валидные
# ai:*-метки (замер #252: 142 прогона ai-review.yml за 14.5 ч). Предикат
# review_labels.should_update_branch — одно место правды, что подтягивать
# стоит: оба вердикта зелёные (близок к слиянию) или конфликт (подтягивание
# может его расшить).


def test_update_remaining_pulls_pulls_only_one_candidate_per_merge(monkeypatch):
    # Второй заход #252: даже среди прошедших предикат кандидатов подтягиваем
    # РОВНО одного за вызов — подтягивание первого (push, меняет head) само
    # способно сбросить ai:ok второго тем же циклом, который эта задача и
    # закрывает. Порядок и предикат не меняются: #2 и #3 оба проходят
    # should_update_branch, подтянут только первый по порядку — #2.
    others = [
        pull(2, labels=["review:ok", "ai:ok"]),         # оба вердикта — подтянуть первым
        pull(3, labels=["conflict"]),                     # конфликт — тоже кандидат, но не в этом запуске
        pull(4, labels=["review:ok"]),                     # нет ai:ok — не трогать
        pull(5, labels=["review:ok", "ai:changes-requested"]),  # доработка — не трогать
    ]
    fake = FakeGh({
        "pulls/2/update-branch": None,
        "pulls/3/update-branch": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 1
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert not any("pulls/3/update-branch" in c for c in fake.calls)  # слот уже занят #2
    updated_lines = [line for line in (observations + actions) if "обновлён из main" in line]
    not_close_lines = [line for line in (observations + actions) if "не подтянут" in line]
    # Находка AI-ревью PR #288: строка обязана называть ПРИЧИНУ (слот занят
    # другим PR), а не приписывать #3 несостоявшееся обновление — #3 сам не
    # обновлён, слот занял #2.
    slot_taken_lines = [
        line for line in (observations + actions)
        if "слот update_branch" in line and "занят другим PR" in line
    ]
    assert len(updated_lines) == 1 and "#2" in updated_lines[0]
    assert len(not_close_lines) == 3  # #3 (слот занят), #4, #5 — не близки к слиянию
    assert any("#4" in line for line in not_close_lines)
    assert any("#5" in line for line in not_close_lines)
    assert len(slot_taken_lines) == 1 and "#3" in slot_taken_lines[0]  # не молчит, назван следующий прогон
    assert "следующий прогон" in slot_taken_lines[0]
    assert not any("уже обновлён" in line for line in (observations + actions))  # #3 сам не обновлён


def test_update_remaining_pulls_pulls_large_ok_pr_prod_form(monkeypatch):
    """Прод-форма #702: живые метки PR #618 и #333 (снято `gh api
    pulls/618`/`pulls/333` 2026-09-08 — `{"labels": ["review:large",
    "review:large-ok", "ai:ok"], "mergeable": true, "mergeable_state":
    "behind"}` для обоих) — до фикса merge_label_gate ни один из них не
    считался «близким к слиянию»: should_update_branch делегирует
    merge_label_gate напрямую, а тот требовал буквальный review:ok, которого
    у этой пары никогда бы не появилось без постороннего пуша (apply_large_ok
    ставит review:large-ok меткой). Круг: PR остаётся BEHIND → strict branch
    protection не даёт слить → «не близок к слиянию» → main не подтягивается
    → PR остаётся BEHIND. Контрольный #700 несёт review:large БЕЗ
    review:large-ok — размер ещё не принят, подтягивание по-прежнему
    пропускается.

    Мутация: верни merge_label_gate к буквальному `REVIEW_OK in names` —
    update_calls ниже опустеет (ни #618, ни #333 не подтянутся), а #700
    остаётся отсеянным в обоих случаях."""
    others = [
        pull(618, labels=["review:large", "review:large-ok", "ai:ok"]),
        pull(333, labels=["review:large", "review:large-ok", "ai:ok"]),
        pull(700, labels=["review:large", "ai:ok"]),  # размер не принят — контроль
    ]
    fake = FakeGh({"pulls/618/update-branch": None})
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 1 and "pulls/618/update-branch" in update_calls[0]
    slot_taken_lines = [
        line for line in (observations + actions)
        if "#333" in line and "слот update_branch" in line and "занят другим PR" in line
    ]
    assert len(slot_taken_lines) == 1, "#333 обязан пройти предикат — слот занял #618, не отсев"
    not_close_lines = [
        line for line in (observations + actions)
        if "не подтянут" in line and "#700" in line and "не близок к слиянию" in line
    ]
    assert len(not_close_lines) == 1, "#700 без review:large-ok обязан остаться отсеянным предикатом"


def test_update_remaining_pulls_draft_skipped_before_predicate(monkeypatch):
    # Драфт отсеивается раньше should_update_branch — даже с зелёными
    # вердиктами его не трогаем (см. merge_queue: драфт не сливается никогда).
    others = [pull(4, draft=True, labels=["review:ok", "ai:ok"])]
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.update_remaining_pulls(REPO, 1, others)
    assert (observations + actions) == []
    assert fake.calls == []


def test_update_remaining_pulls_skip_is_not_silent(monkeypatch):
    others = [pull(4, labels=["review:ok"])]  # нет ai:ok — не близок к слиянию
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.update_remaining_pulls(REPO, 1, others)
    assert fake.calls == []  # update-branch не вызван вовсе — газ не тратим впустую
    assert len((observations + actions)) == 1
    assert "#4" in (observations + actions)[0] and "не подтянут" in (observations + actions)[0]


def test_update_remaining_pulls_failed_attempt_does_not_consume_slot(monkeypatch):
    # Находка AI-ревью PR #288: докстринг update_branch обещает "слот
    # занимается только УСПЕХОМ" — это держится на честном слове, если слот
    # можно пометить занятым ДО вызова gh (мутация: `pulled = True`/
    # `_update_branch_used_this_run = True` раньше настоящего push'а). Первый
    # кандидат падает (не найдя настоящей ошибки — используем боевой
    # update_branch, не заглушку), второй ОБЯЗАН получить попытку тем же
    # прогоном: неудача не должна расходовать общий слот.
    others = [
        pull(2, labels=["review:ok", "ai:ok"]),  # упадёт при update-branch
        pull(3, labels=["conflict"]),             # обязан получить попытку следом
    ]
    fake = FakeGh({
        "pulls/2/update-branch": RuntimeError("422 Merge conflict"),
        "pulls/3/update-branch": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 2, "обе попытки обязаны произойти — неудача не занимает слот"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("pulls/3/update-branch" in c for c in update_calls)
    failed_lines = [line for line in (observations + actions) if "не обновлён" in line]
    updated_lines = [line for line in (observations + actions) if line.startswith("🔄")]
    assert len(failed_lines) == 1 and "#2" in failed_lines[0]
    assert len(updated_lines) == 1 and "#3" in updated_lines[0]


def test_update_remaining_pulls_reports_conflict_loudly_not_silently(monkeypatch):
    others = [pull(2, labels=["conflict"])]  # конфликт проходит предикат — попытка будет

    def broken(repo, n):
        raise RuntimeError("422 Merge conflict")

    monkeypatch.setattr(sch, "update_branch", broken)
    observations, actions = sch.update_remaining_pulls(REPO, 1, others)
    assert len((observations + actions)) == 1
    assert "не обновлён" in (observations + actions)[0] and "422 Merge conflict" in (observations + actions)[0]
    assert "mark_conflicts" in (observations + actions)[0]  # видимая причина, не молчание


def test_update_remaining_pulls_excludes_just_merged_and_empty_is_noop(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.update_remaining_pulls(REPO, 1, [pull(1)])  # единственный "other" == merged
    assert (observations + actions) == []
    assert fake.calls == []


# ── Прод-форма сбоя update_branch: subprocess.CalledProcessError при ORCHESTRA_PAT ──
# Находка AI-ревью PR #288 (вторая): в проде ORCHESTRA_PAT задан
# (.github/workflows/orchestra.yml), значит update_branch падает через
# subprocess.run(check=True) → subprocess.CalledProcessError, а не через
# gh()/RuntimeError. Все тесты выше делают monkeypatch.delenv("ORCHESTRA_PAT")
# или подменяют gh()/update_branch напрямую — ветка except
# subprocess.CalledProcessError в update_branch_or_report не была накрыта
# вовсе. Прод-форма ошибки: gh api пишет причину в stderr процесса, код
# возврата ненулевой — ровно то, что кидает subprocess.run(check=True).


def test_update_branch_skips_when_ai_review_running_for_pr(monkeypatch):
    # Тормоз гонки update_branch/ai-review (#488, живой инцидент): PR #2
    # behind main и близок к слиянию (оба вердикта зелёные), но по нему
    # ПРЯМО СЕЙЧАС летит ai-review.yml (in_progress) — update_branch обязан
    # отказаться двигать head ДО самого push'а (см. update_branch,
    # review_labels.other_active_ai_review_runs), а merge_queue — доложить
    # об этом отдельной строкой-наблюдением, не веткой "не удался" (та —
    # для реальных сетевых сбоев push'а, см. соседний тест сетевого сбоя) и
    # не веткой "слот занят" (слот вообще не был потрачен).
    pulls = [pull(2, labels=["review:ok", "ai:ok"])]
    running_title = sch.review_labels.ai_review_run_name(2)
    fake = FakeGh({
        "pulls/2": {"mergeable_state": "behind"},
        "actions/workflows/ai-review.yml/runs": {
            "workflow_runs": [{"id": 999, "display_title": running_title, "status": "in_progress"}],
        },
        # Не должен быть вызван вовсе — мутация ниже это и проверяет.
        "pulls/2/update-branch": AssertionError("update-branch не должен был вызываться"),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is False
    assert not any("update-branch" in c for c in fake.calls)
    assert any("летит ai-review" in line and "#2" in line for line in observations)
    assert not any("не удался" in line for line in (observations + actions))


def test_update_branch_or_report_pat_set_called_process_error_includes_stderr(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_PAT", "test-pat-token")
    # update_branch сверяет активные ai-review-прогоны ДО push'а (тормоз
    # гонки, #488) — здесь их нет, дальше должен дойти реальный push и упасть
    # на subprocess.CalledProcessError, который и есть предмет теста.
    patch_gh(monkeypatch, FakeGh({"actions/workflows/ai-review.yml/runs": {"workflow_runs": []}}))

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, output="",
            stderr="gh: Update is not a fast forward (HTTP 422)\n",
        )

    monkeypatch.setattr(sch.subprocess, "run", fake_run)

    line = sch.update_branch_or_report(
        REPO, 2,
        on_success="успех — быть не должно",
        on_budget_exhausted="слот — быть не должно",
        on_ai_review_running="ai-review летит — быть не должно",
        on_error="#2 — update_branch не удался: {error}",
    )

    assert "Update is not a fast forward" in line
    assert "HTTP 422" in line


def test_update_branch_or_report_pat_set_success_uses_subprocess(monkeypatch):
    # Контроль: PAT задан — успех тоже обязан идти через subprocess.run
    # (не gh()), иначе тест выше проверял бы ветку, которая в проде не
    # используется вовсе.
    monkeypatch.setenv("ORCHESTRA_PAT", "test-pat-token")
    patch_gh(monkeypatch, FakeGh({"actions/workflows/ai-review.yml/runs": {"workflow_runs": []}}))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(sch.subprocess, "run", fake_run)

    line = sch.update_branch_or_report(
        REPO, 2, on_success="✅", on_budget_exhausted="budget",
        on_ai_review_running="ai-review летит — быть не должно", on_error="{error}",
    )

    assert line == "✅"
    assert len(calls) == 1
    assert any("update-branch" in str(part) for part in calls[0])
    assert any("Bearer test-pat-token" in str(part) for part in calls[0])


# ── Тот же предикат — behind-ветка merge_queue (#252, пункт 3 задачи) ────────────


def test_merge_queue_behind_not_close_to_merge_skips_without_update(monkeypatch):
    pulls = [pull(2, labels=["review:ok"])]  # нет ai:ok — не близок к слиянию
    fake = FakeGh({"pulls/2/update-branch": None, "pulls/2": {"mergeable_state": "behind"}})
    patch_gh(monkeypatch, fake)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is False
    assert not any("update-branch" in c for c in fake.calls)
    assert any("не близок к слиянию" in line for line in (observations + actions))


def test_merge_queue_behind_close_to_merge_updates(monkeypatch):
    pulls = [pull(2, labels=["review:ok", "ai:ok"])]
    fake = FakeGh({"pulls/2/update-branch": None, "pulls/2": {"mergeable_state": "behind"}})
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is True
    assert any("pulls/2/update-branch" in c for c in fake.calls)
    assert any("обновлена из main" in line for line in (observations + actions))


def test_merge_queue_behind_conflict_updates_even_without_verdicts(monkeypatch):
    pulls = [pull(2, labels=["conflict"])]
    fake = FakeGh({"pulls/2/update-branch": None, "pulls/2": {"mergeable_state": "behind"}})
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is True
    assert any("pulls/2/update-branch" in c for c in fake.calls)


def test_merge_queue_two_behind_prs_share_one_update_branch_slot(monkeypatch):
    # Находка AI-ревью PR #288 (главная): раньше дисциплина "максимум один
    # успешно подтянутый за прогон" жила только внутри update_remaining_pulls
    # — сама behind-ветка merge_queue могла подтянуть НЕСКОЛЬКО behind-PR за
    # один свой проход по списку `pulls`, ничем не ограниченная. Два behind-PR
    # с обоими зелёными вердиктами в одном вызове merge_queue обязаны дать
    # РОВНО один update-branch, второй — отдельную строку "слот занят".
    pulls = [
        pull(2, labels=["review:ok", "ai:ok"]),
        pull(3, labels=["review:ok", "ai:ok"]),
    ]
    fake = FakeGh({
        "pulls/2": {"mergeable_state": "behind"},
        "pulls/3": {"mergeable_state": "behind"},
        "pulls/2/update-branch": None,
        "pulls/3/update-branch": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is True
    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 1, "два behind-PR за один проход не должны дать два update-branch"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("слот update_branch" in line and "#3" in line for line in (observations + actions))


def test_merge_queue_behind_network_error_reported_not_raised(monkeypatch):
    # #288 (класс: разная обработка ошибок update_branch в разных точках
    # вызова, тот же класс, что уже чинили точечно в #248 находка 3 и #253
    # находка 4). До фикса behind-ветка merge_queue ловила только
    # UpdateBranchBudgetExhausted — сетевой сбой (RuntimeError/
    # CalledProcessError) пробрасывался наружу и ронял merge_queue и main()
    # целиком: без summary, без отчёта, без очереди слияний. Два behind-PR:
    # первый падает по сети, второй обязан получить попытку тем же обходом —
    # неудача не потребляет общий слот update_branch (см. update_branch).
    pulls = [
        pull(2, labels=["review:ok", "ai:ok"]),  # обновление упадёт по сети
        pull(3, labels=["review:ok", "ai:ok"]),  # обязан получить попытку следом
    ]
    fake = FakeGh({
        # Более специфичные маршруты (.../update-branch) обязаны идти ПЕРЕД
        # короткими (.../pulls/N) — FakeGh матчит по первой подходящей
        # подстроке, и короткий фрагмент иначе перехватит PUT-запрос раньше,
        # чем до него дойдёт исключение (см. соседние тесты behind-ветки).
        "pulls/2/update-branch": RuntimeError("dial tcp: connection refused"),
        "pulls/3/update-branch": None,
        "pulls/2": {"mergeable_state": "behind"},
        "pulls/3": {"mergeable_state": "behind"},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)  # не должно кинуть исключение

    assert not hard_failure
    assert merged_number is None
    assert updated is True
    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 2, "сбой на #2 не должен остановить обход — #3 обязан получить попытку"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("pulls/3/update-branch" in c for c in update_calls)
    failed_lines = [line for line in (observations + actions) if "не удался" in line]
    updated_lines = [line for line in (observations + actions) if "обновлена из main" in line]
    assert len(failed_lines) == 1 and "#2" in failed_lines[0] and "dial tcp" in failed_lines[0]
    assert len(updated_lines) == 1 and "#3" in updated_lines[0]


# ── Гвардия clean-состояния: гейт слияния не обходится за пределами behind ──────


def test_merge_queue_clean_state_without_ai_ok_not_merged(monkeypatch):
    """#297: цикл слияний доверяет решение "готов ли PR" merge_queue — эта
    гвардия доказывает, что clean-состояние с зелёными чеками, но БЕЗ ai:ok,
    не сливается. Мутация: закомментировать проверку gate_reason ниже —
    тест обязан покраснеть (появится -X PUT .../merge)."""
    pulls = [pull(2, labels=["review:ok"])]  # нет ai:ok — второй гейт не пройден
    fake = FakeGh({
        "pulls/2": {"mergeable_state": "clean"},
        "commits/sha2/check-runs": {"check_runs": [{"name": "ci", "conclusion": "success"}]},
    })
    patch_gh(monkeypatch, fake)

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number is None
    assert updated is False
    assert not any(c.startswith("-X PUT") and "/merge" in c for c in fake.calls)
    # #456: пропуск кандидата (гейт меток не пройден) — наблюдение, не действие.
    assert any("ai:ok" in line for line in observations)
    assert actions == []


def test_merge_queue_merges_review_large_with_large_ok_prod_form(monkeypatch):
    """Прод-форма PR #333 (2026-09-08, снято `gh api pulls/333` и
    `commits/<sha>/check-runs` живого репозитория): `mergeable_state=unstable`,
    метки `review:large`+`review:large-ok`+`ai:ok` (НЕ `review:ok` — большой
    дифф, размер принят меткой, а не переносом на review:ok), 11 сырых
    check-run'ов с дублями `contract`(x2)/`orchestra`(x2) (два pull_request-
    триггера workflow orchestra.yml подряд), все success/skipped.

    До фикса merge_label_gate (review_labels.py) этот PR стоял бы с этим же
    набором меток НАВСЕГДА — apply_large_ok (ai_review.py) ставит
    review:large-ok меткой, без нового пуша, а pr-review.yml (где
    check_pr.py конвертировал бы review:large → review:ok) реагирует только
    на opened/synchronize/reopened. Мутация: замени `gate1_ok = REVIEW_OK in
    names or (...)` на `gate1_ok = REVIEW_OK in names` в review_labels.py —
    тест обязан покраснеть (не появится -X PUT .../merge, останется
    "нет вердикта review:ok" в observations)."""
    pulls = [pull(333, labels=["review:large", "review:large-ok", "ai:ok"])]
    fake = FakeGh({
        "pulls/333": {"mergeable_state": "unstable"},
        "commits/sha333/check-runs": {"check_runs": [
            {"name": "CodeQL", "conclusion": "success", "started_at": "2026-09-07T21:58:40Z"},
            {"name": "orchestra", "conclusion": "skipped", "started_at": "2026-09-07T21:58:05Z"},
            {"name": "contract", "conclusion": "success", "started_at": "2026-09-07T21:58:06Z"},
            {"name": "orchestra", "conclusion": "skipped", "started_at": "2026-09-07T21:57:48Z"},
            {"name": "contract", "conclusion": "success", "started_at": "2026-09-07T21:57:50Z"},
            {"name": "review", "conclusion": "success", "started_at": "2026-09-07T21:57:50Z"},
            {"name": "canary", "conclusion": "success", "started_at": "2026-09-07T21:57:49Z"},
            {"name": "analyze", "conclusion": "success", "started_at": "2026-09-07T21:57:49Z"},
            {"name": "test", "conclusion": "success", "started_at": "2026-09-07T21:57:51Z"},
            {"name": "worker-test", "conclusion": "success", "started_at": "2026-09-07T21:57:51Z"},
            {"name": "archive-fixup", "conclusion": "success", "started_at": "2026-09-07T21:57:50Z"},
        ]},
        "pulls/333/merge": None,
    })
    patch_gh(monkeypatch, fake)
    # Предмет теста — решение merge_queue (гейт меток + агрегация чеков), не
    # проводка after_merge (архив/deploy/release задачи) — та разобрана
    # отдельными тестами after_merge_* ниже в этом файле.
    monkeypatch.setattr(sch, "after_merge", lambda repo, pull, others: ([], [], False))

    observations, actions, hard_failure, merged_number, updated = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert merged_number == 333
    assert any(c.startswith("-X PUT") and "pulls/333/merge" in c for c in fake.calls)
    assert any("слит" in line and "#333" in line for line in actions)


# ── Цикл слияний внутри одного прогона (#297) ────────────────────────────────
# merge_loop оборачивает merge_queue повторными проходами — тесты ниже
# подставляют свой merge_queue (не гоняют реальную логику готовности PR,
# та уже покрыта тестами выше) и проверяют именно управление циклом: сколько
# проходов, когда останов, когда пауза.


def test_merge_loop_merges_multiple_prs_in_one_run(monkeypatch):
    """#297: один прогон обязан провести ОЧЕРЕДЬ, а не одного кандидата —
    до этой правки main() звал merge_queue ровно один раз, и второе готовое
    слияние ждало следующего запуска планировщика (расписание с доставкой
    ~7%, docs/research/21). Три прохода: первые два сливают, третий говорит
    "нечего сливать" — итог два слияния за один вызов merge_loop, без пауз
    (каждый прогресс был слиянием, а не обновлением ветки)."""
    calls = []
    results = [
        ([], ["✅ PR #1 слит (squash)"], False, 1, True),
        ([], ["✅ PR #2 слит (squash)"], False, 2, True),
        (["⏸️ очередь пуста"], [], False, None, False),
    ]

    def fake_merge_queue(repo, pulls):
        calls.append(pulls)
        return results[len(calls) - 1]

    monkeypatch.setattr(sch, "merge_queue", fake_merge_queue)
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    slept = []
    monkeypatch.setattr(sch.time, "sleep", lambda s: slept.append(s))

    observations, actions, hard_failure, final_pulls = sch.merge_loop(REPO, [])

    assert not hard_failure
    assert len(calls) == 3
    assert not slept, "прогресс был только слияниями — ждать было нечего"
    assert any("2 PR слито" in line for line in (observations + actions))
    assert final_pulls == []  # open_pulls замокан на []: третий снимок цикла


def test_merge_loop_stops_at_max_merges_cap(monkeypatch):
    """#297: потолок MERGE_LOOP_MAX_MERGES обязан остановить цикл, даже если
    формально есть что сливать ещё дальше — без потолка событийный триггер
    на большую скопившуюся очередь держал бы concurrency-слот orchestra
    сколь угодно долго. Предохранитель в самом фейке (len(calls) > cap+5)
    страхует от зависания теста, если потолок в коде сломан вовсе."""
    calls = []

    def fake_merge_queue(repo, pulls):
        calls.append(1)
        if len(calls) > sch.MERGE_LOOP_MAX_MERGES + 5:
            raise AssertionError("цикл слияний не останавливается на потолке — уходит в бесконечность")
        return ([], [f"✅ PR #{len(calls)} слит (squash)"], False, len(calls), True)

    monkeypatch.setattr(sch, "merge_queue", fake_merge_queue)
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(
        sch.time, "sleep",
        lambda s: pytest.fail("не должен ждать — каждый проход сливал, прогресс всегда был мержем"),
    )

    observations, actions, hard_failure, final_pulls = sch.merge_loop(REPO, [])

    assert not hard_failure
    assert len(calls) == sch.MERGE_LOOP_MAX_MERGES, "цикл обязан остановиться ровно на потолке слияний"
    assert any(f"{sch.MERGE_LOOP_MAX_MERGES} PR слито" in line for line in (observations + actions))
    assert final_pulls == []


def test_merge_loop_stops_immediately_without_progress(monkeypatch):
    """#297: проход без слияния и без обновления ветки не даёт циклу
    продолжать вслепую — без нового внешнего события (нет проверок, ветка не
    отставала) повтор прямо сейчас даст тот же результат. Мутация: убрать
    `if not updated: break` в merge_loop — тест обязан покраснеть (второй
    вызов merge_queue вместо одного)."""
    calls = []
    sentinel_pulls = [pull(1, labels=["review:ok"])]  # отличим от того, что вернул бы open_pulls

    def fake_merge_queue(repo, pulls):
        calls.append(1)
        return (["⏸️ #1 — нет вердикта ai:ok"], [], False, None, False)

    monkeypatch.setattr(sch, "merge_queue", fake_merge_queue)
    monkeypatch.setattr(
        sch, "open_pulls",
        lambda repo: (_ for _ in ()).throw(AssertionError("без прогресса open_pulls звать не за чем")))
    monkeypatch.setattr(sch.time, "sleep", lambda s: pytest.fail("нечего ждать — прогресса не было"))

    observations, actions, hard_failure, final_pulls = sch.merge_loop(REPO, sentinel_pulls)

    assert not hard_failure
    assert len(calls) == 1, "без прогресса цикл обязан остановиться после первого прохода"
    # (#443) без прогресса — исходный снимок возвращается как есть, второй
    # open_pulls() не нужен: ничего не изменилось со времени вызова merge_loop.
    assert final_pulls is sentinel_pulls


def test_merge_loop_waits_after_branch_update_before_retry(monkeypatch):
    """#297: проход, который только обновил ветку (checks перезапущены), — это
    прогресс, не тупик: цикл ждёт MERGE_LOOP_POLL_SECONDS и пробует снова.
    Второй проход без изменений (checks ещё не готовы) останавливает цикл —
    в проде следующая попытка придёт по новому событию workflow_dispatch,
    не по бесконечному опросу."""
    calls = []
    results = [
        ([], ["🔄 #1 обновлён из main"], False, None, True),
        (["⏸️ #1 — behind main, checks ещё не готовы"], [], False, None, False),
    ]

    def fake_merge_queue(repo, pulls):
        calls.append(1)
        return results[len(calls) - 1]

    monkeypatch.setattr(sch, "merge_queue", fake_merge_queue)
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    slept = []
    monkeypatch.setattr(sch.time, "sleep", lambda s: slept.append(s))

    observations, actions, hard_failure, final_pulls = sch.merge_loop(REPO, [])

    assert not hard_failure
    assert len(calls) == 2
    assert slept == [sch.MERGE_LOOP_POLL_SECONDS]
    assert final_pulls == []  # обновление ветки — прогресс, снимок перечитан после паузы


# Газ mark_conflicts (#270): метка conflict должна и сниматься тоже.
def test_mark_conflicts_clears_label_on_explicit_non_conflict_state(monkeypatch):
    pulls = [pull(2, labels=["conflict"])]
    fake = FakeGh({"pulls/2": {"mergeable_state": "clean"}, "labels/conflict": None})
    patch_gh(monkeypatch, fake)
    lines = sch.mark_conflicts(REPO, pulls)
    assert any(c.startswith("-X DELETE") and "labels/conflict" in c for c in fake.calls)
    assert any("снята" in line for line in lines)
    assert sch.review_labels.should_update_branch(set()) is False  # больше не газ


def test_mark_conflicts_keeps_label_on_unknown_state_not_silent_wrong(monkeypatch):
    pulls = [pull(2, labels=["conflict"])]  # mergeable_state ещё не вычислен (null)
    fake = FakeGh({"pulls/2": {"mergeable_state": None}})
    patch_gh(monkeypatch, fake)
    lines = sch.mark_conflicts(REPO, pulls)
    assert lines == []
    assert not any(c.startswith(("-X POST", "-X PUT", "-X DELETE")) for c in fake.calls)


def test_mark_conflicts_posts_label_and_comment_on_dirty_state(monkeypatch):
    fake = FakeGh({"pulls/2": {"mergeable_state": "dirty"}, "issues/2/labels": None, "issues/2/comments": None}); patch_gh(monkeypatch, fake)
    lines = sch.mark_conflicts(REPO, [pull(2, labels=[])])
    assert any("issues/2/labels" in c and "conflict" in c for c in fake.mutating_calls()) and any("issues/2/comments" in c for c in fake.mutating_calls()) and any("помечен" in line and "conflict" in line for line in lines)


# ── Класс «устаревшая метка в памяти» (#252, второй заход) ───────────────────────
# mark_conflicts снимала/ставила метку conflict через gh, но НЕ обновляла
# pull["labels"] в переданном объекте — а main() передаёт тот же список pulls
# дальше в merge_queue/update_remaining_pulls. should_update_branch там видел
# метку, которую API уже удалил секундами раньше (лог 33904096031: слияние
# #284 подтянуло #253/#248 без ai:ok только из-за протухшей `conflict`).
# Мутация: закомментируй в _set_conflict_label обновление pull["labels"]
# (оставь только gh-вызов) — оба теста ниже краснеют.


def test_mark_conflicts_clear_syncs_pull_object_so_predicate_sees_it_now(monkeypatch):
    p = pull(2, labels=["conflict"])
    pulls = [p]
    fake = FakeGh({"pulls/2": {"mergeable_state": "clean"}, "labels/conflict": None})
    patch_gh(monkeypatch, fake)
    sch.mark_conflicts(REPO, pulls)
    # Тот же объект p из того же списка pulls — как main() передаёт его дальше.
    assert sch.review_labels.should_update_branch(p["labels"]) is False
    assert not any(label["name"] == sch.CONFLICT_LABEL for label in p["labels"])


def test_mark_conflicts_post_syncs_pull_object_so_predicate_sees_it_now(monkeypatch):
    p = pull(2, labels=[])
    pulls = [p]
    fake = FakeGh({"pulls/2": {"mergeable_state": "dirty"}, "issues/2/labels": None, "issues/2/comments": None})
    patch_gh(monkeypatch, fake)
    sch.mark_conflicts(REPO, pulls)
    # should_update_branch должен теперь видеть свежепоставленную conflict —
    # без синхронизации объект p её бы не содержал до следующего fetch.
    assert sch.review_labels.should_update_branch(p["labels"]) is True


# ── Расшивка конфликтов, автоматическая (#474) ───────────────────────────────────
# mark_conflicts (выше) только ставит метку и ждёт человека. dispatch_conflict_rework
# — цикл, который её подхватывает: снимает assignee+замок задачи и запускает
# worker.yml адресно (вход task, тот же путь, что доводка открытых PR). Признака
# «дрейф или содержательный конфликт» до попытки нет — решение простое: одна
# авто-попытка на PR (лифтайм-счётчик), не сошлось — эскалация владельцу.


def test_dispatch_conflict_rework_releases_task_and_dispatches_targeted_worker(monkeypatch):
    task = issue(474, assignees=("mytab0r",))
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [],  # метка ещё не проставлялась — ни одной авто-попытки
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "issues/474/assignees": None,
        "workflows/worker.yml/dispatches": None,  # 204 без тела — прод-форма успеха
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is True
    assert task["assignees"] == []  # мутация pool сразу вслед за DELETE (тот же приём, что unhealthy_pulls)
    assert any(c.startswith("-X DELETE") and "issues/474/assignees" in c for c in fake.calls)
    dispatch_calls = [c for c in fake.calls if "worker.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[task]=474" in dispatch_calls[0]
    assert posted and posted[0][0] == 560
    assert sch.CONFLICT_REWORK_MARKER in posted[0][1]
    assert any("#560" in line and "освобождена" in line for line in actions)
    assert observations == []


def test_dispatch_conflict_rework_silent_while_worker_active(monkeypatch):
    task = issue(474, assignees=("mytab0r",))
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [],
        "workflows/worker.yml/runs?status=in_progress": {
            "workflow_runs": [workflow_run(33814313381, "in_progress")]},
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("воркер занят — не пишем"))
    monkeypatch.setattr(sch.claim_task, "release", lambda *a: pytest.fail("воркер занят — не трогаем задачу"))

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is False
    assert actions == []
    assert any("занят" in line and "#560" in line for line in observations)
    assert task["assignees"] != []  # задача не тронута
    assert not any(c.startswith("-X DELETE") for c in fake.calls)


def test_dispatch_conflict_rework_escalates_after_budget_exhausted(monkeypatch):
    # Мутация: убери проверку `attempts >= CONFLICT_REWORK_MAX_ATTEMPTS` в
    # dispatch_conflict_rework — этот тест покраснеет (ушёл бы второй dispatch
    # worker.yml вместо эскалации; assert ниже про отсутствие dispatches это
    # и доказывает).
    task = issue(474, assignees=("mytab0r",))
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase", base_sha="basesha")
    fake = FakeGh({
        # Момент простановки CONFLICT_LABEL (issue #588) — граница, отделяющая
        # прогон, который открыл этот PR, от прогона авто-ребейза.
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        "issues/120/comments?per_page=100": [],
        # Порядок ключей важен (FakeGh матчит первую подстроку по вставке):
        # "pulls/560/files" обязан проверяться раньше "pulls/560" — иначе
        # более короткий фрагмент "pulls/560" перехватил бы и вызов files.
        "pulls/560/files": files_payload(["a.py", "b.py"]),
        "compare/basesha...main": {"files": files_payload(["b.py", "c.py"])},
        # Находка ревью PR #478: эскалация перепроверяет актуальный
        # mergeable_state (не доверяет только метке) — здесь он подтверждён.
        "pulls/560": {"mergeable_state": "dirty"},
        # Единственная попытка уже ЗАВЕРШИЛАСЬ (не в in_progress/queued) —
        # иначе эскалация обязана подождать (см. соседний тест "ещё идёт").
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        # Вторая находка ревью PR #478 ("алерт не гадает"): текст обязан
        # называть conclusion прогона, атрибутированного задаче, а не
        # утверждать причину («содержательный конфликт») от себя. created_at
        # позже метки conflict (#588) — иначе прогон не засчитался бы попыткой.
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            {"id": 34011108934, "conclusion": "success", "created_at": "2026-09-06T09:00:00Z"},
        ]},
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T10:00:00Z",
             "body": "🔒 Аренда задачи: `mytab0r` держит замок `refs/locks/task-474` "
                     "(TTL 24 ч по коммиту замка). Канал: worker run 34011108934."},
            # Отметка «дошли до git-шага» (#588) — без неё этот прогон не
            # засчитался бы попыткой (см. соседний тест на инфра-сбой).
            {"created_at": "2026-09-06T10:05:00Z",
             "body": "🤖 [worker: git-шаг] worker run 34011108934"},
        ],
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("эскалация — не обычный комментарий в PR"))
    monkeypatch.setattr(sch.claim_task, "release", lambda *a: pytest.fail("бюджет исчерпан — задачу не трогаем"))

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is False
    assert not any("worker.yml/dispatches" in c for c in fake.calls)  # второй попытки не было
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert "b.py" in escalated[0][2]  # пересечение изменений PR и main
    assert "a.py" not in escalated[0][2] and "c.py" not in escalated[0][2]
    assert "conclusion='success'" in escalated[0][2]  # факт, не диагноз
    # больше не утверждаем диагноз как решённый факт — только гипотеза наравне с другими
    assert "main и PR правят одно и то же по-разному" not in escalated[0][2]
    assert any("исчерпана" in line and "#560" in line for line in actions)
    assert task["assignees"] != []  # эскалация не трогает задачу


def test_dispatch_conflict_rework_retries_instead_of_escalating_after_infra_failure(monkeypatch):
    # Issue #588: живые случаи #567/#542/#408 — единственный прогон авто-
    # ребейза упал ДО git-шага (морда/деплой недоступны, отсутствующий
    # скрипт). Раньше (маркер CONFLICT_REWORK_MARKER = сырой счётчик
    # диспатчей) это НАВСЕГДА жгло единственный бюджет и вело к эскалации,
    # хотя git rebase origin/main ни разу не запускался. Прогон атрибутирован
    # задаче (след аренды "worker run N" есть), но БЕЗ отметки git-шага —
    # попытка не должна засчитаться, PR обязан получить новый адресный
    # dispatch, а не эскалацию.
    #
    # Мутация: верни conflict_rework_attempts к старой сигнатуре/логике (сырой
    # подсчёт CONFLICT_REWORK_MARKER в комментариях САМОГО PR, без разбора
    # git-шага) — фикстура "issues/561/comments" ниже даёт старому коду ровно
    # то, что нужно, чтобы увидеть attempts=1 и покраснеть (dispatched
    # остался бы False, ушла бы эскалация вместо повтора).
    task = issue(475, assignees=("mytab0r",))
    p = pull(561, labels=["conflict"], ref="agent/475-conflict-auto-rebase", base_sha="basesha")
    fake = FakeGh({
        # Момент простановки CONFLICT_LABEL — граница отсчёта попыток (#588).
        "issues/561/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        # Старая семантика читала ИМЕННО этот эндпоинт (комментарии PR) —
        # оставлен намеренно, чтобы мутация (откат к старой сигнатуре)
        # покраснела на данных, а не молча прошла из-за отсутствия маршрута.
        "issues/561/comments": [
            {"created_at": "2026-09-05T10:00:00Z",
             "body": f"🤖 {sch.CONFLICT_REWORK_MARKER} попытка 1/1"},
        ],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        # Единственный прогон существует и атрибутирован задаче #475, но упал
        # ДО git-шага (живой случай #476/#567: недоступность морды).
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            {"id": 34027035455, "conclusion": "failure", "created_at": "2026-09-06T09:00:00Z"},
        ]},
        f"{REPO}/issues/475/comments?per_page": [
            {"created_at": "2026-09-06T09:01:00Z",
             "body": "🔒 Аренда задачи: `mytab0r` держит замок `refs/locks/task-475` "
                     "(TTL 24 ч по коммиту замка). Канал: worker run 34027035455."},
            # Нет отметки [worker: git-шаг] — воркер упал раньше, чем до неё дошёл.
        ],
        "issues/475/assignees": None,
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    posted = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is True
    assert escalated == []  # инфра-сбой не эскалирует — бюджет не считается сгоревшим
    dispatch_calls = [c for c in fake.calls if "worker.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[task]=475" in dispatch_calls[0]
    assert posted and posted[0][0] == 561
    assert "попытка 1/1" in posted[0][1]  # предыдущий незасчитанный прогон не сдвинул счётчик
    assert task["assignees"] == []
    assert any("#561" in line and "освобождена" in line for line in actions)


def test_dispatch_conflict_rework_escalation_text_admits_unattributed_run(monkeypatch):
    # Находка ревью PR #478 (сохранена через ревью PR #597, требование спеки
    # «текст эскалации называет подтверждённый факт»): попытка ЗАСЧИТАНА по
    # постоянному следу (отметка git-шага в комментариях задачи), но к моменту
    # эскалации её прогон уже выпал из окна recent_runs(per_page=10) —
    # worker.yml не простаивает (другие задачи/PR дают собственные прогоны,
    # ~3.3 слияния/час), окно между засчитанной попыткой и эскалацией вполне
    # успевает сдвинуться. conclusion этой попытки неоткуда взять — текст
    # честно говорит "не атрибутирован", не подсовывает conclusion чужого
    # прогона как диагноз ЭТОЙ попытки. После правки ревью PR #597 эта ветка
    # не только достижима, но и ЕДИНСТВЕННАЯ честная: счётчик больше не
    # зависит от окна (см. соседний тест на выпадение из окна), а вывод
    # conclusion по-прежнему ограничен им.
    task = issue(474, assignees=("mytab0r",))
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase", base_sha="basesha")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        "issues/120/comments?per_page=100": [],
        "pulls/560/files": files_payload([]),
        "compare/basesha...main": {"files": files_payload([])},
        "pulls/560": {"mergeable_state": "dirty"},
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        # В окне только чужой прогон 901: попытка (333→900) туда не попала.
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            {"id": 901, "conclusion": "success", "created_at": "2026-09-06T09:10:00Z"},
        ]},
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T09:01:00Z", "body": "Канал: worker run 900."},
            {"created_at": "2026-09-06T09:05:00Z", "body": "🤖 [worker: git-шаг] worker run 900"},
        ],
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("эскалация — не обычный комментарий в PR"))
    monkeypatch.setattr(sch.claim_task, "release", lambda *a: pytest.fail("бюджет исчерпан — задачу не трогаем"))

    sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert escalated and "не атрибутирован" in escalated[0][2]
    assert "main и PR правят одно и то же по-разному" not in escalated[0][2]


def test_dispatch_conflict_rework_holds_escalation_when_mergeable_state_unconfirmed(monkeypatch):
    # Находка ревью PR #478 (блокирующая): метка `conflict` НАМЕРЕННО
    # переживает mergeable_state None/unknown (mark_conflicts — «"не знаю"
    # не значит "нет конфликта"»). Окно: воркер успешно перебазировал и
    # запушил, прогон завершился, бюджет исчерпан — но GitHub ещё не
    # пересчитал mergeable_state из None обратно в явное состояние. Без
    # перепроверки эскалация соврала бы владельцу «остаётся в конфликте», и
    # маркер эскалации подавил бы её навсегда, хотя реального контента-
    # конфликта уже нет. Мутация: убери перечитывание mergeable_state перед
    # escalate — этот тест покраснеет (escalate был бы вызван на state=None).
    task = issue(474, assignees=())
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T09:01:00Z",
             "body": "Канал: worker run 34011108934."},
            {"created_at": "2026-09-06T09:05:00Z",
             "body": "🤖 [worker: git-шаг] worker run 34011108934"},
        ],
        "issues/120/comments?per_page=100": [],
        "pulls/560": {"mergeable_state": None},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("mergeable_state не подтверждён — рано эскалировать"))

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is False
    assert actions == []
    assert any("не подтверждён" in line and "#560" in line for line in observations)
    # не тратим вызовы на файлы PR/main — решение уже принято по state
    assert not any("pulls/560/files" in c or "compare/" in c for c in fake.calls)


def test_dispatch_conflict_rework_defers_escalation_while_attempt_still_running(monkeypatch):
    # Живая находка #474 (PR #408, прогон 34027474271): маркер попытки
    # ставится СРАЗУ на dispatch, а worker.yml идёт до 280 мин — без этой
    # гвардии следующий тик планировщика (каждые 15 мин) эскалировал бы
    # «не сошлось», пока единственная попытка ещё физически не завершилась.
    task = issue(474, assignees=())  # уже освобождена предыдущим dispatch
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        "workflows/worker.yml/runs?status=in_progress": {
            "workflow_runs": [workflow_run(34027474271, "in_progress")]},
        # Отметка git-шага ставится РАНО в прогоне (перед dsh_run_with_retry,
        # scripts/worker/task.sh) — задолго до конца 280-минутного job'а,
        # поэтому attempts может стать 1 ещё ПОКА прогон "in_progress" (тот
        # самый гоночный случай #474/PR #408, ради которого и нужен
        # worker_runs_active ниже как отдельная гвардия).
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T09:01:00Z",
             "body": "Канал: worker run 34027474271."},
            {"created_at": "2026-09-06T09:05:00Z",
             "body": "🤖 [worker: git-шаг] worker run 34027474271"},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("прогон ещё идёт — рано эскалировать"))

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is False
    assert actions == []
    assert any("ещё идёт" in line and "#560" in line for line in observations)
    # ждём результата попытки — ни файлы PR/main, ни маркер эскалации не читаем зря
    assert not any("pulls/560/files" in c or "compare/" in c or "issues/120/comments" in c
                   for c in fake.calls)


def test_dispatch_conflict_rework_escalation_is_idempotent(monkeypatch):
    marker = f"{sch.CONFLICT_ESCALATION_MARKER} #560"
    task = issue(474, assignees=("mytab0r",))
    p = pull(560, labels=["conflict"], ref="agent/474-conflict-auto-rebase")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T09:01:00Z",
             "body": "Канал: worker run 34011108934."},
            {"created_at": "2026-09-06T09:05:00Z",
             "body": "🤖 [worker: git-шаг] worker run 34011108934"},
        ],
        "issues/120/comments?per_page=100": [
            {"created_at": "2026-09-05T11:00:00Z", "body": f"🚨 {marker} — уже сказано"},
        ],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("уже эскалировано — не должен слать снова"))

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[task])

    assert dispatched is False
    assert actions == []
    assert observations == []
    # уже эскалировано — не читаем файлы PR/main зря
    assert not any("pulls/560/files" in c or "compare/" in c for c in fake.calls)


def test_dispatch_conflict_rework_observes_when_branch_has_no_task(monkeypatch):
    p = pull(560, labels=["conflict"], ref="dependabot/npm_and_yarn/foo-1.2.3")
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p], pool=[])
    assert dispatched is False
    assert actions == []
    assert any("не называет задачу" in line and "#560" in line for line in observations)
    assert fake.calls == []


def test_dispatch_conflict_rework_dispatches_only_one_pr_per_pass(monkeypatch):
    # Идемпотентность внутри одного вызова: второй конфликтующий PR тем же
    # проходом получает "воркер занят", а не второй dispatch — иначе
    # "ровно один workflow_dispatch воркера за пульс" (докстринг модуля)
    # нарушался бы уже внутри самой этой функции.
    task_a = issue(474, assignees=("mytab0r",))
    task_b = issue(475, assignees=("mytab0r",))
    # created_at различаются (#588 — граница возраста конфликта): нет метки
    # ни у одного (timeline пуст ниже), фолбэк — created_at PR, p_a старше —
    # тот же кандидат, что и до сортировки по возрасту, идёт первым.
    p_a = pull(560, labels=["conflict"], ref="agent/474-x", created_at="2026-09-01T00:00:00Z")
    p_b = pull(561, labels=["conflict"], ref="agent/475-y", created_at="2026-09-02T00:00:00Z")
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [],
        "issues/561/timeline?per_page=100": [],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "issues/474/assignees": None,
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: "ok")

    observations, actions, dispatched = sch.dispatch_conflict_rework(REPO, [p_a, p_b], pool=[task_a, task_b])

    assert dispatched is True
    dispatch_calls = [c for c in fake.calls if "worker.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[task]=474" in dispatch_calls[0]
    assert any("#561" in line and "занят" in line for line in observations)
    assert task_b["assignees"] != []  # вторая задача этим же проходом не тронута


def test_dispatch_conflict_rework_processes_oldest_conflict_first(monkeypatch):
    # Issue #588: open_pulls() (сырой `GET /pulls?state=open`) отдаёт НОВЫЕ PR
    # первыми, а один workflow_dispatch за проход раньше всегда доставался
    # самому свежему конфликту (замер живого репозитория: 10 из 14 конфликтных
    # PR не получили ни одной попытки, возраст до 84 часов). Здесь входной
    # порядок [p_new, p_old] намеренно "неправильный" (как отдаёт GitHub) —
    # дождаться дожен СТАРЕЙШИЙ по conflict_labeled_at, не первый по списку.
    #
    # Мутация: убери сортировку conflict_pulls (верни `for pull in pulls:` без
    # переупорядочивания) — этот тест покраснеет (inputs[task]=475, ветка
    # p_new, вместо ожидаемого 474).
    task_new = issue(475, assignees=("mytab0r",))
    task_old = issue(474, assignees=("mytab0r",))
    p_new = pull(561, labels=["conflict"], ref="agent/475-y")
    p_old = pull(560, labels=["conflict"], ref="agent/474-x")
    fake = FakeGh({
        # Метка на p_new проставлена НЕДАВНО, на p_old — почти четверо суток
        # назад: p_old обязан получить единственный слот этого прохода.
        "issues/561/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-06T20:00:00Z"},
        ],
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-03T00:00:00Z"},
        ],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": []},
        f"{REPO}/issues/474/comments?per_page": [],
        f"{REPO}/issues/475/comments?per_page": [],
        "issues/474/assignees": None,
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: "ok")

    # Вход НАРОЧНО в порядке "новый первым" — тот же порядок, что отдаёт
    # open_pulls() на живом репозитории.
    observations, actions, dispatched = sch.dispatch_conflict_rework(
        REPO, [p_new, p_old], pool=[task_new, task_old])

    assert dispatched is True
    dispatch_calls = [c for c in fake.calls if "worker.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[task]=474" in dispatch_calls[0]  # старейший конфликт, не первый по списку
    assert any("#561" in line and "занят" in line for line in observations)
    assert task_new["assignees"] != []  # свежий конфликт этим проходом не тронут


def test_conflict_labeled_at_returns_most_recent_labeling_episode(monkeypatch):
    # Тот же приём, что last_gate1_labeled_at: max(), не min() — метка могла
    # сниматься/ставиться несколькими эпизодами конфликта (mark_conflicts
    # снимает при чистом mergeable_state), нас интересует начало ТЕКУЩЕГО.
    # Мутация: замени max() на min() — тест вернул бы 09-01 вместо 09-05,
    # покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-01T00:00:00Z"},
            {"event": "unlabeled", "label": {"name": "conflict"}, "created_at": "2026-09-02T00:00:00Z"},
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T00:00:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_labeled_at(REPO, 560) == utc(2026, 9, 5, 0, 0)


def test_conflict_labeled_at_none_when_never_labeled(monkeypatch):
    fake = FakeGh({"issues/560/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    assert sch.conflict_labeled_at(REPO, 560) is None


def test_conflict_first_labeled_at_returns_earliest_labeling_episode(monkeypatch):
    # Обратное conflict_labeled_at: min(), не max() — граница лифтайм-бюджета
    # conflict_rework_attempts обязана указывать на ПЕРВЫЙ эпизод конфликта,
    # не на текущий (находка ревью PR #597: max() тут обнулял бы засчитанные
    # попытки при каждой повторной простановке метки). Мутация: замени min()
    # на max() — тест вернул бы 09-05 вместо 09-01, покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-01T00:00:00Z"},
            {"event": "unlabeled", "label": {"name": "conflict"}, "created_at": "2026-09-02T00:00:00Z"},
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
            {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-06T00:00:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_first_labeled_at(REPO, 560) == utc(2026, 9, 1, 0, 0)


def test_conflict_first_labeled_at_none_when_never_labeled(monkeypatch):
    fake = FakeGh({"issues/560/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    assert sch.conflict_first_labeled_at(REPO, 560) is None


def test_conflict_rework_attempts_lifetime_survives_relabeling(monkeypatch):
    # Ядро находки ревью PR #597: владелец задокументировал лифтайм-счётчик
    # (не сбрасывается по эпизодам конфликта, дельта-спека conflict-auto-rebase,
    # требование про бюджет попыток). Здесь PR словил
    # первый конфликт 09-01, засчитанная попытка пришлась НА ТОТ эпизод
    # (09-01), метку сняли и поставили заново 09-05 (новый эпизод дрейфа).
    # Граница по conflict_labeled_at (max, текущий эпизод — 09-05) стёрла бы
    # засчитанную попытку из старого эпизода: attempts вернулся бы к 0, хотя
    # бюджет уже потрачен. Мутация: верни since = conflict_labeled_at(...) —
    # attempts станет 0 вместо 1, тест покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-01T00:00:00Z"},
            {"event": "unlabeled", "label": {"name": "conflict"}, "created_at": "2026-09-02T00:00:00Z"},
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-01T06:05:00Z", "body": "Канал: worker run 444."},
            {"created_at": "2026-09-01T06:10:00Z", "body": "🤖 [worker: git-шаг] worker run 444"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 1


def test_conflict_rework_attempts_ignores_run_before_conflict_episode(monkeypatch):
    # Прогон, который ОТКРЫЛ этот PR (задолго до того, как main ушёл вперёд),
    # тоже дошёл бы до git-шага — он и есть источник PR. Без границы по
    # conflict_labeled_at он был бы ошибочно засчитан как попытка авто-
    # РЕБЕЙЗА. Мутация: убери фильтр `created_at < since` — attempts стал бы
    # 1 вместо 0, тест покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-01T00:05:00Z", "body": "Канал: worker run 111."},
            {"created_at": "2026-09-01T00:10:00Z", "body": "🤖 [worker: git-шаг] worker run 111"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 0


def test_conflict_rework_attempts_ignores_run_that_never_reached_git_step(monkeypatch):
    # Ядро фикса #588: прогон атрибутирован задаче (след аренды "worker run
    # N" есть) и стартовал ПОСЛЕ простановки конфликта, но упал ДО git-шага
    # (живые случаи #567/#542/#408 — сеть/деплой морды, отсутствующий
    # скрипт). Такая попытка не должна жечь единственный бюджет.
    # Мутация: убери проверку WORKER_GIT_STEP_MARKER (засчитывай любой
    # атрибутированный прогон) — attempts стал бы 1 вместо 0, покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T00:05:00Z", "body": "Канал: worker run 222."},
            # Нет отметки git-шага — воркер упал раньше, чем до неё дошёл.
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 0


def test_conflict_rework_attempts_counts_run_that_reached_git_step(monkeypatch):
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T00:05:00Z", "body": "Канал: worker run 333."},
            {"created_at": "2026-09-06T00:10:00Z", "body": "🤖 [worker: git-шаг] worker run 333"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 1


def test_conflict_rework_attempts_counts_run_that_fell_out_of_recent_runs_window(monkeypatch):
    # Блокирующая находка ревью PR #597: счётчик читал окно
    # recent_runs(per_page=10) — топ-10 свежих прогонов worker.yml, а живой
    # репозиторий выдаёт прогон на каждое слияние/ретрай/доводку (~3.3/час),
    # так что засчитанная попытка выпадала из окна уже через ~2–3 часа, сама
    # оставаясь легитимной (прогон идёт до 280 минут). attempts возвращался
    # к 0, PR с несошедшейся попыткой получал адресный диспатч бесконечно и
    # никогда не доходил до эскалации — голодание #588 перестраивалось.
    # Здесь прогон 333 дошёл до git-шага (комментарий в задаче — ПОСТОЯННЫЙ
    # след), а в топ-10 свежих прогонов его НЕТ — одни чужие новее.
    # Мутация: верни цикл по recent_runs (засчитывай только прогоны из окна)
    # — attempts станет 0 вместо 1, тест покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        # Десять ЧУЖИХ прогонов новее попытки 333 — ровно тот сдвиг окна,
        # на котором старая логика теряла засчитанную попытку.
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            {"id": 1000 + i, "conclusion": "success", "created_at": f"2026-09-07T0{i % 10}:00:00Z"}
            for i in range(10)
        ]},
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T00:05:00Z", "body": "Канал: worker run 333."},
            {"created_at": "2026-09-06T00:10:00Z", "body": "🤖 [worker: git-шаг] worker run 333"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 1
    # Прогоны Actions счётчиком вообще не читаются: постоянная сторона —
    # комментарии задачи, окно в решении не участвует.
    assert not any("runs?per_page=10" in c for c in fake.calls)


def test_conflict_rework_attempts_counts_distinct_runs_not_marker_repeats(monkeypatch):
    # Один прогон, отметившийся дважды (например, повтор шага после
    # best-effort провала постановки комментария) — ОДНА попытка, не две:
    # бюджет РОВНО одна попытка на PR, дубликат следа не должен её удвоить.
    # Мутация: замени set на счётчик всех совпавших комментариев — attempts
    # стал бы 2, тест покраснеет.
    fake = FakeGh({
        "issues/560/timeline?per_page=100": [
            {"event": "labeled", "label": {"name": "conflict"}, "created_at": "2026-09-05T00:00:00Z"},
        ],
        f"{REPO}/issues/474/comments?per_page": [
            {"created_at": "2026-09-06T00:10:00Z", "body": "🤖 [worker: git-шаг] worker run 555"},
            {"created_at": "2026-09-06T00:20:00Z", "body": "🤖 [worker: git-шаг] worker run 555"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 1


def test_conflict_rework_attempts_zero_when_never_labeled(monkeypatch):
    fake = FakeGh({"issues/560/timeline?per_page=100": []})
    patch_gh(monkeypatch, fake)
    assert sch.conflict_rework_attempts(REPO, 560, 474) == 0
    # Не тратим вызовы на прогоны/комментарии задачи — нет эпизода конфликта,
    # решать по нему нечего.
    assert not any("runs?per_page=10" in c or "474/comments" in c for c in fake.calls)


def test_worker_git_step_marker_literal_in_task_sh_matches_pulse_guard():
    # Блокирующая находка ревью PR #597: строка отметки git-шага захардкожена
    # в scripts/worker/task.sh, а WORKER_GIT_STEP_MARKER объявлен в
    # pulse_guard.py — синхронизация держалась только комментарием «держи
    # текст в синхроне». Тесты сторонили лишь сторону планировщика, поэтому
    # переформулировка в task.sh проходила CI зелёной, а отказ был тихим:
    # attempts навсегда 0, бюджет не сгорает никогда, эскалация не наступает.
    # Гвардия по исходнику (класс #259/#308): текст отметки обязан быть РОВНО
    # таким, каким его ищет conflict_rework_attempts, — и обязан строиться в
    # проверке из той же константы (переименование в pulse_guard тоже красит
    # этот тест). Мутация: переформулируй --body в task.sh (например, убери
    # «worker run») — тест покраснеет.
    task_sh = (Path(__file__).resolve().parent.parent / "worker" / "task.sh").read_text(encoding="utf-8")
    expected = f'🤖 {sch.WORKER_GIT_STEP_MARKER} worker run ${{GITHUB_RUN_ID:-local}}'
    assert expected in task_sh, (
        "scripts/worker/task.sh потерял точный текст отметки git-шага "
        f"({expected!r}) — conflict_rework_attempts перестанет засчитывать "
        "попытки, бюджет конфликта не будет сгорать никогда (тихий отказ)"
    )


def test_conflict_overlap_hint_intersects_pr_and_main_changed_files(monkeypatch):
    p = pull(560, base_sha="basesha")
    fake = FakeGh({
        "pulls/560/files": files_payload(["shared.py", "only_pr.py"]),
        "compare/basesha...main": {"files": files_payload(["shared.py", "only_main.py"])},
    })
    patch_gh(monkeypatch, fake)
    assert sch.conflict_overlap_hint(REPO, p) == "shared.py"


def test_conflict_overlap_hint_empty_when_no_base_sha():
    p = pull(560)  # base_sha не передан — прод-форма без base тоже возможна (частичный ответ API)
    assert sch.conflict_overlap_hint(REPO, p) == ""


def test_conflict_overlap_hint_empty_on_api_failure_not_silent_wrong(monkeypatch):
    # Сбой API — пустая строка, а не «пересечения нет» (AGENTS.md: «не знаешь —
    # пиши «не подтверждено»»); вызывающий код (эскалация) обязан различить это
    # в тексте ("не удалось определить"), сама функция только возвращает "".
    p = pull(560, base_sha="basesha")
    fake = FakeGh({"pulls/560/files": RuntimeError("gh api repos/o/r/pulls/560/files: HTTP 502")})
    patch_gh(monkeypatch, fake)
    assert sch.conflict_overlap_hint(REPO, p) == ""


def test_main_skips_generic_worker_dispatch_when_conflict_rework_already_dispatched(monkeypatch):
    """#474: расшивка конфликта и обычный dispatch_worker не должны дать ДВА
    workflow_dispatch worker.yml за один проход main() — "ровно один за
    пульс" (докстринг модуля, п.4). conveyor_gate открыт, но
    dispatch_conflict_rework этим проходом уже дёрнул worker.yml — обычный
    dispatch_worker обязан промолчать."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "merged_pr_map", lambda pulls: {})
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], [], False, pulls))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "accept_merged_tasks",
                         lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "mark_stale_unclaimed", lambda repo, now, pool: [])
    # Детектор простоя (#201) — не предмет этого теста, но main() зовёт его
    # безусловно; без мока непатченный detect_and_act/escalate_stale_auto_tasks
    # бьёт по настоящему gh (нашла CI, не локальный прогон — токен раннера уже
    # PAT/GITHUB_TOKEN, а personal auth разработчика молча делал вызов
    # безобидным локально).
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    monkeypatch.setattr(
        sch, "dispatch_conflict_rework",
        lambda repo, pulls, *, pool: (["конфликт расшит"], ["🔧 расшивка ушла"], True),
    )
    dispatched = []
    monkeypatch.setattr(
        sch, "dispatch_worker",
        lambda repo, pool: dispatched.append((repo, pool)) or (["не должно быть вызвано"], []),
    )
    monkeypatch.setattr(sch, "summary", lambda lines: None)

    assert sch.main() == 0
    assert dispatched == []


# ── Пагинация файлов PR: третье место того же класса (находка вердикта на
# PR #294) — after_merge читал сырую первую страницу, теперь через общий
# review_labels.list_pr_files, как check_pr.py и ai_review.py ───────────────


def test_after_merge_reads_files_through_paginated_helper():
    # Гвардия по исходнику: after_merge обязан ходить через
    # review_labels.list_pr_files (общее место с check_pr.py/ai_review.py),
    # а не читать сырую первую страницу gh(...pulls/{number}/files?per_page=100)
    # — эта форма молча теряла файлы за сотым (PR за сотню файлов с
    # cf-worker/* в хвосте не запускал бы deploy-worker.yml).
    source = SCRIPT.read_text(encoding="utf-8")
    assert "review_labels.list_pr_files(repo, number, gh)" in source
    assert 'gh(f"repos/{repo}/pulls/{number}/files?per_page=100")' not in source


# ── Якорь таймеров #196/#269: commit status, не таймлайн-событие (находка
# ревью #424) — last_gate1_labeled_at/last_ready_labeled_at раньше читали
# 'labeled' в таймлайне (#303: полная пагинация вместо сырой первой страницы),
# но #203 сделал перестановку вердикт-меток идемпотентной — 'labeled' не
# выбрасывается вовсе, если новый пуш подтвердил тот же вердикт, и якорь
# замерзал бы навсегда. Оба якоря переведены на review_labels.status_posted_at
# (commit status на текущем head, публикуется каждым прогоном безусловно) ──


def test_last_gate1_and_last_ready_read_status_not_timeline():
    # Гвардия по исходнику: обе функции обязаны ходить через
    # review_labels.status_posted_at, не через список 'labeled' в таймлайне —
    # поведенческие тесты ниже доказывают саму мутацию (пуш с тем же
    # вердиктом всё равно двигает якорь вперёд).
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("review_labels.status_posted_at(") >= 3  # оба якоря + trigger_ai_review не читает лишний раз
    assert "def last_gate1_labeled_at(repo: str, pull: dict)" in source
    assert "def last_ready_labeled_at(repo: str, pull: dict)" in source


def test_last_gate1_labeled_at_mutation_push_with_same_verdict_moves_anchor(monkeypatch):
    # Находка ревью #424: пуш с ТЕМ ЖЕ вердиктом не выбрасывает 'labeled'
    # (идемпотентность #203), но commit status публикуется безусловно на
    # НОВОМ head — якорь обязан сдвинуться вперёд вместе с пушем, а не
    # замереть на первой простановке метки.
    p = {"head": {"sha": "sha-after-second-push"}}
    fake = FakeGh({
        "commits/sha-after-second-push/statuses": [
            {"context": sch.review_labels.STATUS_REVIEW, "created_at": "2026-09-02T11:00:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    result = sch.last_gate1_labeled_at(REPO, p)
    assert result == utc(2026, 9, 2, 11, 0), (
        "якорь обязан отражать статус ТЕКУЩЕГО head, даже если вердикт не "
        "изменился и метка не переставилась"
    )


def test_last_gate1_labeled_at_none_when_status_never_posted(monkeypatch):
    fake = FakeGh({"commits/nostatussha/statuses": []})
    patch_gh(monkeypatch, fake)
    assert sch.last_gate1_labeled_at(REPO, {"head": {"sha": "nostatussha"}}) is None


def test_last_ready_labeled_at_uses_later_of_both_statuses_on_current_head(monkeypatch):
    p = {"head": {"sha": "readysha"}}
    fake = FakeGh({
        "commits/readysha/statuses": [
            {"context": sch.review_labels.STATUS_REVIEW, "created_at": "2026-09-01T00:00:00Z"},
            {"context": sch.review_labels.STATUS_AI_REVIEW, "created_at": "2026-09-01T01:00:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    result = sch.last_ready_labeled_at(REPO, p)
    assert result == utc(2026, 9, 1, 1, 0)  # позже из двух


def test_last_ready_labeled_at_none_when_either_status_missing(monkeypatch):
    p = {"head": {"sha": "onlyreviewsha"}}
    fake = FakeGh({
        "commits/onlyreviewsha/statuses": [
            {"context": sch.review_labels.STATUS_REVIEW, "created_at": "2026-09-01T00:00:00Z"},
        ],
    })
    patch_gh(monkeypatch, fake)
    assert sch.last_ready_labeled_at(REPO, p) is None


def test_conflict_labeled_at_reads_timeline_through_paginated_helper():
    # Гвардия по исходнику (тот же приём, что для after_merge/list_pr_files
    # выше): функции-читатели таймлайна конфликта (conflict_labeled_at и
    # conflict_first_labeled_at, #588) обязаны ходить через
    # review_labels.list_timeline (полный обход постранично), а не читать
    # сырую первую страницу — поведенческая проверка самой пагинации живёт в
    # scripts/lib/test_review_labels.py::test_list_timeline_paginates_finds_event_beyond_first_page
    # (мутация доказана там: обход убран — тест краснеет). Сведение
    # с main (#424): last_gate1/last_ready переведены на commit status
    # (review_labels.status_posted_at) и таймлайн больше не читают — их
    # гвардии живут в соседних тестах выше.
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("review_labels.list_timeline(repo, pr_number, gh)") == 2
    assert 'gh(f"repos/{repo}/issues/{pr_number}/timeline?per_page=100")' not in source


# ── Пагинация пула задач/PR/таймлайна reap_stale: активный дефект в проде
# (#308-класс): open_task_issues/open_pulls читали сырую первую страницу
# `per_page=100` без обхода — при 106 открытых задачах с меткой task (107
# сырых записей issues на этой выборке; #248 сама PR под меткой task,
# отфильтровывается по ключу pull_request — замер 2026-09-05, живой
# репозиторий) хвост за первой сотней был невидим воркеру и планировщику
# без ошибки. reap_stale читал таймлайн issue той же сырой формой — тот же
# класс, что last_gate1_labeled_at/last_ready_labeled_at (#303), сюда не
# мигрировали. ─────────────────────────────────────────────────────────────


def test_open_task_issues_and_open_pulls_read_through_paginated_helper():
    # Гвардия по исходнику: обе функции обязаны ходить через
    # review_labels.list_pages (полный обход постранично), а не читать сырую
    # первую страницу — поведенческая проверка пагинации на прод-форме живёт в
    # scripts/lib/test_review_labels.py::test_list_pages_paginates_over_100_real_open_task_issues_310
    # (мутация доказана там: обход убран — тест краснеет).
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'review_labels.list_pages(\n        f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100", gh)' in source
    assert 'review_labels.list_pages(f"repos/{repo}/pulls?state=open&per_page=100", gh)' in source
    assert 'gh(f"repos/{repo}/issues?state=open&labels={TASK_LABEL}&per_page=100")' not in source
    assert 'gh(f"repos/{repo}/pulls?state=open&per_page=100")' not in source


def test_reap_stale_reads_timeline_through_paginated_helper():
    # reap_stale читал `gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")`
    # без обхода — тот же класс, что last_gate1_labeled_at/
    # last_ready_labeled_at (#303), не мигрировали сюда.
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("review_labels.list_timeline(repo, number, gh)") == 1
    assert 'gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")' not in source


def test_open_task_issues_finds_all_beyond_first_page_real_form(monkeypatch):
    # Поведенческое доказательство на уровне вызывающей функции open_task_issues
    # (не только текст исходника выше): реальный снимок 100+7 открытых issue с
    # меткой task (repos/mytab0r/edge-harness/issues?state=open&labels=task,
    # 2026-09-05) — до фикса (сырая первая страница) видны только 100, после —
    # все 107 сырых записей, но одна из них (#248) сама PR (несёт ключ
    # "pull_request", по которому open_task_issues и фильтрует) — то есть
    # настоящих задач 106, а #248 обязана быть отфильтрована.
    import json
    fixture = _DIR.parent / "lib" / "fixtures_open_task_issues_310.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    page1, page2 = data["page1"], data["page2"]
    assert len(page1) == 100
    assert len(page2) == 7

    def fake_gh(url: str):
        query = url.split("?", 1)[1] if "?" in url else ""
        params = dict(pair.split("=", 1) for pair in query.split("&") if "=" in pair)
        page = params.get("page")
        if page == "1":
            return page1
        if page == "2":
            return page2
        return []

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])  # серии нет — возобновление #220 не событие
    issues = sch.open_task_issues(REPO)
    numbers = [issue["number"] for issue in issues]
    # Мутация 1: верни только первую страницу (уберите обход в list_pages/
    # верните gh(...) без page=) — упадёт до 99 (page1 без #248), покраснеет.
    # Мутация 2: сотри фильтр "pull_request" not in issue в open_task_issues —
    # #248 вернётся в список, число снова станет 107, покраснеет.
    assert len(issues) == 106
    assert 248 not in numbers  # #248 — настоящий PR (несёт "pull_request"), не задача
    assert 86 in numbers  # последняя запись второй страницы — обход не потерял хвост


# ── Мутация гвардии поведения 3: без вызова update-branch список пуст ────────────


def test_after_merge_promises_auto_close_only_for_own_branch_task(monkeypatch):
    """Находка AI-ревью PR #253: accept_merged_tasks видит только задачу ВЕТКИ
    (merged_pr_map строится из task_ref.resolve_pr_task) — обещание «приёмка
    закроет её сама» для ЛЮБОГО упоминания в прозе было ложным (задача,
    упомянутая не веткой, не в карте приёмки, и через
    ACCEPTANCE_PENDING_HOURS reap_stale снял бы с неё назначение с ложной
    причиной «PR не появился»). #78 — задача ветки — получает обещание
    автозакрытия; #79 просто упомянута в прозе — получает предупреждение
    завести PR на ветке agent/<N>-<slug>."""
    body = "#78\n\nОсновная реализация. Заодно поправил соседний баг из #79."
    merged = pull(163, pr_body=body, ref="agent/78-dsh-edge")
    posted = {}

    def fake_gh(*args):
        joined = " ".join(args)
        if joined == "repos/o/r/pulls/163/files?per_page=100&page=1":
            return []
        if joined == "repos/o/r/pulls/163/files?per_page=100&page=2":
            return []
        if joined == "repos/o/r/issues/78":
            return {**issue(78, assignees=("mytab0r",)), "state": "open"}
        if joined == "repos/o/r/issues/79":
            return {**issue(79, assignees=("mytab0r",)), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/78/comments"):
            posted[78] = args[-1].removeprefix("body=")
            return None
        if joined.startswith("-X POST repos/o/r/issues/79/comments"):
            posted[79] = args[-1].removeprefix("body=")
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])  # серии нет — возобновление #220 не событие
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    sch.after_merge("o/r", merged, [])

    assert "стадия приёмки закроет её сама" in posted[78]
    assert "не назвал её именем ветки" in posted[79]
    assert "стадия приёмки закроет её сама" not in posted[79]


def test_after_merge_notifies_telegram_about_merge_once(monkeypatch):
    """#170: слияние в main — единственный факт, на который Telegram говорит
    «выполнена»; раньше этот канал молчал вовсе, и владелец узнавал о готовности
    только руками. Один PR = ОДНО сообщение, даже когда ветка называет #78 и
    рядом в теле упомянута #79 (дубль на каждую задачу — спам). Номера задачи и
    PR — кликабельные <a>-ссылки, заголовок задачи экранирован (мержится как
    HTML)."""
    body = "#78\n\nОсновная реализация. Заодно поправил соседний баг из #79."
    merged = pull(163, pr_body=body, ref="agent/78-dsh-edge")
    sent = []

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/163/files?per_page=100&page=1",
                      "repos/o/r/pulls/163/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/78":
            return {**issue(78, assignees=("mytab0r",), title="Отчёт врёт & <молчит>"),
                    "state": "open"}
        if joined == "repos/o/r/issues/79":
            return {**issue(79, assignees=("mytab0r",), title="Другая задача"), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])  # серии нет — возобновление #220 не событие
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))
    monkeypatch.setattr(
        sch, "send_telegram",
        lambda text, as_html=False: sent.append((text, as_html)) or True)

    observations, actions, hard_failure = sch.after_merge("o/r", merged, [])

    assert len(sent) == 1, "один PR — одно сообщение, а не по одному на задачу"
    text, as_html = sent[0]
    assert as_html is True                                # без parse_mode ссылки мертвы
    assert "слито в main" in text
    assert '<a href="https://github.com/o/r/issues/78">#78</a>' in text
    assert '<a href="https://github.com/o/r/pull/163">#163</a>' in text
    assert "Отчёт врёт &amp; &lt;молчит&gt;" in text      # заголовок ушёл экранированным
    assert hard_failure is False
    assert any("Telegram" in line and "доставлено" in line for line in (observations + actions))


def test_after_merge_announces_only_own_branch_task_not_prose_mentions(monkeypatch):
    """#404 (ревью PR #402), переведено на #394 (решение владельца 2026-09-06):
    «выполнена» вправе звучать только о задаче ВЕТКИ PR. Упомянутая в прозе
    более старая открытая задача (#78 идёт раньше #79 в сортировке упоминаний)
    не перебивает задачу ветки: канал не говорит «#78 выполнена» про задачу,
    которую этот PR не делал и которую приёмка (#227) не закроет. Мутация:
    вернуть захват first_task до сравнения с own_task — тест краснеет."""
    body = "#79\n\nОсновная реализация. Заодно поправил соседний баг из #78."
    merged = pull(163, pr_body=body, ref="agent/79-second-fix")
    sent = []

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/163/files?per_page=100&page=1",
                      "repos/o/r/pulls/163/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/78":
            return {**issue(78, assignees=("mytab0r",), title="Старая задача из прозы"),
                    "state": "open"}
        if joined == "repos/o/r/issues/79":
            return {**issue(79, assignees=("mytab0r",), title="Настоящая задача"), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))
    monkeypatch.setattr(
        sch, "send_telegram",
        lambda text, as_html=False: sent.append((text, as_html)) or True)

    sch.after_merge("o/r", merged, [])

    assert len(sent) == 1
    text = sent[0][0]
    assert '<a href="https://github.com/o/r/issues/79">#79</a>' in text
    assert "Настоящая задача" in text
    assert "#78" not in text and "Старая задача из прозы" not in text


def test_after_merge_without_own_branch_task_sends_nothing(monkeypatch):
    """#404, переведено на #394: PR без agent-ветки (задача упомянута только в
    прозе) не порождает «задача #N выполнена» вовсе — нечего announcing,
    приёмка такую задачу всё равно не закроет."""
    body = "Попутно задел соседний баг из #78."
    merged = pull(163, pr_body=body)
    sent = []

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/163/files?per_page=100&page=1",
                      "repos/o/r/pulls/163/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/78":
            return {**issue(78, assignees=("mytab0r",), title="Старая задача из прозы"),
                    "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch, "recent_runs", lambda *a, **k: [])  # серии нет — возобновление #220 не событие
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))
    monkeypatch.setattr(
        sch, "send_telegram",
        lambda text, as_html=False: sent.append(text) or True)

    observations, actions, hard_failure = sch.after_merge("o/r", merged, [])

    assert sent == [], "нет объявленной задачи — нет и «выполнена» в канале"
    assert hard_failure is False
    assert not any("Telegram" in line and "доставлено" in line for line in (observations + actions))


def test_after_merge_telegram_miss_is_loud_but_not_fatal(monkeypatch):
    """#170: недоставленный Telegram не откатывает мерж и не роняет after_merge —
    место правды (комментарий в задаче выше) уже оставлен; но и не молчит: ⚠️ в отчёте."""
    body = "#78\n\nОсновная реализация."
    merged = pull(163, pr_body=body, ref="agent/78-dsh-edge")

    def fake_gh(*args):
        joined = " ".join(args)
        if joined in ("repos/o/r/pulls/163/files?per_page=100&page=1",
                      "repos/o/r/pulls/163/files?per_page=100&page=2"):
            return []
        if joined == "repos/o/r/issues/78":
            return {**issue(78, assignees=("mytab0r",), title="Любой заголовок"), "state": "open"}
        if joined.startswith("-X POST repos/o/r/issues/") and "/comments" in joined:
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    monkeypatch.setattr(sch, "gh", fake_gh)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))
    monkeypatch.setattr(sch, "send_telegram", lambda text, as_html=False: False)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda repo, merged_number, others: ([], []))

    observations, actions, hard_failure = sch.after_merge("o/r", merged, [])

    assert hard_failure is False
    assert any("⚠️" in line and "Telegram" in line for line in (observations + actions))


def test_after_merge_wires_update_remaining_pulls(monkeypatch):
    # after_merge обязан прокинуть other_pulls в update_remaining_pulls — иначе
    # поведение 3 реализовано, но не вызывается ниоткуда (мертвый код).
    merged = pull(1)
    other = pull(2)
    monkeypatch.setattr(sch, "gh", FakeGh({"pulls/1/files": []}))
    calls = []
    monkeypatch.setattr(sch, "update_remaining_pulls",
                         lambda repo, merged_number, others: calls.append((merged_number, others)) or ([], []))
    sch.after_merge(REPO, merged, [other])
    assert calls == [(1, [other])]


# ── Канал обновления морды (стройка 3 эпика #77, задача #374) ────────────────
# Мерж правки dsh-edge/** (манифест plugin-forge, патч-серия, пин апстрима)
# обязан диспатчить deploy-dsh-edge.yml СРАЗУ после мержа. Класс тот же, что у
# cf-worker → deploy-worker: мерж через GITHUB_TOKEN push-события не создаёт
# (защита GitHub от рекурсии), триггером канал не держится; крон — страховка
# на сутки, а не канал («морда перезапускается с плагином» — минуты, не сутки).


def test_after_merge_dispatches_dsh_edge_deploy(monkeypatch):
    merged = pull(77)
    dispatches = []

    def fake_run(cmd, **_kwargs):
        dispatches.append(cmd)
        return None

    monkeypatch.setattr(sch, "gh", FakeGh({
        "pulls/77/files?per_page=100&page=1": [{"filename": "dsh-edge/plugins.json"}],
        "pulls/77/files?per_page=100&page=2": [],
    }))
    monkeypatch.setattr(sch.subprocess, "run", fake_run)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda repo, merged_number, others: ([], []))
    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert ["gh", "workflow", "run", "deploy-dsh-edge.yml", "--ref", "main"] in dispatches
    assert any("deploy-dsh-edge запущен" in line for line in (observations + actions))


def test_after_merge_skips_dsh_edge_deploy_for_other_paths(monkeypatch):
    merged = pull(78)
    dispatches = []

    def fake_run(cmd, **_kwargs):
        dispatches.append(cmd)
        return None

    monkeypatch.setattr(sch, "gh", FakeGh({
        "pulls/78/files?per_page=100&page=1": [{"filename": "scripts/orchestra/scheduler.py"}],
        "pulls/78/files?per_page=100&page=2": [],
    }))
    monkeypatch.setattr(sch.subprocess, "run", fake_run)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda repo, merged_number, others: ([], []))
    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert dispatches == []
    assert not any("deploy-dsh-edge" in line for line in (observations + actions))


def test_deploy_on_merge_class_covers_both_deployables():
    # Гвардия класса (один хелпер на оба деплоя): снимать диспатч cf-worker или
    # dsh-edge из after_merge — красный тест; новый деплой-таргет добавляется
    # строкой того же вида, а не своей копией subprocess.run.
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'dispatch_deploy_on_merge(files, "cf-worker/", "deploy-worker.yml")' in source
    assert 'dispatch_deploy_on_merge(files, "dsh-edge/", "deploy-dsh-edge.yml")' in source
    # В after_merge не осталось инлайновых копий диспатча мимо хелпера: старая
    # копия была `subprocess.run(` + `["gh", "workflow", "run"` с отступом
    # 12 пробелов (внутри if); у хелпера — 4/8. Снятие любого из двух вызовов
    # хелпера выше красит этот тест, новая копия — последний assert.
    assert 'subprocess.run(\n            ["gh", "workflow", "run"' not in source


# ── Чеклист некритичных замечаний ревью — задача-хвост при слиянии (#462) ─────
# Незакрытые пункты НЕ блокируют мерж (иначе некритичное стало бы критичным),
# но и не теряются молча — одна задача-хвост со ссылкой на PR, не issue на
# каждый пункт (scripts/lib/review_checklist.py).

def _checklist_body(*, unchecked=("Не сделано",), checked=()):
    lines = [sch.review_checklist.CHECKLIST_BEGIN, sch.review_checklist.CHECKLIST_TITLE, ""]
    lines += [f"- [ ] **{t}**" for t in unchecked]
    lines += [f"- [x] **{t}**" for t in checked]
    lines.append(sch.review_checklist.CHECKLIST_END)
    return "Описание PR.\n\n" + "\n".join(lines) + "\n"


def test_after_merge_files_tail_issue_for_unresolved_checklist(monkeypatch):
    merged = pull(163, pr_body=_checklist_body(unchecked=("Первое", "Второе"), checked=("Третье",)))
    fake = FakeGh({
        "pulls/163/files": [],
        "issues?state=open&labels=task&per_page=100": [],
        f"-X POST repos/{REPO}/issues -f title={sch.review_checklist.tail_issue_title(163)}": {"number": 900},
    })
    monkeypatch.setattr(sch, "gh", fake)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda *a, **k: ([], []))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert any("#900" in line and "2" in line for line in actions)
    posts = [c for c in fake.calls if c.startswith("-X POST") and f"repos/{REPO}/issues " in c]
    assert len(posts) == 1
    assert "Первое" in posts[0] and "Второе" in posts[0]
    assert "Третье" not in posts[0]   # отмеченный пункт не попадает в хвост


def test_after_merge_no_checklist_no_tail_issue(monkeypatch):
    # Тело PR без секции чеклиста вовсе — unresolved_items пуст, ни списка
    # открытых задач, ни POST issue не запрашивается (мутация: FakeGh упал бы
    # AssertionError на непредусмотренном маршруте, если бы код всё равно лез
    # в сеть).
    merged = pull(163, pr_body="Обычное описание без чеклиста.")
    fake = FakeGh({"pulls/163/files": []})
    monkeypatch.setattr(sch, "gh", fake)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda *a, **k: ([], []))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("хвост чеклиста" in line for line in observations + actions)


def test_after_merge_tail_issue_idempotent_by_title(monkeypatch):
    # Хвост уже заведён (открытая задача с тем же заголовком) — повторный
    # прогон after_merge не плодит вторую issue.
    merged = pull(163, pr_body=_checklist_body(unchecked=("Первое",)))
    existing = {"title": sch.review_checklist.tail_issue_title(163), "number": 501}
    fake = FakeGh({
        "pulls/163/files": [],
        "issues?state=open&labels=task&per_page=100": [existing],
    })
    monkeypatch.setattr(sch, "gh", fake)
    monkeypatch.setattr(sch, "update_remaining_pulls", lambda *a, **k: ([], []))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    posts = [c for c in fake.calls if c.startswith("-X POST") and f"repos/{REPO}/issues " in c]
    assert posts == []
    assert any("уже заведена" in line for line in observations)


# ── Авто-возобновление предохранителя по мержу (#220) ────────────────────────────
# Прод-форма снята живым API 2026-09-06: worker.yml run 34011108934 (failure,
# 04:17:03Z) — реальный красный прогон; его след аренды в #217 — реальный
# комментарий claim_task; merged_at 04:40:56Z — реальный squash-мерж PR #445.
# Прогон и его заголовок («worker», PR-номера не несёт) — как отдаёт GitHub.


RESUME_RED_RUNS = {"workflow_runs": [
    {"id": 34011108934, "conclusion": "failure", "created_at": "2026-09-06T04:17:03Z",
     "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/34011108934",
     "display_title": "worker"},
    {"id": 34011017990, "conclusion": "success", "created_at": "2026-09-06T04:14:57Z",
     "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/34011017990",
     "display_title": "worker"},
]}
# Реально закрытая серия из живой истории worker.yml (2026-09-06): зелёная
# проба 34007871665 (02:59:58Z) новее красного 34007508064 (02:50:34Z) —
# порядок как отдаёт GitHub, от нового к старому.
RESUME_CLOSED_RUNS = {"workflow_runs": [
    {"id": 34007871665, "conclusion": "success", "created_at": "2026-09-06T02:59:58Z",
     "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/34007871665",
     "display_title": "worker"},
    {"id": 34007508064, "conclusion": "failure", "created_at": "2026-09-06T02:50:34Z",
     "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/34007508064",
     "display_title": "worker"},
]}
# Реальный след аренды (#217, комментарий claim_task.claim через task.sh):
CLAIM_TRACE_BODY = ("🔒 Аренда задачи: `mytab0r` держит замок `refs/locks/task-217` "
                    "(TTL 24 ч по коммиту замка). Канал: worker run 34011108934.")


def resume_pull(number=163, task=217):
    return pull(number, pr_body=f"#{task}\n\nРеализация.",
                ref=f"agent/{task}-slug")


def test_after_merge_resume_series_by_merge_posts_marker(monkeypatch):
    """#220, основной сценарий: слит PR задачи ветки (#217), последний красный
    прогон worker.yml работал над ней (след аренды в комментариях задачи),
    мерж новее прогона — серия сбрасывается success-маркером в #120 через
    escalate, в отчёте названа причина. Мутация: выкинуть вызов
    resume_series_by_merge из after_merge — тест краснеет (escalate не звался).
    Мутация 2: в resume_series_by_merge убрать проверку следа — после
    test_after_merge_resume_skips_without_claim_trace краснеет."""
    merged = resume_pull()
    # #120 — живой носитель: escalate зовётся НАСТОЯЩИЙ, постинг пишет в store,
    # чтение дедупа и перечитывания факта сброса видят ровно то, что в нём.
    store = []

    def fake_gh(*args):
        joined = " ".join(args)
        # порядок важен: FakeGh матчит по подстроке, более частный фрагмент — раньше
        if joined.startswith(f"-X POST repos/{REPO}/issues/120/comments"):
            store.append({"created_at": "2026-09-06T04:41:00Z",
                          "body": args[-1].removeprefix("body=")})
            return None
        if joined == f"repos/{REPO}/issues/120/comments?per_page=100&page=1":
            return list(store)
        if joined.startswith(f"-X POST repos/{REPO}/issues/217/comments"):
            return None  # напоминание о пост-мерж проверке
        if joined in (
                f"repos/{REPO}/pulls/163/files?per_page=100&page=1",
                f"repos/{REPO}/pulls/163/files?per_page=100&page=2"):
            return []
        if joined == f"repos/{REPO}/pulls/163":
            return {"number": 163, "merged_at": "2026-09-06T04:40:56Z"}
        if joined == f"repos/{REPO}/issues/217/comments?per_page=100&page=1":
            return [{"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}]
        if joined == f"repos/{REPO}/issues/217":
            return {**issue(217, assignees=("mytab0r",)), "state": "open"}
        if joined == f"repos/{REPO}/actions/workflows/worker.yml/runs?per_page=10":
            return RESUME_RED_RUNS
        raise AssertionError(f"нет маршрута для: {joined}")

    patch_gh(monkeypatch, fake_gh)
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)  # канал escalate
    monkeypatch.setattr(sch, "send_telegram", lambda text, as_html=False: True)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    # видимый результат: маркер реально лежит в #120 с нужным содержимым
    assert len(store) == 1
    assert f"{pg.RESUME_MARKER} #163]" in store[0]["body"]     # токен для gate и дедупа
    assert "#217" in store[0]["body"] and "#163" in store[0]["body"]
    assert any("сброшена мержем #163" in line for line in actions + observations)
    assert any("Telegram: доставлен" in line for line in actions + observations)


def test_after_merge_resume_skips_when_merge_predates_red_run(monkeypatch):
    """Красный ПОСЛЕ мержа — довод «причина жива»: прогон уже видел фикс и всё
    равно упал, возобновлять мерж права не даёт. Мутация: убрать проверку
    merged_at — тест краснеет (escalate зовётся)."""
    merged = resume_pull()
    fake = FakeGh({
        f"{REPO}/pulls/163/files": [],
        f"repos/{REPO}/pulls/163": {"number": 163, "merged_at": "2026-09-06T04:00:00Z"},
        f"{REPO}/actions/workflows/worker.yml/runs": RESUME_RED_RUNS,
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}],
        f"-X POST repos/{REPO}/issues/217/comments": None,
        f"repos/{REPO}/issues/217": {**issue(217, assignees=("mytab0r",)), "state": "open"},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("мерж старше красного прогона — сброса быть не должно"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("сброшена мержем" in line for line in actions + observations)


def test_after_merge_resume_skips_when_series_closed(monkeypatch):
    """Свежий зелёный прогон новее красного — серия закрыта, возобновлять
    нечего: ни escalate, ни строки в отчёте."""
    merged = resume_pull()
    fake = FakeGh({
        f"{REPO}/pulls/163/files": [],
        f"repos/{REPO}/pulls/163": {"number": 163, "merged_at": "2026-09-06T04:40:56Z"},
        f"{REPO}/actions/workflows/worker.yml/runs": RESUME_CLOSED_RUNS,
        # след в формате claim_task про КРАСНЫЙ прогон этой серии: единственным
        # несработавшим барьером остаётся проверка серии (зелёный новее красного)
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T02:51:00Z",
             "body": "🔒 Аренда задачи: `mytab0r` держит замок `refs/locks/task-217` "
                     "(TTL 24 ч по коммиту замка). Канал: worker run 34007508064."}],
        f"-X POST repos/{REPO}/issues/217/comments": None,
        f"repos/{REPO}/issues/217": {**issue(217, assignees=("mytab0r",)), "state": "open"},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("серия закрыта зелёным — сброса быть не должно"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("сброшена мержем" in line for line in actions + observations)


def test_after_merge_resume_skips_without_claim_trace(monkeypatch):
    """Последний красный прогон работал над другой задачей (следа аренды в
    комментариях задачи нет) — связь «мерж чинил причину» не доказана, сброс
    был бы ложным. Мутация: убрать проверку следа — тест краснеет."""
    merged = resume_pull()
    fake = FakeGh({
        f"{REPO}/pulls/163/files": [],
        f"repos/{REPO}/pulls/163": {"number": 163, "merged_at": "2026-09-06T04:40:56Z"},
        # комментарии задач — раньше одиночного issue: фрагмент частнее, иначе
        # GET комментариев уйдёт на маршрут одиночного issue и тест солжёт
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z",
             "body": "🔒 Аренда задачи: `mytab0r` держит замок `refs/locks/task-217`. Канал: hands."}],
        f"-X POST repos/{REPO}/issues/217/comments": None,
        f"repos/{REPO}/issues/217": {**issue(217, assignees=("mytab0r",)), "state": "open"},
        f"{REPO}/actions/workflows/worker.yml/runs": RESUME_RED_RUNS,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("следа аренды нет — сброса быть не должно"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("сброшена мержем" in line for line in actions + observations)


def test_after_merge_resume_dedupes_by_pr_marker(monkeypatch):
    """Один сигнал на мерж: маркер возобновления этого PR уже стоит в #120 —
    второй после перезапуска оркестратора не пишется."""
    merged = resume_pull()
    fake = FakeGh({
        f"{REPO}/pulls/163/files": [],
        f"repos/{REPO}/pulls/163": {"number": 163, "merged_at": "2026-09-06T04:40:56Z"},
        # комментарии задач — раньше одиночного issue: фрагмент частнее
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}],
        f"{REPO}/issues/120/comments?per_page": [
            {"created_at": "2026-09-06T04:41:00Z",
             "body": pg.resume_alert_text(163, 217, None)}],
        f"-X POST repos/{REPO}/issues/217/comments": None,
        f"repos/{REPO}/issues/217": {**issue(217, assignees=("mytab0r",)), "state": "open"},
        f"{REPO}/actions/workflows/worker.yml/runs": RESUME_RED_RUNS,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("сброс этим мержем уже сигналился"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("сброшена мержем" in line for line in actions + observations)


def test_after_merge_resume_does_not_claim_reset_when_marker_not_posted(monkeypatch):
    """Находка AI-ревью (класс #318): escalate глотает отказ постинга и
    возвращает «след в #120: НЕ оставлен» — серия при этом реально НЕ снята
    (гейт маркер не увидит). Отчёт не вправе утверждать «сброшена». Мутация:
    убрать проверку статуса в resume_series_by_merge — тест краснеет."""
    merged = resume_pull()
    # Постинг в #120 падает ПО-НАСТОЯЩЕМУ (RuntimeError от gh): реальный
    # escalate глотает отказ post_issue_comment — маркер не появляется.
    fake = FakeGh({
        f"{REPO}/pulls/163/files": [],
        f"repos/{REPO}/pulls/163": {"number": 163, "merged_at": "2026-09-06T04:40:56Z"},
        f"{REPO}/actions/workflows/worker.yml/runs": RESUME_RED_RUNS,
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}],
        f"-X POST repos/{REPO}/issues/217/comments": None,
        f"repos/{REPO}/issues/217": {**issue(217, assignees=("mytab0r",)), "state": "open"},
        f"-X POST repos/{REPO}/issues/120/comments": RuntimeError("500 постинг не прошёл"),
        f"{REPO}/issues/120/comments?per_page": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(pg, "send_telegram", lambda text: True)  # канал escalate
    monkeypatch.setattr(sch, "send_telegram", lambda text, as_html=False: True)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    monkeypatch.setattr(sch, "archive_runner_sessions", lambda numbers: ([], False))

    observations, actions, hard_failure = sch.after_merge(REPO, merged, [])

    assert hard_failure is False
    assert not any("сброшена мержем" in line for line in actions + observations), \
        "без маркера в #120 серия не снята — отчёт не утверждает сброс"
    assert any("НЕ снята" in line and "#163" in line for line in actions + observations)
    assert any("пробой (#205)" in line for line in actions + observations)  # газ возобновления назван


def test_run_claimed_task_matches_own_run_not_prefix(monkeypatch):
    """След сопоставляется с границей по цифре: «worker run 34011108934» —
    про прогон ...934, и подстрока «worker run 3401110893» не должна совпасть
    с чужим (более коротким) id. Мутация: убрать (?!\\d) — тест краснеет."""
    fake = FakeGh({
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}],
    })
    patch_gh(monkeypatch, fake)
    assert sch.run_claimed_task(REPO, 217, 34011108934) is True
    assert sch.run_claimed_task(REPO, 217, 3401110893) is False
    assert sch.run_claimed_task(REPO, 217, 999) is False


def test_last_worker_run_conclusion_returns_attributed_run(monkeypatch):
    # #478: dispatch_conflict_rework называет conclusion атрибутированного
    # прогона в тексте эскалации, не гадает — здесь проверяется сам источник
    # факта отдельно от сборки текста.
    fake = FakeGh({
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            # #217 упоминает только "worker run 34011108934" (CLAIM_TRACE_BODY) —
            # более новый прогон 999999 в этом же следе не найдётся, recent_runs
            # идёт новее→старше, поэтому цикл дойдёт до второго элемента.
            {"id": 999999, "conclusion": "cancelled"},
            {"id": 34011108934, "conclusion": "failure"},
        ]},
        f"{REPO}/issues/217/comments?per_page": [
            {"created_at": "2026-09-06T04:17:30Z", "body": CLAIM_TRACE_BODY}],
    })
    patch_gh(monkeypatch, fake)
    assert sch.last_worker_run_conclusion(REPO, 217) == "failure"


def test_last_worker_run_conclusion_none_when_unattributed(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": [
            {"id": 1, "conclusion": "success"},
        ]},
        f"{REPO}/issues/217/comments?per_page": [],
    })
    patch_gh(monkeypatch, fake)
    assert sch.last_worker_run_conclusion(REPO, 217) is None


def test_task_sh_composes_claim_via_worker_run_format():
    """Гвардия формата следа по исходнику: run_claimed_task читает «worker run
    <id>», а пишет его task.sh (CLAIM_VIA). Переименование формата в одном
    месте без другого обязано краснить этот тест, а не молча сломать
    эвристику #220."""
    task_sh = (Path(__file__).resolve().parents[1] / "worker" / "task.sh").read_text()
    assert 'CLAIM_VIA="worker run ${GITHUB_RUN_ID' in task_sh


# ── Гвардия холостого хода (критерий приёмки, пункт 4) ───────────────────────────
# Пустая очередь (нет PR, нет задач) — ни одного мутирующего вызова gh ни от
# одного из трёх поведений и от main() целиком. Это САМАЯ важная гвардия:
# без неё сбойный провайдер/пустой пул превращается в цикл, жгущий квоту
# ai-review каждые 15 минут (крон orchestra.yml) и лимит 500 dispatch/час
# GitHub (docs/research/21-github-actions.md).


def test_trigger_ai_review_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("пустая очередь — писать некуда"))
    observations, actions = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [])
    assert (observations + actions) == []
    assert fake.calls == []


def test_unhealthy_pulls_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("пустая очередь — писать некуда"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [], pool=[])
    assert lines == []
    assert fake.mutating_calls() == []
    assert fake.calls == []


def test_update_remaining_pulls_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    observations, actions = sch.update_remaining_pulls(REPO, 1, [])
    assert (observations + actions) == []
    assert fake.calls == []


# ── Пульс воркера (#89, состав п.3): свободная задача + простаивающий воркер →
# ровно один workflow_dispatch за прогон; занятый воркер и пул без свободных —
# ноль мутирующих вызовов; сбой диспатча — ⚠️ в отчёте, планировщик не роняется.
# Маршруты кормятся прод-формой ответов GitHub API (list workflow runs).


def workflow_run(run_id, status):
    """Прод-форма элемента workflow_runs (GitHub API, снята с прогонов
    worker.yml этого репозитория: id/status/conclusion/event/created_at)."""
    return {
        "id": run_id,
        "name": "worker",
        "node_id": "WFR_kwDOUHBaqc8AAAACyoZFEQ",
        "head_branch": "main",
        "head_sha": "035d8b6000000000000000000000000000000000",
        "run_number": 42,
        "event": "workflow_dispatch",
        "status": status,
        "conclusion": None,
        "created_at": "2026-09-03T22:42:31Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "display_title": "worker",
    }


def graphql_pool_response(issues_list):
    """Ответ `gh api graphql` (уже распарсенный JSON, как его отдаёт
    `_default_gh`/`gh(*args)`) на запрос `task_deps.fetch_pool` — один
    короткий page с нодами, собранными из тех же фикстур `issue()`, что и
    REST-пул: `assignees`/`labels` совместимы по форме (список словарей с
    `login`/`name`), blockedBy/blocking пусты по умолчанию (#361: приоритет
    отчёта dispatch_worker без явно заданного графа деградирует к номеру)."""
    nodes = [{
        "number": iss["number"],
        "title": iss.get("title", ""),
        "labels": {"nodes": iss.get("labels", [])},
        "assignees": {"nodes": iss.get("assignees", [])},
        "blockedBy": {"totalCount": 0, "nodes": []},
        "blocking": {"totalCount": 0, "nodes": []},
    } for iss in issues_list]
    return {"data": {"repository": {"issues": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }}}}


def test_dispatch_worker_fires_once_for_idle_worker_and_free_pool(monkeypatch):
    pool = [issue(95), issue(89, assignees=())]
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "graphql": graphql_pool_response(pool),
        # POST .../dispatches отвечает 204 без тела — прод-форма «успех» есть None.
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=True, pulls=[])
    assert (observations + actions) == [
        "👷 свободная задача #89 — worker.yml запущен "
        "(воркер сам назначится и откроет PR)"
    ]
    assert fake.mutating_calls() == [
        f"-X POST repos/{REPO}/actions/workflows/worker.yml/dispatches -f ref=main"
    ]


def test_dispatch_worker_names_oldest_free_task_like_worker_will_pick(monkeypatch):
    # Пул приходит от issues API по убыванию новизны: без сортировки отчёт
    # назвал бы #101, а воркер выберет старейшую свободную (oldest_free,
    # #245) — строка отчёта обязана называть ту задачу, которую реально возьмут.
    pool = [issue(101, assignees=()), issue(95), issue(89, assignees=())]
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "graphql": graphql_pool_response(pool),
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=True, pulls=[])
    assert (observations + actions)[0].startswith("👷 свободная задача #89 ")
    assert not any("#101" in line for line in (observations + actions))


def test_dispatch_worker_names_task_with_more_blocking_over_older_number(monkeypatch):
    # #361: приоритет отчёта — тот же, что реально использует task.sh, не
    # только номер. #101 блокирует 2 открытые задачи, #89 — ни одной: #101
    # обязана называться первой, хотя её номер больше (свежее).
    pool = [issue(101, assignees=()), issue(89, assignees=())]
    graphql_response = graphql_pool_response(pool)
    graphql_response["data"]["repository"]["issues"]["nodes"][0]["blocking"] = {
        "totalCount": 2,
        "nodes": [{"number": 500, "state": "OPEN"}, {"number": 501, "state": "OPEN"}],
    }
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "graphql": graphql_response,
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=True, pulls=[])
    assert (observations + actions)[0].startswith("👷 свободная задача #101 ")


def test_dispatch_worker_degrades_to_rest_pool_when_graph_unavailable(monkeypatch):
    # Сбой ДОПОЛНИТЕЛЬНОГО GraphQL-запроса (граф блокировок) не должен
    # останавливать сам dispatch — деградация к REST-пулу (номер), видимым
    # предупреждением, не молчанием и не падением всего пульса.
    pool = [issue(101, assignees=()), issue(89, assignees=())]
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "graphql": RuntimeError("gh: HTTP 401: Bad credentials"),
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=True, pulls=[])
    lines = observations + actions
    assert any("граф блокировок недоступен" in line for line in lines)
    assert any(line.startswith("👷 свободная задача #89 ") for line in lines)
    assert fake.mutating_calls() == [
        f"-X POST repos/{REPO}/actions/workflows/worker.yml/dispatches -f ref=main"
    ]


def test_dispatch_worker_silent_when_pool_has_no_free_task(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    assert sch.dispatch_worker(REPO, [issue(89)], wip_allowed=True, pulls=[]) == ([], [])
    assert fake.calls == []  # ноль вызовов вовсе: на занятый пул даже статусы не смотрим


def test_dispatch_worker_silent_while_worker_run_in_progress(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {
            "workflow_runs": [workflow_run(33814313381, "in_progress")]},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(
        REPO, [issue(89, assignees=())], wip_allowed=True, pulls=[])
    # #456: «воркер уже работает» ничего не меняет — наблюдение, не действие.
    assert observations == ["👷 воркер уже работает — dispatch не нужен"]
    assert actions == []
    assert fake.mutating_calls() == []


def test_dispatch_worker_silent_while_worker_queued(monkeypatch):
    # queued считается активным так же, как in_progress: concurrency worker
    # поставит второй прогон в очередь, и он выгорит только после текущего.
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {
            "workflow_runs": [workflow_run(33814313390, "queued")]},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(
        REPO, [issue(89, assignees=())], wip_allowed=True, pulls=[])
    # #456: «воркер уже работает» ничего не меняет — наблюдение, не действие.
    assert observations == ["👷 воркер уже работает — dispatch не нужен"]
    assert actions == []
    assert fake.mutating_calls() == []


def test_dispatch_worker_survives_dispatch_failure(monkeypatch):
    # Best-effort по построению: 403/сеть на диспатче не роняют планировщик —
    # слияния важнее подряда воркеру. Доведение функции до возврата и есть
    # проверка: исключение ушло бы дальше этого assert.
    pool = [issue(89, assignees=())]
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "graphql": graphql_pool_response(pool),
        "workflows/worker.yml/dispatches": RuntimeError(
            "gh api repos/o/r/actions/workflows/worker.yml/dispatches: HTTP 403"),
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=True, pulls=[])
    assert len((observations + actions)) == 1 and (observations + actions)[0].startswith("⚠️ dispatch воркера не удался")


# ── dispatch_worker при закрытом WIP-гейте (#464, критическая находка ревью
# PR #466): гейт держит только НОВЫЕ задачи, доводка уже открытых PR обязана
# идти — иначе тормоз без газа (AGENTS.md), очередь не разгребается сама.


def test_dispatch_worker_targets_declared_pr_task_when_wip_gate_closed(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    # #95 — настоящая новая задача (нет PR); #89 — свободна, но её PR #500
    # ждёт доводки (unhealthy_pulls сняла исполнителя). Воркер обязан пойти
    # именно на #89, не на #95, хотя #89 не старейшая по номеру.
    pool = [issue(95, assignees=()), issue(89, assignees=())]
    pulls = [pull(500, ref="agent/89-fix-thing", labels=["ai:changes-requested"])]
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=False, pulls=pulls)
    assert (observations + actions) == [
        "👷 WIP-лимит закрыл новые задачи, но задача #89 уже с открытым PR — "
        "worker.yml запущен адресно на её доводку"
    ]
    assert fake.mutating_calls() == [
        f"-X POST repos/{REPO}/actions/workflows/worker.yml/dispatches "
        "-f ref=main -f inputs[task]=89"
    ]


def test_dispatch_worker_prefers_oldest_declared_pr_task_when_wip_gate_closed(monkeypatch):
    """Находка ревью PR #466 (блокирующая 1): из нескольких целей доводки
    берётся старейшая по номеру, но конфликтно-объявленная задача целью НЕ
    бывает — у неё свой цикл #474 (одна попытка ребейза с бюджетом по
    маркеру), и адресная ветка этот бюджет не пишет. #89 со `conflict`
    исключена, диспатч уходит на #101."""
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    pool = [issue(101, assignees=()), issue(89, assignees=())]
    pulls = [
        pull(500, ref="agent/101-later", labels=["ai:changes-requested"]),
        pull(501, ref="agent/89-earlier", labels=["conflict"]),
    ]
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=False, pulls=pulls)
    assert any("#101" in line for line in (observations + actions))
    assert not any("#89" in line for line in (observations + actions))


def test_dispatch_worker_blocks_new_task_when_wip_gate_closed_and_no_pr_yet(monkeypatch):
    # Единственная свободная задача не объявлена ни одним открытым PR — доводить
    # нечего, воркер не диспетчируется, но и не молчит (тормоз называет причину).
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    pool = [issue(95, assignees=())]
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=False, pulls=[])
    assert actions == []
    assert observations == [
        "⏸️ WIP-лимит закрыл новые задачи, и ни у одной свободной задачи ещё "
        "нет открытого PR — доводить нечего, dispatch не запущен"
    ]
    # gh api вызван для статусов воркера (worker_runs_active), но не для dispatches.
    assert not any("dispatches" in call for call in fake.mutating_calls())


def test_dispatch_worker_stays_silent_while_worker_active_even_when_rework_available(monkeypatch):
    # Активный прогон воркера блокирует диспатч раньше, чем эта функция вообще
    # смотрит на declared_pr_task_numbers — тот же порядок проверок, что и при
    # разрешённом гейте.
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {
            "workflow_runs": [workflow_run(33814313381, "in_progress")]},
    })
    patch_gh(monkeypatch, fake)
    pool = [issue(89, assignees=())]
    pulls = [pull(500, ref="agent/89-fix-thing", labels=["ai:changes-requested"])]
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=False, pulls=pulls)
    assert observations == ["👷 воркер уже работает — dispatch не нужен"]
    assert actions == []
    assert fake.mutating_calls() == []


# ── Наблюдения vs действия в отчёте (#456) ───────────────────────────────────────
# render_action_report — единственное место, решающее «Действия»/«Действий не
# требуется»; проверяется отдельно от main(), без единого HTTP-вызова.


def test_render_action_report_says_no_actions_needed_when_both_empty():
    assert sch.render_action_report([], []) == ["", "Действий не требуется."]


def test_render_action_report_observations_alone_do_not_trigger_actions_header():
    # Живой баг (#456): «замок жив»/«диспатч разрешён без изменений» — только
    # наблюдения, ничего не изменилось — «### Действия» не должен появиться.
    result = sch.render_action_report(["🔒 замок task-5 жив (1.0 ч из 24)"], [])
    assert "### Действия" not in result
    assert "Действий не требуется." in result
    assert any("замок task-5 жив" in line for line in result)


def test_render_action_report_shows_actions_header_only_with_real_actions():
    result = sch.render_action_report([], ["✅ PR #1 слит (squash)"])
    assert "### Действия" in result
    assert "Действий не требуется." not in result
    assert any("PR #1 слит" in line for line in result)


def test_render_action_report_shows_both_sections_when_mixed():
    result = sch.render_action_report(["👷 воркер уже работает — dispatch не нужен"],
                                       ["✅ PR #1 слит (squash)"])
    assert "### Наблюдения (без изменения состояния)" in result
    assert "### Действия" in result
    assert "Действий не требуется." not in result


# ── pr_needs_rework/wip_gate (#464): WIP-лимит перед взятием НОВОЙ задачи ────────


def test_pr_needs_rework_true_for_ai_changes_requested():
    assert sch.pr_needs_rework(pull(1, labels=["review:ok", "ai:changes-requested"]))


def test_pr_needs_rework_true_for_conflict():
    assert sch.pr_needs_rework(pull(2, labels=["review:ok", "conflict"]))


def test_pr_needs_rework_true_for_contract_failed():
    assert sch.pr_needs_rework(pull(3, labels=["review:ok", "contract:failed"]))


def test_pr_needs_rework_false_when_only_waiting_for_verdict():
    # review:ok без ai:changes-requested/conflict/contract:failed — движется
    # сам (ждёт вердикта или уже готов), не в счёт лимита.
    assert not sch.pr_needs_rework(pull(4, labels=["review:ok"]))
    assert not sch.pr_needs_rework(pull(5, labels=["review:ok", "ai:ok"]))


def test_pr_needs_rework_false_for_ai_failed_alone():
    # ai:failed самолечится авто-повтором trigger_ai_review (см. докстринг
    # REWORK_LABELS) — не в счёт, в отличие от ai:changes-requested.
    assert not sch.pr_needs_rework(pull(6, labels=["review:ok", "ai:failed"]))


def test_pr_needs_rework_false_for_draft_even_with_rework_label():
    assert not sch.pr_needs_rework(pull(7, labels=["ai:changes-requested"], draft=True))


def test_pr_needs_rework_false_for_bot_author():
    assert not sch.pr_needs_rework(
        pull(8, labels=["ai:changes-requested"], author_login="dependabot[bot]"))


def test_wip_gate_matches_live_repository_snapshot_2026_09_06(monkeypatch):
    """Живые данные (замер 2026-09-06, `gh pr list --state open`, 29 открытых
    PR этого репозитория): ровно 25 реально ждут доработки. WIP_LIMIT=12 —
    гейт обязан быть закрыт уже на сегодняшнем состоянии, это и есть ответ на
    «взял бы воркер задачу или нет» из задачи #464."""
    live_labels = {
        241: ["ai:changes-requested", "review:large", "review:large-ok", "review:ok"],
        248: ["ai:changes-requested", "conflict", "review:large", "review:large-ok", "review:ok", "task"],
        261: ["ai:changes-requested", "review:large", "review:large-ok", "review:ok"],
        262: ["ai:changes-requested", "review:large", "review:large-ok", "review:ok"],
        263: ["conflict", "review:ok"],
        327: ["review:large", "review:large-ok", "review:ok"],
        328: ["ai:changes-requested", "conflict", "review:ok"],
        329: ["review:ok"],
        333: ["ai:failed", "conflict", "review:ok"],
        336: ["ai:changes-requested", "review:ok"],
        344: ["ai:changes-requested", "review:ok"],
        360: ["ai:changes-requested", "conflict", "review:ok"],
        367: ["ai:changes-requested", "conflict", "review:large", "review:large-ok", "review:ok"],
        386: ["ai:changes-requested", "review:ok"],
        387: ["review:large", "review:large-ok", "review:ok"],
        395: ["ai:changes-requested", "conflict", "review:large"],
        397: ["ai:changes-requested", "conflict", "review:ok"],
        408: ["conflict"],
        409: ["ai:changes-requested", "conflict", "review:large"],
        410: ["ai:changes-requested", "review:ok"],
        411: ["ai:changes-requested", "review:large"],
        412: ["ai:changes-requested", "review:large"],
        415: ["conflict", "review:ok"],
        424: ["conflict", "review:large"],
        429: ["ai:changes-requested", "conflict", "contract:failed", "review:ok"],
        433: ["ai:ok", "review:ok"],
        439: ["ai:ok", "conflict", "contract:failed", "review:ok"],
        442: ["ai:changes-requested", "review:ok"],
        453: ["ai:changes-requested", "review:large", "review:large-ok"],
    }
    assert len(live_labels) == 29
    pulls = [pull(number, labels=labels) for number, labels in live_labels.items()]
    rework = [p for p in pulls if sch.pr_needs_rework(p)]
    assert len(rework) == 25
    fake = FakeGh({
        "issues/120/comments?per_page=100": [],
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 8, 5), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert any("25" in line and "12" in line for line in observations + actions)
    assert any("не берутся" in line for line in observations + actions)


# ── wip_gate + dispatch_worker: сквозной прогон обоих сценариев критерия
# приёмки (#464/находка ревью PR #466) — «гейт закрыт → доводка идёт, новые
# не берутся» и «гейт открыт → поведение прежнее».


def test_integration_gate_closed_dispatches_rework_not_new_task(monkeypatch):
    """Гейт закрыт (WIP_LIMIT PR ждут доработки — тот же порог, что реально
    закрыл его на живых данных 2026-09-06, см. тест выше): пул содержит и
    настоящую новую задачу (#95, PR не открывался), и задачу с висящим PR
    (#89, PR #500 ждёт доводки). Воркер обязан пойти именно на #89."""
    rework_pulls = [
        pull(900 + n, labels=["ai:changes-requested"], ref=f"agent/{900 + n}-old")
        for n in range(1, sch.WIP_LIMIT + 1)
    ]
    rework_pulls.append(pull(500, labels=["ai:changes-requested"], ref="agent/89-fix-thing"))
    fake = FakeGh({
        "issues/120/comments?per_page=100": [],
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 12, 0), rework_pulls, pool=[], dispatch_allowed=True)
    assert allowed is False  # WIP_LIMIT PR ждут доводки — новые задачи закрыты
    pool = [issue(95, assignees=()), issue(89, assignees=())]
    d_observations, d_actions = sch.dispatch_worker(
        REPO, pool, wip_allowed=allowed, pulls=rework_pulls)
    combined = d_observations + d_actions
    assert any("#89" in line for line in combined)
    assert not any("#95" in line for line in combined)  # новая задача НЕ взята
    assert any(
        call.endswith("-f ref=main -f inputs[task]=89") for call in fake.mutating_calls()
    )


def test_integration_gate_open_dispatches_oldest_free_task_as_before(monkeypatch):
    """Гейт открыт (PR, ждущих доводки, меньше WIP_LIMIT) — поведение не
    отличается от действовавшего до #464: воркер берёт старейшую свободную
    задачу пула, не глядя на declared_pr_task_numbers вовсе."""
    few_rework_pulls = [pull(1, labels=["ai:changes-requested"], ref="agent/1-x")]
    fake = FakeGh({
        "issues/120/comments?per_page=100": [],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 12, 0), few_rework_pulls, pool=[], dispatch_allowed=True)
    assert allowed is True
    pool = [issue(95, assignees=()), issue(89, assignees=())]
    d_observations, d_actions = sch.dispatch_worker(
        REPO, pool, wip_allowed=allowed, pulls=few_rework_pulls)
    combined = d_observations + d_actions
    assert any("#89" in line for line in combined)  # старейшая свободная, не #95
    assert fake.mutating_calls()[-1] == (
        f"-X POST repos/{REPO}/actions/workflows/worker.yml/dispatches -f ref=main"
    )  # без inputs[task] — воркер выбирает сам, как до #464


def test_wip_gate_allows_dispatch_when_rework_below_limit(monkeypatch):
    fake = FakeGh({"issues/120/comments?per_page=100": []})
    patch_gh(monkeypatch, fake)
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT - 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 8, 0), pulls, pool=[], dispatch_allowed=True)
    assert allowed is True
    assert actions == []  # эпизода не было — просто наблюдение, действий нет
    assert any("разрешены" in line for line in observations)
    assert fake.mutating_calls() == []


def test_wip_gate_closes_dispatch_when_rework_at_limit_and_posts_marker_once(monkeypatch):
    fake = FakeGh({
        "issues/120/comments?per_page=100": [],
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
    })
    patch_gh(monkeypatch, fake)
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 8, 0), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert any("не берутся" in line for line in observations + actions)
    posts = [c for c in fake.mutating_calls() if "issues/120/comments" in c]
    assert len(posts) == 1
    assert sch.WIP_GATE_OPEN_MARKER in posts[0]


def test_wip_gate_does_not_repost_open_marker_while_episode_active(monkeypatch):
    # Идемпотентность (тот же приём, что PAUSE_MARKER/RESUME_MARKER): второй
    # пульс подряд с тем же перегруженным пулом не должен слать второй
    # комментарий — маркер уже стоит.
    comments = [{"created_at": "2026-09-06T06:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({"issues/120/comments?per_page=100": comments})
    patch_gh(monkeypatch, fake)
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 8, 0), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert fake.mutating_calls() == []  # уже оповещено — наблюдение, не действие
    assert any("не берутся" in line for line in observations)


def test_wip_gate_reopens_dispatch_and_posts_close_marker_when_queue_drains(monkeypatch):
    comments = [{"created_at": "2026-09-06T06:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({
        "issues/120/comments?per_page=100": comments,
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
    })
    patch_gh(monkeypatch, fake)
    pulls = [pull(1, labels=["ai:changes-requested"])]  # очередь поредела ниже лимита
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 8, 0), pulls, pool=[], dispatch_allowed=True)
    assert allowed is True
    posts = [c for c in fake.mutating_calls() if "issues/120/comments" in c]
    assert len(posts) == 1
    assert sch.WIP_GATE_CLOSE_MARKER in posts[0]


def test_wip_gate_escalates_when_episode_older_than_stuck_threshold(monkeypatch):
    opened_at = utc(2026, 9, 6, 0, 0)  # 9ч назад > WIP_GATE_STUCK_HOURS (8)
    comments = [{"created_at": "2026-09-06T00:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({"issues/120/comments?per_page=100": comments})
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append(text) or "ок")
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    # Доводка доступна: свободная задача #89 объявлена открытым PR (ветка
    # agent/89-... в снимке pulls) — сигнал обязан назвать этот факт и адрес,
    # а не гадать между «не успевает» и «доводить нечего» (AGENTS.md).
    pulls.append(pull(500, labels=["review:ok"], ref="agent/89-rework"))
    pool = [issue(89, assignees=())]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 9, 5), pulls, pool, dispatch_allowed=True)
    assert allowed is False
    assert escalated and sch.WIP_GATE_STUCK_MARKER_PREFIX in escalated[0]
    assert "доводить есть что" in escalated[0]
    assert "#89" in escalated[0]
    assert "не успевает" in escalated[0]
    assert "диспатчей доводки не было" not in escalated[0]
    assert any("держит взятие новых задач" in line for line in actions)


def test_wip_gate_stuck_escalation_states_nothing_to_rework_when_no_targets(monkeypatch):
    """Находка ревью PR #466 + AGENTS.md «алерт не гадает»: когда ни у одной
    свободной задачи нет открытого PR, сигнал не вправе утверждать, что
    «воркер всё это время диспетчируется на доводку» (диспатчей не было), и
    не вправе оставлять владельцу выбор из «либо/либо» — код различает причины
    сам по данным того же прогона. Мутация: замени ветку `if targets:` в
    wip_gate на безусловный текст «доводить есть что» — тест покраснеет."""
    opened_at = utc(2026, 9, 6, 0, 0)
    comments = [{"created_at": "2026-09-06T00:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({"issues/120/comments?per_page=100": comments})
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append(text) or "ок")
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 9, 5), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert escalated
    assert "доводить нечего" in escalated[0]
    assert "диспатчей доводки не было" in escalated[0]
    assert "доводить есть что" not in escalated[0]
    assert "диспетчируется" not in escalated[0]


def test_wip_gate_stays_silent_while_fuse_paused(monkeypatch):
    """Блокирующая находка 2 ревью PR #466: при погашенном предохранителе
    (#120) доводка не диспетчится вовсе, тормоз уже назван conveyor_gate —
    WIP-гейт не открывает эпизод и не эскалирует. Иначе через 8ч сигнал
    говорил бы «доводка не успевает», хотя доводки не было: две
    противоречащие истории владельцу. Мутация: сними ранний return по
    `dispatch_allowed` в wip_gate — тест краснеет (эпизод открылся)."""
    fake = FakeGh({
        "issues/120/comments?per_page=100": [],
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail(
        "не должен эскалировать под предохранителем — тормоз уже назван"))
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 3)]
    observations, actions, allowed = sch.wip_gate(
        REPO, utc(2026, 9, 6, 9, 0), pulls, pool=[], dispatch_allowed=False)
    assert allowed is False
    assert observations == [] and actions == []
    assert fake.mutating_calls() == []  # ни маркера открытия, ни эскалации


def test_wip_gate_closes_open_episode_when_fuse_takes_over(monkeypatch):
    """Та же находка, вторая половина: если эпизод WIP уже был открыт, с
    началом паузы предохранителя (#120) он закрывается — stuck-часы считают
    только время, когда WIP был действующим тормозом. После снятия
    предохранителя при счётчике ≥ лимита эпизод откроется заново с чистого
    листа (сам, без ручного)."""
    comments = [{"created_at": "2026-09-06T00:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({
        "issues/120/comments?per_page=100": comments,
        "-X POST repos/mytab0r/edge-harness/issues/120/comments": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("не должен эскалировать под предохранителем"))
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(
        REPO, utc(2026, 9, 6, 9, 0), pulls, pool=[], dispatch_allowed=False)
    assert allowed is False
    posts = [c for c in fake.mutating_calls() if "issues/120/comments" in c]
    assert len(posts) == 1
    assert sch.WIP_GATE_CLOSE_MARKER in posts[0]
    assert "предохранителем" in posts[0]


def test_wip_gate_stuck_names_conflict_cycle_when_only_conflict_targets(monkeypatch):
    """Блокирующая находка 1 ревью PR #466, сторона алерта: если свободные
    задачи с открытым PR — только конфликтные, сигнал не вправе звать их
    «доводкой, которая не успевает» (их ведёт авто-расшивка #474: одна
    попытка ребейза, бюджет по маркеру, после исчерпания — своя эскалация)
    и не вправе молчать про них («доводить нечего» — тоже неправда)."""
    comments = [{"created_at": "2026-09-06T00:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER}]
    fake = FakeGh({"issues/120/comments?per_page=100": comments})
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue, text: escalated.append(text) or "ок")
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    pulls.append(pull(500, labels=["conflict"], ref="agent/89-fix"))
    pool = [issue(89, assignees=())]
    observations, actions, allowed = sch.wip_gate(
        REPO, utc(2026, 9, 6, 9, 5), pulls, pool, dispatch_allowed=True)
    assert allowed is False
    assert escalated
    assert "#89" in escalated[0]
    assert "#474" in escalated[0]
    assert "доводить есть что" not in escalated[0]
    assert "доводить нечего" not in escalated[0]


def test_dispatch_worker_skips_conflict_declared_task_when_wip_gate_closed(monkeypatch):
    """Блокирующая находка 1 ревью PR #466, сторона диспатча: conflict-
    объявленная задача исключена из целей адресной доводки — у неё свой цикл
    (#474: одна попытка ребейза, бюджет по маркеру CONFLICT_REWORK_MARKER,
    который адресная ветка не пишет). Без исключения гейт пере-диспетчил бы
    конфликтный PR каждые 15 минут бесконечно, невидимо для бюджета. Мутация:
    убери вычитание conflict_declared_tasks в rework_target_numbers — тест
    краснеет (диспатч уйдёт)."""
    pool = [issue(89, assignees=())]
    pulls = [pull(500, labels=["conflict"], ref="agent/89-fix")]
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    observations, actions = sch.dispatch_worker(REPO, pool, wip_allowed=False, pulls=pulls)
    assert fake.mutating_calls() == []  # диспатча нет: конфликт идёт через цикл #474
    assert any("доводить нечего" in line for line in observations)


def test_wip_gate_does_not_escalate_twice_for_the_same_stuck_episode(monkeypatch):
    stuck_marker = f"{sch.WIP_GATE_STUCK_MARKER_PREFIX}{sch.WIP_GATE_STUCK_HOURS}ч]"
    comments = [
        {"created_at": "2026-09-06T00:00:00Z", "body": sch.WIP_GATE_OPEN_MARKER},
        {"created_at": "2026-09-06T08:10:00Z", "body": f"🚨 edge-harness: {stuck_marker}\nуже кричали"},
    ]
    fake = FakeGh({"issues/120/comments?per_page=100": comments})
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("не должен эскалировать повторно"))
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 9, 5), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert fake.mutating_calls() == []  # ни открытия (уже есть), ни повторной тревоги


def test_wip_gate_does_not_escalate_blindly_when_stuck_markers_unreadable(monkeypatch):
    """Находка ревью PR #466: раньше сбой чтения stuck-маркеров (`except
    RuntimeError: stuck_markers = []`) выглядел неотличимо от «маркера ещё не
    было» — при ПОСТОЯННОМ сбое (не разовом) эскалация уходила бы заново
    каждый пульс (каждые 15 минут), нарушая собственную политику функции
    («маркеры недоступны — не гадаем») и escalate_if_new в repo_invariants.
    Мутация: верни `stuck_markers = []` в except без досрочного return —
    этот тест покраснеет (escalate позовётся)."""
    opened_at_iso = "2026-09-06T00:00:00Z"  # 9ч назад > WIP_GATE_STUCK_HOURS (8)

    def fake_issue_marker_times(repo, issue_number, marker):
        if marker == sch.WIP_GATE_STUCK_MARKER_PREFIX:
            raise RuntimeError("gh api repos/o/r/issues/120/comments: HTTP 502")
        if marker == sch.WIP_GATE_OPEN_MARKER:
            return [sch.parse_time(opened_at_iso)]
        return []  # WIP_GATE_CLOSE_MARKER — эпизод ещё не закрывался

    monkeypatch.setattr(sch, "issue_marker_times", fake_issue_marker_times)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail(
        "не должен эскалировать вслепую при сбое чтения stuck-маркеров"))
    pulls = [pull(n, labels=["ai:changes-requested"]) for n in range(1, sch.WIP_LIMIT + 1)]
    observations, actions, allowed = sch.wip_gate(REPO, utc(2026, 9, 6, 9, 5), pulls, pool=[], dispatch_allowed=True)
    assert allowed is False
    assert any("не прочитаны" in line for line in actions)


def test_main_still_dispatches_worker_for_rework_when_wip_gate_closed(monkeypatch):
    """Проводка в main() (не внутри dispatch_worker) — мутация-гвардия для
    критической находки ревью PR #466: верни `if dispatch_allowed and
    wip_allowed:` вместо передачи `wip_allowed` внутрь dispatch_worker —
    dispatch_worker вообще не позовётся при закрытом гейте, вопреки замыслу
    (гейт держит только НОВЫЕ задачи, доводка обязана идти), и этот тест
    покраснеет."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "merged_pr_map", lambda pulls: {})
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    live_pulls = [{"marker": "same snapshot merge_loop produced"}]
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], [], False, live_pulls))
    monkeypatch.setattr(sch, "trigger_ai_review", lambda repo, now, pulls: ([], []))
    monkeypatch.setattr(sch, "stale_ready_pulls", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [issue(89, assignees=())])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "mark_stale_unclaimed", lambda repo, now, pool: [])
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    # Расшивка конфликтов (#474) — не предмет теста: её цикл читает labels[]
    # каждого PR, а здесь снимок — маркер-заглушка без полей.
    monkeypatch.setattr(sch, "dispatch_conflict_rework", lambda repo, pulls, pool: ([], [], False))
    # Детектор простоя (#201) — не предмет теста: он читает строки отчёта
    # (включая здешнюю «новые задачи не берутся»), сверяет отпечаток по сети
    # и в среде без gh-токена падает RuntimeError'ом, крася прогон (return 1),
    # хотя предмет теста — проводка dispatch_worker. Изолируем, как соседние
    # main()-тесты.
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    monkeypatch.setattr(
        sch, "wip_gate",
        lambda repo, now, pulls, pool, dispatch_allowed: (["⏸️ новые задачи не берутся: 25 открытых PR ждут доработки при лимите 12"], [], False))
    dispatched = []
    monkeypatch.setattr(
        sch, "dispatch_worker",
        lambda repo, pool, *, wip_allowed, pulls: dispatched.append((repo, pool, wip_allowed, pulls)) or (
            ["👷 доводка PR по задаче #89 диспетчирована"], []))
    reports = []
    monkeypatch.setattr(sch, "summary", lambda lines: reports.append(lines))
    code = sch.main()
    assert code == 0
    # wip_gate закрыт (allowed=False), но dispatch_worker обязан быть ВЫЗВАН —
    # это его дело решить, есть ли что довести, не main().
    assert len(dispatched) == 1
    called_repo, called_pool, called_wip_allowed, called_pulls = dispatched[0]
    assert called_repo == REPO
    assert called_wip_allowed is False
    assert called_pulls is live_pulls  # тот же снимок, что уже видел wip_gate — второго обхода нет
    [report] = reports
    assert any("не берутся" in line for line in report)
    assert any("доводка PR по задаче #89" in line for line in report)


def test_main_skips_worker_dispatch_while_fuse_paused(monkeypatch):
    """Предохранитель конвейера (#120) в паузе → dispatch_worker не вызывается
    вовсе (проводка в main, не внутри dispatch_worker): этот тормоз — про
    сломанный сам конвейер, не про размер очереди доработки, и остаётся
    единственным, кто гасит dispatch_worker целиком."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "merged_pr_map", lambda pulls: {})
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], [], False, pulls))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [issue(89, assignees=())])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None, open_pulls_list=None: ([], [], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: (["⏸️ пауза диспатча"], [], False))
    wip_seen = []
    monkeypatch.setattr(
        sch, "wip_gate",
        lambda repo, now, pulls, pool, dispatch_allowed: wip_seen.append(dispatch_allowed) or ([], [], True))
    # Не предмет этого теста (#427) — issue(89) без исполнителя и старым
    # дефолтным created_at реально старее STALE_HOURS к моменту прогона:
    # непатченный mark_stale_unclaimed бил бы по настоящему gh (нашла CI, не я).
    monkeypatch.setattr(sch, "mark_stale_unclaimed", lambda repo, now, pool: [])
    # Без заглушки эти два вызова main() бьют настоящим `gh api` (#201) —
    # в CI без GH_TOKEN это гарантированный красный прогон (находка ревью PR #248):
    # gh отказывает без авторизации ДО сетевого запроса, а не молча читает
    # публичный эндпоинт анонимно.
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    dispatched = []
    monkeypatch.setattr(
        sch, "dispatch_worker",
        lambda repo, pool, *, wip_allowed, pulls: dispatched.append((repo, pool)) or [],
    )
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    assert sch.main() == 0
    assert dispatched == []
    # Блокирующая находка 2 ревью PR #466: при паузе предохранителя флаг
    # обязан дойти до wip_gate — по нему гейт молчит и закрывает свой эпизод.
    assert wip_seen == [False]


def test_main_makes_zero_mutating_calls_on_fully_empty_queue(monkeypatch):
    """Сквозная гвардия холостого хода: main() целиком, пустая очередь PR и
    задач — GITHUB_STEP_SUMMARY не пишем на диск, gh() не делает ни одного
    POST/PUT/DELETE ни в одном из семи механизмов сразу."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # main() сам вычисляет now = datetime.now(timezone.utc) — фиксированная
    # дата в фикстуре была бы всегда "в прошлом" и красила heartbeat stale.
    # Последний успех — минуту назад от реального now, гарантированно "в норме".
    just_now = datetime.now(timezone.utc)
    recent_success_iso = just_now.isoformat(timespec="seconds").replace("+00:00", "Z")
    fake = FakeGh({
        # failure_watch (#477): дешёвый опрос status=completed (вывод
        # фильтруется клиентом — FAILURE_WATCH_RUN_CONCLUSIONS) по каждому из
        # WATCHED_WORKFLOWS — один маршрут по подстроке "runs?status=completed"
        # обслуживает все пять (имя workflow в него не входит). Обязан идти
        # ПЕРВЫМ ключом: FakeGh матчит по первому совпадению подстроки в
        # порядке вставки, а более общий ключ "workflows/orchestra.yml/runs"
        # ниже иначе перехватил бы и этот запрос тоже (оба — подстроки одного
        # реального URL "workflows/orchestra.yml/runs?status=completed&...").
        "runs?status=completed": {"workflow_runs": []},
        # event — прод-форма поля, которое реально возвращает GitHub для
        # запроса, отфильтрованного по ?event=... (real_orchestra_ticks,
        # находка AI-ревью PR #318, второй раунд, поймана при фиксе:
        # allowlist по ORCHESTRA_TICK_EVENTS отсеивал фикстуру без event).
        "workflows/orchestra.yml/runs": {"workflow_runs": [
            {"conclusion": "success", "created_at": recent_success_iso,
             "html_url": "https://x", "display_title": "x", "event": "schedule"}]},
        "issues?state=open&labels=task": [],
        "pulls?state=open": [],
        # Приёмка (#227): merged_pr_map(all_merged_pulls(repo)) обходит слитые
        # PR ДО подсчёта пула — пустой пул слитых означает пустую страницу.
        "pulls?state=closed&per_page=100&page=1": [],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        # conveyor_gate читает историю worker.yml без фильтра status — отдельный
        # маршрут от worker_runs_active (?status=in_progress/queued выше).
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": []},
        # conveyor_gate (#205) читает маркеры активной серии из #120 даже при
        # failures=0 — иначе не отличить «серии не было» от «проба ещё бежит».
        # Пустая история worker.yml => маркеров нет, но запрос всё равно уходит.
        "issues/120/comments?per_page=100": [],
        # Сверка дрейфа пина (#134) ходит в каждом холостом пульсе: теги апстрима
        # (прод-форма repos/tags, снята живым запросом 2026-09-03; sha первого
        # тега = текущий пин dsh-edge/upstream.json) и метки задачи #134.
        # Пин свеж → состояние ok → только чтение: гвардия внизу требует,
        # что и здесь не было ни одного POST/PUT/DELETE.
        "repos/pawaca/dsh-edge/tags?per_page=100": [
            # sha первого тега = текущий пин dsh-edge/upstream.json (бамп #505,
            # 2026-09-06): "ok" здесь держится на том, что этот тег — новейший
            # стабильный в списке, а не на конкретном номере версии.
            {"name": "dsh-edge-v0.11.1",
             "commit": {"sha": "e1941bbcb7e2d5693df9fc278362f2c184963024", "url": "https://x"}},
            {"name": "dsh-edge-v0.8.0",
             "commit": {"sha": "b9a8ddd6cd11bc0db94d3f67bbc7de4d674e69a1", "url": "https://x"}},
            {"name": "dsh-edge-v0.7.1",
             "commit": {"sha": "113a96913c51881993122afbf42e776882c4beb7", "url": "https://x"}},
        ],
        "issues/134": {"number": 134, "labels": []},
        # Детектор простоя (#201): пустой отчёт => detect_and_act не делает ни
        # одного вызова (см. test_stall_detector.py::test_idle_conveyor_makes_zero_calls);
        # escalate_stale_auto_tasks всё равно читает список автозадач — маршрут
        # нужен, пустой список => дальше вызовов нет вовсе.
        "issues?state=open&labels=auto-detected": [],
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    reported = {}
    monkeypatch.setattr(sch, "summary", lambda lines: reported.setdefault("lines", lines))

    code = sch.main()

    assert code == 0
    assert fake.mutating_calls() == [], f"холостой ход дёрнул состояние: {fake.mutating_calls()}"
    # #456: до фикса «дозволен диспатч» (conveyor_gate, closed-состояние) само
    # по себе делало отчёт непустым, и main() красноречиво врал «есть
    # действия» даже на полностью холостом обходе — мутация-гвардия ниже это
    # доказывает (см. test_render_action_report_* и revert-мутацию в PR).
    report_text = "\n".join(reported["lines"])
    assert "Действий не требуется." in report_text
    assert "### Действия" not in report_text


def test_main_labels_old_unclaimed_task_end_to_end(monkeypatch):
    """Проводка mark_stale_unclaimed внутри main() (не сама функция — её
    гвардирует блок выше): старая свободная задача пула получает метку
    `stale-unclaimed` через настоящий gh POST и попадает строкой в отчёт.
    Находка ревью #428, п.2 — удаление вызова mark_stale_unclaimed из main()
    раньше проходило мимо всех тестов; этот тест красит именно такую мутацию,
    в отличие от гвардии холостого хода выше (та кормит пустой пул)."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    just_now = datetime.now(timezone.utc)
    recent_success_iso = just_now.isoformat(timespec="seconds").replace("+00:00", "Z")
    old_task = issue(300, assignees=(), labels=["task"], created_at="2020-01-01T00:00:00Z")
    fake = FakeGh({
        # failure_watch (#477): ПЕРВЫМ ключом — см. обоснование в
        # test_main_makes_zero_mutating_calls_on_fully_empty_queue выше
        # (иначе "workflows/orchestra.yml/runs" ниже перехватывает и этот
        # запрос тоже, оба матчат одну и ту же реальную строку URL).
        "runs?status=completed": {"workflow_runs": []},
        "workflows/orchestra.yml/runs": {"workflow_runs": [
            {"conclusion": "success", "created_at": recent_success_iso,
             "html_url": "https://x", "display_title": "x", "event": "schedule"}]},
        "issues?state=open&labels=task": [old_task],
        "pulls?state=open": [],
        "pulls?state=closed&per_page=100&page=1": [],
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/runs?per_page=10": {"workflow_runs": []},
        "issues/120/comments?per_page=100": [],
        "repos/pawaca/dsh-edge/tags?per_page=100": [
            # sha первого тега = текущий пин dsh-edge/upstream.json (бамп #505,
            # 2026-09-06): "ok" здесь держится на том, что этот тег — новейший
            # стабильный в списке, а не на конкретном номере версии.
            {"name": "dsh-edge-v0.11.1",
             "commit": {"sha": "e1941bbcb7e2d5693df9fc278362f2c184963024", "url": "https://x"}},
            {"name": "dsh-edge-v0.8.0",
             "commit": {"sha": "b9a8ddd6cd11bc0db94d3f67bbc7de4d674e69a1", "url": "https://x"}},
            {"name": "dsh-edge-v0.7.1",
             "commit": {"sha": "113a96913c51881993122afbf42e776882c4beb7", "url": "https://x"}},
        ],
        "issues/134": {"number": 134, "labels": []},
        "issues/300/labels": None,
        # Пул с одной свободной задачей допускает dispatch воркера (#120) —
        # не предмет этого теста, но main() дойдёт до него раньше отчёта.
        "workflows/worker.yml/dispatches": None,
        # escalate_stale_auto_tasks (#201) читает список автозадач на КАЖДОМ
        # пульсе, даже здоровом (см. docstring) — без маршрута main() упал бы
        # на этом же вызове раньше, чем дошёл до предмета этого теста.
        "issues?state=open&labels=auto-detected": [],
        # detect_worker_dispatch (#361) — задача 300 свободна, main() дойдёт
        # до именованного приоритета через graphql-пул; без маршрута
        # деградация печатает "⚠️ граф блокировок недоступен" в observations,
        # это выглядит как симптом для stall_detector и требует ещё один
        # маршрут (issue comment), не относящийся к предмету этого теста.
        "graphql": graphql_pool_response([old_task]),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    reports = []
    monkeypatch.setattr(sch, "summary", lambda lines: reports.append(lines))

    code = sch.main()

    assert code == 0
    posts = [c for c in fake.calls if c.startswith("-X POST") and "300/labels" in c]
    assert len(posts) == 1, f"main() не поставил метку stale-unclaimed: {fake.calls}"
    [report] = reports
    assert any("stale-unclaimed" in line and "300" in line for line in report), report


# ── Приёмка (#227): задача закрывается только по проверяемой улике ──────────────
#
# Фикстуры ниже — реальные ответы `gh api` по этому репозиторию (снято
# 2026-09-03, ветки досняты 2026-09-06 после решения владельца сделать ветку
# единственным источником): PR #138 (ветка agent/18-…, задача #18), PR #177
# (agent/21-…, #21), PR #163 (agent/78-…, #78). На момент инцидента #227 все
# три были слиты, а задачи оставались открытыми без исполнителя (reap_stale
# успел снять assignee по неверной причине «PR не появился») — сейчас
# #18/#21/#78 уже закрыты вручную, но тела PR и списки файлов ниже — то, что
# реально видел бы этот код в момент инцидента.

PR_138_BODY = "#18\n\nПост-мерж фиксы AI-ревьюера, вскрытые первыми живыми прогонами."
PR_138_FILES = [
    ".github/workflows/ai-review.yml", ".github/workflows/pr-review.yml",
    "docs/decisions/0007-ai-review-gate.md", "docs/research/21-github-actions.md",
    "openspec/changes/ai-review-gate/design.md", "openspec/changes/ai-review-gate/proposal.md",
    "openspec/changes/ai-review-gate/specs/journal-tasks-hands/spec.md",
    "openspec/changes/ai-review-gate/tasks.md", "scripts/review/ai_dsh.sh",
    "scripts/review/ai_review.py", "scripts/review/file_tasks.py", "scripts/review/test_ai_review.py",
]
# Реальные check-runs головы PR #138 (aaebecbb6adf816be99ab76ab30c3de796e3ff89).
PR_138_CHECK_RUNS = {"check_runs": [
    {"name": "CodeQL", "conclusion": "success"},
    {"name": "orchestra", "conclusion": "skipped"},
    {"name": "contract", "conclusion": "success"},
    {"name": "analyze", "conclusion": "success"},
    {"name": "test", "conclusion": "success"},
    {"name": "review", "conclusion": "success"},
]}

PR_177_BODY = "#21\n\n## Что сделано\n1. Добавлен npm-скрипт `dev:docker` в `cf-worker/package.json`…"
PR_177_FILES = ["cf-worker/README.md", "cf-worker/package.json", "docs/agents/PROTOCOL.md"]

PR_163_BODY = "#78\n\n## Что сделано\n\nСоздан полный дизайн плагинного механизма dsh-edge…"
PR_163_FILES = [
    "openspec/changes/dsh-edge-plugin-system/design.md",
    "openspec/changes/dsh-edge-plugin-system/proposal.md",
    "openspec/changes/dsh-edge-plugin-system/tasks.md",
]


def merged_pull(number, body, head_sha, merged_at, merge_commit_sha=None, branch=None):
    head = {"sha": head_sha}
    if branch is not None:
        head["ref"] = branch
    return {"number": number, "state": "closed", "merged_at": merged_at,
            "body": body, "head": head, "labels": [],
            "merge_commit_sha": merge_commit_sha}


def files_payload(names):
    return [{"filename": name} for name in names]


PR138 = merged_pull(138, PR_138_BODY, "aaebecbb6adf816be99ab76ab30c3de796e3ff89", "2026-09-02T21:31:47Z",
                     branch="agent/18-ai-trusted-from-main")
PR177 = merged_pull(177, PR_177_BODY, "67b23c9fb1bd43984c3a734bed569f0ae01a8d3c", "2026-09-02T17:01:28Z",
                     branch="agent/21-dev-wrangler-dev")
PR163 = merged_pull(163, PR_163_BODY, "fc3e8b2ba2422e81ab23a7ebc2948d84d1f71650", "2026-09-02T20:46:31Z",
                     branch="agent/78-dsh-edge")


def test_classify_acceptance_deploy_when_cf_worker_touched():
    assert sch.classify_acceptance(PR_177_FILES) == sch.ACCEPT_DEPLOY


def test_classify_acceptance_script_for_workflow_and_scripts():
    assert sch.classify_acceptance(PR_138_FILES) == sch.ACCEPT_SCRIPT


def test_classify_acceptance_docs_when_only_openspec_md():
    assert sch.classify_acceptance(PR_163_FILES) == sch.ACCEPT_DOCS


def test_merged_pr_map_uses_branch_not_prose_mention():
    # Задача PR — имя ветки (agent/18-…), не декларация тела (решение владельца
    # 2026-09-06: тело не читается вовсе).
    mapping = sch.merged_pr_map([PR138, PR177, PR163])
    assert mapping[18]["number"] == 138
    assert mapping[21]["number"] == 177
    assert mapping[78]["number"] == 163
    assert 999 not in mapping


def test_merged_pr_map_keeps_most_recent_merge_for_same_task():
    older = merged_pull(1, "старая работа", "sha1", "2026-01-01T00:00:00Z", branch="agent/5-old")
    newer = merged_pull(2, "новая работа поверх старой", "sha2", "2026-02-01T00:00:00Z", branch="agent/5-new")
    mapping = sch.merged_pr_map([older, newer])
    assert mapping[5]["number"] == 2


def test_merged_pr_map_ignores_body_successor_when_branch_task_closed():
    # Живой класс (PR #388/#384/#359/#167 репозитория на 2026-09-06): ветка
    # называет уже закрытую задачу #256, тело докрытия объявляет
    # открытую-преемницу #391. Решение владельца 2026-09-06 отменило
    # регистрацию по телу без исключений: карта видит только #256 (задачу
    # ветки), #391 в неё не попадает — правильная починка для #391 - новая
    # ветка agent/391-<slug>, не эта запись.
    reworked = merged_pull(
        388, "#391\n\nRelated: #256 (закрыта акцептансом, докрытие — #391)",
        "sha388", "2026-09-06T00:00:00Z", branch="agent/256-task-rework-loop",
    )
    mapping = sch.merged_pr_map([reworked])
    assert mapping[256]["number"] == 388
    assert 391 not in mapping


# ── Замена PR, чья ветка называет закрытую задачу (#543) ─────────────────────


def test_replace_closed_task_prs_skips_when_branch_task_is_open(monkeypatch):
    """Задача ветки #100 есть в переданном пуле (открыта) — функция не имеет
    права сделать НИ ОДНОГО сетевого вызова: сравнение чисел уже даёт ответ
    «всё в порядке»."""
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    p = pull(500, ref="agent/100-fix-thing")
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[issue(100)])
    assert lines == []
    assert fake.calls == []


def test_replace_closed_task_prs_skips_pr_without_agent_branch(monkeypatch):
    """PR без agent-ветки (resolve_pr_task вернёт None — ручной пуш,
    dependabot) — не наш случай, ни одного вызова."""
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    p = pull(501)  # ref не задан
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[])
    assert lines == []
    assert fake.calls == []


def test_replace_closed_task_prs_skips_already_marked_pr(monkeypatch):
    """PR уже помечен маркером прошлым прогоном — повторный поиск/создание
    не нужны вовсе, маркер в уже прочитанном теле снимка решает вопрос без
    единого сетевого вызова (экономия search-бюджета, #543 п.2 «не
    плодить»)."""
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    body = sch.TASK_REPLACEMENT_MARKER + "\nRelated: #900\n\nостальное тело"
    p = pull(502, ref="agent/431-old-task", pr_body=body)
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[])
    assert lines == []
    assert fake.calls == []


def test_replace_closed_task_prs_creates_replacement_for_closed_task(monkeypatch):
    """Прод-форма (PR #439, 2026-09-06): ветка называет закрытую задачу
    #431, в переданном пуле её нет, Search API не находит существующей
    замены — заводит новую узкую задачу, метит тело и комментирует PR."""
    fake = FakeGh({
        "search/issues": {"items": []},
        f"repos/{REPO}/issues/431": issue(431, state="closed"),
        f"-X POST repos/{REPO}/issues -f title=": {"number": 999},
        f"-X PATCH repos/{REPO}/pulls/439": None,
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    p = pull(439, ref="agent/431-ai-review-retry-budget",
              pr_body="#538\n\n## Проблема\n...")
    p["title"] = "#431: ai:failed — бюджет автоповторов по эпохе review:ok"
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[issue(100)])

    assert any("заведена замена #999" in line for line in lines)
    create_call = next(c for c in fake.calls if c.startswith(f"-X POST repos/{REPO}/issues"))
    assert "Related: #431" in create_call
    assert "labels[]=task" in create_call and "labels[]=auto-detected" in create_call
    patch_call = next(c for c in fake.calls if c.startswith(f"-X PATCH repos/{REPO}/pulls/439"))
    assert sch.TASK_REPLACEMENT_MARKER in patch_call
    assert "Related: #999" in patch_call
    assert p["body"].startswith(sch.TASK_REPLACEMENT_MARKER)  # снимок мутирован сразу
    assert posted and posted[0][0] == 439
    assert "#999" in posted[0][1] and "#431" in posted[0][1]


def test_replace_closed_task_prs_is_idempotent_across_two_runs(monkeypatch):
    """Второй прогон на том же (уже помеченном первым прогоном) PR не должен
    завести вторую задачу-замену — «не плодить» (#543 п.2), мутация: без
    маркера/skip-проверки счётчик создания вырос бы до 2."""
    fake = FakeGh({
        "search/issues": {"items": []},
        f"repos/{REPO}/issues/431": issue(431, state="closed"),
        f"-X POST repos/{REPO}/issues -f title=": {"number": 999},
        f"-X PATCH repos/{REPO}/pulls/439": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    p = pull(439, ref="agent/431-ai-review-retry-budget", pr_body="исходное тело")

    sch.replace_closed_task_prs(REPO, [p], pool=[])
    first = [c for c in fake.calls if c.startswith(f"-X POST repos/{REPO}/issues")]
    assert len(first) == 1

    lines_second = sch.replace_closed_task_prs(REPO, [p], pool=[])  # тот же PR-объект, второй прогон
    assert lines_second == []
    second = [c for c in fake.calls if c.startswith(f"-X POST repos/{REPO}/issues")]
    assert len(second) == 1  # не выросло


def test_replace_closed_task_prs_finds_existing_manual_replacement_prod_form(monkeypatch):
    """Мутационный прод-случай (#431→#538, снят живым запросом Search API
    2026-09-06): PR #439 (ветка agent/431-...), задача #431 закрыта
    приёмкой, владелец уже вручную завёл замену #538 с телом «Related: #431
    (закрыта приёмкой, работа продолжается в PR #439)». Функция обязана
    НАЙТИ эту замену и не завести вторую — без ветки `if existing is not
    None: ... continue` этот тест красен (заводит дубль поверх #538)."""
    fake = FakeGh({
        "search/issues": {
            "items": [
                {"number": 538, "state": "open",
                 "title": "бюджет автоповторов AI-ревью привязан к эпохе кода"},
            ]
        },
        f"-X PATCH repos/{REPO}/pulls/439": None,
    })
    patch_gh(monkeypatch, fake)
    p = pull(439, ref="agent/431-ai-review-retry-budget", pr_body="#538\n\nостальное тело")
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[])

    assert any("замена #538" in line and "уже существует" in line for line in lines)
    assert not any(c.startswith(f"-X POST repos/{REPO}/issues") for c in fake.calls)
    assert p["body"].startswith(sch.TASK_REPLACEMENT_MARKER)
    assert "Related: #538" in p["body"]


def test_replace_closed_task_prs_skips_task_that_turns_out_still_open(monkeypatch):
    """Задачи ветки нет в переданном пуле (например, страница сместилась
    между двумя вызовами в этом же прогоне), но точечная проверка находит
    её ОТКРЫТОЙ — не наш случай: другая причина непригодности, contract сам
    разберётся, вторая задача не заводится."""
    fake = FakeGh({
        "search/issues": {"items": []},
        f"repos/{REPO}/issues/777": issue(777, state="open"),
    })
    patch_gh(monkeypatch, fake)
    p = pull(600, ref="agent/777-something")
    lines = sch.replace_closed_task_prs(REPO, [p], pool=[])
    assert lines == []
    assert not any(c.startswith(f"-X POST repos/{REPO}/issues") for c in fake.calls)


def test_replace_closed_task_prs_reports_soft_failure_without_crashing(monkeypatch):
    """Сбой (сеть/API) на ОДНОМ PR не должен уронить обход остальных —
    строка-предупреждение в отчёте, следующий PR обрабатывается как ни в
    чём не бывало (тот же принцип, что у lease_observations в main())."""
    fake = FakeGh({
        'search/issues -f q=repo:mytab0r/edge-harness in:body "Related: #111"': RuntimeError("HTTP 500"),
        'search/issues -f q=repo:mytab0r/edge-harness in:body "Related: #431"': {"items": []},
        f"repos/{REPO}/issues/431": issue(431, state="closed"),
        f"-X POST repos/{REPO}/issues -f title=": {"number": 999},
        f"-X PATCH repos/{REPO}/pulls/439": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    broken = pull(700, ref="agent/111-broken")
    healthy = pull(439, ref="agent/431-ai-review-retry-budget")
    lines = sch.replace_closed_task_prs(REPO, [broken, healthy], pool=[])
    assert any("поиск существующей замены" in line and "111" in line for line in lines)
    assert any("заведена замена #999" in line for line in lines)


# ── Запрет переоткрытия (#369): закрытая задача не переоткрывается никогда ──────


def test_reject_reopened_tasks_ignores_normal_open_issue(monkeypatch):
    """Обычная (не переоткрытая) задача — state_reason=None в прод-форме
    Issues API. Функция обязана не сделать НИ ОДНОГО вызова gh — дорогая
    проверка на КАЖДОМ пульсе была бы штрафом за задачи, которые никто не
    трогал."""
    fake = FakeGh({})  # ни один маршрут не должен понадобиться
    patch_gh(monkeypatch, fake)
    pool = [issue(111, state_reason=None), issue(114, state_reason="completed")]
    lines = sch.reject_reopened_tasks(REPO, pool)
    assert lines == []
    assert fake.calls == []


def test_reject_reopened_tasks_closes_back_with_actionable_comment(monkeypatch):
    """Прод-форма живого случая (#111, 2026-09-06): issue закрыта, потом
    переоткрыта человеком, назначения нет. Ветка обязана закрыть её обратно
    и оставить комментарий с ГОТОВЫМ действием (не просто «нельзя»)."""
    fake = FakeGh({
        "issues/111/comments": None,
        "issues/111 -f state=closed": None,
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    released = []
    monkeypatch.setattr(sch.claim_task, "release",
                         lambda repo, n: released.append(n) or f"замок task-{n} снят")

    pool = [issue(111, assignees=(), state_reason="reopened")]
    lines = sch.reject_reopened_tasks(REPO, pool)

    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/111") for call in fake.calls)
    assert posted and posted[0][0] == 111
    assert sch.REOPEN_REJECTED_MARKER in posted[0][1]
    assert "новой" in posted[0][1].lower() and "related" in posted[0][1]
    assert any("111" in line and "закрыта обратно" in line for line in lines)
    # ни assignee, ни lock трогать не за что — их не было (release этой
    # веткой вызывается безусловно — замокан, чтобы не бить прод-API, #380 находка 1)
    assert not any("assignees" in call for call in fake.calls)
    assert released == [111]


def test_reject_reopened_tasks_releases_assignee_and_lock(monkeypatch):
    """Реопен мог вернуть issue назначение (человек/агент назначил себя после
    переоткрытия, #114 из живого случая) — ветка обязана снять и его, и
    замок аренды (#121), не только закрыть issue."""
    fake = FakeGh({
        "issues/114/comments": None,
        "issues/114 -f state=closed": None,
        "-X DELETE repos/mytab0r/edge-harness/issues/114/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    released = []
    monkeypatch.setattr(sch.claim_task, "release",
                         lambda repo, n: released.append(n) or f"замок task-{n} снят")

    pool = [issue(114, assignees=("mytab0r",), state_reason="reopened")]
    lines = sch.reject_reopened_tasks(REPO, pool)

    assert any("DELETE" in call and "114/assignees" in call for call in fake.calls)
    assert released == [114]
    assert any("замок task-114 снят" in line for line in lines)


def test_reject_reopened_tasks_never_closes_watchdog_issue(monkeypatch):
    """WATCHDOG_ISSUE (#120) — постоянный канал эскалации, не задача из пула;
    даже если он окажется reopened (в теории — он не заводился шаблоном
    задачи), автоматика не имеет права его закрыть — тот же приём, что уже
    защищает WATCHDOG_ISSUE в accept_merged_tasks."""
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    pool = [issue(sch.WATCHDOG_ISSUE, state_reason="reopened")]
    lines = sch.reject_reopened_tasks(REPO, pool)
    assert lines == []
    assert fake.calls == []


def test_reject_reopened_tasks_reports_soft_failure_without_crashing(monkeypatch):
    """Сетевой/API сбой на комментарии или PATCH не должен ронять обход
    остальных задач пула — тот же принцип, что и у accept_merged_tasks."""
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(
        monkeypatch, lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 500")))
    pool = [issue(111, state_reason="reopened"), issue(114, state_reason="reopened")]
    lines = sch.reject_reopened_tasks(REPO, pool)
    assert len([line for line in lines if "не отклонено" in line]) == 2


def test_main_closes_reopened_task_before_acceptance_sees_it(monkeypatch):
    """Интеграционный тест на прод-сценарий #363/#369: переоткрытая задача с
    ВСЁ ЕЩЁ валидным старым merged-PR обязана быть закрыта запретом
    переоткрытия ДО того, как accept_merged_tasks её увидит — иначе она
    снова смэтчится по декларации первой строки и «переоткрытие» будет
    отменено приёмкой за один пульс (живой случай #131/PR #132, трижды).
    Мутация: закомментировать пересчёт `pool = open_task_issues(repo)`
    после reject_reopened_tasks — accept_merged_tasks получит СТАРЫЙ снимок
    с ещё открытой (на самом деле уже закрытой) issue и увидит её снова."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "independent_pulse_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "failure_watch", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None, *, pool=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: ([], []))
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    # unhealthy_pulls теперь получает pool параметром (#443, не опрашивает
    # open_task_issues сама) — стаб оставлен просто чтобы не тянуть в тест её
    # внутреннюю логику (pulls тут всегда пуст, реальная реализация тоже
    # вернула бы []); сигнатура обязана принимать pool, иначе main() упадёт.
    monkeypatch.setattr(sch, "unhealthy_pulls", lambda repo, now, pulls, *, pool=None: [])
    monkeypatch.setattr(sch, "merge_loop", lambda repo, pulls: ([], [], False, pulls))
    monkeypatch.setattr(sch, "trigger_ai_review", lambda repo, now, pulls: ([], []))
    monkeypatch.setattr(sch, "stale_ready_pulls", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], [], True))
    monkeypatch.setattr(sch, "wip_gate", lambda repo, now, pulls, pool, dispatch_allowed: ([], [], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool, *, wip_allowed, pulls: ([], []))
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("сбоя тут нет"))
    # Детектор устойчивого простоя (#201) — не предмет этого теста, гасим
    # (без мока main() дошёл бы до open_auto_tasks/escalate_stale_auto_tasks
    # с реальным списком auto-detected issues, которого нет в FakeGh ниже).
    monkeypatch.setattr(sch, "detect_and_act", lambda repo, now, lines, run_url=None: [])
    monkeypatch.setattr(sch, "escalate_stale_auto_tasks", lambda repo, now: [])
    # Единственный сырой gh-вызов этого сценария — PATCH закрытия отклонённого
    # переоткрытия (post_issue_comment/claim_task.release уже замоканы выше).
    patch_gh(monkeypatch, FakeGh({"issues/131 -f state=closed": None}))
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    reopened_issue = issue(131, assignees=(), state_reason="reopened")
    # Прод-форма: open_task_issues — реальная функция репозитория, вызывается
    # дважды за прогон (первый снимок пула + пересчёт после отклонённого
    # переоткрытия): первый ответ — переоткрытая задача ещё открыта, второй
    # (после reject_reopened_tasks её закрыл) — пуста.
    calls_n = [0]

    def _fake_open_task_issues(repo):
        calls_n[0] += 1
        return [reopened_issue] if calls_n[0] == 1 else []

    monkeypatch.setattr(sch, "open_task_issues", _fake_open_task_issues)

    accept_calls = []

    def fake_accept(repo, pool, merged, now=None, *, open_pulls_list=None):
        accept_calls.append([i["number"] for i in pool])
        return [], [], False

    monkeypatch.setattr(sch, "accept_merged_tasks", fake_accept)

    code = sch.main()

    assert code == 0
    # accept_merged_tasks обязан увидеть ПЕРЕСЧИТАННЫЙ пул — без #131,
    # закрытой запретом переоткрытия этим же прогоном.
    assert accept_calls == [[]]
    assert calls_n[0] == 2


class _FakeHealthResponse:
    def __init__(self, status):
        self.status = status

    def read(self, n=-1):
        return b'{"version":"0.8.0"}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _fake_urlopen(status):
    def _open(req, timeout=None):
        return _FakeHealthResponse(status)
    return _open


def test_accept_merged_tasks_closes_on_green_deploy_and_health(monkeypatch):
    """Деплой-класс (#21/PR #177 трогает cf-worker/): зелёный deploy-worker.yml
    (канарейка UI — его последний шаг) + /api/health=200 → задача закрыта."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/21 -f state=closed": None,
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "success", "created_at": "2026-09-02T23:31:31Z",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/33695471222"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "https://dsh-edge.mytab0r.workers.dev")
    monkeypatch.setattr(sch.urllib.request, "urlopen", _fake_urlopen(200))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#21" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/21") for call in fake.calls)
    assert posted and "улика получена" in posted[0][1]


def test_accept_merged_tasks_skips_close_when_second_pr_still_open(monkeypatch):
    """Проверка на входе (живой случай #320/#325): приёмка закрыла #320, пока
    по нему был открыт второй PR #325, чья ветка называет ту же задачу, —
    тот немедленно упал на contract («задача #320 закрыта»). Улика по
    уже слитому PR #177 не отменяет работу открытого PR #325 по той же
    задаче — закрывать рано, задача остаётся в работе."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "success", "created_at": "2026-09-02T23:31:31Z",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/33695471222"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "https://dsh-edge.mytab0r.workers.dev")
    monkeypatch.setattr(sch.urllib.request, "urlopen", _fake_urlopen(200))
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    still_open = pull(325, pr_body="#21\n\nвторой PR по этой задаче, работа продолжается",
                       ref="agent/21-second-pr")
    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(
        REPO, pool, {21: PR177}, open_pulls_list=[still_open])

    assert hard_failure is False
    assert any("приёмка отложена" in line and "#325" in line for line in (observations + actions))
    assert not any(call.startswith(f"-X PATCH repos/{REPO}/issues/21") for call in fake.calls)
    assert posted == []


def test_deploy_evidence_matches_own_merge_commit_not_next_merge(monkeypatch):
    """Прод-форма находки AI-ревью PR #253: оркестратор сливает по одному PR
    каждые ~15 минут, `workflow_runs` идёт от нового к старому. Два
    cf-worker-мержа подряд — у ПЕРВОГО свой зелёный прогон deploy-worker.yml,
    у ВТОРОГО (более нового, идёт в ответе первым) — красный. До фикса
    `next(r for r in runs if created_at >= merged_at)` брал первый по списку
    (самый новый), то есть чужой красный прогон ВТОРОГО мержа, и задача
    первого никогда бы не закрылась. Правильная улика — head_sha прогона
    равен merge_commit_sha самого PR."""
    pr_first = merged_pull(
        177, PR_177_BODY, "67b23c9fb1bd43984c3a734bed569f0ae01a8d3c",
        "2026-09-03T10:00:00Z", merge_commit_sha="1111111111111111111111111111111111merge")
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/21 -f state=closed": None,
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            # Самый новый в ответе — прогон ВТОРОГО мержа (чужой, красный).
            {"conclusion": "failure", "created_at": "2026-09-03T10:20:00Z",
             "head_sha": "2222222222222222222222222222222222merge",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/2"},
            # Прогон ПЕРВОГО мержа — свой, зелёный, но старше по списку.
            {"conclusion": "success", "created_at": "2026-09-03T10:05:00Z",
             "head_sha": "1111111111111111111111111111111111merge",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/1"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "https://dsh-edge.mytab0r.workers.dev")
    monkeypatch.setattr(sch.urllib.request, "urlopen", _fake_urlopen(200))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr_first}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#21" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/21") for call in fake.calls)
    assert posted and "улика получена" in posted[0][1]


def test_accept_merged_tasks_reads_all_pages_of_pr_files(monkeypatch):
    """Пагинация (находка AI-ревью PR #253, четвёртое место того же класса
    #294/#303): accept_merged_tasks читал сырую первую страницу
    `pulls/{n}/files?per_page=100` вместо review_labels.list_pr_files —
    у PR за сотню файлов, где cf-worker/* стоит за сотой позицией,
    classify_acceptance видел бы только первую сотню (обычные скрипты) и
    выдал бы класс "script" вместо "deploy". Последствие: задача закрылась
    бы по зелёным check-runs головы PR, а не по деплою+канарейке, которых
    требует критерий — и after_merge (уже читающий все страницы) запустил
    бы deploy-worker.yml, разойдясь с приёмкой в классификации ОДНОГО PR."""
    page1 = [{"filename": f"scripts/file{i}.py"} for i in range(100)]
    page2 = [{"filename": "cf-worker/worker.js"}]  # значимый файл СТРОГО за первой сотней

    def fake_gh(*args):
        joined = " ".join(args)
        if joined == f"repos/{REPO}/pulls/999/files?per_page=100&page=1":
            return page1
        if joined == f"repos/{REPO}/pulls/999/files?per_page=100&page=2":
            return page2
        if joined == f"repos/{REPO}/pulls/999/files?per_page=100":
            # Сырая одностраничная форма (мутация класса #294/#303): реальный
            # GitHub API без &page= отдаёт первую сотню — именно её вернул бы
            # старый код, теряя cf-worker/worker.js со страницы 2.
            return page1
        # Маршрут с &page=1 — pulse_guard.issue_marker_times читает через
        # all_issue_comments (#308/#309, слито параллельно этому PR): та же
        # пагинация, что и у review_labels.list_pages, обход добавляет page=.
        if joined == f"repos/{REPO}/issues/21/comments?per_page=100&page=1":
            return []
        if joined == f"-X PATCH repos/{REPO}/issues/21 -f state=closed":
            return None
        raise AssertionError(f"нет маршрута для: {joined}")

    patch_gh(monkeypatch, fake_gh)
    # deploy_evidence/script_evidence застублены — тест доказывает КЛАССИФИКАЦИЮ
    # (какая из двух вызвана), а не саму проверку улики (та уже покрыта другими
    # тестами deploy/script-класса выше).
    monkeypatch.setattr(sch, "deploy_evidence", lambda repo, merged_at, sha: ("ok", "стаб: деплой"))
    monkeypatch.setattr(
        sch, "script_evidence",
        lambda repo, sha: (_ for _ in ()).throw(AssertionError("script_evidence не должен вызываться — файл cf-worker/ виден только через полный обход страниц")))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    pr = merged_pull(999, "#21\n\nПравка cf-worker/worker.js среди сотни прочих файлов",
                      "headsha999", "2026-09-04T10:00:00Z")
    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой (deploy)" in line and "#21" in line for line in (observations + actions))


# ── /api/health: находки AI-ревью PR #253 (403 без явного UA, таймаут не громкий) ─


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    """Тот же приём, что и _LoginHandler (класс #225): воспроизводим фильтр
    Cloudflare перед мордой по подписи клиента на настоящем сокете, а не наш
    пересказ — библиотечный User-Agent режется 403'м ДО логики приложения."""

    def do_GET(self):
        user_agent = self.headers.get("User-Agent", "")
        if user_agent.startswith("Python-urllib"):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"error code: 1010")
            return
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"version":"0.8.0"}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):  # тише pytest-вывод
        pass


@pytest.fixture()
def health_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def _green_deploy_gh():
    return FakeGh({
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "success", "created_at": "2026-09-02T23:31:31Z",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/1"},
        ]},
    })


def test_deploy_evidence_health_request_carries_explicit_user_agent_past_cf_filter(
    health_server, monkeypatch,
):
    """Находка 1 AI-ревью PR #253: /api/health ходил голым urllib.request без
    UA и получал 403 error code:1010 на живой морде РАНЬШЕ приложения — этот
    тест бьёт по настоящему сокету (не по моку urlopen, который слеп к
    заголовкам) тем же хендлером, что режет Cloudflare. До фикса (запрос без
    _morde_opener) он красный: deploy_evidence вместо 'ok' поднимает
    RuntimeError, потому что 403 конвертируется в него же."""
    port = health_server.server_address[1]
    monkeypatch.setattr(sch, "gh", _green_deploy_gh())
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{port}")

    state, detail = sch.deploy_evidence(REPO, utc(2026, 9, 2, 23, 30, 0), None)

    assert state == "ok"
    assert "/api/health=200" in detail


@pytest.fixture()
def hanging_health_server():
    """Настоящий сокет, который принимает TCP-соединение и молчит — читающая
    сторона получает не connection-refused (это urllib оборачивает в URLError
    сам, до чтения ответа), а socket.timeout ИМЕННО на чтении ответа, тот же
    момент, где сидит находка 2 AI-ревью PR #253."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept_loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conn  # соединение принято и намеренно не закрыто/не отвечено
            except socket.timeout:
                continue
            except OSError:
                break

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    yield port
    stop.set()
    srv.close()
    thread.join(timeout=2)


def test_deploy_evidence_health_timeout_is_wrapped_into_runtime_error(hanging_health_server, monkeypatch):
    """Находка 2 AI-ревью PR #253: socket.timeout (= TimeoutError, не подкласс
    URLError) при чтении /api/health раньше пробивал `except urllib.error.URLError`
    и улетал как есть — тогда per-item `except RuntimeError` в
    accept_merged_tasks его не ловил и ронял весь прогон приёмки (без summary,
    без остальных задач, без очереди слияний). Докстринг deploy_evidence
    обещает RuntimeError на инфраструктурный сбой — таймаут обязан стать им же.
    До фикса (только `except urllib.error.URLError`) этот тест красный: наружу
    улетает голый socket.timeout, а не RuntimeError."""
    monkeypatch.setattr(sch, "gh", _green_deploy_gh())
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{hanging_health_server}")
    monkeypatch.setattr(sch, "DSH_EDGE_HEALTH_TIMEOUT", 0.5)

    with pytest.raises(RuntimeError, match="/api/health недоступен"):
        sch.deploy_evidence(REPO, utc(2026, 9, 2, 23, 30, 0), None)


def test_accept_merged_tasks_health_timeout_is_hard_failure_not_a_crash(hanging_health_server, monkeypatch):
    """Тот же таймаут на уровне интеграции: приёмка не должна уронить весь
    прогон (что случилось бы, утеки socket.timeout из deploy_evidence как
    есть) — она обязана превратить его в жёсткий сбой с эскалацией, как и
    любую другую сломанную возможность, и продолжить обход остальных задач."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/120/comments?per_page=100": [],
        **_green_deploy_gh().routes,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{hanging_health_server}")
    monkeypatch.setattr(sch, "DSH_EDGE_HEALTH_TIMEOUT", 0.5)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("жёсткий сбой не пишет обычный комментарий в задачу"))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177}, open_pulls_list=[])

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)


def test_accept_merged_tasks_does_not_close_on_red_deploy(monkeypatch):
    """Красный deploy-worker.yml (реальный прогон 17:01:31Z этого репозитория,
    conclusion=failure) — задача НЕ закрыта, снят assignee, PATCH не вызван."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/21/assignees": None,
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "failure", "created_at": "2026-09-02T17:01:31Z",
             "html_url": "https://github.com/mytab0r/edge-harness/actions/runs/33658508814"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177}, open_pulls_list=[])

    assert hard_failure is False
    assert any("не закрыта" in line and "#21" in line for line in (observations + actions))
    assert not any(call.startswith("-X PATCH") for call in fake.calls)
    assert any(call.startswith(f"-X DELETE repos/{REPO}/issues/21/assignees") for call in fake.calls)
    assert posted and sch.ACCEPTANCE_FAIL_MARKER in posted[0][1]


def test_accept_merged_tasks_closes_on_green_check_runs(monkeypatch):
    """Скрипт-класс (#18/PR #138 — workflow+scripts, без cf-worker/): зелёные
    check-runs головы PR — тот же критерий, что pr_bad_checks/merge_queue."""
    fake = FakeGh({
        "pulls/138/files": files_payload(PR_138_FILES),
        "issues/18/comments": [],
        "issues/18 -f state=closed": None,
        f"commits/{PR138['head']['sha']}/check-runs?per_page=100": PR_138_CHECK_RUNS,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    pool = [issue(18, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#18" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/18") for call in fake.calls)


def test_accept_merged_tasks_does_not_close_on_red_check_run(monkeypatch):
    red_runs = {"check_runs": [
        {"name": "CodeQL", "conclusion": "success"},
        {"name": "test", "conclusion": "failure"},
    ]}
    fake = FakeGh({
        "pulls/138/files": files_payload(PR_138_FILES),
        "issues/18/comments": [],
        "issues/18/assignees": None,
        f"commits/{PR138['head']['sha']}/check-runs?per_page=100": red_runs,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(18, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])

    assert hard_failure is False
    assert any("провалена" in line and "#18" in line for line in (observations + actions))
    assert not any(call.startswith("-X PATCH") for call in fake.calls)
    assert posted and "test" in posted[0][1]


def test_latest_check_runs_keeps_newest_attempt_per_name():
    """Прод-форма (#467, PR #437, задача #432): `contract` на одном head sha —
    первая попытка failure 04:02:20Z, rerun success 04:03:39Z. Доказательство,
    что дедуп берёт именно вторую (свежую), а не первую по порядку в ответе."""
    runs = [
        {"name": "contract", "conclusion": "failure", "started_at": "2026-09-06T04:02:15Z"},
        {"name": "test", "conclusion": "success", "started_at": "2026-09-06T04:02:15Z"},
        {"name": "contract", "conclusion": "success", "started_at": "2026-09-06T04:03:33Z"},
    ]
    latest = sch.latest_check_runs(runs)
    by_name = {run["name"]: run["conclusion"] for run in latest}
    assert by_name == {"contract": "success", "test": "success"}


def test_bad_check_names_ignores_stale_failed_rerun():
    """Мутация: без дедупа (см. соседний тест) bad_check_names нашла бы
    старую failure-попытку contract и покрасила бы её в список — с дедупом
    список пуст, потому что АКТУАЛЬНОЕ состояние contract зелёное."""
    runs = [
        {"name": "contract", "conclusion": "failure", "started_at": "2026-09-06T04:02:15Z"},
        {"name": "contract", "conclusion": "success", "started_at": "2026-09-06T04:03:33Z"},
    ]
    assert sch.bad_check_names(runs) == []


def test_accept_merged_tasks_closes_when_stale_rerun_failure_superseded_by_success(monkeypatch):
    """Живой случай #467 (PR #437, задача #432): без дедупа приёмка находила
    старую упавшую попытку contract и писала «результат не достигнут», хотя
    PR к моменту прогона приёмки уже был зелёным и слитым."""
    runs_with_stale_failure = {"check_runs": [
        {"name": "CodeQL", "conclusion": "success", "started_at": "2026-09-06T04:02:15Z", "status": "completed"},
        {"name": "contract", "conclusion": "failure", "started_at": "2026-09-06T04:02:15Z", "status": "completed"},
        {"name": "test", "conclusion": "success", "started_at": "2026-09-06T04:02:15Z", "status": "completed"},
        {"name": "contract", "conclusion": "success", "started_at": "2026-09-06T04:03:33Z", "status": "completed"},
    ]}
    fake = FakeGh({
        "pulls/138/files": files_payload(PR_138_FILES),
        "issues/18/comments": [],
        "issues/18 -f state=closed": None,
        f"commits/{PR138['head']['sha']}/check-runs?per_page=100": runs_with_stale_failure,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    pool = [issue(18, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#18" in line for line in (observations + actions))


def test_script_evidence_pending_when_latest_attempt_still_running(monkeypatch):
    """#467, AGENTS.md «fail loud, не silent-wrong»: чек ещё выполняется
    (status=in_progress, conclusion=None) — это «ещё неизвестно», не
    «результат не достигнут». bad_check_names до фикса читал conclusion=None
    как «плохой», и script_evidence вернул бы fail про live-прогон."""
    fake = FakeGh({
        "commits/shaXYZ/check-runs?per_page=100": {"check_runs": [
            {"name": "test", "conclusion": "success", "status": "completed", "started_at": "t1"},
            {"name": "contract", "conclusion": None, "status": "in_progress", "started_at": "t1"},
        ]},
    })
    patch_gh(monkeypatch, fake)

    state, detail = sch.script_evidence(REPO, "shaXYZ")

    assert state == "pending"
    assert "contract" in detail


def test_script_evidence_fail_when_latest_completed_attempt_is_red(monkeypatch):
    """Контроль к предыдущему тесту: завершённый и красный чек — по-прежнему
    fail, различение pending/fail не глотает настоящий провал."""
    fake = FakeGh({
        "commits/shaXYZ/check-runs?per_page=100": {"check_runs": [
            {"name": "test", "conclusion": "success", "status": "completed", "started_at": "t1"},
            {"name": "contract", "conclusion": "failure", "status": "completed", "started_at": "t1"},
        ]},
    })
    patch_gh(monkeypatch, fake)

    state, detail = sch.script_evidence(REPO, "shaXYZ")

    assert state == "fail"
    assert "contract" in detail


def test_accept_merged_tasks_closes_docs_only_with_no_observable_result(monkeypatch):
    """Докс-класс (#78/PR #163 — только openspec/**/*.md): третий, законный
    исход из требований #227 — закрыт с явным обоснованием «улики по природе
    нет», не спутан с deploy/script веткой (свой маркер, своя причина)."""
    fake = FakeGh({
        "pulls/163/files": files_payload(PR_163_FILES),
        "issues/78/comments": [],
        "issues/78 -f state=closed": None,
        **{f"contents/{name}?ref=main": {"name": name.rsplit('/', 1)[-1]} for name in PR_163_FILES},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#78" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/78") for call in fake.calls)
    assert posted and sch.ACCEPTANCE_DOCS_MARKER in posted[0][1]


def test_accept_merged_tasks_does_not_close_docs_when_file_missing_from_main(monkeypatch):
    """Заявленный файл пропал из main (переименован/удалён после мержа) —
    улика (пусть и «улики по природе нет») получить не удалось: провал, не
    тихое закрытие. Форма ошибки — реальная `gh api` на 404 (проверено живым
    вызовом 2026-09-03: `gh: Not Found (HTTP 404)` в stderr)."""
    fake = FakeGh({
        "pulls/163/files": files_payload(PR_163_FILES),
        "issues/78/comments": [],
        "issues/78/assignees": None,
        f"contents/{PR_163_FILES[0]}?ref=main": {"name": "design.md"},
        f"contents/{PR_163_FILES[1]}?ref=main": {"name": "proposal.md"},
        f"contents/{PR_163_FILES[2]}?ref=main": RuntimeError(
            "gh api repos/o/r/contents/x: gh: Not Found (HTTP 404)"),
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163}, open_pulls_list=[])

    assert hard_failure is False
    assert not any(call.startswith("-X PATCH") for call in fake.calls)
    assert any("провалена" in line and "#78" in line for line in (observations + actions))


def test_accept_merged_tasks_closes_docs_when_pr_only_removes_files(monkeypatch):
    """Прод-форма находки AI-ревью PR #253: архивация спеки (`openspec/changes/*`
    → `openspec/specs/`) — это `status=removed` у старых путей плюс `added`/
    `modified` у новых, в одном и том же PR. Отсутствие удалённого файла в
    main — результат самого мержа, а не пропавшая улика: docs_missing не
    должен даже спрашивать про него (тест НЕ кладёт для него gh-маршрут —
    случайный запрос упал бы AssertionError в FakeGh), и задача закрывается
    по добавленному файлу."""
    archive_files = [
        {"filename": "openspec/changes/dsh-edge-plugin-system/proposal.md", "status": "removed"},
        {"filename": "openspec/changes/dsh-edge-plugin-system/design.md", "status": "removed"},
        {"filename": "openspec/specs/dsh-edge-plugin-system.md", "status": "added"},
    ]
    archive_pr = merged_pull(163, "#78\n\nАрхивация спеки плагинов.", "archivesha", "2026-09-04T09:00:00Z")
    fake = FakeGh({
        "pulls/163/files": archive_files,
        "issues/78/comments": [],
        "issues/78 -f state=closed": None,
        "contents/openspec/specs/dsh-edge-plugin-system.md?ref=main": {"name": "dsh-edge-plugin-system.md"},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: archive_pr}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#78" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/78") for call in fake.calls)
    assert posted and sch.ACCEPTANCE_DOCS_MARKER in posted[0][1]
    assert not any("contents/openspec/changes" in call for call in fake.calls)


def test_accept_merged_tasks_docs_missing_escalates_on_non_404_error(monkeypatch):
    """docs_missing не путает «файла нет» (HTTP 404) со «сбой инструмента»
    (ратлимит/сеть/5xx): любой другой отказ gh — не провал улики, а эскалация
    (найдено в разборе AI-ревью PR #253)."""
    fake = FakeGh({
        "pulls/163/files": files_payload(PR_163_FILES),
        "issues/78/comments": [],
        "issues/120/comments?per_page=100": [],
        f"contents/{PR_163_FILES[0]}?ref=main": {"name": "design.md"},
        f"contents/{PR_163_FILES[1]}?ref=main": {"name": "proposal.md"},
        f"contents/{PR_163_FILES[2]}?ref=main": RuntimeError(
            "gh api repos/o/r/contents/x: HTTP 502 (Bad Gateway)"),
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("сбой инструмента не пишет обычный комментарий"))

    pool = [issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163}, open_pulls_list=[])

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)
    assert not any("провалена" in line for line in (observations + actions))


def test_accept_merged_tasks_escalates_hard_failure_without_touching_task(monkeypatch):
    """Возможность ЕСТЬ, но сломана (DSH_EDGE_URL не задан) — не путать с
    «улики нет»: эскалация к владельцу, задача не тронута (не закрыта и не
    возвращена в пул молча под видом провала)."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/120/comments?per_page=100": [],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "success", "created_at": "2026-09-02T23:31:31Z", "html_url": "https://x"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "")  # не задан — деплой-джоб есть, health не проверить
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("жёсткий сбой не пишет обычный комментарий в задачу"))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177}, open_pulls_list=[])

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)


def test_accept_merged_tasks_hard_failure_escalation_is_idempotent(monkeypatch):
    """Находка AI-ревью PR #253: жёсткий сбой (возможность сломана) раньше
    эскалировал на КАЖДОМ пульсе, пока не восстановится — временный HTTP 502
    спамил бы Telegram каждые 15 минут. Маркер уже стоит в WATCHDOG_ISSUE
    (не в задаче — жёсткий сбой её не трогает, см. соседний тест) — второй
    прогон не должен слать Telegram повторно."""
    error_marker = f"{sch.ACCEPTANCE_ERROR_MARKER} #21 PR #177"
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "issues/120/comments?per_page=100": [
            {"created_at": "2026-09-04T18:00:00Z", "body": f"🚨 {error_marker} — уже сказано"},
        ],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": [
            {"conclusion": "success", "created_at": "2026-09-02T23:31:31Z", "html_url": "https://x"},
        ]},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", "")  # не задан — деплой-джоб есть, health не проверить
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("уже эскалировано — не должен слать снова"))
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("жёсткий сбой не пишет обычный комментарий в задачу"))

    pool = [issue(21, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177}, open_pulls_list=[])

    assert hard_failure is True
    assert any("уже эскалировано" in line and "#21" in line for line in (observations + actions))


def test_accept_merged_tasks_stays_quiet_when_pending_within_threshold(monkeypatch):
    """Деплой ещё не прогнан — это норма сразу после мержа, не сбой: тихая
    строка «⏳», без эскалации и без единого мутирующего вызова, пока не
    прошёл ACCEPTANCE_PENDING_HOURS."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("рано эскалировать — порог не прошёл"))
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано писать — порог не прошёл"))

    merged_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    now = merged_at + timedelta(hours=1)  # меньше ACCEPTANCE_PENDING_HOURS
    pr = merged_pull(177, PR_177_BODY, "sha", merged_at.isoformat().replace("+00:00", "Z"))
    pool = [issue(21, assignees=("mytab0r",))]

    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now, open_pulls_list=[])

    assert hard_failure is False
    # #456: «улика ещё не готова» ничего не меняет — наблюдение, не действие.
    assert any("ещё не готова" in line and "#21" in line for line in observations)
    assert actions == []
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)


def test_accept_merged_tasks_escalates_when_pending_past_threshold(monkeypatch):
    """Улика не появилась дольше ACCEPTANCE_PENDING_HOURS после мержа — путь
    назад для merged-задач (reap_stale их больше не трогает), эскалация тем
    же каналом, что жёсткий сбой (найдено в разборе AI-ревью PR #253)."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    merged_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    now = merged_at + timedelta(hours=sch.ACCEPTANCE_PENDING_HOURS, minutes=1)
    pr = merged_pull(177, PR_177_BODY, "sha", merged_at.isoformat().replace("+00:00", "Z"))
    pool = [issue(21, assignees=("mytab0r",))]

    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now, open_pulls_list=[])

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert posted and posted[0][0] == 21 and sch.ACCEPTANCE_PENDING_MARKER in posted[0][1]
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)


def test_accept_merged_tasks_pending_escalation_is_idempotent(monkeypatch):
    """Эскалация зависшего pending уже отправлена этой паре (задача, PR) —
    второй прогон не должен снова слать Telegram/писать комментарий."""
    pending_marker = f"{sch.ACCEPTANCE_PENDING_MARKER} PR #177"
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [{"created_at": "2026-09-03T18:00:00Z", "body": f"🚨 {pending_marker} — уже сказано"}],
        "actions/workflows/deploy-worker.yml/runs?per_page=10": {"workflow_runs": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("уже эскалировано — не должен слать снова"))
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("уже эскалировано — не должен писать снова"))

    merged_at = datetime(2026, 9, 3, 10, 0, 0, tzinfo=timezone.utc)
    now = merged_at + timedelta(hours=sch.ACCEPTANCE_PENDING_HOURS, minutes=1)
    pr = merged_pull(177, PR_177_BODY, "sha", merged_at.isoformat().replace("+00:00", "Z"))
    pool = [issue(21, assignees=("mytab0r",))]

    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now, open_pulls_list=[])

    assert hard_failure is False
    assert any("уже эскалировано" in line and "#21" in line for line in (observations + actions))


def test_accept_merged_tasks_is_idempotent_after_fail_marker_posted(monkeypatch):
    """Провал уже сообщён этой же паре (задача, PR) — второй прогон не должен
    снова дёргать files/check-runs/комментарий: иначе конвейер спамил бы тот
    же результат каждые 15 минут, пока не придёт новая работа."""
    fail_marker = f"{sch.ACCEPTANCE_FAIL_MARKER} PR #138"
    fake = FakeGh({
        "issues/18/comments": [{"created_at": "2026-09-02T22:00:00Z", "body": f"♻️ {fail_marker} — было плохо"}],
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не должен писать повторно"))

    pool = [issue(18, assignees=())]  # уже без исполнителя — как после первого провала
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])

    assert hard_failure is False
    assert (observations + actions) == []
    # только чтение маркера — pulse_guard.all_issue_comments (#308/#309)
    # листает страницы, короткая первая страница останавливает обход сразу.
    assert fake.calls == [f"repos/{REPO}/issues/18/comments?per_page=100&page=1"]


def test_accept_merged_tasks_fail_retries_cleanup_when_assignee_stuck(monkeypatch):
    """Тот же класс, что и у ветки дисклеймера (находка AI-ревью PR #342,
    класс воспроизведён): fail_marker уже стоит, но assignee всё ещё висит —
    прошлый DELETE assignees упал отдельным сбоем. Дедуп по одному лишь
    маркеру ушёл бы молча навсегда с занятым замком; пульс обязан довести
    расчистку, не переспрашивая комментарий провала."""
    fail_marker = f"{sch.ACCEPTANCE_FAIL_MARKER} PR #138"
    fake = FakeGh({
        "issues/18/comments": [{"created_at": "2026-09-02T22:00:00Z", "body": f"♻️ {fail_marker} — было плохо"}],
        "issues/18/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("маркер уже стоит — комментарий не повторяем"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    pool = [issue(18, assignees=("mytab0r",))]  # снятие assignee в прошлый раз не завершилось
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])

    assert hard_failure is False
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/18/assignees") for c in fake.calls)
    assert any("замок task-18 снят" in line for line in (observations + actions))


def test_accept_merged_tasks_ok_close_failure_does_not_stop_the_rest(monkeypatch):
    """Находка AI-ревью PR #253: докстринг accept_merged_tasks обещает «не
    прерывает обход остальных», но post_issue_comment/PATCH в ветке ok/docs
    не были обёрнуты per-item try — один сетевой сбой на #18 ронял бы
    исключением весь остаток пульса, включая #78, до summary(). Тест кормит
    именно это: два независимых слитых task'а, у первого закрытие ломается,
    второй обязан быть обработан как обычно в том же вызове."""
    def flaky_post(repo, n, text):
        if n == 18:
            raise RuntimeError("gh api repos/o/r/issues/18/comments: HTTP 502 (Bad Gateway)")
        posted.append((n, text))

    posted = []
    fake = FakeGh({
        "pulls/138/files": files_payload(PR_138_FILES),
        "issues/18/comments": [],
        f"commits/{PR138['head']['sha']}/check-runs?per_page=100": PR_138_CHECK_RUNS,
        "pulls/163/files": files_payload(PR_163_FILES),
        "issues/78/comments": [],
        "issues/78 -f state=closed": None,
        **{f"contents/{name}?ref=main": {"name": name.rsplit('/', 1)[-1]} for name in PR_163_FILES},
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, flaky_post)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    pool = [issue(18, assignees=("mytab0r",)), issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138, 78: PR163}, open_pulls_list=[])

    assert any("#18" in line and "закрытие приёмкой не завершено" in line for line in (observations + actions))
    assert not any(call.startswith(f"-X PATCH repos/{REPO}/issues/18") for call in fake.calls)
    assert any("закрыта приёмкой" in line and "#78" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/78") for call in fake.calls)
    assert posted and posted[0][0] == 78


def test_accept_merged_tasks_fail_comment_failure_does_not_stop_the_rest(monkeypatch):
    """Тот же класс, что тест выше, но для ветки fail: сбой post_issue_comment
    на проваленной улике одной задачи не должен помешать закрыть следующую
    (успешную) задачу в том же обходе."""
    def flaky_post(repo, n, text):
        if n == 18:
            raise RuntimeError("gh api repos/o/r/issues/18/comments: HTTP 502 (Bad Gateway)")
        posted.append((n, text))

    posted = []
    red_runs = {"check_runs": [{"name": "test", "conclusion": "failure"}]}
    fake = FakeGh({
        "pulls/138/files": files_payload(PR_138_FILES),
        "issues/18/comments": [],
        "issues/18/assignees": None,
        f"commits/{PR138['head']['sha']}/check-runs?per_page=100": red_runs,
        "pulls/163/files": files_payload(PR_163_FILES),
        "issues/78/comments": [],
        "issues/78 -f state=closed": None,
        **{f"contents/{name}?ref=main": {"name": name.rsplit('/', 1)[-1]} for name in PR_163_FILES},
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, flaky_post)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    pool = [issue(18, assignees=("mytab0r",)), issue(78, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138, 78: PR163}, open_pulls_list=[])

    assert any("#18" in line and "отметка провала приёмки не завершена" in line for line in (observations + actions))
    assert not any(call.startswith(f"-X DELETE repos/{REPO}/issues/18") for call in fake.calls)
    assert any("закрыта приёмкой" in line and "#78" in line for line in (observations + actions))
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/78") for call in fake.calls)


def test_accept_merged_tasks_never_closes_watchdog_issue(monkeypatch):
    """#120 (WATCHDOG_ISSUE) — постоянный канал эскалации pulse_guard, не
    разовая задача: PR #126 объявил #120 первой строкой и давно слит с
    зелёными проверками, поэтому merged_pr_map всегда найдёт его. Приёмка
    обязана пропустить #120 без единого вызова gh — иначе первый же прогон
    после мержа PR #253 закрывает канал, в который pulse_guard пишет маркеры
    пауз (найдено в разборе AI-ревью PR #253)."""
    pr126 = merged_pull(126, "#120\n\nПредохранитель конвейера.", "d4676d5", "2026-08-31T12:01:08Z")
    fake = FakeGh({})  # любой вызов gh — провал теста
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(sch.WATCHDOG_ISSUE, assignees=())]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {sch.WATCHDOG_ISSUE: pr126}, open_pulls_list=[])

    assert hard_failure is False
    assert (observations + actions) == []
    assert fake.calls == []
    assert posted == []


def test_accept_merged_tasks_zero_calls_when_no_task_has_merged_pr(monkeypatch):
    """Холостой ход стадии приёмки отдельно от main(): пул непуст, но ни одна
    задача не упомянута ни в одном слитом PR — ни одного вызова gh вовсе
    (не только мутирующего)."""
    fake = FakeGh({})  # любой вызов — AssertionError, доказывает нулевой обход
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не должен писать"))

    pool = [issue(999, assignees=("mytab0r",)), issue(1000, assignees=())]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138}, open_pulls_list=[])  # #18 не в пуле

    assert (observations + actions) == []
    assert hard_failure is False
    assert fake.calls == []


# ── дисклеймер о неполноте в теле PR (#335) — прод-форма реальных тел ────────

# Реальное тело PR #303 (мерж-случай #335): объявляет #297 первой строкой,
# прозой оговаривает прямо противоположное — «эта часть — открытая #297»,
# «#269 остаётся открытой до решения #297».
PR_303_BODY = """#297

## Область (находка ревью, #303): что этот PR закрывает, а что нет

Симптом и «Что сделано» ниже — из #269 (`schedule */15` доставляется ~7%
тиков, готовый PR ждёт часами). Этот PR закрывает fail-loud часть находки:
dispatch пульса больше не глотает не-204 ответ GitHub молча (бейдж и тест-
гвардия отдельно различают «возможности нет», «dispatch/run сломан» и «alarm
подвис» — три разных причины, три разных текста), плюс инвариант «PR готов к
слиянию, но не слит» (`stale_ready_pulls`, `pr_is_merge_ready`).

Главный запрос #269 — «движение конвейера не должно зависеть от такта»,
то есть слияние по событию простановки метки-вердикта, а не по периодике —
этим PR НЕ реализован: job `orchestra` на `pull_request: labeled`
(`.github/workflows/orchestra.yml`) по-прежнему `skipped`
(`if: github.event_name != 'pull_request'`), решение остаётся периодическим
(DO alarm раз в 15 мин вместо GitHub'овского schedule, но всё ещё периодика,
не событие). Эта часть — открытая #297 (её и объявляет первая строка):
«механизм слияний не зависит от канала с измеренной потерей ~93% доставок».
#269 остаётся открытой до решения #297, упомянута здесь прозой, не декларацией.
"""

# Реальное тело PR #159 (мерж-случай #335): объявляет #158, «Propose-фаза,
# кода нет» — диф только спека, реализации нет.
PR_159_BODY = """#158

Propose-фаза, кода нет. Change: `dsh-edge-provider-registry`.

## Пост-мерж проверка

Не применимо: propose-фаза. Реализация — после ворот #1 (вердикт критика файлом
в `openspec/changes/dsh-edge-provider-registry/reviews/`).
"""

# Реальное тело PR #123 (мерж-случай #335): объявляет #112, заголовок и тело
# сами называют правку «стопгэп».
PR_123_BODY = """#112 (стопгэп к транскрипту сессии #119)

Ответ на «как понять, что агент не завис»: пока DSH работает, task.sh шлёт
/api/heartbeat (контракт рук) — свежий heartbeat = «жив, работает».

Пост-мерж: следующий прогон воркера → /api/status показывает свежий heartbeat
worker-…; закрыть стопгэп-часть #112 уликой.
"""


@pytest.mark.parametrize("number,body,marker", [
    (297, PR_303_BODY, "не реализован"),
    (158, PR_159_BODY, "propose-фаза"),
    (112, PR_123_BODY, "стопгэп"),
])
def test_partial_disclaimer_finds_marker_in_real_pr_bodies(number, body, marker):
    assert sch.partial_disclaimer(body, number) == marker


def test_partial_disclaimer_none_when_no_marker():
    assert sch.partial_disclaimer(PR_177_BODY, 21) is None


def test_partial_disclaimer_matches_body_without_yo():
    """Находка AI-ревью PR #342: живые тела PR пишут «перенесен» без «ё» —
    это норма написания, не опечатка. Маркер объявлен с «ё» («перенесён в #»)
    — без нормализации сравнение молча не находит совпадение, и приёмка тихо
    закрывает задачу вопреки дисклеймеру (тот самый класс, который #335 чинит)."""
    body = "#10\n\nЭта часть перенесен в #11, докрытие отдельным PR."
    assert sch.partial_disclaimer(body, 10) == "перенесён в #"


def test_partial_disclaimer_matches_second_yo_marker_without_yo():
    """Тот же класс, второй маркер с «ё» в списке («остаётся открыт») —
    без нормализации падал бы так же молча, как и «перенесён в #»."""
    body = "#10\n\nЧасть работы остается открытой до решения #11."
    assert sch.partial_disclaimer(body, 10) == "остаётся открыт"


# Реальное тело PR #455 (задача #454, живой ложноположительный случай #467):
# критерий #454 (ранний отказ по квоте GitHub API) выполнен и доказан тестами
# тем же телом PR — абзац с «не реализован» описывает РАССМОТРЕННУЮ И
# ОТКЛОНЁННУЮ альтернативу другой, не заявленной здесь работы (дешёвая
# предпроверка перед сканом orchestra), не критерий #454.
PR_455_BODY = """#454

Живые случаи 2026-09-06: PR #428 — оба обязательных гейта (`test`,
`contract`) упали с `API rate limit exceeded for installation`.

## Что сделано

`scripts/lib/rate_guard.py` — читает `.resources.core` и решает, пропускать
ли дорогой путь. Подключено в три точки.

## Что НЕ сделано в этом PR и почему

- **Дешёвая предпроверка «есть ли кандидат на слияние» перед полным сканом
  orchestra** — рассмотрено и НЕ реализовано. Причины: (а) mark_conflicts/
  unhealthy_pulls/stale_ready_pulls делают независимую полезную работу; (б)
  единственный найденный чистый дубль — scheduler.py уже правится параллельно,
  трогать его core merge-логику ещё раз в этом же PR — риск коллизии без
  согласования; оставляю как конкретную находку для отдельной задачи.
- **Регулярный вызов `scripts/measure/quotas.py`** — этот файл существует
  только в PR #327, который в статусе CONFLICTING и не смёржен в main.

## Проверено

`python -m pytest scripts/lib/test_rate_guard.py -q` — 13/13, включая мутацию
границы порога и мутацию кода возврата — оба раза тест красился.
"""

# Реальное тело PR #382 (задача #370, живой ИСТИННО положительный случай
# #467): #370 сам требует явного решения владельца из нескольких вариантов
# независимо от улик — абзац «не реализовано» тоже не упоминает #370, но не
# несёт признака отклонённой альтернативы (это открытые предложения на
# будущее, не отказ от чего-то другого) — дисклеймер обязан остаться в силе,
# приёмка не должна закрыть #370 автоматом.
PR_382_BODY = """#370

## Решение

`.github/workflows/branch-protection-watch.yml` — триггер branch_protection_rule.

## Смежные события (не реализовано, для отдельного обсуждения)

- workflow_run/settings изменение прав Actions — нет штатного события уровня
  репозитория для этого.
- Удаление ветки — есть событие delete, можно так же кричать при удалении
  main/защищённых веток без опроса.
- Ротация/изменение секретов и vars репозитория — штатного события нет.

Не делаю ничего из списка — один объём работы за раз, как и просили.
"""


def test_partial_disclaimer_suppresses_rejected_alternative_not_own_criterion():
    """Живой случай #467: PR #455 (задача #454) — маркер «не реализован»
    относится к рассмотренной-и-отклонённой альтернативе другой работы, не к
    критерию #454. Доказано мутацией соседним тестом
    (test_partial_disclaimer_keeps_true_positive_without_rejection_signal),
    где то же «нет #N в абзаце» без сигнала отклонения маркер НЕ гасит."""
    assert sch.partial_disclaimer(PR_455_BODY, 454) is None


def test_partial_disclaimer_keeps_true_positive_without_rejection_signal():
    """Живой случай #467: PR #382 (задача #370) — абзац тоже не упоминает
    #370, но не несёт сигнала рассмотренной-отклонённой альтернативы отдельно
    от этого — дисклеймер обязан остаться в силе (#370 требует явного решения
    владельца независимо от улик). Доказывает, что подавление зависит именно
    от ОБОИХ условий разом, не только от отсутствия ссылки на свою задачу."""
    assert sch.partial_disclaimer(PR_382_BODY, 370) == "не реализован"


@pytest.mark.parametrize("number,body", [
    (297, PR_303_BODY),
    (158, PR_159_BODY),
    (112, PR_123_BODY),
])
def test_accept_merged_tasks_does_not_close_when_body_has_partial_disclaimer(monkeypatch, number, body):
    """Прод-форма #335: приёмка не закрывает задачу, если тело закрывающего
    PR несёт дисклеймер о неполноте, даже когда первая строка объявляет её.
    Доказано мутацией: без partial_disclaimer (см. следующий тест) приёмка
    ушла бы к классификации (`pulls/900/files`), которого здесь нет —
    маршрут отсутствует нарочно, чтобы такой обход провалил тест.

    Находка AI-ревью PR #342: раньше ветка дисклеймера оставляла только
    комментарий и задача зависала — assignee не снят, замок не освобождён,
    задача недостижима ни воркером, ни повторной приёмкой. Теперь ветка
    зеркалит `fail`: снимает assignee и вызывает `claim_task.release`."""
    pr = merged_pull(900, body, "sha900", "2026-09-04T10:00:00Z")
    fake = FakeGh({
        f"issues/{number}/comments": [],
        f"issues/{number}/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(number, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {number: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert not any(f"#{number}" in line and "закрыта приёмкой" in line for line in (observations + actions))
    assert posted and posted[0][0] == number
    assert "требует проверки человеком" in posted[0][1]
    assert "возвращена в пул" in posted[0][1]
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/{number}/assignees") for c in fake.calls)
    assert any(f"замок task-{number} снят" in line for line in (observations + actions))


def test_accept_merged_tasks_partial_disclaimer_removed_closes_as_before(monkeypatch):
    """Гвардия мутацией (#335): то же тело PR #303, но без дисклеймера
    (последний абзац с «остаётся открытой»/«НЕ реализован» вырезан) — приёмка
    обязана закрыть #297 обычным путём, доказывая, что закрытие блокировала
    именно фраза, а не что-то ещё в теле."""
    body_without_disclaimer = PR_303_BODY.split("Главный запрос #269")[0]
    assert sch.partial_disclaimer(body_without_disclaimer, 297) is None
    pr = merged_pull(900, body_without_disclaimer, "sha900", "2026-09-04T10:00:00Z")
    fake = FakeGh({
        "pulls/900/files": files_payload(["scripts/orchestra/scheduler.py"]),
        "issues/297/comments": [],
        "issues/297 -f state=closed": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "script_evidence", lambda repo, sha: ("ok", "стаб"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    pool = [issue(297, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {297: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#297" in line for line in (observations + actions))


def test_accept_merged_tasks_partial_disclaimer_is_idempotent(monkeypatch):
    """Маркер уже стоит на этой паре (задача, PR), assignee уже снят прошлым
    пульсом (расчистка завершена) — второй пульс не должен писать комментарий
    повторно и не должен трогать assignee/замок вовсе (тот же приём, что и у
    fail_marker/pending)."""
    pr = merged_pull(900, PR_303_BODY, "sha900", "2026-09-04T10:00:00Z")
    fake = FakeGh({
        "issues/297/comments": [{"body": f"{sch.ACCEPTANCE_PARTIAL_MARKER} PR #900 …",
                                  "created_at": "2026-09-04T11:00:00Z"}],
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не должен писать повторно"))

    pool = [issue(297, assignees=())]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {297: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert (observations + actions) == []
    assert fake.calls == [f"repos/{REPO}/issues/297/comments?per_page=100&page=1"]


def test_accept_merged_tasks_partial_disclaimer_retries_cleanup_when_assignee_stuck(monkeypatch):
    """Класс, воспроизведённый в предыдущей правке этой же ветки (находка
    AI-ревью PR #342): маркер «требует проверки человеком» уже стоит, но
    DELETE assignees в прошлом пульсе упал отдельным HTTP-сбоем — assignee
    всё ещё висит на задаче. Дедуп по одному лишь факту маркера ушёл бы молча
    навсегда, оставив исполнителя и замок claim_task. Пульс обязан ДОВЕСТИ
    расчистку, не переспрашивая комментарий."""
    pr = merged_pull(900, PR_303_BODY, "sha900", "2026-09-04T10:00:00Z")
    fake = FakeGh({
        "issues/297/comments": [{"body": f"{sch.ACCEPTANCE_PARTIAL_MARKER} PR #900 …",
                                  "created_at": "2026-09-04T11:00:00Z"}],
        "issues/297/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("маркер уже стоит — комментарий не повторяем"))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    pool = [issue(297, assignees=("mytab0r",))]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {297: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/297/assignees") for c in fake.calls)
    assert any("замок task-297 снят" in line for line in (observations + actions))


# ── эпик (#335) — родительскую задачу приёмка не закрывает автоматом ────────


def test_is_epic_issue_true_by_title_prefix():
    """#115/#116/#77/#17 — все начинаются с «ЭПИК» (проверено фактом на
    живых issues репозитория); #115 при этом имеет нулевой
    sub_issues_summary — заголовок остаётся единственным сигналом."""
    assert sch.is_epic_issue(issue(115, title="ЭПИК: интеграции внешних систем — Atlassian…",
                                    sub_issues_summary={"total": 0, "completed": 0}))


def test_is_epic_issue_true_by_open_sub_issues():
    assert sch.is_epic_issue(issue(77, title="", sub_issues_summary={"total": 8, "completed": 6}))


def test_is_epic_issue_false_for_plain_task():
    assert not sch.is_epic_issue(issue(21, title="dev:docker в cf-worker",
                                        sub_issues_summary={"total": 0, "completed": 0}))


def test_accept_merged_tasks_never_closes_epic(monkeypatch):
    """Прод-форма #335: PR #123 объявляет #112 первой строкой, #112 сам не
    эпик — но ЭПИК #115 из того же аудита валит прод-деплой, и здесь
    проверяем именно ветку «эпик» отдельно от ветки «дисклеймер»: тело без
    маркера, задача помечена эпиком по заголовку — приёмка обязана пропустить
    её без единого вызова gh (тот же приём, что и WATCHDOG_ISSUE)."""
    pr = merged_pull(400, "#115\n\nПлагин из эпика.", "sha400", "2026-09-04T10:00:00Z")
    fake = FakeGh({})  # любой вызов — провал теста
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("эпик не комментируется приёмкой"))

    pool = [issue(115, assignees=(), title="ЭПИК: интеграции внешних систем")]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {115: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert (observations + actions) == []
    assert fake.calls == []


def test_accept_merged_tasks_marks_epic_completed_when_all_sub_issues_closed(monkeypatch):
    """Находка AI-ревью PR #342: «эпики приёмка не трогает никогда» — тормоз
    без газа, если `sub_issues_summary.total == completed` (все дочерние
    задачи закрыты) и никто никогда не узнает, что эпик готов к ручному
    закрытию. Автозакрытие остаётся запрещённым (issue не закрывается,
    только комментарий-маркер)."""
    pr = merged_pull(400, "#77\n\nПлагин из эпика.", "sha400", "2026-09-04T10:00:00Z")
    fake = FakeGh({"issues/77/comments": []})
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    pool = [issue(77, assignees=(), title="ЭПИК: интеграции внешних систем",
                  sub_issues_summary={"total": 8, "completed": 8})]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {77: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)
    assert posted and posted[0][0] == 77
    assert sch.ACCEPTANCE_EPIC_MARKER in posted[0][1]
    assert "ручного закрытия" in posted[0][1]
    assert any(sch.ACCEPTANCE_EPIC_MARKER in line for line in (observations + actions))


def test_accept_merged_tasks_epic_completed_marker_is_idempotent(monkeypatch):
    """Маркер завершённого эпика уже стоит — второй пульс не должен писать
    комментарий повторно (тот же приём дедупа, что и у остальных маркеров)."""
    pr = merged_pull(400, "#77\n\nПлагин из эпика.", "sha400", "2026-09-04T10:00:00Z")
    fake = FakeGh({
        "issues/77/comments": [{"body": f"{sch.ACCEPTANCE_EPIC_MARKER} …",
                                 "created_at": "2026-09-04T11:00:00Z"}],
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не должен писать повторно"))

    pool = [issue(77, assignees=(), title="ЭПИК: интеграции внешних систем",
                  sub_issues_summary={"total": 8, "completed": 8})]
    observations, actions, hard_failure = sch.accept_merged_tasks(REPO, pool, {77: pr}, open_pulls_list=[])

    assert hard_failure is False
    assert (observations + actions) == []


# ── reap_stale (#227): слитая-но-непринятая задача — не «PR не появился» ────────


def test_reap_stale_skips_task_covered_by_merged_pr(monkeypatch):
    """Мутация видна прямым сравнением с тестом ниже: без параметра merged
    reap_stale снял бы assignee с #21, хотя PR #177 давно слит — именно этот
    класс дал часть замера #227 (assigned=False у #192/#189/#187…)."""
    old_assigned = [{"event": "assigned", "created_at": "2026-08-01T00:00:00Z"}]
    task = issue(21, assignees=("mytab0r",))
    fake = FakeGh({
        "issues/21/timeline?per_page=100": old_assigned,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.now(timezone.utc)

    lines = sch.reap_stale(REPO, now, [], merged={21: PR177}, pool=[task])

    assert lines == []
    assert fake.mutating_calls() == []


def test_reap_stale_still_reaps_when_not_covered_by_merged_pr(monkeypatch):
    """Контроль к тесту выше (доказательство мутацией без правки прод-кода):
    то же самое назначение, но #21 отсутствует в merged — старое поведение
    (снять assignee, «PR не появился») обязано сработать. Если бы guard в
    reap_stale был снят/сломан, этот тест остался бы зелёным, а тест выше —
    покраснел бы: пара тестов вместе и есть доказательство мутацией."""
    old_assigned = [{"event": "assigned", "created_at": "2026-08-01T00:00:00Z"}]
    task = issue(21, assignees=("mytab0r",))
    fake = FakeGh({
        "issues/21/timeline?per_page=100": old_assigned,
        "issues/21/assignees": None,
        "issues/21/comments": None,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.now(timezone.utc)

    lines = sch.reap_stale(REPO, now, [], merged={}, pool=[task])

    assert len(lines) == 1 and "просрочена" in lines[0]
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/21/assignees") for c in fake.calls)
    # (#443) снятие назначения обязано отразиться сразу в переданном pool —
    # дальнейшие потребители того же снимка (unhealthy_pulls/accept) не
    # обязаны перечитывать issue с GitHub, чтобы увидеть это же изменение.
    assert task["assignees"] == []


def test_reap_stale_reaps_reassignment_after_failed_acceptance(monkeypatch):
    """Гвард из теста выше сравнивает время НАЗНАЧЕНИЯ со временем мержа, не
    сам факт «номер когда-то встречался в merged» (замечание AI-ревью, PR
    #253): PR #177 слит и приёмка его провалила, задачу отдали новому
    исполнителю ПОСЛЕ мержа — если этот воркер умер, не открыв PR, reap
    обязан снять просроченное назначение как обычно. merged всё ещё содержит
    старый PR #177 (он не перестаёт быть слитым), но новое assigned позже
    merged_at — старый гвард (`if number in merged: continue`) держал бы
    задачу «в работе» навечно без диспетча и без сигнала."""
    new_assigned = [{"event": "assigned", "created_at": "2026-09-10T00:00:00Z"}]
    task = issue(21, assignees=("mytab0r",))
    fake = FakeGh({
        "issues/21/timeline?per_page=100": new_assigned,
        "issues/21/assignees": None,
        "issues/21/comments": None,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.fromisoformat("2026-09-10T00:00:00+00:00") + timedelta(hours=sch.STALE_HOURS, minutes=1)

    lines = sch.reap_stale(REPO, now, [], merged={21: PR177}, pool=[task])

    assert len(lines) == 1 and "просрочена" in lines[0]
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/21/assignees") for c in fake.calls)
