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


def issue(number, *, assignees=("someone",)):
    return {
        "number": number,
        "assignees": [{"login": a} for a in assignees],
        "labels": [{"name": "task"}],
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


# ── Поведение 3: после слияния — подтянуть остальных ─────────────────────────────


def test_update_remaining_pulls_updates_all_open_others(monkeypatch):
    others = [pull(2), pull(3), pull(4, draft=True)]
    fake = FakeGh({
        "pulls/2/update-branch": None,
        "pulls/3/update-branch": None,
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.delenv("ORCHESTRA_PAT", raising=False)

    lines = sch.update_remaining_pulls(REPO, 1, others)

    update_calls = [c for c in fake.calls if "update-branch" in c]
    assert len(update_calls) == 2  # #4 — draft, пропущен; #1 — сам слитый, не в others
    assert any("2" in c for c in update_calls) and any("3" in c for c in update_calls)
    assert len(lines) == 2
    assert all("обновлён из main" in line for line in lines)


def test_update_remaining_pulls_reports_conflict_loudly_not_silently(monkeypatch):
    others = [pull(2)]

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
    })
    patch_gh(monkeypatch, fake)
    monkeypatch.setattr(sch.claim_task, "collect_stale", lambda repo, now: [])

    code = sch.main()

    assert code == 0
    assert fake.mutating_calls() == [], f"холостой ход дёрнул состояние: {fake.mutating_calls()}"
