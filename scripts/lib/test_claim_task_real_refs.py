#!/usr/bin/env python3
"""Поведенческая гвардия держателя замка (#1190) на РЕАЛЬНЫХ git-рефах.

Не мок JSON-словарём (как `test_claim_task.py::FakeServer`) — здесь `gh()`
подменяется на бэкенд, который реально создаёт коммиты (`git commit-tree`) и
реально создаёт/читает/удаляет ссылки (`git update-ref`) во временном
git-репозитории на диске. Атомарность «создать реф, только если его ещё нет»
даёт РЕАЛЬНЫЙ git (`git update-ref <ref> <sha> ""` — пустой oldvalue требует
отсутствия рефа), не искусственный python-set, как в FakeServer.

Сценарий, воспроизводящий живой случай #1190 (issue, «Критерий готовности»):
канал A берёт замок → канал B пытается взять тот же — получает отказ **с
именем держателя A** → канал A берёт повторно — успех, идемпотентно, БЕЗ
снятия замка.

Мутация, которой доказан основной тест (прогон: сняла защиту → тест
покраснел → вернула → снова зелёный, см. докстринг теста ниже дословно).

Запуск: python -m pytest scripts/lib/test_claim_task_real_refs.py -q
"""

# --- console_utf8 bootstrap (класс: печать кириллицы валит encoding на Windows, issue #723) ---
import importlib.util
from pathlib import Path
_console_utf8_spec = importlib.util.spec_from_file_location(
    "console_utf8", Path(__file__).resolve().parent / "console_utf8.py")
_console_utf8_spec.loader.exec_module(importlib.util.module_from_spec(_console_utf8_spec))
# --- конец console_utf8 bootstrap ---

import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

SCRIPT = Path(__file__).with_name("claim_task.py")
spec = importlib.util.spec_from_file_location("claim_task_real_refs", SCRIPT)
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)  # type: ignore[union-attr]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8",
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.email", "t@test").returncode == 0
    assert _git(repo, "config", "user.name", "t").returncode == 0
    assert _git(repo, "commit", "-q", "--allow-empty", "-m", "seed").returncode == 0


def _parse(args):
    """(method, path, fields) из `["gh", "api", ...]` — тот же разбор, что и
    у мокового FakeServer в test_claim_task.py (одно место, скопировано
    намеренно: это ДРУГОЙ бэкенд, реальные git-команды, а не JSON-словарь —
    общий модуль для двух тестовых бэкендов добавил бы связность там, где её
    не должно быть между «мок» и «реальный git»)."""
    method = "GET"
    path = None
    fields = {}
    tokens = list(args[2:])  # после "gh", "api"
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-X":
            method = tokens[i + 1]
            i += 2
            continue
        if token == "-f":
            key, _, value = tokens[i + 1].partition("=")
            fields[key] = value
            i += 2
            continue
        if path is None and not token.startswith("-"):
            path = token
        i += 1
    return method, path, fields


