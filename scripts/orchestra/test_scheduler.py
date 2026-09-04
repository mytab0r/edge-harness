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


# ── main(): жёсткий сбой красит прогон ПОСЛЕ мержа, эскалирует одним каналом ─────


def test_main_exits_nonzero_and_escalates_on_archive_hard_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    # дрейф пина (#134) здесь не предмет теста — гасим, как остальные механизмы
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: (["✅ PR #1 слит"], True))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None: ([], False))
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
    # дрейф пина (#134) здесь не предмет теста — гасим, как остальные механизмы
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: (["✅ PR #1 слит"], False))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None: ([], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool: [])
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    monkeypatch.setattr(sch, "escalate", lambda *a: pytest.fail("не должен эскалировать — сбоя не было"))
    assert sch.main() == 0


def test_main_exits_nonzero_when_acceptance_hard_failure(monkeypatch):
    """#227: жёсткий сбой приёмки (не архива сессий) тоже красит прогон — своя
    ветка, независимая от archive_hard_failure."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: ([], False))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [])
    monkeypatch.setattr(
        sch, "accept_merged_tasks",
        lambda repo, pool, merged, now=None: (["🚨 #227: улика не проверена"], True))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: ([], True))
    monkeypatch.setattr(sch, "dispatch_worker", lambda repo, pool: [])
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    monkeypatch.setattr(sch, "escalate", lambda *a: "ок")
    assert sch.main() == 1


# pulse_guard живёт как отдельный модуль (sys.modules["pulse_guard"], тот же
# файл, что импортирует scheduler.py через `from pulse_guard import …`).
# issue_marker_times/post_issue_comment внутри pulse_guard вызывают СВОЙ
# module-level gh — патчить нужно оба модуля разом (см. patch_gh ниже),
# иначе часть вызовов уходит в настоящий `gh api` подпроцесс.
pg = sys.modules["pulse_guard"]


def patch_gh(monkeypatch, fake):
    """Единая точка патча: и scheduler.gh (для прямых вызовов scheduler.py),
    и pulse_guard.gh (для issue_marker_times/post_issue_comment/conveyor_gate/
    heartbeat_check, которые scheduler лишь реэкспортирует по имени)."""
    monkeypatch.setattr(sch, "gh", fake)
    monkeypatch.setattr(pg, "gh", fake)


def patch_post_issue_comment(monkeypatch, fn):
    monkeypatch.setattr(sch, "post_issue_comment", fn)
    monkeypatch.setattr(pg, "post_issue_comment", fn)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def label(name):
    """Прод-форма элемента labels[] (GitHub API, снято с открытых PR репозитория)."""
    return {"id": "LA_kwDOUHBaqc8AAAACypPLSQ", "name": name, "description": "", "color": "0E8A16"}


def pull(number, *, labels=(), draft=False, updated_at="2026-09-02T12:00:00Z", pr_body=""):
    return {
        "number": number,
        "draft": draft,
        "labels": [label(n) for n in labels],
        "updated_at": updated_at,
        "body": pr_body,
        "head": {"sha": f"sha{number}"},
    }


def issue(number, *, assignees=("someone",), labels=("task",)):
    return {
        "number": number,
        "assignees": [{"login": a} for a in assignees],
        "labels": [{"name": n} for n in labels],
    }


class FakeGh:
    """Маршрутизатор вызовов gh api по подстроке пути; каждый вызов пишется —
    для гвардии холостого хода это и есть доказательство "ни одного вызова"."""

    def __init__(self, routes: dict):
        self.routes = routes
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
    fake = FakeGh({"issues?state=open&labels=task": [task]})
    patch_gh(monkeypatch, fake)
    lines = sch.reap_stale(REPO, utc(2026, 9, 2, 12, 0), [])
    assert lines == []
    assert not any("timeline" in c for c in fake.calls)


# ── Поведение 1: готовый PR без вердикта — дёрнуть гейт самому ───────────────────


def timeline_with_review_ok(when: str):
    return [
        {"event": "labeled", "label": {"name": "review:ok"}, "created_at": when},
        {"event": "labeled", "label": {"name": "review:large"}, "created_at": when},
    ]


def test_trigger_ai_review_dispatches_after_threshold_no_verdict(monkeypatch):
    p = pull(163, labels=["review:ok"])
    fake = FakeGh({
        "issues/163/timeline": timeline_with_review_ok("2026-09-02T11:00:00Z"),
        "issues/163/comments": [],  # прод-форма: голый массив без маркеров попыток
        "ai-review.yml/dispatches": None,  # 204 без тела — прод-форма ответа dispatch
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))

    now = utc(2026, 9, 2, 11, 45)  # 45 мин > порог 30
    lines = sch.trigger_ai_review(REPO, now, [p])

    dispatch_calls = [c for c in fake.calls if "ai-review.yml/dispatches" in c]
    assert len(dispatch_calls) == 1
    assert "inputs[pr]=163" in dispatch_calls[0]
    assert any("163" in line and "ai-review.yml запущен" in line for line in lines)
    assert posted and posted[0][0] == 163
    assert sch.AI_REVIEW_RETRY_MARKER in posted[0][1]


def test_trigger_ai_review_dispatches_on_ai_failed(monkeypatch):
    p = pull(178, labels=["review:ok", "ai:failed"])
    fake = FakeGh({
        "issues/178/timeline": timeline_with_review_ok("2026-09-01T22:37:09Z"),
        "issues/178/comments": [],
        "ai-review.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)

    now = utc(2026, 9, 1, 23, 30)  # больше порога с момента labeled review:ok
    lines = sch.trigger_ai_review(REPO, now, [p])
    assert any("ai-review.yml/dispatches" in c for c in fake.calls)
    assert any("178" in line for line in lines)


def test_trigger_ai_review_silent_before_threshold(monkeypatch):
    p = pull(163, labels=["review:ok"])
    fake = FakeGh({"issues/163/timeline": timeline_with_review_ok("2026-09-02T11, 40:00Z".replace(", ", ":"))})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано — не должен писать"))

    now = utc(2026, 9, 2, 11, 50)  # 10 мин < порог 30
    lines = sch.trigger_ai_review(REPO, now, [p])
    assert lines == []
    assert not any("dispatches" in c for c in fake.calls)


def test_trigger_ai_review_silent_when_verdict_already_ok(monkeypatch):
    p = pull(181, labels=["review:ok", "ai:ok"])
    fake = FakeGh({})  # ни один маршрут не должен понадобиться
    patch_gh(monkeypatch, fake)
    lines = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []
    assert fake.calls == []  # даже таймлайн не читаем — решение принято по меткам


def test_trigger_ai_review_silent_when_changes_requested(monkeypatch):
    # ai:changes-requested — вердикт ЕСТЬ, это не "нет вердикта": ждём человека/
    # доработку, не повторяем ai-review сами (это забота unhealthy_pulls).
    p = pull(999, labels=["review:ok", "ai:changes-requested"])
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []
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
        "issues/163/timeline": timeline_with_review_ok("2026-09-02T09:00:00Z"),
        "issues/163/comments": comments,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("лимит попыток исчерпан — не пишем"))

    now = utc(2026, 9, 2, 12, 0)
    lines = sch.trigger_ai_review(REPO, now, [p])
    assert not any("dispatches" in c for c in fake.calls)  # квота не жжётся дальше
    assert any("нужен человек" in line for line in lines)


# ── Мутация гвардии поведения 1: без гейта на review:ok — дёргает всё подряд ─────


def test_trigger_ai_review_mutation_without_review_ok_gate_would_fire_on_anything(monkeypatch):
    # Доказательство того, что гейт "review:ok in labels" в проде необходим:
    # PR без review:ok вообще не должен рассматриваться (иначе дёрнули бы
    # ai-review на любом свежем PR, включая черновики и PR без ревью).
    p = pull(555, labels=["review:large"])  # review:ok нет
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []
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
        "issues?state=open&labels=task": [task],
        "commits/sha201/check-runs": CHECK_RUNS_RED,
        "issues/200/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    posted = []
    patch_post_issue_comment(monkeypatch, lambda repo, n, text: posted.append((n, text)))
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: f"замок task-{n} снят")

    now = utc(2026, 9, 2, 12, 0)  # 180 мин > порог 120
    lines = sch.unhealthy_pulls(REPO, now, [p])

    assert any("assignees" in c and "-X DELETE" in c for c in fake.calls)
    assert any("возвращена в пул" in line and "201" in line for line in lines)
    assert posted and posted[0][0] == 200
    assert "#201" in posted[0][1]
    assert "не переделывай" in posted[0][1]


def test_unhealthy_pulls_returns_task_on_ai_changes_requested(monkeypatch):
    task = issue(210)
    p = pull(211, labels=["review:ok", "ai:changes-requested"],
              updated_at="2026-09-02T09:00:00Z", pr_body="#210")
    fake = FakeGh({
        "issues?state=open&labels=task": [task],
        "commits/sha211/check-runs": CHECK_RUNS_GREEN,  # чек зелёный — причина не в нём
        "issues/210/assignees": None,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: None)
    monkeypatch.setattr(sch.claim_task, "release", lambda repo, n: "ok")

    now = utc(2026, 9, 2, 12, 0)
    lines = sch.unhealthy_pulls(REPO, now, [p])
    assert any("ai:changes-requested" in line for line in lines)


def test_unhealthy_pulls_silent_before_threshold(monkeypatch):
    task = issue(220)
    p = pull(221, labels=["review:ok"], updated_at="2026-09-02T11:30:00Z", pr_body="#220")
    fake = FakeGh({
        "issues?state=open&labels=task": [task],
        "commits/sha221/check-runs": CHECK_RUNS_RED,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("рано — не пишем"))

    now = utc(2026, 9, 2, 12, 0)  # 30 мин < порог 120
    lines = sch.unhealthy_pulls(REPO, now, [p])
    assert lines == []
    assert not any("-X DELETE" in c for c in fake.calls)


def test_unhealthy_pulls_silent_when_pr_green(monkeypatch):
    task = issue(230)
    p = pull(231, labels=["review:ok"], updated_at="2026-09-02T08:00:00Z", pr_body="#230")
    fake = FakeGh({
        "issues?state=open&labels=task": [task],
        "commits/sha231/check-runs": CHECK_RUNS_GREEN,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("PR здоров — не пишем"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []


def test_unhealthy_pulls_idempotent_after_release_no_assignee(monkeypatch):
    # После освобождения задачи assignees пуст — тот же приём, что у reap_stale:
    # follow-up вызов не действует повторно (не дублирует комментарий/снятие).
    task = issue(240, assignees=())
    p = pull(241, labels=["review:ok"], updated_at="2026-09-02T08:00:00Z", pr_body="#240")
    fake = FakeGh({
        "issues?state=open&labels=task": [task],
        "commits/sha241/check-runs": CHECK_RUNS_RED,
    })
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("не назначена — не трогаем"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
    assert lines == []
    # issue без assignees отфильтрован ДО чтения check-runs/мутирующих вызовов:
    # единственный вызов — список задач пула, которым unhealthy_pulls начинает.
    assert fake.calls == ["repos/mytab0r/edge-harness/issues?state=open&labels=task&per_page=100"]


def test_unhealthy_pulls_skips_conflict_labeled_pr(monkeypatch):
    # conflict — отдельный класс (mark_conflicts), unhealthy_pulls не дублирует.
    task = issue(250)
    p = pull(251, labels=["review:ok", "conflict"], updated_at="2026-09-02T08:00:00Z", pr_body="#250")
    fake = FakeGh({"issues?state=open&labels=task": [task]})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("conflict — не наш класс"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
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
    fake = FakeGh({"issues?state=open&labels=task": [task]})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("blocked — газ только у владельца"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [p])
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
# готовности (позже из двух событий 'labeled' review:ok/ai:ok в таймлайне).


def timeline_ready(review_at: str, ai_at: str):
    return [
        {"event": "labeled", "label": {"name": "review:ok"}, "created_at": review_at},
        {"event": "labeled", "label": {"name": "ai:ok"}, "created_at": ai_at},
    ]


def test_stale_ready_pulls_escalates_when_ready_longer_than_threshold(monkeypatch):
    p = pull(301, labels=["review:ok", "ai:ok"])
    fake = FakeGh({
        "pulls/301": {"mergeable_state": "clean"},
        "commits/sha301/check-runs": CHECK_RUNS_GREEN,
        "issues/301/timeline": timeline_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
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
        "issues/302/timeline": timeline_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
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
    assert not any("timeline" in c for c in fake.calls)  # готовность даже не проверяем


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
        "issues/305/timeline": timeline_ready("2026-09-02T08:00:00Z", "2026-09-02T08:05:00Z"),
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
        "issues/301/timeline": timeline_ready("2026-09-02T06:00:00Z", "2026-09-02T06:00:00Z"),  # готов 08:00
        "issues/302/timeline": timeline_ready("2026-09-02T06:30:00Z", "2026-09-02T06:30:00Z"),  # готов 08:30
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

    lines = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 1
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert not any("pulls/3/update-branch" in c for c in fake.calls)  # слот уже занят #2
    updated_lines = [line for line in lines if "обновлён из main" in line]
    not_close_lines = [line for line in lines if "не подтянут" in line]
    # Находка AI-ревью PR #288: строка обязана называть ПРИЧИНУ (слот занят
    # другим PR), а не приписывать #3 несостоявшееся обновление — #3 сам не
    # обновлён, слот занял #2.
    slot_taken_lines = [
        line for line in lines
        if "слот update_branch" in line and "занят другим PR" in line
    ]
    assert len(updated_lines) == 1 and "#2" in updated_lines[0]
    assert len(not_close_lines) == 3  # #3 (слот занят), #4, #5 — не близки к слиянию
    assert any("#4" in line for line in not_close_lines)
    assert any("#5" in line for line in not_close_lines)
    assert len(slot_taken_lines) == 1 and "#3" in slot_taken_lines[0]  # не молчит, назван следующий прогон
    assert "следующий прогон" in slot_taken_lines[0]
    assert not any("уже обновлён" in line for line in lines)  # #3 сам не обновлён


def test_update_remaining_pulls_draft_skipped_before_predicate(monkeypatch):
    # Драфт отсеивается раньше should_update_branch — даже с зелёными
    # вердиктами его не трогаем (см. merge_queue: драфт не сливается никогда).
    others = [pull(4, draft=True, labels=["review:ok", "ai:ok"])]
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.update_remaining_pulls(REPO, 1, others)
    assert lines == []
    assert fake.calls == []


def test_update_remaining_pulls_skip_is_not_silent(monkeypatch):
    others = [pull(4, labels=["review:ok"])]  # нет ai:ok — не близок к слиянию
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.update_remaining_pulls(REPO, 1, others)
    assert fake.calls == []  # update-branch не вызван вовсе — газ не тратим впустую
    assert len(lines) == 1
    assert "#4" in lines[0] and "не подтянут" in lines[0]


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

    lines = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 2, "обе попытки обязаны произойти — неудача не занимает слот"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("pulls/3/update-branch" in c for c in update_calls)
    failed_lines = [line for line in lines if "не обновлён" in line]
    updated_lines = [line for line in lines if line.startswith("🔄")]
    assert len(failed_lines) == 1 and "#2" in failed_lines[0]
    assert len(updated_lines) == 1 and "#3" in updated_lines[0]


def test_update_remaining_pulls_reports_conflict_loudly_not_silently(monkeypatch):
    others = [pull(2, labels=["conflict"])]  # конфликт проходит предикат — попытка будет

    def broken(repo, n):
        raise RuntimeError("422 Merge conflict")

    monkeypatch.setattr(sch, "update_branch", broken)
    lines = sch.update_remaining_pulls(REPO, 1, others)
    assert len(lines) == 1
    assert "не обновлён" in lines[0] and "422 Merge conflict" in lines[0]
    assert "mark_conflicts" in lines[0]  # видимая причина, не молчание


def test_update_remaining_pulls_excludes_just_merged_and_empty_is_noop(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.update_remaining_pulls(REPO, 1, [pull(1)])  # единственный "other" == merged
    assert lines == []
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


def test_update_branch_or_report_pat_set_called_process_error_includes_stderr(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_PAT", "test-pat-token")

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
        on_error="#2 — update_branch не удался: {error}",
    )

    assert "Update is not a fast forward" in line
    assert "HTTP 422" in line


def test_update_branch_or_report_pat_set_success_uses_subprocess(monkeypatch):
    # Контроль: PAT задан — успех тоже обязан идти через subprocess.run
    # (не gh()), иначе тест выше проверял бы ветку, которая в проде не
    # используется вовсе.
    monkeypatch.setenv("ORCHESTRA_PAT", "test-pat-token")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(sch.subprocess, "run", fake_run)

    line = sch.update_branch_or_report(
        REPO, 2, on_success="✅", on_budget_exhausted="budget", on_error="{error}",
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

    lines, hard_failure = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert not any("update-branch" in c for c in fake.calls)
    assert any("не близок к слиянию" in line for line in lines)


def test_merge_queue_behind_close_to_merge_updates(monkeypatch):
    pulls = [pull(2, labels=["review:ok", "ai:ok"])]
    fake = FakeGh({"pulls/2/update-branch": None, "pulls/2": {"mergeable_state": "behind"}})
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    lines, hard_failure = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    assert any("pulls/2/update-branch" in c for c in fake.calls)
    assert any("обновлена из main" in line for line in lines)


def test_merge_queue_behind_conflict_updates_even_without_verdicts(monkeypatch):
    pulls = [pull(2, labels=["conflict"])]
    fake = FakeGh({"pulls/2/update-branch": None, "pulls/2": {"mergeable_state": "behind"}})
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    lines, hard_failure = sch.merge_queue(REPO, pulls)

    assert not hard_failure
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

    lines, hard_failure = sch.merge_queue(REPO, pulls)

    assert not hard_failure
    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 1, "два behind-PR за один прогон не должны дать два update-branch"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("слот update_branch" in line and "#3" in line for line in lines)


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

    lines, hard_failure = sch.merge_queue(REPO, pulls)  # не должно кинуть исключение

    assert not hard_failure
    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 2, "сбой на #2 не должен остановить обход — #3 обязан получить попытку"
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("pulls/3/update-branch" in c for c in update_calls)
    failed_lines = [line for line in lines if "не удался" in line]
    updated_lines = [line for line in lines if "обновлена из main" in line]
    assert len(failed_lines) == 1 and "#2" in failed_lines[0] and "dial tcp" in failed_lines[0]
    assert len(updated_lines) == 1 and "#3" in updated_lines[0]


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


# ── Пагинация таймлайна: тот же класс, тесно в один хелпер (#303, находка
# ревью) — last_review_ok_labeled_at и last_ready_labeled_at читали сырую
# первую страницу timeline?per_page=100 без обхода, событие 'labeled' за
# первой сотней молча терялось на длинном таймлайне ────────────────────────


def test_last_review_ok_and_last_ready_read_timeline_through_paginated_helper():
    # Гвардия по исходнику (тот же приём, что для after_merge/list_pr_files
    # выше): обе функции обязаны ходить через review_labels.list_timeline
    # (полный обход постранично), а не читать сырую первую страницу —
    # поведенческая проверка самой пагинации живёт в
    # scripts/lib/test_review_labels.py::test_list_timeline_paginates_finds_event_beyond_first_page
    # (мутация доказана там: обход убран — тест краснеет).
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count("review_labels.list_timeline(repo, pr_number, gh)") == 2
    assert 'gh(f"repos/{repo}/issues/{pr_number}/timeline?per_page=100")' not in source


def test_last_ready_labeled_at_finds_label_beyond_first_page_of_timeline(monkeypatch):
    # Поведенческое доказательство на уровне вызывающей функции: labeled-события
    # обеих меток-гейтов лежат за первой страницей (100 посторонних событий
    # перед ними) — без полного обхода last_ready_labeled_at вернул бы None.
    page1 = [{"event": "commented", "created_at": "2026-08-01T00:00:00Z"} for _ in range(100)]
    page2 = [
        {"event": "labeled", "label": {"name": "review:ok"}, "created_at": "2026-09-01T00:00:00Z"},
        {"event": "labeled", "label": {"name": "ai:ok"}, "created_at": "2026-09-01T01:00:00Z"},
    ]
    fake = FakeGh({
        "timeline?per_page=100&page=1": page1,
        "timeline?per_page=100&page=2": page2,
    })
    patch_gh(monkeypatch, fake)
    result = sch.last_ready_labeled_at(REPO, 999)
    assert result == utc(2026, 9, 1, 1, 0)  # позже из двух — labeled ai:ok, найдено на второй странице


# ── Мутация гвардии поведения 3: без вызова update-branch список пуст ────────────


def test_after_merge_wires_update_remaining_pulls(monkeypatch):
    # after_merge обязан прокинуть other_pulls в update_remaining_pulls — иначе
    # поведение 3 реализовано, но не вызывается ниоткуда (мертвый код).
    merged = pull(1)
    other = pull(2)
    monkeypatch.setattr(sch, "gh", FakeGh({"pulls/1/files": []}))
    calls = []
    monkeypatch.setattr(sch, "update_remaining_pulls",
                         lambda repo, merged_number, others: calls.append((merged_number, others)) or [])
    sch.after_merge(REPO, merged, [other])
    assert calls == [(1, [other])]


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
    lines = sch.trigger_ai_review(REPO, utc(2026, 9, 2, 12, 0), [])
    assert lines == []
    assert fake.calls == []


def test_unhealthy_pulls_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({"issues?state=open&labels=task": []})
    patch_gh(monkeypatch, fake)
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("пустая очередь — писать некуда"))
    lines = sch.unhealthy_pulls(REPO, utc(2026, 9, 2, 12, 0), [])
    assert lines == []
    assert fake.mutating_calls() == []


def test_update_remaining_pulls_noop_on_empty_queue(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    lines = sch.update_remaining_pulls(REPO, 1, [])
    assert lines == []
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


def test_dispatch_worker_fires_once_for_idle_worker_and_free_pool(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        # POST .../dispatches отвечает 204 без тела — прод-форма «успех» есть None.
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    pool = [issue(95), issue(89, assignees=())]
    lines = sch.dispatch_worker(REPO, pool)
    assert lines == [
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
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": None,
    })
    patch_gh(monkeypatch, fake)
    pool = [issue(101, assignees=()), issue(95), issue(89, assignees=())]
    lines = sch.dispatch_worker(REPO, pool)
    assert lines[0].startswith("👷 свободная задача #89 ")
    assert not any("#101" in line for line in lines)


def test_dispatch_worker_silent_when_pool_has_no_free_task(monkeypatch):
    fake = FakeGh({})
    patch_gh(monkeypatch, fake)
    assert sch.dispatch_worker(REPO, [issue(89)]) == []
    assert fake.calls == []  # ноль вызовов вовсе: на занятый пул даже статусы не смотрим


def test_dispatch_worker_silent_while_worker_run_in_progress(monkeypatch):
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {
            "workflow_runs": [workflow_run(33814313381, "in_progress")]},
    })
    patch_gh(monkeypatch, fake)
    lines = sch.dispatch_worker(REPO, [issue(89, assignees=())])
    assert lines == ["👷 воркер уже работает — dispatch не нужен"]
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
    lines = sch.dispatch_worker(REPO, [issue(89, assignees=())])
    assert lines == ["👷 воркер уже работает — dispatch не нужен"]
    assert fake.mutating_calls() == []


def test_dispatch_worker_survives_dispatch_failure(monkeypatch):
    # Best-effort по построению: 403/сеть на диспатче не роняют планировщик —
    # слияния важнее подряда воркеру. Доведение функции до возврата и есть
    # проверка: исключение ушло бы дальше этого assert.
    fake = FakeGh({
        "workflows/worker.yml/runs?status=in_progress": {"workflow_runs": []},
        "workflows/worker.yml/runs?status=queued": {"workflow_runs": []},
        "workflows/worker.yml/dispatches": RuntimeError(
            "gh api repos/o/r/actions/workflows/worker.yml/dispatches: HTTP 403"),
    })
    patch_gh(monkeypatch, fake)
    lines = sch.dispatch_worker(REPO, [issue(89, assignees=())])
    assert len(lines) == 1 and lines[0].startswith("⚠️ dispatch воркера не удался")


def test_main_skips_worker_dispatch_while_fuse_paused(monkeypatch):
    """Предохранитель конвейера (#120) в паузе → dispatch_worker не вызывается
    вовсе (проводка в main, не внутри dispatch_worker)."""
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr(sch, "heartbeat_check", lambda repo, now: [])
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "all_merged_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "merged_pr_map", lambda pulls: {})
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls, merged=None: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: ([], False))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [issue(89, assignees=())])
    monkeypatch.setattr(sch, "accept_merged_tasks", lambda repo, pool, merged, now=None: ([], False))
    monkeypatch.setattr(sch, "conveyor_gate", lambda repo, now: (["⏸️ пауза диспатча"], False))
    dispatched = []
    monkeypatch.setattr(
        sch, "dispatch_worker",
        lambda repo, pool: dispatched.append((repo, pool)) or [],
    )
    monkeypatch.setattr(sch, "summary", lambda lines: None)
    assert sch.main() == 0
    assert dispatched == []


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
        "workflows/orchestra.yml/runs": {"workflow_runs": [
            {"conclusion": "success", "created_at": recent_success_iso,
             "html_url": "https://x", "display_title": "x"}]},
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
            {"name": "dsh-edge-v0.8.0",
             "commit": {"sha": "b9a8ddd6cd11bc0db94d3f67bbc7de4d674e69a1", "url": "https://x"}},
            {"name": "dsh-edge-v0.7.1",
             "commit": {"sha": "113a96913c51881993122afbf42e776882c4beb7", "url": "https://x"}},
        ],
        "issues/134": {"number": 134, "labels": []},
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])

    code = sch.main()

    assert code == 0
    assert fake.mutating_calls() == [], f"холостой ход дёрнул состояние: {fake.mutating_calls()}"


# ── Приёмка (#227): задача закрывается только по проверяемой улике ──────────────
#
# Фикстуры ниже — реальные ответы `gh api` по этому репозиторию (снято
# 2026-09-03): PR #138 (задача #18, "#18\n\n…" — тело объявляет задачу первой
# строкой, как требует task_ref.declared_tasks), PR #177 (#21), PR #163 (#78).
# На момент инцидента #227 все три были слиты, а задачи оставались открытыми
# без исполнителя (reap_stale успел снять assignee по неверной причине «PR не
# появился») — сейчас #18/#21/#78 уже закрыты вручную, но тела PR и списки
# файлов ниже — то, что реально видел бы этот код в момент инцидента.

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


def merged_pull(number, body, head_sha, merged_at, merge_commit_sha=None):
    return {"number": number, "state": "closed", "merged_at": merged_at,
            "body": body, "head": {"sha": head_sha}, "labels": [],
            "merge_commit_sha": merge_commit_sha}


def files_payload(names):
    return [{"filename": name} for name in names]


PR138 = merged_pull(138, PR_138_BODY, "aaebecbb6adf816be99ab76ab30c3de796e3ff89", "2026-09-02T21:31:47Z")
PR177 = merged_pull(177, PR_177_BODY, "67b23c9fb1bd43984c3a734bed569f0ae01a8d3c", "2026-09-02T17:01:28Z")
PR163 = merged_pull(163, PR_163_BODY, "fc3e8b2ba2422e81ab23a7ebc2948d84d1f71650", "2026-09-02T20:46:31Z")


def test_classify_acceptance_deploy_when_cf_worker_touched():
    assert sch.classify_acceptance(PR_177_FILES) == sch.ACCEPT_DEPLOY


def test_classify_acceptance_script_for_workflow_and_scripts():
    assert sch.classify_acceptance(PR_138_FILES) == sch.ACCEPT_SCRIPT


def test_classify_acceptance_docs_when_only_openspec_md():
    assert sch.classify_acceptance(PR_163_FILES) == sch.ACCEPT_DOCS


def test_merged_pr_map_uses_declared_task_not_prose_mention():
    # Тело PR #138 объявляет #18 первой строкой ("#18\n\n…") — узкая семантика
    # declared_tasks, симметричная contract_check при авто-назначении.
    mapping = sch.merged_pr_map([PR138, PR177, PR163])
    assert mapping[18]["number"] == 138
    assert mapping[21]["number"] == 177
    assert mapping[78]["number"] == 163
    assert 999 not in mapping


def test_merged_pr_map_keeps_most_recent_merge_for_same_task():
    older = merged_pull(1, "#5\n\nстарая работа", "sha1", "2026-01-01T00:00:00Z")
    newer = merged_pull(2, "#5\n\nновая работа поверх старой", "sha2", "2026-02-01T00:00:00Z")
    mapping = sch.merged_pr_map([older, newer])
    assert mapping[5]["number"] == 2


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177})

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#21" in line for line in lines)
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/21") for call in fake.calls)
    assert posted and "улика получена" in posted[0][1]


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr_first})

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#21" in line for line in lines)
    assert any(call.startswith(f"-X PATCH repos/{REPO}/issues/21") for call in fake.calls)
    assert posted and "улика получена" in posted[0][1]


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
        **_green_deploy_gh().routes,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch, "DSH_EDGE_URL", f"http://127.0.0.1:{hanging_health_server}")
    monkeypatch.setattr(sch, "DSH_EDGE_HEALTH_TIMEOUT", 0.5)
    escalated = []
    monkeypatch.setattr(sch, "escalate", lambda repo, issue_n, text: escalated.append((repo, issue_n, text)) or "ок")
    patch_post_issue_comment(monkeypatch, lambda *a: pytest.fail("жёсткий сбой не пишет обычный комментарий в задачу"))

    pool = [issue(21, assignees=("mytab0r",))]
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177})

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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177})

    assert hard_failure is False
    assert any("не закрыта" in line and "#21" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138})

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#18" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138})

    assert hard_failure is False
    assert any("провалена" in line and "#18" in line for line in lines)
    assert not any(call.startswith("-X PATCH") for call in fake.calls)
    assert posted and "test" in posted[0][1]


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163})

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#78" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163})

    assert hard_failure is False
    assert not any(call.startswith("-X PATCH") for call in fake.calls)
    assert any("провалена" in line and "#78" in line for line in lines)


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: archive_pr})

    assert hard_failure is False
    assert any("закрыта приёмкой" in line and "#78" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {78: PR163})

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)
    assert not any("провалена" in line for line in lines)


def test_accept_merged_tasks_escalates_hard_failure_without_touching_task(monkeypatch):
    """Возможность ЕСТЬ, но сломана (DSH_EDGE_URL не задан) — не путать с
    «улики нет»: эскалация к владельцу, задача не тронута (не закрыта и не
    возвращена в пул молча под видом провала)."""
    fake = FakeGh({
        "pulls/177/files": files_payload(PR_177_FILES),
        "issues/21/comments": [],
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: PR177})

    assert hard_failure is True
    assert escalated and escalated[0][1] == sch.WATCHDOG_ISSUE
    assert not any(call.startswith(("-X PATCH", "-X DELETE")) for call in fake.calls)


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

    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now)

    assert hard_failure is False
    assert any("ещё не готова" in line and "#21" in line for line in lines)
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

    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now)

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

    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {21: pr}, now)

    assert hard_failure is False
    assert any("уже эскалировано" in line and "#21" in line for line in lines)


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138})

    assert hard_failure is False
    assert lines == []
    assert fake.calls == [f"repos/{REPO}/issues/18/comments?per_page=100"]  # только чтение маркера


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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138, 78: PR163})

    assert any("#18" in line and "закрытие приёмкой не завершено" in line for line in lines)
    assert not any(call.startswith(f"-X PATCH repos/{REPO}/issues/18") for call in fake.calls)
    assert any("закрыта приёмкой" in line and "#78" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138, 78: PR163})

    assert any("#18" in line and "отметка провала приёмки не завершена" in line for line in lines)
    assert not any(call.startswith(f"-X DELETE repos/{REPO}/issues/18") for call in fake.calls)
    assert any("закрыта приёмкой" in line and "#78" in line for line in lines)
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {sch.WATCHDOG_ISSUE: pr126})

    assert hard_failure is False
    assert lines == []
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
    lines, hard_failure = sch.accept_merged_tasks(REPO, pool, {18: PR138})  # #18 не в пуле

    assert lines == []
    assert hard_failure is False
    assert fake.calls == []


# ── reap_stale (#227): слитая-но-непринятая задача — не «PR не появился» ────────


def test_reap_stale_skips_task_covered_by_merged_pr(monkeypatch):
    """Мутация видна прямым сравнением с тестом ниже: без параметра merged
    reap_stale снял бы assignee с #21, хотя PR #177 давно слит — именно этот
    класс дал часть замера #227 (assigned=False у #192/#189/#187…)."""
    old_assigned = [{"event": "assigned", "created_at": "2026-08-01T00:00:00Z"}]
    fake = FakeGh({
        "issues?state=open&labels=task": [issue(21, assignees=("mytab0r",))],
        "issues/21/timeline?per_page=100": old_assigned,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.now(timezone.utc)

    lines = sch.reap_stale(REPO, now, [], merged={21: PR177})

    assert lines == []
    assert fake.mutating_calls() == []


def test_reap_stale_still_reaps_when_not_covered_by_merged_pr(monkeypatch):
    """Контроль к тесту выше (доказательство мутацией без правки прод-кода):
    то же самое назначение, но #21 отсутствует в merged — старое поведение
    (снять assignee, «PR не появился») обязано сработать. Если бы guard в
    reap_stale был снят/сломан, этот тест остался бы зелёным, а тест выше —
    покраснел бы: пара тестов вместе и есть доказательство мутацией."""
    old_assigned = [{"event": "assigned", "created_at": "2026-08-01T00:00:00Z"}]
    fake = FakeGh({
        "issues?state=open&labels=task": [issue(21, assignees=("mytab0r",))],
        "issues/21/timeline?per_page=100": old_assigned,
        "issues/21/assignees": None,
        "issues/21/comments": None,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.now(timezone.utc)

    lines = sch.reap_stale(REPO, now, [], merged={})

    assert len(lines) == 1 and "просрочена" in lines[0]
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/21/assignees") for c in fake.calls)


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
    fake = FakeGh({
        "issues?state=open&labels=task": [issue(21, assignees=("mytab0r",))],
        "issues/21/timeline?per_page=100": new_assigned,
        "issues/21/assignees": None,
        "issues/21/comments": None,
    })
    patch_gh(monkeypatch, fake)
    now = datetime.fromisoformat("2026-09-10T00:00:00+00:00") + timedelta(hours=sch.STALE_HOURS, minutes=1)

    lines = sch.reap_stale(REPO, now, [], merged={21: PR177})

    assert len(lines) == 1 and "просрочена" in lines[0]
    assert any(c.startswith(f"-X DELETE repos/{REPO}/issues/21/assignees") for c in fake.calls)
