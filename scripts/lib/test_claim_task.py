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


def _parse_gh_call(args):
    """Разбор `["gh", "api", ...]` на (method, path, fields) — точнее, чем
    подстрока по всей склеенной строке (находка #1190: подстрока "git/ref" у
    GET-эндпоинта единичного рефа совпадала бы и с "git/refs" у POST/DELETE,
    если бы матчить по join(args) целиком)."""
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


class FakeServer:
    """gh api на моке subprocess.run: маршруты по разобранному (method, path) +
    состояние refs (POST существующего → 422), как это серверно делает GitHub.

    Держатель замка (#1190): POST /git/commits создаёт коммит с ДИНАМИЧЕСКИМ
    sha и реально сохраняет тело сообщения (`commit_messages`) — GET
    /git/ref/locks/task-N и GET /git/commits/{sha} читают его обратно, ровно
    как настоящий GitHub Data API. Раньше POST /git/commits был статичным
    маршрутом с одним и тем же sha на все claim'ы — этого хватало, пока
    претенденты не начали различаться держателем."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls = []
        self.existing_refs: set[str] = set()
        self.ref_sha: dict[str, str] = {}
        self.commit_messages: dict[str, str] = {}
        self._commit_seq = 0

    def add_ref(self, ref, sha=None):
        self.existing_refs.add(ref)
        if sha is not None:
            self.ref_sha[ref] = sha

    def run(self, args, capture_output=True, text=True, encoding=None, env=None):
        joined = " ".join(args)
        self.calls.append(joined)
        method, path, fields = _parse_gh_call(args)

        if method == "POST" and path is not None and path.endswith("/git/refs"):
            # POST /git/refs: атомарное создание ref'а — второй претендент отклонён
            ref = fields["ref"]
            sha = fields.get("sha")
            if ref in self.existing_refs:
                return fail(422, "Reference already exists")
            self.existing_refs.add(ref)
            if sha is not None:
                self.ref_sha[ref] = sha
            return ok_no_body()
        if method == "DELETE" and path is not None and "/git/refs/locks/task-" in path:
            # Реальный GitHub реально убирает реф на DELETE — раньше мок этого
            # не делал (existing_refs не менялся), латентная дыра: ни один
            # тест до #1190 не проверял состояние после release() достаточно
            # строго, чтобы это заметить.
            ref = "refs/" + path.split("/git/refs/", 1)[1]
            self.existing_refs.discard(ref)
            self.ref_sha.pop(ref, None)
            return ok_no_body()
        if method == "POST" and path is not None and path.endswith("/git/commits"):
            message = fields.get("message", "")
            self._commit_seq += 1
            sha = f"csha{self._commit_seq}"
            self.commit_messages[sha] = message
            return out({"sha": sha})
        if method == "GET" and path is not None and "/git/ref/locks/task-" in path:
            ref = "refs/" + path.split("/git/ref/", 1)[1]
            sha = self.ref_sha.get(ref)
            if sha is None:
                return fail(404, "Not Found")
            return out({"ref": ref, "object": {"sha": sha}})
        if method == "GET" and path is not None and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            return out({"sha": sha, "message": self.commit_messages.get(sha, "")})
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


def test_gh_pins_utf8_encoding_not_console_codepage(monkeypatch):
    # Находка ai-review PR #326: text=True без явного encoding декодирует
    # чужой пайп кодовой страницей консоли (cp1251 на Windows), не UTF-8 —
    # PYTHONIOENCODING на это не влияет (та переменная задаёт кодировку
    # только собственных stdin/stdout/stderr процесса). Мутационная проверка:
    # убери encoding="utf-8" в gh() — этот тест покраснеет.
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=fake_run))
    ct.gh("repos/o/r")
    assert seen.get("encoding") == "utf-8"


# ── Инъецируемый гейт записи (#951, доводка PR #950) ─────────────────────────────
# claim_task обслуживает МНОГО каналов (task-branch/task.sh/dsh_task.sh пишут
# ЛОКАЛЬНО и это штатно) — set_write_guard(None) по умолчанию обязан оставить
# поведение прежним для всех, кто хук не подключает; только вызвавший
# set_write_guard(...) получает решение.


@pytest.fixture(autouse=True)
def _reset_write_guard():
    """set_write_guard — module-level состояние: тест, забывший его снять,
    красил бы все следующие — сброс на None (штатное поведение) до и после
    каждого теста."""
    ct.set_write_guard(None)
    yield
    ct.set_write_guard(None)


def test_write_guard_default_none_does_not_change_behavior(monkeypatch):
    seen = []

    def fake_run(args, **kwargs):
        seen.append(" ".join(args))
        return ok_no_body()

    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=fake_run))
    ct.gh("-X", "DELETE", "repos/o/r/git/refs/locks/task-5")
    assert seen  # вызов реально ушёл — хук не подключён, ничего не изменилось


def test_write_guard_false_skips_the_real_call_and_raises(monkeypatch):
    # Мутационное доказательство: сними проверку `_write_guard is not None and
    # _is_write(args) and not _write_guard(...)` в gh() (например, замени на
    # `if False:`) — этот тест покраснеет: subprocess.run окажется вызван.
    #
    # До находки ревью PR #950 (третий проход) gh() тихо возвращал None —
    # вызывающий код (release/release_full/collect_stale) не проверял его и
    # рапортовал успех, хотя запись не уходила вовсе. Теперь отказ гейта
    # наблюдаем: WriteGateSkipped, а не немой None.
    calls = []

    def fake_run(args, **kwargs):
        calls.append(" ".join(args))
        return ok_no_body()

    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=fake_run))
    ct.set_write_guard(lambda description: False)
    with pytest.raises(ct.WriteGateSkipped):
        ct.gh("-X", "DELETE", "repos/o/r/git/refs/locks/task-5")
    assert calls == []  # DRY-RUN — реального похода в сеть не было


def test_write_guard_true_lets_the_real_call_through(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(" ".join(args))
        return ok_no_body()

    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=fake_run))
    ct.set_write_guard(lambda description: True)
    ct.gh("-X", "DELETE", "repos/o/r/git/refs/locks/task-5")
    assert len(calls) == 1


def test_write_guard_does_not_gate_reads(monkeypatch):
    # GET не несёт -X МЕТОД из _GH_WRITE_METHODS — гейту нечего проверять,
    # чтение обязано идти всегда, даже если хук стоит и всегда отвечает False
    # (иначе claim() не смог бы прочитать даже состояние задачи вне CI).
    calls = []

    def fake_run(args, **kwargs):
        calls.append(" ".join(args))
        return out({"number": 5, "state": "open"})

    monkeypatch.setattr(ct, "subprocess", SimpleNamespace(run=fake_run))
    ct.set_write_guard(lambda description: False)
    result = ct.gh("repos/o/r/issues/5")
    assert result == {"number": 5, "state": "open"}
    assert len(calls) == 1


def test_write_guard_gates_release_end_to_end(monkeypatch):
    """Живая находка ревью PR #950: claim_task.release шёл в обход
    scheduler._guard_raw_subprocess_write целиком. С подключённым хуком
    release() обязан молчать (DRY-RUN), а не реально снимать замок.

    Усилено находкой третьего прохода ревью: `detail` раньше говорил «замок
    ... снят» даже когда DELETE не уходил вовсе (gh() тихо возвращал None) —
    отчёт врал об исходе. Теперь ассерт бьёт именно по этой лжи: «снят» без
    «пропущено» в detail означает регресс на старое поведение."""
    server = install(monkeypatch, FakeServer({}))
    server.add_ref("refs/locks/task-5")
    ct.set_write_guard(lambda description: False)
    detail = ct.release("o/r", 5)
    assert "task-5" in detail
    assert "пропущен" in detail  # честно назвал DRY-RUN, не соврал про «снят»
    assert "снят" not in detail  # ключевая проверка находки: раньше здесь было "замок ... снят"
    assert not any("DELETE" in c for c in server.calls)  # DRY-RUN — DELETE не ушёл
    assert "refs/locks/task-5" in server.existing_refs  # замок реально жив


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
    # Держатель (#1190) различает КАНАЛЫ, не логин (actor у обоих может
    # совпадать) — двум разным претендентам нужны РАЗНЫЕ holder, иначе второй
    # вызов читается как идемпотентный перезабор своего же замка (см. отдельный
    # тест на это поведение ниже, test_claim_same_holder_reclaims_idempotently).
    server = install(monkeypatch, FakeServer(dict(BASE)))
    first = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="run:A")
    second = ct.claim("o/r", 5, "worker-b", now=utc(12, 0), holder="run:B")
    assert first.claimed is True
    assert second.claimed is False and "занята" in second.detail
    assert second.holder == "run:A"  # чужой отказ называет держателя (#1190)
    # ref создан один раз — серверное доказательство атомарности
    assert sum(1 for c in server.calls if "git/refs" in c and "-X" in c and "POST" in c) == 2
    assert server.existing_refs == {"refs/locks/task-5"}


# ── Держатель замка (#1190): свой / чужой / неизвестный ─────────────────────────


def test_claim_same_holder_reclaims_idempotently_without_removing_lock(monkeypatch):
    # Живой случай #1190: канал A держит замок; A же повторно вызывает claim
    # (например перезапуск того же прогона/дерева) — обязан получить успех БЕЗ
    # снятия и пересоздания ref'а (идемпотентность), не «занята».
    server = install(monkeypatch, FakeServer(dict(BASE)))
    first = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="tree:/work/A")
    assert first.claimed is True
    sha_after_first = server.ref_sha["refs/locks/task-5"]
    posts_before = sum(1 for c in server.calls if "-X" in c and "POST" in c)

    second = ct.claim("o/r", 5, "worker-a", now=utc(13, 0), holder="tree:/work/A")
    assert second.claimed is True
    assert second.holder == "tree:/work/A"
    assert "идемпотентна" in second.detail
    # Замок НЕ тронут: тот же sha, ни один DELETE не ушёл.
    assert server.ref_sha["refs/locks/task-5"] == sha_after_first
    assert not any("-X" in c and "DELETE" in c for c in server.calls)
    # Мутация: если бы reclaim молча совпадал по detail с обычным успехом
    # («установлен»), этот ассерт бы не различил их — намеренно проверяем
    # именно слово «идемпотентна», а не факт claimed=True.


def test_claim_unknown_holder_is_third_state_not_silent_success_or_refusal(monkeypatch):
    # Замок старого формата (#1190: создан ДО этого изменения, без строки
    # `holder:`) — держателя установить нельзя. Мутационная проверка класса:
    # ЕСЛИ бы claim() трактовал None-держателя как «свой» — это был бы
    # молчаливый успех поверх чужого живого замка (ровно инцидент #1190);
    # если бы трактовал как обычного «чужого» — сообщение потеряло бы разницу
    # между «есть конкретный держатель X» и «держателя не установить».
    server = install(monkeypatch, FakeServer(dict(BASE)))
    server.add_ref("refs/locks/task-5", sha="legacy-sha")  # без commit_messages записи
    result = ct.claim("o/r", 5, "worker-b", now=utc(12, 0), holder="tree:/work/B")
    assert result.claimed is False
    assert result.holder is None
    assert "неизвестен" in result.detail
    assert "старого формата" in result.detail


def test_claim_race_lock_disappears_between_422_and_read_is_not_confused_with_old_format(monkeypatch):
    """Находка ai-review PR #1206 (некритичная, зафиксирована): раньше
    `claim()` отбрасывал `exists` из `_lock_state` и гадал — «либо замок
    старого формата, либо гонка чтения» в одном сообщении, хотя данные уже
    различают эти два случая. Здесь POST /git/refs получает 422 («уже
    существует» — `existing_refs` содержит ref), но GET ref НЕ находит его
    (`ref_sha` пуст — ref пропал, ровно 404, как отвечал бы настоящий GitHub
    после снятия замка между попыткой и чтением). Сообщение обязано назвать
    именно эту причину, не смешивать её с «замок стоит, но старого формата»."""
    server = install(monkeypatch, FakeServer(dict(BASE)))
    server.existing_refs.add("refs/locks/task-5")  # POST 422, но GET ref → 404 (нет sha)
    result = ct.claim("o/r", 5, "worker-b", now=utc(12, 0), holder="tree:/work/B")
    assert result.claimed is False
    assert result.holder is None
    assert "гонка снятия" in result.detail
    assert "уже свободна" in result.detail
    assert "старого формата" not in result.detail


def test_release_own_holder_succeeds_without_force(monkeypatch):
    server = install(monkeypatch, FakeServer(dict(BASE)))
    claimed = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="tree:/work/A")
    assert claimed.claimed is True
    detail = ct.release("o/r", 5, holder="tree:/work/A")
    assert "снят" in detail
    assert "refs/locks/task-5" not in server.existing_refs


def test_release_foreign_holder_refuses_and_names_holder(monkeypatch):
    # Ровно инцидент #1190: канал B пытается снять замок канала A, полагая его
    # своим — обязан получить ForeignLockError, называющий держателя A, а НЕ
    # тихо снять чужой живой замок.
    server = install(monkeypatch, FakeServer(dict(BASE)))
    claimed = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="tree:/work/A")
    assert claimed.claimed is True
    with pytest.raises(ct.ForeignLockError, match="tree:/work/A"):
        ct.release("o/r", 5, holder="tree:/work/B")
    # Замок НЕ снят — отказ произошёл ДО DELETE.
    assert "refs/locks/task-5" in server.existing_refs
    assert not any("-X" in c and "DELETE" in c for c in server.calls)


def test_release_unknown_holder_lock_refuses_by_default(monkeypatch):
    # Третье состояние и у release(): замок старого формата (без holder) —
    # безопасный дефолт отказывает, не снимает вслепую.
    server = install(monkeypatch, FakeServer(dict(BASE)))
    server.add_ref("refs/locks/task-5", sha="legacy-sha")
    with pytest.raises(ct.ForeignLockError, match="старого формата"):
        ct.release("o/r", 5, holder="tree:/work/B")
    assert not any("-X" in c and "DELETE" in c for c in server.calls)


def test_release_force_bypasses_ownership_check(monkeypatch):
    # Газ для тормоза (AGENTS.md «Тормоз без газа не принимается»): явный
    # обход для того, кто уверен, что имеет право снять чужой/неизвестный замок.
    server = install(monkeypatch, FakeServer(dict(BASE)))
    claimed = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="tree:/work/A")
    assert claimed.claimed is True
    detail = ct.release("o/r", 5, holder="tree:/work/B", force=True)
    assert "снят" in detail
    assert "refs/locks/task-5" not in server.existing_refs


def test_release_default_holder_none_is_unchanged_force_behavior(monkeypatch):
    # scheduler.py зовёт release(repo, task) без holder — поведение НЕ меняется
    # этим change: снятие идёт БЕЗ проверки владения (post-merge/TTL-сборщик
    # авторитетны независимо от держателя). Мутация: если бы holder=None стал
    # проверяться — этот тест упал бы ForeignLockError.
    server = install(monkeypatch, FakeServer(dict(BASE)))
    claimed = ct.claim("o/r", 5, "worker-a", now=utc(12, 0), holder="tree:/work/A")
    assert claimed.claimed is True
    detail = ct.release("o/r", 5)  # holder не передан вовсе
    assert "снят" in detail


def test_current_holder_prefers_env_over_run_id_over_cwd(monkeypatch):
    monkeypatch.delenv("CLAIM_HOLDER", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_JOB", raising=False)
    assert ct.current_holder().startswith("tree:")

    monkeypatch.setenv("GITHUB_RUN_ID", "998877")
    assert ct.current_holder() == "run:998877"

    monkeypatch.setenv("CLAIM_HOLDER", "explicit-holder")
    assert ct.current_holder() == "explicit-holder"


def test_current_holder_appends_job_to_run_id(monkeypatch):
    """Находка ai-review PR #1206 (некритичная, зафиксирована): GITHUB_RUN_ID
    один на ВСЕ job'ы одного прогона воркфлоу — второй job на том же прогоне
    молча прочитал бы чужой замок как свой без этого различения."""
    monkeypatch.delenv("CLAIM_HOLDER", raising=False)
    monkeypatch.setenv("GITHUB_RUN_ID", "998877")
    monkeypatch.delenv("GITHUB_JOB", raising=False)
    assert ct.current_holder() == "run:998877"

    monkeypatch.setenv("GITHUB_JOB", "task")
    assert ct.current_holder() == "run:998877:task"


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


def test_claim_refuses_blocked_task_without_creating_lock(monkeypatch):
    # Симметрично closed выше (#357): задача открыта, но несёт blocked —
    # эскалация владельцу, hands (dsh_task.sh) идут мимо task-branch и
    # единственные их ворота это claim. Мутация: закомментируй проверку
    # blocked в claim() — этот тест краснеет (claimed становится True).
    routes = dict(BASE)
    routes["repos/o/r/issues/5"] = {
        "number": 5, "state": "open", "labels": [{"name": "task"}, {"name": "blocked"}],
    }
    server = install(monkeypatch, FakeServer(routes))
    result = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    assert result.claimed is False
    assert "заблокирована" in result.detail
    assert not any("git/commits" in c for c in server.calls)
    assert not any("git/refs" in c and "matching-refs" not in c for c in server.calls)


def test_claim_refuses_waiting_owner_task_without_creating_lock(monkeypatch):
    # Симметрично blocked выше (#254/#470): задача открыта, но несёт
    # waiting:owner — ждёт явного выбора владельца, воркер/hands не должны
    # начинать работу над тем, что ещё не выбрано. Мутация: закомментируй
    # проверку waiting:owner в claim() — этот тест краснеет (claimed
    # становится True).
    routes = dict(BASE)
    routes["repos/o/r/issues/5"] = {
        "number": 5, "state": "open", "labels": [{"name": "task"}, {"name": "waiting:owner"}],
    }
    server = install(monkeypatch, FakeServer(routes))
    result = ct.claim("o/r", 5, "worker-a", now=utc(12, 0))
    assert result.claimed is False
    assert "ждёт решения владельца" in result.detail
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
        def run(self, args, capture_output=True, text=True, encoding=None, env=None):
            if "-X" in args and "POST" in args and "git/refs" in " ".join(args):
                return fail(422, "Validation Failed: tree sha wasn't found")
            return super().run(args, capture_output=capture_output, text=text, encoding=encoding, env=env)
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


# ── release_full: и назначение, и замок (#422 — провайдер в лимите/квоте) ────────


def test_release_full_removes_assignee_and_lock(monkeypatch):
    # Живой случай #422: воркер узнал СРАЗУ, что дальше нет смысла (бюджет
    # ретрая RATE_LIMIT исчерпан) — задача обязана вернуться в пул целиком,
    # не дожидаясь 24-часового reap_stale.
    routes = {
        "issues/5/assignees": ok_no_body(),
        "repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]},
    }
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5")
    detail = ct.release_full("o/r", 5)
    assert "назначение снято" in detail and "mytab0r" in detail
    assert "снят" in detail  # часть release() тоже отражена в итоговой строке
    assert any("DELETE" in c and "issues/5/assignees" in c and "mytab0r" in c
               for c in server.calls)
    assert any("DELETE" in c and "git/refs/locks/task-5" in c for c in server.calls)


def test_release_full_without_assignee_only_touches_lock(monkeypatch):
    routes = {"repos/o/r/issues/7": {"number": 7, "assignees": []}}
    server = install(monkeypatch, FakeServer(routes))
    detail = ct.release_full("o/r", 7)
    assert "назначения не было" in detail
    assert not any("DELETE" in c and "issues/7/assignees" in c for c in server.calls)


def test_release_full_assignee_removal_failure_does_not_block_lock_release(monkeypatch):
    # Снятие assignee — видимость, не защита (симметрично claim._visibility):
    # сбой не должен помешать снять замок — иначе задача осталась бы занятой
    # ДВОЙНО (и assignee, и мёртвый замок) именно там, где нужно освободить её
    # быстрее всего.
    class ForbiddenAssignees(FakeServer):
        def run(self, args, **kw):
            joined = " ".join(args)
            if "-X" in args and "DELETE" in args and "issues/5/assignees" in joined:
                return fail(403, "Forbidden")
            return super().run(args, **kw)
    routes = {"repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]}}
    server = install(monkeypatch, ForbiddenAssignees(routes))
    server.add_ref("refs/locks/task-5")
    detail = ct.release_full("o/r", 5)
    assert "не снято" in detail
    assert any("DELETE" in c and "git/refs/locks/task-5" in c for c in server.calls)


def test_release_full_assignee_removal_is_dry_run_via_write_guard(monkeypatch):
    """Находка ревью PR #950 (третий проход): release_full раньше рапортовал
    «назначение снято» даже когда гейт заблокировал DELETE (gh() тихо
    возвращал None). Мутация: замени `except WriteGateSkipped` обратно на
    неотличимый успех — detail снова солжёт «снято»."""
    routes = {"repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]}}
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5")
    ct.set_write_guard(lambda description: False)
    detail = ct.release_full("o/r", 5)
    assert "НЕ снято" in detail
    assert not any("DELETE" in c and "issues/5/assignees" in c for c in server.calls)


def test_release_full_with_matching_holder_succeeds(monkeypatch):
    # CLI release-full (#1190): с правильным holder снимает замок и назначение.
    routes = {
        "issues/5/assignees": ok_no_body(),
        "repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]},
    }
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5", sha="lock-sha")
    server.commit_messages["lock-sha"] = "lock: task #5 claimed...\nholder: tree:/work/A"
    detail = ct.release_full("o/r", 5, holder="tree:/work/A")
    assert "назначение снято" in detail and "mytab0r" in detail
    assert "снят" in detail
    assert "refs/locks/task-5" not in server.existing_refs


def test_release_full_with_foreign_holder_refuses(monkeypatch):
    # CLI release-full (#1190): чужой holder — ForeignLockError, замок не тронут.
    routes = {
        "issues/5/assignees": ok_no_body(),
        "repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]},
    }
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5", sha="lock-sha")
    server.commit_messages["lock-sha"] = "lock: task #5 claimed...\nholder: tree:/work/A"
    with pytest.raises(ct.ForeignLockError, match="tree:/work/A"):
        ct.release_full("o/r", 5, holder="tree:/work/B")
    assert "refs/locks/task-5" in server.existing_refs
    # Замок не удалён — DELETE git/refs/locks/task-5 не ушёл (assignee DELETE — это видимость, он может уйти до проверки holder)
    assert not any("-X" in c and "DELETE" in c and "git/refs/locks/task-5" in c for c in server.calls)


def test_release_full_with_unknown_holder_lock_refuses(monkeypatch):
    # Третье состояние: замок старого формата без holder — отказ по умолчанию.
    routes = {
        "issues/5/assignees": ok_no_body(),
        "repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]},
    }
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5", sha="legacy-sha")  # без commit_messages записи
    with pytest.raises(ct.ForeignLockError, match="старого формата"):
        ct.release_full("o/r", 5, holder="tree:/work/B")
    assert not any("-X" in c and "DELETE" in c and "git/refs/locks/task-5" in c for c in server.calls)


def test_release_full_without_holder_is_force_behavior(monkeypatch):
    # scheduler/merged/TTL зовут release_full() без holder — форс-снятие,
    # поведение НЕ меняется. Мутация: если бы holder=None стал проверяться —
    # этот тест упал бы ForeignLockError.
    routes = {
        "issues/5/assignees": ok_no_body(),
        "repos/o/r/issues/5": {"number": 5, "assignees": [{"login": "mytab0r"}]},
    }
    server = install(monkeypatch, FakeServer(routes))
    server.add_ref("refs/locks/task-5", sha="lock-sha")
    server.commit_messages["lock-sha"] = "lock: task #5 claimed...\nholder: tree:/work/A"
    detail = ct.release_full("o/r", 5)  # holder не передан
    assert "назначение снято" in detail and "mytab0r" in detail
    assert "снят" in detail
    assert "refs/locks/task-5" not in server.existing_refs


def test_collect_stale_expired_lock_is_dry_run_via_write_guard(monkeypatch):
    """Тот же класс для collect_stale: протухший замок под гейтом обязан
    остаться нетронутым, а действие — честно назвать DRY-RUN, не «снят»."""
    routes = {
        "git/matching-refs/locks/": [
            {"ref": "refs/locks/task-5", "object": {"sha": "oldsha"}},
        ],
        "commits/oldsha": {"commit": {"committer": {"date": "2026-08-30T00:00:00Z"}}},  # 48 ч
    }
    server = install(monkeypatch, FakeServer(routes))
    ct.set_write_guard(lambda description: False)
    _, actions = ct.collect_stale("o/r", utc(12, 0))
    assert any("task-5" in line and "пропущено" in line for line in actions)
    assert not any("DELETE" in c and "task-5" in c for c in server.calls)


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
    observations, actions = ct.collect_stale("o/r", utc(12, 0))
    assert any("task-5" in line and "снят" in line for line in actions)
    # #456: живой замок — наблюдение (ничего не изменилось), не действие
    assert any("task-6" in line and "жив" in line for line in observations)
    assert not any("task-6" in line for line in actions)
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
    _, actions = ct.collect_stale("o/r", utc(12, 0))
    assert any("не снят" in line for line in actions)  # не уронил обход, но и не молчит


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
    monkeypatch.setattr(ct, "release_full", lambda *a, **kw: "назначение снято; снят")
    assert ct.main(["x", "release-full", "5"]) == ct.EXIT_OK


def test_cli_release_default_passes_current_holder(monkeypatch):
    # Без --force CLI обязан передать holder=current_holder() в release() —
    # это и есть переключение с «слепого force-снятия» на «снимает только
    # своё» (#1190). Мутация: убери holder=current_holder() из main() — этот
    # тест покраснеет (seen['holder'] останется None).
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("CLAIM_HOLDER", "tree:/work/mine")
    seen = {}

    def fake_release(repo, task, holder=None, force=False):
        seen["holder"] = holder
        seen["force"] = force
        return "снят"

    monkeypatch.setattr(ct, "release", fake_release)
    assert ct.main(["x", "release", "5"]) == ct.EXIT_OK
    assert seen == {"holder": "tree:/work/mine", "force": False}


def test_cli_release_force_skips_holder_check(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    seen = {}

    def fake_release(repo, task):  # старая сигнатура — --force не должен передавать holder
        seen["called"] = True
        return "снят"

    monkeypatch.setattr(ct, "release", fake_release)
    assert ct.main(["x", "release", "5", "--force"]) == ct.EXIT_OK
    assert seen == {"called": True}


def test_cli_release_foreign_lock_error_is_busy_not_broken(monkeypatch):
    # ForeignLockError — это «не смог снять чужое», не «инструмент сломан»:
    # CLI обязан дать EXIT_BUSY (1), а не EXIT_ERROR (2), иначе вызывающий
    # (человек/скрипт) не отличит осмысленный отказ владения от поломки.
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def fake_release(repo, task, holder=None, force=False):
        raise ct.ForeignLockError(f"замок task-{task} принадлежит держателю run:OTHER")

    monkeypatch.setattr(ct, "release", fake_release)
    assert ct.main(["x", "release", "5"]) == ct.EXIT_BUSY


def test_cli_release_full_default_passes_current_holder(monkeypatch):
    # CLI release-full (#1190): без --force обязан передать holder=current_holder()
    # в release_full() — снимает только СВОЙ замок. Мутация: убери holder из
    # main() — этот тест покраснеет (seen['holder'] останется None).
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("CLAIM_HOLDER", "tree:/work/mine")
    seen = {}

    def fake_release_full(repo, task, holder=None):
        seen["holder"] = holder
        return "назначение снято; снят"

    monkeypatch.setattr(ct, "release_full", fake_release_full)
    assert ct.main(["x", "release-full", "5"]) == ct.EXIT_OK
    assert seen == {"holder": "tree:/work/mine"}


def test_cli_release_full_foreign_lock_error_is_busy_not_broken(monkeypatch):
    # ForeignLockError из release_full — это «не смог снять чужое», не
    # «инструмент сломан»: CLI обязан дать EXIT_BUSY (1), а не EXIT_ERROR (2).
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")

    def fake_release_full(repo, task, holder=None):
        raise ct.ForeignLockError(f"замок task-{task} принадлежит держателю run:OTHER")

    monkeypatch.setattr(ct, "release_full", fake_release_full)
    assert ct.main(["x", "release-full", "5"]) == ct.EXIT_BUSY


def test_cli_release_rejects_unknown_flag(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert ct.main(["x", "release", "5", "--wat"]) == ct.EXIT_ERROR


def test_cli_requires_repo_and_valid_task(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert ct.main(["x", "claim", "5"]) == ct.EXIT_ERROR
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert ct.main(["x", "claim", "abc"]) == ct.EXIT_ERROR
    assert ct.main(["x"]) == ct.EXIT_ERROR
