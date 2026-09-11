#!/usr/bin/env python3
"""Тесты sweep осиротевших сессий раннера (#940).

`classify_sessions` — чистая функция, тестируется прод-формой (то, что
реально несёт `harness-<N>`/`harness-<N>-r<run>`, — конвенция
scripts/worker/task.sh:472/478, не наш пересказ). Логин/RPC — тот же
поведенческий приём, что test_scheduler.py::login_server (настоящий HTTP-
сервер, контракт 303 + Set-Cookie + фильтр User-Agent), не мок текста.

Доказательство мутацией (ручной прогон, дословный вывод — в отчёте):
  - заменить `state == "CLOSED"` на `state != "OPEN"` — `test_none_state_is_kept_not_orphaned`
    красный: неопределённый статус (None) начинает считаться сиротой.
  - убрать guard `SESSION_ID_RE.match` (архивировать всё подряд) —
    `test_non_harness_session_is_ignored` красный.
  - в `archive_session` убрать ветку "session-not-found считается успехом" —
    `test_archive_session_treats_not_found_as_soft_success` красный.
  - в `archive_session` вернуть обобщённую подстроку "already" как признак
    успеха (находка ревью PR #944: мягкий успех маскировал поломку) —
    `test_archive_session_failure_containing_word_already_is_hard_failure`
    красный: поломка с "already" в тексте снова считается успехом.

Запуск: python -m pytest scripts/orchestra/test_session_orphan_sweep.py -q
"""

import http.server
import importlib.util
import sys
import threading
from pathlib import Path

import pytest
import yaml

_DIR = Path(__file__).resolve().parent
SCRIPT = _DIR / "session_orphan_sweep.py"
spec = importlib.util.spec_from_file_location("session_orphan_sweep", SCRIPT)
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)  # type: ignore[union-attr]


# ── classify_sessions: чистая функция ────────────────────────────────────────


def _item(session_id=None, id_=None, title=""):
    d = {"projections": {"values": {"title": title}}}
    if session_id is not None:
        d["sessionId"] = session_id
    if id_ is not None:
        d["id"] = id_
    return d


def test_closed_task_session_is_an_orphan():
    items = [_item(session_id="harness-42", title="#42: чинить X")]
    stats = sweep.classify_sessions(items, lambda n: "CLOSED")
    assert stats["orphans"] == [("harness-42", 42)]
    assert stats["kept"] == []


def test_open_task_session_is_kept():
    items = [_item(session_id="harness-42", title="#42: чинить X")]
    stats = sweep.classify_sessions(items, lambda n: "OPEN")
    assert stats["orphans"] == []
    assert stats["kept"] == [("harness-42", 42, "задача открыта (state=OPEN)")]


def test_fallback_run_suffix_session_is_recognised_and_closed_is_orphan():
    # Класс #809/#871: холодная загрузка испорчена -> harness-N-r<run_id>.
    items = [_item(session_id="harness-140-r34471287514", title="#140: X")]
    stats = sweep.classify_sessions(items, lambda n: "CLOSED")
    assert stats["orphans"] == [("harness-140-r34471287514", 140)]


def test_none_state_is_kept_not_orphaned():
    # Не удалось определить статус (gh недоступен) — fail loud/безопасно, не гадаем.
    # Причина обязана называть именно неопределённость статуса, а не
    # "задача открыта (state=None)" — иначе тест не поймает регресс, где
    # ветку `state is None` убрали и None провалился в общий else.
    items = [_item(session_id="harness-7", title="#7: X")]
    stats = sweep.classify_sessions(items, lambda n: None)
    assert stats["orphans"] == []
    assert stats["kept"] == [("harness-7", 7, "статус задачи не определён — не трогаем")]


def test_non_harness_session_is_ignored():
    items = [_item(session_id="some-other-session", title="не наше")]
    stats = sweep.classify_sessions(items, lambda n: "CLOSED")
    assert stats["orphans"] == []
    assert stats["not_harness"] == 1


