#!/usr/bin/env python3
"""Тесты атомарной аренды задачи (#121, scripts/lib/claim_task.py).

Сеть не нужна: gh-вызовы подменены на уровне subprocess.run — семантика
прод-формы сохранена (код возврата gh, stderr в форме «gh: HTTP 422: …»,
JSON как у GitHub API). Гонка двух claim воспроизводится общим состоянием
«сервера»: POST нового ref отклоняется, если ref уже существует.

Запуск: python -m pytest scripts/lib/test_claim_task.py -q
"""

import contextlib
import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("claim_task.py")
spec = importlib.util.spec_from_file_location("claim_task", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)  # type: ignore[union-attr]


def utc(h, m=0):
    return datetime(2026, 8, 31, h, m, tzinfo=timezone.utc)


def utc_plus(hours, minutes=0):
    return datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hours, minutes=minutes)


def out(payload):
    return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def fail(status, message="ошибка"):
    return SimpleNamespace(returncode=1, stdout="", stderr=f"gh: HTTP {status}: {message}")


def ok_no_body():
    # 204/201 без тела — валидный ответ gh api (POST dispatches, DELETE refs)
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeServer:
    """gh api на моке subprocess.run: маршруты по подстроке пути + состояние
    refs (POST существующего → 422), как это серверно делает GitHub."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls = []
        self.existing_refs: set[str] = set()

    def add_ref(self, ref):
        self.existing_refs.add(ref)

    def run(self, args, capture_output=True, text=True, env=None):
        joined = " ".join(args)
        self.calls.append(joined)
        if "-X" in args and "POST" in args and "git/refs" in joined and "matching-refs" not in joined:
            # POST /git/refs: атомарное создание ref'а — второй претендент отклонён
            ref = next(a.split("=", 1)[1] for a in args if a.startswith("ref="))
            if ref in self.existing_refs:
                return fail(422, "Reference already exists")
            self.existing_refs.add(ref)
            return ok_no_body()
        if "-X" in args and "DELETE" in args and "git/refs" in joined:
            return ok_no_body()
        for fragment, payload in self.routes.items():
            if fragment in joined:
                return payload if isinstance(payload, SimpleNamespace) else out(payload)
        raise AssertionError(f"нет маршрута: {joined}")


def install(monkeypatch, server: FakeServer) -> FakeServer:
    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=server.run))
    return server


BASE = {
    "repos/o/r/commits/main": {
        "sha": "basesha", "commit": {"tree": {"sha": "treasha"}, "committer": {"date": "2026-08-31T11:00:00Z"}}},
    "repos/o/r/git/commits": {
        "sha": "locksha", "commit": {"committer": {"date": "2026-08-31T12:00:00Z"}}},
    "issues/5/assignees": ok_no_body(),
    "issues/5/comments": ok_no_body(),
    # Проверка на входе (claim не выдаёт аренду на закрытую задачу): дефолт
    # для всех тестов, использующих BASE, — задача открыта. Порядок ключей
    # важен для FakeServer.run (substring-роутинг, первое совпадение
    # выигрывает): этот ключ идёт ПОСЛЕ issues/5/assignees и issues/5/comments,
    # иначе он перехватил бы их вызовы (обе строки содержат "issues/5").
    "repos/o/r/issues/5": {"number": 5, "state": "open"},
}


# ── Имена и разбор refs ──────────────────────────────────────────────────────────


def test_lock_ref_naming_and_roundtrip():
    assert ct.lock_ref(5) == "refs/locks/task-5"
    assert ct.task_of_ref("refs/locks/task-125") == 125
    assert ct.task_of_ref("refs/heads/main") is None
    assert ct.task_of_ref("refs/locks/not-a-number") is None
    with pytest.raises(ValueError):
        ct.lock_ref(0)
    with pytest.raises(ValueError):
        ct.lock_ref(True)  # bool — тоже int в python: явно запрещён


def test_gh_status_parsing_prod_form():
    assert ct.gh_status("gh: HTTP 422: Reference already exists [...]") == 422
    assert ct.gh_status("gh: HTTP 404: Not Found") == 404
    assert ct.gh_status("dial tcp: connectex failed") is None


# ── TTL по дате коммита замка ────────────────────────────────────────────────────


def test_lock_age_accepts_prod_timestamp_forms():
    for raw in ("2026-08-31T10:00:00Z", "2026-08-31T10:00:00.000Z", "2026-08-31T10:00:00+00:00"):
        assert ct.lock_age_hours(raw, utc(11, 0)) == 1.0


def test_stale_boundary_is_strictly_beyond_ttl():
    # ровно TTL — ещё жив (протухшим считается замок «старше» LOCK_TTL_HOURS)
    assert ct.is_stale(utc(0, 0), utc_plus(ct.LOCK_TTL_HOURS)) is False
    assert ct.is_stale(utc(0, 0), utc_plus(ct.LOCK_TTL_HOURS, 1)) is True
    assert ct.is_stale("2026-08-29T00:00:00Z", utc(8, 31)) is True  # > 24 ч


# ── Гонка двух claim: выигрывает ровно один ─────────────────────────────────────


def test_race_two_claims_one_wins_other_refused(monkeypatch):
    server = install(monkeypatch, FakeServer(dict(BASE)))
    first = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    second = ct.claim("o/r", 5, "worker-b", now=utc(12, 0, ))
    assert first.claimed is True
    assert second.claimed is False and "занята" in second.detail
    # ref создан один раз — серверное доказательство атомарности
    assert sum(1 for c in server.calls if "git/refs" in c and "-X" in c and "POST" in c) == 2
    assert server.existing_refs == {"refs/locks/task-5"}


def test_claim_refuses_closed_task_without_creating_lock(monkeypatch):
    # Проверка на входе (не гвардия постфактум): приёмка уже закрыла задачу —
    # воркер/hands не должны снова браться за неё через claim. Отказ обязан
    # случиться ДО создания коммита/ref'а замка (дешёвый GET раньше дорогой
    # записи), поэтому проверяем отсутствие POST git/commits и git/refs.
    routes = dict(BASE)
    routes["repos/o/r/issues/5"] = {"number": 5, "state": "closed"}
    server = install(monkeypatch, FakeServer(routes))
    result = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    assert result.claimed is False
    assert "закрыта" in result.detail
    assert not any("git/commits" in c for c in server.calls)
    assert not any("git/refs" in c and "matching-refs" not in c for c in server.calls)


def test_claim_success_visibility_after_lock(monkeypatch):
    server = install(monkeypatch, FakeServer(dict(BASE)))
    result = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    assert result.claimed is True
    joined_calls = server.calls
    # порядок: сначала замок, потом видимость (назначение — НЕ защита)
    first_lock = next(i for i, c in enumerate(joined_calls) if "git/commits" in c)
    first_assign = next(i for i, c in enumerate(joined_calls) if "issues/5/assignees" in c)
    assert first_lock < first_assign
    assert any("issues/5/assignees" in c for c in joined_calls)
    assert any("issues/5/comments" in c and "worker-a" in c for c in joined_calls)


def test_claim_via_labels_channel_in_comment(monkeypatch):
    # Все агенты — один логин: «кто держит» различается каналом (worker/hands),
    # он обязан попасть в след в задаче, а не только в лог job'а.
    server = install(monkeypatch, FakeServer(dict(BASE)))
    result = ct.claim("o/r", 5, "mytab0r", now=utc(12, 0), via="hands issue-5 (run 1)")
    assert result.claimed is True
    comment = next(c for c in server.calls if "issues/5/comments" in c)
    assert "hands issue-5 (run 1)" in comment


def test_claim_visibility_failure_does_not_break_ownership(monkeypatch):
    routes = dict(BASE)
    routes["issues/5/assignees"] = fail(403, "Forbidden")
    routes["issues/5/comments"] = fail(403, "Forbidden")
    server = install(monkeypatch, FakeServer(routes))
    result = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    assert result.claimed is True  # замок стоит — владение не откатывается
    assert any("неполная" in c for c in server.calls) is False  # warning — в лог, не в calls


def test_claim_infra_failure_is_loud_not_busy(monkeypatch):
    routes = {
        "repos/o/r/issues/5": {"number": 5, "state": "open"},
        "repos/o/r/commits/main": fail(502, "Bad Gateway"),
    }
    install(monkeypatch, FakeServer(routes))
    # «занято» и «сломано» — разные состояния: поломка не маскируется отказом
    with pytest.raises(RuntimeError):
        ct.claim("o/r", 5, "worker-a", now=utc(12, 0))


def test_claim_unexpected_422_is_loud_not_busy(monkeypatch):
    # 422 у GitHub отвечает за разные состояния: «Reference already exists» —
    # отказ аренды (зелёный), прочие validation-ошибки — поломка (громко).
    # Различение по тексту, симметрично _ref_missing() у release.
    class ValidationServer(FakeServer):
        def run(self, args, capture_output=True, text=True, env=None):
            if "-X" in args and "POST" in args and "git/refs" in " ".join(args):
                return fail(422, "Validation Failed: tree sha wasn't found")
            return super().run(args, capture_output=capture_output, text=text, env=env)
    install(monkeypatch, ValidationServer(dict(BASE)))
    with pytest.raises(RuntimeError):
        ct.claim("o/r", 5, "worker-a", now=utc(12, 0))


def test_cli_unexpected_exception_is_error_not_busy(monkeypatch):
    # Чужой класс исключения (смена формы ответа API → KeyError) обязан дать
    # EXIT_ERROR (2) «инструмент сломан»: дефолтный exit CPython — 1, который
    # каналы трактуют как зелёный no-op «занято» (контракт кодов 0/1/2).
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    def broken(*a, **kw):
        raise KeyError("commit")
    monkeypatch.setattr(ct, "claim", broken)
    assert ct.main(["x", "claim", "5"]) == ct.EXIT_ERROR


# ── Release: идемпотентный, 404 — не ошибка ──────────────────────────────────────


def test_release_deletes_lock(monkeypatch):
    server = install(monkeypatch, FakeServer({}))
    server.add_ref("refs/locks/task-5")
    detail = ct.release("o/r", 5)
    assert "снят" in detail and "task-5" in detail
    assert any("DELETE" in c and "git/refs/locks/task-5" in c for c in server.calls)


def test_release_missing_lock_is_ok(monkeypatch):
    class NotFoundServer(FakeServer):
        def run(self, args, **kw):
            if "-X" in args and "DELETE" in args:
                return fail(404, "Not Found")
            return super().run(args, **kw)
    install(monkeypatch, NotFoundServer(FakeServer({}).routes))
    assert "отсутствовал" in ct.release("o/r", 7)


def test_release_missing_lock_prod_form_422_is_ok(monkeypatch):
    # Прод-форма (прогон orchestra 33562818220, задачи #18/#138/#147): GitHub
    # на DELETE несуществующего ref отвечает НЕ 404, а 422 "Reference does not
    # exist" — это тот же класс «рефа нет», а не поломка.
    class RefDoesNotExistServer(FakeServer):
        def run(self, args, **kw):
            if "-X" in args and "DELETE" in args:
                return fail(422, "Reference does not exist")
            return super().run(args, **kw)
    install(monkeypatch, RefDoesNotExistServer(FakeServer({}).routes))
    assert "отсутствовал" in ct.release("o/r", 7)


def test_release_422_reference_already_exists_is_not_missing(monkeypatch):
    # 422 — не универсальный «рефа нет»: тот же код у claim() при гонке
    # ("Reference already exists"). release() обязан различать по сообщению,
    # а не по одному статусу — иначе настоящая поломка на DELETE замаскируется.
    class WeirdServer(FakeServer):
        def run(self, args, **kw):
            if "-X" in args and "DELETE" in args:
                return fail(422, "Reference already exists")
            return super().run(args, **kw)
    install(monkeypatch, WeirdServer(FakeServer({}).routes))
    with pytest.raises(RuntimeError):
        ct.release("o/r", 7)


def test_release_real_failure_is_loud(monkeypatch):
    class BrokenServer(FakeServer):
        def run(self, args, **kw):
            if "-X" in args and "DELETE" in args:
                return fail(500, "server exploded")
            return super().run(args, **kw)
    install(monkeypatch, BrokenServer({}))
    with pytest.raises(RuntimeError):
        ct.release("o/r", 7)


def test_release_merged_is_idempotent_batch(monkeypatch):
    install(monkeypatch, FakeServer({}))
    lines = ct.release_merged("o/r", [5, 6])
    assert len(lines) == 2 and all("снят" in line or "отсутствовал" in line for line in lines)


# ── Сборщик протухших замков ─────────────────────────────────────────────────────


def test_collect_stale_removes_only_expired_and_leaves_trace(monkeypatch):
    routes = {
        "git/matching-refs/locks/": [
            {"ref": "refs/locks/task-5", "object": {"sha": "oldsha"}},
            {"ref": "refs/locks/task-6", "object": {"sha": "newsha"}},
            {"ref": "refs/locks/weird", "object": {"sha": "x"}},  # не задача — не трогаем
        ],
        "commits/oldsha": {"commit": {"committer": {"date": "2026-08-30T00:00:00Z"}}},  # 48 ч
        "commits/newsha": {"commit": {"committer": {"date": "2026-08-31T10:00:00Z"}}},  # 2 ч
        "issues/5/comments": ok_no_body(),
    }
    server = install(monkeypatch, FakeServer(routes))
    lines = ct.collect_stale("o/r", utc(12, 0))
    assert any("task-5" in line and "снят" in line for line in lines)
    assert any("task-6" in line and "жив" in line for line in lines)
    assert any("DELETE" in c and "task-5" in c for c in server.calls)
    assert not any("DELETE" in c and "task-6" in c for c in server.calls)
    # след в задаче: комментарий с причиной и порогом
    trace = [c for c in server.calls if "issues/5/comments" in c]
    assert trace and "24" in trace[0] and "Протухший замок" in trace[0]


def test_collect_stale_delete_failure_is_loud_not_fatal(monkeypatch):
    routes = {
        "git/matching-refs/locks/": [{"ref": "refs/locks/task-5", "object": {"sha": "oldsha"}}],
        "commits/oldsha": {"commit": {"committer": {"date": "2026-08-30T00:00:00Z"}}},
    }

    class DeleteBroken(FakeServer):
        def run(self, args, **kw):
            if "-X" in args and "DELETE" in args:
                return fail(422, "weird state")
            return super().run(args, **kw)

    install(monkeypatch, DeleteBroken(routes))
    lines = ct.collect_stale("o/r", utc(12, 0))
    assert any("не снят" in line for line in lines)  # не уронил обход, но и не молчит


# ── CLI: контракт для каналов worker/hands ───────────────────────────────────────


def test_cli_locks_prints_machine_readable_task_numbers(monkeypatch):
    # free_task (task.sh) пропускает занятые арендой: список номеров в одну
    # строку через пробел — без рефов и sha, чужие рефы под locks/ отфильтрованы.
    routes = {
        "git/matching-refs/locks/": [
            {"ref": "refs/locks/task-5", "object": {"sha": "s5"}},
            {"ref": "refs/locks/task-125", "object": {"sha": "s125"}},
            {"ref": "refs/locks/weird", "object": {"sha": "x"}},  # не задача
        ],
        "commits/s5": {"commit": {"committer": {"date": "2026-08-31T11:00:00Z"}}},
        "commits/s125": {"commit": {"committer": {"date": "2026-08-31T11:00:00Z"}}},
    }
    install(monkeypatch, FakeServer(routes))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ct.main(["x", "locks"]) == ct.EXIT_OK
    assert out.getvalue().split() == ["5", "125"]


def test_cli_locks_empty_pool_prints_empty_line(monkeypatch):
    install(monkeypatch, FakeServer({"git/matching-refs/locks/": []}))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert ct.main(["x", "locks"]) == ct.EXIT_OK
    assert out.getvalue().strip() == ""


def test_cli_exit_codes_contract(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(ct, "claim", lambda *a, **kw: ct.ClaimResult(True, 5, "ок"))
    assert ct.main(["x", "claim", "5"]) == ct.EXIT_OK
    monkeypatch.setattr(ct, "claim", lambda *a, **kw: ct.ClaimResult(False, 5, "занята"))
    assert ct.main(["x", "claim", "5"]) == ct.EXIT_BUSY  # зелёный no-op вызывающего
    monkeypatch.setattr(ct, "release", lambda *a, **kw: "снят")
    assert ct.main(["x", "release", "5"]) == ct.EXIT_OK


def test_cli_requires_repo_and_valid_task(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert ct.main(["x", "claim", "5"]) == ct.EXIT_ERROR
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert ct.main(["x", "claim", "abc"]) == ct.EXIT_ERROR
    assert ct.main(["x"]) == ct.EXIT_ERROR