class RealGitServer:
    """Бэкенд `gh api`, реализующий РЕАЛЬНЫМИ git-командами ровно те
    эндпоинты, которые использует `claim_task.py`: атомарное создание рефа
    (`git update-ref <ref> <sha> ""` — пустой oldvalue требует, чтобы рефа
    ещё не было, это и есть настоящая атомарность, не имитация), чтение
    единичного рефа/коммита, DELETE рефа. Issues/assignees/comments — не
    часть git-модели, оставлены плоской заглушкой (не то, что здесь
    проверяется)."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.calls: list[str] = []
        self.main_sha = _git(repo, "rev-parse", "main").stdout.strip()
        self.main_tree = _git(repo, "rev-parse", "main^{tree}").stdout.strip()

    def run(self, args, capture_output=True, text=True, encoding=None, env=None):
        joined = " ".join(args)
        self.calls.append(joined)
        method, path, fields = _parse(args)

        def ok(body=""):
            return SimpleNamespace(returncode=0, stdout=body, stderr="")

        def fail(status, message):
            return SimpleNamespace(returncode=1, stdout="", stderr=f"gh: HTTP {status}: {message}")

        if method == "GET" and path == "repos/o/r/issues/42":
            return ok('{"number": 42, "state": "open", "labels": []}')
        if method == "GET" and path is not None and path.endswith("/commits/main"):
            return ok(
                '{"sha": "%s", "commit": {"tree": {"sha": "%s"}, '
                '"committer": {"date": "2026-09-14T00:00:00Z"}}}'
                % (self.main_sha, self.main_tree)
            )
        if method == "POST" and path is not None and path.endswith("/git/commits"):
            message = fields.get("message", "")
            tree = fields.get("tree", self.main_tree)
            parent = fields.get("parents[]", self.main_sha)
            proc = subprocess.run(
                ["git", "-C", str(self.repo), "commit-tree", tree, "-p", parent],
                input=message, capture_output=True, text=True, encoding="utf-8",
            )
            if proc.returncode != 0:
                return fail(500, proc.stderr.strip())
            sha = proc.stdout.strip()
            return ok('{"sha": "%s"}' % sha)
        if method == "POST" and path is not None and path.endswith("/git/refs"):
            ref = fields["ref"]
            sha = fields["sha"]
            proc = _git(self.repo, "update-ref", ref, sha, "")
            if proc.returncode != 0:
                return fail(422, "Reference already exists")
            return ok()
        if method == "GET" and path is not None and "/git/ref/locks/task-" in path:
            ref = "refs/" + path.split("/git/ref/", 1)[1]
            proc = _git(self.repo, "rev-parse", "--verify", "-q", ref)
            if proc.returncode != 0:
                return fail(404, "Not Found")
            sha = proc.stdout.strip()
            return ok('{"ref": "%s", "object": {"sha": "%s"}}' % (ref, sha))
        if method == "GET" and path is not None and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            # Прод-форма ответа GitHub: message (держатель) И committer.date
            # (TTL) приходят из ОДНОГО GET коммита — %cI даёт настоящую дату
            # коммита реального git, не подставку.
            proc = _git(self.repo, "log", "-1", "--format=%H%x00%cI%x00%B", sha)
            if proc.returncode != 0:
                return fail(404, "Not Found")
            real_sha, iso_date, message = proc.stdout.split("\x00", 2)
            escaped = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return ok('{"sha": "%s", "message": "%s", "commit": '
                      '{"committer": {"date": "%s"}}}' % (real_sha, escaped, iso_date))
        if method == "DELETE" and path is not None and "/git/refs/locks/task-" in path:
            ref = "refs/" + path.split("/git/refs/", 1)[1]
            proc = _git(self.repo, "update-ref", "-d", ref)
            if proc.returncode != 0:
                return fail(404, "Not Found")
            return ok()
        if method == "POST" and path is not None and ("/assignees" in path or "/comments" in path):
            return ok()
        raise AssertionError(f"нет маршрута в RealGitServer: {joined}")


def _lock_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "refs/locks/task-42").stdout.strip()


def _now():
    return datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def test_real_refs_foreign_holder_named_then_self_reclaim_idempotent(tmp_path, monkeypatch):
    """Критерий готовности issue #1190, дословно: канал A берёт замок, канал
    B пытается взять тот же — получает отказ с ИМЕНЕМ держателя A; канал A
    берёт повторно — успех без снятия. На реальных git-рефах, не на моке.

    Мутация (доказана прогоном: сняла защиту → тест покраснел → вернула →
    снова зелёный): в `claim_task.py::claim` заменить
    `if existing_holder == holder:` на `if False:` — второй вызов A
    перестаёт читаться как «свой», `result_a2.claimed` становится `False` —
    падает ассерт `assert result_a2.claimed is True`.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    server = RealGitServer(repo)
    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=server.run))

    # Канал A берёт замок.
    result_a = ct.claim("o/r", 42, "worker-a", now=_now(), holder="run:A")
    assert result_a.claimed is True
    sha_after_a = _lock_sha(repo)
    assert sha_after_a  # реф реально существует в репозитории на диске

    # Канал B пытается взять тот же замок — отказ, называющий держателя A.
    result_b = ct.claim("o/r", 42, "worker-b", now=_now(), holder="run:B")
    assert result_b.claimed is False
    assert "run:A" in result_b.detail, result_b.detail
    assert result_b.holder == "run:A"
    assert _lock_sha(repo) == sha_after_a  # замок не тронут чужой попыткой

    # Канал A берёт повторно — успех, идемпотентно, БЕЗ снятия замка.
    result_a2 = ct.claim("o/r", 42, "worker-a", now=_now(), holder="run:A")
    assert result_a2.claimed is True
    assert "идемпотентна" in result_a2.detail
    # Замечание ревью PR #1206: идемпотентный перезабор НЕ продлевает TTL и
    # честно сообщает остаток (дата — из реального коммита git). Мутация:
    # убери из claim() хвост «; TTL НЕ продлевается…» — этот assert краснеет.
    assert "TTL НЕ продлевается" in result_a2.detail, result_a2.detail
    assert "осталось" in result_a2.detail
    assert _lock_sha(repo) == sha_after_a  # реф — тот же самый объект


def test_real_refs_release_refuses_foreign_then_self_succeeds(tmp_path, monkeypatch):
    """Тот же сценарий для release() (#1190, пункт 2/4 задачи — «возврат»):
    канал B не может снять замок канала A вслепую (`ForeignLockError`,
    называющий A), но A сам снимает свой замок штатно."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    server = RealGitServer(repo)
    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=server.run))

    assert ct.claim("o/r", 42, "worker-a", now=_now(), holder="run:A").claimed is True

    try:
        ct.release("o/r", 42, holder="run:B")
        raised = False
    except ct.ForeignLockError as error:
        raised = True
        assert "run:A" in str(error)
    assert raised, "release() чужим holder обязан отказать ForeignLockError"
    assert _lock_sha(repo)  # замок всё ещё стоит — снятия не было

    detail = ct.release("o/r", 42, holder="run:A")
    assert "снят" in detail
    assert _git(repo, "rev-parse", "--verify", "-q", "refs/locks/task-42").returncode != 0


def test_real_refs_update_ref_atomic_create_only(tmp_path):
    """Доказательство самого примитива атомарности (не claim_task, а сам
    git): `git update-ref <ref> <sha> ""` создаёт реф только если его ещё не
    было — ровно то, на чём стоит защита от гонки (ADR 0006). Без пустого
    oldvalue вторая команда молча перезаписала бы первый замок, а не
    отказала."""
    repo = tmp_path / "repo2"
    _init_repo(repo)
    sha = _git(repo, "rev-parse", "main").stdout.strip()

    first = _git(repo, "update-ref", "refs/locks/task-1", sha, "")
    assert first.returncode == 0

    second = _git(repo, "update-ref", "refs/locks/task-1", sha, "")
    assert second.returncode != 0
    stderr_lower = (second.stderr or "").lower()
    assert "already exists" in stderr_lower or "cannot lock" in stderr_lower
