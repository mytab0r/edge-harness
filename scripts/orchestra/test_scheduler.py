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
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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
    # дрейф пина (#134) здесь не предмет теста — гасим, как остальные механизмы
    monkeypatch.setattr(sch, "upstream_drift_lines", lambda repo: [])
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
        """Вызовы, меняющие состояние (POST/PUT/DELETE) — не GET."""
        return [c for c in self.calls if c.startswith(("-X POST", "-X PUT", "-X DELETE"))]


REPO = "mytab0r/edge-harness"


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


# ── Поведение 3: после слияния — подтянуть остальных, но выборочно (#252) ────────
# Раньше update_remaining_pulls дёргал update-branch для ВСЕХ открытых недрафт
# PR — каждый такой push синхронизирует pr-review.yml и снимает валидные
# ai:*-метки (замер #252: 142 прогона ai-review.yml за 14.5 ч). Предикат
# review_labels.should_update_branch — одно место правды, что подтягивать
# стоит: оба вердикта зелёные (близок к слиянию) или конфликт (подтягивание
# может его расшить).


def test_update_remaining_pulls_updates_only_close_to_merge_or_conflict(monkeypatch):
    others = [
        pull(2, labels=["review:ok", "ai:ok"]),         # оба вердикта — подтянуть
        pull(3, labels=["conflict"]),                     # конфликт — подтянуть
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
    assert len(update_calls) == 2
    assert any("pulls/2/update-branch" in c for c in update_calls)
    assert any("pulls/3/update-branch" in c for c in update_calls)
    updated_lines = [line for line in lines if "обновлён из main" in line]
    skipped_lines = [line for line in lines if "не подтянут" in line]
    assert len(updated_lines) == 2
    assert len(skipped_lines) == 2
    assert any("#4" in line for line in skipped_lines)
    assert any("#5" in line for line in skipped_lines)


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
    monkeypatch.setattr(sch, "open_pulls", lambda repo: [])
    monkeypatch.setattr(sch, "reap_stale", lambda repo, now, pulls: [])
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])
    monkeypatch.setattr(sch, "mark_conflicts", lambda repo, pulls: [])
    monkeypatch.setattr(sch, "merge_queue", lambda repo, pulls: ([], False))
    monkeypatch.setattr(sch, "open_task_issues", lambda repo: [issue(89, assignees=())])
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