def test_item_without_any_id_field_is_unparseable_not_crashed():
    items = [_item(session_id=None, title="#9: X")]  # ни sessionId, ни id
    stats = sweep.classify_sessions(items, lambda n: "CLOSED")
    assert stats["unparseable"] == 1
    assert stats["orphans"] == []


def test_id_field_fallback_when_session_id_absent():
    items = [_item(id_="harness-11", title="#11: X")]
    stats = sweep.classify_sessions(items, lambda n: "CLOSED")
    assert stats["orphans"] == [("harness-11", 11)]


# ── orchestra.yml: шаг sweep гейтится тем же quota-skip, что соседи ──────────
# (находка ревью PR #944: шаг стоял БЕЗ `if: steps.quota.outputs.skip !=
# 'true'`, в отличие от двух соседних шагов той же job — при исчерпанной
# квоте sweep всё равно запускался бы и тратил остаток лимита).


def test_orchestra_yml_gates_orphan_sweep_step_on_quota_skip():
    """Мутация: убери `if:` у шага «Sweep осиротевших сессий морды» в
    orchestra.yml (или измени условие) — тест краснеет."""
    workflow_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "orchestra.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    steps = None
    for job in jobs.values():
        candidate = [s for s in job.get("steps", []) if s.get("id") == "session_orphan_sweep"]
        if candidate:
            steps = job["steps"]
            break
    assert steps is not None, "шаг session_orphan_sweep не найден в orchestra.yml"
    sweep_step = next(s for s in steps if s.get("id") == "session_orphan_sweep")
    assert sweep_step.get("if") == "steps.quota.outputs.skip != 'true'"


# ── run_sweep: квота/сломанный gh не маскируется под честное «сирот нет» ─────
# (находка ревью PR #944: issue_state молчит None на КАЖДЫЙ номер при
# исчерпанной квоте — без отдельного счётчика прогон печатал бы "0 сирот"
# неотличимо от честной пустой очереди).


def test_run_sweep_distinguishes_undetermined_status_from_no_orphans(monkeypatch, capsys):
    monkeypatch.setattr(sweep, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sweep, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sweep, "_morde_opener", lambda: object())
    monkeypatch.setattr(sweep, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sweep, "fetch_session_list",
        lambda opener: [_item(session_id="harness-42", title="#42: X")],
    )

    import subprocess

    def fake_run(*args, **kwargs):
        # rc!=0 — тот же признак, что живой gh при исчерпанной квоте/сети.
        return type("R", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = sweep.run_sweep(dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0  # ничего не сломалось явно — но и не честное "сирот нет"
    assert "0 сирот" in out
    assert "1 статус не определён" in out


def test_run_sweep_warns_loudly_when_no_harness_status_resolved(monkeypatch, capsys):
    """Мутация: убери условие `undetermined == harness_count` (замени на
    `False`) — предупреждение не печатается, тест краснеет."""
    monkeypatch.setattr(sweep, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sweep, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sweep, "_morde_opener", lambda: object())
    monkeypatch.setattr(sweep, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sweep, "fetch_session_list",
        lambda opener: [
            _item(session_id="harness-1", title="#1: X"),
            _item(session_id="harness-2", title="#2: X"),
        ],
    )

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    sweep.run_sweep(dry_run=True)
    err = capsys.readouterr().err
    assert "НИ ОДНОЙ harness-сессии не определился" in err


def test_run_sweep_no_false_warning_when_orphans_genuinely_found(monkeypatch, capsys):
    monkeypatch.setattr(sweep, "DSH_EDGE_URL", "http://morde.invalid")
    monkeypatch.setattr(sweep, "DSH_EDGE_ACCESS_KEY", "key")
    monkeypatch.setattr(sweep, "_morde_opener", lambda: object())
    monkeypatch.setattr(sweep, "_morde_login", lambda opener: None)
    monkeypatch.setattr(
        sweep, "fetch_session_list",
        lambda opener: [_item(session_id="harness-42", title="#42: X")],
    )

    import subprocess

    def fake_run(*args, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "CLOSED"})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    sweep.run_sweep(dry_run=True)
    err = capsys.readouterr().err
    assert "НИ ОДНОЙ harness-сессии не определился" not in err


# ── archive_session: мягкий успех на session-not-found ───────────────────────


def test_archive_session_treats_not_found_as_soft_success(monkeypatch):
    def fake_rpc(opener, method, payload):
        raise RuntimeError("session-not-found: no such session")

    monkeypatch.setattr(sweep, "_morde_rpc", fake_rpc)
    ok, message = sweep.archive_session(object(), "harness-1")
    assert ok is True
    assert "не активна" in message


def test_archive_session_failure_containing_word_already_is_hard_failure(monkeypatch):
    # Находка ревью PR #944: мягкий успех по подстроке "already" маскировал
    # поломку — любой отказ, кроме точного кода session-not-found, обязан
    # остаться отказом, даже если текст содержит слово "already".
    def fake_rpc(opener, method, payload):
        raise RuntimeError("internal-error: session already locked by another operation")

    monkeypatch.setattr(sweep, "_morde_rpc", fake_rpc)
    ok, message = sweep.archive_session(object(), "harness-1")
    assert ok is False
    assert "internal-error" in message


def test_archive_session_hard_failure_is_reported(monkeypatch):
    def fake_rpc(opener, method, payload):
        raise RuntimeError("internal-error: DO unavailable")

    monkeypatch.setattr(sweep, "_morde_rpc", fake_rpc)
    ok, message = sweep.archive_session(object(), "harness-1")
    assert ok is False
    assert "internal-error" in message


def test_archive_session_success(monkeypatch):
    calls = []

    def fake_rpc(opener, method, payload):
        calls.append((method, payload))
        return {}

    monkeypatch.setattr(sweep, "_morde_rpc", fake_rpc)
    ok, message = sweep.archive_session(object(), "harness-1")
    assert ok is True
    assert calls == [("workspace.archiveSession", {"sessionId": "harness-1"})]


# ── Логин: тот же контракт, что scheduler.py (303 + Set-Cookie + UA-фильтр) ──


class _LoginHandler(http.server.BaseHTTPRequestHandler):
    status = 303

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
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

    def log_message(self, *a):
        pass


@pytest.fixture()
def login_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _LoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def test_sweep_login_303_succeeds_without_following_redirect(login_server, monkeypatch):
    _LoginHandler.status = 303
    port = login_server.server_address[1]
    monkeypatch.setattr(sweep, "DSH_EDGE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(sweep, "DSH_EDGE_ACCESS_KEY", "key")
    opener = sweep._morde_opener()
    sweep._morde_login(opener)  # не должен бросить


def test_sweep_login_without_explicit_user_agent_is_blocked_by_cf_filter(login_server):
    # Доказательство, что addheaders в _morde_opener — не декоративный: без
    # него дефолтный urllib UA ловит 403 ДО приложения (класс #225), тот же
    # контрольный эксперимент, что test_scheduler.py::
    # test_morde_opener_without_explicit_user_agent_is_blocked_by_cf_filter.
    import urllib.error
    import urllib.parse
    import urllib.request

    port = login_server.server_address[1]
    bare_opener = urllib.request.build_opener(
        sweep._NoRedirect, urllib.request.HTTPCookieProcessor())
    data = urllib.parse.urlencode({"accessKey": "key"}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auth/login", data=data, method="POST")
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        bare_opener.open(req, timeout=5)
    assert excinfo.value.code == 403


def test_sweep_opener_sets_explicit_user_agent_past_cf_filter():
    opener = sweep._morde_opener()
    assert ("User-Agent", sweep.MORDE_USER_AGENT) in opener.addheaders
    assert not sweep.MORDE_USER_AGENT.startswith("Python-urllib")
